use crate::{conversation_rpc, sidebar_ipc, state::AppState};
use anyhow::{Context, Result, anyhow};
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State, TryData},
    socket::DisconnectReason,
};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tracing::warn;

const IPC_NAMESPACE: &str = "/ipc";
const SIDEBAR_RPC_DRAFT_METHODS: &[&str] = &[
    "sidebar.drafts.list",
    "sidebar.draftState.get",
    "sidebar.draft.clear",
];

#[derive(Clone, Debug)]
struct IpcRequestOwner {
    sid: String,
    request_id: String,
    requestor_id: String,
}

#[derive(Default)]
struct IpcClientState {
    clients: HashSet<String>,
    requests: HashMap<String, IpcRequestOwner>,
}

#[derive(Clone, Default)]
pub struct IpcClientStore {
    inner: Arc<Mutex<IpcClientState>>,
    begin_lock: Arc<tokio::sync::Mutex<()>>,
}

impl IpcClientStore {
    fn insert(&self, sid: String) {
        if let Ok(mut state) = self.inner.lock() {
            state.clients.insert(sid);
        }
    }

    fn remove_client(&self, sid: &str) -> Vec<IpcRequestOwner> {
        let Ok(mut state) = self.inner.lock() else {
            return Vec::new();
        };
        state.clients.remove(sid);
        let request_ids = state
            .requests
            .iter()
            .filter_map(|(request_id, owner)| (owner.sid == sid).then(|| request_id.clone()))
            .collect::<Vec<_>>();
        request_ids
            .into_iter()
            .filter_map(|request_id| state.requests.remove(&request_id))
            .collect()
    }

    fn replace_request(
        &self,
        sid: &str,
        request_id: &str,
        requestor_id: &str,
    ) -> Vec<IpcRequestOwner> {
        let Ok(mut state) = self.inner.lock() else {
            return Vec::new();
        };
        let stale_ids = state
            .requests
            .iter()
            .filter_map(|(pending_id, owner)| {
                (owner.requestor_id == requestor_id && pending_id != request_id)
                    .then(|| pending_id.clone())
            })
            .collect::<Vec<_>>();
        let replaced = stale_ids
            .into_iter()
            .filter_map(|pending_id| state.requests.remove(&pending_id))
            .collect::<Vec<_>>();
        state.requests.insert(
            request_id.to_owned(),
            IpcRequestOwner {
                sid: sid.to_owned(),
                request_id: request_id.to_owned(),
                requestor_id: requestor_id.to_owned(),
            },
        );
        replaced
    }

    fn remove_request(&self, request_id: &str) -> Option<IpcRequestOwner> {
        self.inner
            .lock()
            .ok()
            .and_then(|mut state| state.requests.remove(request_id))
    }

    fn owns_request(&self, sid: &str, request_id: &str) -> bool {
        self.inner
            .lock()
            .ok()
            .and_then(|state| state.requests.get(request_id).cloned())
            .map(|owner| owner.sid == sid)
            .unwrap_or(false)
    }

    fn request_is_active(&self, request_id: &str, requestor_id: &str) -> bool {
        self.inner
            .lock()
            .ok()
            .and_then(|state| state.requests.get(request_id).cloned())
            .map(|owner| owner.requestor_id == requestor_id)
            .unwrap_or(false)
    }

    fn remove_request_if_owned(&self, sid: &str, request_id: &str) -> Option<IpcRequestOwner> {
        let Ok(mut state) = self.inner.lock() else {
            return None;
        };
        if state
            .requests
            .get(request_id)
            .map(|owner| owner.sid == sid)
            .unwrap_or(false)
        {
            state.requests.remove(request_id)
        } else {
            None
        }
    }

    fn contains(&self, sid: &str) -> bool {
        self.inner
            .lock()
            .map(|state| state.clients.contains(sid))
            .unwrap_or(false)
    }
}

pub fn register_ipc_namespace(io: &SocketIo) {
    io.ns(
        IPC_NAMESPACE,
        async |socket: SocketRef, State(state): State<AppState>, TryData(auth): TryData<Value>| {
            let auth = auth.unwrap_or(Value::Null);
            if !ipc_auth_ok(&auth) {
                let _ = socket.disconnect();
                return;
            }

            let sid = socket.id.to_string();
            state.ipc_clients.insert(sid.clone());
            socket.on("ask_user_begin", handle_ask_user_begin);
            socket.on("ask_user_cancel", handle_ask_user_cancel);
            socket.on("ask_user_ack", handle_ask_user_ack);
            socket.on("sidebar_rpc", handle_sidebar_rpc);
            socket.on_disconnect(
                async |socket: SocketRef,
                       State(state): State<AppState>,
                       io: SocketIo,
                       _reason: DisconnectReason| {
                    let _begin_guard = state.ipc_clients.begin_lock.lock().await;
                    let owners = state.ipc_clients.remove_client(&socket.id.to_string());
                    for owner in owners {
                        let _ = conversation_rpc::invalidate_ask_user_interaction(
                            &io,
                            &state,
                            &owner.request_id,
                            "ipc_disconnected",
                        )
                        .await;
                    }
                },
            );
        },
    );
}

