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

pub fn register_ui_rpc_namespace(io: &SocketIo) {
    io.ns("/rpc/ui", async |socket: SocketRef| {
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
        "view.get" => {
            Ok(json!({"active_view": "splash", "conversation_id": Value::Null, "transport": "rpc"}))
        }
        "view.set" => Ok(json!({
            "active_view": request.params.get("view").and_then(Value::as_str).unwrap_or("splash"),
            "conversation_id": Value::Null,
            "transport": "rpc"
        })),
        "hostUi.get" | "hostUi.recheck" => Ok(json!({
            "showClose": false,
            "ideMode": false,
            "projectRoot": Value::Null,
            "transport": "rpc"
        })),
        "filesystem.list" => Ok(json!({"items": [], "transport": "rpc"})),
        "filesystem.search" => Ok(json!({"items": [], "transport": "rpc"})),
        "file.open" | "url.open" => Ok(json!({
            "ok": false,
            "error": format!("{} is not implemented in ALS-RS yet", request.method),
            "transport": "rpc"
        })),
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
