use crate::config::{FrameworkShellConfig, ServerConfig};
use als_adapter_protocol::{ExtensionInitializeParams, JsonMap, events, methods};
use als_dto::RuntimeRoots;
use als_jsonrpc::{
    ErrorResponse, Notification, Request, RequestId, Response, RpcError, SuccessResponse,
};
use anyhow::{Context, Result, anyhow, bail};
use ferrous_framework::{
    FerrousNativeManager, FerrousNativePipeConfig, FerrousNativeShellRecord,
    FerrousNativeShellStatus, ferrous_native_enabled,
    shellspec::{ShellspecRenderInput, render_shellspec_entry},
};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    env,
    ffi::OsString,
    fs,
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
    sync::{Mutex, broadcast, mpsc, oneshot},
    time::{Duration, sleep, timeout},
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

        let client = Arc::new(
            AdapterClient::spawn(
                self.config.adapters.python_bin.clone(),
                self.config.extensions_dir.parent().map(Path::to_path_buf),
                self.config.roots.clone(),
                self.config.framework_shells.clone(),
                self.events.clone(),
            )
            .await?,
        );
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

    pub async fn reload_extensions_if_running(
        &self,
        enabled_overrides: JsonMap,
        changed_extension_ids: Option<Vec<String>>,
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
        if let (Some(changed_extension_ids), Value::Object(object)) =
            (changed_extension_ids, &mut params)
        {
            let changed_extension_ids = changed_extension_ids
                .into_iter()
                .filter(|value| !value.trim().is_empty())
                .map(Value::String)
                .collect::<Vec<_>>();
            if !changed_extension_ids.is_empty() {
                object.insert(
                    "changed_extension_ids".to_owned(),
                    Value::Array(changed_extension_ids),
                );
            }
        }
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
    Ferrous(FerrousAdapterTransport),
}

#[derive(Clone)]
struct FerrousAdapterTransport {
    manager: FerrousNativeManager,
    shell_id: String,
}

enum FerrousAdapterReadMessage {
    Line(String),
    Closed,
    Failed(String),
}

