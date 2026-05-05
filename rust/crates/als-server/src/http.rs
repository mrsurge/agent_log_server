use crate::adapter_routes;
use crate::socketio::register_socket_namespaces;
use crate::state::AppState;
use crate::static_assets;
use crate::{conversation_routes, conversation_rpc};
use als_dto::{APP_ID, HealthResponse, HealthStatus};
use axum::{Json, Router, routing::get};
use socketioxide::SocketIo;
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

pub fn build_router(state: AppState) -> Router {
    let (socket_layer, io) = SocketIo::builder().with_state(state.clone()).build_layer();
    register_socket_namespaces(&io);
    conversation_rpc::start_adapter_event_fanout(io.clone(), state.clone());
    start_extension_warmup(state.clone());

    Router::new()
        .merge(static_assets::routes(&state.config.roots.static_dir))
        .merge(adapter_routes::routes())
        .merge(conversation_routes::routes())
        .route("/api/health", get(health))
        .with_state(state)
        .layer(socket_layer)
        .layer(TraceLayer::new_for_http())
}

fn start_extension_warmup(state: AppState) {
    tokio::spawn(async move {
        let Some(extension_id) = state
            .extensions
            .list()
            .into_iter()
            .find(|entry| entry.active)
            .map(|entry| entry.id)
        else {
            info!("no active ALS-RS extensions to warm up");
            return;
        };
        match state
            .adapter
            .warm_up_extensions(state.extensions.enabled_overrides(), extension_id)
            .await
        {
            Ok(result) => {
                state.extensions.apply_runtime_extensions(&result);
                info!(%result, "ALS-RS extension warmup completed");
            }
            Err(error) => {
                warn!(%error, "ALS-RS extension warmup failed");
            }
        }
    });
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
