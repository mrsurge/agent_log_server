use anyhow::{Result, bail, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const ENV: &str = "ALS_RS_ADAPTER_CODEC";
pub const MAX_FRAME_BYTES: usize = 32 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Codec {
    Messagepack,
    Json,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn shared_fixture_and_every_split() {
        let hex = include_str!("../../../../tests/fixtures/adapter_frame.msgpack.hex").trim();
        let bytes: Vec<u8> = (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
            .collect();
        let expected = json!({"jsonrpc":"2.0","id":7,"result":{"text":"one\ntwo","ok":true}});
        let observed = ferrous_framework::msgpack_observation::decode_frame(&bytes, 1024)
            .unwrap()
            .unwrap();
        assert_eq!(observed.value, expected);
        assert_eq!(observed.consumed, bytes.len());
        for split in 0..=bytes.len() {
            let mut decoder = Decoder::new(Codec::Messagepack);
            let mut frames = decoder.feed(&bytes[..split]).unwrap();
            frames.extend(decoder.feed(&[&bytes[split..], &bytes].concat()).unwrap());
            decoder.finish().unwrap();
            assert_eq!(frames, vec![expected.clone(), expected.clone()]);
        }
    }

    #[test]
    fn codecs_roundtrip_and_reject_bad_frames() {
        let expected = json!({"method":"event.live","params":{"text":"\n\r unicode: \u{2603}"}});
        for codec in [Codec::Messagepack, Codec::Json] {
            let bytes = codec.encode(&expected).unwrap();
            let mut decoder = Decoder::new(codec);
            assert_eq!(decoder.feed(&bytes).unwrap(), vec![expected.clone()]);
            decoder.finish().unwrap();
            let mut decoder = Decoder::new(codec);
            decoder.feed(&bytes[..bytes.len() - 1]).unwrap();
            assert!(decoder.finish().is_err());
        }
        assert!(Decoder::new(Codec::Messagepack).feed(&[0xc0]).is_err());
        assert!(
            Decoder::new(Codec::Messagepack)
                .feed(&[0x81, 0xa1, b'x', 0xc1])
                .is_err()
        );
    }

    #[test]
    fn python_decodes_rust_frames_and_encodes_response() {
        use std::{
            io::Write,
            process::{Command, Stdio},
        };
        let expected = json!({"jsonrpc":"2.0","id":7,"result":{"text":"one\ntwo","ok":true}});
        let mut child = Command::new("python")
            .args(["-c", "import sys; from agent_log_server_rs.codec import decode_frame, encode_frame; sys.stdout.buffer.write(encode_frame(decode_frame(sys.stdin.buffer.read(), 'messagepack'), 'messagepack'))"])
            .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/../../.."))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(&Codec::Messagepack.encode(&expected).unwrap())
            .unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success());
        let mut decoder = Decoder::new(Codec::Messagepack);
        assert_eq!(decoder.feed(&output.stdout).unwrap(), vec![expected]);
        decoder.finish().unwrap();
    }
}

impl Codec {
    pub fn from_env() -> Result<Self> {
        match std::env::var(ENV).as_deref().unwrap_or("messagepack") {
            "messagepack" => Ok(Self::Messagepack),
            "json" => Ok(Self::Json),
            value => bail!("invalid {ENV}: {value}"),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Messagepack => "messagepack",
            Self::Json => "json",
        }
    }

    pub fn encode(self, value: &impl Serialize) -> Result<Vec<u8>> {
        let mut bytes = match self {
            Self::Messagepack => rmp_serde::to_vec_named(value)?,
            Self::Json => serde_json::to_vec(value)?,
        };
        ensure!(
            bytes.len() <= MAX_FRAME_BYTES,
            "adapter frame exceeds byte limit"
        );
        if self == Self::Json {
            bytes.push(b'\n');
        }
        Ok(bytes)
    }
}

pub struct Decoder {
    codec: Codec,
    pending: Vec<u8>,
}

impl Decoder {
    pub fn new(codec: Codec) -> Self {
        Self {
            codec,
            pending: Vec::new(),
        }
    }

    pub fn feed(&mut self, chunk: &[u8]) -> Result<Vec<Value>> {
        let mut frames = Vec::new();
        for part in chunk.chunks(65536) {
            self.pending.extend_from_slice(part);
            let mut consumed = 0;
            while consumed < self.pending.len() {
                let bytes = &self.pending[consumed..];
                let (value, size) = match self.codec {
                    Codec::Json => {
                        let Some(end) = bytes.iter().position(|b| *b == b'\n') else {
                            ensure!(
                                bytes.len() <= MAX_FRAME_BYTES,
                                "adapter frame exceeds byte limit"
                            );
                            break;
                        };
                        ensure!(end <= MAX_FRAME_BYTES, "adapter frame exceeds byte limit");
                        if bytes[..end].iter().all(u8::is_ascii_whitespace) {
                            consumed += end + 1;
                            continue;
                        }
                        (serde_json::from_slice(&bytes[..end])?, end + 1)
                    }
                    Codec::Messagepack => {
                        ensure!(
                            matches!(bytes[0], 0x80..=0x8f | 0xde | 0xdf),
                            "expected adapter envelope map"
                        );
                        let mut cursor =
                            std::io::Cursor::new(&bytes[..bytes.len().min(MAX_FRAME_BYTES)]);
                        let result =
                            Value::deserialize(&mut rmp_serde::Deserializer::new(&mut cursor));
                        match result {
                            Ok(value) => (value, cursor.position() as usize),
                            Err(
                                rmp_serde::decode::Error::InvalidMarkerRead(ref e)
                                | rmp_serde::decode::Error::InvalidDataRead(ref e),
                            ) if e.kind() == std::io::ErrorKind::UnexpectedEof
                                && bytes.len() < MAX_FRAME_BYTES =>
                            {
                                break;
                            }
                            Err(error) => return Err(error.into()),
                        }
                    }
                };
                ensure!(value.is_object(), "expected adapter envelope map");
                frames.push(value);
                consumed += size;
            }
            self.pending.drain(..consumed);
        }
        Ok(frames)
    }

    pub fn finish(&self) -> Result<()> {
        ensure!(self.pending.is_empty(), "truncated adapter frame at EOF");
        Ok(())
    }
}