async fn handle_sidebar_rpc(
    socket: SocketRef,
    State(state): State<AppState>,
    io: SocketIo,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let sid = socket.id.to_string();
    if !state.ipc_clients.contains(&sid) {
        let _ = ack.send(&json!({"ok": false, "error": "unauthorized"}));
        return;
    }

    let Some((method, params)) = sidebar_rpc_request_from_payload(&data) else {
        let _ = ack.send(&json!({"ok": false, "error": "method is required"}));
        return;
    };
    if !SIDEBAR_RPC_DRAFT_METHODS.contains(&method.as_str()) {
        let _ = ack.send(&json!({
            "ok": false,
            "error": "sidebar RPC method is not allowed from MCP IPC",
            "method": method,
        }));
        return;
    }

    match sidebar_ipc::proxy_sidebar_rpc(&io, &state, &method, params).await {
        Ok(result) => {
            let _ = ack.send(&json!({
                "ok": true,
                "method": method,
                "result": result,
            }));
        }
        Err(error) => {
            let _ = ack.send(&json!({
                "ok": false,
                "method": method,
                "error": error.to_string(),
            }));
        }
    }
}

pub async fn emit_ask_user_response(
    io: &SocketIo,
    request_id: &str,
    requestor_id: &str,
    response: &Map<String, Value>,
) {
    emit_ipc_event(
        io,
        "ask_user_response",
        json!({
            "request_id": request_id,
            "requestor_id": requestor_id,
            "response": response,
        }),
    )
    .await;
}

pub fn ask_user_request_is_active(state: &AppState, request_id: &str, requestor_id: &str) -> bool {
    state
        .ipc_clients
        .request_is_active(request_id, requestor_id)
}

async fn handle_ask_user_begin(
    socket: SocketRef,
    State(state): State<AppState>,
    io: SocketIo,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let sid = socket.id.to_string();
    let _begin_guard = state.ipc_clients.begin_lock.lock().await;
    if !state.ipc_clients.contains(&sid) {
        let _ = ack.send(&json!({"ok": false, "error": "unauthorized"}));
        return;
    }
    let registration = match conversation_rpc::prepare_ask_user_interaction(&state, &data) {
        Ok(registration) => registration,
        Err(error) => {
            let _ = ack.send(&json!({"ok": false, "error": error.to_string()}));
            return;
        }
    };
    let replaced_owners = state.ipc_clients.replace_request(
        &sid,
        &registration.request_id,
        &registration.requestor_id,
    );
    conversation_rpc::publish_ask_user_interaction(&io, &state, &registration).await;
    let mut terminal_ids = registration
        .invalidated
        .iter()
        .filter_map(|descriptor| request_id_from_payload(&Value::Object(descriptor.clone())))
        .collect::<HashSet<_>>();
    terminal_ids.extend(replaced_owners.into_iter().map(|owner| owner.request_id));
    terminal_ids.remove(&registration.request_id);
    for request_id in terminal_ids {
        emit_ask_user_terminal(&io, &request_id, &registration.requestor_id, "superseded").await;
    }
    let _ = ack.send(&json!({
        "ok": true,
        "request_id": registration.request_id,
        "requestor_id": registration.requestor_id,
    }));
}

