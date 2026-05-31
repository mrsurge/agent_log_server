use crate::{
    adapter_process::AdapterCapturedEvent,
    card_truncation::{sanitize_live_card_event, sanitize_transcript_card_entry},
    conversation_store::{
        ConversationMeta, ConversationMetaUpdate, CreateConversationRequest,
        ForkConversationRequest, TranscriptOffset,
    },
    ipc, sidebar_ipc,
    state::AppState,
};
use als_adapter_protocol::{
    ConversationControlParams, ConversationForkParams, ConversationResumeParams,
    ConversationSendParams, JsonMap, McpContext, events, methods,
};
use als_jsonrpc::{ErrorResponse, RequestId, RpcError, SuccessResponse};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State},
};
use std::{
    path::PathBuf,
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::broadcast::error::RecvError;
use tracing::warn;

const RPC_EVENT: &str = "rpc";
const RPC_NOTIFY_EVENT: &str = "rpc.notify";
const JSONRPC_VERSION: &str = "2.0";

const METHOD_GET: &str = "conversation.get";
const METHOD_LIST: &str = "conversation.list";
const METHOD_CREATE: &str = "conversation.create";
const METHOD_SELECT: &str = "conversation.select";
const METHOD_UPDATE: &str = "conversation.update";
const METHOD_DELETE: &str = "conversation.delete";
const METHOD_FORK: &str = "conversation.fork";
const METHOD_DRAFT_SET: &str = "conversation.draft.set";
const METHOD_SEND: &str = "conversation.send";
const METHOD_REPLAY_GET_CHUNK: &str = "conversation.replay.getChunk";
const METHOD_INTERRUPT: &str = "conversation.interrupt";
const METHOD_COMPACT: &str = "conversation.compact";
const METHOD_APPROVAL_RESPOND: &str = "conversation.approval.respond";
const METHOD_SHELL_EXEC: &str = "conversation.shell.exec";
const METHOD_PINS_SET: &str = "conversation.pins.set";
const METHOD_LIST_UPDATED: &str = "conversation.list.updated";
const AGENT_PTY_ASK_USER_REQUEST_METHOD: &str = "agent-pty/ask-user";
const AGENT_PTY_BLOCKS_MCP_SERVER_NAME: &str = "agent-pty-blocks";
const TE2_MCP_SERVER_NAME: &str = "te2-mcp";
const DEFAULT_TE2_BASE_URL: &str = "http://127.0.0.1:8089";

pub fn register_conversations_rpc_namespace(io: &SocketIo) {
    io.ns(
        "/rpc/conversations",
        async |socket: SocketRef, State(_state): State<AppState>| {
            socket.on(RPC_EVENT, handle_rpc_request);
        },
    );
}

pub fn start_adapter_event_fanout(io: SocketIo, state: AppState) {
    let mut adapter_events = state.adapter.events().subscribe();
    tokio::spawn(async move {
        loop {
            match adapter_events.recv().await {
                Ok(event) => {
                    if let Err(error) = handle_adapter_event(&io, &state, event).await {
                        warn!(error = %error.message, "failed to fan out adapter event");
                    }
                }
                Err(RecvError::Lagged(count)) => {
                    warn!(count, "ALS-RS adapter event fanout lagged");
                }
                Err(RecvError::Closed) => break,
            }
        }
    });
}

async fn handle_rpc_request(
    socket: SocketRef,
    State(state): State<AppState>,
    io: SocketIo,
    Data(request): Data<JsonRpcRequest>,
    ack: AckSender,
) {
    let id = request.id.clone();
    let response = match dispatch_rpc(socket, io, state, request).await {
        Ok(result) => RpcAck::Success(SuccessResponse::new(id, result)),
        Err(error) => RpcAck::Error(ErrorResponse::new(id, error)),
    };
    let _ = ack.send(&response);
}

async fn dispatch_rpc(
    socket: SocketRef,
    io: SocketIo,
    state: AppState,
    request: JsonRpcRequest,
) -> Result<Value, RpcError> {
    if request.jsonrpc != JSONRPC_VERSION {
        return Err(rpc_error(-32600, "Invalid JSON-RPC version"));
    }

    match request.method.as_str() {
        METHOD_LIST => conversation_list(&state),
        METHOD_GET => conversation_get(&state, &request.params),
        METHOD_CREATE => conversation_create(socket, io, state, request.params).await,
        METHOD_SELECT => conversation_select(&io, &state, &request.params).await,
        METHOD_UPDATE => conversation_update(socket, io, state, request.params).await,
        METHOD_DELETE => conversation_delete(&io, &state, &request.params).await,
        METHOD_FORK => conversation_fork(&io, &state, &request.params).await,
        METHOD_DRAFT_SET => conversation_draft_set(&io, &state, &request.params).await,
        METHOD_REPLAY_GET_CHUNK => conversation_replay_get_chunk(&state, &request.params),
        METHOD_SEND => conversation_send(socket, io, state, request.params).await,
        METHOD_PINS_SET => conversation_pins_set(&io, &state, &request.params).await,
        METHOD_APPROVAL_RESPOND => {
            conversation_approval_respond(socket, io, state, request.params).await
        }
        METHOD_INTERRUPT => conversation_interrupt(&state, &request.params).await,
        METHOD_COMPACT | METHOD_SHELL_EXEC => Ok(json!({
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

fn conversation_list(state: &AppState) -> Result<Value, RpcError> {
    let items = state
        .conversations
        .list()
        .map_err(internal_error)?
        .into_iter()
        .map(|item| serde_json::to_value(item).map_err(internal_error))
        .collect::<Result<Vec<_>, _>>()?;
    let pinned_conversations = items
        .iter()
        .filter_map(|item| {
            let object = item.as_object()?;
            if object.get("pinned").and_then(Value::as_bool) != Some(true) {
                return None;
            }
            object
                .get("conversation_id")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned)
        })
        .collect::<Vec<_>>();
    let selection = state.ui_selection.snapshot().map_err(internal_error)?;
    Ok(json!({
        "items": items,
        "active_conversation": selection.active_conversation_id.clone(),
        "active_conversation_id": selection.active_conversation_id,
        "active_view": selection.active_view,
        "pinned_conversations": pinned_conversations,
        "revision": state.current_list_revision(),
        "transport": "rpc"
    }))
}

fn conversation_get(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let requested_id = optional_str(params, "conversation_id");
    let selection = state.ui_selection.snapshot().map_err(internal_error)?;
    let conversation_id = match requested_id {
        Some(conversation_id) => conversation_id.to_owned(),
        None => {
            let Some(conversation_id) = selection.active_conversation_id.clone() else {
                return Ok(json!({
                    "ok": false,
                    "error": "No active conversation",
                    "transport": "rpc",
                }));
            };
            conversation_id
        }
    };
    let meta = if requested_id.is_some() {
        state
            .conversations
            .load_meta(&conversation_id)
            .map_err(internal_error)?
    } else {
        match state
            .conversations
            .load_meta_if_exists(&conversation_id)
            .map_err(internal_error)?
        {
            Some(meta) => meta,
            None => {
                state
                    .ui_selection
                    .select(None, Some("splash".to_owned()))
                    .map_err(internal_error)?;
                return Ok(json!({
                    "ok": false,
                    "error": "Active conversation does not exist",
                    "transport": "rpc",
                }));
            }
        }
    };
    let mut value = meta_result(meta)?;
    value["active_view"] = Value::String(selection.active_view);
    Ok(value)
}

async fn conversation_create(
    _socket: SocketRef,
    io: SocketIo,
    state: AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let mut settings = optional_map(&params, "settings").unwrap_or_default();
    let picked_session = take_session_binding(&mut settings);
    let extension_id = optional_owned(&params, "extension_id")
        .or_else(|| optional_owned(&params, "agent"))
        .or_else(|| string_from_map(&settings, "agent"));
    let cwd = optional_owned(&params, "cwd").or_else(|| string_from_map(&settings, "cwd"));
    let label = optional_owned(&params, "label").or_else(|| string_from_map(&settings, "label"));
    let alias = optional_owned(&params, "alias").or_else(|| string_from_map(&settings, "alias"));
    let request = CreateConversationRequest {
        conversation_id: optional_str(&params, "conversation_id").map(ToOwned::to_owned),
        title: optional_str(&params, "title").map(ToOwned::to_owned),
        agent_type: optional_str(&params, "agent_type").map(ToOwned::to_owned),
        extension_id: extension_id.clone(),
        thread_id: binding_id_from_params(&params).or_else(|| picked_session.clone()),
        cwd,
        label,
        alias,
        pinned: params
            .get("pinned")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        settings,
    };
    let meta = state
        .conversations
        .create(request)
        .map_err(internal_error)?;
    state
        .ui_selection
        .select(
            Some(meta.conversation_id.clone()),
            Some("conversation".to_owned()),
        )
        .map_err(internal_error)?;
    let mut value = meta_json(meta.clone())?;
    value["active_view"] = json!("conversation");
    if let Some(session_id) = picked_session {
        let extension_id = extension_id
            .or_else(|| meta.extension_id.clone())
            .or_else(|| meta.agent_type.clone())
            .or_else(|| string_from_map(&meta.settings, "agent"))
            .or_else(|| {
                state
                    .extensions
                    .list()
                    .into_iter()
                    .find(|entry| entry.active)
                    .map(|entry| entry.id)
            });
        match extension_id {
            Some(extension_id) => {
                let bind_result =
                    bind_provider_session(&state, &meta, &extension_id, session_id).await;
                if let Some(bound_session_id) =
                    adapter_provider_session_id(&bind_result, &meta.conversation_id)
                {
                    let updated = state
                        .conversations
                        .update_meta(
                            &meta.conversation_id,
                            ConversationMetaUpdate {
                                thread_id: Some(bound_session_id),
                                ..ConversationMetaUpdate::default()
                            },
                        )
                        .map_err(internal_error)?;
                    emit_meta_updated_to_namespace(&io, &state, &updated.conversation_id).await;
                    value = meta_json(updated)?;
                }
                value["binding_result"] = bind_result;
            }
            None => {
                let error = "No active extension available for session binding";
                warn!(conversation_id = %meta.conversation_id, error, "failed to bind picked provider session");
                value["binding_result"] = json!({"ok": false, "error": error});
            }
        }
    }
    value["active_view"] = json!("conversation");
    emit_conversation_list_updated(
        &io,
        &state,
        "created",
        Some(meta.conversation_id.as_str()),
        None,
    )
    .await;
    Ok(value)
}

async fn conversation_select(
    io: &SocketIo,
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = optional_str(&params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let requested_view = optional_str(params, "view")
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| "conversation".to_owned());
    let selection = state
        .ui_selection
        .select(Some(conversation_id.to_owned()), Some(requested_view))
        .map_err(internal_error)?;
    let mut value = meta_json(
        state
            .conversations
            .load_meta(conversation_id)
            .map_err(internal_error)?,
    )?;
    value["active_view"] = Value::String(selection.active_view);
    emit_conversation_list_updated(io, state, "selected", Some(conversation_id), None).await;
    Ok(value)
}

async fn conversation_update(
    _socket: SocketRef,
    io: SocketIo,
    state: AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = optional_str(&params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let mut settings = optional_map(&params, "settings");
    let picked_session = settings.as_mut().and_then(take_session_binding);
    let thread_id = binding_id_from_params(&params).or_else(|| picked_session.clone());
    let update = ConversationMetaUpdate {
        settings,
        thread_id,
        agent_type: optional_str(&params, "agent_type").map(ToOwned::to_owned),
        extension_id: optional_str(&params, "extension_id")
            .or_else(|| optional_str(&params, "agent"))
            .map(ToOwned::to_owned),
        title: optional_str(&params, "title").map(ToOwned::to_owned),
        draft: None,
        cwd: optional_str(&params, "cwd").map(ToOwned::to_owned),
        label: optional_str(&params, "label").map(ToOwned::to_owned),
        alias: optional_str(&params, "alias").map(ToOwned::to_owned),
        pinned: params.get("pinned").and_then(Value::as_bool),
    };
    let meta = state
        .conversations
        .update_meta(conversation_id, update)
        .map_err(internal_error)?;
    emit_meta_updated_to_namespace(&io, &state, conversation_id).await;

    let mut value = meta_json(meta.clone())?;
    if let Some(session_id) = picked_session {
        let extension_id = resolve_extension_id(&state, &params, &meta);
        match extension_id {
            Some(extension_id) => {
                let bind_result =
                    bind_provider_session(&state, &meta, &extension_id, session_id.clone()).await;
                if let Some(bound_session_id) =
                    adapter_provider_session_id(&bind_result, conversation_id)
                {
                    let updated = state
                        .conversations
                        .update_meta(
                            conversation_id,
                            ConversationMetaUpdate {
                                thread_id: Some(bound_session_id),
                                ..ConversationMetaUpdate::default()
                            },
                        )
                        .map_err(internal_error)?;
                    emit_meta_updated_to_namespace(&io, &state, conversation_id).await;
                    value = meta_json(updated)?;
                }
                value["binding_result"] = bind_result;
            }
            None => {
                let error = "No active extension available for session binding";
                warn!(
                    conversation_id,
                    error, "failed to bind picked provider session"
                );
                value["binding_result"] = json!({"ok": false, "error": error});
            }
        }
    }

    emit_conversation_list_updated(&io, &state, "updated", Some(conversation_id), None).await;
    Ok(value)
}

async fn conversation_pins_set(
    io: &SocketIo,
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let Some(requested) = params.get("pinned_conversations").and_then(Value::as_array) else {
        return Err(rpc_error(-32602, "pinned_conversations must be a list"));
    };
    let pinned = requested
        .iter()
        .filter_map(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();
    let pinned = state
        .conversations
        .set_pinned_conversations(pinned)
        .map_err(internal_error)?;
    emit_conversation_list_updated(io, state, "pins_reordered", None, None).await;
    Ok(json!({
        "ok": true,
        "pinned_conversations": pinned,
        "transport": "rpc"
    }))
}

async fn conversation_delete(
    io: &SocketIo,
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let deleted = state
        .conversations
        .delete(conversation_id)
        .map_err(internal_error)?;
    if deleted {
        let selection = state.ui_selection.snapshot().map_err(internal_error)?;
        if selection.active_conversation_id.as_deref() == Some(conversation_id) {
            state
                .ui_selection
                .select(None, Some("splash".to_owned()))
                .map_err(internal_error)?;
        }
        emit_conversation_list_updated(io, state, "deleted", None, Some(conversation_id)).await;
    }
    Ok(
        json!({"ok": true, "deleted": deleted, "conversation_id": conversation_id, "transport": "rpc"}),
    )
}

async fn conversation_fork(
    io: &SocketIo,
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let source_conversation_id = optional_str(params, "conversation_id")
        .or_else(|| optional_str(params, "source_conversation_id"))
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?
        .to_owned();
    let source_meta = state
        .conversations
        .load_meta_if_exists(&source_conversation_id)
        .map_err(internal_error)?
        .ok_or_else(|| rpc_error(-32602, "source conversation does not exist"))?;
    let extension_id = resolve_extension_id(state, params, &source_meta)
        .ok_or_else(|| rpc_error(-32603, "No active extension available"))?;
    let source_provider_session_id = source_meta
        .provider_session_id
        .clone()
        .or_else(|| source_meta.thread_id.clone())
        .ok_or_else(|| rpc_error(-32602, "conversation has no provider session to fork"))?;
    let target_conversation_id = optional_str(params, "target_conversation_id")
        .or_else(|| optional_str(params, "new_conversation_id"))
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| state.conversations.allocate_conversation_id());
    let cwd = resolve_cwd(params, &source_meta);
    let title = optional_owned(params, "title");
    let mut settings =
        optional_map(params, "settings").unwrap_or_else(|| source_meta.settings.clone());
    if let Some(cwd_text) = cwd.as_ref().and_then(|path| path.to_str()) {
        settings.insert("cwd".to_owned(), Value::String(cwd_text.to_owned()));
    }
    settings
        .entry("agent".to_owned())
        .or_insert_with(|| Value::String(extension_id.clone()));
    let adapter_metadata = optional_map(params, "metadata").unwrap_or_default();

    state
        .adapter
        .initialize_extension(&extension_id)
        .await
        .map_err(internal_error)?;
    let adapter_result = state
        .adapter
        .client()
        .await
        .map_err(internal_error)?
        .request_value(
            methods::CONVERSATION_FORK,
            ConversationForkParams {
                extension_id: extension_id.clone(),
                source_conversation_id: source_conversation_id.clone(),
                conversation_id: target_conversation_id.clone(),
                provider_session_id: source_provider_session_id.clone(),
                cwd: cwd.clone(),
                mcp_context: build_mcp_context(
                    &state.config,
                    &target_conversation_id,
                    Some(&settings),
                    cwd.as_ref()
                        .and_then(|path| path.to_str())
                        .or(source_meta.cwd.as_deref()),
                ),
                devins_context: Some(
                    crate::devins_context::build_devins_context(
                        Some(&settings),
                        cwd.as_ref()
                            .and_then(|path| path.to_str())
                            .or(source_meta.cwd.as_deref()),
                    )
                    .map_err(internal_error)?,
                ),
                settings: settings.clone(),
                metadata: adapter_metadata,
            },
        )
        .await
        .map_err(internal_error)?;
    if !adapter_bool(&adapter_result, "ok") && !adapter_bool(&adapter_result, "accepted") {
        let message = adapter_result
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("provider fork failed");
        return Err(rpc_error(-32603, message));
    }
    let provider_session_id = adapter_provider_session_id(&adapter_result, &target_conversation_id)
        .ok_or_else(|| rpc_error(-32603, "provider fork did not return a new session id"))?;

    let forked = state
        .conversations
        .fork_from(ForkConversationRequest {
            source_conversation_id: source_conversation_id.clone(),
            conversation_id: Some(target_conversation_id.clone()),
            provider_session_id,
            title,
            settings: Some(settings),
        })
        .map_err(internal_error)?;
    state
        .ui_selection
        .select(
            Some(forked.conversation_id.clone()),
            Some("conversation".to_owned()),
        )
        .map_err(internal_error)?;
    emit_meta_updated_to_namespace(io, state, &forked.conversation_id).await;
    emit_conversation_list_updated(io, state, "forked", Some(&forked.conversation_id), None).await;
    let mut value = meta_json(forked)?;
    value["source_conversation_id"] = json!(source_conversation_id);
    value["fork_result"] = adapter_result;
    value["active_view"] = json!("conversation");
    Ok(value)
}

async fn conversation_draft_set(
    io: &SocketIo,
    state: &AppState,
    params: &JsonMap,
) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let draft = optional_str(params, "draft").unwrap_or("").to_owned();
    state
        .conversations
        .update_meta(
            conversation_id,
            ConversationMetaUpdate {
                draft: Some(draft.clone()),
                ..ConversationMetaUpdate::default()
            },
        )
        .map_err(internal_error)?;
    emit_draft_updated(io, conversation_id, &draft).await;
    Ok(json!({"ok": true, "conversation_id": conversation_id, "transport": "rpc"}))
}

fn conversation_replay_get_chunk(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let offset = match optional_i64(params, "cursor", "offset").unwrap_or(0) {
        value if value < 0 => TranscriptOffset::Latest,
        value => TranscriptOffset::Absolute(value as usize),
    };
    let max_entries = optional_u64_direct(params, "max_entries")
        .unwrap_or(200)
        .max(1) as usize;
    let replay = state
        .conversations
        .read_transcript_chunk(conversation_id, offset, max_entries)
        .map_err(internal_error)?;
    let next_offset = replay.offset + replay.rows.len();
    let complete = next_offset >= replay.total_count;
    let jsonl = replay.rows.join("\n");
    Ok(json!({
        "conversation_id": conversation_id,
        "replay_id": format!("als-rs-{conversation_id}"),
        "frame": {
            "format": "jsonl",
            "offset": replay.offset,
            "item_count": replay.rows.len(),
            "total_count": replay.total_count,
            "chunk_index": replay.offset / max_entries,
            "complete": complete,
            "next_cursor": if complete { Value::Null } else { json!({"offset": next_offset}) },
            "jsonl": if jsonl.is_empty() { jsonl } else { format!("{jsonl}\n") }
        },
        "transport": "rpc"
    }))
}

async fn conversation_send(
    socket: SocketRef,
    io: SocketIo,
    state: AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let text = optional_str(&params, "text")
        .ok_or_else(|| rpc_error(-32602, "text is required"))?
        .to_owned();
    let conversation_id = match optional_str(&params, "conversation_id") {
        Some(id) => id.to_owned(),
        None => {
            let meta = state
                .conversations
                .create(CreateConversationRequest::default())
                .map_err(internal_error)?;
            state
                .ui_selection
                .select(
                    Some(meta.conversation_id.clone()),
                    Some("conversation".to_owned()),
                )
                .map_err(internal_error)?;
            meta.conversation_id
        }
    };
    if params
        .get("conversation_id")
        .and_then(Value::as_str)
        .is_none()
    {
        emit_conversation_list_updated(&io, &state, "created", Some(&conversation_id), None).await;
    }
    let meta = state
        .conversations
        .load_meta(&conversation_id)
        .map_err(internal_error)?;
    let extension_id = resolve_extension_id(&state, &params, &meta)
        .ok_or_else(|| rpc_error(-32603, "No active extension available"))?;
    let cwd = resolve_cwd(&params, &meta);

    let user_event = json!({
        "type": "message",
        "conversation_id": conversation_id,
        "role": "user",
        "text": text
    });
    let transcript = state
        .conversations
        .append_transcript(&conversation_id, user_event.clone())
        .map_err(internal_error)?;
    emit_rpc_notification(&socket, "conversation.user.message", transcript.clone());
    emit_meta_and_list_updated_to_namespace(&io, &state, &conversation_id, "meta_changed").await;

    let thread_id = meta.thread_id.clone();
    let provider_session_id = meta
        .provider_session_id
        .clone()
        .or_else(|| thread_id.clone());
    let adapter_params = ConversationSendParams {
        extension_id: extension_id.clone(),
        conversation_id: conversation_id.clone(),
        text,
        thread_id,
        provider_session_id,
        turn_id: optional_str(&params, "turn_id").map(ToOwned::to_owned),
        cwd,
        attachments: Vec::new(),
        toast_context: None,
        mcp_context: build_mcp_context(
            &state.config,
            &conversation_id,
            Some(&meta.settings),
            meta.cwd.as_deref(),
        ),
        devins_context: Some(
            crate::devins_context::build_devins_context(Some(&meta.settings), meta.cwd.as_deref())
                .map_err(internal_error)?,
        ),
        settings: meta.settings,
    };
    state
        .adapter
        .initialize_extension(&extension_id)
        .await
        .map_err(internal_error)?;
    let adapter_result = state
        .adapter
        .client()
        .await
        .map_err(internal_error)?
        .request_value(methods::CONVERSATION_SEND, adapter_params)
        .await
        .map_err(internal_error)?;
    if let Some(provider_session_id) =
        adapter_provider_session_id(&adapter_result, &conversation_id)
    {
        let _ = state.conversations.update_meta(
            &conversation_id,
            ConversationMetaUpdate {
                thread_id: Some(provider_session_id),
                ..ConversationMetaUpdate::default()
            },
        );
        emit_meta_and_list_updated_to_namespace(&io, &state, &conversation_id, "session_bound")
            .await;
    }
    let mut result = adapter_result.as_object().cloned().unwrap_or_default();
    result.insert("conversation_id".to_owned(), Value::String(conversation_id));
    result.insert("transport".to_owned(), Value::String("rpc".to_owned()));
    Ok(Value::Object(result))
}

async fn conversation_interrupt(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?
        .to_owned();
    let meta = state
        .conversations
        .load_meta(&conversation_id)
        .map_err(internal_error)?;
    let extension_id = resolve_extension_id(state, params, &meta)
        .ok_or_else(|| rpc_error(-32603, "No active extension available"))?;
    let adapter_params = ConversationControlParams {
        extension_id: extension_id.clone(),
        conversation_id: conversation_id.clone(),
    };
    state
        .adapter
        .initialize_extension(&extension_id)
        .await
        .map_err(internal_error)?;
    let adapter_result = state
        .adapter
        .client()
        .await
        .map_err(internal_error)?
        .request_value(methods::CONVERSATION_INTERRUPT, adapter_params)
        .await
        .map_err(internal_error)?;
    let mut result = adapter_result.as_object().cloned().unwrap_or_default();
    result.insert("extension_id".to_owned(), Value::String(extension_id));
    result.insert("conversation_id".to_owned(), Value::String(conversation_id));
    result.insert("transport".to_owned(), Value::String("rpc".to_owned()));
    Ok(Value::Object(result))
}

async fn handle_adapter_event(
    io: &SocketIo,
    state: &AppState,
    event: AdapterCapturedEvent,
) -> Result<(), RpcError> {
    match event {
        AdapterCapturedEvent::Live(value) => forward_adapter_live_event(io, state, value).await,
        AdapterCapturedEvent::Transcript(value) => {
            persist_adapter_transcript(io, state, value).await
        }
        AdapterCapturedEvent::Other(other) => handle_adapter_other_event(io, state, other).await,
    }
}

async fn handle_adapter_other_event(
    io: &SocketIo,
    state: &AppState,
    other: crate::adapter_process::AdapterOtherEvent,
) -> Result<(), RpcError> {
    match other.method.as_str() {
        events::IMPORT_STARTED => {
            forward_import_event(io, "conversation.import.started", other.params).await
        }
        events::IMPORT_PROGRESS => {
            forward_import_event(io, "conversation.import.progress", other.params).await
        }
        events::IMPORT_TRANSCRIPT_BATCH => {
            persist_import_transcript_batch(state, other.params)?;
            Ok(())
        }
        events::IMPORT_COMPLETED => {
            if let Some(conversation_id) = conversation_id_from_value(&other.params) {
                emit_meta_and_list_updated_to_namespace(
                    io,
                    state,
                    &conversation_id,
                    "meta_changed",
                )
                .await;
            }
            forward_import_event(io, "conversation.import.completed", other.params).await
        }
        events::IMPORT_FAILED => {
            if let Some(conversation_id) = conversation_id_from_value(&other.params) {
                emit_meta_and_list_updated_to_namespace(
                    io,
                    state,
                    &conversation_id,
                    "meta_changed",
                )
                .await;
            }
            forward_import_event(io, "conversation.import.failed", other.params).await
        }
        _ => Ok(()),
    }
}

async fn forward_import_event(io: &SocketIo, method: &str, params: Value) -> Result<(), RpcError> {
    emit_rpc_notification_to_namespace(io, method, params).await;
    Ok(())
}

async fn forward_adapter_live_event(
    io: &SocketIo,
    state: &AppState,
    value: Value,
) -> Result<(), RpcError> {
    let Some((conversation_id, mut event)) = adapter_conversation_object(value) else {
        return Ok(());
    };
    let event_type = event
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| rpc_error(-32603, "adapter live event is missing type"))?
        .to_owned();
    sanitize_live_card_event(&mut event);
    if should_skip_adapter_event_type(&event_type) {
        return Ok(());
    }
    let Some(method) = notification_method_for_event_type(&event_type) else {
        return Ok(());
    };
    if event_type.trim().eq_ignore_ascii_case("diff") {
        if let Some(entry) = state
            .agent_edits
            .record_live_diff(&state.conversations, &conversation_id, &event)
            .map_err(internal_error)?
        {
            if let Some(inline_publish) = entry.inline_publish_payload() {
                match state.inline_agent_edits.publish(&inline_publish) {
                    Ok(_) => {
                        if let Some(document_params) = entry.inline_document_state_params() {
                            match state.inline_agent_edits.document_state(&document_params) {
                                Ok(Value::Object(projection)) => {
                                    let _ = sidebar_ipc::publish_agent_edits(io, state, projection)
                                        .await;
                                }
                                Ok(value) => {
                                    warn!(
                                        ?value,
                                        "inline agent edit document state was not an object"
                                    );
                                }
                                Err(error) => {
                                    warn!(
                                        %error,
                                        "failed to build inline agent edit document state"
                                    );
                                }
                            }
                        }
                    }
                    Err(error) => {
                        warn!(%error, "failed to publish live diff into inline agent edit ledger");
                    }
                }
            }
            let _ = sidebar_ipc::emit_agent_edit(io, state, entry.sidebar_payload()).await;
            crate::ui_rpc::emit_project_agent_diff_added(io, &entry).await;
        }
    }
    if event_type.trim().eq_ignore_ascii_case("approval") {
        persist_pending_approval_event(state, &conversation_id, &event)?;
        emit_rpc_notification_to_namespace(io, method, event).await;
        emit_meta_and_list_updated_to_namespace(io, state, &conversation_id, "meta_changed").await;
        return Ok(());
    }
    emit_rpc_notification_to_namespace(io, method, event).await;
    Ok(())
}

async fn conversation_approval_respond(
    socket: SocketRef,
    io: SocketIo,
    state: AppState,
    params: JsonMap,
) -> Result<Value, RpcError> {
    let request_id = optional_str(&params, "request_id")
        .or_else(|| optional_str(&params, "requestId"))
        .or_else(|| optional_str(&params, "id"))
        .ok_or_else(|| rpc_error(-32602, "request_id is required"))?
        .to_owned();
    let requested_conversation_id = optional_str(&params, "conversation_id")
        .or_else(|| optional_str(&params, "conversationId"))
        .map(ToOwned::to_owned);

    let (conversation_id, descriptor) = match requested_conversation_id {
        Some(conversation_id) => {
            let meta = state
                .conversations
                .load_meta(&conversation_id)
                .map_err(internal_error)?;
            let Some(descriptor) = meta
                .pending_approvals
                .get(&request_id)
                .and_then(Value::as_object)
                .cloned()
            else {
                return Err(rpc_error(-32009, "Approval is no longer pending"));
            };
            (meta.conversation_id, descriptor)
        }
        None => state
            .conversations
            .find_pending_approval(&request_id)
            .map_err(internal_error)?
            .ok_or_else(|| rpc_error(-32009, "Approval is no longer pending"))?,
    };

    let meta = state
        .conversations
        .load_meta(&conversation_id)
        .map_err(internal_error)?;
    let extension_id = string_from_map(&descriptor, "agent")
        .or_else(|| string_from_map(&descriptor, "extension_id"))
        .or_else(|| resolve_extension_id(&state, &JsonMap::new(), &meta))
        .ok_or_else(|| rpc_error(-32603, "No approval resolver for conversation"))?;
    let mut resolution = optional_map(&params, "result").unwrap_or_default();
    if let Some(decision) = optional_str(&params, "decision") {
        resolution
            .entry("decision".to_owned())
            .or_insert_with(|| Value::String(decision.to_owned()));
    }

    if is_agent_pty_ask_user_descriptor(&descriptor) {
        submit_ask_user_response(&io, &state, &conversation_id, &request_id, &resolution).await?;
        emit_meta_and_list_updated_to_namespace(&io, &state, &conversation_id, "meta_changed")
            .await;
        return Ok(json!({
            "ok": true,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "result": resolution,
            "awaiting_harness_ack": true,
            "transport": "ipc"
        }));
    }

    state
        .adapter
        .initialize_extension(&extension_id)
        .await
        .map_err(internal_error)?;
    let adapter_result = state
        .adapter
        .client()
        .await
        .map_err(internal_error)?
        .request_value(
            methods::APPROVAL_RESPOND,
            json!({
                "extension_id": extension_id,
                "conversation_id": conversation_id.clone(),
                "request_id": request_id.clone(),
                "decision": resolution.get("decision").cloned().unwrap_or(Value::Null),
                "result": Value::Object(resolution.clone()),
            }),
        )
        .await
        .map_err(internal_error)?;
    if !adapter_bool(&adapter_result, "ok") && !adapter_bool(&adapter_result, "resolved") {
        let _ = state
            .conversations
            .remove_pending_approval(&conversation_id, &request_id);
        emit_meta_and_list_updated_to_namespace(&io, &state, &conversation_id, "meta_changed")
            .await;
        return Err(rpc_error(
            -32009,
            "Approval is stale or no longer actionable",
        ));
    }

    let mut handoff_event =
        build_approval_handoff_event(&state, &conversation_id, &descriptor, &resolution)?;
    let recorded_entry =
        append_approval_handoff_transcript_entry(&state, &conversation_id, &handoff_event)?;
    merge_recorded_approval_fields(&mut handoff_event, &recorded_entry);
    let _ = state
        .conversations
        .remove_pending_approval(&conversation_id, &request_id);
    emit_meta_and_list_updated_to_namespace(&io, &state, &conversation_id, "meta_changed").await;
    emit_rpc_notification(
        &socket,
        "conversation.approval.handoff",
        Value::Object(handoff_event.clone()),
    );
    maybe_emit_diff_declined(&socket, &conversation_id, &handoff_event);

    Ok(json!({
        "ok": true,
        "conversation_id": conversation_id,
        "request_id": request_id,
        "decision": resolution.get("decision").cloned().unwrap_or(Value::Null),
        "result": resolution,
        "handoff_event": handoff_event,
        "transport": "rpc"
    }))
}

async fn submit_ask_user_response(
    io: &SocketIo,
    state: &AppState,
    conversation_id: &str,
    request_id: &str,
    resolution: &JsonMap,
) -> Result<(), RpcError> {
    let submitted = normalize_approval_resolution(resolution);
    record_pending_approval_submission(state, conversation_id, request_id, &submitted)?;
    ipc::emit_ask_user_response(io, request_id, &submitted).await;
    Ok(())
}

pub async fn acknowledge_ask_user_interaction(
    io: &SocketIo,
    state: &AppState,
    request_id: &str,
) -> Result<bool, anyhow::Error> {
    let Some((conversation_id, mut handoff_event, recorded_entry)) =
        complete_submitted_ask_user_interaction(state, request_id)
            .map_err(|error| anyhow::anyhow!("{} ({})", error.message, error.code))?
    else {
        return Ok(false);
    };
    merge_recorded_approval_fields(&mut handoff_event, &recorded_entry);
    emit_meta_and_list_updated_to_namespace(io, state, &conversation_id, "meta_changed").await;
    emit_rpc_notification_to_namespace(
        io,
        "conversation.approval.handoff",
        Value::Object(handoff_event),
    )
    .await;
    Ok(true)
}

fn complete_submitted_ask_user_interaction(
    state: &AppState,
    request_id: &str,
) -> Result<Option<(String, JsonMap, Value)>, RpcError> {
    let Some((conversation_id, descriptor)) = state
        .conversations
        .find_pending_approval(request_id)
        .map_err(internal_error)?
    else {
        return Ok(None);
    };
    if !is_agent_pty_ask_user_descriptor(&descriptor) {
        return Ok(None);
    }
    let Some(submitted) = descriptor
        .get("submitted_resolution")
        .and_then(Value::as_object)
        .cloned()
    else {
        return Ok(None);
    };
    let handoff_event =
        build_approval_handoff_event(state, &conversation_id, &descriptor, &submitted)?;
    let recorded_entry =
        append_approval_handoff_transcript_entry(state, &conversation_id, &handoff_event)?;
    state
        .conversations
        .remove_pending_approval(&conversation_id, request_id)
        .map_err(internal_error)?;
    Ok(Some((conversation_id, handoff_event, recorded_entry)))
}

fn record_pending_approval_submission(
    state: &AppState,
    conversation_id: &str,
    request_id: &str,
    resolution: &JsonMap,
) -> Result<JsonMap, RpcError> {
    let meta = state
        .conversations
        .load_meta(conversation_id)
        .map_err(internal_error)?;
    let Some(mut descriptor) = meta
        .pending_approvals
        .get(request_id)
        .and_then(Value::as_object)
        .cloned()
    else {
        return Err(rpc_error(-32009, "Approval is no longer pending"));
    };
    descriptor.insert(
        "submitted_resolution".to_owned(),
        Value::Object(resolution.clone()),
    );
    descriptor.insert("submitted_at".to_owned(), Value::String(utc_ts_for_rpc()));
    state
        .conversations
        .upsert_pending_approval(conversation_id, request_id, descriptor.clone())
        .map_err(internal_error)?;
    Ok(descriptor)
}

fn normalize_approval_resolution(resolution: &JsonMap) -> JsonMap {
    resolution
        .get("result")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_else(|| resolution.clone())
}

fn is_agent_pty_ask_user_descriptor(descriptor: &JsonMap) -> bool {
    string_from_map(descriptor, "request_method")
        .map(|method| method.eq_ignore_ascii_case(AGENT_PTY_ASK_USER_REQUEST_METHOD))
        .unwrap_or(false)
}

async fn persist_adapter_transcript(
    io: &SocketIo,
    state: &AppState,
    value: Value,
) -> Result<(), RpcError> {
    if let Some(conversation_id) = persist_adapter_transcript_entry(state, value)? {
        emit_meta_and_list_updated_to_namespace(io, state, &conversation_id, "meta_changed").await;
    }
    Ok(())
}

fn persist_adapter_transcript_entry(
    state: &AppState,
    value: Value,
) -> Result<Option<String>, RpcError> {
    let Some((conversation_id, mut entry)) = adapter_conversation_object(value) else {
        return Ok(None);
    };
    if should_skip_adapter_transcript_entry(&entry) {
        return Ok(None);
    }
    strip_internal_adapter_transcript_fields(&mut entry);
    sanitize_transcript_card_entry(&mut entry);
    state
        .conversations
        .append_transcript(&conversation_id, entry)
        .map(|_| ())
        .map_err(internal_error)?;
    Ok(Some(conversation_id))
}

fn persist_import_transcript_batch(state: &AppState, value: Value) -> Result<(), RpcError> {
    let conversation_id = conversation_id_from_value(&value)
        .ok_or_else(|| rpc_error(-32603, "import transcript batch is missing conversation_id"))?;
    let records = value
        .get("records")
        .and_then(Value::as_array)
        .ok_or_else(|| rpc_error(-32603, "import transcript batch is missing records"))?;
    let mut entries = Vec::with_capacity(records.len());
    for record in records {
        let mut entry = record.clone();
        if !entry.is_object() {
            continue;
        }
        if let Some(object) = entry.as_object_mut() {
            object
                .entry("conversation_id".to_owned())
                .or_insert_with(|| Value::String(conversation_id.clone()));
        }
        if should_skip_adapter_transcript_entry(&entry) {
            continue;
        }
        strip_internal_adapter_transcript_fields(&mut entry);
        sanitize_transcript_card_entry(&mut entry);
        entries.push(entry);
    }
    state
        .conversations
        .append_transcript_batch(&conversation_id, entries)
        .map(|_| ())
        .map_err(internal_error)?;
    Ok(())
}

fn conversation_id_from_value(value: &Value) -> Option<String> {
    value
        .as_object()?
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

async fn bind_provider_session(
    state: &AppState,
    meta: &ConversationMeta,
    extension_id: &str,
    session_id: String,
) -> Value {
    let conversation_id = meta.conversation_id.clone();
    let attempt: Result<Value, RpcError> = async {
        state
            .adapter
            .initialize_extension(extension_id)
            .await
            .map_err(internal_error)?;
        state
            .adapter
            .client()
            .await
            .map_err(internal_error)?
            .request_value(
                methods::CONVERSATION_RESUME,
                ConversationResumeParams {
                    extension_id: extension_id.to_owned(),
                    conversation_id: conversation_id.clone(),
                    provider_session_id: session_id,
                    cwd: meta.cwd.clone().map(PathBuf::from),
                    mcp_context: build_mcp_context(
                        &state.config,
                        &conversation_id,
                        Some(&meta.settings),
                        meta.cwd.as_deref(),
                    ),
                    devins_context: Some(
                        crate::devins_context::build_devins_context(
                            Some(&meta.settings),
                            meta.cwd.as_deref(),
                        )
                        .map_err(internal_error)?,
                    ),
                    settings: meta.settings.clone(),
                },
            )
            .await
            .map_err(internal_error)
    }
    .await;

    match attempt {
        Ok(value) => value,
        Err(error) => {
            warn!(
                conversation_id,
                extension_id,
                error = %error.message,
                "provider session binding failed"
            );
            json!({"ok": false, "error": error.message})
        }
    }
}

async fn emit_rpc_notification_to_namespace(io: &SocketIo, method: &str, params: Value) {
    let notification = json!({
        "jsonrpc": "2.0",
        "method": method,
        "params": params
    });
    let Some(namespace) = io.of("/rpc/conversations") else {
        warn!("conversation RPC namespace is unavailable for adapter event fanout");
        return;
    };
    if let Err(error) = namespace.emit(RPC_NOTIFY_EVENT, &notification).await {
        warn!(error = %error, "failed to emit adapter event over conversations RPC");
    }
}

pub async fn emit_draft_updated(io: &SocketIo, conversation_id: &str, draft: &str) {
    emit_rpc_notification_to_namespace(
        io,
        "conversation.draft.updated",
        draft_updated_event(conversation_id, draft),
    )
    .await;
}

async fn emit_conversation_list_updated(
    io: &SocketIo,
    state: &AppState,
    reason: &str,
    changed_conversation_id: Option<&str>,
    deleted_conversation_id: Option<&str>,
) {
    state.bump_list_revision();
    let mut value = match conversation_list(state) {
        Ok(value) => value,
        Err(error) => {
            warn!(
                error = %error.message,
                reason,
                "failed to build conversation list update"
            );
            return;
        }
    };
    value["reason"] = Value::String(reason.to_owned());
    if let Some(conversation_id) = changed_conversation_id.filter(|value| !value.is_empty()) {
        value["conversation_id"] = Value::String(conversation_id.to_owned());
        value["changed_conversation_id"] = Value::String(conversation_id.to_owned());
    }
    if let Some(conversation_id) = deleted_conversation_id.filter(|value| !value.is_empty()) {
        value["deleted_conversation_id"] = Value::String(conversation_id.to_owned());
    }
    emit_rpc_notification_to_namespace(io, METHOD_LIST_UPDATED, value).await;
}

fn draft_updated_event(conversation_id: &str, draft: &str) -> Value {
    json!({
        "conversation_id": conversation_id,
        "draft": draft,
    })
}

fn adapter_conversation_object(value: Value) -> Option<(String, Value)> {
    let conversation_id = value
        .as_object()?
        .get("conversation_id")
        .and_then(Value::as_str)?
        .trim()
        .to_owned();
    if conversation_id.is_empty() {
        return None;
    }
    Some((conversation_id, value))
}

fn should_skip_adapter_event_type(event_type: &str) -> bool {
    event_type.trim().eq_ignore_ascii_case("message")
}

fn should_skip_adapter_transcript_entry(entry: &Value) -> bool {
    entry
        .get("role")
        .and_then(Value::as_str)
        .is_some_and(|role| {
            role.trim().eq_ignore_ascii_case("user") && !adapter_history_import_entry(entry)
        })
}

fn adapter_history_import_entry(entry: &Value) -> bool {
    entry
        .get("_hydrated_history")
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn strip_internal_adapter_transcript_fields(entry: &mut Value) {
    let Some(object) = entry.as_object_mut() else {
        return;
    };
    object.remove("_hydrated_history");
}

fn notification_method_for_event_type(event_type: &str) -> Option<&'static str> {
    match event_type.trim().to_ascii_lowercase().as_str() {
        "activity" => Some("conversation.activity"),
        "approval" => Some("conversation.approval.request"),
        "approval_handoff" => Some("conversation.approval.handoff"),
        "assistant_delta" => Some("conversation.message.delta"),
        "assistant_end" | "assistant_finalize" => Some("conversation.message.final"),
        "command_result" => Some("conversation.command.result"),
        "context_compacted" => Some("conversation.context.compacted"),
        "diff" => Some("conversation.diff"),
        "diff_declined" => Some("conversation.diff.declined"),
        "draft_update" => Some("conversation.draft.updated"),
        "error" => Some("conversation.error"),
        "list_updated" => Some(METHOD_LIST_UPDATED),
        "mention_insert" => Some("conversation.mention.inserted"),
        "message" => Some("conversation.user.message"),
        "meta_updated" => Some("conversation.meta.updated"),
        "mode" => Some("conversation.mode.changed"),
        "plan" => Some("conversation.plan"),
        "plan_state" => Some("conversation.plan.state"),
        "plan_update" => Some("conversation.plan.update"),
        "preview_updated" => Some("conversation.preview.updated"),
        "reasoning_delta" => Some("conversation.reasoning.delta"),
        "reasoning_end" | "reasoning_finalize" => Some("conversation.reasoning.final"),
        "shell_begin" => Some("conversation.command.begin"),
        "shell_delta" => Some("conversation.command.delta"),
        "shell_end" => Some("conversation.command.end"),
        "status" => Some("conversation.status"),
        "subagent_end" => Some("conversation.subagent.end"),
        "subagent_start" => Some("conversation.subagent.start"),
        "thought" => Some("conversation.thought"),
        "toast" => Some("conversation.toast"),
        "token_count" => Some("conversation.token.updated"),
        "tool_interaction" => Some("conversation.tool.interaction"),
        "tool_begin" => Some("conversation.tool.begin"),
        "tool_delta" => Some("conversation.tool.delta"),
        "tool_end" => Some("conversation.tool.end"),
        "search" => Some("conversation.search"),
        "view" => Some("conversation.view"),
        "warning" => Some("conversation.warning"),
        _ => None,
    }
}

fn persist_pending_approval_event(
    state: &AppState,
    conversation_id: &str,
    event: &Value,
) -> Result<(), RpcError> {
    let event_object = event
        .as_object()
        .ok_or_else(|| rpc_error(-32603, "approval event must be an object"))?;
    let request_id = first_nonempty_event_string(event_object, &["request_id", "id"])
        .ok_or_else(|| rpc_error(-32603, "approval event is missing request_id"))?;
    let meta = state
        .conversations
        .load_meta(conversation_id)
        .map_err(internal_error)?;
    let request_method = first_nonempty_event_string(event_object, &["request_method"]);
    let turn_id = first_nonempty_event_string(event_object, &["turn_id"]);
    let mut render_event = event_object.clone();
    if request_method.as_deref() == Some(AGENT_PTY_ASK_USER_REQUEST_METHOD)
        && !render_event.contains_key("order_id")
        && !render_event.contains_key("orderId")
    {
        render_event.insert("order_id".to_owned(), Value::Number((-1).into()));
    }
    let payload = event_object
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let request_params = event_object
        .get("request_params")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut transcript_anchor = Map::new();
    if let Some(turn_id) = turn_id.clone() {
        transcript_anchor.insert("turn_id".to_owned(), Value::String(turn_id));
    }
    let agent = first_nonempty_event_string(event_object, &["agent", "extension_id"])
        .or_else(|| meta.extension_id.clone())
        .or_else(|| meta.agent_type.clone())
        .or_else(|| string_from_map(&meta.settings, "agent"))
        .or_else(|| {
            state
                .extensions
                .list()
                .into_iter()
                .find(|entry| entry.active)
                .map(|entry| entry.id)
        })
        .unwrap_or_else(|| "unknown".to_owned());
    let thread_id = first_nonempty_event_string(
        event_object,
        &["thread_id", "provider_session_id", "session_id"],
    )
    .or_else(|| meta.thread_id.clone())
    .or_else(|| meta.provider_session_id.clone());
    let created_at =
        first_nonempty_event_string(event_object, &["created_at"]).unwrap_or_else(utc_ts_for_rpc);
    let mut descriptor = Map::new();
    descriptor.insert("request_id".to_owned(), Value::String(request_id.clone()));
    descriptor.insert("agent".to_owned(), Value::String(agent));
    descriptor.insert(
        "kind".to_owned(),
        Value::String(
            first_nonempty_event_string(event_object, &["kind"])
                .unwrap_or_else(|| "unknown".to_owned()),
        ),
    );
    if let Some(request_method) = request_method {
        descriptor.insert("request_method".to_owned(), Value::String(request_method));
    }
    descriptor.insert("request_params".to_owned(), Value::Object(request_params));
    descriptor.insert("payload".to_owned(), Value::Object(payload));
    descriptor.insert(
        "conversation_id".to_owned(),
        Value::String(conversation_id.to_owned()),
    );
    if let Some(thread_id) = thread_id {
        descriptor.insert("thread_id".to_owned(), Value::String(thread_id));
    }
    if let Some(turn_id) = first_nonempty_event_string(event_object, &["turn_id"]) {
        descriptor.insert("turn_id".to_owned(), Value::String(turn_id));
    }
    descriptor.insert(
        "transcript_anchor".to_owned(),
        Value::Object(transcript_anchor),
    );
    descriptor.insert("source".to_owned(), Value::String("live".to_owned()));
    descriptor.insert("created_at".to_owned(), Value::String(created_at));
    descriptor.insert("status".to_owned(), Value::String("pending".to_owned()));
    descriptor.insert("render_event".to_owned(), Value::Object(render_event));
    state
        .conversations
        .upsert_pending_approval(conversation_id, &request_id, descriptor)
        .map_err(internal_error)?;
    Ok(())
}

fn build_approval_handoff_event(
    state: &AppState,
    conversation_id: &str,
    descriptor: &JsonMap,
    resolution: &JsonMap,
) -> Result<JsonMap, RpcError> {
    let request_id = string_from_map(descriptor, "request_id")
        .or_else(|| string_from_map(descriptor, "id"))
        .ok_or_else(|| rpc_error(-32603, "pending approval descriptor is missing request_id"))?;
    let render_event = descriptor
        .get("render_event")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let payload = render_event
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .or_else(|| {
            descriptor
                .get("payload")
                .and_then(Value::as_object)
                .cloned()
        })
        .unwrap_or_default();
    let request_params = render_event
        .get("request_params")
        .and_then(Value::as_object)
        .cloned()
        .or_else(|| {
            descriptor
                .get("request_params")
                .and_then(Value::as_object)
                .cloned()
        })
        .unwrap_or_default();
    let request_method = string_from_map(&render_event, "request_method")
        .or_else(|| string_from_map(descriptor, "request_method"));
    let turn_id = string_from_map(&render_event, "turn_id")
        .or_else(|| string_from_map(descriptor, "turn_id"));
    let mut event = render_event;
    event.insert(
        "type".to_owned(),
        Value::String("approval_handoff".to_owned()),
    );
    event.insert(
        "conversation_id".to_owned(),
        Value::String(conversation_id.to_owned()),
    );
    event.insert(
        "id".to_owned(),
        Value::String(string_from_map(&event, "id").unwrap_or_else(|| request_id.clone())),
    );
    event.insert("request_id".to_owned(), Value::String(request_id));
    event.insert(
        "kind".to_owned(),
        Value::String(
            string_from_map(&event, "kind")
                .or_else(|| string_from_map(descriptor, "kind"))
                .unwrap_or_else(|| "unknown".to_owned()),
        ),
    );
    if let Some(request_method) = request_method.clone() {
        event.insert("request_method".to_owned(), Value::String(request_method));
    }
    event.insert("request_params".to_owned(), Value::Object(request_params));
    event.insert("payload".to_owned(), Value::Object(payload));
    if let Some(turn_id) = turn_id {
        event.insert("turn_id".to_owned(), Value::String(turn_id));
    }
    event.insert(
        "created_at".to_owned(),
        Value::String(
            string_from_map(&event, "created_at")
                .or_else(|| string_from_map(descriptor, "created_at"))
                .unwrap_or_else(utc_ts_for_rpc),
        ),
    );
    if let Some(card_id) =
        string_from_map(&event, "card_id").or_else(|| string_from_map(descriptor, "card_id"))
    {
        event.insert("card_id".to_owned(), Value::String(card_id));
    }
    if request_method.as_deref() == Some(AGENT_PTY_ASK_USER_REQUEST_METHOD) {
        let msg_id = state
            .conversations
            .next_ask_user_msg_id(conversation_id)
            .map_err(internal_error)?;
        event.insert("ask_user_msg_id".to_owned(), Value::Number(msg_id.into()));
    }
    event.insert(
        "status".to_owned(),
        Value::String(approval_status_from_resolution(resolution).to_owned()),
    );
    event.insert(
        "decision".to_owned(),
        resolution.get("decision").cloned().unwrap_or(Value::Null),
    );
    event.insert("result".to_owned(), Value::Object(resolution.clone()));
    event.insert("resolved_at".to_owned(), Value::String(utc_ts_for_rpc()));
    Ok(event)
}

fn append_approval_handoff_transcript_entry(
    state: &AppState,
    conversation_id: &str,
    handoff_event: &JsonMap,
) -> Result<Value, RpcError> {
    let payload = handoff_event
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let request_id = handoff_event
        .get("request_id")
        .or_else(|| handoff_event.get("id"))
        .cloned()
        .unwrap_or(Value::Null);
    let card_id = handoff_event
        .get("card_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    let item_id = card_id
        .as_ref()
        .map(|value| Value::String(value.clone()))
        .unwrap_or_else(|| request_id.clone());
    let mut entry = Map::new();
    entry.insert("role".to_owned(), Value::String("approval".to_owned()));
    entry.insert(
        "status".to_owned(),
        handoff_event.get("status").cloned().unwrap_or(Value::Null),
    );
    entry.insert(
        "decision".to_owned(),
        handoff_event
            .get("decision")
            .cloned()
            .unwrap_or(Value::Null),
    );
    entry.insert(
        "result".to_owned(),
        handoff_event.get("result").cloned().unwrap_or(Value::Null),
    );
    entry.insert(
        "request_method".to_owned(),
        handoff_event
            .get("request_method")
            .cloned()
            .unwrap_or(Value::Null),
    );
    entry.insert("payload".to_owned(), Value::Object(payload.clone()));
    entry.insert(
        "diff".to_owned(),
        handoff_event
            .get("diff")
            .cloned()
            .or_else(|| payload.get("diff").cloned())
            .unwrap_or(Value::Null),
    );
    entry.insert(
        "path".to_owned(),
        handoff_event
            .get("path")
            .cloned()
            .or_else(|| payload.get("path").cloned())
            .unwrap_or(Value::Null),
    );
    entry.insert("request_id".to_owned(), request_id);
    entry.insert("item_id".to_owned(), item_id);
    if let Some(card_id) = card_id {
        entry.insert("card_id".to_owned(), Value::String(card_id));
    }
    if let Some(value) = handoff_event.get("ask_user_msg_id") {
        entry.insert("ask_user_msg_id".to_owned(), value.clone());
    }
    if let Some(value) = handoff_event.get("turn_id") {
        entry.insert("turn_id".to_owned(), value.clone());
    }
    entry.insert(
        "event".to_owned(),
        Value::String("approval_decision".to_owned()),
    );
    state
        .conversations
        .append_transcript(conversation_id, Value::Object(entry))
        .map_err(internal_error)
}

fn merge_recorded_approval_fields(handoff_event: &mut JsonMap, recorded_entry: &Value) {
    let Some(recorded) = recorded_entry.as_object() else {
        return;
    };
    for key in ["nid", "card_id", "order_id", "ask_user_msg_id"] {
        if let Some(value) = recorded.get(key) {
            handoff_event.insert(key.to_owned(), value.clone());
        }
    }
}

fn maybe_emit_diff_declined(socket: &SocketRef, conversation_id: &str, handoff_event: &JsonMap) {
    if handoff_event.get("status").and_then(Value::as_str) != Some("declined") {
        return;
    }
    let payload = handoff_event
        .get("payload")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let diff = handoff_event
        .get("diff")
        .cloned()
        .or_else(|| payload.get("diff").cloned());
    let Some(diff) = diff.filter(|value| !value.is_null()) else {
        return;
    };
    let path = handoff_event
        .get("path")
        .cloned()
        .or_else(|| payload.get("path").cloned())
        .unwrap_or(Value::Null);
    emit_rpc_notification(
        socket,
        "conversation.diff.declined",
        json!({
            "type": "diff_declined",
            "id": handoff_event.get("request_id").cloned().unwrap_or(Value::Null),
            "text": diff,
            "path": path,
            "conversation_id": conversation_id,
        }),
    );
}

fn approval_status_from_resolution(resolution: &JsonMap) -> &'static str {
    let decision = string_from_map(resolution, "decision")
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(
        decision.as_str(),
        "decline" | "deny" | "denied" | "reject" | "rejected"
    ) {
        return "declined";
    }
    if matches!(decision.as_str(), "cancel" | "cancelled" | "canceled") {
        return "cancelled";
    }
    let action = string_from_map(resolution, "action")
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(action.as_str(), "decline" | "deny" | "reject") {
        return "declined";
    }
    if matches!(action.as_str(), "cancel" | "cancelled" | "canceled") {
        return "cancelled";
    }
    if resolution.get("success").and_then(Value::as_bool) == Some(false) {
        return "declined";
    }
    "accepted"
}

fn adapter_bool(value: &Value, key: &str) -> bool {
    value
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_bool)
        .unwrap_or(false)
}

fn first_nonempty_event_string(object: &JsonMap, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| string_from_map(object, key))
}

fn utc_ts_for_rpc() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    format!("unix_ms:{millis}")
}

fn meta_result(meta: ConversationMeta) -> Result<Value, RpcError> {
    meta_json(meta)
}

fn meta_json(meta: ConversationMeta) -> Result<Value, RpcError> {
    let mut value = serde_json::to_value(meta).map_err(internal_error)?;
    value["active_view"] = json!("conversation");
    value["transport"] = json!("rpc");
    Ok(value)
}

fn emit_rpc_notification(socket: &SocketRef, method: &str, params: Value) {
    let notification = json!({
        "jsonrpc": "2.0",
        "method": method,
        "params": params
    });
    let _ = socket.emit(RPC_NOTIFY_EVENT, &notification);
}

async fn emit_meta_updated_to_namespace(io: &SocketIo, state: &AppState, conversation_id: &str) {
    let Ok(meta) = state.conversations.load_meta(conversation_id) else {
        return;
    };
    let Ok(value) = serde_json::to_value(meta) else {
        return;
    };
    emit_rpc_notification_to_namespace(io, "conversation.meta.updated", value).await;
}

async fn emit_meta_and_list_updated_to_namespace(
    io: &SocketIo,
    state: &AppState,
    conversation_id: &str,
    reason: &str,
) {
    emit_meta_updated_to_namespace(io, state, conversation_id).await;
    emit_conversation_list_updated(io, state, reason, Some(conversation_id), None).await;
}

fn optional_str<'a>(params: &'a JsonMap, key: &str) -> Option<&'a str> {
    params
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}

