use crate::state::AppState;
use als_adapter_protocol::{
    ConversationAckResult, ConversationSendParams, ConversationStartParams,
    ExtensionListModelsResult, JsonMap, methods,
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
    let params = ConversationStartParams {
        extension_id: "copilot-sdk".to_owned(),
        conversation_id,
        cwd: body.cwd.map(Into::into),
        settings: body.settings.unwrap_or_default(),
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
    let params = ConversationSendParams {
        extension_id: "copilot-sdk".to_owned(),
        conversation_id,
        text: body.text,
        thread_id: None,
        provider_session_id: None,
        turn_id: body.turn_id,
        cwd: body.cwd.map(Into::into),
        attachments: Vec::new(),
        toast_context: None,
        settings: body.settings.unwrap_or_default(),
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
