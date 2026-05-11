use als_dto::{APP_ID, DEFAULT_HOST, DEFAULT_PORT, RuntimeRoots};
use anyhow::{Context, Result, anyhow, bail};
use std::env;
use std::ffi::OsString;
use std::fs;
use std::net::SocketAddr;
use std::path::PathBuf;

#[derive(Clone, Debug)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub extensions_dir: PathBuf,
    pub roots: RuntimeRoots,
    pub adapters: AdapterConfig,
    pub framework_shells: FrameworkShellConfig,
}

#[derive(Clone, Debug)]
pub struct AdapterConfig {
    pub copilot_python: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct FrameworkShellConfig {
    pub base_dir: Option<String>,
    pub secret: Option<String>,
    pub repo_fingerprint: Option<String>,
    pub secret_fingerprint: Option<String>,
    pub fws_socketio_server_pid: Option<String>,
    pub run_id: Option<String>,
}

impl ServerConfig {
    pub fn from_env() -> Result<Self> {
        Self::from_env_and_args(env::args_os().skip(1))
    }

    pub fn from_env_and_args(args: impl IntoIterator<Item = OsString>) -> Result<Self> {
        let host = env::var("ALS_RS_HOST").unwrap_or_else(|_| DEFAULT_HOST.to_owned());
        let port = match env::var("ALS_RS_PORT") {
            Ok(raw) => raw
                .parse::<u16>()
                .with_context(|| format!("invalid ALS_RS_PORT value: {raw}"))?,
            Err(env::VarError::NotPresent) => DEFAULT_PORT,
            Err(err) => return Err(err).context("invalid ALS_RS_PORT environment value"),
        };

        let mut framework_shells = FrameworkShellConfig::from_env();
        framework_shells.apply_args(args)?;

        let config = Self {
            host,
            port,
            extensions_dir: env_path_or_default("ALS_RS_EXTENSIONS_DIR", default_extensions_dir),
            roots: RuntimeRoots {
                data_dir: env_path_or_default("ALS_RS_DATA_DIR", default_data_dir),
                cache_dir: env_path_or_default("ALS_RS_CACHE_DIR", default_cache_dir),
                config_dir: env_path_or_default("ALS_RS_CONFIG_DIR", default_data_dir),
                static_dir: env_path_or_default("ALS_RS_STATIC_DIR", default_static_dir),
            },
            adapters: AdapterConfig {
                copilot_python: env::var("ALS_RS_PYTHON_BIN")
                    .unwrap_or_else(|_| "python".to_owned()),
            },
            framework_shells,
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
        let user_extensions_dir = self.user_extensions_dir();
        for root in [
            &self.roots.data_dir,
            &self.roots.cache_dir,
            &self.roots.config_dir,
            &user_extensions_dir,
        ] {
            fs::create_dir_all(root)
                .with_context(|| format!("failed to create ALS-RS root {}", root.display()))?;
        }
        Ok(())
    }

    pub fn user_extensions_dir(&self) -> PathBuf {
        self.roots.data_dir.join("extensions")
    }

    pub fn extension_roots(&self) -> Vec<PathBuf> {
        let mut roots = vec![self.extensions_dir.clone()];
        let user_root = self.user_extensions_dir();
        if user_root != self.extensions_dir {
            roots.push(user_root);
        }
        roots
    }
}

impl FrameworkShellConfig {
    fn from_env() -> Self {
        Self {
            base_dir: env::var("FRAMEWORK_SHELLS_BASE_DIR").ok(),
            secret: env::var("FRAMEWORK_SHELLS_SECRET").ok(),
            repo_fingerprint: env::var("FRAMEWORK_SHELLS_REPO_FINGERPRINT").ok(),
            secret_fingerprint: env::var("FRAMEWORK_SHELLS_SECRET_FINGERPRINT").ok(),
            fws_socketio_server_pid: env::var("FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID").ok(),
            run_id: env::var("FRAMEWORK_SHELLS_RUN_ID").ok(),
        }
    }

    fn apply_args(&mut self, args: impl IntoIterator<Item = OsString>) -> Result<()> {
        let mut iter = args.into_iter();
        while let Some(raw) = iter.next() {
            let arg = os_string_to_string(raw)?;
            let (flag, inline_value) = match arg.split_once('=') {
                Some((flag, value)) => (flag.to_owned(), Some(value.to_owned())),
                None => (arg, None),
            };
            let target = match flag.as_str() {
                "--framework-shells-base-dir" => &mut self.base_dir,
                "--framework-shells-secret" => &mut self.secret,
                "--framework-shells-repo-fingerprint" => &mut self.repo_fingerprint,
                "--framework-shells-secret-fingerprint" => &mut self.secret_fingerprint,
                "--framework-shells-fws-socketio-server-pid" => &mut self.fws_socketio_server_pid,
                "--framework-shells-run-id" => &mut self.run_id,
                unknown => bail!("unknown ALS-RS argument: {unknown}"),
            };
            let value = match inline_value {
                Some(value) => value,
                None => {
                    let Some(raw_value) = iter.next() else {
                        bail!("missing value for ALS-RS argument: {flag}");
                    };
                    os_string_to_string(raw_value)?
                }
            };
            *target = Some(value);
        }
        Ok(())
    }

