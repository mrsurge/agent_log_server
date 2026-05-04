use serde::{Deserialize, Serialize};
use std::path::PathBuf;

pub mod methods {
    pub const EXTENSION_INITIALIZE: &str = "extension.initialize";
    pub const EXTENSION_LIST_MODELS: &str = "extension.list_models";
    pub const CONVERSATION_START: &str = "conversation.start";
    pub const CONVERSATION_RESUME: &str = "conversation.resume";
    pub const CONVERSATION_SEND: &str = "conversation.send";
    pub const CONVERSATION_COMPACT: &str = "conversation.compact";
    pub const APPROVAL_RESPOND: &str = "approval.respond";
}

pub mod events {
    pub const ASSISTANT_DELTA: &str = "event.assistant_delta";
    pub const TRANSCRIPT_RECORD: &str = "event.transcript_record";
    pub const APPROVAL_REQUEST: &str = "event.approval_request";
    pub const TOKEN_USAGE: &str = "event.token_usage";
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ExtensionInitializeParams {
    pub extension_id: String,
    pub cwd: PathBuf,
    pub data_dir: PathBuf,
    pub cache_dir: PathBuf,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ConversationSendParams {
    pub conversation_id: String,
    pub text: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ConversationResumeParams {
    pub conversation_id: String,
    pub provider_session_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ApprovalRespondParams {
    pub conversation_id: String,
    pub request_id: String,
    pub decision: ApprovalDecision,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalDecision {
    Approve,
    Deny,
}
