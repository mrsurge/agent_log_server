use crate::config::{FrameworkShellConfig, ServerConfig};
use als_adapter_protocol::{ExtensionInitializeParams, JsonMap, events, methods};
use als_dto::RuntimeRoots;
use als_jsonrpc::{
    ErrorResponse, Notification, Request, RequestId, Response, RpcError, SuccessResponse,
};
use anyhow::{Context, Result, anyhow, bail};
use ferrous_framework::{FerrousFrameworkPipe, FerrousPipeConfig, pyo3_embed_enabled};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    env,
    ffi::OsString,
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicI64, Ordering},
    },
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, broadcast, oneshot},
    time::{Duration, timeout},
};
use tracing::{debug, error, warn};

const EVENT_BUFFER_LIMIT: usize = 512;
const EVENT_STREAM_LIMIT: usize = 1024;
const LOG_LINE_LIMIT: usize = 4 * 1024;
type PendingSender = oneshot::Sender<Result<Value, RpcError>>;
type PendingMap = Arc<Mutex<HashMap<RequestId, PendingSender>>>;

#[derive(Clone)]
pub struct AdapterSupervisor {
    config: ServerConfig,
    events: AdapterEventSink,
    client: Arc<Mutex<Option<Arc<AdapterClient>>>>,
}

impl AdapterSupervisor {
    pub fn new(config: ServerConfig, events: AdapterEventSink) -> Self {
        Self {
            config,
            events,
            client: Arc::new(Mutex::new(None)),
        }
    }

    pub fn events(&self) -> AdapterEventSink {
        self.events.clone()
    }

    pub async fn client(&self) -> Result<Arc<AdapterClient>> {
        let mut guard = self.client.lock().await;
        if let Some(client) = guard.as_ref() {
            return Ok(client.clone());
        }

        let client = Arc::new(AdapterClient::spawn(
            self.config.adapters.copilot_python.clone(),
            self.config.extensions_dir.parent().map(Path::to_path_buf),
            self.config.roots.clone(),
            self.config.framework_shells.clone(),
            self.events.clone(),
        )?);
        *guard = Some(client.clone());
        Ok(client)
    }

    pub async fn initialize_extension(&self, extension_id: &str) -> Result<Value> {
        let params = ExtensionInitializeParams {
            extension_id: extension_id.to_owned(),
            extensions_dir: Some(self.config.extensions_dir.clone()),
            extensions_dirs: Some(self.config.extension_roots()),
            cwd: self
                .config
                .extensions_dir
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| {
                    std::env::current_dir().unwrap_or_else(|_| self.config.roots.data_dir.clone())
                }),
            data_dir: self.config.roots.data_dir.clone(),
            cache_dir: self.config.roots.cache_dir.clone(),
            config_dir: self.config.roots.config_dir.clone(),
            settings: JsonMap::new(),
        };
        self.client()
            .await?
            .request_value(methods::EXTENSION_INITIALIZE, params)
            .await
    }

    pub async fn initialize_copilot(&self) -> Result<Value> {
        self.initialize_extension("copilot-sdk").await
    }

    pub async fn reload_extensions_if_running(
        &self,
        enabled_overrides: JsonMap,
        wait_ready_extension_id: Option<String>,
    ) -> Result<Option<Value>> {
        let client = self.client.lock().await.clone();
        let Some(client) = client else {
            return Ok(None);
        };
        let mut params = json!({
            "force": true,
            "enabled_overrides": enabled_overrides,
        });
        if let (Some(extension_id), Value::Object(object)) = (wait_ready_extension_id, &mut params)
        {
            object.insert(
                "wait_ready_extension_id".to_owned(),
                Value::String(extension_id),
            );
        }
        let result = client
            .request_value(methods::EXTENSION_RELOAD, params)
            .await?;
        Ok(Some(result))
    }

    pub async fn warm_up_extensions(
        &self,
        enabled_overrides: JsonMap,
        init_extension_id: String,
    ) -> Result<Value> {
        self.initialize_extension(&init_extension_id).await?;
        let result = self
            .client()
            .await?
            .request_value(
                methods::EXTENSION_WARM_UP,
                json!({
                    "enabled_overrides": enabled_overrides,
                    "timeout": 60.0,
                }),
            )
            .await?;
        Ok(result)
    }

    pub async fn shutdown(&self) -> Result<()> {
        let client = self.client.lock().await.take();
        if let Some(client) = client {
            client.shutdown().await?;
        }
        Ok(())
    }
}

