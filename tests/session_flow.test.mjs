import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/orchestrator/session_flow.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const { bindSessionFlow } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);

function setup(session, sendError) {
  const activity = [];
  const sends = [];
  let currentId = 'conv-one';
  const flow = bindSessionFlow({
    getState: () => ({ initialized: true, clientConversationId: currentId }),
    setState() {}, sioCall: async () => {}, waitForWs: async () => true,
    settingsRpcClient: { getExtensionSessionState: async (options) => {
      assert.equal(options.conversationId, 'conv-one');
      assert.equal(options.timeoutMs, 3000);
      if (session instanceof Error) throw session;
      return session;
    } },
    conversationsRpcClient: { sendMessage: async (options) => {
      sends.push(options);
      if (sendError) throw sendError;
      return { ok: true, accepted: true };
    } },
    setActivity: (label) => activity.push(label),
    updateScrollButton() {}, maybeAutoScroll() {}, renderShellBatchResult() {},
    setStatusDot() {}, shellRows: new Map(),
  });
  return { flow, activity, sends, switchConversation: () => { currentId = 'conv-two'; } };
}

test('cold and unbound sessions show loading and get two minutes without duplicate sends', async () => {
  for (const state of ['cold', 'unbound']) {
    const { flow, sends, activity } = setup({ ok: true, supported: true, loaded: false, state });
    await flow.sendUserMessage('hello');
    assert.deepEqual(activity, ['sending', 'Loading session']);
    assert.deepEqual(sends, [{ conversationId: 'conv-one', text: 'hello', timeoutMs: 120000 }]);
  }
});

test('loaded, unsupported, unknown and failed probes retain normal send behavior', async () => {
  for (const session of [
    { supported: true, loaded: true, state: 'loaded' },
    { supported: false, loaded: false, state: 'unsupported' },
    { supported: true, loaded: false, state: 'unknown' },
    { supported: true, ok: false, state: 'cold' },
    new Error('probe unavailable'),
  ]) {
    const { flow, sends, activity } = setup(session);
    await flow.sendUserMessage('hello');
    assert.deepEqual(activity, ['sending']);
    assert.equal(sends.length, 1);
    assert.equal(sends[0].timeoutMs, 10000);
  }
});

test('cold send timeout has session-specific wording while provider failures stay intact', async () => {
  for (const [failure, expected] of [
    ['Timed out waiting for conversation.send', 'Session loading timed out after 2 minutes'],
    ['Authentication required', 'Authentication required'],
  ]) {
    const { flow, activity, sends } = setup({ supported: true, state: 'cold' }, new Error(failure));
    await flow.sendUserMessage('hello');
    assert.equal(activity.at(-1), expected);
    assert.equal(sends.length, 1);
  }
});

test('a conversation switch prevents pending probe and send errors from overwriting the new view', async () => {
  const { flow, activity, sends, switchConversation } = setup({ supported: true, state: 'cold' }, new Error('send failed'));
  const pending = flow.sendUserMessage('hello');
  switchConversation();
  await pending;
  assert.deepEqual(activity, ['sending']);
  assert.equal(sends[0].conversationId, 'conv-one');
});
