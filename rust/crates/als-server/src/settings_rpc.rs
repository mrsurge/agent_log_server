use crate::config::FrameworkShellConfig;
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
use std::{
    fs,
    path::{Component, Path, PathBuf},
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
        "config.get" => config_get(state),
        "config.update" => config_update(state, request.params),
        "status.get" => Ok(json!({"running": true, "transport": "rpc"})),
        "extensions.list" => Ok(json!({"extensions": state.extensions.list(), "transport": "rpc"})),
        "extensions.reload" => {
            let extensions = state.extensions.reload().map_err(internal_rpc_error)?;
            let adapter = state
                .adapter
                .reload_extensions_if_running(state.extensions.enabled_overrides(), None, None)
                .await
                .map_err(internal_rpc_error)?;
            let extensions = adapter
                .as_ref()
                .map(|value| state.extensions.apply_runtime_extensions(value))
                .unwrap_or(extensions);
            Ok(json!({
                "ok": true,
                "extensions": extensions,
                "adapter": adapter,
                "transport": "rpc"
            }))
        }
        "extension.enabled.set" => extension_enabled_set(state, &request.params).await,
        "extension.install" => extension_install(state, &request.params).await,
        "extension.package.validate" => extension_package_validate(state, &request.params).await,
        "extension.package.install" => extension_package_install(state, &request.params).await,
        "extension.package.update" => extension_package_update(state, &request.params).await,
        "extension.package.remove" => extension_package_remove(state, &request.params).await,
        "extension.debug.probe" => extension_debug_probe(state, &request.params).await,
        "extension.session.bind" => Ok(
            json!({"ok": false, "error": format!("{} is not implemented in ALS-RS yet", request.method), "transport": "rpc"}),
        ),
        "extension.settingsSchema.get" => {
            extension_schema(state, &request.params, "", SchemaKind::Settings).await
        }
        "extension.settingsSchema.fragment.get" => {
            extension_schema_fragment(state, &request.params).await
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
        "extension.runtimeOptions.get" => extension_runtime_options(state, &request.params).await,
        "extension.providerInfo.get" => extension_provider_info(state, &request.params).await,
        "extension.schemaInteraction.run" => {
            extension_schema_interaction(state, &request.params).await
        }
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
        "extension.plan.get" => extension_plan(state, &request.params).await,
        "extension.models.list" => extension_models(state, &request.params).await,
        "extension.sessions.list" => extension_sessions(state, &request.params).await,
        "extension.session.state.get" => extension_session_state(state, &request.params).await,
        "extension.session.unload" => extension_session_unload(state, &request.params).await,
        _ => Err(rpc_error(
            -32601,
            format!("Unsupported method: {}", request.method),
        )),
    }
}

fn config_get(state: &AppState) -> Result<Value, RpcError> {
    let selection = state.ui_selection.snapshot().map_err(internal_rpc_error)?;
    Ok(json!({
        "ok": true,
        "user_name": selection.user_name,
        "show_console_worker_id": selection.show_console_worker_id,
        "active_conversation": selection.active_conversation_id.clone(),
        "active_conversation_id": selection.active_conversation_id,
        "active_view": selection.active_view,
        "transport": "rpc",
    }))
}

fn config_update(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let user_name = if params.contains_key("user_name") {
        Some(
            params
                .get("user_name")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned),
        )
    } else {
        None
    };
    let show_console_worker_id = if params.contains_key("show_console_worker_id") {
        Some(
            params
                .get("show_console_worker_id")
                .and_then(Value::as_bool)
                .ok_or_else(|| rpc_error(-32602, "show_console_worker_id boolean is required"))?,
        )
    } else {
        None
    };
    let selection = state
        .ui_selection
        .update_app_config(user_name, show_console_worker_id)
        .map_err(internal_rpc_error)?;
    Ok(json!({
        "ok": true,
        "user_name": selection.user_name,
        "show_console_worker_id": selection.show_console_worker_id,
        "active_conversation": selection.active_conversation_id.clone(),
        "active_conversation_id": selection.active_conversation_id,
        "active_view": selection.active_view,
        "transport": "rpc",
    }))
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
        .reload_extensions_if_running(
            state.extensions.enabled_overrides(),
            Some(vec![extension_id.clone()]),
            enabled.then(|| extension_id.clone()),
        )
        .await
        .map_err(internal_rpc_error)?;
    let extension = adapter
        .as_ref()
        .map(|value| state.extensions.apply_runtime_extensions(value))
        .and_then(|_| state.extensions.get(&extension_id))
        .unwrap_or(extension);
    Ok(json!({
        "ok": true,
        "extension": extension,
        "adapter": adapter,
        "transport": "rpc"
    }))
}

async fn extension_install(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    install_extension_dependencies(state, &extension_id).await
}

async fn install_extension_dependencies(
    state: &AppState,
    extension_id: &str,
) -> Result<Value, RpcError> {
    state
        .adapter
        .initialize_extension(extension_id)
        .await
        .map_err(internal_rpc_error)?;
    let mut result = state
        .adapter
        .client()
        .await
        .map_err(internal_rpc_error)?
        .request_value(
            methods::EXTENSION_INSTALL_DEPENDENCIES,
            json!({ "extension_id": extension_id }),
        )
        .await
        .map_err(internal_rpc_error)?;
    state.extensions.apply_runtime_extensions(&result);
    if let Value::Object(ref mut object) = result {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
    }
    Ok(result)
}

