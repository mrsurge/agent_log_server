use crate::{
    config::TranscriptTransport, conversation_store::TranscriptProjectionAction, state::AppState,
};
use axum::{
    Router,
    extract::{
        State, WebSocketUpgrade,
        ws::{Message, WebSocket},
    },
    response::IntoResponse,
    routing::get,
};
use flate2::{Compression, write::GzEncoder};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    io::Write,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;
use tracing::{debug, warn};

const PROTOCOL_VERSION: u8 = 1;
const FLAG_GZIP: u8 = 1;
const COMPRESSION_THRESHOLD_BYTES: usize = 32 * 1024;
const CLIENT_QUEUE_CAPACITY: usize = 256;
const MAX_CLIENT_ID_BYTES: usize = 160;
const MAX_KNOWN_CARDS: usize = 256;

const TAG_CLIENT_HELLO: u64 = 1;
const TAG_SERVER_HELLO: u64 = 2;
const TAG_WINDOW_REQUEST: u64 = 3;
const TAG_WINDOW_SNAPSHOT: u64 = 4;
const TAG_WINDOW_DELTA: u64 = 5;
const TAG_LIVE_EVENT: u64 = 6;
const TAG_ERROR: u64 = 255;

#[derive(Clone)]
pub struct TranscriptStreamHub {
    inner: Arc<Mutex<TranscriptStreamHubState>>,
    next_connection_id: Arc<AtomicU64>,
    server_epoch: Arc<str>,
}

#[derive(Default)]
struct TranscriptStreamHubState {
    clients: HashMap<String, TranscriptStreamClient>,
    conversation_sequences: HashMap<String, u64>,
}

struct TranscriptStreamClient {
    connection_id: u64,
    conversation_id: Option<String>,
    sender: mpsc::Sender<OutboundFrame>,
}

#[derive(Clone)]
struct OutboundFrame {
    value: Value,
    allow_compression: bool,
    terminal: bool,
}

impl Default for TranscriptStreamHub {
    fn default() -> Self {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        Self {
            inner: Arc::new(Mutex::new(TranscriptStreamHubState::default())),
            next_connection_id: Arc::new(AtomicU64::new(0)),
            server_epoch: Arc::from(format!("{}-{nanos}", std::process::id())),
        }
    }
}