async fn handle_ask_user_cancel(
    socket: SocketRef,
    State(state): State<AppState>,
    io: SocketIo,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let sid = socket.id.to_string();
    let _begin_guard = state.ipc_clients.begin_lock.lock().await;
    let Some(request_id) = request_id_from_payload(&data) else {
        let _ = ack.send(&json!({"ok": false, "error": "request_id is required"}));
        return;
    };
    if state
        .ipc_clients
        .remove_request_if_owned(&sid, &request_id)
        .is_none()
    {
        let _ = ack.send(
            &json!({"ok": false, "error": "ask_user request is not owned by this IPC client"}),
        );
        return;
    }
    let reason = data
        .as_object()
        .and_then(|payload| payload.get("reason"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("request_cancelled");
    let invalidated =
        conversation_rpc::invalidate_ask_user_interaction(&io, &state, &request_id, reason)
            .await
            .unwrap_or(false);
    let _ = ack.send(&json!({"ok": true, "request_id": request_id, "invalidated": invalidated}));
}

async fn emit_ask_user_terminal(io: &SocketIo, request_id: &str, requestor_id: &str, status: &str) {
    emit_ipc_event(
        io,
        "ask_user_terminal",
        json!({
            "request_id": request_id,
            "requestor_id": requestor_id,
            "status": status,
        }),
    )
    .await;
}

async fn emit_ipc_event(io: &SocketIo, event_name: &str, payload: Value) {
    let Some(namespace) = io.of(IPC_NAMESPACE) else {
        warn!(event_name, "IPC namespace is unavailable");
        return;
    };
    if let Err(error) = namespace.emit(event_name, &payload).await {
        warn!(%error, event_name, "failed to emit IPC event");
    }
}

async fn handle_ask_user_ack(
    socket: SocketRef,
    State(state): State<AppState>,
    io: SocketIo,
    Data(data): Data<Value>,
    ack: AckSender,
) {
    let sid = socket.id.to_string();
    if !state.ipc_clients.contains(&sid) {
        let _ = ack.send(&json!({"ok": false, "error": "unauthorized"}));
        return;
    }
    let Some(request_id) = request_id_from_payload(&data) else {
        let _ = ack.send(&json!({"ok": false, "error": "request_id is required"}));
        return;
    };
    if !state.ipc_clients.owns_request(&sid, &request_id) {
        let _ = ack.send(
            &json!({"ok": false, "error": "ask_user request is not owned by this IPC client"}),
        );
        return;
    }

    match conversation_rpc::acknowledge_ask_user_interaction(&io, &state, &request_id).await {
        Ok(true) => {
            state.ipc_clients.remove_request(&request_id);
            let _ = ack.send(&json!({"ok": true, "request_id": request_id}));
        }
        Ok(false) => {
            let _ = ack.send(&json!({"ok": false, "error": "approval is no longer pending"}));
        }
        Err(error) => {
            let _ = ack.send(&json!({"ok": false, "error": error.to_string()}));
        }
    }
}

fn ipc_auth_ok(auth: &Value) -> bool {
    let provided = auth
        .as_object()
        .and_then(|payload| payload.get("secret"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let Some(provided) = provided else {
        return false;
    };
    let Ok(expected) = load_or_create_ipc_secret() else {
        return false;
    };
    constant_time_eq(provided.as_bytes(), expected.trim().as_bytes())
}

fn request_id_from_payload(data: &Value) -> Option<String> {
    let payload = data.as_object()?;
    for key in ["request_id", "requestId", "id"] {
        let value = payload.get(key).and_then(Value::as_str).map(str::trim);
        if let Some(value) = value.filter(|value| !value.is_empty()) {
            return Some(value.to_owned());
        }
    }
    None
}

fn sidebar_rpc_request_from_payload(data: &Value) -> Option<(String, Value)> {
    let payload = data.as_object()?;
    let method = payload
        .get("method")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())?
        .to_owned();
    let params = payload
        .get("params")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    if !params.is_object() {
        return None;
    }
    Some((method, params))
}

fn load_or_create_ipc_secret() -> Result<String> {
    let path = ipc_secret_path()?;
    if path.exists() {
        let secret = fs::read_to_string(&path)
            .with_context(|| format!("failed to read IPC secret {}", path.display()))?;
        let secret = secret.trim().to_owned();
        if !secret.is_empty() {
            return Ok(secret);
        }
    }

    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).context("failed to generate IPC secret")?;
    let secret = hex_lower(&bytes);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create IPC secret dir {}", parent.display()))?;
    }
    fs::write(&path, &secret)
        .with_context(|| format!("failed to write IPC secret {}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o600));
    }
    Ok(secret)
}

fn ipc_secret_path() -> Result<PathBuf> {
    let home =
        env::var_os("HOME").ok_or_else(|| anyhow!("HOME is required for IPC secret path"))?;
    Ok(PathBuf::from(home)
        .join(".cache")
        .join("app_server")
        .join("ipc_secret"))
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for (left, right) in left.iter().zip(right) {
        diff |= left ^ right;
    }
    diff == 0
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::IpcClientStore;

    #[test]
    fn request_ownership_is_replaced_per_requestor_and_cleared_on_disconnect() {
        let store = IpcClientStore::default();
        store.insert("sid-old".to_owned());
        store.insert("sid-new".to_owned());

        assert!(
            store
                .replace_request("sid-old", "request-old", "conversation-1")
                .is_empty()
        );
        let replaced = store.replace_request("sid-new", "request-new", "conversation-1");

        assert_eq!(replaced.len(), 1);
        assert_eq!(replaced[0].sid, "sid-old");
        assert_eq!(replaced[0].request_id, "request-old");
        assert!(!store.request_is_active("request-old", "conversation-1"));
        assert!(store.request_is_active("request-new", "conversation-1"));

        let disconnected = store.remove_client("sid-new");
        assert_eq!(disconnected.len(), 1);
        assert_eq!(disconnected[0].request_id, "request-new");
        assert!(!store.request_is_active("request-new", "conversation-1"));
    }
}