async fn extension_package_validate(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let mut result =
        adapter_package_request(state, methods::EXTENSION_PACKAGE_VALIDATE, params).await?;
    if let Value::Object(ref mut object) = result {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
    }
    Ok(result)
}

async fn extension_package_install(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let result = adapter_package_request(state, methods::EXTENSION_PACKAGE_INSTALL, params).await?;
    finalize_package_mutation(state, result, params).await
}

async fn extension_package_update(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let result = adapter_package_request(state, methods::EXTENSION_PACKAGE_UPDATE, params).await?;
    finalize_package_mutation(state, result, params).await
}

async fn extension_package_remove(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let result = adapter_package_request(state, methods::EXTENSION_PACKAGE_REMOVE, params).await?;
    finalize_package_mutation(state, result, params).await
}

async fn extension_debug_probe(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    state
        .adapter
        .initialize_extension(&extension_id)
        .await
        .map_err(internal_rpc_error)?;
    let ensure_manager = params
        .get("ensure_manager")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let adapter = state
        .adapter
        .client()
        .await
        .map_err(internal_rpc_error)?
        .request_value(
            methods::EXTENSION_DEBUG_PROBE,
            json!({
                "extension_id": extension_id,
                "ensure_manager": ensure_manager,
            }),
        )
        .await
        .map_err(internal_rpc_error)?;
    let adapter_events = state.adapter.events().snapshot().await;
    Ok(json!({
        "ok": true,
        "transport": "rpc",
        "rust": {
            "pid": std::process::id(),
            "framework_shells": framework_shell_config_probe(&state.config.framework_shells),
        },
        "adapter": adapter,
        "adapter_events": adapter_events,
    }))
}

async fn extension_schema(
    state: &AppState,
    params: &JsonMap,
    adapter_method: &str,
    kind: SchemaKind,
) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    let entry = ensure_registered_extension(state, &extension_id)?;
    if matches!(kind, SchemaKind::Settings) {
        let (mut schema, source) = match read_extension_schema_file(&entry, "settings_schema.json")?
        {
            Some(schema) => (schema, "file"),
            None => (json!({"version": "1", "fields": []}), "missing_file"),
        };
        if let Value::Object(ref mut object) = schema {
            object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
            object.insert("schema_source".to_owned(), Value::String(source.to_owned()));
        }
        return Ok(schema);
    }
    let mut schema = adapter_extension_request(state, &extension_id, adapter_method).await?;
    if let Value::Object(ref mut object) = schema {
        if matches!(kind, SchemaKind::Splash) {
            object
                .entry("extension_id")
                .or_insert_with(|| Value::String(extension_id.clone()));
        }
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        object
            .entry("schema_source")
            .or_insert_with(|| Value::String("adapter".to_owned()));
        return Ok(schema);
    }
    match kind {
        SchemaKind::Settings => Ok(json!({"version": "1", "fields": [], "transport": "rpc"})),
        SchemaKind::Splash => Ok(
            json!({"version": "1", "extension_id": extension_id, "fields": [], "transport": "rpc"}),
        ),
    }
}

async fn extension_schema_fragment(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    let target = params
        .get("target")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| rpc_error(-32602, "target is required"))?;
    let entry = ensure_registered_extension(state, &extension_id)?;
    let Some(path) = extension_root_relative_file_path(&entry, target) else {
        return Err(rpc_error(-32602, "invalid schema fragment target"));
    };
    if !path.is_file() {
        return Err(rpc_error(
            -32602,
            format!("schema fragment not found: {target}"),
        ));
    }
    let raw = fs::read_to_string(&path).map_err(|error| {
        internal_rpc_error(format!("failed to read {}: {error}", path.display()))
    })?;
    let mut parsed: Value = serde_json::from_str(&raw).map_err(|error| {
        internal_rpc_error(format!("failed to parse {}: {error}", path.display()))
    })?;
    if let Value::Array(fields) = parsed {
        parsed = json!({ "fields": fields });
    }
    if let Value::Object(ref mut object) = parsed {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        object.insert(
            "schema_source".to_owned(),
            Value::String("fragment_file".to_owned()),
        );
        object.insert("schema_target".to_owned(), Value::String(target.to_owned()));
        return Ok(parsed);
    }
    Err(internal_rpc_error(format!(
        "{} must contain a JSON object or fields array",
        path.display()
    )))
}

fn read_extension_schema_file(
    entry: &ExtensionRegistryEntry,
    file_name: &str,
) -> Result<Option<Value>, RpcError> {
    let Some(path) = extension_root_file_path(entry, file_name) else {
        return Ok(None);
    };
    if !path.is_file() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path).map_err(|error| {
        internal_rpc_error(format!("failed to read {}: {error}", path.display()))
    })?;
    let parsed: Value = serde_json::from_str(&raw).map_err(|error| {
        internal_rpc_error(format!("failed to parse {}: {error}", path.display()))
    })?;
    if parsed.is_object() {
        Ok(Some(parsed))
    } else {
        Err(internal_rpc_error(format!(
            "{} must contain a JSON object",
            path.display()
        )))
    }
}

