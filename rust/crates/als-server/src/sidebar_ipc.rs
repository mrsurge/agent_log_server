use crate::{
    conversation_rpc,
    conversation_store::ConversationMetaUpdate,
    state::{AppState, FocusedWindowSnapshot, HostUiSnapshot},
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
use tracing::{info, warn};

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
        warn!(
            namespace = SIDEBAR_NAMESPACE,
            socket_path = SIDEBAR_SOCKET_PATH,
            event = RPC_EVENT,
            method = "sidebar.file.open",
            payload = ?payload,
            "cannot send sidebar.file.open RPC because sidebar IPC client is unavailable"
        );
        return false;
    };
    match sidebar_rpc_call(&client, "sidebar.file.open", Value::Object(payload.clone())).await {
        Ok(value) if !rpc_result_explicitly_not_ok(&value) => true,
        Ok(value) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.file.open",
                payload = ?payload,
                ?value,
                "sidebar.file.open RPC returned an unsuccessful result"
            );
            false
        }
        Err(error) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.file.open",
                payload = ?payload,
                %error,
                "sidebar.file.open RPC failed"
            );
            false
        }
    }
}

pub async fn emit_agent_edit(io: &SocketIo, state: &AppState, payload: JsonMap) -> bool {
    let Ok(Some(client)) = ensure_client(io, state).await else {
        return false;
    };
    match sidebar_rpc_call(&client, "sidebar.file.edit", Value::Object(payload.clone())).await {
        Ok(value) if !rpc_result_explicitly_not_ok(&value) => true,
        Ok(value) => {
            warn!(
                ?value,
                "sidebar.file.edit RPC returned an unsuccessful result"
            );
            false
        }
        Err(error) => {
            warn!(%error, "sidebar.file.edit RPC failed");
            false
        }
    }
}

pub async fn publish_agent_edits(io: &SocketIo, state: &AppState, payload: JsonMap) -> bool {
    let Ok(Some(client)) = ensure_client(io, state).await else {
        warn!(
            namespace = SIDEBAR_NAMESPACE,
            socket_path = SIDEBAR_SOCKET_PATH,
            event = RPC_EVENT,
            method = "sidebar.agentEdits.publish",
            payload = ?payload,
            "cannot send sidebar.agentEdits.publish RPC because sidebar IPC client is unavailable"
        );
        return false;
    };
    publish_agent_edits_with_client(&client, payload).await
}

async fn publish_agent_edits_with_current_client(state: &AppState, payload: JsonMap) -> bool {
    let Some(client) = state.sidebar_ipc.current().await else {
        warn!(
            namespace = SIDEBAR_NAMESPACE,
            socket_path = SIDEBAR_SOCKET_PATH,
            event = RPC_EVENT,
            method = "sidebar.agentEdits.publish",
            payload = ?payload,
            "cannot send sidebar.agentEdits.publish RPC because sidebar IPC client is unavailable"
        );
        return false;
    };
    publish_agent_edits_with_client(&client, payload).await
}

async fn publish_agent_edits_with_client(client: &Client, payload: JsonMap) -> bool {
    match sidebar_rpc_call(
        client,
        "sidebar.agentEdits.publish",
        Value::Object(payload.clone()),
    )
    .await
    {
        Ok(value) if !rpc_result_explicitly_not_ok(&value) => true,
        Ok(value) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.agentEdits.publish",
                payload = ?payload,
                ?value,
                "sidebar.agentEdits.publish RPC returned an unsuccessful result"
            );
            false
        }
        Err(error) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.agentEdits.publish",
                payload = ?payload,
                %error,
                "sidebar.agentEdits.publish RPC failed"
            );
            false
        }
    }
}

pub async fn proxy_sidebar_rpc(
    io: &SocketIo,
    state: &AppState,
    method: &str,
    params: Value,
) -> Result<Value> {
    let Some(client) = ensure_client(io, state).await? else {
        bail!("sidebar_unavailable");
    };
    sidebar_rpc_call(&client, method, params).await
}

