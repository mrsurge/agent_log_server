use crate::socketio::register_socket_namespaces;
use crate::state::AppState;
use crate::static_assets;
use als_dto::{APP_ID, HealthResponse, HealthStatus};
use axum::{Json, Router, routing::get};
use socketioxide::SocketIo;
use tower_http::trace::TraceLayer;

pub fn build_router(state: AppState) -> Router {
    let (socket_layer, io) = SocketIo::new_layer();
    register_socket_namespaces(&io);

    Router::new()
        .merge(static_assets::routes())
        .route("/api/health", get(health))
        .with_state(state)
        .layer(socket_layer)
        .layer(TraceLayer::new_for_http())
}

async fn health(
    axum::extract::State(state): axum::extract::State<AppState>,
) -> Json<HealthResponse> {
    Json(HealthResponse {
        status: HealthStatus::Ok,
        app: APP_ID.to_owned(),
        version: env!("CARGO_PKG_VERSION").to_owned(),
        roots: state.config.roots,
    })
}