pub struct AdapterClient {
    writer: AdapterWriter,
    pending: PendingMap,
    next_id: AtomicI64,
    child: Mutex<Option<Child>>,
}

#[derive(Clone)]
enum AdapterWriter {
    Direct(Arc<Mutex<ChildStdin>>),
    Ferrous(FerrousFrameworkPipe),
}

impl AdapterClient {
    pub fn spawn(
        python: String,
        python_path_root: Option<PathBuf>,
        roots: RuntimeRoots,
        framework_shells: FrameworkShellConfig,
        events: AdapterEventSink,
    ) -> Result<Self> {
        if framework_shells.is_configured() && pyo3_embed_enabled() {
            match Self::spawn_ferrous(
                python.clone(),
                python_path_root.clone(),
                &roots,
                &framework_shells,
                events.clone(),
            ) {
                Ok(client) => return Ok(client),
                Err(err) => {
                    warn!(
                        error = %err,
                        "falling back to direct extension adapter child after ferrous_framework spawn failed"
                    );
                    let events = events.clone();
                    tokio::spawn(async move {
                        events
                            .push_other(
                                "adapter.transport.fallback".to_owned(),
                                json!({
                                    "from": "ferrous_framework",
                                    "to": "direct_child",
                                    "error": err.to_string(),
                                }),
                            )
                            .await;
                    });
                }
            }
        }
        Self::spawn_direct(python, python_path_root, &roots, &framework_shells, events)
    }

    fn spawn_ferrous(
        python: String,
        python_path_root: Option<PathBuf>,
        roots: &RuntimeRoots,
        framework_shells: &FrameworkShellConfig,
        events: AdapterEventSink,
    ) -> Result<Self> {
        let cwd = Some(adapter_working_dir(python_path_root.as_deref(), roots));
        let shellspec_path = python_path_root
            .as_ref()
            .map(|root| root.join("agent_log_server_rs/shellspec/extension_adapter.yaml"));
        let env = adapter_env_overrides(python_path_root.as_deref(), roots, framework_shells);
        let pipe = FerrousFrameworkPipe::spawn(FerrousPipeConfig {
            command: vec![
                python,
                "-m".to_owned(),
                "agent_log_server_rs.adapters.extension_adapter".to_owned(),
            ],
            cwd,
            env,
            label: "als-rs-extension-adapter".to_owned(),
            spec_id: "als-rs-extension-adapter".to_owned(),
            subgroups: vec![
                "als-rs".to_owned(),
                "extension-adapter".to_owned(),
                "jsonrpc".to_owned(),
                "observed".to_owned(),
            ],
            shellspec_path,
        })?;
        let shell_id = pipe.shell_id().ok();
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(read_ferrous_adapter_stdout(
            pipe.clone(),
            pending.clone(),
            events.clone(),
        ));
        tokio::spawn(async move {
            events
                .push_other(
                    "adapter.transport.started".to_owned(),
                    json!({
                        "transport": "ferrous_framework",
                        "shell_id": shell_id,
                        "label": "als-rs-extension-adapter",
                        "spec_id": "als-rs-extension-adapter",
                    }),
                )
                .await;
        });
        Ok(Self {
            writer: AdapterWriter::Ferrous(pipe),
            pending,
            next_id: AtomicI64::new(1),
            child: Mutex::new(None),
        })
    }

