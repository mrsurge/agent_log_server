//! Only transport payloads are opaque; service admission uses typed metadata.
use als_dto::APP_ID;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fmt;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct PipeError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<Value>,
}

impl PipeError {
    pub fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
            retryable: false,
            details: None,
        }
    }
}
impl fmt::Display for PipeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}
impl std::error::Error for PipeError {}
pub type PipeResult = Result<Value, PipeError>;

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Hello,
    Request,
    Response,
    Notification,
    Progress,
    Cancel,
    Error,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Envelope {
    pub jsonrpc: String,
    pub protocol_version: u16,
    pub kind: Kind,
    pub origin_nid: u32,
    pub origin_name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_nid: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub op_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub project_generation: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workspace_root: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub method: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<PipeError>,
}

impl Envelope {
    pub fn outbound(kind: Kind) -> Self {
        Self {
            jsonrpc: "2.0".into(),
            protocol_version: 1,
            kind,
            origin_nid: 0,
            origin_name: APP_ID.into(),
            target_nid: Some(1),
            target_name: Some("framework.rust".into()),
            id: None,
            correlation_id: None,
            op_id: None,
            project_generation: None,
            workspace_root: None,
            method: None,
            params: None,
            result: None,
            error: None,
        }
    }

    pub fn validate(&self) -> Result<(), PipeError> {
        if self.jsonrpc != "2.0" || self.protocol_version != 1 {
            return Err(PipeError::new(
                "protocol.version",
                "Invalid framework pipe version",
            ));
        }
        if self.origin_nid != 1 || self.origin_name != "framework.rust" {
            return Err(PipeError::new(
                "protocol.origin",
                "Untrusted framework origin",
            ));
        }
        if self.target_nid.is_some_and(|nid| nid != 0)
            || self
                .target_name
                .as_deref()
                .is_some_and(|name| name != APP_ID)
        {
            return Err(PipeError::new("protocol.target", "Wrong ALS pipe target"));
        }
        if self.kind == Kind::Request
            && (self.id.as_deref().is_none_or(str::is_empty)
                || self.method.as_deref().is_none_or(str::is_empty))
        {
            return Err(PipeError::new(
                "protocol.request",
                "Missing request ID/method",
            ));
        }
        if self.id.as_ref().is_some_and(|id| id.len() > 512) {
            return Err(PipeError::new(
                "protocol.id",
                "Request/response ID exceeds limit",
            ));
        }
        if matches!(self.kind, Kind::Response | Kind::Error)
            && (self.target_nid != Some(0) || self.target_name.as_deref() != Some(APP_ID))
        {
            return Err(PipeError::new(
                "protocol.target",
                "Response lacks exact ALS target",
            ));
        }
        Ok(())
    }

    pub fn reply(&self, result: PipeResult) -> Self {
        let mut reply = Self::outbound(if result.is_ok() {
            Kind::Response
        } else {
            Kind::Error
        });
        reply.id = self.id.clone();
        reply.correlation_id = self.correlation_id.clone().or_else(|| self.id.clone());
        reply.op_id = self.op_id.clone();
        reply.project_generation = self.project_generation;
        reply.workspace_root = self.workspace_root.clone();
        match result {
            Ok(value) => reply.result = Some(value),
            Err(error) => reply.error = Some(error),
        }
        reply
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AppCall {
    pub target_app: String,
    pub service: String,
    pub version: u32,
    pub method: String,
    pub context: Value,
    pub payload: Value,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Source {
    pub app_id: String,
    pub shell_id: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServiceParams {
    pub service: String,
    pub version: u32,
    pub method: String,
    pub context: Value,
    pub payload: Value,
    pub source: Source,
}

#[derive(Clone, Debug)]
pub struct ServiceRequest {
    pub request_id: String,
    pub operation_id: String,
    pub project_generation: Option<u64>,
    pub workspace_root: Option<String>,
    pub params: ServiceParams,
}

pub fn address(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._:-".contains(&c))
}
