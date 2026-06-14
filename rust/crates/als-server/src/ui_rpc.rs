use crate::{agent_edits::TrackedAgentDiff, sidebar_ipc, state::AppState};
use als_adapter_protocol::JsonMap;
use als_dto::APP_ID;
use als_jsonrpc::{ErrorResponse, RequestId, RpcError, SuccessResponse};
use regex::RegexBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State},
};
use std::{
    collections::HashSet,
    env, fs, io,
    path::{Path, PathBuf},
    process::{Command, Stdio},
};
use tracing::{info, warn};

const RPC_EVENT: &str = "rpc";
const RPC_NOTIFY_EVENT: &str = "rpc.notify";
const UI_RPC_NAMESPACE: &str = "/rpc/ui";
const JSONRPC_VERSION: &str = "2.0";
const DEFAULT_SEARCH_LIMIT: usize = 200;
const MAX_SEARCH_LIMIT: usize = 1000;

pub fn register_ui_rpc_namespace(io: &SocketIo) {
    io.ns(
        "/rpc/ui",
        async |socket: SocketRef, State(_state): State<AppState>| {
            socket.on(RPC_EVENT, handle_rpc_request);
        },
    );
}

async fn handle_rpc_request(
    State(state): State<AppState>,
    io: SocketIo,
    Data(request): Data<JsonRpcRequest>,
    ack: AckSender,
) {
    let id = request.id.clone();
    let response = match dispatch_rpc(&state, &io, request).await {
        Ok(result) => RpcAck::Success(SuccessResponse::new(id, result)),
        Err(error) => RpcAck::Error(ErrorResponse::new(id, error)),
    };
    let _ = ack.send(&response);
}

async fn dispatch_rpc(
    state: &AppState,
    io: &SocketIo,
    request: JsonRpcRequest,
) -> Result<Value, RpcError> {
    if request.jsonrpc != JSONRPC_VERSION {
        return Err(rpc_error(-32600, "Invalid JSON-RPC version"));
    }

    match request.method.as_str() {
        "view.get" => view_get(state),
        "view.set" => view_set(state, &request.params),
        "hostUi.get" => Ok(sidebar_ipc::host_ui_response(state)),
        "hostUi.recheck" => {
            let recheck = sidebar_ipc::recheck_status(io, state).await;
            let mut result = sidebar_ipc::host_ui_response(state);
            if let Some(object) = result.as_object_mut() {
                object.insert("recheck".to_owned(), recheck);
            }
            Ok(result)
        }
        "filesystem.home" => Ok(filesystem_home()),
        "filesystem.list" => filesystem_list(request.params).await,
        "filesystem.search" => filesystem_search(state, request.params).await,
        "project.summary.get" => project_summary_get(state, request.params).await,
        "project.agentDiff.accept" => project_agent_diff_accept(io, state, request.params).await,
        "project.agentDiff.reject" => project_agent_diff_reject(io, state, request.params).await,
        "project.agentDiff.rejectAll" => {
            project_agent_diff_reject_all(io, state, request.params).await
        }
        "agentEdits.documentState.get" => inline_agent_edits_document_state(state, request.params),
        "agentEdits.publish" => inline_agent_edits_publish(state, request.params),
        "agentEdits.decide" => inline_agent_edits_decide(state, request.params),
        "agentEdits.clear" => inline_agent_edits_clear(state, request.params),
        "agentEdits.list" => inline_agent_edits_list(state, request.params),
        "project.git.stage" => project_git_stage(state, request.params).await,
        "project.git.unstage" => project_git_unstage(state, request.params).await,
        "project.git.restore" => project_git_restore(state, request.params).await,
        "project.git.commit" => project_git_commit(state, request.params).await,
        "project.te2.status.get" => {
            Ok(sidebar_ipc::te2_project_status(io, state, request.params).await)
        }
        "project.te2.open" => Ok(sidebar_ipc::te2_project_open(io, state, request.params).await),
        "project.te2.create" => {
            Ok(sidebar_ipc::te2_project_create(io, state, request.params).await)
        }
        "app.windowState.publish" => app_window_state_publish(io, state, request.params).await,
        "file.open" => {
            let forwarded_params = file_open_sidebar_params(&request.params);
            info!(
                namespace = UI_RPC_NAMESPACE,
                event = RPC_EVENT,
                method = "file.open",
                path = ?request.params.get("path"),
                line = ?request.params.get("line"),
                column = ?request.params.get("column"),
                params = ?request.params,
                forwarded_params = ?forwarded_params,
                "received UI RPC file.open request"
            );
            let sent = sidebar_ipc::emit_agent_open(io, state, forwarded_params.clone()).await;
            Ok(json!({
                "ok": true,
                "sent": sent,
                "path": request.params.get("path").cloned().unwrap_or(Value::Null),
                "line": request.params.get("line").cloned().unwrap_or(Value::Null),
                "column": request.params.get("column").cloned().unwrap_or(Value::Null),
                "transport": "rpc"
            }))
        }
        "url.open" => url_open(request.params),
        _ => Err(rpc_error(
            -32601,
            format!("Unsupported method: {}", request.method),
        )),
    }
}