    fn spawn_direct(
        python: String,
        python_path_root: Option<PathBuf>,
        roots: &RuntimeRoots,
        framework_shells: &FrameworkShellConfig,
        events: AdapterEventSink,
    ) -> Result<Self> {
        let mut command = Command::new(&python);
        command
            .arg("-m")
            .arg("agent_log_server_rs.adapters.extension_adapter")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        for (key, value) in
            adapter_env_overrides(python_path_root.as_deref(), roots, framework_shells)
        {
            command.env(key, value);
        }
        command.current_dir(adapter_working_dir(python_path_root.as_deref(), roots));
        let mut child = command
            .spawn()
            .with_context(|| format!("failed to spawn extension adapter via {python}"))?;

        let stdin = child.stdin.take().context("adapter stdin is unavailable")?;
        let stdout = child
            .stdout
            .take()
            .context("adapter stdout is unavailable")?;
        let stderr = child
            .stderr
            .take()
            .context("adapter stderr is unavailable")?;
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(read_adapter_stdout(stdout, pending.clone(), events.clone()));
        tokio::spawn(read_adapter_stderr(stderr, events));

        Ok(Self {
            writer: AdapterWriter::Direct(Arc::new(Mutex::new(stdin))),
            pending,
            next_id: AtomicI64::new(1),
            child: Mutex::new(Some(child)),
        })
    }

    pub async fn request_value<P>(&self, method: &str, params: P) -> Result<Value>
    where
        P: Serialize,
    {
        let result = self
            .request_raw(method, serde_json::to_value(params)?)
            .await?;
        Ok(result)
    }

    pub async fn request<T, P>(&self, method: &str, params: P) -> Result<T>
    where
        T: DeserializeOwned,
        P: Serialize,
    {
        let value = self.request_value(method, params).await?;
        Ok(serde_json::from_value(value)?)
    }

    async fn request_raw(&self, method: &str, params: Value) -> Result<Value> {
        let id = RequestId::Number(self.next_id.fetch_add(1, Ordering::Relaxed));
        let request = Request::new(id.clone(), method, Some(params));
        let line = serde_json::to_string(&request)?;
        let (sender, receiver) = oneshot::channel();

        self.pending.lock().await.insert(id.clone(), sender);
        if let Err(err) = self.write_line(&line).await {
            self.pending.lock().await.remove(&id);
            return Err(err);
        }

        match receiver.await.context("adapter response channel closed")? {
            Ok(result) => Ok(result),
            Err(error) => Err(anyhow!(
                "adapter RPC error {}: {}",
                error.code,
                error.message
            )),
        }
    }

    async fn write_line(&self, line: &str) -> Result<()> {
        match &self.writer {
            AdapterWriter::Direct(stdin) => {
                let mut stdin = stdin.lock().await;
                stdin.write_all(line.as_bytes()).await?;
                stdin.write_all(b"\n").await?;
                stdin.flush().await?;
                Ok(())
            }
            AdapterWriter::Ferrous(pipe) => {
                let pipe = pipe.clone();
                let line = line.to_owned();
                tokio::task::spawn_blocking(move || pipe.write_line_blocking(&line))
                    .await
                    .context("ferrous_framework write task failed")?
            }
        }
    }

    pub async fn shutdown(&self) -> Result<()> {
        match timeout(
            Duration::from_secs(5),
            self.request_value(methods::EXTENSION_SHUTDOWN, json!({})),
        )
        .await
        {
            Ok(Ok(_)) => {}
            Ok(Err(err)) => warn!(error = %err, "extension adapter shutdown RPC failed"),
            Err(_) => warn!("extension adapter shutdown RPC timed out"),
        }

        match &self.writer {
            AdapterWriter::Direct(_) => self.shutdown_direct_child().await,
            AdapterWriter::Ferrous(pipe) => {
                let pipe = pipe.clone();
                match tokio::task::spawn_blocking(move || pipe.close_blocking()).await {
                    Ok(Ok(())) => Ok(()),
                    Ok(Err(err)) => {
                        warn!(error = %err, "ferrous_framework adapter close failed");
                        Ok(())
                    }
                    Err(err) => {
                        warn!(error = %err, "ferrous_framework adapter close task failed");
                        Ok(())
                    }
                }
            }
        }
    }

