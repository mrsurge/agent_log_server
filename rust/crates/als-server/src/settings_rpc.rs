use crate::extension_registry::ExtensionRegistryEntry;
use crate::state::AppState;
use als_adapter_protocol::{JsonMap, methods};
use als_jsonrpc::{ErrorResponse, RequestId, RpcError, SuccessResponse};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State},
};

const RPC_EVENT: &str = "rpc";
const JSONRPC_VERSION: &str = "2.0";

pub fn register_settings_rpc_namespace(io: &SocketIo) {
    io.ns(
        "/rpc/settings",
        async |socket: SocketRef, State(_state): State<AppState>| {
            socket.on(RPC_EVENT, handle_rpc_request);
        },
    );
}

async fn handle_rpc_request(
    State(state): State<AppState>,
    Data(request): Data<JsonRpcRequest>,
    ack: AckSender,
) {
    let id = request.id.clone();
    let response = match dispatch_rpc(&state, request).await {
        Ok(result) => RpcAck::Success(SuccessResponse::new(id, result)),
        Err(error) => RpcAck::Error(ErrorResponse::new(id, error)),
    };
    let _ = ack.send(&response);
}

async fn dispatch_rpc(state: &AppState, request: JsonRpcRequest) -> Result<Value, RpcError> {
    if request.jsonrpc != JSONRPC_VERSION {
        return Err(rpc_error(-32600, "Invalid JSON-RPC version"));
    }

    match request.method.as_str() {
        "config.get" => Ok(json!({"transport": "rpc"})),
        "config.update" => Ok(json!({"ok": true, "transport": "rpc"})),
        "status.get" => Ok(json!({"running": true, "transport": "rpc"})),
        "extensions.list" => Ok(json!({"extensions": state.extensions.list(), "transport": "rpc"})),
        "extensions.reload" => {
            let extensions = state.extensions.reload().map_err(internal_rpc_error)?;
            let adapter = state
                .adapter
                .reload_extensions_if_running()
                .await
                .map_err(internal_rpc_error)?;
            Ok(json!({
                "ok": true,
                "extensions": extensions,
                "adapter": adapter,
                "transport": "rpc"
            }))
        }
        "extension.enabled.set" => extension_enabled_set(state, &request.params).await,
        "extension.install" | "extension.session.bind" => Ok(
            json!({"ok": false, "error": format!("{} is not implemented in ALS-RS yet", request.method), "transport": "rpc"}),
        ),
        "extension.settingsSchema.get" => {
            extension_schema(
                state,
                &request.params,
                methods::EXTENSION_GET_SETTINGS_SCHEMA,
                SchemaKind::Settings,
            )
            .await
        }
        "extension.splashSchema.get" => {
            extension_schema(
                state,
                &request.params,
                methods::EXTENSION_GET_SPLASH_SCHEMA,
                SchemaKind::Splash,
            )
            .await
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
        "extension.requestCards.get" => {
            let extension_id = extension_id_param(&request.params);
            let cards = extension_id
                .as_deref()
                .and_then(|id| state.extensions.get(id))
                .map(|entry| request_cards_for_entry(extension_id.as_deref().unwrap_or(""), &entry))
                .unwrap_or_default();
            Ok(json!({
            "extension_id": extension_id.unwrap_or_default(),
            "cards": cards,
            "schemas": {},
            "transport": "rpc"
            }))
        }
        "extension.uiFeatures.get" => {
            let extension_id = extension_id_param(&request.params);
            let ui_features = extension_id
                .as_deref()
                .and_then(|id| state.extensions.get(id))
                .map(|entry| ui_features_for_entry(&entry))
                .unwrap_or_else(default_ui_features);
            Ok(json!({"ui_features": ui_features, "transport": "rpc"}))
        }
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
        "extension.models.list" => extension_models(state, &request.params).await,
        "extension.sessions.list" => Ok(json!({"sessions": [], "transport": "rpc"})),
        _ => Err(rpc_error(
            -32601,
            format!("Unsupported method: {}", request.method),
        )),
    }
}

fn extension_id_param(params: &JsonMap) -> Option<String> {
    params
        .get("extension_id")
        .or_else(|| params.get("agent"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

async fn extension_enabled_set(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    let enabled = params
        .get("enabled")
        .and_then(Value::as_bool)
        .ok_or_else(|| rpc_error(-32602, "enabled boolean is required"))?;
    let extension = state
        .extensions
        .set_enabled(&extension_id, enabled)
        .map_err(internal_rpc_error)?
        .ok_or_else(|| rpc_error(-32602, format!("Extension not found: {extension_id}")))?;
    let adapter = state
        .adapter
        .reload_extensions_if_running()
        .await
        .map_err(internal_rpc_error)?;
    Ok(json!({
        "ok": true,
        "extension": extension,
        "adapter": adapter,
        "transport": "rpc"
    }))
}

async fn extension_schema(
    state: &AppState,
    params: &JsonMap,
    adapter_method: &str,
    kind: SchemaKind,
) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    let mut schema = adapter_extension_request(state, &extension_id, adapter_method).await?;
    if let Value::Object(ref mut object) = schema {
        if matches!(kind, SchemaKind::Splash) {
            object
                .entry("extension_id")
                .or_insert_with(|| Value::String(extension_id.clone()));
        }
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(schema);
    }
    match kind {
        SchemaKind::Settings => Ok(json!({"version": "1", "fields": [], "transport": "rpc"})),
        SchemaKind::Splash => Ok(
            json!({"version": "1", "extension_id": extension_id, "fields": [], "transport": "rpc"}),
        ),
    }
}

async fn extension_models(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    let mut result =
        adapter_extension_request(state, &extension_id, methods::EXTENSION_LIST_MODELS).await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("models")
            .or_insert_with(|| Value::Array(Vec::new()));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({"models": [], "transport": "rpc"}))
}

async fn adapter_extension_request(
    state: &AppState,
    extension_id: &str,
    method: &str,
) -> Result<Value, RpcError> {
    state
        .adapter
        .initialize_extension(extension_id)
        .await
        .map_err(internal_rpc_error)?;
    state
        .adapter
        .client()
        .await
        .map_err(internal_rpc_error)?
        .request_value(method, json!({ "extension_id": extension_id }))
        .await
        .map_err(internal_rpc_error)
}

fn require_extension_id(params: &JsonMap) -> Result<String, RpcError> {
    extension_id_param(params).ok_or_else(|| rpc_error(-32602, "extension_id is required"))
}

fn ensure_registered_extension(state: &AppState, extension_id: &str) -> Result<(), RpcError> {
    if state.extensions.get(extension_id).is_some() {
        Ok(())
    } else {
        Err(rpc_error(
            -32602,
            format!("Extension not found: {extension_id}"),
        ))
    }
}

fn internal_rpc_error(error: impl std::fmt::Display) -> RpcError {
    rpc_error(-32603, error.to_string())
}

#[derive(Copy, Clone)]
enum SchemaKind {
    Settings,
    Splash,
}

fn request_cards_for_entry(extension_id: &str, entry: &ExtensionRegistryEntry) -> Vec<Value> {
    entry
        .ui
        .get("requestCards")
        .and_then(Value::as_array)
        .map(|cards| {
            cards
                .iter()
                .filter_map(|card| request_card_for_extension(extension_id, card))
                .collect()
        })
        .unwrap_or_default()
}

fn request_card_for_extension(extension_id: &str, card: &Value) -> Option<Value> {
    let mut object = card.as_object().cloned()?;
    let module_path = object
        .get("module")
        .and_then(Value::as_str)
        .map(str::trim)
        .map(|value| value.trim_start_matches('/'))
        .filter(|value| !value.is_empty())?
        .to_owned();
    let export_name = object
        .get("export")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("renderRequestCard")
        .to_owned();
    object.insert("module".to_owned(), Value::String(module_path.clone()));
    object.insert("export".to_owned(), Value::String(export_name));
    object.insert(
        "module_url".to_owned(),
        Value::String(format!(
            "/api/extensions/{extension_id}/assets/{module_path}"
        )),
    );
    Some(Value::Object(object))
}

fn ui_features_for_entry(entry: &ExtensionRegistryEntry) -> Value {
    let quote_parsing = entry
        .ui
        .get("semanticShellRibbon")
        .or_else(|| entry.ui.get("semantic_shell_ribbon"))
        .and_then(Value::as_object)
        .map(semantic_shell_quote_parsing)
        .unwrap_or(false);
    let tool_render_policy = entry
        .ui
        .get("toolRenderPolicy")
        .cloned()
        .unwrap_or_else(default_tool_render_policy);
    json!({
        "semanticShellRibbon": {
            "quoteParsing": quote_parsing
        },
        "toolRenderPolicy": tool_render_policy
    })
}

fn semantic_shell_quote_parsing(map: &Map<String, Value>) -> bool {
    map.get("quoteParsing")
        .or_else(|| map.get("quote_parsing"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn default_ui_features() -> Value {
    json!({
        "semanticShellRibbon": {
            "quoteParsing": false
        },
        "toolRenderPolicy": default_tool_render_policy()
    })
}

fn default_tool_render_policy() -> Value {
    json!({
        "default": {
            "request": {"kind": "plain"},
            "response": {"kind": "plain"}
        },
        "rules": []
    })
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