fn inline_agent_edits_document_state(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    state
        .inline_agent_edits
        .document_state(&params)
        .map_err(internal_rpc_error)
}

fn url_open(params: JsonMap) -> Result<Value, RpcError> {
    let url = params
        .get("url")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default();
    if url.is_empty() {
        return Err(rpc_error(-32602, "url.open requires a non-empty url"));
    }
    let lower = url.to_ascii_lowercase();
    if !lower.starts_with("http://") && !lower.starts_with("https://") {
        return Err(rpc_error(
            -32602,
            "url.open only supports http and https URLs",
        ));
    }
    if url.chars().any(|ch| ch.is_control() || ch.is_whitespace()) {
        return Err(rpc_error(
            -32602,
            "url.open rejects URLs with whitespace or control characters",
        ));
    }
    Command::new("xdg-open")
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| {
            rpc_error(
                -32603,
                format!("Failed to launch xdg-open for url.open: {error}"),
            )
        })?;
    Ok(json!({
        "ok": true,
        "url": url,
        "source": params.get("source").cloned().unwrap_or(Value::Null),
        "conversation_id": params.get("conversation_id").cloned().unwrap_or(Value::Null),
        "transport": "rpc"
    }))
}

async fn app_window_state_publish(
    io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let host_id = required_string_any(&params, &["host_id", "hostId"])?;
    let conversation_id = required_string_any(&params, &["conversation_id", "conversationId"])?;
    let meta = state
        .conversations
        .load_meta_if_exists(&conversation_id)
        .map_err(internal_rpc_error)?
        .ok_or_else(|| rpc_error(-32044, "Conversation not found"))?;
    let view = optional_string_any(&params, &["view"])
        .filter(|value| value == "conversation" || value == "splash" || value == "project")
        .unwrap_or_else(|| "conversation".to_owned());
    let token_id =
        optional_string_any(&params, &["token_id", "tokenId"]).unwrap_or_else(|| APP_ID.to_owned());
    let console_worker_id = optional_string_any(&params, &["console_worker_id", "consoleWorkerId"]);
    let title = optional_string_any(&params, &["title", "label"])
        .or_else(|| meta.alias.clone())
        .or_else(|| meta.label.clone())
        .or_else(|| setting_string(&meta.settings, "alias"))
        .or_else(|| setting_string(&meta.settings, "label"))
        .or_else(|| meta.title.clone())
        .unwrap_or_else(|| meta.conversation_id.clone());
    let base_url = format!("/app/{APP_ID}");
    let url = build_stateful_conversation_url(
        &base_url,
        &host_id,
        &token_id,
        console_worker_id.as_deref(),
        &conversation_id,
        &view,
    );
    let query_state = json!({
        "conversation_id": conversation_id.clone(),
        "view": view.clone(),
    });
    let sidebar_payload = json!({
        "lane": {
            "app_id": APP_ID,
            "base_url": base_url.clone(),
        },
        "app_id": APP_ID,
        "base_url": base_url.clone(),
        "host_id": host_id.clone(),
        "token_id": token_id.clone(),
        "console_worker_id": console_worker_id.clone().unwrap_or_default(),
        "state_kind": "conversation",
        "query_state": query_state,
        "url": url.clone(),
        "restore_url": url.clone(),
        "label": title.clone(),
        "title": title,
        "activate": false,
        "source": "als_rs_backend",
    });
    let sidebar = match sidebar_ipc::proxy_sidebar_rpc(
        io,
        state,
        "sidebar.window.state.update",
        sidebar_payload,
    )
    .await
    {
        Ok(value) => value,
        Err(error) => json!({
            "ok": false,
            "error": error.to_string(),
        }),
    };
    let sidebar_ok = sidebar.get("ok").and_then(Value::as_bool) != Some(false);
    Ok(json!({
        "ok": sidebar_ok,
        "transport": "rpc",
        "host_id": host_id,
        "conversation_id": conversation_id,
        "view": view,
        "url": url,
        "sidebar": sidebar,
    }))
}

fn inline_agent_edits_publish(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    state
        .inline_agent_edits
        .publish(&params)
        .map_err(internal_rpc_error)
}

fn inline_agent_edits_decide(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    state
        .inline_agent_edits
        .decide(&params)
        .map_err(internal_rpc_error)
}

fn inline_agent_edits_clear(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    state
        .inline_agent_edits
        .clear(&params)
        .map_err(internal_rpc_error)
}

fn inline_agent_edits_list(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    state
        .inline_agent_edits
        .list(&params)
        .map_err(internal_rpc_error)
}

fn file_open_sidebar_params(params: &JsonMap) -> JsonMap {
    let mut forwarded = JsonMap::new();
    if let Some(path) = params.get("path") {
        forwarded.insert("path".to_owned(), path.clone());
    }
    if let Some(line) = params.get("line") {
        forwarded.insert("line".to_owned(), line.clone());
    }
    if let Some(column) = params.get("column") {
        forwarded.insert("column".to_owned(), column.clone());
    }
    forwarded
}

