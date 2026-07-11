use crate::agent_log;
use crate::sidebar_ipc;
use crate::socketio::register_socket_namespaces;
use crate::state::AppState;
use crate::static_assets;
use crate::{conversation_routes, conversation_rpc, extension_routes};
use als_dto::{APP_ID, HealthResponse, HealthStatus};
use axum::{Json, Router, routing::get};
use socketioxide::{ParserConfig, SocketIo};
use tower_http::trace::TraceLayer;
use tracing::{info, warn};

pub fn build_router(state: AppState) -> Router {
    let socket_builder = SocketIo::builder().with_state(state.clone());
    let socket_builder = match state.config.socketio_serializer {
        crate::config::SocketIoSerializer::Json => socket_builder,
        crate::config::SocketIoSerializer::Msgpack => {
            socket_builder.with_parser(ParserConfig::msgpack())
        }
    };
    let (socket_layer, io) = socket_builder.build_layer();
    register_socket_namespaces(&io);
    conversation_rpc::start_adapter_event_fanout(io.clone(), state.clone());
    agent_log::start_socketio_fanout(io.clone(), state.clone());
    start_extension_warmup(state.clone());
    start_sidebar_cwd_fetch(io.clone(), state.clone());

    Router::new()
        .merge(static_assets::routes(&state.config.roots.static_dir))
        .merge(agent_log::routes())
        .merge(conversation_routes::routes())
        .merge(extension_routes::routes())
        .route("/api/health", get(health))
        .with_state(state)
        .layer(socket_layer)
        .layer(TraceLayer::new_for_http())
}

fn start_sidebar_cwd_fetch(io: SocketIo, state: AppState) {
    tokio::spawn(async move {
        let result = sidebar_ipc::recheck_status(&io, &state).await;
        let project_root = state
            .host_ui
            .snapshot()
            .ok()
            .and_then(|snapshot| snapshot.project_root);
        info!(
            %result,
            project_root = project_root.as_deref().unwrap_or(""),
            "ALS-RS sidebar IPC startup CWD fetch completed"
        );
    });
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
