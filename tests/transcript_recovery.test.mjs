import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

async function load(name) {
  const result = await build({ entryPoints: [`rust/crates/als-server/src/static/js/codex_agent/${name}.ts`],
    bundle: true, platform: 'node', format: 'esm', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}
const { bindTranscriptRecovery } = await load('transcript_recovery');
const { bindTranscriptLoader } = await load('transcript_loader');
globalThis.requestAnimationFrame = (callback) => setImmediate(callback);

const tick = (ms = 15) => new Promise(resolve => setTimeout(resolve, ms));

function controller(refresh) {
  const documentRef = Object.assign(new EventTarget(), { hidden: false });
  const windowRef = new EventTarget();
  let key = 'conversation:1';
  const recovery = bindTranscriptRecovery({ windowRef, documentRef, getKey: () => key, refresh, debounceMs: 5 });
  return { recovery, documentRef, windowRef, setKey(value) { key = value; } };
}

test('control, resume, and stream recovery coalesce and handshake does not recurse', async () => {
  const calls = [];
  let release;
  const blocked = new Promise(resolve => { release = resolve; });
  const { recovery } = controller(async reconnect => { calls.push(reconnect); await blocked; return true; });
  const done = recovery.request('stream');
  void recovery.request('control');
  void recovery.request('resume');
  await tick();
  void recovery.request('stream');
  assert.deepEqual(calls, [true]);
  release();
  await done;
  await tick();
  assert.equal(calls.length, 1);
  recovery.dispose();
});

test('hidden reconnect waits for foreground and bfcache return also recovers', async () => {
  const calls = [];
  const { recovery, documentRef, windowRef } = controller(async reconnect => { calls.push(reconnect); return true; });
  documentRef.hidden = true;
  documentRef.dispatchEvent(new Event('visibilitychange'));
  await recovery.request('control');
  assert.equal(calls.length, 0);
  documentRef.hidden = false;
  documentRef.dispatchEvent(new Event('visibilitychange'));
  await tick();
  assert.deepEqual(calls, [true]);
  windowRef.dispatchEvent(Object.assign(new Event('pageshow'), { persisted: true }));
  await tick();
  assert.deepEqual(calls, [true, true]);
  recovery.dispose();
});

test('resume without a close event refreshes, while changed conversations cancel scheduled recovery', async () => {
  let count = 0;
  const { recovery, documentRef, setKey } = controller(async () => { count++; return true; });
  documentRef.dispatchEvent(new Event('resume'));
  await tick();
  assert.equal(count, 1);
  const done = recovery.request('control');
  setKey('another:2');
  await done;
  assert.equal(count, 1);
  recovery.dispose();
});

test('visibility-only suspension recovers without a network disconnect', async () => {
  let count = 0;
  const { recovery, documentRef } = controller(async () => { count++; return true; });
  const realNow = Date.now;
  let now = realNow();
  try {
    Date.now = () => now;
    documentRef.hidden = true;
    documentRef.dispatchEvent(new Event('visibilitychange'));
    now += 2000;
    documentRef.hidden = false;
    documentRef.dispatchEvent(new Event('visibilitychange'));
    await tick();
    assert.equal(count, 1);
  } finally {
    Date.now = realNow;
    recovery.dispose();
  }
});

function loader({ pinned = false, atTail = false, visibleIndex = 25, fetch } = {}) {
  const state = { transcriptStart: 0, transcriptEnd: 75, transcriptLimit: 75,
    transcriptTotal: 150, transcriptLoading: false, transcriptGeneration: 1,
    transcriptAtTail: atTail, transcriptHistoryMode: !atTail };
  const requests = [], anchors = [], scrolls = [], clears = [];
  let anchor = { cardId: 'card-25', edge: 'start', offsetPx: -12 };
  let renders = 0;
  const result = { conversation_id: 'conv', cards: [], runtime_state: [],
    projection: { start_card: 0, end_card: 75, total_cards: 150, at_start: true, at_tail: atTail },
    frame: { format: 'card_recipes' }, live_projection: { generation: 1, revision: 1, items: [], truncated: false } };
  const binding = bindTranscriptLoader({
    getConversationId: () => 'conv', isPinned: () => pinned,
    getVisibleCardIndex: () => visibleIndex,
    getTranscriptState: () => state, setTranscriptState: patch => Object.assign(state, patch),
    sioCall: async () => { throw new Error('unexpected RPC'); },
    projectionClient: {
      clearProjectionCache: preserve => clears.push(preserve),
      fetchReplayProjection: async options => { requests.push(options); return fetch ? fetch(result) : result; },
    },
    renderTranscriptCards: () => { renders++; }, applyTranscriptRuntimeState() {},
    setScrollProgrammatic() {}, isSemanticShellRibbonEnabled: () => false,
    ensureTreeSitterRibbonReady: async () => {}, maybeAutoScroll: force => scrolls.push(force),
    captureVirtualAnchor: () => anchor, restoreVirtualAnchor: value => anchors.push(value),
  });
  return { binding, state, requests, anchors, scrolls, clears, get renders() { return renders; },
    setPinned(value) { pinned = value; }, setAnchor(value) { anchor = value; } };
}

test('pinned recovery requests tail and forces a fresh bounded snapshot', async () => {
  const f = loader({ pinned: true, atTail: true });
  assert.equal(await f.binding.refreshCurrentTranscriptProjection(), true);
  await tick();
  assert.equal(f.requests[0].action, 'tail');
  assert.equal(f.requests[0].windowCards, 75);
  assert.deepEqual(f.clears, [true]);
  assert.ok(f.scrolls.length > 0);
});

test('unpinned tail and history both preserve the latest visible anchor without forced scroll', async () => {
  for (const atTail of [true, false]) {
    let f;
    const moved = { cardId: 'card-30', edge: 'start', offsetPx: -43 };
    f = loader({ atTail, fetch: async result => { f.setAnchor(moved); return result; } });
    await f.binding.refreshCurrentTranscriptProjection();
    await tick();
    assert.equal(f.requests[0].action, 'current');
    assert.deepEqual(f.anchors, [moved]);
    assert.deepEqual(f.scrolls, []);
  }
});

test('a pin change during fetch does not replace the window or jump to tail', async () => {
  let f;
  f = loader({ pinned: true, fetch: async result => { f.setPinned(false); return result; } });
  assert.equal(await f.binding.refreshCurrentTranscriptProjection(), false);
  assert.equal(f.renders, 0);
  assert.equal(f.state.transcriptLoading, false);
});

test('visible live cards beyond stale cursor bounds seed the current-window request', async () => {
  const f = loader({ visibleIndex: 125, fetch: async result => ({ ...result,
    projection: { ...result.projection, start_card: 105, end_card: 150 } }) });
  assert.equal(await f.binding.refreshCurrentTranscriptProjection(), true);
  assert.equal(f.requests[0].startCard, 105);
  assert.deepEqual(f.scrolls, []);
});