fn view_get(state: &AppState) -> Result<Value, RpcError> {
    let selection = state.ui_selection.snapshot().map_err(internal_rpc_error)?;
    Ok(view_response(&selection))
}

fn view_set(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let snapshot = state
        .ui_selection
        .set_view(
            params
                .get("view")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned),
        )
        .map_err(internal_rpc_error)?;
    Ok(view_response(&snapshot))
}

fn view_response(selection: &crate::state::UiSelectionSnapshot) -> Value {
    let conversation_id = selection
        .active_conversation_id
        .as_ref()
        .map(|value| Value::String(value.clone()))
        .unwrap_or(Value::Null);
    json!({
        "active_view": selection.active_view,
        "active_conversation": conversation_id.clone(),
        "active_conversation_id": conversation_id.clone(),
        "conversation_id": conversation_id,
        "user_name": selection.user_name,
        "transport": "rpc"
    })
}

fn filesystem_home() -> Value {
    json!({
        "ok": true,
        "home": path_to_string(&home_dir()),
        "transport": "rpc",
    })
}

async fn filesystem_list(params: JsonMap) -> Result<Value, RpcError> {
    tokio::task::spawn_blocking(move || filesystem_list_sync(&params))
        .await
        .map_err(internal_rpc_error)?
}

fn filesystem_list_sync(params: &JsonMap) -> Result<Value, RpcError> {
    let logical = logical_absolute_path(params.get("path").and_then(Value::as_str), "~")
        .map_err(internal_rpc_error)?;
    let metadata = fs::metadata(&logical).map_err(|error| match error.kind() {
        io::ErrorKind::NotFound => rpc_error(-32044, "Path not found"),
        _ => internal_rpc_error(error),
    })?;
    if !metadata.is_dir() {
        return Err(rpc_error(-32602, "Path is not a directory"));
    }

    let mut items = Vec::new();
    let entries = fs::read_dir(&logical).map_err(internal_rpc_error)?;
    for entry in entries {
        let entry = entry.map_err(internal_rpc_error)?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let is_symlink = entry
            .file_type()
            .map(|file_type| file_type.is_symlink())
            .unwrap_or(false);
        let target_metadata = fs::metadata(&path).ok();
        let symlink_metadata = fs::symlink_metadata(&path).ok();
        let item_type = if target_metadata
            .as_ref()
            .map(|metadata| metadata.is_dir())
            .unwrap_or(false)
        {
            "directory"
        } else if target_metadata
            .as_ref()
            .map(|metadata| metadata.is_file())
            .unwrap_or(false)
        {
            "file"
        } else if is_symlink {
            "symlink"
        } else if symlink_metadata
            .as_ref()
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(false)
        {
            "symlink"
        } else {
            "other"
        };
        items.push(FilesystemItem {
            name,
            path: path_to_string(&path),
            item_type: item_type.to_owned(),
            is_symlink,
        });
    }
    sort_items(&mut items);
    let parent = lexical_parent(&logical).map(|path| path_to_string(&path));
    Ok(json!({
        "ok": true,
        "path": path_to_string(&logical),
        "parent": parent,
        "items": items,
        "transport": "rpc",
    }))
}

async fn filesystem_search(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let config_dir = state.config.roots.config_dir.clone();
    tokio::task::spawn_blocking(move || filesystem_search_sync(&config_dir, &params))
        .await
        .map_err(internal_rpc_error)?
}

async fn project_summary_get(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    let agent_diffs = if let Some(conversation_id) = conversation_id.as_deref() {
        state
            .agent_edits
            .list(conversation_id)
            .map(crate::agent_edits::agent_diffs_json)
            .map_err(internal_rpc_error)?
    } else {
        Value::Array(Vec::new())
    };
    let start = project_start_from_params(state, &params)?;
    let max_diff_bytes = params.get("max_diff_bytes").and_then(Value::as_u64);
    tokio::task::spawn_blocking(move || {
        crate::project_summary::project_summary(&start, max_diff_bytes)
            .map_err(internal_rpc_error)
            .map(|mut value| {
                if let Some(object) = value.as_object_mut() {
                    object.insert("transport".to_owned(), json!("rpc"));
                    object.insert("agent_diffs".to_owned(), agent_diffs);
                }
                value
            })
    })
    .await
    .map_err(internal_rpc_error)?
}

async fn project_git_stage(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let start = project_start_from_params(state, &params)?;
    let paths = paths_from_params(&params);
    tokio::task::spawn_blocking(move || {
        crate::project_git::stage_paths(&start, &paths).map_err(internal_rpc_error)
    })
    .await
    .map_err(internal_rpc_error)?
}

async fn project_git_unstage(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let start = project_start_from_params(state, &params)?;
    let paths = paths_from_params(&params);
    tokio::task::spawn_blocking(move || {
        crate::project_git::unstage_paths(&start, &paths).map_err(internal_rpc_error)
    })
    .await
    .map_err(internal_rpc_error)?
}