impl TranscriptStreamHub {
    fn attach(
        &self,
        client_id: String,
        conversation_id: Option<String>,
    ) -> anyhow::Result<(
        u64,
        mpsc::Sender<OutboundFrame>,
        mpsc::Receiver<OutboundFrame>,
    )> {
        let connection_id = self.next_connection_id.fetch_add(1, Ordering::SeqCst) + 1;
        let (sender, receiver) = mpsc::channel(CLIENT_QUEUE_CAPACITY);
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("transcript stream hub lock poisoned"))?;
        if let Some(previous) = inner.clients.remove(&client_id) {
            let terminal = OutboundFrame {
                value: tagged_frame(
                    TAG_ERROR,
                    json!({
                        "code": "client_superseded",
                        "message": "A newer transcript stream connection replaced this client",
                        "fatal": true,
                    }),
                ),
                allow_compression: false,
                terminal: true,
            };
            if previous.sender.try_send(terminal.clone()).is_err() {
                tokio::spawn(async move {
                    let _ = previous.sender.send(terminal).await;
                });
            }
        }
        inner.clients.insert(
            client_id,
            TranscriptStreamClient {
                connection_id,
                conversation_id,
                sender: sender.clone(),
            },
        );
        Ok((connection_id, sender, receiver))
    }

    fn subscribe(
        &self,
        client_id: &str,
        connection_id: u64,
        conversation_id: String,
    ) -> anyhow::Result<()> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| anyhow::anyhow!("transcript stream hub lock poisoned"))?;
        let client = inner
            .clients
            .get_mut(client_id)
            .filter(|client| client.connection_id == connection_id)
            .ok_or_else(|| anyhow::anyhow!("transcript stream client is no longer current"))?;
        client.conversation_id = Some(conversation_id);
        Ok(())
    }

    fn detach(&self, client_id: &str, connection_id: u64) {
        let Ok(mut inner) = self.inner.lock() else {
            return;
        };
        if inner
            .clients
            .get(client_id)
            .is_some_and(|client| client.connection_id == connection_id)
        {
            inner.clients.remove(client_id);
        }
    }

    pub fn publish(&self, method: &str, params: Value) {
        let Some(conversation_id) = params
            .get("conversation_id")
            .or_else(|| params.get("conversationId"))
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
        else {
            return;
        };
        let Ok(mut inner) = self.inner.lock() else {
            warn!("transcript stream hub lock poisoned during publish");
            return;
        };
        let sequence = inner
            .conversation_sequences
            .entry(conversation_id.clone())
            .and_modify(|value| *value = value.saturating_add(1))
            .or_insert(1)
            .to_owned();
        let frame = OutboundFrame {
            value: tagged_frame(
                TAG_LIVE_EVENT,
                json!({
                    "sequence": sequence,
                    "method": method,
                    "params": params,
                }),
            ),
            allow_compression: true,
            terminal: false,
        };
        let mut stale = Vec::new();
        let mut overflowed = Vec::new();
        for (client_id, client) in &inner.clients {
            if client.conversation_id.as_deref() != Some(conversation_id.as_str()) {
                continue;
            }
            match client.sender.try_send(frame.clone()) {
                Ok(()) => {}
                Err(mpsc::error::TrySendError::Closed(_)) => stale.push(client_id.clone()),
                Err(mpsc::error::TrySendError::Full(_)) => {
                    stale.push(client_id.clone());
                    overflowed.push(client.sender.clone());
                }
            }
        }
        for client_id in stale {
            inner.clients.remove(&client_id);
        }
        drop(inner);
        for sender in overflowed {
            tokio::spawn(async move {
                let _ = sender
                    .send(OutboundFrame {
                        value: tagged_frame(
                            TAG_ERROR,
                            json!({
                                "code": "client_queue_overflow",
                                "message": "Transcript stream client fell behind and must reconnect",
                                "fatal": true,
                            }),
                        ),
                        allow_compression: false,
                        terminal: true,
                    })
                    .await;
            });
        }
    }

    fn sequence(&self, conversation_id: &str) -> u64 {
        self.inner
            .lock()
            .ok()
            .and_then(|inner| inner.conversation_sequences.get(conversation_id).copied())
            .unwrap_or(0)
    }
}

pub fn routes() -> Router<AppState> {
    Router::new().route("/ws/transcript", get(transcript_ws))
}

async fn transcript_ws(
    websocket: WebSocketUpgrade,
    State(state): State<AppState>,
) -> impl IntoResponse {
    websocket.on_upgrade(move |socket| handle_socket(socket, state))
}

