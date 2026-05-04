use als_dto::{APP_ID, DEFAULT_HOST, DEFAULT_PORT, RuntimeRoots};
use anyhow::{Context, Result};
use std::env;
use std::fs;
use std::net::SocketAddr;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub roots: RuntimeRoots,
}

impl ServerConfig {
    pub fn from_env() -> Result<Self> {
        let host = env::var("ALS_RS_HOST").unwrap_or_else(|_| DEFAULT_HOST.to_owned());
        let port = match env::var("ALS_RS_PORT") {
            Ok(raw) => raw
                .parse::<u16>()
                .with_context(|| format!("invalid ALS_RS_PORT value: {raw}"))?,
            Err(env::VarError::NotPresent) => DEFAULT_PORT,
            Err(err) => return Err(err).context("invalid ALS_RS_PORT environment value"),
        };

        let config = Self {
            host,
            port,
            roots: RuntimeRoots {
                data_dir: env_path_or_default("ALS_RS_DATA_DIR", default_data_dir),
                cache_dir: env_path_or_default("ALS_RS_CACHE_DIR", default_cache_dir),
                config_dir: env_path_or_default("ALS_RS_CONFIG_DIR", default_data_dir),
                static_dir: env_path_or_default("ALS_RS_STATIC_DIR", default_static_dir),
            },
        };
        config.ensure_roots()?;
        Ok(config)
    }

    pub fn socket_addr(&self) -> Result<SocketAddr> {
        format!("{}:{}", self.host, self.port)
            .parse()
            .with_context(|| format!("invalid ALS-RS bind address {}:{}", self.host, self.port))
    }

    fn ensure_roots(&self) -> Result<()> {
        for root in [
            &self.roots.data_dir,
            &self.roots.cache_dir,
            &self.roots.config_dir,
        ] {
            fs::create_dir_all(root)
                .with_context(|| format!("failed to create ALS-RS root {}", root.display()))?;
        }
        Ok(())
    }
}

fn env_path_or_default(key: &str, default_builder: impl FnOnce() -> PathBuf) -> PathBuf {
    env::var_os(key)
        .map(PathBuf::from)
        .unwrap_or_else(default_builder)
}

fn default_data_dir() -> PathBuf {
    env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(default_home_local_share)
        .join(APP_ID)
}

fn default_cache_dir() -> PathBuf {
    env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(default_home_cache)
        .join(APP_ID)
}

fn default_static_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("agent_log_server")
        .join("static")
}

fn default_home_local_share() -> PathBuf {
    home_dir().join(".local").join("share")
}

fn default_home_cache() -> PathBuf {
    home_dir().join(".cache")
}

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}
