use crate::state::AppState;
use axum::{
    Json, Router,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
};
use serde_json::{Value, json};

pub fn routes() -> Router<AppState> {
    Router::new().route(
        "/api/extensions/{extension_id}/reload",
        post(reload_extension),
    )
}

async fn reload_extension(
    State(state): State<AppState>,
    Path(extension_id): Path<String>,
) -> Result<Json<Value>, ExtensionRouteError> {
    let extension_id = extension_id.trim().to_owned();
    if extension_id.is_empty() {
        return Err(ExtensionRouteError::bad_request(
            "extension_id is required".to_owned(),
        ));
    }

    state.extensions.reload()?;
    if state.extensions.get(&extension_id).is_none() {
        return Err(ExtensionRouteError::not_found(format!(
            "Extension not found: {extension_id}"
        )));
    }

    let adapter = state
        .adapter
        .reload_extensions_if_running(
            state.extensions.enabled_overrides(),
            Some(vec![extension_id.clone()]),
            Some(extension_id.clone()),
        )
        .await?;
    if let Some(adapter_result) = adapter.as_ref() {
        state.extensions.apply_runtime_extensions(adapter_result);
    }

    Ok(Json(json!({
        "ok": true,
        "extension_id": extension_id,
        "extension": state.extensions.get(&extension_id),
        "extensions": state.extensions.list(),
        "adapter": adapter,
        "transport": "http",
    })))
}

struct ExtensionRouteError {
    status: StatusCode,
    message: String,
}

impl ExtensionRouteError {
    fn bad_request(message: String) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message,
        }
    }

    fn not_found(message: String) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            message,
        }
    }
}

impl<E> From<E> for ExtensionRouteError
where
    E: Into<anyhow::Error>,
{
    fn from(error: E) -> Self {
        let error = error.into();
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            message: error.to_string(),
        }
    }
}

impl IntoResponse for ExtensionRouteError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({
                "ok": false,
                "error": self.message,
            })),
        )
            .into_response()
    }
}