pub async fn te2_project_status(io: &SocketIo, state: &AppState, params: JsonMap) -> Value {
    let Some(target_path) = string_field(&params, "path") else {
        return json!({
            "ok": false,
            "connected": false,
            "action": "disabled",
            "reason": "missing_path",
        });
    };
    let current_cwd = state
        .host_ui
        .snapshot()
        .ok()
        .and_then(|snapshot| snapshot.project_root);
    if current_cwd
        .as_deref()
        .is_some_and(|cwd| same_logical_path(cwd, &target_path))
    {
        return json!({
            "ok": true,
            "connected": true,
            "target_path": target_path,
            "current_cwd": current_cwd,
            "matches_current": true,
            "action": "current",
            "lookup": Value::Null,
        });
    }
    let Ok(client) = ensure_client(io, state).await else {
        return te2_project_status_unavailable(&target_path, current_cwd, "connect_failed");
    };
    let Some(client) = client else {
        return te2_project_status_unavailable(&target_path, current_cwd, "sidebar_unavailable");
    };

    match sidebar_rpc_call(
        &client,
        "sidebar.project.lookup",
        json!({ "path": target_path }),
    )
    .await
    {
        Ok(lookup) => {
            let known = lookup
                .get("known")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let reason = lookup.get("reason").cloned().unwrap_or(Value::Null);
            json!({
                "ok": true,
                "connected": true,
                "target_path": target_path,
                "current_cwd": current_cwd,
                "matches_current": false,
                "known": known,
                "reason": reason,
                "action": if known { "switch" } else { "create" },
                "lookup": lookup,
            })
        }
        Err(error) => {
            warn!(%error, "sidebar.project.lookup RPC failed");
            state.sidebar_ipc.clear().await;
            te2_project_status_unavailable(&target_path, current_cwd, "lookup_failed")
        }
    }
}

pub async fn te2_project_open(io: &SocketIo, state: &AppState, params: JsonMap) -> Value {
    let Some(target_path) = string_field(&params, "path") else {
        return json!({"ok": false, "error": "missing_path"});
    };
    let Ok(Some(client)) = ensure_client(io, state).await else {
        return json!({"ok": false, "error": "sidebar_unavailable", "path": target_path});
    };
    match sidebar_rpc_call(
        &client,
        "sidebar.project.open",
        json!({ "path": target_path }),
    )
    .await
    {
        Ok(result) if !rpc_result_explicitly_not_ok(&result) => {
            if let Some(path) =
                sidebar_project_result_path(&result).or_else(|| Some(target_path.clone()))
            {
                if let Err(error) = update_project_root(io, state, path).await {
                    warn!(%error, "failed to update host UI after sidebar.project.open");
                }
            }
            json!({"ok": true, "path": target_path, "result": result})
        }
        Ok(result) => json!({"ok": false, "path": target_path, "result": result}),
        Err(error) => {
            warn!(%error, "sidebar.project.open RPC failed");
            state.sidebar_ipc.clear().await;
            json!({"ok": false, "path": target_path, "error": error.to_string()})
        }
    }
}

pub async fn te2_project_create(io: &SocketIo, state: &AppState, params: JsonMap) -> Value {
    let Some(target_path) = string_field(&params, "path") else {
        return json!({"ok": false, "error": "missing_path"});
    };
    let adopt_existing = params
        .get("adoptExisting")
        .or_else(|| params.get("adopt_existing"))
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let open = params.get("open").and_then(Value::as_bool).unwrap_or(true);
    let Ok(Some(client)) = ensure_client(io, state).await else {
        return json!({"ok": false, "error": "sidebar_unavailable", "path": target_path});
    };
    match sidebar_rpc_call(
        &client,
        "sidebar.project.create",
        json!({
            "path": target_path,
            "adoptExisting": adopt_existing,
            "open": open,
        }),
    )
    .await
    {
        Ok(result) if !rpc_result_explicitly_not_ok(&result) => {
            if open {
                if let Some(path) =
                    sidebar_project_result_path(&result).or_else(|| Some(target_path.clone()))
                {
                    if let Err(error) = update_project_root(io, state, path).await {
                        warn!(%error, "failed to update host UI after sidebar.project.create");
                    }
                }
            }
            json!({"ok": true, "path": target_path, "result": result})
        }
        Ok(result) => json!({"ok": false, "path": target_path, "result": result}),
        Err(error) => {
            warn!(%error, "sidebar.project.create RPC failed");
            state.sidebar_ipc.clear().await;
            json!({"ok": false, "path": target_path, "error": error.to_string()})
        }
    }
}

