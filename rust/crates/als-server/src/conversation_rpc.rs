use crate::{
    adapter_process::AdapterCapturedEvent,
    conversation_store::{
        ConversationMeta, ConversationMetaUpdate, CreateConversationRequest, TranscriptOffset,
    },
    state::AppState,
};
use als_adapter_protocol::{ConversationResumeParams, ConversationSendParams, JsonMap, methods};
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
const METHOD_DRAFT_SET: &str = "conversation.draft.set";
const METHOD_SEND: &str = "conversation.send";
const METHOD_REPLAY_GET_CHUNK: &str = "conversation.replay.getChunk";
const METHOD_INTERRUPT: &str = "conversation.interrupt";
const METHOD_COMPACT: &str = "conversation.compact";
const METHOD_APPROVAL_RESPOND: &str = "conversation.approval.respond";
const METHOD_SHELL_EXEC: &str = "conversation.shell.exec";
const METHOD_PINS_SET: &str = "conversation.pins.set";
const AGENT_PTY_ASK_USER_REQUEST_METHOD: &str = "agent-pty/ask-user";

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
    Data(request): Data<JsonRpcRequest>,
    ack: AckSender,
) {
    let id = request.id.clone();
    let response = match dispatch_rpc(socket, state, request).await {
        Ok(result) => RpcAck::Success(SuccessResponse::new(id, result)),
        Err(error) => RpcAck::Error(ErrorResponse::new(id, error)),
    };
    let _ = ack.send(&response);
}

async fn dispatch_rpc(
    socket: SocketRef,
    state: AppState,
    request: JsonRpcRequest,
) -> Result<Value, RpcError> {
    if request.jsonrpc != JSONRPC_VERSION {
        return Err(rpc_error(-32600, "Invalid JSON-RPC version"));
    }

    match request.method.as_str() {
        METHOD_LIST => conversation_list(&state),
        METHOD_GET => conversation_get(&state, &request.params),
        METHOD_CREATE => conversation_create(socket, state, request.params).await,
        METHOD_SELECT => conversation_select(&state, &request.params),
        METHOD_UPDATE => conversation_update(socket, state, request.params).await,
        METHOD_DELETE => conversation_delete(&state, &request.params),
        METHOD_DRAFT_SET => conversation_draft_set(&state, &request.params),
        METHOD_REPLAY_GET_CHUNK => conversation_replay_get_chunk(&state, &request.params),
        METHOD_SEND => conversation_send(socket, state, request.params).await,
        METHOD_PINS_SET => conversation_pins_set(&state, &request.params),
        METHOD_APPROVAL_RESPOND => conversation_approval_respond(socket, state, request.params).await,
        METHOD_INTERRUPT | METHOD_COMPACT | METHOD_SHELL_EXEC => {
            Ok(json!({
                "ok": false,
                "error": format!("{} is not implemented in ALS-RS yet", request.method),
                "transport": "rpc"
            }))
        }
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
        "active_conversation_id": selection.active_conversation_id,
        "active_view": selection.active_view,
        "pinned_conversations": pinned_conversations,
        "transport": "rpc"
    }))
}

fn conversation_get(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(&params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    meta_result(
        state
            .conversations
            .load_meta(conversation_id)
            .map_err(internal_error)?,
    )
}

async fn conversation_create(
    socket: SocketRef,
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
                    emit_meta_updated_to_socket(&socket, &state, &updated.conversation_id);
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
    Ok(value)
}

fn conversation_select(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
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
    Ok(value)
}

async fn conversation_update(
    socket: SocketRef,
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
    emit_meta_updated_to_socket(&socket, &state, conversation_id);

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
                    emit_meta_updated_to_socket(&socket, &state, conversation_id);
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

    Ok(value)
}

fn conversation_pins_set(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
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
    Ok(json!({
        "ok": true,
        "pinned_conversations": pinned,
        "transport": "rpc"
    }))
}

fn conversation_delete(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
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
    }
    Ok(
        json!({"ok": true, "deleted": deleted, "conversation_id": conversation_id, "transport": "rpc"}),
    )
}

fn conversation_draft_set(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let draft = optional_str(params, "draft").unwrap_or("").to_owned();
    state
        .conversations
        .update_meta(
            conversation_id,
            ConversationMetaUpdate {
                draft: Some(draft),
                ..ConversationMetaUpdate::default()
            },
        )
        .map_err(internal_error)?;
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
    emit_meta_updated_to_socket(&socket, &state, &conversation_id);

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
        emit_meta_updated_to_socket(&socket, &state, &conversation_id);
    }
    let mut result = adapter_result.as_object().cloned().unwrap_or_default();
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
        AdapterCapturedEvent::Other(other) => {
            let _ = (&other.method, &other.params);
            Ok(())
        }
    }
}

async fn forward_adapter_live_event(
    io: &SocketIo,
    state: &AppState,
    value: Value,
) -> Result<(), RpcError> {
    let Some((conversation_id, event)) = adapter_conversation_object(value) else {
        return Ok(());
    };
    let event_type = event
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| rpc_error(-32603, "adapter live event is missing type"))?;
    if should_skip_adapter_event_type(event_type) {
        return Ok(());
    }
    let Some(method) = notification_method_for_event_type(event_type) else {
        return Ok(());
    };
    if event_type.trim().eq_ignore_ascii_case("approval") {
        persist_pending_approval_event(state, &conversation_id, &event)?;
        emit_rpc_notification_to_namespace(io, method, event).await;
        emit_meta_updated_to_namespace(io, state, &conversation_id).await;
        return Ok(());
    }
    emit_rpc_notification_to_namespace(io, method, event).await;
    Ok(())
}