fn binding_id_from_params(params: &JsonMap) -> Option<String> {
    [
        "provider_session_id",
        "thread_id",
        "session_id",
        "threadId",
        "sessionId",
    ]
    .iter()
    .find_map(|key| optional_str(params, key).map(ToOwned::to_owned))
}

fn take_session_binding(settings: &mut Map<String, Value>) -> Option<String> {
    settings
        .remove("session")
        .and_then(|value| value.as_str().map(str::trim).map(ToOwned::to_owned))
        .filter(|value| !value.is_empty())
}

fn adapter_provider_session_id(adapter_result: &Value, conversation_id: &str) -> Option<String> {
    let object = adapter_result.as_object()?;
    let accepted = object
        .get("ok")
        .and_then(Value::as_bool)
        .or_else(|| object.get("accepted").and_then(Value::as_bool))
        .unwrap_or(false);
    if !accepted {
        return None;
    }
    object
        .get("provider_session_id")
        .or_else(|| object.get("thread_id"))
        .or_else(|| object.get("session_id"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .filter(|value| *value != conversation_id)
        .map(ToOwned::to_owned)
}

fn optional_owned(params: &JsonMap, key: &str) -> Option<String> {
    optional_str(params, key).map(ToOwned::to_owned)
}

fn string_from_map(params: &JsonMap, key: &str) -> Option<String> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn bool_from_map(params: &JsonMap, key: &str) -> bool {
    params.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn resolve_extension_id(
    state: &AppState,
    params: &JsonMap,
    meta: &ConversationMeta,
) -> Option<String> {
    optional_str(params, "extension_id")
        .or_else(|| optional_str(params, "agent_type"))
        .or_else(|| optional_str(params, "agent"))
        .map(ToOwned::to_owned)
        .or_else(|| meta.extension_id.clone())
        .or_else(|| meta.agent_type.clone())
        .or_else(|| string_from_map(&meta.settings, "agent"))
        .or_else(|| {
            state
                .extensions
                .list()
                .into_iter()
                .find(|entry| entry.active)
                .map(|entry| entry.id)
        })
}

fn resolve_cwd(params: &JsonMap, meta: &ConversationMeta) -> Option<PathBuf> {
    optional_str(params, "cwd")
        .map(ToOwned::to_owned)
        .or_else(|| meta.cwd.clone())
        .or_else(|| string_from_map(&meta.settings, "cwd"))
        .map(PathBuf::from)
}

fn build_mcp_context(
    config: &crate::config::ServerConfig,
    conversation_id: &str,
    settings: Option<&JsonMap>,
    cwd: Option<&str>,
) -> Option<McpContext> {
    let mut requested_servers = JsonMap::new();
    if let Some(existing_servers) = settings
        .and_then(|value| value.get("mcp_servers"))
        .and_then(Value::as_object)
    {
        requested_servers.extend(existing_servers.clone());
    }

    let resolved_cwd = cwd
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| {
            settings
                .and_then(|value| string_from_map(value, "cwd"))
                .map(PathBuf::from)
        });

    let mut defaults = JsonMap::new();
    let mut agent_pty_defaults = JsonMap::new();
    agent_pty_defaults.insert("enabled_by_default".to_owned(), Value::Bool(true));
    agent_pty_defaults.insert("eager_load_tools".to_owned(), Value::Bool(true));
    agent_pty_defaults.insert("transport".to_owned(), Value::String("stdio".to_owned()));
    agent_pty_defaults.insert(
        "appserver_origin".to_owned(),
        Value::String(appserver_origin(config)),
    );
    agent_pty_defaults.insert(
        "conversation_id".to_owned(),
        Value::String(conversation_id.to_owned()),
    );
    if let Some(path) = resolved_cwd.as_ref() {
        agent_pty_defaults.insert(
            "cwd".to_owned(),
            Value::String(path.to_string_lossy().into_owned()),
        );
    }
    defaults.insert(
        AGENT_PTY_BLOCKS_MCP_SERVER_NAME.to_owned(),
        Value::Object(agent_pty_defaults),
    );

    let te2_enabled = settings.is_some_and(|value| bool_from_map(value, "te2_mcp_integration"));
    if te2_enabled {
        let mut te2_defaults = JsonMap::new();
        te2_defaults.insert("enabled_by_default".to_owned(), Value::Bool(true));
        te2_defaults.insert("transport".to_owned(), Value::String("http".to_owned()));
        te2_defaults.insert(
            "base_url".to_owned(),
            Value::String(te2_base_url_from_settings(settings)),
        );
        defaults.insert(TE2_MCP_SERVER_NAME.to_owned(), Value::Object(te2_defaults));
    }

    Some(McpContext {
        conversation_id: conversation_id.to_owned(),
        cwd: resolved_cwd,
        requested_servers,
        defaults,
    })
}

fn appserver_origin(config: &crate::config::ServerConfig) -> String {
    let host = match config.host.trim() {
        "" | "0.0.0.0" | "::" => "127.0.0.1",
        value => value,
    };
    format!("http://{}:{}", host, config.port)
}

fn te2_base_url_from_settings(settings: Option<&JsonMap>) -> String {
    settings
        .and_then(|value| string_from_map(value, "te2_base_url"))
        .unwrap_or_else(|| DEFAULT_TE2_BASE_URL.to_owned())
}

fn optional_map(params: &JsonMap, key: &str) -> Option<Map<String, Value>> {
    params.get(key).and_then(Value::as_object).cloned()
}

fn optional_u64_direct(params: &JsonMap, key: &str) -> Option<u64> {
    params.get(key).and_then(Value::as_u64)
}

fn optional_i64(params: &JsonMap, key: &str, nested_key: &str) -> Option<i64> {
    params
        .get(key)
        .and_then(Value::as_object)
        .and_then(|nested| nested.get(nested_key))
        .and_then(Value::as_i64)
}

fn rpc_error(code: i64, message: impl Into<String>) -> RpcError {
    RpcError::new(code, message, None)
}

fn internal_error(error: impl std::fmt::Display) -> RpcError {
    RpcError::new(-32603, error.to_string(), None)
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
    use als_dto::RuntimeRoots;
    use serde_json::json;
    use std::{
        fs,
        time::{SystemTime, UNIX_EPOCH},
    };

    #[test]
    fn maps_adapter_event_types_to_conversation_notifications() {
        assert_eq!(
            notification_method_for_event_type("assistant_delta"),
            Some("conversation.message.delta")
        );
        assert_eq!(
            notification_method_for_event_type("assistant_finalize"),
            Some("conversation.message.final")
        );
        assert_eq!(
            notification_method_for_event_type("shell_begin"),
            Some("conversation.command.begin")
        );
        assert_eq!(
            notification_method_for_event_type("token_count"),
            Some("conversation.token.updated")
        );
        assert_eq!(
            notification_method_for_event_type("list_updated"),
            Some("conversation.list.updated")
        );
        assert_eq!(notification_method_for_event_type("debug_trace"), None);
    }

    #[test]
    fn draft_updated_event_matches_frontend_contract() {
        assert_eq!(
            draft_updated_event("conv-draft", "hello"),
            json!({
                "conversation_id": "conv-draft",
                "draft": "hello",
            })
        );
    }

    #[test]
    fn extracts_conversation_scoped_adapter_payloads() {
        let (conversation_id, value) = adapter_conversation_object(
            json!({"type": "assistant_delta", "conversation_id": "conv-a"}),
        )
        .expect("conversation_id should be extracted");
        assert_eq!(conversation_id, "conv-a");
        assert_eq!(value["conversation_id"], "conv-a");

        assert!(adapter_conversation_object(json!({"type": "assistant_delta"})).is_none());
        assert!(adapter_conversation_object(json!("not an object")).is_none());
    }

    #[test]
    fn skips_rust_owned_adapter_user_events() {
        assert!(should_skip_adapter_event_type("message"));
        assert!(!should_skip_adapter_event_type("assistant_finalize"));
        assert!(should_skip_adapter_transcript_entry(
            &json!({"role": "user", "conversation_id": "conv-a", "text": "skip"})
        ));
        assert!(!should_skip_adapter_transcript_entry(
            &json!({"role": "user", "conversation_id": "conv-a", "text": "keep", "_hydrated_history": true})
        ));
        assert!(!should_skip_adapter_transcript_entry(
            &json!({"role": "assistant", "conversation_id": "conv-a", "text": "keep"})
        ));
    }

    #[test]
    fn extracts_and_strips_provider_binding_values() {
        let mut params = JsonMap::new();
        params.insert("threadId".to_owned(), json!("thread-from-param"));
        assert_eq!(
            binding_id_from_params(&params),
            Some("thread-from-param".to_owned())
        );

        let mut settings = Map::new();
        settings.insert("session".to_owned(), json!("picked-session"));
        settings.insert("cwd".to_owned(), json!("/repo/project"));
        assert_eq!(
            take_session_binding(&mut settings),
            Some("picked-session".to_owned())
        );
        assert!(!settings.contains_key("session"));
        assert_eq!(settings["cwd"], "/repo/project");
    }

    #[test]
    fn mcp_context_includes_te2_base_url_when_enabled() {
        let config = ServerConfig {
            host: "0.0.0.0".to_owned(),
            port: 12459,
            extensions_dir: PathBuf::from("extensions"),
            roots: RuntimeRoots {
                data_dir: PathBuf::from("data"),
                cache_dir: PathBuf::from("cache"),
                config_dir: PathBuf::from("config"),
                static_dir: PathBuf::from("static"),
            },
            adapters: AdapterConfig {
                python_bin: "python".to_owned(),
            },
            framework_shells: FrameworkShellConfig::default(),
        };

        let mut settings = JsonMap::new();
        settings.insert("te2_mcp_integration".to_owned(), json!(true));
        let context = build_mcp_context(&config, "conv-te2", Some(&settings), Some("/repo"))
            .expect("mcp context should be built");
        assert_eq!(
            context.defaults[TE2_MCP_SERVER_NAME]["base_url"],
            DEFAULT_TE2_BASE_URL
        );

        settings.insert("te2_base_url".to_owned(), json!("http://127.0.0.1:9090/"));
        let context = build_mcp_context(&config, "conv-te2", Some(&settings), Some("/repo"))
            .expect("mcp context should be built");
        assert_eq!(
            context.defaults[TE2_MCP_SERVER_NAME]["base_url"],
            "http://127.0.0.1:9090/"
        );
    }

    #[test]
    fn normalizes_adapter_provider_binding_ack() {
        assert_eq!(
            adapter_provider_session_id(
                &json!({"ok": true, "provider_session_id": "provider-123"}),
                "conv_123",
            ),
            Some("provider-123".to_owned())
        );
        assert_eq!(
            adapter_provider_session_id(
                &json!({"accepted": true, "session_id": "session-456"}),
                "conv_123",
            ),
            Some("session-456".to_owned())
        );
        assert_eq!(
            adapter_provider_session_id(
                &json!({"ok": true, "provider_session_id": "provider-123", "session_id": "legacy-456"}),
                "conv_123",
            ),
            Some("provider-123".to_owned())
        );
        assert_eq!(
            adapter_provider_session_id(&json!({"ok": true}), "conv_123"),
            None
        );
        assert_eq!(
            adapter_provider_session_id(
                &json!({"ok": false, "provider_session_id": "provider-123"}),
                "conv_123",
            ),
            None
        );
        assert_eq!(
            adapter_provider_session_id(&json!({"ok": true, "session_id": "conv_123"}), "conv_123",),
            None
        );
    }

    #[test]
    fn persists_scoped_adapter_transcript_records() {
        let root = std::env::temp_dir().join(format!("als-rs-rpc-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });

        persist_adapter_transcript_entry(
            &state,
            json!({"role": "assistant", "conversation_id": "conv-a", "text": "pong"}),
        )
        .unwrap();
        persist_adapter_transcript_entry(
            &state,
            json!({"role": "user", "conversation_id": "conv-a", "text": "skip"}),
        )
        .unwrap();
        persist_adapter_transcript_entry(
            &state,
            json!({"role": "user", "conversation_id": "conv-a", "text": "keep", "_hydrated_history": true}),
        )
        .unwrap();
        persist_adapter_transcript_entry(
            &state,
            json!({"role": "assistant", "text": "missing conversation"}),
        )
        .unwrap();

        let rows = state.conversations.read_transcript("conv-a").unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["conversation_id"], "conv-a");
        assert_eq!(rows[0]["role"], "assistant");
        assert_eq!(rows[0]["text"], "pong");
        assert_eq!(rows[0]["order_id"], 0);
        assert_eq!(rows[1]["conversation_id"], "conv-a");
        assert_eq!(rows[1]["role"], "user");
        assert_eq!(rows[1]["text"], "keep");
        assert_eq!(rows[1]["order_id"], 1);
        assert!(rows[1].get("_hydrated_history").is_none());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn persist_adapter_transcript_entry_truncates_card_output() {
        let root = std::env::temp_dir().join(format!("als-rs-rpc-truncate-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });

        persist_adapter_transcript_entry(
            &state,
            json!({
                "role": "search",
                "conversation_id": "conv-truncate",
                "content": format!(
                    "/repo/file.rs:10:{}",
                    "x".repeat(crate::card_truncation::MAX_CARD_OUTPUT_BYTES + 100)
                ),
            }),
        )
        .unwrap();

        let rows = state
            .conversations
            .read_transcript("conv-truncate")
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["truncated"], true);
        assert!(
            rows[0]["content"]
                .as_str()
                .expect("content should be a string")
                .len()
                <= crate::card_truncation::MAX_CARD_OUTPUT_BYTES
        );
        assert!(
            rows[0]["truncation_note"]
                .as_str()
                .expect("truncation_note should be a string")
                .contains("search output truncated")
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn resolves_send_extension_from_persisted_settings() {
        let root = std::env::temp_dir().join(format!("als-rs-rpc-routing-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("other-ext"));
        settings.insert("cwd".to_owned(), json!("/repo/project"));
        let meta = state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some("routing-test".to_owned()),
                settings,
                ..CreateConversationRequest::default()
            })
            .unwrap();

        assert_eq!(
            resolve_extension_id(&state, &JsonMap::new(), &meta),
            Some("other-ext".to_owned())
        );
        assert_eq!(
            resolve_cwd(&JsonMap::new(), &meta),
            Some("/repo/project".into())
        );

        let mut params = JsonMap::new();
        params.insert("extension_id".to_owned(), json!("sample-ext"));
        assert_eq!(
            resolve_extension_id(&state, &params, &meta),
            Some("sample-ext".to_owned())
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn conversation_list_uses_shared_selection_state() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-selection-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });
        state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some("conv-select".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        state
            .ui_selection
            .select(
                Some("conv-select".to_owned()),
                Some("conversation".to_owned()),
            )
            .unwrap();

        let listed = conversation_list(&state).unwrap();
        assert_eq!(listed["active_conversation_id"], "conv-select");
        assert_eq!(listed["active_view"], "conversation");

        assert!(state.conversations.delete("conv-select").unwrap());
        let selection = state.ui_selection.snapshot().unwrap();
        if selection.active_conversation_id.as_deref() == Some("conv-select") {
            state
                .ui_selection
                .select(None, Some("splash".to_owned()))
                .unwrap();
        }

        let after_delete = conversation_list(&state).unwrap();
        assert_eq!(after_delete["active_conversation_id"], Value::Null);
        assert_eq!(after_delete["active_view"], "splash");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn conversation_list_carries_monotonic_revision() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-list-revision-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });

        assert_eq!(conversation_list(&state).unwrap()["revision"], json!(0));
        assert_eq!(state.bump_list_revision(), 1);
        assert_eq!(conversation_list(&state).unwrap()["revision"], json!(1));
        assert_eq!(state.bump_list_revision(), 2);
        assert_eq!(conversation_list(&state).unwrap()["revision"], json!(2));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn conversation_get_hydrates_persisted_active_conversation() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-active-restore-test-{}", unix_millis()));
        let config = ServerConfig {
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
        };
        let state = AppState::new(config.clone());
        state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some("conv-active".to_owned()),
                ..CreateConversationRequest::default()
            })
            .unwrap();
        state
            .ui_selection
            .select(
                Some("conv-active".to_owned()),
                Some("conversation".to_owned()),
            )
            .unwrap();

        let reloaded_state = AppState::new(config);
        let restored = conversation_get(&reloaded_state, &JsonMap::new()).unwrap();
        assert_eq!(restored["conversation_id"], "conv-active");
        assert_eq!(restored["active_view"], "conversation");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn approval_events_persist_and_handoff_records_match_legacy_shape() {
        let root = std::env::temp_dir().join(format!("als-rs-rpc-approval-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
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
        });
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("other-ext"));
        state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some("conv-approval".to_owned()),
                thread_id: Some("thread-123".to_owned()),
                settings,
                ..CreateConversationRequest::default()
            })
            .unwrap();

        persist_pending_approval_event(
            &state,
            "conv-approval",
            &json!({
                "type": "approval",
                "conversation_id": "conv-approval",
                "id": "req-1",
                "request_id": "req-1",
                "kind": "user_input",
                "request_method": "agent-pty/ask-user",
                "request_params": {"question": "Proceed?"},
                "payload": {"question": "Proceed?"},
                "turn_id": "turn-a",
                "created_at": "created"
            }),
        )
        .unwrap();

        let meta = state.conversations.load_meta("conv-approval").unwrap();
        let descriptor = meta.pending_approvals["req-1"]
            .as_object()
            .expect("pending descriptor should be persisted")
            .clone();
        assert_eq!(descriptor["agent"], "other-ext");
        assert_eq!(descriptor["thread_id"], "thread-123");
        assert_eq!(descriptor["render_event"]["order_id"], -1);

        let mut resolution = JsonMap::new();
        resolution.insert("decision".to_owned(), json!("decline"));
        let mut handoff =
            build_approval_handoff_event(&state, "conv-approval", &descriptor, &resolution)
                .unwrap();
        let recorded =
            append_approval_handoff_transcript_entry(&state, "conv-approval", &handoff).unwrap();
        merge_recorded_approval_fields(&mut handoff, &recorded);
        state
            .conversations
            .remove_pending_approval("conv-approval", "req-1")
            .unwrap();

        assert_eq!(handoff["type"], "approval_handoff");
        assert_eq!(handoff["status"], "declined");
        assert_eq!(handoff["ask_user_msg_id"], 0);
        assert_eq!(recorded["role"], "approval");
        assert_eq!(recorded["event"], "approval_decision");
        assert_eq!(recorded["request_id"], "req-1");
        assert_eq!(recorded["order_id"], 0);
        assert!(
            state
                .conversations
                .load_meta("conv-approval")
                .unwrap()
                .pending_approvals
                .is_empty()
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn ask_user_submission_completes_on_ipc_ack() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-ask-user-ipc-test-{}", unix_millis()));
        let state = AppState::new(ServerConfig {
            host: "127.0.0.1".to_owned(),
            port: 12459,
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
        });
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("other-ext"));
        state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some("conv-ask-user".to_owned()),
                thread_id: Some("thread-ask-user".to_owned()),
                settings,
                ..CreateConversationRequest::default()
            })
            .unwrap();

        persist_pending_approval_event(
            &state,
            "conv-ask-user",
            &json!({
                "type": "approval",
                "conversation_id": "conv-ask-user",
                "id": "conv-ask-user",
                "request_id": "conv-ask-user",
                "kind": "user_input",
                "request_method": "agent-pty/ask-user",
                "request_params": {"requestId": "conv-ask-user", "question": "Pick one"},
                "payload": {"requestId": "conv-ask-user", "question": "Pick one"},
                "turn_id": "turn-ask",
                "created_at": "created"
            }),
        )
        .unwrap();

        let mut resolution = JsonMap::new();
        resolution.insert("answer".to_owned(), json!("Approve"));
        resolution.insert("accepted".to_owned(), json!(true));
        record_pending_approval_submission(&state, "conv-ask-user", "conv-ask-user", &resolution)
            .unwrap();
        let pending_meta = state.conversations.load_meta("conv-ask-user").unwrap();
        assert_eq!(
            pending_meta.pending_approvals["conv-ask-user"]["submitted_resolution"]["answer"],
            "Approve"
        );

        let completed = complete_submitted_ask_user_interaction(&state, "conv-ask-user")
            .unwrap()
            .expect("submitted ask_user should complete");
        assert_eq!(completed.0, "conv-ask-user");
        assert_eq!(completed.1["type"], "approval_handoff");
        assert_eq!(completed.1["status"], "accepted");
        assert_eq!(completed.2["role"], "approval");
        assert!(
            state
                .conversations
                .load_meta("conv-ask-user")
                .unwrap()
                .pending_approvals
                .is_empty()
        );

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn mcp_context_includes_als_rs_origin_for_stdio_ipc() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-mcp-origin-test-{}", unix_millis()));
        let config = ServerConfig {
            host: "0.0.0.0".to_owned(),
            port: 12459,
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
        };
        let context = build_mcp_context(&config, "conv-origin", None, Some("/repo"))
            .expect("mcp context should be built");
        let agent_defaults = context
            .defaults
            .get(AGENT_PTY_BLOCKS_MCP_SERVER_NAME)
            .and_then(Value::as_object)
            .expect("agent-pty defaults should exist");
        assert_eq!(agent_defaults["appserver_origin"], "http://127.0.0.1:12459");
        assert_eq!(agent_defaults["conversation_id"], "conv-origin");

        let _ = fs::remove_dir_all(root);
    }

    fn unix_millis() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_millis()
    }
}