fn extension_root_dir(entry: &ExtensionRegistryEntry) -> Option<PathBuf> {
    let mut path = entry.source_root.clone();
    for component in Path::new(&entry.path).components() {
        match component {
            Component::Normal(part) => {
                let text = part.to_str()?.trim();
                if text.is_empty() {
                    return None;
                }
                path.push(text);
                if fs::symlink_metadata(&path)
                    .ok()
                    .is_some_and(|metadata| metadata.file_type().is_symlink())
                {
                    return None;
                }
            }
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    Some(path)
}

fn extension_root_file_path(entry: &ExtensionRegistryEntry, file_name: &str) -> Option<PathBuf> {
    let mut path = extension_root_dir(entry)?;
    path.push(file_name);
    if fs::symlink_metadata(&path)
        .ok()
        .is_some_and(|metadata| metadata.file_type().is_symlink())
    {
        return None;
    }
    Some(path)
}

fn extension_root_relative_file_path(
    entry: &ExtensionRegistryEntry,
    relative_target: &str,
) -> Option<PathBuf> {
    if relative_target.trim().is_empty() {
        return None;
    }
    let mut path = extension_root_dir(entry)?;
    for component in Path::new(relative_target).components() {
        match component {
            Component::Normal(part) => {
                let text = part.to_str()?.trim();
                if text.is_empty() {
                    return None;
                }
                path.push(text);
                if fs::symlink_metadata(&path)
                    .ok()
                    .is_some_and(|metadata| metadata.file_type().is_symlink())
                {
                    return None;
                }
            }
            Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => return None,
        }
    }
    Some(path)
}

async fn extension_models(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    let mut request_params = JsonMap::new();
    for (key, value) in params {
        if key == "extension_id" {
            continue;
        }
        request_params.insert(key.clone(), value.clone());
    }
    request_params.insert(
        "extension_id".to_owned(),
        Value::String(extension_id.clone()),
    );
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_LIST_MODELS,
        request_params,
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("models")
            .or_insert_with(|| Value::Array(Vec::new()));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({"models": [], "transport": "rpc"}))
}

async fn extension_sessions(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    let mut request_params = JsonMap::new();
    for (key, value) in params {
        if key == "extension_id" {
            continue;
        }
        request_params.insert(key.clone(), value.clone());
    }
    request_params.insert(
        "extension_id".to_owned(),
        Value::String(extension_id.clone()),
    );
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_LIST_SESSIONS,
        request_params,
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("sessions")
            .or_insert_with(|| Value::Array(Vec::new()));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({"sessions": [], "transport": "rpc"}))
}

async fn extension_session_state(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let request = live_session_request(state, params)?;
    if !live_session_request_enabled(&request.entry) {
        return Ok(unsupported_live_session_response(&request));
    }
    let mut result = match adapter_extension_request_with_params(
        state,
        methods::EXTENSION_SESSION_STATE_GET,
        request.to_adapter_params(),
    )
    .await
    {
        Ok(value) => value,
        Err(error) => {
            return Ok(json!({
                "ok": false,
                "supported": true,
                "state": "unknown",
                "loaded": false,
                "unload_supported": false,
                "error": error.message,
                "extension_id": request.extension_id,
                "conversation_id": request.conversation_id,
                "provider_session_id": request.provider_session_id,
                "transport": "rpc"
            }));
        }
    };
    if let Value::Object(ref mut object) = result {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "ok": false,
        "supported": true,
        "state": "unknown",
        "loaded": false,
        "unload_supported": false,
        "extension_id": request.extension_id,
        "conversation_id": request.conversation_id,
        "provider_session_id": request.provider_session_id,
        "transport": "rpc"
    }))
}

async fn extension_session_unload(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let request = live_session_request(state, params)?;
    if !live_session_request_enabled(&request.entry) {
        return Ok(unsupported_live_session_response(&request));
    }
    let mut result = match adapter_extension_request_with_params(
        state,
        methods::EXTENSION_SESSION_UNLOAD,
        request.to_adapter_params(),
    )
    .await
    {
        Ok(value) => value,
        Err(error) => {
            return Ok(json!({
                "ok": false,
                "supported": true,
                "state": "unknown",
                "loaded": false,
                "unload_supported": true,
                "error": error.message,
                "extension_id": request.extension_id,
                "conversation_id": request.conversation_id,
                "provider_session_id": request.provider_session_id,
                "transport": "rpc"
            }));
        }
    };
    if let Value::Object(ref mut object) = result {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "ok": false,
        "supported": true,
        "state": "unknown",
        "loaded": false,
        "unload_supported": true,
        "extension_id": request.extension_id,
        "conversation_id": request.conversation_id,
        "provider_session_id": request.provider_session_id,
        "transport": "rpc"
    }))
}