async fn ensure_client(io: &SocketIo, state: &AppState) -> Result<Option<Client>> {
    if let Some(client) = state.sidebar_ipc.current().await {
        return Ok(Some(client));
    }

    let address = sidebar_address(state);
    let disconnect_state = state.clone();
    let notify_io = io.clone();
    let notify_state = state.clone();

    let client = ClientBuilder::new(address)
        .namespace(SIDEBAR_NAMESPACE)
        .transport_type(TransportType::Websocket)
        .reconnect(false)
        .reconnect_on_disconnect(false)
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
    register_sidebar_client(&client).await;
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
    let value = sidebar_rpc_call(client, "sidebar.cwd.get", json!({})).await?;
    if let Some(cwd) = cwd_from_value(&value) {
        update_project_root(io, state, cwd.clone()).await?;
        return Ok(Some(cwd));
    }
    Ok(None)
}

async fn register_sidebar_client(client: &Client) {
    let payload = json!({
        "role": "iframe",
        "app": "als-rs",
        "app_id": "als-rs",
        "appId": "als-rs",
        "client_id": "als-rs-backend",
        "clientId": "als-rs-backend",
        "agentEdits": true,
        "capabilities": ["agentEdits", "sidebar.agentEdits", "sidebar.window.focused"],
    });
    match sidebar_rpc_call(client, "sidebar.register", payload).await {
        Ok(value) if !rpc_result_explicitly_not_ok(&value) => {
            info!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.register",
                ?value,
                "registered ALS-RS sidebar IPC client"
            );
        }
        Ok(value) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.register",
                ?value,
                "sidebar.register RPC returned an unsuccessful result"
            );
        }
        Err(error) => {
            warn!(
                namespace = SIDEBAR_NAMESPACE,
                socket_path = SIDEBAR_SOCKET_PATH,
                event = RPC_EVENT,
                method = "sidebar.register",
                %error,
                "sidebar.register RPC failed"
            );
        }
    }
}

async fn sidebar_rpc_call(client: &Client, method: &str, params: Value) -> Result<Value> {
    let request_id = format!("als-rs-sidebar-{}", unix_millis());
    let request = json!({
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
    });
    info!(
        namespace = SIDEBAR_NAMESPACE,
        socket_path = SIDEBAR_SOCKET_PATH,
        event = RPC_EVENT,
        method,
        request_id = %request_id,
        request = ?request,
        "sending sidebar IPC RPC request"
    );
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
    info!(
        namespace = SIDEBAR_NAMESPACE,
        socket_path = SIDEBAR_SOCKET_PATH,
        event = RPC_EVENT,
        method,
        request_id = %request_id,
        ack = ?value,
        "received sidebar IPC RPC ack"
    );
    sidebar_rpc_result(method, value)
}

fn sidebar_rpc_result(method: &str, value: Value) -> Result<Value> {
    let object = sidebar_rpc_ack_envelope(&value)
        .ok_or_else(|| anyhow!("sidebar RPC {method} ack is not a JSON-RPC envelope"))?;
    if let Some(error) = object.get("error") {
        let message = error
            .as_object()
            .and_then(|error| string_from_object(error, "message"))
            .unwrap_or_else(|| format!("sidebar RPC {method} failed"));
        bail!("{message}");
    }
    Ok(object.get("result").cloned().unwrap_or(Value::Null))
}

