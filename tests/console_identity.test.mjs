import assert from 'node:assert/strict';
import test from 'node:test';

const bridgeUrl = new URL('../rust/crates/als-server/src/static/js/console_bridge.js', import.meta.url);

test('ALS shared slot gets a connection-local console handle without rewriting routing identity', async () => {
  const module = await import(`${bridgeUrl.href}?identity-test`);
  const handlers = new Map();
  const emitted = [];
  const query = '?clientInstanceId=client-A&presentationId=page-A&te2_host_id=shared-slot';
  globalThis.window = {
    location: { search: query },
    dispatchEvent() {}, addEventListener() {},
  };
  const socket = {
    connected: true,
    on(event, fn) { handlers.set(event, fn); },
    off(event, fn) { if (handlers.get(event) === fn) handlers.delete(event); },
    emit(event, data) { emitted.push({ event, data }); },
  };
  const bridge = module.initConsoleBridge({ socket, workerId: 'als:shared', workerLabel: 'als' });
  try {
    const original = emitted.find(e => e.event === 'console:register').data;
    assert.deepEqual(original, {
      workerId: 'als:shared', workerLabel: 'als', role: 'worker', compactIdentity: true,
      qualifiedIdentity: false,
      clientInstanceId: 'client-A', presentationId: 'page-A',
    });
    handlers.get('console:registered')({ requestedWorkerId: 'als:shared', workerId: 'als:Ab12.Cd34' });
    assert.equal(window.__TE2_CONSOLE_WORKER.workerId, 'als:Ab12.Cd34');
    assert.equal(window.location.search, query);
    handlers.get('connect')();
    assert.deepEqual(emitted.findLast(e => e.event === 'console:register').data, original);
  } finally { bridge.destroy(); }
  assert.equal(handlers.has('console:registered'), false);
  assert.equal(handlers.has('connect'), false);
  assert.equal(handlers.has('console:eval'), false);
});

test('qualified window URL is the console name rather than a random replacement', async () => {
  const module = await import(`${bridgeUrl.href}?qualified-identity-test`);
  const handlers = new Map();
  const emitted = [];
  globalThis.window = {
    location: { search: '?clientInstanceId=client-A&presentationId=inc-A&mainPageId=f0f0&sidebarWindowId=als%3Aa1b2.f0f0' },
    dispatchEvent() {}, addEventListener() {},
  };
  const socket = { connected: true, on(event, fn) { handlers.set(event, fn); },
    off(event, fn) { if (handlers.get(event) === fn) handlers.delete(event); },
    emit(event, data) { emitted.push({ event, data }); } };
  const bridge = module.initConsoleBridge({ socket, workerId: 'old-long-shared-id', workerLabel: 'als' });
  try {
    const registration = emitted.find(e => e.event === 'console:register').data;
    assert.equal(registration.workerId, 'als:a1b2.f0f0');
    assert.equal(registration.qualifiedIdentity, true);
    assert.equal(window.__TE2_CONSOLE_WORKER.workerId, 'als:a1b2.f0f0');
  } finally { bridge.destroy(); }
});
