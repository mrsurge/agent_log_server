use super::protocol::{
    AppCall, Envelope, Kind, PipeError, PipeResult, ServiceParams, ServiceRequest, address,
};
use serde_json::Value;
use std::{
    collections::HashMap,
    future::Future,
    pin::Pin,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc::SyncSender,
    },
    time::Duration,
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot, watch};

pub(super) const MAX_PENDING: usize = 16;
pub(super) const MAX_CONTROL_BYTES: usize = 64 * 1024;
pub(super) const MAX_OUTPUT_BYTES: usize = 9 * 1024 * 1024;
const MAX_RPC_BYTES: usize = 8 * 1024 * 1024;
pub(super) const WRITER_BYTES: usize = 2 * MAX_OUTPUT_BYTES;
pub(super) const INCOMING_BYTES: usize = 2 * 1024 * 1024;
pub(super) const WRITER_FRAMES: usize = 32;
pub type Handler =
    Arc<dyn Fn(ServiceRequest) -> Pin<Box<dyn Future<Output = PipeResult> + Send>> + Send + Sync>;
struct Pending {
    sender: oneshot::Sender<PipeResult>,
    operation_id: String,
    reply_bytes: usize,
}

#[derive(Clone, Copy)]
pub struct CallLimits {
    pub request_bytes: usize,
    pub reply_bytes: usize,
}

impl Default for CallLimits {
    fn default() -> Self {
        Self {
            request_bytes: MAX_CONTROL_BYTES,
            reply_bytes: 1024 * 1024,
        }
    }
}

pub(super) struct Frame {
    pub bytes: Vec<u8>,
    pub ack: Option<oneshot::Sender<Result<(), PipeError>>>,
    pub _permit: OwnedSemaphorePermit,
}
struct Job {
    request: Envelope,
    _permit: OwnedSemaphorePermit,
    _bytes: OwnedSemaphorePermit,
}

pub(super) struct Inner {
    writer: Mutex<Option<SyncSender<Frame>>>,
    bytes: Arc<Semaphore>,
    pending: Mutex<HashMap<String, Pending>>,
    closed: watch::Sender<bool>,
    failure: Mutex<Option<String>>,
    next_id: AtomicU64,
    ready: AtomicBool,
    handler: Mutex<Option<Handler>>,
    handler_deadline: Mutex<Duration>,
    requests: mpsc::Sender<Job>,
    admission: Arc<Semaphore>,
    incoming_bytes: Arc<Semaphore>,
}

#[derive(Clone)]
pub struct Client {
    pub(super) inner: Arc<Inner>,
}

impl Client {
    pub(super) fn new(writer: SyncSender<Frame>) -> Self {
        let (closed, _) = watch::channel(false);
        let (requests, receiver) = mpsc::channel(MAX_PENDING);
        let client = Self {
            inner: Arc::new(Inner {
                writer: Mutex::new(Some(writer)),
                bytes: Arc::new(Semaphore::new(WRITER_BYTES)),
                pending: Mutex::default(),
                closed,
                failure: Mutex::default(),
                next_id: AtomicU64::new(1),
                ready: AtomicBool::new(false),
                handler: Mutex::default(),
                handler_deadline: Mutex::new(Duration::from_secs(10)),
                requests,
                admission: Arc::new(Semaphore::new(MAX_PENDING)),
                incoming_bytes: Arc::new(Semaphore::new(INCOMING_BYTES)),
            }),
        };
        tokio::spawn(dispatch(client.clone(), receiver));
        client
    }

    pub fn bind_handler(&self, handler: Handler) -> Result<(), PipeError> {
        self.bind_handler_with_deadline(handler, Duration::from_secs(10))
    }

    pub fn bind_handler_with_deadline(
        &self,
        handler: Handler,
        deadline: Duration,
    ) -> Result<(), PipeError> {
        if deadline.is_zero() || deadline > Duration::from_secs(180) {
            return Err(PipeError::new(
                "pipe.invalidDeadline",
                "Handler deadline must be bounded to 180 seconds",
            ));
        }
        let mut slot = self.inner.handler.lock().unwrap_or_else(|e| e.into_inner());
        if self.is_closed() {
            return Err(closed_error());
        }
        if slot.is_some() {
            return Err(PipeError::new(
                "pipe.alreadyBound",
                "Service handler already bound",
            ));
        }
        *self
            .inner
            .handler_deadline
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = deadline;
        *slot = Some(handler);
        Ok(())
    }

