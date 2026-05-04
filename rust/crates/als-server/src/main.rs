mod adapter_process;
mod adapter_routes;
mod config;
mod http;
mod socketio;
mod state;
mod static_assets;

use crate::config::ServerConfig;
use crate::http::build_router;
use crate::state::AppState;
use anyhow::Result;
use tracing::info;
use tracing_subscriber::{EnvFilter, fmt};

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let config = ServerConfig::from_env()?;
    let addr = config.socket_addr()?;
    let app = build_router(AppState::new(config));
    let listener = tokio::net::TcpListener::bind(addr).await?;

    info!(%addr, "starting als-rs server");
    axum::serve(listener, app).await?;
    Ok(())
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt().with_env_filter(filter).init();
}