fn sidebar_rpc_ack_envelope(value: &Value) -> Option<&Map<String, Value>> {
    fn find_envelope(value: &Value, depth: usize) -> Option<&Map<String, Value>> {
        if depth > 4 {
            return None;
        }
        match value {
            Value::Object(object)
                if object.contains_key("result")
                    || object.contains_key("error")
                    || object.get("jsonrpc").and_then(Value::as_str) == Some(JSONRPC_VERSION) =>
            {
                Some(object)
            }
            Value::Array(values) => values
                .iter()
                .find_map(|value| find_envelope(value, depth + 1)),
            _ => None,
        }
    }

    find_envelope(value, 0)
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
        "sidebar.project.opened" => {
            if let Some(cwd) =
                string_field(&params, "resolved_path").or_else(|| string_field(&params, "path"))
            {
                update_project_root(io, state, cwd).await?;
            }
        }
        "sidebar.mention" => {
            process_mention(io, state, params).await?;
        }
        "sidebar.window.focused" => {
            process_focused_window(state, params)?;
        }
        "sidebar.agentEdits.documentState.get" => {
            publish_inline_agent_document_state(io, state, params).await?;
        }
        "sidebar.agentEdits.decide" => {
            publish_inline_agent_decision(io, state, params).await?;
        }
        _ => {
            warn!(method, "ignored sidebar RPC notification");
        }
    }
    Ok(())
}

fn process_focused_window(state: &AppState, params: JsonMap) -> Result<()> {
    if let Some(app_id) = string_field(&params, "app_id").or_else(|| string_field(&params, "appId"))
    {
        if app_id != "als-rs" {
            return Ok(());
        }
    }
    if params.get("focused").and_then(Value::as_bool) == Some(false) {
        return Ok(());
    }
    let Some(snapshot) = focused_window_snapshot_from_params(&params) else {
        warn!(
            host_id = ?string_field(&params, "host_id").or_else(|| string_field(&params, "hostId")),
            "sidebar.window.focused notification did not include a conversation target"
        );
        return Ok(());
    };
    let conversation_id = snapshot.conversation_id.clone();
    let host_id = snapshot.host_id.clone();
    state.focused_window.set(snapshot)?;
    info!(
        conversation_id = %conversation_id,
        host_id = ?host_id,
        "updated focused ALS-RS sidebar window target"
    );
    Ok(())
}

fn focused_window_snapshot_from_params(params: &JsonMap) -> Option<FocusedWindowSnapshot> {
    let conversation_id = focused_conversation_id_from_params(params)?;
    Some(FocusedWindowSnapshot {
        host_id: string_field(params, "host_id").or_else(|| string_field(params, "hostId")),
        conversation_id,
        state_kind: string_field(params, "state_kind")
            .or_else(|| string_field(params, "stateKind")),
        url: string_field(params, "url"),
        restore_url: string_field(params, "restore_url")
            .or_else(|| string_field(params, "restoreUrl")),
        token_id: string_field(params, "token_id").or_else(|| string_field(params, "tokenId")),
        console_worker_id: string_field(params, "console_worker_id")
            .or_else(|| string_field(params, "consoleWorkerId")),
        source: string_field(params, "source"),
        ts: int_field(params, "ts"),
    })
}

fn focused_conversation_id_from_params(params: &JsonMap) -> Option<String> {
    conversation_id_from_query_state(params.get("query_state"))
        .or_else(|| conversation_id_from_query_state(params.get("queryState")))
        .or_else(|| {
            string_field(params, "restore_url")
                .or_else(|| string_field(params, "restoreUrl"))
                .and_then(|url| conversation_id_from_url(&url))
        })
        .or_else(|| string_field(params, "url").and_then(|url| conversation_id_from_url(&url)))
}

fn conversation_id_from_query_state(value: Option<&Value>) -> Option<String> {
    let object = value?.as_object()?;
    string_from_object(object, "conversation_id")
        .or_else(|| string_from_object(object, "conversationId"))
}

