use crate::{
    adapter_process::AdapterCapturedEvent,
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
    let meta = state
        .conversations
        .load_meta(&conversation_id)
        .map_err(internal_error)?;
    let extension_id = optional_str(&params, "extension_id")
        .or(meta.agent_type.as_deref())
        .unwrap_or("copilot-sdk")
        .to_owned();

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
        extension_id: extension_id.clone(),
        conversation_id: conversation_id.clone(),
        text,
        turn_id: optional_str(&params, "turn_id").map(ToOwned::to_owned),
        cwd: None,
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
        AdapterCapturedEvent::Live(value) => forward_adapter_live_event(io, value).await,
        AdapterCapturedEvent::Transcript(value) => persist_adapter_transcript(state, value),
        AdapterCapturedEvent::Other(other) => {
            let _ = (&other.method, &other.params);
            Ok(())
        }
    }
}

async fn forward_adapter_live_event(io: &SocketIo, value: Value) -> Result<(), RpcError> {
    let Some((_, event)) = adapter_conversation_object(value) else {
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
    emit_rpc_notification_to_namespace(io, method, event).await;
    Ok(())
}

fn persist_adapter_transcript(state: &AppState, value: Value) -> Result<(), RpcError> {
    let Some((conversation_id, entry)) = adapter_conversation_object(value) else {
        return Ok(());
    };
    if should_skip_adapter_transcript_entry(&entry) {
        return Ok(());
    }
    state
        .conversations
        .append_transcript(&conversation_id, entry)
        .map(|_| ())
        .map_err(internal_error)
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{AdapterConfig, ServerConfig};
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
        });

        persist_adapter_transcript(
            &state,
            json!({"role": "assistant", "conversation_id": "conv-a", "text": "pong"}),
        )
        .unwrap();
        persist_adapter_transcript(
            &state,
            json!({"role": "user", "conversation_id": "conv-a", "text": "skip"}),
        )
        .unwrap();
        persist_adapter_transcript(
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

    fn unix_millis() -> u128 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_millis()
    }
}