    pub fn is_closed(&self) -> bool {
        *self.inner.closed.borrow()
    }

    pub(super) fn subscribe(&self) -> watch::Receiver<bool> {
        self.inner.closed.subscribe()
    }

    pub(super) fn failure(&self) -> Option<String> {
        self.inner
            .failure
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    pub(super) fn close(&self, failure: Option<String>) {
        // Lock serializes failure publication and the close signal.
        let mut reason = self.inner.failure.lock().unwrap_or_else(|e| e.into_inner());
        if self.is_closed() {
            return;
        }
        *reason = failure;
        self.inner.closed.send_replace(true);
        self.inner
            .writer
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
        self.inner
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clear();
        self.inner
            .handler
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .take();
    }

    pub(super) fn enqueue(
        &self,
        envelope: &Envelope,
        ack: Option<oneshot::Sender<Result<(), PipeError>>>,
    ) -> Result<(), PipeError> {
        let bytes = rmp_serde::to_vec_named(envelope)
            .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?;
        if bytes.len() > MAX_OUTPUT_BYTES {
            return Err(PipeError::new(
                "pipe.frameTooLarge",
                "Output exceeds control-pipe limit; outcome may be unknown",
            ));
        }
        let permit = self
            .inner
            .bytes
            .clone()
            .try_acquire_many_owned(bytes.len() as u32)
            .map_err(|_| PipeError::new("pipe.busy", "Pipe writer byte budget exhausted"))?;
        let writer = self.inner.writer.lock().unwrap_or_else(|e| e.into_inner());
        if self.is_closed() {
            return Err(closed_error());
        }
        writer
            .as_ref()
            .ok_or_else(closed_error)?
            .try_send(Frame {
                bytes,
                ack,
                _permit: permit,
            })
            .map_err(|_| PipeError::new("pipe.busy", "Pipe writer unavailable/full"))
    }

    pub(super) fn mark_ready(&self) -> Result<(), PipeError> {
        if self.inner.ready.swap(true, Ordering::AcqRel) {
            return Err(PipeError::new(
                "pipe.alreadyReady",
                "Readiness already emitted",
            ));
        }
        if self.is_closed() {
            return Err(closed_error());
        }
        Ok(())
    }

    pub async fn call(&self, call: AppCall, operation_id: String, wait: Duration) -> PipeResult {
        self.call_with_limits(call, operation_id, wait, CallLimits::default())
            .await
    }

    pub async fn call_with_limits(
        &self,
        call: AppCall,
        operation_id: String,
        wait: Duration,
        limits: CallLimits,
    ) -> PipeResult {
        if !address(&call.target_app)
            || !address(&call.service)
            || !address(&call.method)
            || call.version == 0
            || operation_id.is_empty()
            || operation_id.len() > 512
            || wait.is_zero()
            || !(1..=MAX_RPC_BYTES).contains(&limits.request_bytes)
            || !(1..=MAX_RPC_BYTES).contains(&limits.reply_bytes)
        {
            return Err(PipeError::new(
                "pipe.invalidCall",
                "Invalid app RPC address/operation/deadline",
            ));
        }
        let params = serde_json::to_value(call)
            .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?;
        if serde_json::to_vec(&params)
            .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?
            .len()
            > limits.request_bytes
        {
            return Err(PipeError::new(
                "pipe.payloadTooLarge",
                "Request exceeds its local app RPC budget",
            ));
        }
        let id = format!(
            "als:{}:{}",
            std::process::id(),
            self.inner.next_id.fetch_add(1, Ordering::Relaxed)
        );
        let (tx, rx) = oneshot::channel();
        let mut closed = self.subscribe();
        {
            let mut pending = self.inner.pending.lock().unwrap_or_else(|e| e.into_inner());
            if self.is_closed() {
                return Err(closed_error());
            }
            if pending.len() >= MAX_PENDING {
                return Err(PipeError::new(
                    "pipe.busy",
                    "Too many outstanding app calls",
                ));
            }
            pending.insert(
                id.clone(),
                Pending {
                    sender: tx,
                    operation_id: operation_id.clone(),
                    reply_bytes: limits.reply_bytes,
                },
            );
        }
        let _guard = PendingGuard {
            client: self.clone(),
            id: id.clone(),
        };
        let mut request = Envelope::outbound(Kind::Request);
        request.id = Some(id.clone());
        request.correlation_id = Some(id);
        request.op_id = Some(operation_id);
        request.method = Some("app.rpc.call".into());
        request.params = Some(params);
        self.enqueue(&request, None)?;
        tokio::select! {
            reply = tokio::time::timeout(wait, rx) => match reply {
                Ok(Ok(value)) => value,
                Ok(Err(_)) => Err(closed_error()),
                Err(_) => Err(PipeError::new("pipe.timeout", "App RPC timed out; outcome may be unknown")),
            },
            _ = closed.changed() => Err(closed_error()),
        }
    }

    pub(super) fn receive(&self, request: Envelope) -> Result<(), PipeError> {
        request.validate()?;
        match request.kind {
            Kind::Response | Kind::Error => {
                if request.id.is_none() || request.id != request.correlation_id {
                    return Ok(());
                }
                let mut pending = self.inner.pending.lock().unwrap_or_else(|e| e.into_inner());
                let id = request.id.as_deref().unwrap_or_default();
                if !pending.get(id).is_some_and(|entry| {
                    request.op_id.as_deref() == Some(entry.operation_id.as_str())
                }) {
                    return Ok(());
                }
                let reply_bytes = serde_json::to_vec(&request)
                    .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?
                    .len();
                let result =
                    if reply_bytes > pending.get(id).map(|entry| entry.reply_bytes).unwrap_or(0) {
                        Err(PipeError::new(
                            "pipe.replyTooLarge",
                            "Control reply exceeds limit; outcome may be unknown",
                        ))
                    } else {
                        match (request.kind, request.error) {
                            (Kind::Error, Some(error)) if request.result.is_none() => Err(error),
                            (Kind::Response, None) => Ok(request.result.unwrap_or(Value::Null)),
                            _ => {
                                return Err(PipeError::new(
                                    "protocol.response",
                                    "Invalid response/error envelope",
                                ));
                            }
                        }
                    };
                if let Some(entry) = pending.remove(id) {
                    let _ = entry.sender.send(result);
                }
            }
            Kind::Request => {
                let params_bytes = serde_json::to_vec(&request.params)
                    .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?
                    .len();
                if params_bytes > MAX_CONTROL_BYTES {
                    return self.enqueue(
                        &request.reply(Err(PipeError::new(
                            "pipe.payloadTooLarge",
                            "Control request exceeds 64 KiB",
                        ))),
                        None,
                    );
                }
                let size = rmp_serde::to_vec_named(&request)
                    .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?
                    .len();
                let bytes = self
                    .inner
                    .incoming_bytes
                    .clone()
                    .try_acquire_many_owned(size as u32);
                let Ok(bytes) = bytes else {
                    return self.enqueue(
                        &request.reply(Err(PipeError::new(
                            "pipe.busy",
                            "ALS request byte budget exhausted",
                        ))),
                        None,
                    );
                };
                let permit = self.inner.admission.clone().try_acquire_owned();
                match permit {
                    Ok(permit) => self
                        .inner
                        .requests
                        .try_send(Job {
                            request,
                            _permit: permit,
                            _bytes: bytes,
                        })
                        .map_err(|_| PipeError::new("pipe.closed", "Request dispatcher stopped"))?,
                    Err(_) => self.enqueue(
                        &request.reply(Err(PipeError::new(
                            "pipe.busy",
                            "ALS request admission full",
                        ))),
                        None,
                    )?,
                }
            }
            // No subscription or cancellation semantics are advertised in Stage A.
            Kind::Hello | Kind::Notification | Kind::Progress | Kind::Cancel => {}
        }
        Ok(())
    }
}

struct PendingGuard {
    client: Client,
    id: String,
}
impl Drop for PendingGuard {
    fn drop(&mut self) {
        self.client
            .inner
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&self.id);
    }
}
fn closed_error() -> PipeError {
    PipeError::new(
        "pipe.closed",
        "Framework pipe closed; outcome may be unknown",
    )
}

