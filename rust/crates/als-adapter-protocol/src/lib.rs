use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::path::PathBuf;

pub type JsonMap = Map<String, Value>;

pub const ADAPTER_PROTOCOL_VERSION: &str = "0.1.0";

pub mod methods {
    pub const EXTENSION_INITIALIZE: &str = "extension.initialize";
    pub const EXTENSION_SHUTDOWN: &str = "extension.shutdown";
    pub const EXTENSION_LIST_MODELS: &str = "extension.list_models";
    pub const EXTENSION_LIST_SESSIONS: &str = "extension.list_sessions";
    pub const CONVERSATION_START: &str = "conversation.start";
    pub const CONVERSATION_RESUME: &str = "conversation.resume";
    pub const CONVERSATION_SEND: &str = "conversation.send";
    pub const CONVERSATION_INTERRUPT: &str = "conversation.interrupt";
    pub const CONVERSATION_COMPACT: &str = "conversation.compact";
    pub const APPROVAL_RESPOND: &str = "approval.respond";
}

pub mod events {
    pub const LIVE_EVENT: &str = "event.live";
    pub const TRANSCRIPT_RECORD: &str = "event.transcript_record";
    pub const STATUS: &str = "event.status";
    pub const USER_MESSAGE: &str = "event.user_message";
    pub const ASSISTANT_DELTA: &str = "event.assistant_delta";
    pub const ASSISTANT_FINALIZE: &str = "event.assistant_finalize";
    pub const REASONING_DELTA: &str = "event.reasoning_delta";
    pub const REASONING_FINALIZE: &str = "event.reasoning_finalize";
    pub const TOOL_BEGIN: &str = "event.tool_begin";
    pub const TOOL_DELTA: &str = "event.tool_delta";
    pub const TOOL_END: &str = "event.tool_end";
    pub const SHELL_BEGIN: &str = "event.shell_begin";
    pub const SHELL_DELTA: &str = "event.shell_delta";
    pub const SHELL_END: &str = "event.shell_end";
    pub const APPROVAL_REQUEST: &str = "event.approval_request";
    pub const TOKEN_USAGE: &str = "event.token_usage";
    pub const ERROR: &str = "event.error";
    pub const WARNING: &str = "event.warning";
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExtensionInitializeParams {
    pub extension_id: String,
    pub cwd: PathBuf,
    pub data_dir: PathBuf,
    pub cache_dir: PathBuf,
    pub config_dir: PathBuf,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExtensionInitializeResult {
    pub extension_id: String,
    pub protocol_version: String,
    pub capabilities: AdapterCapabilities,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct AdapterCapabilities {
    #[serde(default)]
    pub conversations: bool,
    #[serde(default)]
    pub models: bool,
    #[serde(default)]
    pub sessions: bool,
    #[serde(default)]
    pub approvals: bool,
    #[serde(default)]
    pub compaction: bool,
    #[serde(default)]
    pub interruption: bool,
    #[serde(default)]
    pub live_events: bool,
    #[serde(default)]
    pub transcript_records: bool,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub extra: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExtensionListModelsResult {
    #[serde(default)]
    pub models: Vec<AdapterModelInfo>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AdapterModelInfo {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub supported_reasoning_efforts: Vec<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub capabilities: JsonMap,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExtensionListSessionsParams {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub limit: Option<u32>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExtensionListSessionsResult {
    #[serde(default)]
    pub sessions: Vec<AdapterSessionInfo>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AdapterSessionInfo {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ConversationStartParams {
    pub conversation_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ConversationResumeParams {
    pub conversation_id: String,
    pub provider_session_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ConversationSendParams {
    pub conversation_id: String,
    pub text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cwd: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub attachments: Vec<AdapterAttachment>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub toast_context: Option<ToastContext>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub settings: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AdapterAttachment {
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ToastContext {
    pub toast_id: String,
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub assistant_id: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ConversationAckResult {
    pub conversation_id: String,
    #[serde(default)]
    pub accepted: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default)]
    pub restore_draft: bool,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ConversationControlParams {
    pub conversation_id: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ConversationControlResult {
    pub conversation_id: String,
    pub ok: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ApprovalRespondParams {
    pub conversation_id: String,
    pub request_id: String,
    pub decision: ApprovalDecision,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalDecision {
    Approve,
    Deny,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AdapterLiveEvent {
    #[serde(rename = "type")]
    pub event_type: AdapterLiveEventType,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subagent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delta: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub column: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty", flatten)]
    pub extra: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AdapterLiveEventType {
    Message,
    AssistantDelta,
    AssistantFinalize,
    ReasoningDelta,
    ReasoningFinalize,
    Thought,
    ToolInteraction,
    ToolBegin,
    ToolDelta,
    ToolEnd,
    ShellBegin,
    ShellDelta,
    ShellEnd,
    CommandResult,
    Diff,
    Error,
    Warning,
    Status,
    TokenCount,
    Approval,
    ApprovalHandoff,
    Toast,
    Plan,
    PlanState,
    PlanUpdate,
    SubagentStart,
    SubagentEnd,
    Search,
    View,
    Activity,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct AdapterTranscriptRecord {
    pub role: AdapterTranscriptRole,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub item_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub conversation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub subagent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub path: Option<PathBuf>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub column: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub internal: Option<bool>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty", flatten)]
    pub extra: JsonMap,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AdapterTranscriptRole {
    User,
    Assistant,
    Reasoning,
    Command,
    View,
    Search,
    Diff,
    Error,
    TokenUsage,
    ContextCompacted,
    Tool,
    McpTool,
    WebSearch,
    DebugRaw,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ApprovalRequestEvent {
    pub conversation_id: String,
    pub request_id: String,
    pub prompt: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "JsonMap::is_empty")]
    pub metadata: JsonMap,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct TokenUsageEvent {
    pub conversation_id: String,
    pub total: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub output_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cached_input_tokens: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn serializes_live_event_type_for_frontend_contract() {
        let event = AdapterLiveEvent {
            event_type: AdapterLiveEventType::AssistantDelta,
            conversation_id: Some("c1".to_owned()),
            id: Some("a1".to_owned()),
            turn_id: Some("t1".to_owned()),
            subagent_id: None,
            role: None,
            text: None,
            delta: Some("hello".to_owned()),
            message: None,
            path: None,
            line: None,
            column: None,
            timestamp: None,
            extra: JsonMap::new(),
        };
        assert_eq!(
            serde_json::to_value(event).unwrap(),
            json!({
                "type": "assistant_delta",
                "conversation_id": "c1",
                "id": "a1",
                "turn_id": "t1",
                "delta": "hello"
            })
        );
    }

    #[test]
    fn serializes_transcript_role_for_replay_contract() {
        let record = AdapterTranscriptRecord {
            role: AdapterTranscriptRole::Assistant,
            id: Some("a1".to_owned()),
            item_id: None,
            conversation_id: Some("c1".to_owned()),
            turn_id: Some("t1".to_owned()),
            subagent_id: None,
            text: Some("done".to_owned()),
            message: None,
            timestamp: Some("2026-05-04T00:00:00Z".to_owned()),
            path: None,
            line: None,
            column: None,
            internal: None,
            extra: JsonMap::new(),
        };
        assert_eq!(
            serde_json::to_value(record).unwrap(),
            json!({
                "role": "assistant",
                "id": "a1",
                "conversation_id": "c1",
                "turn_id": "t1",
                "text": "done",
                "timestamp": "2026-05-04T00:00:00Z"
            })
        );
    }
}