fn conversation_id_from_url(url: &str) -> Option<String> {
    let query = url.split_once('?')?.1.split('#').next().unwrap_or_default();
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        let key = decode_query_component(key);
        if key == "conversation_id" || key == "conversationId" {
            let value = decode_query_component(value);
            if !value.trim().is_empty() {
                return Some(value.trim().to_owned());
            }
        }
    }
    None
}

fn decode_query_component(value: &str) -> String {
    fn hex(value: u8) -> Option<u8> {
        match value {
            b'0'..=b'9' => Some(value - b'0'),
            b'a'..=b'f' => Some(value - b'a' + 10),
            b'A'..=b'F' => Some(value - b'A' + 10),
            _ => None,
        }
    }

    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' if index + 2 < bytes.len() => {
                if let (Some(high), Some(low)) = (hex(bytes[index + 1]), hex(bytes[index + 2])) {
                    output.push((high << 4) | low);
                    index += 3;
                    continue;
                }
                output.push(bytes[index]);
                index += 1;
            }
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&output).into_owned()
}

async fn publish_inline_agent_document_state(
    _io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<()> {
    match state.inline_agent_edits.document_state(&params) {
        Ok(Value::Object(projection)) => {
            let _ = publish_agent_edits_with_current_client(state, projection).await;
        }
        Ok(value) => {
            warn!(
                ?value,
                "inline agent edit document state notification did not produce an object"
            );
        }
        Err(error) => {
            warn!(%error, "inline agent edit document state notification failed");
        }
    }
    Ok(())
}

async fn publish_inline_agent_decision(
    io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<()> {
    let decision = string_field(&params, "decision").unwrap_or_default();
    let document_params = inline_agent_decision_document_params(&params);
    let project_params = match inline_agent_decision_project_params(&params) {
        Ok(value) => value,
        Err(error) => {
            warn!(decision, %error, "inline agent edit decision rejected");
            return Ok(());
        }
    };
    let project_result = match decision.as_str() {
        "accept" | "accepted" => {
            crate::ui_rpc::project_agent_diff_accept(io, state, project_params).await
        }
        "reject" | "rejected" => {
            crate::ui_rpc::project_agent_diff_reject(io, state, project_params).await
        }
        other => {
            warn!(
                decision = other,
                "inline agent edit decision must be accept or reject"
            );
            return Ok(());
        }
    };

    if let Err(error) = project_result {
        let failure_conversation_id = string_field(&params, "conversationId")
            .or_else(|| string_field(&params, "conversation_id"))
            .unwrap_or_default();
        let failure_edit_id = string_field(&params, "editId")
            .or_else(|| string_field(&params, "edit_id"))
            .or_else(|| string_field(&params, "diffId"))
            .or_else(|| string_field(&params, "diff_id"))
            .or_else(|| string_field(&params, "id"))
            .unwrap_or_default();
        let failure_uri = string_field(&params, "uri").unwrap_or_default();
        let failure_project_path = string_field(&params, "projectPath")
            .or_else(|| string_field(&params, "project_path"))
            .or_else(|| string_field(&params, "cwd"))
            .unwrap_or_default();
        warn!(
            code = error.code,
            message = %error.message,
            decision,
            conversation_id = %failure_conversation_id,
            edit_id = %failure_edit_id,
            uri = %failure_uri,
            project_path = %failure_project_path,
            "inline agent edit decision failed through project diff path"
        );
        publish_inline_agent_document_state(io, state, document_params).await?;
        return Ok(());
    }

    match state.inline_agent_edits.clear(&params) {
        Ok(_) => publish_inline_agent_document_state(io, state, document_params).await?,
        Err(error) => {
            warn!(%error, "inline agent edit decision clear failed");
            publish_inline_agent_document_state(io, state, document_params).await?;
        }
    }
    Ok(())
}

fn inline_agent_decision_document_params(params: &JsonMap) -> JsonMap {
    let mut document_params = JsonMap::new();
    for key in [
        "uri",
        "projectPath",
        "project_path",
        "conversationId",
        "conversation_id",
        "sessionId",
        "session_id",
        "threadId",
        "thread_id",
    ] {
        if let Some(value) = params.get(key) {
            document_params.insert(key.to_owned(), value.clone());
        }
    }
    document_params
}

fn inline_agent_decision_project_params(params: &JsonMap) -> Result<JsonMap> {
    let conversation_id = string_field(params, "conversationId")
        .or_else(|| string_field(params, "conversation_id"))
        .ok_or_else(|| anyhow!("conversationId is required"))?;
    let diff_id = string_field(params, "diffId")
        .or_else(|| string_field(params, "diff_id"))
        .or_else(|| string_field(params, "editId"))
        .or_else(|| string_field(params, "edit_id"))
        .or_else(|| string_field(params, "id"))
        .ok_or_else(|| anyhow!("editId is required"))?;
    let mut project_params = JsonMap::new();
    project_params.insert("conversation_id".to_owned(), Value::String(conversation_id));
    project_params.insert("diff_id".to_owned(), Value::String(diff_id));
    if let Some(project_path) = string_field(params, "projectPath")
        .or_else(|| string_field(params, "project_path"))
        .or_else(|| string_field(params, "cwd"))
    {
        project_params.insert("cwd".to_owned(), Value::String(project_path));
    }
    Ok(project_params)
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
    let explicit_conversation_id = string_field(&payload, "conversation_id")
        .or_else(|| string_field(&payload, "conversationId"));
    let focused_conversation_id = state.focused_window.conversation_id()?;
    let focused_conversation_is_valid = match explicit_conversation_id.as_ref() {
        Some(_) => false,
        None => match focused_conversation_id.as_ref() {
            Some(conversation_id) => state
                .conversations
                .load_meta_if_exists(conversation_id)?
                .is_some(),
            None => false,
        },
    };
    if explicit_conversation_id.is_none()
        && focused_conversation_id.is_some()
        && !focused_conversation_is_valid
    {
        warn!(
            conversation_id = ?focused_conversation_id,
            "focused sidebar window conversation is stale; falling back to active conversation"
        );
    }
    let conversation_id = explicit_conversation_id
        .clone()
        .or_else(|| {
            focused_conversation_is_valid
                .then(|| focused_conversation_id.clone())
                .flatten()
        })
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
    let focused_conversation = focused_conversation_id.as_deref() == Some(&conversation_id);
    if focused_conversation || (selection.active_view == "conversation" && active_conversation) {
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

fn te2_project_status_unavailable(
    target_path: &str,
    current_cwd: Option<String>,
    reason: &str,
) -> Value {
    json!({
        "ok": true,
        "connected": false,
        "target_path": target_path,
        "current_cwd": current_cwd,
        "matches_current": false,
        "known": false,
        "reason": reason,
        "action": "disabled",
        "lookup": Value::Null,
    })
}

fn sidebar_project_result_path(value: &Value) -> Option<String> {
    match value {
        Value::Array(values) => values.iter().find_map(sidebar_project_result_path),
        Value::Object(object) => string_from_object(object, "resolved_path")
            .or_else(|| string_from_object(object, "path"))
            .or_else(|| {
                object
                    .get("project")
                    .and_then(Value::as_object)
                    .and_then(|project| string_from_object(project, "path"))
            })
            .or_else(|| object.get("data").and_then(sidebar_project_result_path)),
        _ => None,
    }
}

fn same_logical_path(left: &str, right: &str) -> bool {
    fn normalize(value: &str) -> &str {
        let value = value.trim();
        if value == "/" {
            value
        } else {
            value.trim_end_matches('/')
        }
    }
    normalize(left) == normalize(right)
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
        conversation_id_from_url, cwd_from_value, encode_draft_mention_token,
        focused_conversation_id_from_params, focused_window_snapshot_from_params,
        rpc_result_explicitly_not_ok, same_logical_path, sidebar_project_result_path,
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
    fn same_logical_path_ignores_trailing_slashes_only() {
        assert!(same_logical_path("/repo/project", "/repo/project/"));
        assert!(same_logical_path("/", "/"));
        assert!(!same_logical_path("/repo/project", "/repo/other"));
    }

    #[test]
    fn sidebar_project_result_path_accepts_wrapped_shapes() {
        assert_eq!(
            sidebar_project_result_path(&json!({"resolved_path": "/repo"})),
            Some("/repo".to_owned())
        );
        assert_eq!(
            sidebar_project_result_path(&json!({"project": {"path": "/project"}})),
            Some("/project".to_owned())
        );
        assert_eq!(
            sidebar_project_result_path(&json!({"data": {"path": "/nested"}})),
            Some("/nested".to_owned())
        );
    }

    #[test]
    fn focused_window_snapshot_prefers_query_state() {
        let params = json!({
            "app_id": "als-rs",
            "host_id": "slot:als-rs:als_rs:a1b2",
            "state_kind": "conversation",
            "query_state": {"conversation_id": "conv-query"},
            "restore_url": "/app/als-rs?conversation_id=conv-restore",
            "url": "/app/als-rs?conversation_id=conv-url",
            "token_id": "als_rs",
            "console_worker_id": "als_rs:a1b2",
            "source": "header_icon",
            "ts": 1778220000000i64,
        })
        .as_object()
        .expect("params should be object")
        .clone();

        let snapshot = focused_window_snapshot_from_params(&params)
            .expect("focused window snapshot should parse");
        assert_eq!(snapshot.conversation_id, "conv-query");
        assert_eq!(snapshot.host_id.as_deref(), Some("slot:als-rs:als_rs:a1b2"));
        assert_eq!(snapshot.state_kind.as_deref(), Some("conversation"));
        assert_eq!(snapshot.token_id.as_deref(), Some("als_rs"));
        assert_eq!(snapshot.console_worker_id.as_deref(), Some("als_rs:a1b2"));
        assert_eq!(snapshot.source.as_deref(), Some("header_icon"));
        assert_eq!(snapshot.ts, Some(1778220000000));
    }

    #[test]
    fn focused_conversation_id_accepts_camel_query_state_and_urls() {
        let params = json!({
            "queryState": {"conversationId": "conv-camel"},
            "restoreUrl": "/app/als-rs?conversation_id=conv-restore",
            "url": "/app/als-rs?conversation_id=conv-url",
        })
        .as_object()
        .expect("params should be object")
        .clone();
        assert_eq!(
            focused_conversation_id_from_params(&params),
            Some("conv-camel".to_owned())
        );

        let params = json!({
            "restoreUrl": "/app/als-rs?embed=1&conversation_id=conv%20restore",
            "url": "/app/als-rs?conversation_id=conv-url",
        })
        .as_object()
        .expect("params should be object")
        .clone();
        assert_eq!(
            focused_conversation_id_from_params(&params),
            Some("conv restore".to_owned())
        );
    }

    #[test]
    fn conversation_id_from_url_decodes_query_values() {
        assert_eq!(
            conversation_id_from_url(
                "/app/als-rs?embed=1&conversation_id=conv%3A123+with+space#ignored"
            ),
            Some("conv:123 with space".to_owned())
        );
        assert_eq!(
            conversation_id_from_url("/app/als-rs?conversationId=conv-camel"),
            Some("conv-camel".to_owned())
        );
        assert_eq!(conversation_id_from_url("/app/als-rs?embed=1"), None);
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
        assert_eq!(
            sidebar_rpc_result(
                "sidebar.cwd.get",
                json!([{"jsonrpc": "2.0", "id": "1", "result": {"cwd": "/ack-array"}}])
            )
            .expect("ack array result should parse"),
            json!({"cwd": "/ack-array"})
        );
        assert_eq!(
            sidebar_rpc_result(
                "sidebar.cwd.get",
                json!([[{"jsonrpc": "2.0", "id": "1", "result": {"cwd": "/nested-ack-array"}}]])
            )
            .expect("nested ack array result should parse"),
            json!({"cwd": "/nested-ack-array"})
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