async fn dispatch(client: Client, mut requests: mpsc::Receiver<Job>) {
    let mut closed = client.subscribe();
    let mut tasks = tokio::task::JoinSet::new();
    loop {
        if client.is_closed() {
            break;
        }
        tokio::select! {
            biased;
            _ = closed.changed() => break,
            done = tasks.join_next(), if !tasks.is_empty() => {
                if let Some(Err(error)) = done { client.close(Some(format!("Pipe handler failed: {error}"))); }
            },
            job = requests.recv() => match job {
                Some(job) => {
                    let client = client.clone();
                    let Job { request, _permit, _bytes } = job;
                    tasks.spawn(async move {
                        let _admission = _permit;
                        let _byte_admission = _bytes;
                        let deadline = *client.inner.handler_deadline.lock().unwrap_or_else(|e| e.into_inner());
                        let result = match tokio::time::timeout(deadline, invoke(&client, &request)).await {
                            Ok(result) => result,
                            Err(_) => Err(PipeError::new("pipe.handlerTimeout", "Control handler timed out; outcome may be unknown")),
                        };
                        if let Err(error) = client.enqueue(&request.reply(result), None) {
                            // Size rejection happens before enqueue. A bounded
                            // error reply is not a retry of domain execution.
                            if error.code != "pipe.frameTooLarge"
                                || client.enqueue(&request.reply(Err(PipeError::new(
                                    "pipe.replyTooLarge", "Control reply exceeds limit; outcome may be unknown",
                                ))), None).is_err() {
                                client.close(Some(error.to_string()));
                            }
                        }
                    });
                }
                None => break,
            },
        }
    }
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
}