async fn extension_runtime_options(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let request = runtime_options_request(state, params)?;
    ensure_registered_extension(state, &request.extension_id)?;
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_GET_RUNTIME_OPTIONS,
        request.to_adapter_params(),
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("agent")
            .or_insert_with(|| Value::String(request.extension_id));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "agent": request.extension_id,
        "fields": {},
        "quickControls": [],
        "transport": "rpc"
    }))
}

async fn extension_provider_info(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let request = provider_info_request(state, params)?;
    ensure_registered_extension(state, &request.extension_id)?;
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_GET_PROVIDER_INFO,
        request.to_adapter_params(),
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("extension_id")
            .or_insert_with(|| Value::String(request.extension_id.clone()));
        if let Some(conversation_id) = request.conversation_id.as_ref() {
            object
                .entry("conversation_id")
                .or_insert_with(|| Value::String(conversation_id.clone()));
        }
        if let Some(provider_session_id) = request.provider_session_id.as_ref() {
            object
                .entry("provider_session_id")
                .or_insert_with(|| Value::String(provider_session_id.clone()));
        }
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "ok": false,
        "supported": true,
        "extension_id": request.extension_id,
        "conversation_id": request.conversation_id,
        "provider_session_id": request.provider_session_id,
        "transport": "rpc",
        "error": "Invalid provider info response"
    }))
}

async fn extension_schema_interaction(
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let extension_id = require_extension_id(params)?;
    ensure_registered_extension(state, &extension_id)?;
    let interaction_id = params
        .get("interaction_id")
        .or_else(|| params.get("interactionId"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| rpc_error(-32602, "interaction_id is required"))?
        .to_owned();
    let mut request_params = params.clone();
    request_params.insert(
        "extension_id".to_owned(),
        Value::String(extension_id.clone()),
    );
    request_params.insert(
        "interaction_id".to_owned(),
        Value::String(interaction_id.clone()),
    );
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_SCHEMA_INTERACTION_RUN,
        request_params,
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("extension_id")
            .or_insert_with(|| Value::String(extension_id));
        object
            .entry("interaction_id")
            .or_insert_with(|| Value::String(interaction_id));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "ok": false,
        "supported": true,
        "extension_id": extension_id,
        "interaction_id": interaction_id,
        "error": "Invalid schema interaction response",
        "transport": "rpc"
    }))
}

async fn extension_plan(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let request = plan_request(state, params)?;
    ensure_registered_extension(state, &request.extension_id)?;
    let mut result = adapter_extension_request_with_params(
        state,
        methods::EXTENSION_GET_PLAN,
        request.to_adapter_params(),
    )
    .await?;
    if let Value::Object(ref mut object) = result {
        object
            .entry("extension_id")
            .or_insert_with(|| Value::String(request.extension_id.clone()));
        object
            .entry("conversation_id")
            .or_insert_with(|| Value::String(request.conversation_id.clone()));
        object
            .entry("has_plan")
            .or_insert_with(|| Value::Bool(false));
        object
            .entry("plan_exists")
            .or_insert_with(|| Value::Bool(false));
        object
            .entry("plan_content")
            .or_insert_with(|| Value::String(String::new()));
        object.entry("plan_path").or_insert(Value::Null);
        object.entry("plan_source").or_insert(Value::Null);
        object
            .entry("has_todo")
            .or_insert_with(|| Value::Bool(false));
        object
            .entry("plan_steps")
            .or_insert_with(|| Value::Array(Vec::new()));
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        return Ok(result);
    }
    Ok(json!({
        "extension_id": request.extension_id,
        "conversation_id": request.conversation_id,
        "has_plan": false,
        "plan_exists": false,
        "plan_content": "",
        "plan_path": Value::Null,
        "plan_source": Value::Null,
        "has_todo": false,
        "plan_steps": [],
        "transport": "rpc"
    }))
}

struct LiveSessionRequest {
    extension_id: String,
    conversation_id: String,
    provider_session_id: Option<String>,
    settings: JsonMap,
    entry: ExtensionRegistryEntry,
}

