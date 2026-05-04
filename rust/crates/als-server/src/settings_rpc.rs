use als_adapter_protocol::JsonMap;
use als_jsonrpc::{ErrorResponse, RequestId, RpcError, SuccessResponse};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef},
};

const RPC_EVENT: &str = "rpc";
const JSONRPC_VERSION: &str = "2.0";

pub fn register_settings_rpc_namespace(io: &SocketIo) {
    io.ns("/rpc/settings", async |socket: SocketRef| {
        socket.on(RPC_EVENT, handle_rpc_request);
    });
}

async fn handle_rpc_request(Data(request): Data<JsonRpcRequest>, ack: AckSender) {
    let id = request.id.clone();
    let response = match dispatch_rpc(request) {
        Ok(result) => RpcAck::Success(SuccessResponse::new(id, result)),
        Err(error) => RpcAck::Error(ErrorResponse::new(id, error)),
    };
    let _ = ack.send(&response);
}

fn dispatch_rpc(request: JsonRpcRequest) -> Result<Value, RpcError> {
    if request.jsonrpc != JSONRPC_VERSION {
        return Err(rpc_error(-32600, "Invalid JSON-RPC version"));
    }

    match request.method.as_str() {
        "config.get" => Ok(json!({"transport": "rpc"})),
        "config.update" => Ok(json!({"ok": true, "transport": "rpc"})),
        "status.get" => Ok(json!({"running": true, "transport": "rpc"})),
        "extensions.list" => Ok(json!({"extensions": [], "transport": "rpc"})),
        "extensions.reload" => Ok(json!({"ok": true, "extensions": [], "transport": "rpc"})),
        "extension.enabled.set" | "extension.install" | "extension.session.bind" => Ok(
            json!({"ok": false, "error": format!("{} is not implemented in ALS-RS yet", request.method), "transport": "rpc"}),
        ),
        "extension.splashSchema.get" | "extension.settingsSchema.get" => {
            Ok(json!({"schema": {}, "transport": "rpc"}))
        }
        "extension.splashAction.run" => Ok(json!({"ok": false, "transport": "rpc"})),
        "extension.runtimeOptions.get" => Ok(json!({
            "agent": request.params.get("agent").cloned().unwrap_or(Value::Null),
            "has_plan": false,
            "has_todo": false,
            "quickControls": [],
            "fields": {},
            "transport": "rpc"
        })),
        "extension.requestCards.get" => Ok(json!({
            "extension_id": request.params.get("extension_id").cloned().unwrap_or(Value::String(String::new())),
            "cards": [],
            "schemas": {},
            "transport": "rpc"
        })),
        "extension.uiFeatures.get" => Ok(json!({"ui_features": {}, "transport": "rpc"})),
        "extension.plan.get" => Ok(json!({
            "has_plan": false,
            "plan_exists": false,
            "plan_content": "",
            "plan_path": Value::Null,
            "plan_source": Value::Null,
            "has_todo": false,
            "plan_steps": [],
            "transport": "rpc"
        })),
        "extension.models.list" => Ok(json!({"models": [], "transport": "rpc"})),
        "extension.sessions.list" => Ok(json!({"sessions": [], "transport": "rpc"})),
        _ => Err(rpc_error(
            -32601,
            format!("Unsupported method: {}", request.method),
        )),
    }
}

fn rpc_error(code: i64, message: impl Into<String>) -> RpcError {
    RpcError::new(code, message, None)
}

#[derive(Clone, Debug, Deserialize)]
struct JsonRpcRequest {
    jsonrpc: String,
    id: RequestId,
    method: String,
    #[serde(default)]
    params: JsonMap,
}

#[derive(Serialize)]
#[serde(untagged)]
enum RpcAck {
    Success(SuccessResponse),
    Error(ErrorResponse),
}
