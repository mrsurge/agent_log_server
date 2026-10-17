//! Framework stdout is concatenated MessagePack, NOT adapter length-prefixed I/O.
use als_dto::APP_ID;
use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    io::{self, Cursor, Read, Write},
    sync::{Arc, Mutex},
    thread,
};
use tokio::sync::oneshot;

const MAX_FRAME_BYTES: usize = 32 * 1024 * 1024;

pub fn take_stdio_flag(args: &mut Vec<OsString>) -> bool {
    let enabled = args.iter().any(|arg| arg == "--stdio");
    args.retain(|arg| arg != "--stdio");
    enabled
}

pub struct FrameworkPipe {
    output: Arc<Mutex<io::Stdout>>,
    closed: oneshot::Receiver<Result<()>>,
}

impl FrameworkPipe {
    pub fn start() -> Result<Self> {
        let output = Arc::new(Mutex::new(io::stdout()));
        let writer = output.clone();
        let (tx, closed) = oneshot::channel();
        // Tokio stdin's blocking read can prevent runtime exit on SIGTERM.
        // This process-owned OS thread is detached, not a Tokio blocking task.
        thread::Builder::new()
            .name("als-framework-pipe".into())
            .spawn(move || {
                let _ = tx.send(read_input(io::stdin().lock(), writer));
            })?;
        Ok(Self { output, closed })
    }

    pub fn serving_ready(&mut self) -> Result<()> {
        match self.closed.try_recv() {
            Ok(result) => {
                result?;
                bail!("framework stdin closed before serving");
            }
            Err(oneshot::error::TryRecvError::Closed) => bail!("framework pipe reader stopped"),
            Err(oneshot::error::TryRecvError::Empty) => {}
        }
        write_frame(&self.output, &ready_envelope())
    }

    pub async fn closed(&mut self) -> Result<()> {
        (&mut self.closed)
            .await
            .context("framework pipe reader stopped")?
    }
}

fn ready_envelope() -> Value {
    json!({"jsonrpc":"2.0", "protocolVersion":1, "kind":"notification",
        "method":"app.readiness", "originNid":0, "originName":APP_ID,
        "targetNid":1, "targetName":"framework.rust",
        "params":{"status":"ready", "phase":"serving"}})
}

fn write_frame<W: Write>(output: &Mutex<W>, value: &Value) -> Result<()> {
    let bytes = rmp_serde::to_vec_named(value)?;
    ensure!(
        bytes.len() <= MAX_FRAME_BYTES,
        "framework frame exceeds byte limit"
    );
    let mut writer = output
        .lock()
        .map_err(|_| anyhow::anyhow!("framework stdout lock poisoned"))?;
    writer.write_all(&bytes)?;
    writer.flush()?;
    Ok(())
}

fn handle_envelope<W: Write>(value: Value, output: &Mutex<W>) -> Result<()> {
    ensure!(
        value.get("jsonrpc").and_then(Value::as_str) == Some("2.0")
            && value.get("protocolVersion").and_then(Value::as_u64) == Some(1),
        "invalid framework pipe version"
    );
    let kind = value
        .get("kind")
        .and_then(Value::as_str)
        .context("missing framework kind")?;
    ensure!(
        matches!(
            kind,
            "hello" | "request" | "response" | "notification" | "progress" | "cancel" | "error"
        ),
        "invalid framework pipe kind"
    );
    let origin = value
        .get("originName")
        .and_then(Value::as_str)
        .context("missing framework origin")?;
    let nid = value
        .get("originNid")
        .and_then(Value::as_u64)
        .context("missing framework origin NID")?;
    if kind == "request" {
        let id = value
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .context("missing request id")?;
        ensure!(
            value
                .get("method")
                .and_then(Value::as_str)
                .is_some_and(|method| !method.is_empty()),
            "missing request method"
        );
        // Only readiness is implemented here. Do not fake service support or
        // silently reroute unknown requests over the Sidebar socket.
        write_frame(
            output,
            &json!({"jsonrpc":"2.0", "protocolVersion":1, "kind":"error",
            "id":id, "originNid":0, "originName":APP_ID, "targetNid":nid, "targetName":origin,
            "error":{"code":"method_not_found", "message":"ALS framework method not supported", "retryable":false}}),
        )?;
    }
    Ok(())
}

fn read_input<R: Read, W: Write>(mut input: R, output: Arc<Mutex<W>>) -> Result<()> {
    let mut pending = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let count = match input.read(&mut chunk) {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            other => other?,
        };
        if count == 0 {
            ensure!(pending.is_empty(), "truncated framework MessagePack frame");
            return Ok(());
        }
        pending.extend_from_slice(&chunk[..count]);
        while !pending.is_empty() {
            ensure!(
                matches!(pending[0], 0x80..=0x8f | 0xde | 0xdf),
                "expected framework envelope map"
            );
            let mut cursor = Cursor::new(&pending[..pending.len().min(MAX_FRAME_BYTES)]);
            match Value::deserialize(&mut rmp_serde::Deserializer::new(&mut cursor)) {
                Ok(value) => {
                    let consumed = cursor.position() as usize;
                    handle_envelope(value, &output)?;
                    pending.drain(..consumed);
                }
                Err(
                    rmp_serde::decode::Error::InvalidMarkerRead(ref error)
                    | rmp_serde::decode::Error::InvalidDataRead(ref error),
                ) if error.kind() == io::ErrorKind::UnexpectedEof
                    && pending.len() < MAX_FRAME_BYTES =>
                {
                    break;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
    fn ready_is_one_unprefixed_messagepack_envelope() {
        let output = Mutex::new(Vec::new());
        write_frame(&output, &ready_envelope()).unwrap();
        let bytes = output.lock().unwrap();
        assert!((0x80..=0x8f).contains(&bytes[0]));
        let value: Value = rmp_serde::from_slice(&bytes).unwrap();
        assert_eq!(value["method"], "app.readiness");
        assert_eq!(
            value["params"],
            json!({"status":"ready", "phase":"serving"})
        );
        assert_eq!(value["targetName"], "framework.rust");
    }
    #[test]
    fn concatenated_requests_receive_routed_method_errors() {
        let value = json!({"jsonrpc":"2.0", "protocolVersion":1, "kind":"request",
            "id":"test", "method":"unknown", "originNid":1, "originName":"framework.rust"});
        let frame = rmp_serde::to_vec_named(&value).unwrap();
        let mut bytes = frame.clone();
        bytes.extend(frame);
        let output = Arc::new(Mutex::new(Vec::new()));
        read_input(Cursor::new(bytes), output.clone()).unwrap();
        let bytes = output.lock().unwrap();
        let mut cursor = Cursor::new(bytes.as_slice());
        for _ in 0..2 {
            let value = Value::deserialize(&mut rmp_serde::Deserializer::new(&mut cursor)).unwrap();
            assert_eq!(value["kind"], "error");
            assert_eq!(value["id"], "test");
            assert_eq!(value["targetName"], "framework.rust");
            assert_eq!(value["error"]["code"], "method_not_found");
            assert_eq!(value["error"]["retryable"], false);
        }
        assert_eq!(cursor.position() as usize, bytes.len());
    }
    #[test]
    fn malformed_or_truncated_input_is_fatal() {
        for bytes in [vec![0x81], vec![0xc1], b"log output".to_vec()] {
            assert!(read_input(Cursor::new(bytes), Arc::new(Mutex::new(Vec::new()))).is_err());
        }
    }
}
