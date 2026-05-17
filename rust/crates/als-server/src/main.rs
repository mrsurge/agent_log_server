mod adapter_process;
mod agent_log;
mod config;
mod conversation_routes;
mod conversation_rpc;
mod conversation_store;
mod devins_context;
mod extension_registry;
mod extension_routes;
mod http;
mod ipc;
mod settings_rpc;
mod sidebar_ipc;
mod socketio;
mod state;
mod static_assets;
mod ui_rpc;

use crate::config::ServerConfig;
use crate::http::build_router;
use crate::state::AppState;
use anyhow::Result;
use std::sync::Arc;
use tokio::{
    sync::Notify,
    time::{Duration, timeout},
};
use tracing::{info, warn};
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let config = ServerConfig::from_env()?;
    let addr = config.socket_addr()?;
    let state = AppState::new(config);
    let app = build_router(state.clone());
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let shutdown_notify = Arc::new(Notify::new());
    let server_shutdown = shutdown_notify.clone();

    info!(%addr, "starting als-rs server");
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                server_shutdown.notified().await;
            })
            .await
    });

    wait_for_shutdown_signal().await;
    info!("ALS-RS shutdown signal received");
    match timeout(Duration::from_secs(15), state.adapter.shutdown()).await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => warn!(%error, "ALS-RS adapter shutdown failed"),
        Err(_) => warn!("ALS-RS adapter shutdown timed out"),
    }
    shutdown_notify.notify_waiters();
    match timeout(Duration::from_secs(10), server).await {
        Ok(Ok(Ok(()))) => {}
        Ok(Ok(Err(error))) => return Err(error.into()),
        Ok(Err(error)) => warn!(%error, "ALS-RS server task failed during shutdown"),
        Err(_) => warn!("ALS-RS HTTP server graceful shutdown timed out"),
    }
    Ok(())
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).init();
}

async fn wait_for_shutdown_signal() {
    let ctrl_c = async {
        if let Err(error) = tokio::signal::ctrl_c().await {
            warn!(%error, "failed to install Ctrl-C handler");
        }
    };

    #[cfg(unix)]
    {
        let terminate = async {
            match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
                Ok(mut signal) => {
                    signal.recv().await;
                }
                Err(error) => warn!(%error, "failed to install SIGTERM handler"),
            }
        };
        tokio::select! {
            _ = ctrl_c => {}
            _ = terminate => {}
        }
    }

    #[cfg(not(unix))]
    {
        ctrl_c.await;
    }
}
