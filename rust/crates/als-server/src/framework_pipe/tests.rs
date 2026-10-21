use super::*;
use protocol::{AppCall, PipeError};
use runtime::{MAX_PENDING, WRITER_BYTES, WRITER_FRAMES};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};

fn fixture() -> (FrameworkClient, mpsc::Receiver<runtime::Frame>) {
    let (tx, rx) = mpsc::sync_channel(WRITER_FRAMES);
    (FrameworkClient::new(tx), rx)
}

async fn frame(rx: &mpsc::Receiver<runtime::Frame>) -> Envelope {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match rx.try_recv() {
                Ok(frame) => return rmp_serde::from_slice(&frame.bytes).unwrap(),
                Err(mpsc::TryRecvError::Empty) => tokio::task::yield_now().await,
                Err(error) => panic!("writer stopped: {error}"),
            }
        }
    })
    .await
    .unwrap()
}

fn incoming(id: &str, method: &str) -> Envelope {
    let mut envelope = Envelope::outbound(Kind::Request);
    envelope.origin_nid = 1;
    envelope.origin_name = "framework.rust".into();
    envelope.target_nid = Some(0);
    envelope.target_name = Some("als-rs".into());
    envelope.id = Some(id.into());
    envelope.correlation_id = Some(id.into());
    envelope.op_id = Some(format!("op-{id}"));
    envelope.method = Some(method.into());
    envelope.params = Some(json!({"service":"test","version":1,"method":"echo",
        "context":null,"payload":{"sample":"text"},"source":{"appId":"code_te2","shellId":"owned-shell"}}));
    envelope
}

fn call() -> AppCall {
    AppCall {
        target_app: "code_te2".into(),
        service: "sidebar".into(),
        version: 1,
        method: "cwd.get".into(),
        context: Value::Null,
        payload: json!({}),
    }
}

fn response(request: &Envelope) -> Envelope {
    let mut reply = incoming(request.id.as_deref().unwrap(), "unused");
    reply.kind = Kind::Response;
    reply.method = None;
    reply.params = None;
    reply.op_id = request.op_id.clone();
    reply.result = Some(json!({"cwd":"/proof"}));
    reply
}

#[test]
fn extracts_only_explicit_stdio_flag() {
    let mut args = vec![
        "--stdio".into(),
        "--framework-shells-run-id".into(),
        "run".into(),
    ];
    assert!(take_stdio_flag(&mut args));
    assert_eq!(
        args,
        vec![
            OsString::from("--framework-shells-run-id"),
            OsString::from("run")
        ]
    );
    assert!(!take_stdio_flag(&mut args));
}

#[test]
fn fragmented_and_coalesced_maps_are_not_length_prefixed() {
    struct Fragments {
        bytes: Cursor<Vec<u8>>,
        size: usize,
    }
    impl Read for Fragments {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            let count = buf.len().min(self.size);
            self.bytes.read(&mut buf[..count])
        }
    }
    let envelope = incoming("probe", "not.implemented");
    let mut bytes = rmp_serde::to_vec_named(&envelope).unwrap();
    bytes.extend(bytes.clone());
    assert!((0x80..=0x8f).contains(&bytes[0]));
    for size in [1, 7, bytes.len()] {
        let mut received = Vec::new();
        read_input(
            Fragments {
                bytes: Cursor::new(bytes.clone()),
                size,
            },
            |frame| {
                received.push(frame);
                Ok(())
            },
        )
        .unwrap();
        assert_eq!(received.len(), 2);
        assert_eq!(received[0].id.as_deref(), Some("probe"));
    }
}

#[test]
fn malformed_or_truncated_input_is_fatal() {
    for bytes in [vec![0x81], vec![0xc1], b"log output".to_vec()] {
        assert!(read_input(Cursor::new(bytes), |_| Ok(())).is_err());
    }
}

