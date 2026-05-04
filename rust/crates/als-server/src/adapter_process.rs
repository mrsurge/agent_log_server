use crate::config::ServerConfig;
use als_adapter_protocol::{ExtensionInitializeParams, JsonMap, events, methods};
use als_jsonrpc::{
    ErrorResponse, Notification, Request, RequestId, Response, RpcError, SuccessResponse,
};
use anyhow::{Context, Result, anyhow, bail};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::Value;
use std::{
    collections::{HashMap, VecDeque},
    env,
    ffi::OsString,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicI64, Ordering},
    },
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, broadcast, oneshot},
};
use tracing::{debug, error, warn};

const EVENT_BUFFER_LIMIT: usize = 512;
const EVENT_STREAM_LIMIT: usize = 1024;
const FRAMEWORK_SHELL_ENV_KEYS: &[&str] = &[
    "FRAMEWORK_SHELLS_BASE_DIR",
    "FRAMEWORK_SHELLS_SECRET",
    "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
];

type PendingSender = oneshot::Sender<Result<Value, RpcError>>;
type PendingMap = Arc<Mutex<HashMap<RequestId, PendingSender>>>;

#[derive(Clone)]
pub struct AdapterSupervisor {
    config: ServerConfig,
    events: AdapterEventSink,
    client: Arc<Mutex<Option<Arc<AdapterClient>>>>,
}

impl AdapterSupervisor {
    pub fn new(config: ServerConfig, events: AdapterEventSink) -> Self {
        Self {
            config,
            events,
            client: Arc::new(Mutex::new(None)),
        }
    }

    pub fn events(&self) -> AdapterEventSink {
        self.events.clone()
    }

    pub async fn client(&self) -> Result<Arc<AdapterClient>> {
        let mut guard = self.client.lock().await;
        if let Some(client) = guard.as_ref() {
            return Ok(client.clone());
        }

        let client = Arc::new(AdapterClient::spawn(
            self.config.adapters.copilot_python.clone(),
            self.config.extensions_dir.parent().map(Path::to_path_buf),
            self.events.clone(),
        )?);
        *guard = Some(client.clone());
        Ok(client)
    }

    pub async fn initialize_extension(&self, extension_id: &str) -> Result<Value> {
        let params = ExtensionInitializeParams {
            extension_id: extension_id.to_owned(),
            extensions_dir: Some(self.config.extensions_dir.clone()),
            cwd: self
                .config
                .extensions_dir
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| {
                    std::env::current_dir().unwrap_or_else(|_| self.config.roots.data_dir.clone())
                }),
            data_dir: self.config.roots.data_dir.clone(),
            cache_dir: self.config.roots.cache_dir.clone(),
            config_dir: self.config.roots.config_dir.clone(),
            settings: JsonMap::new(),
        };
        self.client()
            .await?
            .request_value(methods::EXTENSION_INITIALIZE, params)
            .await
    }

    pub async fn initialize_copilot(&self) -> Result<Value> {
        self.initialize_extension("copilot-sdk").await
    }
}

pub struct AdapterClient {
    stdin: Arc<Mutex<ChildStdin>>,
    pending: PendingMap,
    next_id: AtomicI64,
    _child: Child,
}

impl AdapterClient {
    pub fn spawn(
        python: String,
        python_path_root: Option<PathBuf>,
        events: AdapterEventSink,
    ) -> Result<Self> {
        let mut command = Command::new(&python);
        command
            .arg("-m")
            .arg("agent_log_server_rs.adapters.extension_adapter")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .kill_on_drop(true);
        if let Some(root) = python_path_root.as_deref() {
            if let Some(pythonpath) = pythonpath_with_root(root, env::var_os("PYTHONPATH")) {
                command.env("PYTHONPATH", pythonpath);
            }
        }
        apply_framework_shell_env(&mut command);
        let mut child = command
            .spawn()
            .with_context(|| format!("failed to spawn extension adapter via {python}"))?;

        let stdin = child.stdin.take().context("adapter stdin is unavailable")?;
        let stdout = child
            .stdout
            .take()
            .context("adapter stdout is unavailable")?;
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(read_adapter_stdout(stdout, pending.clone(), events));

        Ok(Self {
            stdin: Arc::new(Mutex::new(stdin)),
            pending,
            next_id: AtomicI64::new(1),
            _child: child,
        })
    }

    pub async fn request_value<P>(&self, method: &str, params: P) -> Result<Value>
    where
        P: Serialize,
    {
        let result = self
            .request_raw(method, serde_json::to_value(params)?)
            .await?;
        Ok(result)
    }

    pub async fn request<T, P>(&self, method: &str, params: P) -> Result<T>
    where
        T: DeserializeOwned,
        P: Serialize,
    {
        let value = self.request_value(method, params).await?;
        Ok(serde_json::from_value(value)?)
    }

