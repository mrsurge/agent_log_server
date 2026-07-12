use crate::adapter_process::AdapterEventSink;
use als_jsonrpc::{Request, Response, SuccessResponse};
use anyhow::{Context, Result, anyhow};
use pyo3::{
    prelude::*,
    types::{PyList, PyModule},
};
use serde_json::Value;
use std::{
    collections::HashMap,
    env,
    ffi::OsString,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};
use tokio::{task::spawn_blocking, time::sleep};
use tracing::warn;

#[derive(Clone)]
pub struct EmbeddedAdapterTransport {
    inner: Arc<EmbeddedAdapterInner>,
}

struct EmbeddedAdapterInner {
    adapter: Mutex<Py<PyAny>>,
    closed: AtomicBool,
}

impl EmbeddedAdapterTransport {
    pub async fn spawn(
        python_path_root: Option<PathBuf>,
        env_overrides: HashMap<String, String>,
        events: AdapterEventSink,
    ) -> Result<Self> {
        let adapter =
            spawn_blocking(move || create_python_adapter(python_path_root, env_overrides))
                .await
                .context("embedded adapter initialization task failed")??;
        let transport = Self {
            inner: Arc::new(EmbeddedAdapterInner {
                adapter: Mutex::new(adapter),
                closed: AtomicBool::new(false),
            }),
        };
        let drain_transport = transport.clone();
        tokio::spawn(async move {
            drain_transport.drain_notifications_loop(events).await;
        });
        Ok(transport)
    }

    pub async fn request(&self, request: &Request) -> Result<Value> {
        let payload = serde_json::to_string(request)?;
        let response_json = self
            .call_string_method("request_json", Some(payload))
            .await?;
        let value: Value = serde_json::from_str(&response_json)
            .context("embedded adapter returned invalid JSON-RPC response")?;
        match serde_json::from_value::<Response>(value)? {
            Response::Success(SuccessResponse { result, .. }) => Ok(result),
            Response::Error(error) => Err(anyhow!(
                "adapter RPC error {}: {}",
                error.error.code,
                error.error.message
            )),
        }
    }

    pub async fn shutdown(&self) -> Result<()> {
        self.inner.closed.store(true, Ordering::Relaxed);
        self.call_string_method("shutdown", None).await.map(|_| ())
    }

    async fn drain_notifications_loop(&self, events: AdapterEventSink) {
        loop {
            if let Err(error) = self.drain_notifications_once(&events).await {
                warn!(%error, "embedded adapter notification drain failed");
            }
            if self.inner.closed.load(Ordering::Relaxed) {
                break;
            }
            sleep(Duration::from_millis(25)).await;
        }
        if let Err(error) = self.drain_notifications_once(&events).await {
            warn!(%error, "embedded adapter final notification drain failed");
        }
    }

    async fn drain_notifications_once(&self, events: &AdapterEventSink) -> Result<()> {
        let payload = self
            .call_string_method("drain_notifications_json", None)
            .await?;
        let notifications: Vec<Value> = serde_json::from_str(&payload)
            .context("embedded adapter returned invalid notification batch")?;
        for notification in notifications {
            crate::adapter_process::handle_adapter_notification(notification, events).await?;
        }
        Ok(())
    }

    async fn call_string_method(
        &self,
        method: &'static str,
        arg: Option<String>,
    ) -> Result<String> {
        let inner = self.inner.clone();
        spawn_blocking(move || {
            Python::attach(|py| -> PyResult<String> {
                let adapter = {
                    let adapter = inner.adapter.lock().map_err(|_| {
                        pyo3::exceptions::PyRuntimeError::new_err("embedded adapter lock poisoned")
                    })?;
                    adapter.clone_ref(py)
                };
                let result = match arg {
                    Some(value) => adapter.call_method1(py, method, (value,))?,
                    None => adapter.call_method0(py, method)?,
                };
                if result.is_none(py) {
                    Ok(String::new())
                } else {
                    result.extract(py)
                }
            })
        })
        .await
        .context("embedded adapter Python call task failed")?
        .map_err(|error| anyhow!("embedded adapter Python call failed: {error}"))
    }
}

fn create_python_adapter(
    python_path_root: Option<PathBuf>,
    env_overrides: HashMap<String, String>,
) -> Result<Py<PyAny>> {
    Python::initialize();
    Python::attach(|py| -> PyResult<Py<PyAny>> {
        apply_python_environ(py, &env_overrides)?;
        prepend_sys_paths(py, python_path_root, env_overrides.get("PYTHONPATH"))?;
        let module = PyModule::import(py, "agent_log_server_rs.adapters.embedded_adapter")?;
        let adapter = module.getattr("EmbeddedExtensionAdapter")?.call0()?;
        Ok(adapter.into())
    })
    .map_err(|error| anyhow!("failed to initialize embedded Python adapter: {error}"))
}

fn apply_python_environ(py: Python<'_>, env_overrides: &HashMap<String, String>) -> PyResult<()> {
    let os = PyModule::import(py, "os")?;
    let environ = os.getattr("environ")?;
    for (key, value) in env_overrides {
        environ.set_item(key, value)?;
    }
    Ok(())
}

fn prepend_sys_paths(
    py: Python<'_>,
    python_path_root: Option<PathBuf>,
    pythonpath: Option<&String>,
) -> PyResult<()> {
    let mut paths = Vec::new();
    if let Some(root) = python_path_root {
        paths.push(root);
    }
    if let Some(pythonpath) = pythonpath {
        let raw = OsString::from(pythonpath);
        paths.extend(env::split_paths(&raw));
    }
    let sys = PyModule::import(py, "sys")?;
    let path = sys.getattr("path")?.cast_into::<PyList>()?;
    for path_entry in paths.into_iter().rev() {
        path.insert(0, path_entry.to_string_lossy().into_owned())?;
    }
    Ok(())
}