async fn project_git_restore(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let start = project_start_from_params(state, &params)?;
    let paths = paths_from_params(&params);
    tokio::task::spawn_blocking(move || {
        crate::project_git::restore_paths(&start, &paths).map_err(internal_rpc_error)
    })
    .await
    .map_err(internal_rpc_error)?
}

async fn project_git_commit(state: &AppState, params: JsonMap) -> Result<Value, RpcError> {
    let start = project_start_from_params(state, &params)?;
    let message = required_string(&params, "message")?;
    tokio::task::spawn_blocking(move || {
        crate::project_git::commit_staged(&start, &message).map_err(internal_rpc_error)
    })
    .await
    .map_err(internal_rpc_error)?
}

pub(crate) async fn project_agent_diff_accept(
    io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = required_string(&params, "conversation_id")?;
    let diff_id = required_string(&params, "diff_id")?;
    let removed = state
        .agent_edits
        .accept(&conversation_id, &diff_id)
        .map_err(internal_rpc_error)?;
    if let Some(entry) = removed.as_ref() {
        emit_project_agent_diff_removed(io, state, entry).await;
    }
    Ok(json!({
        "ok": true,
        "conversation_id": conversation_id,
        "diff_id": diff_id,
        "accepted": removed.is_some(),
        "transport": "rpc",
    }))
}

pub(crate) async fn project_agent_diff_reject(
    io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = required_string(&params, "conversation_id")?;
    let diff_id = required_string(&params, "diff_id")?;
    let force = params.get("force").and_then(Value::as_bool) == Some(true);
    let entry = state
        .agent_edits
        .get(&conversation_id, &diff_id)
        .map_err(internal_rpc_error)?
        .ok_or_else(|| rpc_error(-32044, "Tracked diff not found"))?;
    let repo_root = repo_root_for_agent_diff(state, &params, &entry)?;
    apply_project_agent_diff_reverse(&conversation_id, &entry, repo_root, force).await?;
    let removed = state
        .agent_edits
        .remove(&conversation_id, &diff_id)
        .map_err(internal_rpc_error)?;
    if let Some(entry) = removed.as_ref() {
        emit_project_agent_diff_removed(io, state, entry).await;
    }
    Ok(json!({
        "ok": true,
        "conversation_id": conversation_id,
        "diff_id": diff_id,
        "rejected": removed.is_some(),
        "transport": "rpc",
    }))
}

pub(crate) async fn project_agent_diff_reject_all(
    io: &SocketIo,
    state: &AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = required_string(&params, "conversation_id")?;
    let entries = state
        .agent_edits
        .list_newest_first(&conversation_id)
        .map_err(internal_rpc_error)?;
    let requested_count = entries.len();
    let mut rejected_ids = Vec::new();

    for snapshot_entry in entries {
        let Some(entry) = state
            .agent_edits
            .get(&conversation_id, &snapshot_entry.id)
            .map_err(internal_rpc_error)?
        else {
            continue;
        };
        let repo_root = repo_root_for_agent_diff(state, &params, &entry)?;
        apply_project_agent_diff_reverse(&conversation_id, &entry, repo_root, false).await?;
        let removed = state
            .agent_edits
            .remove(&conversation_id, &entry.id)
            .map_err(internal_rpc_error)?;
        if let Some(entry) = removed.as_ref() {
            rejected_ids.push(entry.id.clone());
            emit_project_agent_diff_removed(io, state, entry).await;
        }
    }

    Ok(json!({
        "ok": true,
        "conversation_id": conversation_id,
        "requested_count": requested_count,
        "rejected_count": rejected_ids.len(),
        "rejected_ids": rejected_ids,
        "transport": "rpc",
    }))
}

async fn apply_project_agent_diff_reverse(
    conversation_id: &str,
    entry: &TrackedAgentDiff,
    repo_root: PathBuf,
    force: bool,
) -> Result<(), RpcError> {
    let entry_for_apply = entry.clone();
    let repo_root_for_log = path_to_string(&repo_root);
    let apply_result = tokio::task::spawn_blocking(move || {
        let mode = if force {
            crate::reverse_patch::ReversePatchMode::Fuzzy
        } else {
            crate::reverse_patch::ReversePatchMode::Strict
        };
        crate::agent_edits::apply_reverse_patch_with_mode(&repo_root, &entry_for_apply, mode)
    })
    .await
    .map_err(internal_rpc_error)?;
    if let Err(error) = apply_result {
        if !force {
            if let Some(mismatch) = reverse_patch_mismatch(&error) {
                return Err(agent_diff_metadata_mismatch_error(entry, mismatch));
            }
        }
        warn!(
            conversation_id = %conversation_id,
            diff_id = %entry.id.as_str(),
            repo_root = %repo_root_for_log,
            path = entry.path.as_deref().unwrap_or(""),
            abs = entry.abs.as_deref().unwrap_or(""),
            rel = entry.rel.as_deref().unwrap_or(""),
            source = %entry.source,
            diff_bytes = entry.diff_bytes,
            additions = entry.additions,
            deletions = entry.deletions,
            diff_preview = %diff_log_preview(&entry.diff_text),
            error = %error,
            "project agent diff reject failed"
        );
        return Err(internal_rpc_error(error));
    }
    Ok(())
}

