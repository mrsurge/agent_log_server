#[cfg(feature = "pyo3-embed")]
mod pyo3_pipe {
    use anyhow::{Context, Result, anyhow};
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};
    use std::{collections::HashMap, env, ffi::OsString, path::PathBuf, sync::Arc};

    #[derive(Clone, Debug)]
    pub struct FerrousPipeConfig {
        pub command: Vec<String>,
        pub cwd: Option<PathBuf>,
        pub env: HashMap<String, String>,
        pub label: String,
        pub spec_id: String,
        pub subgroups: Vec<String>,
        pub shellspec_path: Option<PathBuf>,
    }

    #[derive(Clone)]
    pub struct FerrousFrameworkPipe {
        inner: Arc<Py<PyAny>>,
    }

    impl FerrousFrameworkPipe {
        pub fn spawn(config: FerrousPipeConfig) -> Result<Self> {
            let pythonpath = config.env.get("PYTHONPATH").map(OsString::from);
            Python::initialize();
            Python::attach(|py| -> PyResult<Self> {
                if let Some(pythonpath) = pythonpath {
                    let sys = py.import("sys")?;
                    let sys_path = sys.getattr("path")?;
                    let paths: Vec<_> = env::split_paths(&pythonpath).collect();
                    for path in paths.into_iter().rev() {
                        sys_path
                            .call_method1("insert", (0, path.to_string_lossy().into_owned()))?;
                    }
                }
                let module = py.import("agent_log_server_rs.ferrous_framework")?;
                let cls = module.getattr("FerrousFrameworkPipe")?;
                let command = PyList::new(py, &config.command)?;
                let env = PyDict::new(py);
                for (key, value) in config.env {
                    env.set_item(key, value)?;
                }
                let subgroups = PyList::new(py, &config.subgroups)?;
                let cwd = config
                    .cwd
                    .as_ref()
                    .map(|path| path.to_string_lossy().into_owned());
                let shellspec_path = config
                    .shellspec_path
                    .as_ref()
                    .map(|path| path.to_string_lossy().into_owned());
                let object = cls.call1((
                    command,
                    cwd,
                    env,
                    config.label,
                    config.spec_id,
                    subgroups,
                    shellspec_path,
                ))?;
                Ok(Self {
                    inner: Arc::new(object.into()),
                })
            })
            .map_err(|err| anyhow!("failed to start ferrous_framework pipe: {err}"))
        }

        pub fn write_line_blocking(&self, line: &str) -> Result<()> {
            Python::attach(|py| -> PyResult<()> {
                self.inner.call_method1(py, "write_line", (line,))?;
                Ok(())
            })
            .map_err(|err| anyhow!("ferrous_framework pipe write failed: {err}"))
        }

        pub fn read_line_blocking(&self) -> Result<Option<String>> {
            Python::attach(|py| -> PyResult<Option<String>> {
                self.inner
                    .call_method1(py, "read_line", (None::<f64>,))?
                    .extract(py)
            })
            .map_err(|err| anyhow!("ferrous_framework pipe read failed: {err}"))
        }

        pub fn shell_id(&self) -> Result<String> {
            Python::attach(|py| -> PyResult<String> {
                self.inner.call_method0(py, "shell_id")?.extract(py)
            })
            .context("failed to read ferrous_framework shell id")
        }
    }
}

#[cfg(not(feature = "pyo3-embed"))]
mod pyo3_pipe {
    use anyhow::{Result, bail};
    use std::{collections::HashMap, path::PathBuf};

    #[derive(Clone, Debug)]
    pub struct FerrousPipeConfig {
        pub command: Vec<String>,
        pub cwd: Option<PathBuf>,
        pub env: HashMap<String, String>,
        pub label: String,
        pub spec_id: String,
        pub subgroups: Vec<String>,
        pub shellspec_path: Option<PathBuf>,
    }

    #[derive(Clone)]
    pub struct FerrousFrameworkPipe;

    impl FerrousFrameworkPipe {
        pub fn spawn(_config: FerrousPipeConfig) -> Result<Self> {
            bail!("ferrous_framework was built without the pyo3-embed feature")
        }

        pub fn write_line_blocking(&self, _line: &str) -> Result<()> {
            bail!("ferrous_framework was built without the pyo3-embed feature")
        }

        pub fn read_line_blocking(&self) -> Result<Option<String>> {
            bail!("ferrous_framework was built without the pyo3-embed feature")
        }

        pub fn shell_id(&self) -> Result<String> {
            bail!("ferrous_framework was built without the pyo3-embed feature")
        }
    }
}

pub use pyo3_pipe::{FerrousFrameworkPipe, FerrousPipeConfig};

pub const fn pyo3_embed_enabled() -> bool {
    cfg!(feature = "pyo3-embed")
}
