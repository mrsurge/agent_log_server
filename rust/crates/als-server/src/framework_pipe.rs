//! Framework stdout is concatenated MessagePack, NOT adapter length-prefixed I/O.
mod protocol;
mod runtime;
pub use protocol::PipeError;
pub use runtime::Client as FrameworkClient;

use anyhow::{Context, Result, bail, ensure};
use protocol::{Envelope, Kind};
use serde::Deserialize;
use serde_json::json;
use std::{
    ffi::OsString,
    io::{self, Cursor, Read, Write},
    sync::mpsc,
    thread,
};
use tokio::sync::{oneshot, watch};

const MAX_FRAME_BYTES: usize = 32 * 1024 * 1024;

pub fn take_stdio_flag(args: &mut Vec<OsString>) -> bool {
    let enabled = args.iter().any(|arg| arg == "--stdio");
    args.retain(|arg| arg != "--stdio");
    enabled
}

pub struct FrameworkPipe {
    client: FrameworkClient,
    closed: watch::Receiver<bool>,
}

impl FrameworkPipe {
    pub fn start() -> Result<Self> {
        let (sender, receiver) = mpsc::sync_channel(runtime::WRITER_FRAMES);
        let client = FrameworkClient::new(sender);
        let writer = client.clone();
        if let Err(error) = thread::Builder::new()
            .name("als-framework-writer".into())
            .spawn(move || {
                write_output(io::stdout().lock(), receiver, writer);
            })
        {
            client.close(Some(error.to_string()));
            return Err(error.into());
        }
        let reader = client.clone();
        if let Err(error) = thread::Builder::new()
            .name("als-framework-pipe".into())
            .spawn(move || {
                // Detached OS reader: a blocked stdin read must not pin Tokio shutdown.
                let result = read_input(io::stdin().lock(), |frame| {
                    reader.receive(frame).map_err(Into::into)
                });
                reader.close(result.err().map(|error| error.to_string()));
            })
        {
            client.close(Some(error.to_string()));
            return Err(error.into());
        }
        Ok(Self {
            closed: client.subscribe(),
            client,
        })
    }

    pub fn client(&self) -> FrameworkClient {
        self.client.clone()
    }

    pub async fn serving_ready(&self) -> Result<()> {
        let mut closed = self.client.subscribe();
        self.client.mark_ready()?;
        let mut ready = Envelope::outbound(Kind::Notification);
        ready.method = Some("app.readiness".into());
        ready.params = Some(json!({"status":"ready", "phase":"serving"}));
        let (tx, rx) = oneshot::channel();
        self.client.enqueue(&ready, Some(tx))?;
        tokio::select! {
            ack = rx => ack.context("framework writer stopped before readiness")??,
            _ = closed.changed() => bail!("framework pipe closed before readiness"),
        }
        Ok(())
    }

    pub async fn closed(&mut self) -> Result<()> {
        while !*self.closed.borrow_and_update() {
            self.closed
                .changed()
                .await
                .context("framework pipe stopped")?;
        }
        match self.client.failure() {
            Some(error) => bail!("{error}"),
            None => Ok(()),
        }
    }
}

impl Drop for FrameworkPipe {
    fn drop(&mut self) {
        self.client.close(None);
    }
}

fn write_output<W: Write>(
    mut output: W,
    input: mpsc::Receiver<runtime::Frame>,
    client: FrameworkClient,
) {
    for frame in input {
        if client.is_closed() {
            return;
        }
        let result = output.write_all(&frame.bytes).and_then(|()| output.flush());
        match result {
            Ok(()) => {
                if let Some(ack) = frame.ack {
                    let _ = ack.send(Ok(()));
                }
            }
            Err(error) => {
                client.close(Some(format!("framework writer failed: {error}")));
                if let Some(ack) = frame.ack {
                    let _ = ack.send(Err(PipeError::new("pipe.write", &error.to_string())));
                }
                return;
            }
        }
    }
}

fn read_input<R: Read>(
    mut input: R,
    mut receive: impl FnMut(Envelope) -> Result<()>,
) -> Result<()> {
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
            match Envelope::deserialize(&mut rmp_serde::Deserializer::new(&mut cursor)) {
                Ok(value) => {
                    let consumed = cursor.position() as usize;
                    receive(value)?;
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
mod tests;
