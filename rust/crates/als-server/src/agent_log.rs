use crate::state::AppState;
use anyhow::{Context, Result, anyhow};
use axum::{
    Json, Router,
    extract::{
        Path, Query, State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State as SioState},
};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::broadcast;
use tracing::warn;

const APPSERVER_NAMESPACE: &str = "/appserver";
const AGENT_LOG_FILE: &str = "agent_chat.log.jsonl";

#[derive(Clone)]
pub struct AgentLogStore {
    inner: Arc<Mutex<AgentLogInner>>,
    tx: broadcast::Sender<Value>,
}

#[derive(Debug)]
struct AgentLogInner {
    path: PathBuf,
    next_msg_num: i64,
}

impl AgentLogStore {
    pub fn with_cache_dir(cache_dir: PathBuf) -> Self {
        let path = cache_dir.join("agent-log").join(AGENT_LOG_FILE);
        let mut inner = AgentLogInner {
            path,
            next_msg_num: 1,
        };
        if let Err(error) = initialize_inner(&mut inner) {
            warn!(%error, path = %inner.path.display(), "failed to initialize ALS-RS agent log");
        }
        let (tx, _) = broadcast::channel(256);
        Self {
            inner: Arc::new(Mutex::new(inner)),
            tx,
        }
    }

    pub fn append_record(&self, mut record: Map<String, Value>) -> Result<Map<String, Value>> {
        {
            let mut inner = self
                .inner
                .lock()
                .map_err(|_| anyhow!("agent log lock poisoned"))?;
            initialize_inner(&mut inner)?;
            match record.get("msg_num").and_then(Value::as_i64) {
                Some(msg_num) => {
                    if msg_num >= inner.next_msg_num {
                        inner.next_msg_num = msg_num + 1;
                    }
                }
                None => {
                    record.insert("msg_num".to_owned(), Value::from(inner.next_msg_num));
                    inner.next_msg_num += 1;
                }
            }
            append_json_line(&inner.path, &record)?;
        }
        let _ = self.tx.send(Value::Object(record.clone()));
        Ok(record)
    }

    pub fn read_records(&self, limit: Option<usize>) -> Result<Vec<Value>> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent log lock poisoned"))?;
        let records = read_records_from_path(&inner.path)?;
        if let Some(limit) = limit.filter(|limit| *limit > 0) {
            let start = records.len().saturating_sub(limit);
            return Ok(records[start..].to_vec());
        }
        Ok(records)
    }

    pub fn get_record_by_msg_num(&self, msg_num: i64) -> Result<Option<Value>> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent log lock poisoned"))?;
        for record in read_records_from_path(&inner.path)? {
            if record
                .as_object()
                .and_then(|object| object.get("msg_num"))
                .and_then(Value::as_i64)
                == Some(msg_num)
            {
                return Ok(Some(record));
            }
        }
        Ok(None)
    }

    pub fn delete_record_by_msg_num(&self, msg_num: i64) -> Result<bool> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent log lock poisoned"))?;
        let records = read_records_from_path(&inner.path)?;
        let mut found = false;
        let mut kept = Vec::with_capacity(records.len());
        for record in records {
            if record
                .as_object()
                .and_then(|object| object.get("msg_num"))
                .and_then(Value::as_i64)
                == Some(msg_num)
            {
                found = true;
            } else {
                kept.push(record);
            }
        }
        if found {
            write_records(&inner.path, &kept)?;
        }
        Ok(found)
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.tx.subscribe()
    }

    #[cfg(test)]
    fn log_path(&self) -> Result<PathBuf> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| anyhow!("agent log lock poisoned"))?;
        Ok(inner.path.clone())
    }
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/api/messages",
            get(api_get_messages).post(api_post_message),
        )
        .route(
            "/api/messages/{msg_num}",
            get(api_get_message_by_num).delete(api_delete_message_by_num),
        )
        .route("/api/messages/await", post(api_await_message))
        .route("/ws", get(websocket_endpoint))
}

pub fn register_appserver_namespace(io: &SocketIo) {
    io.ns(
        APPSERVER_NAMESPACE,
        async |socket: SocketRef, SioState(_state): SioState<AppState>| {
            socket.on("get_log_messages", sio_get_log_messages);
            socket.on("post_log_message", sio_post_log_message);
        },
    );
}

