use crate::state::AppState;
use als_adapter_protocol::{
    ConversationAckResult, ConversationSendParams, ConversationStartParams,
    ExtensionListModelsResult, JsonMap, McpContext, methods,
};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Deserialize;
use serde_json::{Value, json};

const DEFAULT_TE2_BASE_URL: &str = "http://127.0.0.1:8089";

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/api/adapter/copilot/initialize", post(initialize_copilot))
        .route("/api/adapter/copilot/models", get(list_copilot_models))
        .route(
            "/api/adapter/copilot/conversations/{conversation_id}/start",
            post(start_copilot_conversation),
        )
        .route(
            "/api/adapter/copilot/conversations/{conversation_id}/send",
            post(send_copilot_message),
        )
        .route("/api/adapter/copilot/events", get(adapter_events))
}

async fn initialize_copilot(
    State(state): State<AppState>,
) -> Result<Json<Value>, AdapterRouteError> {
    Ok(Json(state.adapter.initialize_copilot().await?))
}

async fn list_copilot_models(
    State(state): State<AppState>,
) -> Result<Json<ExtensionListModelsResult>, AdapterRouteError> {
    state.adapter.initialize_copilot().await?;
    let result = state
        .adapter
        .client()
        .await?
        .request(methods::EXTENSION_LIST_MODELS, JsonMap::new())
        .await?;
    Ok(Json(result))
}

async fn start_copilot_conversation(
    State(state): State<AppState>,
    Path(conversation_id): Path<String>,
    Json(body): Json<StartConversationBody>,
) -> Result<Json<ConversationAckResult>, AdapterRouteError> {
    state.adapter.initialize_copilot().await?;
    let settings = body.settings.unwrap_or_default();
    let params = ConversationStartParams {
        extension_id: "copilot-sdk".to_owned(),
        conversation_id: conversation_id.clone(),
        cwd: body.cwd.clone().map(Into::into),
        mcp_context: Some(adapter_mcp_context(
            &state.config,
            &conversation_id,
            Some(&settings),
            body.cwd.as_deref(),
        )),
        devins_context: Some(crate::devins_context::build_devins_context(
            Some(&settings),
            body.cwd.as_deref(),
        )?),
        settings,
    };
    let result = state
        .adapter
        .client()
        .await?
        .request(methods::CONVERSATION_START, params)
        .await?;
    Ok(Json(result))
}

async fn send_copilot_message(
    State(state): State<AppState>,
    Path(conversation_id): Path<String>,
    Json(body): Json<SendMessageBody>,
) -> Result<Json<ConversationAckResult>, AdapterRouteError> {
    state.adapter.initialize_copilot().await?;
    let settings = body.settings.unwrap_or_default();
    let params = ConversationSendParams {
        extension_id: "copilot-sdk".to_owned(),
        conversation_id: conversation_id.clone(),
        text: body.text,
        thread_id: None,
        provider_session_id: None,
        turn_id: body.turn_id,
        cwd: body.cwd.clone().map(Into::into),
        attachments: Vec::new(),
        toast_context: None,
        mcp_context: Some(adapter_mcp_context(
            &state.config,
            &conversation_id,
            Some(&settings),
            body.cwd.as_deref(),
        )),
        devins_context: Some(crate::devins_context::build_devins_context(
            Some(&settings),
            body.cwd.as_deref(),
        )?),
        settings,
    };
    let result = state
        .adapter
        .client()
        .await?
        .request(methods::CONVERSATION_SEND, params)
        .await?;
    Ok(Json(result))
}

async fn adapter_events(
    State(state): State<AppState>,
) -> Json<crate::adapter_process::AdapterEventSnapshot> {
    Json(state.adapter.events().snapshot().await)
}

#[derive(Debug, Deserialize)]
struct StartConversationBody {
    cwd: Option<String>,
    #[serde(default)]
    settings: Option<JsonMap>,
}

#[derive(Debug, Deserialize)]
struct SendMessageBody {
    text: String,
    turn_id: Option<String>,
    cwd: Option<String>,
    #[serde(default)]
    settings: Option<JsonMap>,
}

fn adapter_mcp_context(
    config: &crate::config::ServerConfig,
    conversation_id: &str,
    settings: Option<&JsonMap>,
    cwd: Option<&str>,
) -> McpContext {
    let mut requested_servers = JsonMap::new();
    if let Some(existing_servers) = settings
        .and_then(|value| value.get("mcp_servers"))
        .and_then(Value::as_object)
    {
        requested_servers.extend(existing_servers.clone());
    }

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
    if let Some(path) = cwd.and_then(|value| {
        let trimmed = value.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_owned())
    }) {
        agent_pty_defaults.insert("cwd".to_owned(), Value::String(path));
    }
    defaults.insert(
        "agent-pty-blocks".to_owned(),
        Value::Object(agent_pty_defaults),
    );

    let te2_enabled = settings
        .and_then(|value| value.get("te2_mcp_integration"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if te2_enabled {
        let mut te2_defaults = JsonMap::new();
        te2_defaults.insert("enabled_by_default".to_owned(), Value::Bool(true));
        te2_defaults.insert("transport".to_owned(), Value::String("http".to_owned()));
        te2_defaults.insert(
            "base_url".to_owned(),
            Value::String(te2_base_url_from_settings(settings)),
        );
        defaults.insert("te2-mcp".to_owned(), Value::Object(te2_defaults));
    }

    McpContext {
        conversation_id: conversation_id.to_owned(),
        cwd: cwd
            .and_then(|value| {
                let trimmed = value.trim();
                (!trimmed.is_empty()).then(|| trimmed.to_owned())
            })
            .map(Into::into),
        requested_servers,
        defaults,
    }
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
        .and_then(|value| value.get("te2_base_url"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .unwrap_or_else(|| DEFAULT_TE2_BASE_URL.to_owned())
}

struct AdapterRouteError(anyhow::Error);

impl<E> From<E> for AdapterRouteError
where
    E: Into<anyhow::Error>,
{
    fn from(error: E) -> Self {
        Self(error.into())
    }
}

impl IntoResponse for AdapterRouteError {
    fn into_response(self) -> Response {
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({
                "ok": false,
                "error": self.0.to_string(),
            })),
        )
            .into_response()
    }
}