    async fn request_raw(&self, method: &str, params: Value) -> Result<Value> {
        let id = RequestId::Number(self.next_id.fetch_add(1, Ordering::Relaxed));
        let request = Request::new(id.clone(), method, Some(params));
        let line = serde_json::to_string(&request)?;
        let (sender, receiver) = oneshot::channel();

        self.pending.lock().await.insert(id.clone(), sender);
        if let Err(err) = self.write_line(&line).await {
            self.pending.lock().await.remove(&id);
            return Err(err);
        }

        match receiver.await.context("adapter response channel closed")? {
            Ok(result) => Ok(result),
            Err(error) => Err(anyhow!(
                "adapter RPC error {}: {}",
                error.code,
                error.message
            )),
        }
    }

    async fn write_line(&self, line: &str) -> Result<()> {
        let mut stdin = self.stdin.lock().await;
        stdin.write_all(line.as_bytes()).await?;
        stdin.write_all(b"\n").await?;
        stdin.flush().await?;
        Ok(())
    }
}

fn pythonpath_with_root(root: &Path, existing: Option<OsString>) -> Option<OsString> {
    let mut paths = vec![root.to_path_buf()];
    if let Some(existing) = existing {
        paths.extend(env::split_paths(&existing));
    }
    env::join_paths(paths).ok()
}

fn apply_framework_shell_env(command: &mut Command) {
    for (key, value) in framework_shell_env_overrides() {
        command.env(key, value);
    }
}

fn framework_shell_env_overrides() -> Vec<(&'static str, OsString)> {
    framework_shell_env_overrides_from(|key| env::var_os(key))
}

fn framework_shell_env_overrides_from(
    get_env: impl Fn(&str) -> Option<OsString>,
) -> Vec<(&'static str, OsString)> {
    let mut values = Vec::new();
    for key in FRAMEWORK_SHELL_ENV_KEYS {
        if let Some(value) = get_env(key) {
            values.push((*key, value));
        }
    }
    if let Some(value) = get_env("FRAMEWORK_SHELLS_REPO_FINGERPRINT")
        .or_else(|| get_env("FRAMEWORK_SHELLS_SECRET_FINGERPRINT"))
    {
        values.push(("FRAMEWORK_SHELLS_SECRET_FINGERPRINT", value));
    }
    values
}

async fn read_adapter_stdout(
    stdout: tokio::process::ChildStdout,
    pending: PendingMap,
    events: AdapterEventSink,
) {
    let mut lines = BufReader::new(stdout).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                if let Err(err) = handle_adapter_line(&line, &pending, &events).await {
                    warn!(error = %err, "failed to handle adapter JSON-RPC line");
                }
            }
            Ok(None) => {
                fail_all_pending(&pending, "adapter stdout closed").await;
                break;
            }
            Err(err) => {
                error!(error = %err, "adapter stdout read failed");
                fail_all_pending(&pending, "adapter stdout read failed").await;
                break;
            }
        }
    }
}

async fn handle_adapter_line(
    line: &str,
    pending: &PendingMap,
    events: &AdapterEventSink,
) -> Result<()> {
    let value: Value = serde_json::from_str(line).context("invalid adapter JSON")?;
    if value.get("id").is_some() && (value.get("result").is_some() || value.get("error").is_some())
    {
        handle_response(value, pending).await
    } else if value.get("method").is_some() {
        handle_notification(value, events).await
    } else {
        bail!("adapter message is neither response nor notification")
    }
}

async fn handle_response(value: Value, pending: &PendingMap) -> Result<()> {
    let response: Response = serde_json::from_value(value)?;
    let (id, result) = match response {
        Response::Success(SuccessResponse { id, result, .. }) => (id, Ok(result)),
        Response::Error(ErrorResponse { id, error, .. }) => (id, Err(error)),
    };
    let Some(sender) = pending.lock().await.remove(&id) else {
        debug!(?id, "dropping response for unknown adapter request");
        return Ok(());
    };
    let _ = sender.send(result);
    Ok(())
}

async fn handle_notification(value: Value, events: &AdapterEventSink) -> Result<()> {
    let notification: Notification = serde_json::from_value(value)?;
    let params = notification.params.unwrap_or(Value::Null);
    match notification.method.as_str() {
        events::LIVE_EVENT => events.push_live(params).await,
        events::TRANSCRIPT_RECORD => events.push_transcript(params).await,
        _ => events.push_other(notification.method, params).await,
    }
    Ok(())
}

async fn fail_all_pending(pending: &PendingMap, message: &str) {
    let mut guard = pending.lock().await;
    for (_, sender) in guard.drain() {
        let _ = sender.send(Err(RpcError::new(-32000, message, None)));
    }
}

#[derive(Clone)]
pub struct AdapterEventSink {
    inner: Arc<Mutex<AdapterEventSinkInner>>,
    stream: broadcast::Sender<AdapterCapturedEvent>,
}