async fn conversation_approval_respond(
    socket: SocketRef,
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
        emit_meta_updated_to_socket(&socket, &state, &conversation_id);
        return Err(rpc_error(-32009, "Approval is stale or no longer actionable"));
    }

    let mut handoff_event =
        build_approval_handoff_event(&state, &conversation_id, &descriptor, &resolution)?;
    let recorded_entry =
        append_approval_handoff_transcript_entry(&state, &conversation_id, &handoff_event)?;
    merge_recorded_approval_fields(&mut handoff_event, &recorded_entry);
    let _ = state
        .conversations
        .remove_pending_approval(&conversation_id, &request_id);
    emit_meta_updated_to_socket(&socket, &state, &conversation_id);
    emit_rpc_notification(&socket, "conversation.approval.handoff", Value::Object(handoff_event.clone()));
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

async fn persist_adapter_transcript(
    io: &SocketIo,
    state: &AppState,
    value: Value,
) -> Result<(), RpcError> {
    if let Some(conversation_id) = persist_adapter_transcript_entry(state, value)? {
        emit_meta_updated_to_namespace(io, state, &conversation_id).await;
    }
    Ok(())
}

fn persist_adapter_transcript_entry(
    state: &AppState,
    value: Value,
) -> Result<Option<String>, RpcError> {
    let Some((conversation_id, entry)) = adapter_conversation_object(value) else {
        return Ok(None);
    };
    if should_skip_adapter_transcript_entry(&entry) {
        return Ok(None);
    }
    state
        .conversations
        .append_transcript(&conversation_id, entry)
        .map(|_| ())
        .map_err(internal_error)?;
    Ok(Some(conversation_id))
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
        .is_some_and(|role| role.trim().eq_ignore_ascii_case("user"))
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
        .unwrap_or_else(|| "codex-ext".to_owned());
    let thread_id = first_nonempty_event_string(
        event_object,
        &["thread_id", "provider_session_id", "session_id"],
    )
    .or_else(|| meta.thread_id.clone())
    .or_else(|| meta.provider_session_id.clone());
    let created_at = first_nonempty_event_string(event_object, &["created_at"])
        .unwrap_or_else(utc_ts_for_rpc);
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
        .or_else(|| descriptor.get("payload").and_then(Value::as_object).cloned())
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
        Value::String(
            string_from_map(&event, "id").unwrap_or_else(|| request_id.clone()),
        ),
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
        handoff_event.get("decision").cloned().unwrap_or(Value::Null),
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

fn emit_meta_updated_to_socket(socket: &SocketRef, state: &AppState, conversation_id: &str) {
    let Ok(meta) = state.conversations.load_meta(conversation_id) else {
        return;
    };
    let Ok(value) = serde_json::to_value(meta) else {
        return;
    };
    emit_rpc_notification(socket, "conversation.meta.updated", value);
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
        assert_eq!(notification_method_for_event_type("debug_trace"), None);
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
        assert_eq!(adapter_provider_session_id(&json!({"ok": true}), "conv_123"), None);
        assert_eq!(
            adapter_provider_session_id(
                &json!({"ok": false, "provider_session_id": "provider-123"}),
                "conv_123",
            ),
            None
        );
        assert_eq!(
            adapter_provider_session_id(
                &json!({"ok": true, "session_id": "conv_123"}),
                "conv_123",
            ),
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
                copilot_python: "python".to_owned(),
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
            json!({"role": "assistant", "text": "missing conversation"}),
        )
        .unwrap();

        let rows = state.conversations.read_transcript("conv-a").unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["conversation_id"], "conv-a");
        assert_eq!(rows[0]["role"], "assistant");
        assert_eq!(rows[0]["text"], "pong");
        assert_eq!(rows[0]["order_id"], 0);

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
                copilot_python: "python".to_owned(),
            },
            framework_shells: FrameworkShellConfig::default(),
        });
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("codex-ext"));
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
            Some("codex-ext".to_owned())
        );
        assert_eq!(
            resolve_cwd(&JsonMap::new(), &meta),
            Some("/repo/project".into())
        );

        let mut params = JsonMap::new();
        params.insert("extension_id".to_owned(), json!("copilot-sdk"));
        assert_eq!(
            resolve_extension_id(&state, &params, &meta),
            Some("copilot-sdk".to_owned())
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
                copilot_python: "python".to_owned(),
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

        let mut params = JsonMap::new();
        params.insert("conversation_id".to_owned(), json!("conv-select"));
        assert_eq!(
            conversation_delete(&state, &params).unwrap()["deleted"],
            json!(true)
        );

        let after_delete = conversation_list(&state).unwrap();
        assert_eq!(after_delete["active_conversation_id"], Value::Null);
        assert_eq!(after_delete["active_view"], "splash");

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn approval_events_persist_and_handoff_records_match_legacy_shape() {
        let root =
            std::env::temp_dir().join(format!("als-rs-rpc-approval-test-{}", unix_millis()));
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
                copilot_python: "python".to_owned(),
            },
            framework_shells: FrameworkShellConfig::default(),
        });
        let mut settings = JsonMap::new();
        settings.insert("agent".to_owned(), json!("codex-ext"));
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
        assert_eq!(descriptor["agent"], "codex-ext");
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

    fn unix_millis() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_millis()
    }
}
