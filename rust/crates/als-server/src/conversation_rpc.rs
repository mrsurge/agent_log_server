use crate::{
    conversation_store::{
        ConversationMeta, ConversationMetaUpdate, CreateConversationRequest, TranscriptOffset,
    },
    state::AppState,
};
use als_adapter_protocol::{ConversationSendParams, JsonMap, methods};
use als_jsonrpc::{ErrorResponse, RequestId, RpcError, SuccessResponse};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use socketioxide::{
    SocketIo,
    extract::{AckSender, Data, SocketRef, State},
};

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

pub fn register_conversations_rpc_namespace(io: &SocketIo) {
    io.ns(
        "/rpc/conversations",
        async |socket: SocketRef, State(_state): State<AppState>| {
            socket.on(RPC_EVENT, handle_rpc_request);
        },
    );
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
        METHOD_CREATE => conversation_create(&state, &request.params),
        METHOD_SELECT => conversation_select(&state, &request.params),
        METHOD_UPDATE => conversation_update(&state, &request.params),
        METHOD_DELETE => conversation_delete(&state, &request.params),
        METHOD_DRAFT_SET => conversation_draft_set(&state, &request.params),
        METHOD_REPLAY_GET_CHUNK => conversation_replay_get_chunk(&state, &request.params),
        METHOD_SEND => conversation_send(socket, state, request.params).await,
        METHOD_INTERRUPT
        | METHOD_COMPACT
        | METHOD_APPROVAL_RESPOND
        | METHOD_SHELL_EXEC
        | METHOD_PINS_SET => Ok(json!({
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
    Ok(json!({
        "items": items,
        "active_conversation_id": Value::Null,
        "active_view": "splash",
        "pinned_conversations": [],
        "transport": "rpc"
    }))
}

fn conversation_get(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    meta_result(
        state
            .conversations
            .load_meta(conversation_id)
            .map_err(internal_error)?,
    )
}

fn conversation_create(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let settings = optional_map(params, "settings").unwrap_or_default();
    let request = CreateConversationRequest {
        conversation_id: optional_str(params, "conversation_id").map(ToOwned::to_owned),
        title: optional_str(params, "title").map(ToOwned::to_owned),
        agent_type: optional_str(params, "agent_type").map(ToOwned::to_owned),
        settings,
    };
    meta_result(
        state
            .conversations
            .create(request)
            .map_err(internal_error)?,
    )
}

fn conversation_select(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let mut value = meta_json(
        state
            .conversations
            .load_meta(conversation_id)
            .map_err(internal_error)?,
    )?;
    value["active_view"] = json!(optional_str(params, "view").unwrap_or("conversation"));
    Ok(value)
}

fn conversation_update(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let update = ConversationMetaUpdate {
        settings: optional_map(params, "settings"),
        thread_id: optional_str(params, "thread_id").map(ToOwned::to_owned),
        title: optional_str(params, "title").map(ToOwned::to_owned),
        draft: None,
    };
    meta_result(
        state
            .conversations
            .update_meta(conversation_id, update)
            .map_err(internal_error)?,
    )
}

fn conversation_delete(state: &AppState, params: &JsonMap) -> Result<Value, RpcError> {
    let conversation_id = optional_str(params, "conversation_id")
        .ok_or_else(|| rpc_error(-32602, "conversation_id is required"))?;
    let deleted = state
        .conversations
        .delete(conversation_id)
        .map_err(internal_error)?;
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
            meta.conversation_id
        }
    };

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

    let adapter_params = ConversationSendParams {
        conversation_id: conversation_id.clone(),
        text,
        turn_id: optional_str(&params, "turn_id").map(ToOwned::to_owned),
        cwd: None,
        attachments: Vec::new(),
        toast_context: None,
        settings: state
            .conversations
            .load_meta(&conversation_id)
            .map_err(internal_error)?
            .settings,
    };
    state
        .adapter
        .initialize_copilot()
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
    let mut result = adapter_result.as_object().cloned().unwrap_or_default();
    result.insert("conversation_id".to_owned(), Value::String(conversation_id));
    result.insert("transport".to_owned(), Value::String("rpc".to_owned()));
    Ok(Value::Object(result))
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

fn optional_str<'a>(params: &'a JsonMap, key: &str) -> Option<&'a str> {
    params
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
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