impl Default for AdapterEventSink {
    fn default() -> Self {
        let (stream, _) = broadcast::channel(EVENT_STREAM_LIMIT);
        Self {
            inner: Arc::new(Mutex::new(AdapterEventSinkInner::default())),
            stream,
        }
    }
}

#[derive(Default)]
struct AdapterEventSinkInner {
    live: VecDeque<Value>,
    transcript: VecDeque<Value>,
    other: VecDeque<AdapterOtherEvent>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AdapterEventSnapshot {
    pub live: Vec<Value>,
    pub transcript: Vec<Value>,
    pub other: Vec<AdapterOtherEvent>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AdapterOtherEvent {
    pub method: String,
    pub params: Value,
}

#[derive(Clone, Debug)]
pub enum AdapterCapturedEvent {
    Live(Value),
    Transcript(Value),
    Other(AdapterOtherEvent),
}

impl AdapterEventSink {
    async fn push_live(&self, value: Value) {
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.live, value.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Live(value));
    }

    async fn push_transcript(&self, value: Value) {
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.transcript, value.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Transcript(value));
    }

    async fn push_other(&self, method: String, params: Value) {
        let event = AdapterOtherEvent { method, params };
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.other, event.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Other(event));
    }

    pub async fn snapshot(&self) -> AdapterEventSnapshot {
        let guard = self.inner.lock().await;
        AdapterEventSnapshot {
            live: guard.live.iter().cloned().collect(),
            transcript: guard.transcript.iter().cloned().collect(),
            other: guard.other.iter().cloned().collect(),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<AdapterCapturedEvent> {
        self.stream.subscribe()
    }
}

fn push_capped<T>(items: &mut VecDeque<T>, item: T) {
    if items.len() >= EVENT_BUFFER_LIMIT {
        items.pop_front();
    }
    items.push_back(item);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn correlates_success_response_by_request_id() {
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        let (sender, receiver) = oneshot::channel();
        pending.lock().await.insert(RequestId::Number(7), sender);

        handle_adapter_line(
            r#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#,
            &pending,
            &AdapterEventSink::default(),
        )
        .await
        .unwrap();

        let result = receiver.await.unwrap().unwrap();
        assert_eq!(result, json!({"ok": true}));
        assert!(pending.lock().await.is_empty());
    }

    #[tokio::test]
    async fn captures_live_and_transcript_notifications() {
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        let sink = AdapterEventSink::default();
        let mut stream = sink.subscribe();

        handle_adapter_line(
            r#"{"jsonrpc":"2.0","method":"event.live","params":{"type":"assistant_delta","delta":"pong"}}"#,
            &pending,
            &sink,
        )
        .await
        .unwrap();
        handle_adapter_line(
            r#"{"jsonrpc":"2.0","method":"event.transcript_record","params":{"role":"assistant","text":"pong"}}"#,
            &pending,
            &sink,
        )
        .await
        .unwrap();

        let snapshot = sink.snapshot().await;
        assert_eq!(
            snapshot.live,
            vec![json!({"type": "assistant_delta", "delta": "pong"})]
        );
        assert_eq!(
            snapshot.transcript,
            vec![json!({"role": "assistant", "text": "pong"})]
        );
        assert!(snapshot.other.is_empty());

        match stream.recv().await.unwrap() {
            AdapterCapturedEvent::Live(value) => {
                assert_eq!(value, json!({"type": "assistant_delta", "delta": "pong"}));
            }
            other => panic!("unexpected adapter event: {other:?}"),
        }
        match stream.recv().await.unwrap() {
            AdapterCapturedEvent::Transcript(value) => {
                assert_eq!(value, json!({"role": "assistant", "text": "pong"}));
            }
            other => panic!("unexpected adapter event: {other:?}"),
        }
    }

    #[test]
    fn framework_shell_env_maps_repo_fingerprint_to_secret_fingerprint() {
        let values = HashMap::from([
            (
                "FRAMEWORK_SHELLS_BASE_DIR",
                OsString::from("/example/framework_shells"),
            ),
            ("FRAMEWORK_SHELLS_SECRET", OsString::from("secret")),
            (
                "FRAMEWORK_SHELLS_REPO_FINGERPRINT",
                OsString::from("repo-fingerprint"),
            ),
            (
                "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
                OsString::from("123"),
            ),
        ]);
        let overrides = framework_shell_env_overrides_from(|key| values.get(key).cloned());

        assert_eq!(
            overrides,
            vec![
                (
                    "FRAMEWORK_SHELLS_BASE_DIR",
                    OsString::from("/example/framework_shells")
                ),
                ("FRAMEWORK_SHELLS_SECRET", OsString::from("secret")),
                (
                    "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
                    OsString::from("123")
                ),
                (
                    "FRAMEWORK_SHELLS_SECRET_FINGERPRINT",
                    OsString::from("repo-fingerprint")
                ),
            ]
        );
    }
}
