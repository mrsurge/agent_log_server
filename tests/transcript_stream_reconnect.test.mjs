import assert from 'node:assert/strict';
import test from 'node:test';

import { decode, encode } from '@msgpack/msgpack';
import { build } from 'esbuild';

const PROTOCOL_VERSION = 1;
const TAG_SERVER_HELLO = 2;
const TAG_WINDOW_REQUEST = 3;
const TAG_WINDOW_SNAPSHOT = 4;

class FakeReconnectingWebSocket {
  constructor() {
    FakeReconnectingWebSocket.instances.push(this);
  }

  static instances = [];

  readyState = 0;
  sent = [];
  listeners = new Map();

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name, event = {}) {
    for (const listener of this.listeners.get(name) || []) listener(event);
  }

  open() {
    this.readyState = 1;
    this.dispatch('open');
  }

  disconnect() {
    this.readyState = 3;
    this.dispatch('close');
  }

  message(tag, payload) {
    this.dispatch('message', { data: encodeServerFrame(tag, payload) });
  }

  send(value) {
    this.sent.push(new Uint8Array(value));
  }

  reconnect() {}

  close() {
    this.readyState = 3;
  }
}

globalThis.__AlsTranscriptTestSocket = FakeReconnectingWebSocket;
globalThis.WebSocket = { OPEN: 1 };

const result = await build({
  bundle: true,
  entryPoints: [
    'rust/crates/als-server/src/static/js/codex_agent/transcript_stream.ts',
  ],
  format: 'esm',
  platform: 'node',
  target: 'es2020',
  write: false,
  plugins: [{
    name: 'fake-reconnecting-websocket',
    setup(buildApi) {
      buildApi.onResolve(
        { filter: /^reconnecting-websocket$/ },
        () => ({ path: 'fake-reconnecting-websocket', namespace: 'test-double' }),
      );
      buildApi.onLoad(
        { filter: /.*/, namespace: 'test-double' },
        () => ({ contents: 'export default globalThis.__AlsTranscriptTestSocket;' }),
      );
    },
  }],
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { createTranscriptStreamClient } = await import(moduleUrl);

function encodeServerFrame(tag, payload) {
  const body = encode([tag, payload]);
  const frame = new Uint8Array(body.byteLength + 2);
  frame[0] = PROTOCOL_VERSION;
  frame[1] = 0;
  frame.set(body, 2);
  return frame.buffer;
}

function decodeClientFrame(frame) {
  assert.equal(frame[0], PROTOCOL_VERSION);
  assert.equal(frame[1], 0);
  return decode(frame.subarray(2));
}

function projectionPayload(requestId, version, streamSequence) {
  const cardId = 'user:message-1';
  return {
    request_id: requestId,
    conversation_id: 'conv-reconnect',
    projection: {
      unit: 'transcript_card',
      start_card: 0,
      end_card: 1,
      total_cards: 1,
      window_cards: 75,
      shift_cards: 20,
      card_count: 1,
      at_start: true,
      at_tail: true,
      revision: version,
    },
    ordered_cards: [{ card_id: cardId, card_index: 0, version }],
    removed_card_ids: [],
    cards: [{
      card_id: cardId,
      card_index: 0,
      version,
      family: 'user',
      scope: 'durable',
      events: [{
        role: 'user',
        text: `message ${version}`,
        projection_card_id: cardId,
        projection_card_index: 0,
        projection_card_version: version,
        projection_card_op: 'create',
        projection_card_scope: 'durable',
      }],
    }],
    runtime_state: [],
    live_projection: {
      generation: 0,
      revision: 0,
      items: [],
      truncated: false,
    },
    stream_sequence: streamSequence,
    frame: {
      format: 'card_recipes',
      card_count: 1,
      raw_event_count: 1,
      raw_total_count: 1,
      complete: true,
    },
    transport: 'stream',
  };
}

async function waitFor(predicate, message) {
  const deadline = Date.now() + 1000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error(message);
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function latestWindowRequest(socket) {
  for (let index = socket.sent.length - 1; index >= 0; index -= 1) {
    const frame = decodeClientFrame(socket.sent[index]);
    if (Array.isArray(frame) && frame[0] === TAG_WINDOW_REQUEST) return frame[1];
  }
  return null;
}

function windowRequestCount(socket) {
  return socket.sent.reduce((count, sentFrame) => {
    const frame = decodeClientFrame(sentFrame);
    return count + Number(Array.isArray(frame) && frame[0] === TAG_WINDOW_REQUEST);
  }, 0);
}

test('reconnect requests and decodes a complete transcript snapshot', async () => {
  FakeReconnectingWebSocket.instances.length = 0;
  let reconnectResult = null;
  let client;
  const projectionOptions = {
    conversationId: 'conv-reconnect',
    action: 'current',
    windowCards: 75,
    shiftCards: 20,
    timeoutMs: 1000,
  };
  client = createTranscriptStreamClient({
    windowRef: {
      location: new URL('http://127.0.0.1:12459/'),
      crypto: { randomUUID: () => 'reconnect-test' },
    },
    getConversationId: () => 'conv-reconnect',
    onEvent() {},
    onReconnect: async () => {
      reconnectResult = await client.fetchReplayProjection(projectionOptions);
    },
  });

  const initialResultPromise = client.fetchReplayProjection({
    ...projectionOptions,
    action: 'tail',
  });
  const socket = FakeReconnectingWebSocket.instances[0];
  assert.ok(socket);
  socket.open();
  socket.message(TAG_SERVER_HELLO, {
    protocol: PROTOCOL_VERSION,
    server_epoch: 'epoch-1',
    client_id: client.clientId,
  });
  await waitFor(() => latestWindowRequest(socket) !== null, 'initial projection request was not sent');
  const initialRequest = latestWindowRequest(socket);
  socket.message(TAG_WINDOW_SNAPSHOT, projectionPayload(initialRequest.request_id, 1, 4));
  const initialResult = await initialResultPromise;
  assert.equal(initialResult.cards[0].version, 1);

  const requestsBeforeReconnect = windowRequestCount(socket);
  socket.disconnect();
  socket.open();
  socket.message(TAG_SERVER_HELLO, {
    protocol: PROTOCOL_VERSION,
    server_epoch: 'epoch-1',
    client_id: client.clientId,
  });
  await waitFor(
    () => windowRequestCount(socket) > requestsBeforeReconnect,
    'reconnect projection request was not sent',
  );
  const reconnectRequest = latestWindowRequest(socket);
  assert.deepEqual(reconnectRequest.known.cards, []);
  assert.equal(reconnectRequest.known.start_card, 0);

  socket.message(TAG_WINDOW_SNAPSHOT, projectionPayload(reconnectRequest.request_id, 2, 7));
  await waitFor(() => reconnectResult !== null, 'reconnect snapshot response was not decoded');
  assert.equal(reconnectResult.cards[0].version, 2);
  assert.equal(client.debugSnapshot().stream_sequence, 7);
  client.dispose();
});