fn reverse_patch_mismatch(
    error: &anyhow::Error,
) -> Option<&crate::reverse_patch::ReversePatchMismatch> {
    error
        .chain()
        .find_map(|cause| cause.downcast_ref::<crate::reverse_patch::ReversePatchMismatch>())
}

fn agent_diff_metadata_mismatch_error(
    entry: &TrackedAgentDiff,
    mismatch: &crate::reverse_patch::ReversePatchMismatch,
) -> RpcError {
    RpcError::new(
        -32062,
        "The diff metadata does not match the file metadata on disk.",
        Some(json!({
            "kind": "agent_diff_metadata_mismatch",
            "diff_id": entry.id,
            "path": entry.path,
            "abs": entry.abs,
            "rel": entry.rel,
            "line": entry.line,
            "column": entry.column,
            "modified_start": mismatch.modified_start,
            "detail": mismatch.detail,
            "message": "The diff metadata does not match the file metadata on disk, probably because of a recent change to the file.",
        })),
    )
}

pub async fn emit_project_agent_diff_added(io: &SocketIo, entry: &TrackedAgentDiff) {
    emit_rpc_notification(io, "project.agentDiff.added", json!(entry)).await;
}

async fn emit_project_agent_diff_removed(
    io: &SocketIo,
    state: &AppState,
    entry: &TrackedAgentDiff,
) {
    emit_rpc_notification(
        io,
        "project.agentDiff.removed",
        json!({
            "id": entry.id,
            "conversation_id": entry.conversation_id,
            "path": entry.path,
            "abs": entry.abs,
            "rel": entry.rel,
            "repo_root": entry.repo_root,
        }),
    )
    .await;
    clear_inline_agent_edit_widget(state, entry).await;
}

async fn clear_inline_agent_edit_widget(state: &AppState, entry: &TrackedAgentDiff) {
    let Some(clear_params) = entry.inline_clear_payload() else {
        return;
    };
    match state.inline_agent_edits.clear(&clear_params) {
        Ok(_) => {}
        Err(error) => {
            warn!(%error, "inline agent edit ledger clear failed after project diff removal");
        }
    }
    let _ = sidebar_ipc::clear_agent_edits(state, clear_params).await;
    let Some(document_params) = entry.inline_document_state_params() else {
        return;
    };
    match state.inline_agent_edits.document_state(&document_params) {
        Ok(Value::Object(projection)) => {
            let _ = sidebar_ipc::publish_agent_edits_with_current_client(state, projection).await;
        }
        Ok(value) => {
            warn!(
                ?value,
                "inline agent edit document state was not an object after clear"
            );
        }
        Err(error) => {
            warn!(%error, "failed to publish inline agent edit document state after clear");
        }
    }
}

async fn emit_rpc_notification(io: &SocketIo, method: &str, params: Value) {
    let notification = json!({
        "jsonrpc": JSONRPC_VERSION,
        "method": method,
        "params": params,
    });
    let Some(namespace) = io.of(UI_RPC_NAMESPACE) else {
        warn!("UI RPC namespace is unavailable for project event fanout");
        return;
    };
    if let Err(error) = namespace.emit(RPC_NOTIFY_EVENT, &notification).await {
        warn!(error = %error, method, "failed to emit project event over UI RPC");
    }
}

fn conversation_cwd_from_params(state: &AppState, params: &JsonMap) -> Option<String> {
    let conversation_id = params
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())?;
    let meta = state
        .conversations
        .load_meta_if_exists(conversation_id)
        .ok()??;
    meta.settings
        .get("cwd")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| {
            meta.cwd
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
        })
}

fn repo_root_for_agent_diff(
    state: &AppState,
    params: &JsonMap,
    entry: &TrackedAgentDiff,
) -> Result<PathBuf, RpcError> {
    if let Some(repo_root) = entry
        .repo_root
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        return Ok(PathBuf::from(repo_root));
    }
    if params
        .get("path")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_some()
    {
        return project_start_from_params(state, params);
    }
    conversation_cwd_from_params(state, params)
        .map(PathBuf::from)
        .ok_or_else(|| rpc_error(-32602, "Tracked diff has no project root"))
}

fn project_start_from_params(state: &AppState, params: &JsonMap) -> Result<PathBuf, RpcError> {
    let conversation_cwd = conversation_cwd_from_params(state, params);
    let host_root = state
        .host_ui
        .snapshot()
        .ok()
        .and_then(|snapshot| snapshot.project_root);
    let config_dir = state.config.roots.config_dir.to_string_lossy().into_owned();
    let fallback = conversation_cwd
        .as_deref()
        .or(host_root.as_deref())
        .unwrap_or(config_dir.as_str());
    logical_absolute_path(params.get("path").and_then(Value::as_str), fallback)
        .map_err(internal_rpc_error)
}

