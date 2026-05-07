use crate::{conversation_rpc, state::AppState};
use anyhow::{Context, Result, anyhow};
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State, TryData},
    socket::DisconnectReason,
};
use std::{
    collections::HashSet,
    env, fs,
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tracing::warn;

const IPC_NAMESPACE: &str = "/ipc";

#[derive(Clone, Default)]
pub struct IpcClientStore {
    inner: Arc<Mutex<HashSet<String>>>,
}

impl IpcClientStore {
    fn insert(&self, sid: String) {
        if let Ok(mut clients) = self.inner.lock() {
            clients.insert(sid);
        }
    }

    fn remove(&self, sid: &str) {
        if let Ok(mut clients) = self.inner.lock() {
            clients.remove(sid);
        }
    }

    fn contains(&self, sid: &str) -> bool {
        self.inner
            .lock()
            .map(|clients| clients.contains(sid))
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
            socket.on("ask_user_ack", handle_ask_user_ack);
            socket.on_disconnect(
                async |socket: SocketRef, State(state): State<AppState>, _reason: DisconnectReason| {
                    state.ipc_clients.remove(&socket.id.to_string());
                },
            );
        },
    );
}

pub async fn emit_ask_user_response(io: &SocketIo, request_id: &str, response: &Map<String, Value>) {
    emit_ipc_event(
        io,
        "ask_user_response",
        json!({
            "request_id": request_id,
            "response": response,
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

    match conversation_rpc::acknowledge_ask_user_interaction(&io, &state, &request_id).await {
        Ok(true) => {
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
    let home = env::var_os("HOME").ok_or_else(|| anyhow!("HOME is required for IPC secret path"))?;
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
