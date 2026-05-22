use crate::{sidebar_ipc, state::AppState};
use als_adapter_protocol::JsonMap;
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
    process::Command,
};

const RPC_EVENT: &str = "rpc";
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
        "file.open" => {
            let sent = sidebar_ipc::emit_agent_open(io, state, request.params.clone()).await;
            Ok(json!({
                "ok": true,
                "sent": sent,
                "path": request.params.get("path").cloned().unwrap_or(Value::Null),
                "line": request.params.get("line").cloned().unwrap_or(Value::Null),
                "column": request.params.get("column").cloned().unwrap_or(Value::Null),
                "transport": "rpc"
            }))
        }
        "url.open" => Ok(json!({
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
    let conversation_cwd = conversation_cwd_from_params(state, &params);
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
    let start = logical_absolute_path(params.get("path").and_then(Value::as_str), fallback)
        .map_err(internal_rpc_error)?;
    let max_diff_bytes = params.get("max_diff_bytes").and_then(Value::as_u64);
    tokio::task::spawn_blocking(move || {
        crate::project_summary::project_summary(&start, max_diff_bytes)
            .map_err(internal_rpc_error)
            .map(|mut value| {
                if let Some(object) = value.as_object_mut() {
                    object.insert("transport".to_owned(), json!("rpc"));
                }
                value
            })
    })
    .await
    .map_err(internal_rpc_error)?
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