fn paths_from_params(params: &JsonMap) -> Vec<String> {
    params
        .get("paths")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn required_string(params: &JsonMap, key: &str) -> Result<String, RpcError> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| rpc_error(-32602, format!("{key} is required")))
}

fn required_string_any(params: &JsonMap, keys: &[&str]) -> Result<String, RpcError> {
    optional_string_any(params, keys)
        .ok_or_else(|| rpc_error(-32602, format!("{} is required", keys.join(" or "))))
}

fn optional_string_any(params: &JsonMap, keys: &[&str]) -> Option<String> {
    keys.iter()
        .filter_map(|key| params.get(*key).and_then(Value::as_str))
        .map(str::trim)
        .find(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn setting_string(settings: &JsonMap, key: &str) -> Option<String> {
    settings
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn build_stateful_conversation_url(
    base_url: &str,
    host_id: &str,
    token_id: &str,
    console_worker_id: Option<&str>,
    conversation_id: &str,
    view: &str,
) -> String {
    let mut params = vec![
        ("embed", "1".to_owned()),
        ("te2_host_id", host_id.to_owned()),
        ("te2_token_id", token_id.to_owned()),
    ];
    if let Some(console_worker_id) = console_worker_id
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        params.push(("te2_console_worker_id", console_worker_id.to_owned()));
    }
    params.push(("conversation_id", conversation_id.to_owned()));
    params.push(("view", view.to_owned()));
    let query = params
        .into_iter()
        .map(|(key, value)| format!("{key}={}", percent_encode_query_value(&value)))
        .collect::<Vec<_>>()
        .join("&");
    format!("{base_url}?{query}")
}

fn percent_encode_query_value(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.as_bytes() {
        let ch = *byte as char;
        if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '~') {
            encoded.push(ch);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

fn filesystem_search_sync(config_dir: &Path, params: &JsonMap) -> Result<Value, RpcError> {
    let query = params
        .get("query")
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("");
    if query.is_empty() {
        return Ok(json!({"ok": true, "root": Value::Null, "items": [], "transport": "rpc"}));
    }
    let pattern = RegexBuilder::new(query)
        .case_insensitive(true)
        .build()
        .map_err(|error| rpc_error(-32602, format!("Invalid regex: {error}")))?;
    let limit = params
        .get("limit")
        .and_then(Value::as_u64)
        .map(|value| value as usize)
        .unwrap_or(DEFAULT_SEARCH_LIMIT)
        .clamp(1, MAX_SEARCH_LIMIT);
    let root = params.get("root").and_then(Value::as_str);
    let fallback = config_dir.to_string_lossy();
    let logical_base =
        logical_absolute_path(root, fallback.as_ref()).map_err(internal_rpc_error)?;
    let metadata = fs::metadata(&logical_base).map_err(|error| match error.kind() {
        io::ErrorKind::NotFound => rpc_error(-32044, "Root not found"),
        _ => internal_rpc_error(error),
    })?;
    if !metadata.is_dir() {
        return Err(rpc_error(-32602, "Root is not a directory"));
    }

    let repo_root = detect_repo_root(&logical_base);
    let rels = rg_list_files(&repo_root).or_else(|_| walk_files(&repo_root, limit * 8));
    let rels = rels.map_err(internal_rpc_error)?;
    let mut items = Vec::new();
    let mut seen = HashSet::new();
    for rel in rels {
        let full_path = repo_root.join(&rel);
        let full_text = path_to_string(&full_path);
        if pattern.is_match(&rel) || pattern.is_match(&full_text) {
            push_search_item(&mut items, &mut seen, &full_path, "file");
            if items.len() >= limit {
                break;
            }
        }
        for parent in Path::new(&rel).ancestors().skip(1) {
            if parent.as_os_str().is_empty() || parent == Path::new(".") {
                continue;
            }
            let parent_rel = parent.to_string_lossy();
            let parent_path = repo_root.join(parent);
            let parent_text = path_to_string(&parent_path);
            if pattern.is_match(&parent_rel) || pattern.is_match(&parent_text) {
                push_search_item(&mut items, &mut seen, &parent_path, "directory");
                if items.len() >= limit {
                    break;
                }
            }
        }
        if items.len() >= limit {
            break;
        }
    }
    sort_items(&mut items);
    Ok(json!({
        "ok": true,
        "root": path_to_string(&repo_root),
        "items": items,
        "transport": "rpc",
    }))
}

fn push_search_item(
    items: &mut Vec<FilesystemItem>,
    seen: &mut HashSet<String>,
    path: &Path,
    item_type: &str,
) {
    let key = path_to_string(path);
    if !seen.insert(key.clone()) {
        return;
    }
    let name = path
        .file_name()
        .map(|value| value.to_string_lossy().into_owned())
        .unwrap_or_else(|| key.clone());
    items.push(FilesystemItem {
        name,
        path: key,
        item_type: item_type.to_owned(),
        is_symlink: fs::symlink_metadata(path)
            .map(|metadata| metadata.file_type().is_symlink())
            .unwrap_or(false),
    });
}

fn logical_absolute_path(raw_path: Option<&str>, fallback: &str) -> Result<PathBuf, io::Error> {
    let raw = raw_path
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or(fallback);
    let expanded = expand_home(raw);
    let path = PathBuf::from(expanded);
    if path.is_absolute() {
        Ok(path)
    } else {
        Ok(env::current_dir()?.join(path))
    }
}

fn expand_home(raw: &str) -> String {
    if raw == "~" {
        return home_dir().to_string_lossy().into_owned();
    }
    if let Some(rest) = raw.strip_prefix("~/") {
        return home_dir().join(rest).to_string_lossy().into_owned();
    }
    raw.to_owned()
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn lexical_parent(path: &Path) -> Option<PathBuf> {
    let parent = path.parent()?;
    if parent == path {
        None
    } else {
        Some(parent.to_path_buf())
    }
}

fn detect_repo_root(start: &Path) -> PathBuf {
    let output = Command::new("git")
        .arg("-C")
        .arg(start)
        .arg("rev-parse")
        .arg("--show-toplevel")
        .output();
    if let Ok(output) = output {
        if output.status.success() {
            let root = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            if !root.is_empty() {
                return PathBuf::from(root);
            }
        }
    }
    start.to_path_buf()
}

fn rg_list_files(root: &Path) -> Result<Vec<String>, io::Error> {
    let output = Command::new("rg")
        .arg("--files")
        .arg("--glob")
        .arg("!.git/*")
        .current_dir(root)
        .output()?;
    if !output.status.success() {
        return Err(io::Error::other("rg --files failed"));
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| {
            let trimmed = line.trim();
            (!trimmed.is_empty()).then(|| trimmed.to_owned())
        })
        .collect())
}

fn walk_files(root: &Path, limit: usize) -> Result<Vec<String>, io::Error> {
    let mut out = Vec::new();
    walk_files_inner(root, root, limit, &mut out)?;
    Ok(out)
}

fn walk_files_inner(
    root: &Path,
    dir: &Path,
    limit: usize,
    out: &mut Vec<String>,
) -> Result<(), io::Error> {
    if out.len() >= limit {
        return Ok(());
    }
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = entry.file_name();
        if name == ".git" {
            continue;
        }
        let metadata = fs::metadata(&path).ok();
        if metadata
            .as_ref()
            .map(|metadata| metadata.is_dir())
            .unwrap_or(false)
        {
            walk_files_inner(root, &path, limit, out)?;
        } else if metadata
            .as_ref()
            .map(|metadata| metadata.is_file())
            .unwrap_or(false)
        {
            if let Ok(rel) = path.strip_prefix(root) {
                out.push(rel.to_string_lossy().into_owned());
            }
        }
        if out.len() >= limit {
            break;
        }
    }
    Ok(())
}

fn sort_items(items: &mut [FilesystemItem]) {
    items.sort_by(|left, right| {
        let left_rank = usize::from(left.item_type != "directory");
        let right_rank = usize::from(right.item_type != "directory");
        left_rank
            .cmp(&right_rank)
            .then_with(|| left.name.to_lowercase().cmp(&right.name.to_lowercase()))
    });
}

fn path_to_string(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn diff_log_preview(diff_text: &str) -> String {
    const MAX_LINES: usize = 24;
    const MAX_CHARS_PER_LINE: usize = 220;

    let mut out = Vec::new();
    let mut total_lines = 0usize;
    for line in diff_text.lines() {
        total_lines += 1;
        if out.len() < MAX_LINES {
            out.push(truncate_preview_line(line, MAX_CHARS_PER_LINE));
        }
    }
    if total_lines > MAX_LINES {
        out.push(format!("... ({} more lines)", total_lines - MAX_LINES));
    }
    if out.is_empty() {
        "<empty>".to_owned()
    } else {
        out.join("\\n")
    }
}

fn truncate_preview_line(line: &str, max_chars: usize) -> String {
    let mut out = String::new();
    for (index, ch) in line.chars().enumerate() {
        if index >= max_chars {
            out.push_str("...");
            break;
        }
        out.push(ch);
    }
    out
}

#[derive(Clone, Debug, Serialize)]
struct FilesystemItem {
    name: String,
    path: String,
    #[serde(rename = "type")]
    item_type: String,
    is_symlink: bool,
}

fn rpc_error(code: i64, message: impl Into<String>) -> RpcError {
    RpcError::new(code, message, None)
}

fn internal_rpc_error(error: impl std::fmt::Display) -> RpcError {
    rpc_error(-32603, error.to_string())
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
    use crate::config::{AdapterConfig, FrameworkShellConfig, ServerConfig};
    use crate::state::AppState;
    use als_dto::RuntimeRoots;
    use serde_json::json;
    use std::{
        path::PathBuf,
        sync::atomic::{AtomicU64, Ordering},
        time::{SystemTime, UNIX_EPOCH},
    };

    static NEXT_TEST_ROOT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn expands_home_without_resolving_symlinks() {
        let path = logical_absolute_path(Some("~/project"), "~").unwrap();
        assert!(path_to_string(&path).ends_with("/project"));
    }

    #[test]
    fn sorts_directories_before_files() {
        let mut items = vec![
            FilesystemItem {
                name: "z.txt".to_owned(),
                path: "/z.txt".to_owned(),
                item_type: "file".to_owned(),
                is_symlink: false,
            },
            FilesystemItem {
                name: "a-dir".to_owned(),
                path: "/a-dir".to_owned(),
                item_type: "directory".to_owned(),
                is_symlink: false,
            },
        ];
        sort_items(&mut items);
        assert_eq!(items[0].name, "a-dir");
    }

    #[tokio::test]
    async fn empty_search_returns_ok_payload() {
        let value = filesystem_search_sync(Path::new("."), &JsonMap::new()).unwrap();
        assert_eq!(value["ok"], json!(true));
        assert_eq!(value["items"], json!([]));
    }

    #[test]
    fn filesystem_home_returns_ok_payload() {
        let value = filesystem_home();
        assert_eq!(value["ok"], json!(true));
        assert!(value["home"].as_str().is_some());
    }

    #[test]
    fn builds_stateful_conversation_url() {
        let value = build_stateful_conversation_url(
            "/app/als-rs",
            "slot:als-rs:als_rs:a1b2",
            "als_rs",
            Some("als_rs:a1b2"),
            "conv 1",
            "conversation",
        );
        assert_eq!(
            value,
            "/app/als-rs?embed=1&te2_host_id=slot%3Aals-rs%3Aals_rs%3Aa1b2&te2_token_id=als_rs&te2_console_worker_id=als_rs%3Aa1b2&conversation_id=conv%201&view=conversation"
        );
    }

    #[test]
    fn project_summary_context_uses_conversation_cwd() {
        let root = test_root("als-rs-ui-project-test");
        let state = test_state(&root);
        let mut settings = JsonMap::new();
        settings.insert("cwd".to_owned(), json!("/conversation/settings"));
        state
            .conversations
            .create(crate::conversation_store::CreateConversationRequest {
                conversation_id: Some("conv-project".to_owned()),
                settings,
                ..Default::default()
            })
            .unwrap();

        let mut params = JsonMap::new();
        params.insert("conversation_id".to_owned(), json!("conv-project"));
        assert_eq!(
            conversation_cwd_from_params(&state, &params).as_deref(),
            Some("/conversation/settings")
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn project_summary_context_falls_back_to_meta_cwd() {
        let root = test_root("als-rs-ui-project-test");
        let state = test_state(&root);
        state
            .conversations
            .create(crate::conversation_store::CreateConversationRequest {
                conversation_id: Some("conv-project-meta".to_owned()),
                cwd: Some("/conversation/meta".to_owned()),
                ..Default::default()
            })
            .unwrap();
        let mut meta = state.conversations.load_meta("conv-project-meta").unwrap();
        meta.settings.remove("cwd");
        fs::write(
            root.join("data/conversations/conv-project-meta/meta.json"),
            serde_json::to_string_pretty(&meta).unwrap(),
        )
        .unwrap();

        let mut params = JsonMap::new();
        params.insert("conversation_id".to_owned(), json!("conv-project-meta"));
        assert_eq!(
            conversation_cwd_from_params(&state, &params).as_deref(),
            Some("/conversation/meta")
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn view_get_and_set_use_shared_ui_selection_state() {
        let root = test_root("als-rs-ui-view-test");
        let state = test_state(&root);

        state
            .ui_selection
            .select(Some("conv-ui".to_owned()), Some("conversation".to_owned()))
            .unwrap();

        let initial = view_get(&state).unwrap();
        assert_eq!(initial["conversation_id"], "conv-ui");
        assert_eq!(initial["active_view"], "conversation");

        let mut params = JsonMap::new();
        params.insert("view".to_owned(), json!("splash"));
        let updated = view_set(&state, &params).unwrap();
        assert_eq!(updated["conversation_id"], "conv-ui");
        assert_eq!(updated["active_view"], "splash");

        let _ = fs::remove_dir_all(root);
    }

    fn test_state(root: &Path) -> AppState {
        AppState::new(ServerConfig {
            host: "127.0.0.1".to_owned(),
            port: 0,
            extensions_dir: root.join("extensions"),
            roots: RuntimeRoots {
                data_dir: root.join("data"),
                cache_dir: root.join("cache"),
                config_dir: root.join("config"),
                static_dir: root.join("static"),
            },
            adapters: AdapterConfig {
                python_bin: "python".to_owned(),
            },
            framework_shells: FrameworkShellConfig::default(),
        })
    }

    fn test_root(prefix: &str) -> PathBuf {
        let sequence = NEXT_TEST_ROOT.fetch_add(1, Ordering::Relaxed);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "{}-{}-{}-{}",
            prefix,
            std::process::id(),
            nanos,
            sequence
        ))
    }
}