impl AdapterClient {
    pub async fn spawn(
        python: String,
        python_path_root: Option<PathBuf>,
        roots: RuntimeRoots,
        framework_shells: FrameworkShellConfig,
        events: AdapterEventSink,
    ) -> Result<Self> {
        if framework_shells.is_configured() && ferrous_native_enabled() {
            match Self::spawn_ferrous(
                python.clone(),
                python_path_root.clone(),
                &roots,
                &framework_shells,
                events.clone(),
            )
            .await
            {
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

    async fn spawn_ferrous(
        python: String,
        python_path_root: Option<PathBuf>,
        roots: &RuntimeRoots,
        framework_shells: &FrameworkShellConfig,
        events: AdapterEventSink,
    ) -> Result<Self> {
        let cwd = adapter_working_dir(python_path_root.as_deref(), roots);
        let shellspec_path = python_path_root
            .as_ref()
            .map(|root| root.join("agent_log_server_rs/shellspec/extension_adapter.yaml"))
            .filter(|path| path.is_file());
        let env = adapter_env_overrides(python_path_root.as_deref(), roots, framework_shells);
        let manager = FerrousNativeManager::try_with_env_map(&env)
            .context("failed to initialize ferrous_framework native manager")?;
        let command = vec![
            python,
            "-m".to_owned(),
            "agent_log_server_rs.adapters.extension_adapter".to_owned(),
        ];
        let subgroups = vec![
            "als-rs".to_owned(),
            "extension-adapter".to_owned(),
            "jsonrpc".to_owned(),
            "observed".to_owned(),
        ];
        let record = if let Some(shellspec_path) = shellspec_path {
            spawn_ferrous_shellspec_pipe(
                &manager,
                shellspec_path,
                command,
                Some(cwd.clone()),
                env,
                subgroups,
            )
            .await?
        } else {
            manager
                .spawn_shell_pipe(FerrousNativePipeConfig {
                    command,
                    cwd: Some(cwd.clone()),
                    env,
                    label: "als-rs-extension-adapter".to_owned(),
                    spec_id: "als-rs-extension-adapter".to_owned(),
                    subgroups,
                    log_dir: None,
                })
                .await?
        };
        wait_for_ferrous_pipe_ready(&manager, &record.id, Duration::from_secs(5)).await?;
        let transport = FerrousAdapterTransport {
            manager,
            shell_id: record.id.clone(),
        };
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(read_ferrous_adapter_stdout(
            transport.clone(),
            pending.clone(),
            events.clone(),
        ));
        let shell_id = record.id;
        let label = record.label;
        let spec_id = record.spec_id;
        tokio::spawn(async move {
            events
                .push_other(
                    "adapter.transport.started".to_owned(),
                    json!({
                        "transport": "ferrous_framework",
                        "shell_id": shell_id,
                        "label": label,
                        "spec_id": spec_id,
                    }),
                )
                .await;
        });
        Ok(Self {
            writer: AdapterWriter::Ferrous(transport),
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
            AdapterWriter::Ferrous(transport) => {
                transport
                    .manager
                    .write_to_shell(&transport.shell_id, line, true)
                    .await
                    .context("ferrous_framework adapter write failed")?;
                Ok(())
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
            AdapterWriter::Ferrous(transport) => {
                if let Err(err) = transport
                    .manager
                    .terminate_shell(&transport.shell_id, true)
                    .await
                {
                    warn!(error = %err, "ferrous_framework adapter close failed");
                }
                Ok(())
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

async fn spawn_ferrous_shellspec_pipe(
    manager: &FerrousNativeManager,
    shellspec_path: PathBuf,
    command: Vec<String>,
    cwd: Option<PathBuf>,
    env: HashMap<String, String>,
    fallback_subgroups: Vec<String>,
) -> Result<FerrousNativeShellRecord> {
    let raw = fs::read_to_string(&shellspec_path)
        .with_context(|| format!("failed to read shellspec {}", shellspec_path.display()))?;
    let document: Value = match shellspec_path.extension().and_then(|value| value.to_str()) {
        Some("json") => serde_json::from_str(&raw).with_context(|| {
            format!(
                "failed to parse shellspec JSON {}",
                shellspec_path.display()
            )
        })?,
        _ => serde_yaml::from_str(&raw).with_context(|| {
            format!(
                "failed to parse shellspec YAML {}",
                shellspec_path.display()
            )
        })?,
    };
    let mut ctx = HashMap::new();
    ctx.insert(
        "PYTHON".to_owned(),
        command.first().cloned().unwrap_or_default(),
    );
    if let Some(cwd) = &cwd {
        ctx.insert("CWD".to_owned(), cwd.to_string_lossy().into_owned());
    }
    let input = ShellspecRenderInput {
        ctx,
        env: env.clone(),
    };
    let mut spec = render_shellspec_entry(&document, "extension_adapter", &input)?;
    if spec.backend != "pipe" {
        bail!(
            "extension adapter shellspec rendered backend '{}', expected pipe",
            spec.backend
        );
    }
    let mut merged_env = env;
    merged_env.extend(spec.env);
    spec.env = merged_env;
    if spec.cwd.is_none() {
        spec.cwd = cwd;
    }
    if spec.subgroups.is_empty() {
        spec.subgroups = fallback_subgroups;
    }
    let manager = manager.clone();
    tokio::task::spawn_blocking(move || {
        manager.spawn_rendered_shellspec_with_log_dir_blocking(spec, None)
    })
    .await
    .context("ferrous_framework shellspec pipe spawn task failed")?
}

async fn wait_for_ferrous_pipe_ready(
    manager: &FerrousNativeManager,
    shell_id: &str,
    duration: Duration,
) -> Result<()> {
    let deadline = tokio::time::Instant::now() + duration;
    loop {
        match manager.get_pipe_state(shell_id)? {
            Some(state) if state.stdin_supported => return Ok(()),
            Some(state) if state.status == FerrousNativeShellStatus::Exited => {
                bail!("ferrous_framework pipe {shell_id} exited before stdin became ready");
            }
            Some(_) | None => {}
        }
        if tokio::time::Instant::now() >= deadline {
            bail!("ferrous_framework pipe {shell_id} stdin never became ready");
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn read_ferrous_adapter_stdout(
    transport: FerrousAdapterTransport,
    pending: PendingMap,
    events: AdapterEventSink,
) {
    let (sender, mut receiver) = mpsc::unbounded_channel();
    let pump_transport = transport.clone();
    let pump = tokio::task::spawn_blocking(move || {
        pump_ferrous_adapter_stdout(pump_transport, sender);
    });
    let mut terminal_message = false;

    while let Some(message) = receiver.recv().await {
        match message {
            FerrousAdapterReadMessage::Line(line) => {
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
            FerrousAdapterReadMessage::Closed => {
                terminal_message = true;
                events
                    .push_other("adapter.ferrous_framework.closed".to_owned(), json!({}))
                    .await;
                fail_all_pending(&pending, "ferrous_framework adapter pipe closed").await;
                break;
            }
            FerrousAdapterReadMessage::Failed(error) => {
                terminal_message = true;
                error!(error = %error, "ferrous_framework adapter pipe read failed");
                events
                    .push_other(
                        "adapter.ferrous_framework.read_failed".to_owned(),
                        json!({"error": error}),
                    )
                    .await;
                fail_all_pending(&pending, "ferrous_framework adapter pipe read failed").await;
                break;
            }
        }
    }

    match pump.await {
        Ok(()) => {}
        Err(err) if !terminal_message => {
            error!(error = %err, "ferrous_framework adapter pipe task failed");
            events
                .push_other(
                    "adapter.ferrous_framework.task_failed".to_owned(),
                    json!({"error": err.to_string()}),
                )
                .await;
            fail_all_pending(&pending, "ferrous_framework adapter pipe task failed").await;
        }
        Err(_) => {}
    }
}

fn pump_ferrous_adapter_stdout(
    transport: FerrousAdapterTransport,
    sender: mpsc::UnboundedSender<FerrousAdapterReadMessage>,
) {
    let mut buffer = Vec::<u8>::new();
    loop {
        match transport
            .manager
            .read_stdout_chunk_blocking(&transport.shell_id, Duration::from_millis(250))
        {
            Ok(Some(chunk)) => {
                buffer.extend_from_slice(&chunk);
                while let Some(line) = take_line_from_buffer(&mut buffer) {
                    if sender.send(FerrousAdapterReadMessage::Line(line)).is_err() {
                        return;
                    }
                }
            }
            Ok(None) => match transport.manager.get_shell(&transport.shell_id) {
                Ok(Some(record)) if record.status != FerrousNativeShellStatus::Exited => {}
                Ok(_) => {
                    if !buffer.is_empty() {
                        let line = String::from_utf8_lossy(&buffer).into_owned();
                        buffer.clear();
                        if sender.send(FerrousAdapterReadMessage::Line(line)).is_err() {
                            return;
                        }
                    }
                    let _ = sender.send(FerrousAdapterReadMessage::Closed);
                    return;
                }
                Err(err) => {
                    let _ = sender.send(FerrousAdapterReadMessage::Failed(err.to_string()));
                    return;
                }
            },
            Err(err) => {
                let _ = sender.send(FerrousAdapterReadMessage::Failed(err.to_string()));
                return;
            }
        }
    }
}

fn take_line_from_buffer(buffer: &mut Vec<u8>) -> Option<String> {
    let newline = buffer.iter().position(|byte| *byte == b'\n')?;
    let mut raw = buffer.drain(..=newline).collect::<Vec<_>>();
    if raw.ends_with(b"\n") {
        raw.pop();
    }
    if raw.ends_with(b"\r") {
        raw.pop();
    }
    Some(String::from_utf8_lossy(&raw).into_owned())
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
    lossless_streams: Arc<Mutex<Vec<mpsc::UnboundedSender<AdapterCapturedEvent>>>>,
}

impl Default for AdapterEventSink {
    fn default() -> Self {
        let (stream, _) = broadcast::channel(EVENT_STREAM_LIMIT);
        Self {
            inner: Arc::new(Mutex::new(AdapterEventSinkInner::default())),
            stream,
            lossless_streams: Arc::new(Mutex::new(Vec::new())),
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
        self.publish(AdapterCapturedEvent::Live(value)).await;
    }

    async fn push_transcript(&self, value: Value) {
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.transcript, value.clone());
        }
        self.publish(AdapterCapturedEvent::Transcript(value)).await;
    }

    async fn push_other(&self, method: String, params: Value) {
        let event = AdapterOtherEvent { method, params };
        {
            let mut guard = self.inner.lock().await;
            push_capped(&mut guard.other, event.clone());
        }
        self.publish(AdapterCapturedEvent::Other(event)).await;
    }

    async fn publish(&self, event: AdapterCapturedEvent) {
        let _ = self.stream.send(event.clone());
        let mut guard = self.lossless_streams.lock().await;
        guard.retain(|sender| sender.send(event.clone()).is_ok());
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

    pub async fn subscribe_lossless(&self) -> mpsc::UnboundedReceiver<AdapterCapturedEvent> {
        let (sender, receiver) = mpsc::unbounded_channel();
        self.lossless_streams.lock().await.push(sender);
        receiver
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

    #[tokio::test]
    async fn lossless_subscription_receives_events_beyond_broadcast_limit() {
        let sink = AdapterEventSink::default();
        let mut stream = sink.subscribe_lossless().await;
        let count = EVENT_STREAM_LIMIT + 10;

        for seq in 0..count {
            sink.push_live(json!({"type": "assistant_delta", "seq": seq}))
                .await;
        }

        for seq in 0..count {
            match timeout(Duration::from_secs(1), stream.recv()).await {
                Ok(Some(AdapterCapturedEvent::Live(value))) => {
                    assert_eq!(value["seq"], json!(seq));
                }
                other => panic!("unexpected adapter event at seq {seq}: {other:?}"),
            }
        }
    }
}
