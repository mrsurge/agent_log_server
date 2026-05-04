use crate::{
    conversation_store::{ConversationMeta, ConversationSummary, CreateConversationRequest},
    state::AppState,
};
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::get,
};
use serde_json::{Value, json};

pub fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/api/conversations",
            get(list_conversations).post(create_conversation),
        )
        .route("/api/conversations/{conversation_id}/meta", get(load_meta))
        .route(
            "/api/conversations/{conversation_id}/transcript",
            get(read_transcript).post(append_transcript),
        )
}

async fn create_conversation(
    State(state): State<AppState>,
    Json(body): Json<CreateConversationRequest>,
) -> Result<Json<ConversationMeta>, StoreRouteError> {
    Ok(Json(state.conversations.create(body)?))
}

async fn list_conversations(
    State(state): State<AppState>,
) -> Result<Json<Vec<ConversationSummary>>, StoreRouteError> {
    Ok(Json(state.conversations.list()?))
}

async fn load_meta(
    State(state): State<AppState>,
    Path(conversation_id): Path<String>,
) -> Result<Json<ConversationMeta>, StoreRouteError> {
    Ok(Json(state.conversations.load_meta(&conversation_id)?))
}

async fn append_transcript(
    State(state): State<AppState>,
    Path(conversation_id): Path<String>,
    Json(entry): Json<Value>,
) -> Result<Json<Value>, StoreRouteError> {
    Ok(Json(
        state
            .conversations
            .append_transcript(&conversation_id, entry)?,
    ))
}

async fn read_transcript(
    State(state): State<AppState>,
    Path(conversation_id): Path<String>,
) -> Result<Json<Vec<Value>>, StoreRouteError> {
    Ok(Json(state.conversations.read_transcript(&conversation_id)?))
}

struct StoreRouteError(anyhow::Error);

impl<E> From<E> for StoreRouteError
where
    E: Into<anyhow::Error>,
{
    fn from(error: E) -> Self {
        Self(error.into())
    }
}

impl IntoResponse for StoreRouteError {
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