async fn handle_socket(mut socket: WebSocket, state: AppState) {
    if state.config.transcript_transport != TranscriptTransport::Stream {
        let _ = send_direct_error(
            &mut socket,
            "transport_disabled",
            "Binary transcript streaming is disabled by ALS_RS_TRANSCRIPT_TRANSPORT",
        )
        .await;
        let _ = socket.close().await;
        return;
    }

    let Some(Ok(Message::Binary(bytes))) = socket.recv().await else {
        let _ = send_direct_error(
            &mut socket,
            "hello_required",
            "First frame must be binary hello",
        )
        .await;
        let _ = socket.close().await;
        return;
    };
    let hello = match decode_client_frame(&bytes).and_then(parse_hello) {
        Ok(hello) => hello,
        Err(error) => {
            let _ = send_direct_error(&mut socket, "invalid_hello", &error.to_string()).await;
            let _ = socket.close().await;
            return;
        }
    };
    let client_id = hello.client_id;
    let supports_gzip = hello.supports_gzip;
    let (connection_id, outbound_sender, mut outbound_receiver) = match state
        .transcript_streams
        .attach(client_id.clone(), hello.conversation_id)
    {
        Ok(value) => value,
        Err(error) => {
            let _ = send_direct_error(&mut socket, "attach_failed", &error.to_string()).await;
            let _ = socket.close().await;
            return;
        }
    };
    let _ = outbound_sender
        .send(OutboundFrame {
            value: tagged_frame(
                TAG_SERVER_HELLO,
                json!({
                    "protocol": PROTOCOL_VERSION,
                    "server_epoch": state.transcript_streams.server_epoch.as_ref(),
                    "client_id": client_id,
                    "gzip": supports_gzip,
                }),
            ),
            allow_compression: false,
            terminal: false,
        })
        .await;

    let (mut websocket_sender, mut websocket_receiver) = socket.split();
    let writer = tokio::spawn(async move {
        while let Some(frame) = outbound_receiver.recv().await {
            let encoded =
                match encode_server_frame(&frame.value, supports_gzip && frame.allow_compression) {
                    Ok(encoded) => encoded,
                    Err(error) => {
                        warn!(%error, "failed to encode transcript stream frame");
                        break;
                    }
                };
            if websocket_sender
                .send(Message::Binary(encoded.into()))
                .await
                .is_err()
            {
                break;
            }
            if frame.terminal {
                break;
            }
        }
    });

    while let Some(message) = websocket_receiver.next().await {
        let frame = match message {
            Ok(Message::Binary(bytes)) => match decode_client_frame(&bytes) {
                Ok(value) => value,
                Err(error) => {
                    let _ = send_error_frame(
                        &outbound_sender,
                        None,
                        "invalid_frame",
                        &error.to_string(),
                        false,
                    )
                    .await;
                    continue;
                }
            },
            Ok(Message::Ping(_)) | Ok(Message::Pong(_)) => continue,
            Ok(Message::Close(_)) | Err(_) => break,
            Ok(Message::Text(_)) => {
                let _ = send_error_frame(
                    &outbound_sender,
                    None,
                    "binary_required",
                    "Transcript WebSocket accepts binary MessagePack frames only",
                    false,
                )
                .await;
                continue;
            }
        };
        let request = match parse_window_request(frame) {
            Ok(request) => request,
            Err(error) => {
                let _ = send_error_frame(
                    &outbound_sender,
                    None,
                    "invalid_request",
                    &error.to_string(),
                    false,
                )
                .await;
                continue;
            }
        };
        if state
            .transcript_streams
            .subscribe(&client_id, connection_id, request.conversation_id.clone())
            .is_err()
        {
            break;
        }
        let request_id = request.request_id;
        let conversation_id = request.conversation_id;
        let conversations = state.conversations.clone();
        let turn_projections = state.turn_projections.clone();
        let projection_client_id = client_id.clone();
        let projection_conversation_id = conversation_id.clone();
        let projected = tokio::task::spawn_blocking(move || {
            turn_projections.project_transcript_transfer_with_live(
                &conversations,
                &projection_client_id,
                &projection_conversation_id,
                request.action,
                request.window_cards,
                request.shift_cards,
                request.max_bytes,
                request.requested_start,
                &request.known_cards,
            )
        })
        .await;
        let (projection, live_projection) = match projected {
            Ok(Ok(value)) => value,
            Ok(Err(error)) => {
                let _ = send_error_frame(
                    &outbound_sender,
                    Some(&request_id),
                    "projection_failed",
                    &error.to_string(),
                    false,
                )
                .await;
                continue;
            }
            Err(error) => {
                let _ = send_error_frame(
                    &outbound_sender,
                    Some(&request_id),
                    "projection_task_failed",
                    &error.to_string(),
                    false,
                )
                .await;
                continue;
            }
        };
        let stream_sequence = state.transcript_streams.sequence(&conversation_id);
        let tag = if projection.snapshot {
            TAG_WINDOW_SNAPSHOT
        } else {
            TAG_WINDOW_DELTA
        };
        let card_count = projection.ordered_cards.len();
        let upsert_count = projection.cards.len();
        let response = tagged_frame(
            tag,
            json!({
                "request_id": request_id,
                "conversation_id": conversation_id,
                "projection": {
                    "unit": "transcript_card",
                    "start_card": projection.start_card,
                    "end_card": projection.end_card,
                    "total_cards": projection.total_cards,
                    "window_cards": projection.window_cards,
                    "shift_cards": projection.shift_cards,
                    "card_count": card_count,
                    "at_start": projection.at_start,
                    "at_tail": projection.at_tail,
                    "revision": projection.revision,
                },
                "ordered_cards": projection.ordered_cards,
                "removed_card_ids": projection.removed_card_ids,
                "cards": projection.cards,
                "runtime_state": projection.runtime_state,
                "live_projection": live_projection,
                "stream_sequence": stream_sequence,
                "frame": {
                    "format": if projection.snapshot { "card_recipes" } else { "card_delta" },
                    "card_count": card_count,
                    "upsert_count": upsert_count,
                    "raw_total_count": projection.raw_total_count,
                    "complete": true,
                },
                "transport": "stream",
            }),
        );
        if outbound_sender
            .send(OutboundFrame {
                value: response,
                allow_compression: true,
                terminal: false,
            })
            .await
            .is_err()
        {
            break;
        }
    }

    state.transcript_streams.detach(&client_id, connection_id);
    drop(outbound_sender);
    let _ = writer.await;
    debug!(client_id, connection_id, "transcript stream disconnected");
}

