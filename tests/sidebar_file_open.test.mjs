import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const result = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/rpc/ui/client.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`;

function windowRef(client, presentation, sent) {
  return {
    location: {
      pathname: '/api/app/als-rs/proxy/',
      search: `?te2_host_id=shared-als-slot&clientInstanceId=${client}&presentationId=${presentation}`,
    },
    sessionStorage: { getItem: () => null },
    io: () => ({ connected: true, on() {},
      emit(_event, request, ack) {
        sent.push(request);
        ack({ jsonrpc: '2.0', id: request.id, result: { ok: true, sent: false } });
      },
    }),
  };
}

test('common file-open boundary uses each iframe caller, not a shared slot/global target', async () => {
  for (const client of ['client-a', 'client-b']) {
    // Separate module realms stand in for separate iframe realms/socket caches.
    const { createUiRpcClient } = await import(`${moduleUrl}#${client}`);
    const sent = [];
    const rpc = createUiRpcClient({ sioCall: async () => { throw Error('no legacy fallback'); },
      windowRef: windowRef(client, `p-${client}`, sent) });
    await rpc.openFile({ path: '/project/readme.md', line: 50, column: 1,
      target: { clientId: 'wrong-client' } });
    assert.deepEqual(sent[0].params, { path: '/project/readme.md', line: 50, column: 1,
      target: { clientId: client, hostId: 'shared-als-slot', presentationId: `p-${client}` } });
  }
});

test('incomplete caller fails before sending and never chooses another window', async () => {
  const { createUiRpcClient } = await import(`${moduleUrl}#missing`);
  const sent = [];
  const win = windowRef('client-a', 'p-a', sent);
  win.location.search = '?te2_host_id=shared-als-slot';
  const rpc = createUiRpcClient({ sioCall: async () => ({}), windowRef: win });
  await assert.rejects(rpc.openFile({ path: '/project/readme.md' }), /current sidebar presentation/);
  assert.equal(sent.length, 0);
});