pub fn start_socketio_fanout(io: SocketIo, state: AppState) {
    let mut rx = state.agent_log.subscribe();
    tokio::spawn(async move {
        loop {
            match rx.recv().await {
                Ok(record) => {
                    let Some(namespace) = io.of(APPSERVER_NAMESPACE) else {
                        continue;
                    };
                    if let Err(error) = namespace.emit("agent_log_message", &record).await {
                        warn!(%error, "failed to emit ALS-RS agent_log_message");
                    }
                }
                Err(broadcast::error::RecvError::Lagged(count)) => {
                    warn!(count, "ALS-RS agent-log Socket.IO fanout lagged");
                }
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    });
}

#[derive(Deserialize)]
struct MessagesQuery {
    limit: Option<usize>,
}

#[derive(Deserialize)]
struct MessageIn {
    who: String,
    message: String,
}

#[derive(Deserialize)]
struct AwaitIn {
    after_msg_num: i64,
    from_who: Option<String>,
    timeout_ms: Option<u64>,
}

async fn api_get_messages(
    State(state): State<AppState>,
    Query(query): Query<MessagesQuery>,
) -> Result<Json<Vec<Value>>, AgentLogRouteError> {
    Ok(Json(state.agent_log.read_records(query.limit)?))
}

async fn api_get_message_by_num(
    State(state): State<AppState>,
    Path(msg_num): Path<i64>,
) -> Result<Json<Value>, AgentLogRouteError> {
    state
        .agent_log
        .get_record_by_msg_num(msg_num)?
        .map(Json)
        .ok_or_else(|| AgentLogRouteError::not_found(format!("Message {msg_num} not found")))
}

async fn api_delete_message_by_num(
    State(state): State<AppState>,
    Path(msg_num): Path<i64>,
) -> Result<Json<Value>, AgentLogRouteError> {
    if state.agent_log.delete_record_by_msg_num(msg_num)? {
        Ok(Json(json!({"ok": true, "deleted": msg_num})))
    } else {
        Err(AgentLogRouteError::not_found(format!(
            "Message {msg_num} not found"
        )))
    }
}

async fn api_post_message(
    State(state): State<AppState>,
    Json(input): Json<MessageIn>,
) -> Result<(StatusCode, Json<Value>), AgentLogRouteError> {
    let who = input.who.trim();
    let message = input.message.trim();
    if who.is_empty() || message.is_empty() {
        return Err(AgentLogRouteError::bad_request(
            "Both 'who' and 'message' are required",
        ));
    }
    let mut record = Map::new();
    record.insert("ts".to_owned(), Value::String(utc_ts()));
    record.insert("who".to_owned(), Value::String(who.to_owned()));
    record.insert("message".to_owned(), Value::String(message.to_owned()));
    let record = state.agent_log.append_record(record)?;
    Ok((StatusCode::CREATED, Json(Value::Object(record))))
}

async fn api_await_message(
    State(state): State<AppState>,
    Json(input): Json<AwaitIn>,
) -> Result<Json<Value>, AgentLogRouteError> {
    let from_who = input
        .from_who
        .as_ref()
        .map(|value| value.trim())
        .filter(|value| !value.is_empty());
    let timeout = Duration::from_millis(input.timeout_ms.unwrap_or(180_000).clamp(1, 600_000));
    let poll_interval = Duration::from_millis(500);
    let start = tokio::time::Instant::now();

    while start.elapsed() < timeout {
        if let Some(record) = find_await_record(&state, input.after_msg_num, from_who)? {
            return Ok(Json(record));
        }
        tokio::time::sleep(poll_interval).await;
    }
    Err(AgentLogRouteError {
        status: StatusCode::REQUEST_TIMEOUT,
        message: "timeout".to_owned(),
        extra: json!({"after_msg_num": input.after_msg_num}),
    })
}

async fn websocket_endpoint(
    State(state): State<AppState>,
    ws: WebSocketUpgrade,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| websocket_loop(socket, state.agent_log.subscribe()))
}

async fn websocket_loop(socket: WebSocket, mut rx: broadcast::Receiver<Value>) {
    let (mut sender, mut receiver) = socket.split();
    loop {
        tokio::select! {
            incoming = receiver.next() => {
                match incoming {
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {}
                    Some(Err(_)) => break,
                }
            }
            broadcast = rx.recv() => {
                match broadcast {
                    Ok(record) => {
                        let Ok(text) = serde_json::to_string(&record) else {
                            continue;
                        };
                        if sender.send(Message::Text(text.into())).await.is_err() {
                            break;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(_)) => {}
                    Err(broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }
}

async fn sio_get_log_messages(
    SioState(state): SioState<AppState>,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let limit = data
        .as_object()
        .and_then(|payload| payload.get("limit"))
        .and_then(coerce_usize);
    let response = state
        .agent_log
        .read_records(limit)
        .map(Value::Array)
        .unwrap_or_else(|error| json!({"__error": error.to_string()}));
    let _ = ack.send(&response);
}

async fn sio_post_log_message(
    SioState(state): SioState<AppState>,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let payload = data.as_object().cloned().unwrap_or_default();
    let who = payload
        .get("who")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    let message = payload
        .get("message")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    if who.is_empty() || message.is_empty() {
        let _ = ack.send(&json!({"__error": "Both 'who' and 'message' are required"}));
        return;
    }
    let mut record = Map::new();
    record.insert("ts".to_owned(), Value::String(utc_ts()));
    record.insert("who".to_owned(), Value::String(who.to_owned()));
    record.insert("message".to_owned(), Value::String(message.to_owned()));
    let response = state
        .agent_log
        .append_record(record)
        .map(Value::Object)
        .unwrap_or_else(|error| json!({"__error": error.to_string()}));
    let _ = ack.send(&response);
}

fn find_await_record(
    state: &AppState,
    after_msg_num: i64,
    from_who: Option<&str>,
) -> Result<Option<Value>> {
    for record in state.agent_log.read_records(None)? {
        let Some(object) = record.as_object() else {
            continue;
        };
        let rec_num = object.get("msg_num").and_then(Value::as_i64);
        if rec_num.is_none_or(|rec_num| rec_num <= after_msg_num) {
            continue;
        }
        if let Some(from_who) = from_who {
            if object.get("who").and_then(Value::as_str) != Some(from_who) {
                continue;
            }
        }
        return Ok(Some(record));
    }
    Ok(None)
}

fn initialize_inner(inner: &mut AgentLogInner) -> Result<()> {
    if let Some(parent) = inner.path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create agent-log dir {}", parent.display()))?;
    }
    if !inner.path.exists() {
        fs::write(&inner.path, "")
            .with_context(|| format!("failed to create agent log {}", inner.path.display()))?;
    }
    let mut records = read_records_from_path(&inner.path)?;
    if records.iter().any(|record| {
        record
            .as_object()
            .is_some_and(|object| !object.contains_key("msg_num"))
    }) {
        for (index, record) in records.iter_mut().enumerate() {
            if let Some(object) = record.as_object_mut() {
                object.insert("msg_num".to_owned(), Value::from(index as i64 + 1));
            }
        }
        write_records(&inner.path, &records)?;
        inner.next_msg_num = records.len() as i64 + 1;
        return Ok(());
    }

    inner.next_msg_num = records
        .iter()
        .filter_map(|record| record.as_object()?.get("msg_num")?.as_i64())
        .max()
        .unwrap_or(0)
        + 1;
    Ok(())
}

fn read_records_from_path(path: &PathBuf) -> Result<Vec<Value>> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let file = fs::File::open(path)
        .with_context(|| format!("failed to open agent log {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut records = Vec::new();
    for line in reader.lines() {
        let line = line.with_context(|| format!("failed to read agent log {}", path.display()))?;
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(_)) => records.push(serde_json::from_str(line)?),
            Ok(_) | Err(_) => {}
        }
    }
    Ok(records)
}

fn append_json_line(path: &PathBuf, record: &Map<String, Value>) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .with_context(|| format!("failed to open agent log {}", path.display()))?;
    writeln!(file, "{}", serde_json::to_string(record)?)
        .with_context(|| format!("failed to write agent log {}", path.display()))?;
    file.flush()
        .with_context(|| format!("failed to flush agent log {}", path.display()))?;
    Ok(())
}

fn write_records(path: &PathBuf, records: &[Value]) -> Result<()> {
    let mut file = fs::File::create(path)
        .with_context(|| format!("failed to rewrite agent log {}", path.display()))?;
    for record in records {
        writeln!(file, "{}", serde_json::to_string(record)?)
            .with_context(|| format!("failed to rewrite agent log {}", path.display()))?;
    }
    Ok(())
}

fn coerce_usize(value: &Value) -> Option<usize> {
    if let Some(value) = value.as_u64() {
        return usize::try_from(value).ok();
    }
    if let Some(value) = value.as_str() {
        return value.trim().parse::<usize>().ok();
    }
    None
}

fn utc_ts() -> String {
    format!("unix_ms:{}", unix_millis())
}

fn unix_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

struct AgentLogRouteError {
    status: StatusCode,
    message: String,
    extra: Value,
}

impl AgentLogRouteError {
    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: message.into(),
            extra: Value::Null,
        }
    }

    fn not_found(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message: message.into(),
            extra: Value::Null,
        }
    }
}