    async fn shutdown_direct_child(&self) -> Result<()> {
        let child = self.child.lock().await.take();
        let Some(mut child) = child else {
            return Ok(());
        };
        match timeout(Duration::from_secs(5), child.wait()).await {
            Ok(Ok(_status)) => Ok(()),
            Ok(Err(err)) => {
                warn!(error = %err, "extension adapter child wait failed");
                Ok(())
            }
            Err(_) => {
                warn!("extension adapter child did not exit after shutdown; killing");
                if let Err(err) = child.kill().await {
                    warn!(error = %err, "failed to kill extension adapter child");
                }
                let _ = child.wait().await;
                Ok(())
            }
        }
    }
}

fn pythonpath_with_root(root: &Path, existing: Option<OsString>) -> Option<OsString> {
    let mut paths = vec![root.to_path_buf()];
    if let Some(existing) = existing {
        paths.extend(env::split_paths(&existing));
    }
    env::join_paths(paths).ok()
}

fn adapter_env_overrides(
    python_path_root: Option<&Path>,
    roots: &RuntimeRoots,
    framework_shells: &FrameworkShellConfig,
) -> HashMap<String, String> {
    let mut env = HashMap::new();
    if let Some(root) = python_path_root {
        if let Some(pythonpath) = pythonpath_with_root(root, env::var_os("PYTHONPATH")) {
            if let Ok(value) = pythonpath.into_string() {
                env.insert("PYTHONPATH".to_owned(), value);
            }
        }
    }
    for (key, value) in framework_shells.env_overrides() {
        env.insert(key.to_owned(), value);
    }
    env.insert(
        "ALS_RS_DATA_DIR".to_owned(),
        roots.data_dir.to_string_lossy().into_owned(),
    );
    env.insert(
        "ALS_RS_CACHE_DIR".to_owned(),
        roots.cache_dir.to_string_lossy().into_owned(),
    );
    env.insert(
        "ALS_RS_CONFIG_DIR".to_owned(),
        roots.config_dir.to_string_lossy().into_owned(),
    );
    env.insert(
        "ALS_RS_STATIC_DIR".to_owned(),
        roots.static_dir.to_string_lossy().into_owned(),
    );
    env
}

fn adapter_working_dir(python_path_root: Option<&Path>, roots: &RuntimeRoots) -> PathBuf {
    for candidate in [
        python_path_root,
        Some(roots.data_dir.as_path()),
        Some(roots.cache_dir.as_path()),
        Some(roots.config_dir.as_path()),
    ] {
        if let Some(path) = candidate {
            if path.is_dir() {
                return path.to_path_buf();
            }
        }
    }
    roots.data_dir.clone()
}

async fn read_ferrous_adapter_stdout(
    pipe: FerrousFrameworkPipe,
    pending: PendingMap,
    events: AdapterEventSink,
) {
    loop {
        let reader = pipe.clone();
        let line = match tokio::task::spawn_blocking(move || reader.read_line_blocking()).await {
            Ok(Ok(line)) => line,
            Ok(Err(err)) => {
                error!(error = %err, "ferrous_framework adapter pipe read failed");
                events
                    .push_other(
                        "adapter.ferrous_framework.read_failed".to_owned(),
                        json!({"error": err.to_string()}),
                    )
                    .await;
                fail_all_pending(&pending, "ferrous_framework adapter pipe read failed").await;
                break;
            }
            Err(err) => {
                error!(error = %err, "ferrous_framework adapter pipe task failed");
                events
                    .push_other(
                        "adapter.ferrous_framework.task_failed".to_owned(),
                        json!({"error": err.to_string()}),
                    )
                    .await;
                fail_all_pending(&pending, "ferrous_framework adapter pipe task failed").await;
                break;
            }
        };

        let Some(line) = line else {
            events
                .push_other("adapter.ferrous_framework.closed".to_owned(), json!({}))
                .await;
            fail_all_pending(&pending, "ferrous_framework adapter pipe closed").await;
            break;
        };
        if let Err(err) = handle_adapter_line(&line, &pending, &events).await {
            warn!(error = %err, "failed to handle ferrous_framework adapter JSON-RPC line");
            events
                .push_other(
                    "adapter.ferrous_framework.invalid_json".to_owned(),
                    json!({
                        "error": err.to_string(),
                        "line": truncate_log_line(&line),
                    }),
                )
                .await;
        }
    }
}