async fn invoke(client: &Client, request: &Envelope) -> PipeResult {
    if request.method.as_deref() != Some("app.rpc.deliver") {
        return Err(PipeError::new(
            "method_not_found",
            "ALS framework method not supported",
        ));
    }
    if request.id != request.correlation_id
        || request
            .op_id
            .as_deref()
            .is_none_or(|id| id.is_empty() || id.len() > 512)
    {
        return Err(PipeError::new(
            "protocol.correlation",
            "Service request needs matching correlation and operation ID",
        ));
    }
    if !client.inner.ready.load(Ordering::Acquire) {
        return Err(PipeError::new("pipe.notReady", "ALS is not serving yet"));
    }
    let params = request.params.clone().unwrap_or(Value::Null);
    if serde_json::to_vec(&params)
        .map_err(|e| PipeError::new("pipe.encode", &e.to_string()))?
        .len()
        > MAX_CONTROL_BYTES
    {
        return Err(PipeError::new(
            "pipe.payloadTooLarge",
            "Control request exceeds 64 KiB",
        ));
    }
    let service: ServiceParams = serde_json::from_value(params)
        .map_err(|e| PipeError::new("protocol.params", &e.to_string()))?;
    if !address(&service.service)
        || !address(&service.method)
        || !address(&service.source.app_id)
        || service.source.shell_id.is_empty()
        || service.source.shell_id.len() > 512
        || service.version == 0
    {
        return Err(PipeError::new(
            "protocol.params",
            "Invalid framework service metadata",
        ));
    }
    let handler = client
        .inner
        .handler
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .clone();
    match handler {
        Some(handler) => {
            handler(ServiceRequest {
                request_id: request.id.clone().unwrap_or_default(),
                operation_id: request.op_id.clone().unwrap_or_default(),
                project_generation: request.project_generation,
                workspace_root: request.workspace_root.clone(),
                params: service,
            })
            .await
        }
        None => Err(PipeError::new(
            "method_not_found",
            "ALS framework service not supported",
        )),
    }
}

#[cfg(test)]
impl Client {
    pub(super) fn pending_count(&self) -> usize {
        self.inner
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .len()
    }
    pub(super) fn incoming_available(&self) -> usize {
        self.inner.admission.available_permits()
    }
    pub(super) fn writer_available(&self) -> usize {
        self.inner.bytes.available_permits()
    }
    pub(super) fn incoming_bytes_available(&self) -> usize {
        self.inner.incoming_bytes.available_permits()
    }
}