impl LiveSessionRequest {
    fn to_adapter_params(&self) -> JsonMap {
        let mut params = JsonMap::new();
        params.insert(
            "extension_id".to_owned(),
            Value::String(self.extension_id.clone()),
        );
        params.insert(
            "conversation_id".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        if let Some(provider_session_id) = self.provider_session_id.as_ref() {
            params.insert(
                "provider_session_id".to_owned(),
                Value::String(provider_session_id.clone()),
            );
            params.insert(
                "thread_id".to_owned(),
                Value::String(provider_session_id.clone()),
            );
        }
        params.insert("settings".to_owned(), Value::Object(self.settings.clone()));
        params
    }
}

fn live_session_request(
    state: &AppState,
    params: &JsonMap,
) -> Result<LiveSessionRequest, RpcError> {
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let meta = state
        .conversations
        .load_meta_if_exists(&conversation_id)
        .map_err(internal_rpc_error)?
        .ok_or_else(|| rpc_error(-32602, format!("Conversation not found: {conversation_id}")))?;
    let extension_id = extension_id_param(params)
        .or_else(|| {
            meta.extension_id
                .as_deref()
                .or(meta.agent_type.as_deref())
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            meta.settings
                .get("agent")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .ok_or_else(|| rpc_error(-32602, "extension_id or conversation extension is required"))?;
    let entry = state
        .extensions
        .get(&extension_id)
        .ok_or_else(|| rpc_error(-32602, format!("Extension not found: {extension_id}")))?;
    let provider_session_id = params
        .get("provider_session_id")
        .or_else(|| params.get("thread_id"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or(meta.thread_id.clone())
        .or(meta.provider_session_id.clone());
    Ok(LiveSessionRequest {
        extension_id,
        conversation_id,
        provider_session_id,
        settings: meta.settings,
        entry,
    })
}

fn live_session_request_enabled(entry: &ExtensionRegistryEntry) -> bool {
    entry
        .capabilities
        .get("live_session_request")
        .or_else(|| entry.capabilities.get("liveSessionRequest"))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn unsupported_live_session_response(request: &LiveSessionRequest) -> Value {
    json!({
        "ok": true,
        "supported": false,
        "state": "unsupported",
        "loaded": false,
        "unload_supported": false,
        "extension_id": request.extension_id,
        "conversation_id": request.conversation_id,
        "provider_session_id": request.provider_session_id,
        "transport": "rpc"
    })
}

struct RuntimeOptionsRequest {
    extension_id: String,
    conversation_id: Option<String>,
    settings: JsonMap,
}

impl RuntimeOptionsRequest {
    fn to_adapter_params(&self) -> JsonMap {
        let mut params = JsonMap::new();
        params.insert(
            "extension_id".to_owned(),
            Value::String(self.extension_id.clone()),
        );
        if let Some(conversation_id) = self.conversation_id.as_ref() {
            params.insert(
                "conversation_id".to_owned(),
                Value::String(conversation_id.clone()),
            );
        }
        params.insert("settings".to_owned(), Value::Object(self.settings.clone()));
        params
    }
}

struct ProviderInfoRequest {
    extension_id: String,
    conversation_id: Option<String>,
    provider_session_id: Option<String>,
    settings: JsonMap,
}

impl ProviderInfoRequest {
    fn to_adapter_params(&self) -> JsonMap {
        let mut params = JsonMap::new();
        params.insert(
            "extension_id".to_owned(),
            Value::String(self.extension_id.clone()),
        );
        if let Some(conversation_id) = self.conversation_id.as_ref() {
            params.insert(
                "conversation_id".to_owned(),
                Value::String(conversation_id.clone()),
            );
        }
        if let Some(provider_session_id) = self.provider_session_id.as_ref() {
            params.insert(
                "provider_session_id".to_owned(),
                Value::String(provider_session_id.clone()),
            );
            params.insert(
                "thread_id".to_owned(),
                Value::String(provider_session_id.clone()),
            );
        }
        params.insert("settings".to_owned(), Value::Object(self.settings.clone()));
        params
    }
}

fn provider_info_request(
    state: &AppState,
    params: &JsonMap,
) -> Result<ProviderInfoRequest, RpcError> {
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    let meta = conversation_id
        .as_deref()
        .map(|id| {
            state
                .conversations
                .load_meta_if_exists(id)
                .map_err(internal_rpc_error)
        })
        .transpose()?
        .flatten();
    let extension_id = extension_id_param(params)
        .or_else(|| {
            params
                .get("agent")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            meta.as_ref()
                .and_then(|meta| meta.extension_id.as_deref().or(meta.agent_type.as_deref()))
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            meta.as_ref()
                .and_then(|meta| meta.settings.get("agent"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .ok_or_else(|| rpc_error(-32602, "extension_id or conversation extension is required"))?;
    let provider_session_id = params
        .get("provider_session_id")
        .or_else(|| params.get("thread_id"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| {
            meta.as_ref()
                .and_then(|meta| meta.provider_session_id.clone().or(meta.thread_id.clone()))
        });
    let settings = meta
        .as_ref()
        .map(|meta| meta.settings.clone())
        .unwrap_or_default();
    Ok(ProviderInfoRequest {
        extension_id,
        conversation_id,
        provider_session_id,
        settings,
    })
}

fn runtime_options_request(
    state: &AppState,
    params: &JsonMap,
) -> Result<RuntimeOptionsRequest, RpcError> {
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    let meta = conversation_id
        .as_deref()
        .map(|id| {
            state
                .conversations
                .load_meta_if_exists(id)
                .map_err(internal_rpc_error)
        })
        .transpose()?
        .flatten();
    let extension_id = params
        .get("agent")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| {
            meta.as_ref()
                .and_then(|meta| meta.extension_id.as_deref().or(meta.agent_type.as_deref()))
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            meta.as_ref()
                .and_then(|meta| meta.settings.get("agent"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            state
                .extensions
                .list()
                .into_iter()
                .find(|entry| entry.active)
                .map(|entry| entry.id)
        })
        .ok_or_else(|| rpc_error(-32602, "extension_id or active extension is required"))?;
    let settings = meta
        .as_ref()
        .map(|meta| meta.settings.clone())
        .unwrap_or_default();
    Ok(RuntimeOptionsRequest {
        extension_id,
        conversation_id,
        settings,
    })
}

struct PlanRequest {
    extension_id: String,
    conversation_id: String,
}

impl PlanRequest {
    fn to_adapter_params(&self) -> JsonMap {
        let mut params = JsonMap::new();
        params.insert(
            "extension_id".to_owned(),
            Value::String(self.extension_id.clone()),
        );
        params.insert(
            "conversation_id".to_owned(),
            Value::String(self.conversation_id.clone()),
        );
        params
    }
}

fn plan_request(state: &AppState, params: &JsonMap) -> Result<PlanRequest, RpcError> {
    let selection = state.ui_selection.snapshot().map_err(internal_rpc_error)?;
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or(selection.active_conversation_id)
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let meta = state
        .conversations
        .load_meta_if_exists(&conversation_id)
        .map_err(internal_rpc_error)?
        .ok_or_else(|| rpc_error(-32602, format!("Conversation not found: {conversation_id}")))?;
    let extension_id = extension_id_param(params)
        .or_else(|| {
            meta.extension_id
                .as_deref()
                .or(meta.agent_type.as_deref())
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .or_else(|| {
            meta.settings
                .get("agent")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
        .ok_or_else(|| rpc_error(-32602, "extension_id or conversation extension is required"))?;
    Ok(PlanRequest {
        extension_id,
        conversation_id,
    })
}

async fn adapter_extension_request(
    state: &AppState,
    extension_id: &str,
    method: &str,
) -> Result<Value, RpcError> {
    let mut params = JsonMap::new();
    params.insert(
        "extension_id".to_owned(),
        Value::String(extension_id.to_owned()),
    );
    adapter_extension_request_with_params(state, method, params).await
}

async fn adapter_extension_request_with_params(
    state: &AppState,
    method: &str,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let extension_id = params
        .get("extension_id")
        .and_then(Value::as_str)
        .ok_or_else(|| rpc_error(-32602, "extension_id is required"))?;
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
        .request_value(method, params)
        .await
        .map_err(internal_rpc_error)
}

async fn adapter_package_request(
    state: &AppState,
    method: &str,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let init_extension_id = state
        .extensions
        .list()
        .into_iter()
        .find(|entry| entry.active)
        .or_else(|| state.extensions.list().into_iter().next())
        .map(|entry| entry.id)
        .ok_or_else(|| rpc_error(-32602, "at least one extension root entry is required"))?;
    state
        .adapter
        .initialize_extension(&init_extension_id)
        .await
        .map_err(internal_rpc_error)?;
    state
        .adapter
        .client()
        .await
        .map_err(internal_rpc_error)?
        .request_value(method, params)
        .await
        .map_err(internal_rpc_error)
}

async fn finalize_package_mutation(
    state: &AppState,
    mut result: Value,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let ok = result
        .as_object()
        .and_then(|object| object.get("ok"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let mut dependency_install: Option<Value> = None;
    if ok {
        let changed_extension_id = package_result_extension_id(&result, params);
        state.extensions.reload().map_err(internal_rpc_error)?;
        let wait_ready_extension_id = changed_extension_id
            .as_ref()
            .filter(|extension_id| state.extensions.get(extension_id).is_some())
            .cloned();
        let adapter = state
            .adapter
            .reload_extensions_if_running(
                state.extensions.enabled_overrides(),
                changed_extension_id
                    .clone()
                    .map(|extension_id| vec![extension_id]),
                wait_ready_extension_id,
            )
            .await
            .map_err(internal_rpc_error)?;
        if let Some(adapter) = adapter.as_ref() {
            state.extensions.apply_runtime_extensions(adapter);
        }
        if params
            .get("install_dependencies")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            if let Some(extension_id) = package_result_extension_id(&result, params) {
                dependency_install =
                    Some(install_extension_dependencies(state, &extension_id).await?);
            }
        }
    }
    if let Value::Object(ref mut object) = result {
        object.insert("transport".to_owned(), Value::String("rpc".to_owned()));
        object.insert(
            "extensions".to_owned(),
            serde_json::to_value(state.extensions.list()).map_err(internal_rpc_error)?,
        );
        if let Some(value) = dependency_install {
            object.insert("dependency_install".to_owned(), value);
        }
    }
    Ok(result)
}

fn package_result_extension_id(result: &Value, params: &JsonMap) -> Option<String> {
    result
        .as_object()
        .and_then(|object| object.get("extension_id"))
        .and_then(Value::as_str)
        .or_else(|| {
            result
                .as_object()
                .and_then(|object| object.get("result"))
                .and_then(Value::as_object)
                .and_then(|object| object.get("extension_id"))
                .and_then(Value::as_str)
        })
        .or_else(|| params.get("extension_id").and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn require_extension_id(params: &JsonMap) -> Result<String, RpcError> {
    extension_id_param(params).ok_or_else(|| rpc_error(-32602, "extension_id is required"))
}

fn ensure_registered_extension(
    state: &AppState,
    extension_id: &str,
) -> Result<ExtensionRegistryEntry, RpcError> {
    state
        .extensions
        .get(extension_id)
        .ok_or_else(|| rpc_error(-32602, format!("Extension not found: {extension_id}")))
}

fn internal_rpc_error(error: impl std::fmt::Display) -> RpcError {
    rpc_error(-32603, error.to_string())
}

fn framework_shell_config_probe(config: &FrameworkShellConfig) -> Value {
    json!({
        "FRAMEWORK_SHELLS_BASE_DIR": value_probe(config.base_dir.as_deref()),
        "FRAMEWORK_SHELLS_SECRET": secret_probe(config.secret.as_deref()),
        "FRAMEWORK_SHELLS_REPO_FINGERPRINT": value_probe(config.repo_fingerprint.as_deref()),
        "FRAMEWORK_SHELLS_SECRET_FINGERPRINT": value_probe(config.secret_fingerprint.as_deref().or(config.repo_fingerprint.as_deref())),
        "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID": value_probe(config.fws_socketio_server_pid.as_deref()),
        "FRAMEWORK_SHELLS_RUN_ID": value_probe(config.run_id.as_deref()),
    })
}

fn value_probe(value: Option<&str>) -> Value {
    json!({
        "present": value.is_some_and(|value| !value.is_empty()),
        "value": value,
    })
}

fn secret_probe(value: Option<&str>) -> Value {
    json!({
        "present": value.is_some_and(|value| !value.is_empty()),
        "length": value.map(str::len).unwrap_or(0),
    })
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
    let tool_render_policy = normalize_tool_render_policy(entry.ui.get("toolRenderPolicy"));
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

fn normalize_tool_render_policy(policy_raw: Option<&Value>) -> Value {
    let mut policy = default_tool_render_policy();
    let Some(policy_map) = policy_raw.and_then(Value::as_object) else {
        return policy;
    };

    if let Some(default_raw) = policy_map.get("default").and_then(Value::as_object) {
        let mut normalized_default = Map::new();
        if let Some(spec) = first_tool_render_spec(default_raw, &["request", "args", "arguments"]) {
            normalized_default.insert("request".to_owned(), spec);
        }
        if let Some(spec) = first_tool_render_spec(default_raw, &["response", "result"]) {
            normalized_default.insert("response".to_owned(), spec);
        }
        if let Some(fields) = first_tool_render_field_map(
            default_raw,
            &["requestFields", "argsFields", "argumentsFields"],
        ) {
            normalized_default.insert("requestFields".to_owned(), fields);
        }
        if let Some(fields) =
            first_tool_render_field_map(default_raw, &["responseFields", "resultFields"])
        {
            normalized_default.insert("responseFields".to_owned(), fields);
        }
        if !normalized_default.is_empty() {
            if let Some(existing_default) = policy.get_mut("default").and_then(Value::as_object_mut)
            {
                for (key, value) in normalized_default {
                    existing_default.insert(key, value);
                }
            }
        }
    }

    if let Some(rules_raw) = policy_map.get("rules").and_then(Value::as_array) {
        let rules: Vec<Value> = rules_raw
            .iter()
            .filter_map(normalize_tool_render_rule)
            .collect();
        if let Some(policy_map) = policy.as_object_mut() {
            policy_map.insert("rules".to_owned(), Value::Array(rules));
        }
    }

    policy
}

fn normalize_tool_render_spec(spec_raw: &Value) -> Option<Value> {
    if let Some(text) = spec_raw
        .as_str()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let lowered = text.to_ascii_lowercase();
        if lowered == "plain" || lowered == "markdown" {
            return Some(json!({"kind": lowered}));
        }
        if lowered == "hljs" {
            return Some(json!({"kind": "hljs"}));
        }
        return Some(json!({"kind": "hljs", "language": text}));
    }

    let spec_map = spec_raw.as_object()?;
    let kind = spec_map
        .get("kind")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_lowercase)
        .unwrap_or_default();
    let language = spec_map
        .get("language")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());

    if kind != "plain" && kind != "markdown" && kind != "hljs" {
        return language.map(|language| json!({"kind": "hljs", "language": language}));
    }

    let mut spec = Map::new();
    let is_hljs = kind == "hljs";
    spec.insert("kind".to_owned(), Value::String(kind));
    if is_hljs {
        if let Some(language) = language {
            spec.insert("language".to_owned(), Value::String(language.to_owned()));
        }
    }
    Some(Value::Object(spec))
}

fn normalize_tool_render_field_map(fields_raw: &Value) -> Option<Value> {
    let fields_map = fields_raw.as_object()?;
    let mut normalized = Map::new();
    for (key, value) in fields_map {
        let field_name = key.trim();
        if field_name.is_empty() {
            continue;
        }
        if let Some(spec) = normalize_tool_render_spec(value) {
            normalized.insert(field_name.to_owned(), spec);
        }
    }
    if normalized.is_empty() {
        None
    } else {
        Some(Value::Object(normalized))
    }
}

fn first_tool_render_spec(map: &Map<String, Value>, keys: &[&str]) -> Option<Value> {
    keys.iter()
        .filter_map(|key| map.get(*key))
        .find_map(normalize_tool_render_spec)
}

fn first_tool_render_field_map(map: &Map<String, Value>, keys: &[&str]) -> Option<Value> {
    keys.iter()
        .filter_map(|key| map.get(*key))
        .find_map(normalize_tool_render_field_map)
}

fn normalize_tool_render_rule(rule_raw: &Value) -> Option<Value> {
    let rule_map = rule_raw.as_object()?;
    let mut rule = Map::new();

    for field in ["server", "tool", "serverPrefix", "toolPrefix"] {
        if let Some(value) = rule_map
            .get(field)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            rule.insert(field.to_owned(), Value::String(value.to_owned()));
        }
    }

    for field in ["servers", "tools"] {
        if let Some(items_raw) = rule_map.get(field).and_then(Value::as_array) {
            let items: Vec<Value> = items_raw
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(|value| Value::String(value.to_owned()))
                .collect();
            if !items.is_empty() {
                rule.insert(field.to_owned(), Value::Array(items));
            }
        }
    }

    if let Some(spec) = first_tool_render_spec(rule_map, &["request", "args", "arguments"]) {
        rule.insert("request".to_owned(), spec);
    }
    if let Some(spec) = first_tool_render_spec(rule_map, &["response", "result"]) {
        rule.insert("response".to_owned(), spec);
    }
    if let Some(fields) = first_tool_render_field_map(
        rule_map,
        &["requestFields", "argsFields", "argumentsFields"],
    ) {
        rule.insert("requestFields".to_owned(), fields);
    }
    if let Some(fields) = first_tool_render_field_map(rule_map, &["responseFields", "resultFields"])
    {
        rule.insert("responseFields".to_owned(), fields);
    }

    if rule.is_empty() {
        None
    } else {
        Some(Value::Object(rule))
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

#[cfg(test)]
mod tests {
    use super::*;

    fn test_extension_entry() -> ExtensionRegistryEntry {
        ExtensionRegistryEntry {
            id: "test-ext".to_owned(),
            name: "Test Extension".to_owned(),
            extension_type: "adapter".to_owned(),
            path: "test_ext".to_owned(),
            source_root: PathBuf::from("extensions"),
            source_kind: "builtin".to_owned(),
            enabled: true,
            active: true,
            version: "0.1.0".to_owned(),
            dependency_ok: true,
            dependency_status: "ok".to_owned(),
            dependency_message: String::new(),
            dependency_details: Map::new(),
            has_dependency_check: false,
            has_dependency_install: false,
            install_source: Map::new(),
            installer_meta: Map::new(),
            manifest: Map::new(),
            capabilities: Map::new(),
            ui: Map::new(),
        }
    }

    #[test]
    fn schema_fragment_targets_stay_under_extension_root() {
        let entry = test_extension_entry();

        assert_eq!(
            extension_root_relative_file_path(&entry, "settings/model_extras.json"),
            Some(PathBuf::from(
                "extensions/test_ext/settings/model_extras.json"
            ))
        );
        assert!(extension_root_relative_file_path(&entry, "../settings.json").is_none());
        assert!(extension_root_relative_file_path(&entry, "settings/../model.json").is_none());
        assert!(extension_root_relative_file_path(&entry, "/settings/model.json").is_none());
    }

    #[test]
    fn normalizes_tool_render_policy_aliases_and_specs() {
        let policy = normalize_tool_render_policy(Some(&json!({
            "default": {
                "args": "markdown",
                "result": {"kind": "hljs", "language": "json"},
                "argsFields": {
                    "content": "markdown",
                    "code": {"kind": "hljs", "language": "javascript"},
                    "ignored": {"kind": "unknown"}
                },
                "resultFields": {
                    "value": "json"
                }
            },
            "rules": [
                {
                    "server": "agent-pty-blocks",
                    "toolPrefix": "kb_",
                    "response": "markdown"
                },
                {
                    "servers": ["te2-mcp", "", 42],
                    "tools": ["te2_console_eval"],
                    "argumentsFields": {
                        "code": "javascript"
                    },
                    "responseFields": {
                        "value": {"language": "json"}
                    }
                },
                "bad-rule"
            ]
        })));

        assert_eq!(policy["default"]["request"], json!({"kind": "markdown"}));
        assert_eq!(
            policy["default"]["response"],
            json!({"kind": "hljs", "language": "json"})
        );
        assert_eq!(
            policy["default"]["requestFields"]["code"],
            json!({"kind": "hljs", "language": "javascript"})
        );
        assert!(policy["default"]["requestFields"].get("ignored").is_none());
        assert_eq!(
            policy["default"]["responseFields"]["value"],
            json!({"kind": "hljs", "language": "json"})
        );
        assert_eq!(policy["rules"].as_array().map(Vec::len), Some(2));
        assert_eq!(policy["rules"][0]["response"], json!({"kind": "markdown"}));
        assert_eq!(
            policy["rules"][1]["requestFields"]["code"],
            json!({"kind": "hljs", "language": "javascript"})
        );
        assert_eq!(
            policy["rules"][1]["responseFields"]["value"],
            json!({"kind": "hljs", "language": "json"})
        );
    }

    #[test]
    fn falls_back_to_plain_policy_for_invalid_manifest_value() {
        assert_eq!(
            normalize_tool_render_policy(Some(&json!("markdown"))),
            default_tool_render_policy()
        );
        assert_eq!(
            normalize_tool_render_policy(Some(&json!({"rules": "bad"}))),
            default_tool_render_policy()
        );
    }
}