struct ClientHello {
    client_id: String,
    conversation_id: Option<String>,
    supports_gzip: bool,
}

struct WindowRequest {
    request_id: String,
    conversation_id: String,
    action: TranscriptProjectionAction,
    window_cards: usize,
    shift_cards: usize,
    max_bytes: usize,
    requested_start: Option<usize>,
    known_cards: HashMap<String, u64>,
}

fn parse_hello(frame: Value) -> anyhow::Result<ClientHello> {
    let (tag, payload) = split_tagged_frame(frame)?;
    anyhow::ensure!(tag == TAG_CLIENT_HELLO, "first frame must be client hello");
    let object = object(payload)?;
    let protocol = object.get("protocol").and_then(Value::as_u64).unwrap_or(0);
    anyhow::ensure!(
        protocol == u64::from(PROTOCOL_VERSION),
        "unsupported transcript protocol"
    );
    let client_id = required_string(&object, "client_id")?;
    anyhow::ensure!(
        client_id.len() <= MAX_CLIENT_ID_BYTES,
        "client_id is too long"
    );
    let conversation_id = optional_string(&object, "conversation_id");
    Ok(ClientHello {
        client_id,
        conversation_id,
        supports_gzip: object
            .get("supports_gzip")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

fn parse_window_request(frame: Value) -> anyhow::Result<WindowRequest> {
    let (tag, payload) = split_tagged_frame(frame)?;
    anyhow::ensure!(
        tag == TAG_WINDOW_REQUEST,
        "unsupported client frame tag: {tag}"
    );
    let object = object(payload)?;
    let request_id = required_string(&object, "request_id")?;
    let conversation_id = required_string(&object, "conversation_id")?;
    let action = match required_string(&object, "action")?.as_str() {
        "tail" => TranscriptProjectionAction::Tail,
        "older" => TranscriptProjectionAction::Older,
        "newer" => TranscriptProjectionAction::Newer,
        "current" => TranscriptProjectionAction::Current,
        other => anyhow::bail!("invalid projection action: {other}"),
    };
    let window_cards = usize_field(&object, "window_cards", 75).clamp(1, 200);
    let shift_cards = usize_field(&object, "shift_cards", 20).clamp(1, window_cards);
    let max_bytes = usize_field(&object, "max_bytes", 2 * 1024 * 1024).clamp(1, 8 * 1024 * 1024);
    let known = object.get("known").and_then(Value::as_object);
    let requested_start = known
        .and_then(|known| known.get("start_card"))
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok());
    let mut known_cards = HashMap::new();
    if let Some(cards) = known
        .and_then(|known| known.get("cards"))
        .and_then(Value::as_array)
    {
        anyhow::ensure!(
            cards.len() <= MAX_KNOWN_CARDS,
            "known card list is too large"
        );
        for card in cards {
            let Some(pair) = card.as_array() else {
                continue;
            };
            let Some(card_id) = pair.first().and_then(Value::as_str).map(str::trim) else {
                continue;
            };
            let Some(version) = pair.get(1).and_then(Value::as_u64) else {
                continue;
            };
            if !card_id.is_empty() {
                known_cards.insert(card_id.to_owned(), version);
            }
        }
    }
    Ok(WindowRequest {
        request_id,
        conversation_id,
        action,
        window_cards,
        shift_cards,
        max_bytes,
        requested_start,
        known_cards,
    })
}

fn decode_client_frame(bytes: &[u8]) -> anyhow::Result<Value> {
    anyhow::ensure!(bytes.len() >= 3, "transcript frame is too short");
    anyhow::ensure!(
        bytes[0] == PROTOCOL_VERSION,
        "unsupported transcript frame version"
    );
    anyhow::ensure!(bytes[1] == 0, "compressed client frames are not supported");
    rmp_serde::from_slice(&bytes[2..]).map_err(Into::into)
}

fn encode_server_frame(value: &Value, gzip: bool) -> anyhow::Result<Vec<u8>> {
    let payload = rmp_serde::to_vec(value)?;
    let (flags, body) = if gzip && payload.len() >= COMPRESSION_THRESHOLD_BYTES {
        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        encoder.write_all(&payload)?;
        (FLAG_GZIP, encoder.finish()?)
    } else {
        (0, payload)
    };
    let mut encoded = Vec::with_capacity(body.len() + 2);
    encoded.push(PROTOCOL_VERSION);
    encoded.push(flags);
    encoded.extend_from_slice(&body);
    Ok(encoded)
}

fn tagged_frame(tag: u64, payload: Value) -> Value {
    json!([tag, payload])
}

fn split_tagged_frame(frame: Value) -> anyhow::Result<(u64, Value)> {
    let mut values = frame
        .as_array()
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("transcript frame must be a tagged tuple"))?;
    anyhow::ensure!(
        values.len() == 2,
        "transcript frame must contain tag and payload"
    );
    let payload = values.pop().unwrap_or(Value::Null);
    let tag = values
        .pop()
        .and_then(|value| value.as_u64())
        .ok_or_else(|| anyhow::anyhow!("transcript frame tag must be an integer"))?;
    Ok((tag, payload))
}

fn object(value: Value) -> anyhow::Result<Map<String, Value>> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("transcript frame payload must be an object"))
}