async fn read_adapter_stdout(
    stdout: tokio::process::ChildStdout,
    pending: PendingMap,
    events: AdapterEventSink,
) {
    let mut lines = BufReader::new(stdout).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                if let Err(err) = handle_adapter_line(&line, &pending, &events).await {
                    warn!(error = %err, "failed to handle adapter JSON-RPC line");
                    events
                        .push_other(
                            "adapter.stdout.invalid_json".to_owned(),
                            json!({
                                "error": err.to_string(),
                                "line": truncate_log_line(&line),
                            }),
                        )
                        .await;
                }
            }
            Ok(None) => {
                events
                    .push_other("adapter.stdout.closed".to_owned(), json!({}))
                    .await;
                fail_all_pending(&pending, "adapter stdout closed").await;
                break;
            }
            Err(err) => {
                error!(error = %err, "adapter stdout read failed");
                events
                    .push_other(
                        "adapter.stdout.read_failed".to_owned(),
                        json!({"error": err.to_string()}),
                    )
                    .await;
                fail_all_pending(&pending, "adapter stdout read failed").await;
                break;
            }
        }
    }
}

async fn read_adapter_stderr(stderr: tokio::process::ChildStderr, events: AdapterEventSink) {
    let mut lines = BufReader::new(stderr).lines();
    loop {
        match lines.next_line().await {
            Ok(Some(line)) => {
                eprintln!("[extension-adapter] {line}");
                events
                    .push_other(
                        "adapter.stderr".to_owned(),
                        json!({"line": truncate_log_line(&line)}),
                    )
                    .await;
            }
            Ok(None) => {
                events
                    .push_other("adapter.stderr.closed".to_owned(), json!({}))
                    .await;
                break;
            }
            Err(err) => {
                error!(error = %err, "adapter stderr read failed");
                events
                    .push_other(
                        "adapter.stderr.read_failed".to_owned(),
                        json!({"error": err.to_string()}),
                    )
                    .await;
                break;
            }
        }
    }
}

async fn handle_adapter_line(
    line: &str,
    pending: &PendingMap,
    events: &AdapterEventSink,
) -> Result<()> {
    let value: Value = serde_json::from_str(line).context("invalid adapter JSON")?;
    if value.get("id").is_some() && (value.get("result").is_some() || value.get("error").is_some())
    {
        handle_response(value, pending).await
    } else if value.get("method").is_some() {
        handle_notification(value, events).await
    } else {
        bail!("adapter message is neither response nor notification")
    }
}

async fn handle_response(value: Value, pending: &PendingMap) -> Result<()> {
    let response: Response = serde_json::from_value(value)?;
    let (id, result) = match response {
        Response::Success(SuccessResponse { id, result, .. }) => (id, Ok(result)),
        Response::Error(ErrorResponse { id, error, .. }) => (id, Err(error)),
    };
    let Some(sender) = pending.lock().await.remove(&id) else {
        debug!(?id, "dropping response for unknown adapter request");
        return Ok(());
    };
    let _ = sender.send(result);
    Ok(())
}

async fn handle_notification(value: Value, events: &AdapterEventSink) -> Result<()> {
    let notification: Notification = serde_json::from_value(value)?;
    let params = notification.params.unwrap_or(Value::Null);
    match notification.method.as_str() {
        events::LIVE_EVENT => events.push_live(params).await,
        events::TRANSCRIPT_RECORD => events.push_transcript(params).await,
        _ => events.push_other(notification.method, params).await,
    }
    Ok(())
}

async fn fail_all_pending(pending: &PendingMap, message: &str) {
    let mut guard = pending.lock().await;
    for (_, sender) in guard.drain() {
        let _ = sender.send(Err(RpcError::new(-32000, message, None)));
    }
}

#[derive(Clone)]
pub struct AdapterEventSink {
    inner: Arc<Mutex<AdapterEventSinkInner>>,
    stream: broadcast::Sender<AdapterCapturedEvent>,
}