#[tokio::test]
async fn unsupported_method_echoes_operation_and_correlation() {
    let (client, rx) = fixture();
    let mut request = incoming("request", "unknown");
    request.project_generation = Some(7);
    request.workspace_root = Some("/project".into());
    client.receive(request).unwrap();
    let reply = frame(&rx).await;
    assert_eq!(reply.kind, Kind::Error);
    assert_eq!(reply.id, reply.correlation_id);
    assert_eq!(reply.op_id.as_deref(), Some("op-request"));
    assert_eq!(reply.project_generation, Some(7));
    assert_eq!(reply.workspace_root.as_deref(), Some("/project"));
    assert_eq!(reply.error.unwrap().code, "method_not_found");
    client.close(None);
}

#[tokio::test]
async fn outbound_calls_are_correlated_and_errors_preserved() {
    let (client, rx) = fixture();
    let c = client.clone();
    let task = tokio::spawn(async move {
        c.call(call(), "operation".into(), Duration::from_secs(2))
            .await
    });
    let request = frame(&rx).await;
    assert_eq!(request.method.as_deref(), Some("app.rpc.call"));
    assert_eq!(request.params.as_ref().unwrap()["targetApp"], "code_te2");
    assert_eq!(request.id, request.correlation_id);
    let mut wrong = response(&request);
    wrong.op_id = Some("forged".into());
    client.receive(wrong).unwrap();
    assert_eq!(client.pending_count(), 1);
    let mut wrong = response(&request);
    wrong.correlation_id = Some("old".into());
    client.receive(wrong).unwrap();
    assert_eq!(client.pending_count(), 1);
    let mut reply = response(&request);
    reply.kind = Kind::Error;
    reply.result = None;
    reply.error = Some(PipeError {
        code: "revision.conflict".into(),
        message: "changed".into(),
        retryable: false,
        details: Some(json!({"revision":2})),
    });
    client.receive(reply.clone()).unwrap();
    assert_eq!(task.await.unwrap().unwrap_err(), reply.error.unwrap());
    client.receive(response(&request)).unwrap();
    assert_eq!(client.pending_count(), 0);
    assert!(rx.try_recv().is_err());
    client.close(None);
}

#[tokio::test]
async fn reader_takes_reply_while_inbound_handler_awaits_outbound_call() {
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    let c = client.clone();
    client
        .bind_handler(Arc::new(move |request| {
            assert_eq!(request.request_id, "inbound");
            assert_eq!(request.operation_id, "op-inbound");
            assert_eq!(request.params.source.app_id, "code_te2");
            assert_eq!(request.params.context, Value::Null);
            assert_eq!(request.params.payload, json!({"sample":"text"}));
            let c = c.clone();
            Box::pin(async move {
                c.call(call(), "nested-operation".into(), Duration::from_secs(2))
                    .await
            })
        }))
        .unwrap();
    client
        .receive(incoming("inbound", "app.rpc.deliver"))
        .unwrap();
    let outbound = frame(&rx).await;
    assert_eq!(outbound.method.as_deref(), Some("app.rpc.call"));
    client.receive(response(&outbound)).unwrap();
    let reply = frame(&rx).await;
    assert_eq!(reply.id.as_deref(), Some("inbound"));
    assert_eq!(reply.result, Some(json!({"cwd":"/proof"})));
    client.close(None);
}

#[tokio::test]
async fn wrong_origin_target_version_and_service_metadata_never_invoke() {
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    client
        .bind_handler(Arc::new(|_| panic!("must reject before domain access")))
        .unwrap();
    for variant in 0..3 {
        let mut request = incoming("bad", "app.rpc.deliver");
        match variant {
            0 => request.origin_name = "forged".into(),
            1 => request.target_name = Some("other".into()),
            _ => request.protocol_version = 2,
        }
        assert!(client.receive(request).is_err());
    }
    let mut request = incoming("bad-correlation", "app.rpc.deliver");
    request.correlation_id = None;
    client.receive(request).unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "protocol.correlation");
    let mut request = incoming("bad-params", "app.rpc.deliver");
    request.params.as_mut().unwrap()["source"]["forged"] = json!(true);
    client.receive(request).unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "protocol.params");
    client.close(None);
}