fn required_string(object: &Map<String, Value>, key: &str) -> anyhow::Result<String> {
    optional_string(object, key).ok_or_else(|| anyhow::anyhow!("{key} is required"))
}

fn optional_string(object: &Map<String, Value>, key: &str) -> Option<String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn usize_field(object: &Map<String, Value>, key: &str, fallback: usize) -> usize {
    object
        .get(key)
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .unwrap_or(fallback)
}

async fn send_error_frame(
    sender: &mpsc::Sender<OutboundFrame>,
    request_id: Option<&str>,
    code: &str,
    message: &str,
    fatal: bool,
) -> Result<(), mpsc::error::SendError<OutboundFrame>> {
    sender
        .send(OutboundFrame {
            value: tagged_frame(
                TAG_ERROR,
                json!({
                    "request_id": request_id,
                    "code": code,
                    "message": message,
                    "fatal": fatal,
                }),
            ),
            allow_compression: false,
            terminal: fatal,
        })
        .await
}

async fn send_direct_error(
    socket: &mut WebSocket,
    code: &str,
    message: &str,
) -> anyhow::Result<()> {
    let value = tagged_frame(
        TAG_ERROR,
        json!({"code": code, "message": message, "fatal": true}),
    );
    socket
        .send(Message::Binary(encode_server_frame(&value, false)?.into()))
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binary_frame_round_trip_and_compression() {
        let value = tagged_frame(TAG_WINDOW_SNAPSHOT, json!({"text": "x".repeat(64 * 1024)}));
        let encoded = encode_server_frame(&value, true).unwrap();
        assert_eq!(encoded[0], PROTOCOL_VERSION);
        assert_eq!(encoded[1], FLAG_GZIP);
        assert!(encoded.len() < 2048);
    }

    #[test]
    fn parses_known_card_versions() {
        let request = parse_window_request(tagged_frame(
            TAG_WINDOW_REQUEST,
            json!({
                "request_id": "request-1",
                "conversation_id": "conv-1",
                "action": "older",
                "window_cards": 75,
                "shift_cards": 20,
                "known": {"start_card": 100, "cards": [["assistant:a", 2], ["tool:b", 7]]},
            }),
        ))
        .unwrap();
        assert_eq!(request.requested_start, Some(100));
        assert_eq!(request.known_cards["assistant:a"], 2);
        assert_eq!(request.known_cards["tool:b"], 7);
    }
}