    pub fn env_overrides(&self) -> Vec<(&'static str, String)> {
        let mut values = Vec::new();
        push_env(&mut values, "FRAMEWORK_SHELLS_BASE_DIR", &self.base_dir);
        push_env(&mut values, "FRAMEWORK_SHELLS_SECRET", &self.secret);
        push_env(
            &mut values,
            "FRAMEWORK_SHELLS_REPO_FINGERPRINT",
            &self.repo_fingerprint,
        );
        push_env(
            &mut values,
            "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
            &self.fws_socketio_server_pid,
        );
        push_env(&mut values, "FRAMEWORK_SHELLS_RUN_ID", &self.run_id);
        if let Some(value) = self
            .secret_fingerprint
            .as_ref()
            .or(self.repo_fingerprint.as_ref())
        {
            values.push(("FRAMEWORK_SHELLS_SECRET_FINGERPRINT", value.clone()));
        }
        values
    }

    pub fn is_configured(&self) -> bool {
        self.base_dir.is_some()
            || self.secret.is_some()
            || self.repo_fingerprint.is_some()
            || self.secret_fingerprint.is_some()
            || self.fws_socketio_server_pid.is_some()
            || self.run_id.is_some()
    }
}

fn push_env(values: &mut Vec<(&'static str, String)>, key: &'static str, value: &Option<String>) {
    if let Some(value) = value {
        values.push((key, value.clone()));
    }
}

fn os_string_to_string(value: OsString) -> Result<String> {
    value
        .into_string()
        .map_err(|_| anyhow!("ALS-RS arguments must be valid UTF-8"))
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
    default_repo_root()
        .join("rust")
        .join("crates")
        .join("als-server")
        .join("src")
        .join("static")
}

fn default_extensions_dir() -> PathBuf {
    default_repo_root().join("extensions")
}

fn default_repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn framework_shell_args_override_env_config() {
        let mut config = FrameworkShellConfig {
            base_dir: Some("/env/base".to_owned()),
            secret: Some("env-secret".to_owned()),
            repo_fingerprint: Some("env-repo".to_owned()),
            secret_fingerprint: None,
            fws_socketio_server_pid: None,
            run_id: None,
        };

        config
            .apply_args([
                OsString::from("--framework-shells-base-dir"),
                OsString::from("/arg/base"),
                OsString::from("--framework-shells-secret=arg-secret"),
                OsString::from("--framework-shells-repo-fingerprint"),
                OsString::from("arg-repo"),
                OsString::from("--framework-shells-fws-socketio-server-pid"),
                OsString::from("123"),
                OsString::from("--framework-shells-run-id"),
                OsString::from("app-server"),
            ])
            .unwrap();

        assert_eq!(config.base_dir.as_deref(), Some("/arg/base"));
        assert_eq!(config.secret.as_deref(), Some("arg-secret"));
        assert_eq!(config.repo_fingerprint.as_deref(), Some("arg-repo"));
        assert_eq!(config.fws_socketio_server_pid.as_deref(), Some("123"));
        assert_eq!(config.run_id.as_deref(), Some("app-server"));
    }

    #[test]
    fn framework_shell_env_overrides_map_repo_to_secret_fingerprint() {
        let config = FrameworkShellConfig {
            base_dir: Some("/example/framework_shells".to_owned()),
            secret: Some("secret".to_owned()),
            repo_fingerprint: Some("repo-fingerprint".to_owned()),
            secret_fingerprint: None,
            fws_socketio_server_pid: Some("123".to_owned()),
            run_id: Some("app-server".to_owned()),
        };

        assert_eq!(
            config.env_overrides(),
            vec![
                (
                    "FRAMEWORK_SHELLS_BASE_DIR",
                    "/example/framework_shells".to_owned()
                ),
                ("FRAMEWORK_SHELLS_SECRET", "secret".to_owned()),
                (
                    "FRAMEWORK_SHELLS_REPO_FINGERPRINT",
                    "repo-fingerprint".to_owned()
                ),
                ("FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID", "123".to_owned()),
                ("FRAMEWORK_SHELLS_RUN_ID", "app-server".to_owned()),
                (
                    "FRAMEWORK_SHELLS_SECRET_FINGERPRINT",
                    "repo-fingerprint".to_owned()
                ),
            ]
        );
    }
}