#[tokio::test]
async fn inbound_admission_is_held_until_handler_finishes() {
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    client
        .bind_handler(Arc::new(|_| Box::pin(std::future::pending())))
        .unwrap();
    for id in 0..MAX_PENDING {
        client
            .receive(incoming(&format!("request-{id}"), "app.rpc.deliver"))
            .unwrap();
    }
    tokio::task::yield_now().await;
    assert_eq!(client.incoming_available(), 0);
    client
        .receive(incoming("overflow", "app.rpc.deliver"))
        .unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "pipe.busy");
    client.close(None);
    tokio::time::timeout(Duration::from_secs(2), async {
        while client.incoming_available() != MAX_PENDING {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn outbound_limits_timeout_cancellation_and_close_release_waiters() {
    let (client, rx) = fixture();
    let mut tasks = Vec::new();
    for id in 0..MAX_PENDING {
        let c = client.clone();
        tasks.push(tokio::spawn(async move {
            c.call(call(), format!("op-{id}"), Duration::from_secs(2))
                .await
        }));
        frame(&rx).await;
    }
    assert_eq!(
        client
            .call(call(), "overflow".into(), Duration::from_secs(1))
            .await
            .unwrap_err()
            .code,
        "pipe.busy"
    );
    tasks.pop().unwrap().abort();
    tokio::time::timeout(Duration::from_secs(2), async {
        while client.pending_count() == MAX_PENDING {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    client.close(None);
    for task in tasks {
        assert_eq!(task.await.unwrap().unwrap_err().code, "pipe.closed");
    }
    assert_eq!(client.pending_count(), 0);
    let (client, rx) = fixture();
    assert_eq!(
        client
            .call(call(), "timeout".into(), Duration::from_millis(1))
            .await
            .unwrap_err()
            .code,
        "pipe.timeout"
    );
    let request = frame(&rx).await;
    client.receive(response(&request)).unwrap();
    assert_eq!(client.pending_count(), 0);
    assert!(rx.try_recv().is_err());
    client.close(None);
}

#[tokio::test]
async fn writer_is_count_and_byte_bounded_and_releases_reservations() {
    let (client, rx) = fixture();
    let mut message = Envelope::outbound(Kind::Notification);
    message.params = Some(json!({"text":"x".repeat(runtime::MAX_OUTPUT_BYTES - 1024)}));
    client.enqueue(&message, None).unwrap();
    client.enqueue(&message, None).unwrap();
    assert_eq!(
        client.enqueue(&message, None).unwrap_err().code,
        "pipe.busy"
    );
    drop(rx.recv().unwrap());
    drop(rx.recv().unwrap());
    assert_eq!(client.writer_available(), WRITER_BYTES);
    message.params = None;
    for _ in 0..WRITER_FRAMES {
        client.enqueue(&message, None).unwrap();
    }
    assert_eq!(
        client.enqueue(&message, None).unwrap_err().code,
        "pipe.busy"
    );
    client.close(None);
    drop(rx);
    assert_eq!(client.writer_available(), WRITER_BYTES);
}

#[tokio::test]
async fn oversized_requests_and_writer_failure_do_not_replay() {
    let (client, rx) = fixture();
    let mut request = call();
    request.payload = json!({"text":"x".repeat(65536)});
    assert_eq!(
        client
            .call(request, "large".into(), Duration::from_secs(1))
            .await
            .unwrap_err()
            .code,
        "pipe.payloadTooLarge"
    );
    assert!(rx.try_recv().is_err());
    assert_eq!(client.pending_count(), 0);
    struct Broken;
    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let (ack, result) = oneshot::channel();
    client
        .enqueue(&Envelope::outbound(Kind::Notification), Some(ack))
        .unwrap();
    write_output(Broken, rx, client.clone());
    assert!(result.await.unwrap().is_err());
    assert!(client.is_closed());
    assert!(client.failure().unwrap().contains("writer failed"));
}

#[tokio::test]
async fn readiness_acknowledges_one_protocol_frame_and_is_single_use() {
    struct Output(Arc<std::sync::Mutex<Vec<u8>>>);
    impl Write for Output {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let (client, rx) = fixture();
    let output = Arc::new(std::sync::Mutex::new(Vec::new()));
    let writer = client.clone();
    let bytes = output.clone();
    let thread = thread::spawn(move || write_output(Output(bytes), rx, writer));
    let pipe = FrameworkPipe {
        closed: client.subscribe(),
        client: client.clone(),
    };
    pipe.serving_ready().await.unwrap();
    assert!(
        pipe.serving_ready()
            .await
            .unwrap_err()
            .to_string()
            .contains("alreadyReady")
    );
    let ready: Value = rmp_serde::from_slice(&output.lock().unwrap()).unwrap();
    assert_eq!(
        ready,
        json!({"jsonrpc":"2.0","protocolVersion":1,"kind":"notification",
        "originNid":0,"originName":"als-rs","targetNid":1,"targetName":"framework.rust",
        "method":"app.readiness","params":{"status":"ready","phase":"serving"}})
    );
    drop(pipe);
    thread.join().unwrap();
    assert!(client.is_closed());
}

#[tokio::test]
async fn oversized_handler_reply_reports_error_without_reexecution() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let count = calls.clone();
    client
        .bind_handler(Arc::new(move |_| {
            count.fetch_add(1, Ordering::SeqCst);
            Box::pin(async { Ok(json!({"text":"x".repeat(runtime::MAX_OUTPUT_BYTES)})) })
        }))
        .unwrap();
    client
        .receive(incoming("large", "app.rpc.deliver"))
        .unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "pipe.replyTooLarge");
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(!client.is_closed());
    assert!(rx.try_recv().is_err());
    client.close(None);
}

#[tokio::test]
async fn close_breaks_handler_reference_cycles_and_binding_is_one_time() {
    let (client, rx) = fixture();
    let c = client.clone();
    client
        .bind_handler(Arc::new(move |_| {
            let _owned = c.clone();
            Box::pin(async { Ok(Value::Null) })
        }))
        .unwrap();
    assert_eq!(
        client
            .bind_handler(Arc::new(|_| Box::pin(async { Ok(Value::Null) })))
            .unwrap_err()
            .code,
        "pipe.alreadyBound"
    );
    let weak = Arc::downgrade(&client.inner);
    client.close(None);
    drop(client);
    drop(rx);
    tokio::time::timeout(Duration::from_secs(2), async {
        while weak.upgrade().is_some() {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn control_handler_deadline_is_bounded_and_never_replayed() {
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    client
        .bind_handler(Arc::new(|_| Box::pin(std::future::pending())))
        .unwrap();
    client
        .receive(incoming("timeout", "app.rpc.deliver"))
        .unwrap();
    let reply = tokio::time::timeout(Duration::from_secs(12), async {
        loop {
            if let Ok(frame) = rx.try_recv() {
                break rmp_serde::from_slice::<Envelope>(&frame.bytes).unwrap();
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert_eq!(reply.error.unwrap().code, "pipe.handlerTimeout");
    assert!(rx.try_recv().is_err());
    client.close(None);
}

#[tokio::test]
async fn concurrent_replies_can_arrive_in_reverse_order() {
    let (client, rx) = fixture();
    let a = client.clone();
    let b = client.clone();
    let first =
        tokio::spawn(async move { a.call(call(), "first".into(), Duration::from_secs(2)).await });
    let second = tokio::spawn(async move {
        b.call(call(), "second".into(), Duration::from_secs(2))
            .await
    });
    let requests = [frame(&rx).await, frame(&rx).await];
    assert_ne!(requests[0].id, requests[1].id);
    for request in requests.iter().rev() {
        let mut reply = response(request);
        reply.result = Some(json!({"operation":request.op_id}));
        client.receive(reply).unwrap();
    }
    assert_eq!(first.await.unwrap().unwrap(), json!({"operation":"first"}));
    assert_eq!(
        second.await.unwrap().unwrap(),
        json!({"operation":"second"})
    );
    assert_eq!(client.pending_count(), 0);
    client.close(None);
}

fn integration_state(client: FrameworkClient, root: &std::path::Path) -> crate::state::AppState {
    use crate::config::{
        AdapterConfig, FrameworkShellConfig, ServerConfig, SocketIoSerializer, TranscriptTransport,
    };
    let mut state = crate::state::AppState::new(ServerConfig {
        host: "127.0.0.1".into(),
        port: 0,
        extensions_dir: root.join("extensions"),
        roots: als_dto::RuntimeRoots {
            data_dir: root.join("data"),
            cache_dir: root.join("cache"),
            config_dir: root.join("config"),
            static_dir: root.join("static"),
        },
        adapters: AdapterConfig {
            python_bin: "python".into(),
        },
        framework_shells: FrameworkShellConfig::default(),
        socketio_serializer: SocketIoSerializer::Msgpack,
        transcript_transport: TranscriptTransport::Stream,
    });
    state.framework_pipe = Some(client);
    state
}

fn integration_root() -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "als-sidebar-pipe-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ))
}

#[tokio::test]
async fn production_sidebar_facade_uses_owned_pipe_and_closed_pipe_never_opens_socket() {
    let (client, rx) = fixture();
    let root = integration_root();
    let state = integration_state(client.clone(), &root);
    let (_, io) = socketioxide::SocketIo::builder().build_layer();
    crate::sidebar_ipc::bind_framework_pipe(&io, &state).unwrap();
    let task_state = state.clone();
    let task =
        tokio::spawn(async move { crate::sidebar_ipc::recheck_status(&io, &task_state).await });
    let request = frame(&rx).await;
    assert_eq!(request.method.as_deref(), Some("app.rpc.call"));
    assert_eq!(request.params.as_ref().unwrap()["method"], "cwd.get");
    client.receive(response(&request)).unwrap();
    let result = task.await.unwrap();
    assert_eq!(result["cwd"], "/proof");
    assert_eq!(
        state.host_ui.snapshot().unwrap().project_root.as_deref(),
        Some("/proof")
    );
    client.close(None);
    let (_, io) = socketioxide::SocketIo::builder().build_layer();
    let result = tokio::time::timeout(
        Duration::from_millis(250),
        crate::sidebar_ipc::recheck_status(&io, &state),
    )
    .await
    .unwrap();
    assert_eq!(result["connected"], false);
    assert!(rx.try_recv().is_err());
    let _ = std::fs::remove_dir_all(root);
}

#[tokio::test]
async fn bound_sidebar_handler_targets_explicit_conversation_and_rejects_wrong_app() {
    use crate::conversation_store::CreateConversationRequest;
    let (client, rx) = fixture();
    let root = integration_root();
    let state = integration_state(client.clone(), &root);
    for name in ["first", "second"] {
        state
            .conversations
            .create(CreateConversationRequest {
                conversation_id: Some(name.into()),
                ..Default::default()
            })
            .unwrap();
    }
    let (_, io) = socketioxide::SocketIo::builder().build_layer();
    crate::sidebar_ipc::bind_framework_pipe(&io, &state).unwrap();
    client.mark_ready().unwrap();
    let mut request = incoming("mention", "app.rpc.deliver");
    request.params = Some(
        json!({"service":"sidebar","version":1,"method":"mention", "context":null,
        "source":{"appId":"code_te2","shellId":"owned"},
        "payload":{"path":"/project/file.rs","lineNo":4,"conversation_id":"second"}}),
    );
    client.receive(request.clone()).unwrap();
    let receipt = frame(&rx).await;
    assert_eq!(receipt.op_id, request.op_id);
    let receipt = receipt.result.unwrap();
    assert_eq!(receipt["conversation_id"], "second");
    assert_eq!(receipt["queued"], true);
    assert!(receipt["operation_id"].is_string());
    assert!(
        state
            .conversations
            .load_meta_if_exists("first")
            .unwrap()
            .unwrap()
            .draft
            .is_none()
    );
    let draft = state
        .conversations
        .load_meta_if_exists("second")
        .unwrap()
        .unwrap()
        .draft
        .unwrap();
    assert!(draft.contains("/project/file.rs"));
    request.id = Some("wrong-app".into());
    request.correlation_id = request.id.clone();
    request.params.as_mut().unwrap()["source"]["appId"] = json!("terminal");
    client.receive(request).unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "sidebar.rejected");
    assert_eq!(
        state
            .conversations
            .load_meta_if_exists("second")
            .unwrap()
            .unwrap()
            .draft
            .unwrap(),
        draft
    );
    client.close(None);
    let _ = std::fs::remove_dir_all(root);
}

#[tokio::test]
async fn per_call_bulk_receipt_budget_preserves_full_utf8_without_changing_control_default() {
    let (client, rx) = fixture();
    let text = "😀".repeat(300_000);
    let local = client.clone();
    let task = tokio::spawn(async move {
        local
            .call_with_limits(
                call(),
                "bulk".into(),
                Duration::from_secs(2),
                CallLimits {
                    reply_bytes: 8 * 1024 * 1024,
                    ..Default::default()
                },
            )
            .await
    });
    let request = frame(&rx).await;
    let mut reply = response(&request);
    reply.result = Some(json!({"content":text}));
    client.receive(reply).unwrap();
    assert_eq!(task.await.unwrap().unwrap()["content"], text);
    let local = client.clone();
    let task = tokio::spawn(async move {
        local
            .call(call(), "control".into(), Duration::from_secs(2))
            .await
    });
    let request = frame(&rx).await;
    let mut reply = response(&request);
    reply.result = Some(json!({"content":text}));
    client.receive(reply).unwrap();
    assert_eq!(task.await.unwrap().unwrap_err().code, "pipe.replyTooLarge");
    client.close(None);
}

#[tokio::test]
async fn bulk_request_policy_rejects_invalid_limits_before_any_enqueue() {
    let (client, rx) = fixture();
    let mut body = call();
    body.payload = json!({"text":"😀".repeat(512)});
    let error = client
        .call_with_limits(
            body,
            "oversize".into(),
            Duration::from_secs(2),
            CallLimits {
                request_bytes: 1024,
                reply_bytes: 1024,
            },
        )
        .await
        .unwrap_err();
    assert_eq!(error.code, "pipe.payloadTooLarge");
    let error = client
        .call_with_limits(
            call(),
            "invalid".into(),
            Duration::from_secs(2),
            CallLimits {
                request_bytes: 8 * 1024 * 1024 + 1,
                reply_bytes: 1024,
            },
        )
        .await
        .unwrap_err();
    assert_eq!(error.code, "pipe.invalidCall");
    assert_eq!(client.pending_count(), 0);
    assert!(rx.try_recv().is_err());
    client.close(None);
}

#[tokio::test]
async fn input_control_size_and_total_bytes_are_checked_before_queueing() {
    let (client, rx) = fixture();
    client.mark_ready().unwrap();
    client
        .bind_handler(Arc::new(|_| Box::pin(std::future::pending())))
        .unwrap();
    let mut large = incoming("oversized", "app.rpc.deliver");
    large.params.as_mut().unwrap()["payload"] = json!({"text":"x".repeat(65536)});
    client.receive(large).unwrap();
    assert_eq!(frame(&rx).await.error.unwrap().code, "pipe.payloadTooLarge");
    assert_eq!(client.incoming_available(), MAX_PENDING);
    for id in 0..3 {
        let mut large = incoming(&format!("metadata-{id}"), "app.rpc.deliver");
        large.workspace_root = Some("x".repeat(900_000));
        client.receive(large).unwrap();
    }
    assert_eq!(frame(&rx).await.error.unwrap().code, "pipe.busy");
    assert_eq!(client.incoming_available(), MAX_PENDING - 2);
    client.close(None);
    tokio::time::timeout(Duration::from_secs(2), async {
        while client.incoming_bytes_available() != runtime::INCOMING_BYTES {
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
}
