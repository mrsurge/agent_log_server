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
      search: `?te2_host_id=shared-als-slot&clientInstanceId=${client}&presentationId=${presentation}&te2_console_worker_id=als%3Ashared`,
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

test('qualified sidebar/main-page identity accompanies the existing incarnation proof', async () => {
  for (const parent of ['f0f0', 'c3d4']) {
    const { createUiRpcClient } = await import(`${moduleUrl}#qualified-${parent}`);
    const sent = [];
    const win = windowRef(`client-${parent}`, `inc-${parent}`, sent);
    win.location.search += `&mainPageId=${parent}&sidebarWindowId=als%3Aa1b2.${parent}`;
    const rpc = createUiRpcClient({ sioCall: async () => ({}), windowRef: win });
    await rpc.openFile({ path: '/project/readme.md' });
    assert.deepEqual(sent[0].params.target, { clientId: `client-${parent}`, hostId: 'shared-als-slot',
      presentationId: `inc-${parent}`, mainPageId: parent, sidebarWindowId: `als:a1b2.${parent}` });
  }
});

test('project open/create use the same qualified originating presentation', async () => {
  for (const client of ['client-a', 'client-b']) {
    const { createUiRpcClient } = await import(`${moduleUrl}#projects-${client}`);
    const sent = [];
    const rpc = createUiRpcClient({ sioCall: async () => { throw Error('no fallback'); },
      windowRef: windowRef(client, `p-${client}`, sent) });
    await rpc.openTe2Project({ path: '/project' });
    await rpc.createTe2Project({ path: '/project', adoptExisting: true, open: false });
    assert.equal(sent.length, 2);
    for (const request of sent) {
      assert.deepEqual(request.params.target, { clientId: client, hostId: 'shared-als-slot', presentationId: `p-${client}` });
    }
    assert.equal(sent[1].params.open, false);
  }
});
