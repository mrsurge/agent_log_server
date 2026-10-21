//! Explicit framework mode shares semantic owners with standalone integration.
use super::*;
use crate::framework_pipe::{PipeError, PipeResult, ServiceRequest};
use serde::Deserialize;

#[derive(Deserialize)]
struct Mention {
    path: String,
    conversation_id: String,
    #[serde(rename = "conversationId")]
    conversation_alias: Option<String>,
    #[serde(rename = "lineNo")]
    line_no: Option<u64>,
    #[serde(rename = "endLineNo")]
    end_line_no: Option<u64>,
    col: Option<u64>,
    #[serde(rename = "endCol")]
    end_col: Option<u64>,
    content: Option<String>,
}

impl Mention {
    fn validate(&self) -> Result<()> {
        if self.path.trim().is_empty()
            || self.path.contains('`')
            || self.conversation_id.trim().is_empty()
            || self
                .conversation_alias
                .as_ref()
                .is_some_and(|id| id != &self.conversation_id)
            || [self.line_no, self.end_line_no, self.col, self.end_col]
                .into_iter()
                .flatten()
                .any(|n| n == 0 || n > i64::MAX as u64)
        {
            bail!("Invalid explicit-conversation mention");
        }
        // Content formatting/truncation belongs to process_mention, not transport.
        let _ = &self.content;
        Ok(())
    }
}

#[derive(Deserialize)]
struct CwdNotice {
    cwd: String,
}

#[derive(Deserialize)]
struct ProjectNotice {
    resolved_path: Option<String>,
    path: Option<String>,
}

#[derive(Deserialize)]
struct FocusNotice {
    app_id: String,
    client_id: String,
    host_id: String,
    focused: bool,
}

#[derive(Deserialize)]
struct DocumentRequest {
    uri: String,
}

#[derive(Deserialize)]
struct DecisionRequest {
    decision: String,
}

pub fn bind_framework_pipe(io: &SocketIo, state: &AppState) -> Result<()> {
    let Some(pipe) = state.framework_pipe.as_ref() else {
        return Ok(());
    };
    let io = io.clone();
    let state = state.clone();
    pipe.bind_handler_with_deadline(
        Arc::new(move |request| {
            let io = io.clone();
            let state = state.clone();
            Box::pin(async move { dispatch(&io, &state, request).await })
        }),
        Duration::from_secs(60),
    )?;
    Ok(())
}

fn admit(request: &ServiceRequest) -> Result<()> {
    let params = &request.params;
    if params.source.app_id != "code_te2"
        || params.source.shell_id.is_empty()
        || params.service != "sidebar"
        || params.version != 1
        || !params.context.is_null()
    {
        bail!("Untrusted or unsupported ALS Sidebar service");
    }
    Ok(())
}

async fn dispatch(io: &SocketIo, state: &AppState, request: ServiceRequest) -> PipeResult {
    dispatch_inner(io, state, request)
        .await
        .map_err(|error| PipeError::new("sidebar.rejected", &error.to_string()))
}

async fn dispatch_inner(io: &SocketIo, state: &AppState, request: ServiceRequest) -> Result<Value> {
    admit(&request)?;
    let params = request.params;
    match params.method.as_str() {
        "mention" => {
            let mention: Mention = serde_json::from_value(params.payload.clone())?;
            mention.validate()?;
            let payload: JsonMap = serde_json::from_value(params.payload)?;
            process_mention(io, state, payload).await
        }
        "cwd.set" => {
            let notice: CwdNotice = serde_json::from_value(params.payload)?;
            if notice.cwd.trim().is_empty() {
                bail!("Missing project directory");
            }
            update_project_root(io, state, notice.cwd).await?;
            Ok(json!({"ok":true}))
        }
        "project.opened" => {
            let notice: ProjectNotice = serde_json::from_value(params.payload)?;
            let path = notice
                .resolved_path
                .or(notice.path)
                .filter(|path| !path.trim().is_empty())
                .ok_or_else(|| anyhow!("Missing opened project path"))?;
            update_project_root(io, state, path).await?;
            Ok(json!({"ok":true}))
        }
        "window.focused" => {
            let notice: FocusNotice = serde_json::from_value(params.payload.clone())?;
            if !matches!(notice.app_id.as_str(), "als" | "als-rs" | "als_rs")
                || notice.client_id.trim().is_empty()
                || notice.host_id.trim().is_empty()
                || !notice.focused
            {
                bail!("Invalid ALS focus projection");
            }
            let payload: JsonMap = serde_json::from_value(params.payload)?;
            // The existing focus owner validates slot/conversation projection;
            // mentions never use this as a substitute for explicit conversation.
            process_focused_window(state, payload)?;
            Ok(json!({"ok":true}))
        }
        "agentEdits.documentState.get" => {
            let request: DocumentRequest = serde_json::from_value(params.payload.clone())?;
            if request.uri.trim().is_empty() {
                bail!("Missing agent document URI");
            }
            let payload: JsonMap = serde_json::from_value(params.payload)?;
            state.inline_agent_edits.document_state(&payload)
        }
        "agentEdits.decide" => {
            let request: DecisionRequest = serde_json::from_value(params.payload.clone())?;
            if !matches!(
                request.decision.as_str(),
                "accept" | "accepted" | "reject" | "rejected"
            ) {
                bail!("Unsupported agent edit decision");
            }
            let payload: JsonMap = serde_json::from_value(params.payload)?;
            publish_inline_agent_decision(io, state, payload).await
        }
        _ => bail!("Unsupported ALS Sidebar operation"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mention_requires_explicit_consistent_conversation_and_valid_coordinates() {
        assert!(serde_json::from_value::<Mention>(json!({"path":"/file"})).is_err());
        for value in [
            json!({"path":"/file","conversation_id":"conv","conversationId":"other"}),
            json!({"path":"/file","conversation_id":""}),
            json!({"path":"/file","conversation_id":"conv","lineNo":0}),
        ] {
            assert!(
                serde_json::from_value::<Mention>(value)
                    .unwrap()
                    .validate()
                    .is_err()
            );
        }
        assert!(
            serde_json::from_value::<Mention>(
                json!({"path":"/file","conversation_id":"conv", "lineNo":2})
            )
            .unwrap()
            .validate()
            .is_ok()
        );
    }
}
