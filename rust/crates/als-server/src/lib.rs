mod adapter_codec;
mod adapter_process;
mod agent_edits;
mod agent_log;
mod card_truncation;
mod composer_sync;
mod config;
mod conversation_routes;
mod conversation_rpc;
mod conversation_store;
mod devins_context;
#[cfg(feature = "embedded-python-adapter")]
mod embedded_adapter;
mod extension_registry;
mod extension_routes;
mod framework_pipe;
mod http;
mod inline_agent_edits;
mod ipc;
mod project_git;
mod project_summary;
mod reverse_patch;
mod settings_rpc;
mod shell_output;
mod sidebar_ipc;
mod socketio;
mod state;
mod static_assets;
mod transcript_card_projection;
mod transcript_stream;
mod turn_projection;
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

pub async fn run() -> Result<()> {
    init_tracing();

    let mut args = std::env::args_os().skip(1).collect();
    let stdio = framework_pipe::take_stdio_flag(&mut args);
    let config = ServerConfig::from_env_and_args(args)?;
    let mut pipe = if stdio {
        Some(framework_pipe::FrameworkPipe::start()?)
    } else {
        None
    };
    let addr = config.socket_addr()?;
    let state = AppState::new(config);
    let app = build_router(state.clone());
    let listener = tokio::net::TcpListener::bind(addr).await?;
    let shutdown_notify = Arc::new(Notify::new());
    let server_shutdown = shutdown_notify.clone();

    info!(%addr, "starting als-rs server");
    let mut server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                server_shutdown.notified().await;
            })
            .await
    });
    let readiness = match pipe.as_mut() {
        Some(pipe) => pipe.serving_ready(),
        None => Ok(()),
    };

    let mut server_finished = false;
    let result = if let Err(error) = readiness {
        Err(error)
    } else {
        tokio::select! {
            _ = wait_for_shutdown_signal() => Ok(()),
            closed = async {
                match pipe.as_mut() {
                    Some(pipe) => pipe.closed().await,
                    None => std::future::pending().await,
                }
            } => closed,
            finished = &mut server => {
                server_finished = true;
                match finished {
                    Ok(Err(error)) => Err(error.into()),
                    Err(error) => Err(error.into()),
                    Ok(Ok(())) => Err(anyhow::anyhow!("ALS HTTP server stopped unexpectedly")),
                }
            },
        }
    };
    info!("ALS-RS shutdown requested");
    match timeout(Duration::from_secs(15), state.adapter.shutdown()).await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => warn!(%error, "ALS-RS adapter shutdown failed"),
        Err(_) => warn!("ALS-RS adapter shutdown timed out"),
    }
    shutdown_notify.notify_one();
    if !server_finished {
        match timeout(Duration::from_secs(10), server).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => return Err(error.into()),
            Ok(Err(error)) => warn!(%error, "ALS-RS server task failed during shutdown"),
            Err(_) => warn!("ALS-RS HTTP server graceful shutdown timed out"),
        }
    }
    result
}

fn init_tracing() {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .init();
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