impl From<anyhow::Error> for AgentLogRouteError {
    fn from(error: anyhow::Error) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: error.to_string(),
            extra: Value::Null,
        }
    }
}

impl IntoResponse for AgentLogRouteError {
    fn into_response(self) -> Response {
        let mut payload = Map::new();
        payload.insert("error".to_owned(), Value::String(self.message));
        if let Some(extra) = self.extra.as_object() {
            for (key, value) in extra {
                payload.insert(key.clone(), value.clone());
            }
        }
        (self.status, Json(Value::Object(payload))).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn append_assigns_stable_message_numbers() {
        let root = unique_temp_root("agent-log-numbering");
        let store = AgentLogStore::with_cache_dir(root.clone());

        let first = store.append_record(record("Ada", "hello")).unwrap();
        let second = store.append_record(record("Linus", "hi")).unwrap();

        assert_eq!(first.get("msg_num").and_then(Value::as_i64), Some(1));
        assert_eq!(second.get("msg_num").and_then(Value::as_i64), Some(2));
        assert_eq!(store.read_records(None).unwrap().len(), 2);

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rewrites_legacy_records_missing_message_numbers() {
        let root = unique_temp_root("agent-log-rewrite");
        let store = AgentLogStore::with_cache_dir(root.clone());
        let path = store.log_path().unwrap();
        fs::write(
            &path,
            "{\"ts\":\"old\",\"who\":\"Ada\",\"message\":\"hello\"}\n{\"ts\":\"old\",\"who\":\"Linus\",\"message\":\"hi\"}\n",
        )
        .unwrap();

        let reloaded = AgentLogStore::with_cache_dir(root.clone());
        let records = reloaded.read_records(None).unwrap();
        assert_eq!(
            records[0]
                .as_object()
                .and_then(|object| object.get("msg_num"))
                .and_then(Value::as_i64),
            Some(1)
        );
        let next = reloaded.append_record(record("Grace", "next")).unwrap();
        assert_eq!(next.get("msg_num").and_then(Value::as_i64), Some(3));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn delete_removes_record_without_renumbering() {
        let root = unique_temp_root("agent-log-delete");
        let store = AgentLogStore::with_cache_dir(root.clone());
        store.append_record(record("Ada", "one")).unwrap();
        store.append_record(record("Ada", "two")).unwrap();

        assert!(store.delete_record_by_msg_num(1).unwrap());
        assert!(!store.delete_record_by_msg_num(99).unwrap());
        let records = store.read_records(None).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(
            records[0]
                .as_object()
                .and_then(|object| object.get("msg_num"))
                .and_then(Value::as_i64),
            Some(2)
        );

        let _ = fs::remove_dir_all(root);
    }

    #[tokio::test]
    async fn append_broadcasts_records_to_subscribers() {
        let root = unique_temp_root("agent-log-broadcast");
        let store = AgentLogStore::with_cache_dir(root.clone());
        let mut rx = store.subscribe();
        store.append_record(record("Ada", "hello")).unwrap();

        let event = rx.recv().await.unwrap();
        assert_eq!(
            event
                .as_object()
                .and_then(|object| object.get("message"))
                .and_then(Value::as_str),
            Some("hello")
        );

        let _ = fs::remove_dir_all(root);
    }

    fn record(who: &str, message: &str) -> Map<String, Value> {
        let mut record = Map::new();
        record.insert("ts".to_owned(), Value::String("test".to_owned()));
        record.insert("who".to_owned(), Value::String(who.to_owned()));
        record.insert("message".to_owned(), Value::String(message.to_owned()));
        record
    }

    fn unique_temp_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "als-rs-{name}-{}-{}",
            std::process::id(),
            unix_millis()
        ))
    }
}
