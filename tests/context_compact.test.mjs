import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const messages = [];
globalThis.__compactTestToast = message => messages.push(message);
const bundle = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/conversation/runtime.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
  plugins: [{ name: 'toast-test', setup(build) {
    build.onResolve({ filter: /\/toast\.ts$/ }, () => ({ path: 'toast', namespace: 'test' }));
    build.onLoad({ filter: /.*/, namespace: 'test' }, () => ({ contents: 'export const showToast = message => globalThis.__compactTestToast(message);' }));
  } }],
});
const { bindConversationRuntime } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);

test('compact waits for acknowledgment, blocks duplicate taps, and visibly reports failure', async (t) => {
  t.mock.method(console, 'warn', () => {});
  let finish;
  const calls = [];
  const runtime = bindConversationRuntime({
    getState: () => ({ clientConversationId: 'conv-one' }),
    conversationsRpcClient: { compactConversation: options => {
      calls.push(options);
      return new Promise(resolve => { finish = resolve; });
    } },
  });
  const pending = runtime.requestContextCompact();
  await runtime.requestContextCompact();
  assert.deepEqual(calls, [{ conversationId: 'conv-one', timeoutMs: 150000 }]);
  finish({ ok: false, error: 'Provider refused' });
  await pending;
  assert.equal(messages.at(-1), 'Context compaction failed: Provider refused');
  const second = runtime.requestContextCompact();
  finish({ ok: true });
  await second;
  assert.equal(calls.length, 2);
  assert.equal(messages.at(-1), 'Context compaction request accepted');
});