impl Default for AdapterEventSink {
    fn default() -> Self {
        let (stream, _) = broadcast::channel(EVENT_STREAM_LIMIT);
        Self {
            inner: Arc::new(Mutex::new(AdapterEventSinkInner::default())),
            stream,
        }
    }
}

#[derive(Default)]
struct AdapterEventSinkInner {
    live: VecDeque<Value>,
    transcript: VecDeque<Value>,
    other: VecDeque<AdapterOtherEvent>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AdapterEventSnapshot {
    pub live: Vec<Value>,
    pub transcript: Vec<Value>,
    pub other: Vec<AdapterOtherEvent>,
}

#[derive(Clone, Debug, Serialize)]
pub struct AdapterOtherEvent {
    pub method: String,
    pub params: Value,
}

#[derive(Clone, Debug)]
pub enum AdapterCapturedEvent {
    Live(Value),
    Transcript(Value),
    Other(AdapterOtherEvent),
}

impl AdapterEventSink {
    async fn push_live(&self, value: Value) {
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.live, value.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Live(value));
    }

    async fn push_transcript(&self, value: Value) {
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.transcript, value.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Transcript(value));
    }

    async fn push_other(&self, method: String, params: Value) {
        let event = AdapterOtherEvent { method, params };
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.other, event.clone());
        }
        let _ = self.stream.send(AdapterCapturedEvent::Other(event));
    }

    pub async fn snapshot(&self) -> AdapterEventSnapshot {
        let guard = self.inner.lock().await;
        AdapterEventSnapshot {
            live: guard.live.iter().cloned().collect(),
            transcript: guard.transcript.iter().cloned().collect(),
            other: guard.other.iter().cloned().collect(),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<AdapterCapturedEvent> {
        self.stream.subscribe()
    }
}

fn push_capped<T>(items: &mut VecDeque<T>, item: T) {
    if items.len() >= EVENT_BUFFER_LIMIT {
        items.pop_front();
    }
    items.push_back(item);
}

fn truncate_log_line(line: &str) -> String {
    line.chars().take(LOG_LINE_LIMIT).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[tokio::test]
    async fn correlates_success_response_by_request_id() {
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        let (sender, receiver) = oneshot::channel();
        pending.lock().await.insert(RequestId::Number(7), sender);

        handle_adapter_line(
            r#"{"jsonrpc":"2.0","id":7,"result":{"ok":true}}"#,
            &pending,
            &AdapterEventSink::default(),
        )
        .await
        .unwrap();

        let result = receiver.await.unwrap().unwrap();
        assert_eq!(result, json!({"ok": true}));
        assert!(pending.lock().await.is_empty());
    }

    #[tokio::test]
    async fn captures_live_and_transcript_notifications() {
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        let sink = AdapterEventSink::default();
        let mut stream = sink.subscribe();

        handle_adapter_line(
            r#"{"jsonrpc":"2.0","method":"event.live","params":{"type":"assistant_delta","delta":"pong"}}"#,
            &pending,
            &sink,
        )
        .await
        .unwrap();
        handle_adapter_line(
            r#"{"jsonrpc":"2.0","method":"event.transcript_record","params":{"role":"assistant","text":"pong"}}"#,
            &pending,
            &sink,
        )
        .await
        .unwrap();

        let snapshot = sink.snapshot().await;
        assert_eq!(
            snapshot.live,
            vec![json!({"type": "assistant_delta", "delta": "pong"})]
        );
        assert_eq!(
            snapshot.transcript,
            vec![json!({"role": "assistant", "text": "pong"})]
        );
        assert!(snapshot.other.is_empty());

        match stream.recv().await.unwrap() {
            AdapterCapturedEvent::Live(value) => {
                assert_eq!(value, json!({"type": "assistant_delta", "delta": "pong"}));
            }
            other => panic!("unexpected adapter event: {other:?}"),
        }
        match stream.recv().await.unwrap() {
            AdapterCapturedEvent::Transcript(value) => {
                assert_eq!(value, json!({"role": "assistant", "text": "pong"}));
            }
            other => panic!("unexpected adapter event: {other:?}"),
        }
    }
}
