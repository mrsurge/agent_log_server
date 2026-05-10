use crate::{
    conversation_rpc,
    conversation_store::ConversationMetaUpdate,
    state::{AppState, HostUiSnapshot},
};
use als_adapter_protocol::JsonMap;
use anyhow::{Result, anyhow, bail};
use futures_util::FutureExt;
use rust_socketio::{
    Payload, TransportType,
    asynchronous::{Client, ClientBuilder},
};
use serde_json::{Map, Value, json};
use socketioxide::SocketIo;
use std::{
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::{sync::Mutex as AsyncMutex, sync::oneshot, time::timeout};
use tracing::warn;

const SIDEBAR_NAMESPACE: &str = "/sidebar_ipc";
const SIDEBAR_SOCKET_PATH: &str = "/ui_ipc_ws/socket.io/";
const UI_RPC_NAMESPACE: &str = "/rpc/ui";
const CONVERSATIONS_RPC_NAMESPACE: &str = "/rpc/conversations";
const RPC_EVENT: &str = "rpc";
const RPC_NOTIFY_EVENT: &str = "rpc.notify";
const JSONRPC_VERSION: &str = "2.0";

#[derive(Clone, Default)]
pub struct SidebarIpcStore {
    client: Arc<AsyncMutex<Option<Client>>>,
}

impl SidebarIpcStore {
    async fn current(&self) -> Option<Client> {
        self.client.lock().await.clone()
    }

    async fn set(&self, client: Client) {
        *self.client.lock().await = Some(client);
    }

    pub async fn clear(&self) {
        let client = self.client.lock().await.take();
        if let Some(client) = client {
            let _ = client.disconnect().await;
        }
    }
}

pub async fn recheck_status(io: &SocketIo, state: &AppState) -> Value {
    let client = ensure_client(io, state).await.unwrap_or(None);
    let connected = client.is_some();
    let cwd = if let Some(client) = client.as_ref() {
        match query_initial_cwd(client, io, state).await {
            Ok(cwd) => cwd,
            Err(error) => {
                warn!(%error, "sidebar cwd_get failed");
                state.sidebar_ipc.clear().await;
                None
            }
        }
    } else {
        None
    };
    emit_host_ui_updated(io, state).await;
    json!({
        "ok": true,
        "connected": connected,
        "cwd": cwd,
    })
}

pub async fn emit_agent_open(io: &SocketIo, state: &AppState, payload: JsonMap) -> bool {
    let Ok(Some(client)) = ensure_client(io, state).await else {
        return false;
    };
    match sidebar_rpc_call(&client, "sidebar.file.open", Value::Object(payload.clone())).await {
        Ok(value) if !rpc_result_explicitly_not_ok(&value) => return true,
        Ok(value) => {
            warn!(
                ?value,
                "sidebar.file.open RPC returned an unsuccessful result; falling back to legacy event"
            );
        }
        Err(error) => {
            warn!(%error, "sidebar.file.open RPC failed; falling back to legacy event");
        }
    }
    match client
        .emit("sidebar:agent_open", Value::Object(payload))
        .await
    {
        Ok(()) => true,
        Err(error) => {
            warn!(%error, "failed to emit sidebar:agent_open");
            state.sidebar_ipc.clear().await;
            false
        }
    }
}

async fn ensure_client(io: &SocketIo, state: &AppState) -> Result<Option<Client>> {
    if let Some(client) = state.sidebar_ipc.current().await {
        return Ok(Some(client));
    }

    let address = sidebar_address(state);
    let cwd_io = io.clone();
    let cwd_state = state.clone();
    let disconnect_state = state.clone();
    let mention_io = io.clone();
    let mention_state = state.clone();
    let notify_io = io.clone();
    let notify_state = state.clone();

    let client = ClientBuilder::new(address)
        .namespace(SIDEBAR_NAMESPACE)
        .transport_type(TransportType::Websocket)
        .reconnect(false)
        .reconnect_on_disconnect(false)
        .on("sidebar:cwd_set", move |payload, _client| {
            let io = cwd_io.clone();
            let state = cwd_state.clone();
            async move {
                if let Some(cwd) =
                    payload_to_object(payload).and_then(|payload| string_field(&payload, "cwd"))
                {
                    if let Err(error) = update_project_root(&io, &state, cwd).await {
                        warn!(%error, "failed to apply sidebar cwd_set");
                    }
                }
            }
            .boxed()
        })
        .on("sidebar:mention", move |payload, _client| {
            let io = mention_io.clone();
            let state = mention_state.clone();
            async move {
                let Some(payload) = payload_to_object(payload) else {
                    warn!("sidebar mention ignored: payload is not an object");
                    return;
                };
                if let Err(error) = process_mention(&io, &state, payload).await {
                    warn!(%error, "sidebar mention processing failed");
                }
            }
            .boxed()
        })
        .on(RPC_NOTIFY_EVENT, move |payload, _client| {
            let io = notify_io.clone();
            let state = notify_state.clone();
            async move {
                if let Err(error) = process_rpc_notification(&io, &state, payload).await {
                    warn!(%error, "sidebar RPC notification processing failed");
                }
            }
            .boxed()
        })
        .on("disconnect", move |_payload, _client| {
            let state = disconnect_state.clone();
            async move {
                state.sidebar_ipc.clear().await;
            }
            .boxed()
        })
        .on("error", |payload, _client| {
            async move {
                warn!(?payload, "sidebar IPC client error");
            }
            .boxed()
        })
        .connect()
        .await;

    let client = match client {
        Ok(client) => client,
        Err(error) => {
            state.sidebar_ipc.clear().await;
            warn!(%error, "failed to connect to TE2 sidebar IPC");
            return Ok(None);
        }
    };

    state.sidebar_ipc.set(client.clone()).await;
    Ok(Some(client))
}

fn sidebar_address(state: &AppState) -> String {
    format!(
        "{}{}?app_id=file_editor_cm6&source=appserver",
        state.host_ui.te2_base_url(),
        SIDEBAR_SOCKET_PATH
    )
}

async fn query_initial_cwd(
    client: &Client,
    io: &SocketIo,
    state: &AppState,
) -> Result<Option<String>> {
    match sidebar_rpc_call(client, "sidebar.cwd.get", json!({})).await {
        Ok(value) => {
            if let Some(cwd) = cwd_from_value(&value) {
                update_project_root(io, state, cwd.clone()).await?;
                return Ok(Some(cwd));
            }
            return Ok(None);
        }
        Err(error) => {
            warn!(%error, "sidebar.cwd.get RPC failed; falling back to legacy event");
        }
    }

    let (tx, rx) = oneshot::channel::<Option<Value>>();
    let tx = Arc::new(Mutex::new(Some(tx)));
    let tx_for_ack = tx.clone();
    client
        .emit_with_ack(
            "sidebar:cwd_get",
            json!({"source": "codex_agent"}),
            Duration::from_secs(5),
            move |payload, _client| {
                let tx = tx_for_ack.clone();
                async move {
                    if let Ok(mut guard) = tx.lock() {
                        if let Some(sender) = guard.take() {
                            let _ = sender.send(payload_first_value(payload));
                        }
                    }
                }
                .boxed()
            },
        )
        .await
        .map_err(|error| anyhow!(error.to_string()))?;

    let Some(value) = timeout(Duration::from_secs(5), rx).await?? else {
        return Ok(None);
    };
    if let Some(cwd) = cwd_from_value(&value) {
        update_project_root(io, state, cwd.clone()).await?;
        return Ok(Some(cwd));
    }
    Ok(None)
}

async fn sidebar_rpc_call(client: &Client, method: &str, params: Value) -> Result<Value> {
    let request_id = format!("als-rs-sidebar-{}", unix_millis());
    let request = json!({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    });
    let (tx, rx) = oneshot::channel::<Option<Value>>();
    let tx = Arc::new(Mutex::new(Some(tx)));
    let tx_for_ack = tx.clone();
    client
        .emit_with_ack(
            RPC_EVENT,
            request,
            Duration::from_secs(5),
            move |payload, _client| {
                let tx = tx_for_ack.clone();
                async move {
                    if let Ok(mut guard) = tx.lock() {
                        if let Some(sender) = guard.take() {
                            let _ = sender.send(payload_first_value(payload));
                        }
                    }
                }
                .boxed()
            },
        )
        .await
        .map_err(|error| anyhow!(error.to_string()))?;

    let Some(value) = timeout(Duration::from_secs(5), rx).await?? else {
        bail!("sidebar RPC {method} returned no ack payload");
    };
    sidebar_rpc_result(method, value)
}

fn sidebar_rpc_result(method: &str, value: Value) -> Result<Value> {
    let object = value
        .as_object()
        .ok_or_else(|| anyhow!("sidebar RPC {method} ack is not an object"))?;
    if let Some(error) = object.get("error") {
        let message = error
            .as_object()
            .and_then(|error| string_from_object(error, "message"))
            .unwrap_or_else(|| format!("sidebar RPC {method} failed"));
        bail!("{message}");
    }
    Ok(object.get("result").cloned().unwrap_or(Value::Null))
}

fn rpc_result_explicitly_not_ok(value: &Value) -> bool {
    value
        .as_object()
        .and_then(|object| object.get("ok"))
        .and_then(Value::as_bool)
        == Some(false)
}

async fn update_project_root(io: &SocketIo, state: &AppState, cwd: String) -> Result<()> {
    state.host_ui.set_project_root(Some(cwd), true)?;
    emit_host_ui_updated(io, state).await;
    Ok(())
}

async fn process_rpc_notification(io: &SocketIo, state: &AppState, payload: Payload) -> Result<()> {
    let Some(envelope) = payload_to_object(payload) else {
        bail!("sidebar RPC notification payload is not an object");
    };
    let method = string_field(&envelope, "method").ok_or_else(|| anyhow!("missing RPC method"))?;
    let params = envelope
        .get("params")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    match method.as_str() {
        "sidebar.cwd.set" => {
            if let Some(cwd) = string_field(&params, "cwd") {
                update_project_root(io, state, cwd).await?;
            }
        }
        "sidebar.mention" => {
            process_mention(io, state, params).await?;
        }
        _ => {
            warn!(method, "ignored sidebar RPC notification");
        }
    }
    Ok(())
}

async fn emit_host_ui_updated(io: &SocketIo, state: &AppState) {
    let payload = host_ui_response(state);
    emit_rpc_notification(io, UI_RPC_NAMESPACE, "hostUi.updated", payload).await;
}

pub fn host_ui_response(state: &AppState) -> Value {
    let host_ui = state.host_ui.snapshot().unwrap_or_default();
    let selection = state.ui_selection.snapshot().ok();
    let conversation_id = selection
        .as_ref()
        .and_then(|value| value.active_conversation_id.clone());
    json!({
        "ok": true,
        "host_ui": host_ui_json(host_ui),
        "active_view": selection.as_ref().map(|value| value.active_view.as_str()).unwrap_or("splash"),
        "active_conversation": conversation_id.clone(),
        "active_conversation_id": conversation_id.clone(),
        "conversation_id": conversation_id,
        "transport": "rpc",
    })
}

fn host_ui_json(snapshot: HostUiSnapshot) -> Value {
    json!({
        "show_close": snapshot.show_close,
        "parent_origin": snapshot.parent_origin,
        "ide_mode": snapshot.ide_mode,
        "project_root": snapshot.project_root,
    })
}

async fn process_mention(io: &SocketIo, state: &AppState, payload: JsonMap) -> Result<Value> {
    let path = string_field(&payload, "path").ok_or_else(|| anyhow!("missing mention path"))?;
    if path.contains('`') {
        bail!("mention path cannot contain backticks");
    }
    let selection = state.ui_selection.snapshot()?;
    let conversation_id = string_field(&payload, "conversation_id")
        .or(selection.active_conversation_id.clone())
        .ok_or_else(|| anyhow!("no active conversation selected"))?;
    let Some(meta) = state.conversations.load_meta_if_exists(&conversation_id)? else {
        bail!("conversation not found: {conversation_id}");
    };

    let line_no = int_field(&payload, "lineNo");
    let end_line_no = int_field(&payload, "endLineNo");
    let col = int_field(&payload, "col");
    let end_col = int_field(&payload, "endCol");
    let content = truncated_content(payload.get("content"));
    let mut event = JsonMap::new();
    event.insert(
        "type".to_owned(),
        Value::String("mention_insert".to_owned()),
    );
    event.insert("path".to_owned(), Value::String(path.clone()));
    event.insert(
        "conversation_id".to_owned(),
        Value::String(conversation_id.clone()),
    );
    insert_optional_number(&mut event, "lineNo", line_no);
    insert_optional_number(&mut event, "endLineNo", end_line_no);
    insert_optional_number(&mut event, "col", col);
    insert_optional_number(&mut event, "endCol", end_col);
    if let Some(content) = content.as_ref() {
        event.insert("content".to_owned(), Value::String(content.clone()));
    }

    let active_conversation = selection.active_conversation_id.as_deref() == Some(&conversation_id);
    if selection.active_view == "conversation" && active_conversation {
        emit_rpc_notification(
            io,
            CONVERSATIONS_RPC_NAMESPACE,
            "conversation.mention.inserted",
            Value::Object(event),
        )
        .await;
        return Ok(
            json!({"ok": true, "queued": false, "conversation_id": conversation_id, "path": path}),
        );
    }

    let mut draft = meta.draft.unwrap_or_default();
    let token = encode_draft_mention_token(
        &path,
        line_no,
        end_line_no,
        col,
        end_col,
        content.as_deref(),
    );
    if !draft.is_empty() && !draft.ends_with([' ', '\n', '\t']) {
        draft.push(' ');
    }
    draft.push_str(&token);
    state.conversations.update_meta(
        &conversation_id,
        ConversationMetaUpdate {
            draft: Some(draft),
            ..Default::default()
        },
    )?;
    let updated_draft = state
        .conversations
        .load_meta(&conversation_id)?
        .draft
        .unwrap_or_default();
    conversation_rpc::emit_draft_updated(io, &conversation_id, &updated_draft).await;
    Ok(json!({"ok": true, "queued": true, "conversation_id": conversation_id, "path": path}))
}

async fn emit_rpc_notification(io: &SocketIo, namespace: &str, method: &str, params: Value) {
    let notification = json!({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    });
    if let Some(namespace) = io.of(namespace) {
        let _ = namespace.emit(RPC_NOTIFY_EVENT, &notification).await;
    }
}

fn payload_to_object(payload: Payload) -> Option<JsonMap> {
    payload_first_value(payload).and_then(|value| value.as_object().cloned())
}

fn payload_first_value(payload: Payload) -> Option<Value> {
    match payload {
        Payload::Text(values) => values.into_iter().next(),
        Payload::Binary(_) => None,
        _ => None,
    }
}

fn unix_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

fn cwd_from_value(value: &Value) -> Option<String> {
    match value {
        Value::Array(values) => values.iter().find_map(cwd_from_value),
        Value::Object(object) => string_from_object(object, "cwd")
            .or_else(|| object.get("data").and_then(cwd_from_value)),
        _ => None,
    }
}

fn string_field(map: &JsonMap, key: &str) -> Option<String> {
    map.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn string_from_object(map: &Map<String, Value>, key: &str) -> Option<String> {
    map.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn int_field(map: &JsonMap, key: &str) -> Option<i64> {
    match map.get(key) {
        Some(Value::Number(value)) => value
            .as_i64()
            .or_else(|| value.as_u64().map(|value| value as i64)),
        Some(Value::String(value)) => value.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn insert_optional_number(map: &mut JsonMap, key: &str, value: Option<i64>) {
    if let Some(value) = value {
        map.insert(key.to_owned(), Value::Number(value.into()));
    }
}

fn truncated_content(value: Option<&Value>) -> Option<String> {
    let content = value?.as_str()?;
    let mut lines = content.lines();
    let mut kept: Vec<&str> = lines.by_ref().take(20).collect();
    if kept.is_empty() {
        return None;
    }
    let remaining = lines.count();
    if remaining > 0 {
        kept.push("...");
        Some(format!(
            "{}\n... (truncated, {} total lines)",
            kept[..kept.len() - 1].join("\n"),
            kept.len() - 1 + remaining
        ))
    } else {
        Some(kept.join("\n"))
    }
}

fn encode_draft_mention_token(
    path: &str,
    line_no: Option<i64>,
    end_line_no: Option<i64>,
    col: Option<i64>,
    end_col: Option<i64>,
    content: Option<&str>,
) -> String {
    let mut token = format!("`{path}");
    if let Some(line_no) = positive(line_no) {
        token.push_str(&format!(":{line_no}"));
        if let Some(col) = positive(col) {
            token.push_str(&format!(":{col}"));
        }
        if let Some(end_line_no) = positive(end_line_no) {
            token.push_str(&format!("-{end_line_no}"));
            if let Some(end_col) = positive(end_col) {
                token.push_str(&format!(":{end_col}"));
            }
        }
    }
    token.push('`');
    if let Some(content) = content.filter(|value| !value.trim().is_empty()) {
        token.push_str("\n```\n");
        token.push_str(content);
        token.push_str("\n```");
    }
    token
}

fn positive(value: Option<i64>) -> Option<i64> {
    value.filter(|value| *value > 0)
}

#[cfg(test)]
mod tests {
    use super::{
        cwd_from_value, encode_draft_mention_token, rpc_result_explicitly_not_ok,
        sidebar_rpc_result, truncated_content,
    };
    use serde_json::json;

    #[test]
    fn cwd_from_value_accepts_direct_and_wrapped_shapes() {
        assert_eq!(
            cwd_from_value(&json!({"cwd": "/project"})),
            Some("/project".to_owned())
        );
        assert_eq!(
            cwd_from_value(&json!({"data": {"cwd": "/nested"}})),
            Some("/nested".to_owned())
        );
        assert_eq!(
            cwd_from_value(&json!([{"ok": true, "data": {"cwd": "/ack-array"}}])),
            Some("/ack-array".to_owned())
        );
        assert_eq!(
            cwd_from_value(&json!([[{"ok": true, "data": {"cwd": "/nested-ack-array"}}]])),
            Some("/nested-ack-array".to_owned())
        );
        assert_eq!(cwd_from_value(&json!({"data": {"cwd": ""}})), None);
    }

    #[test]
    fn sidebar_rpc_result_extracts_result_or_error() {
        assert_eq!(
            sidebar_rpc_result(
                "sidebar.cwd.get",
                json!({"jsonrpc": "2.0", "id": "1", "result": {"cwd": "/repo"}})
            )
            .expect("result should parse"),
            json!({"cwd": "/repo"})
        );

        let error = sidebar_rpc_result(
            "sidebar.cwd.get",
            json!({
                "jsonrpc": "2.0",
                "id": "1",
                "error": {"code": -32601, "message": "unknown sidebar method"}
            }),
        )
        .expect_err("error response should fail");
        assert!(error.to_string().contains("unknown sidebar method"));
    }

    #[test]
    fn rpc_result_ok_checker_only_rejects_explicit_false() {
        assert!(rpc_result_explicitly_not_ok(&json!({"ok": false})));
        assert!(!rpc_result_explicitly_not_ok(&json!({"ok": true})));
        assert!(!rpc_result_explicitly_not_ok(&json!({"cwd": "/repo"})));
        assert!(!rpc_result_explicitly_not_ok(&json!(null)));
    }

    #[test]
    fn encodes_draft_mention_tokens_like_legacy_python() {
        assert_eq!(
            encode_draft_mention_token("/repo/file.rs", None, None, None, None, None),
            "`/repo/file.rs`"
        );
        assert_eq!(
            encode_draft_mention_token("/repo/file.rs", Some(12), Some(15), Some(4), Some(8), None),
            "`/repo/file.rs:12:4-15:8`"
        );
        assert_eq!(
            encode_draft_mention_token(
                "/repo/file.rs",
                Some(12),
                None,
                None,
                None,
                Some("let x = 1;")
            ),
            "`/repo/file.rs:12`\n```\nlet x = 1;\n```"
        );
    }

    #[test]
    fn truncates_mention_content_to_twenty_lines() {
        let content = (1..=22)
            .map(|value| format!("line {value}"))
            .collect::<Vec<_>>()
            .join("\n");
        let truncated = truncated_content(Some(&json!(content))).expect("content should truncate");
        assert!(truncated.starts_with("line 1\nline 2"));
        assert!(truncated.contains("line 20"));
        assert!(!truncated.contains("line 21"));
        assert!(truncated.ends_with("... (truncated, 22 total lines)"));
    }
}
