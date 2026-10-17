import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

const compiled = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/boot/input_flow.ts'],
  bundle: true, write: false, format: 'esm', platform: 'node',
});
const { bindInputFlow } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`,
);

function harness() {
  const { document, window } = parseHTML('<html><body><div id="prompt"></div><button id="interrupt"></button></body></html>');
  globalThis.Element = window.Element;
  const state = { conversationMeta: { conversation_id: 'a' } };
  const button = document.getElementById('interrupt');
  const listeners = [];
  const timers = new Map();
  let timerId = 0;
  let interruptions = 0;
  const flow = bindInputFlow({
    getState: () => state, setState: (patch) => Object.assign(state, patch),
    elements: { interruptBtn: button, promptEl: document.getElementById('prompt') },
    documentRef: document,
    windowRef: {
      addEventListener(name, callback, capture = false) { listeners.push({ name, callback, capture }); },
      setTimeout(callback, delay) { assert.equal(delay, 10_000); timers.set(++timerId, callback); return timerId; },
      clearTimeout(id) { timers.delete(id); },
    },
    bindSplashTabHandlers() {}, initTribute() {}, interruptTurn: async () => { interruptions += 1; },
  });
  flow.bindInputHandlers();
  return {
    state, button, document, timers,
    get interruptions() { return interruptions; },
    async key(overrides = {}, between = () => {}) {
      const event = { key: 'Escape', target: document.getElementById('prompt'),
        defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...overrides };
      for (const listener of listeners.filter((entry) => entry.name === 'keydown' && entry.capture)) {
        await listener.callback(event);
      }
      between(event);
      for (const listener of listeners.filter((entry) => entry.name === 'keydown' && !entry.capture)) {
        await listener.callback(event);
      }
      return event;
    },
    async click() {
      button.dispatchEvent(new window.Event('click'));
      await Promise.resolve();
    },
  };
}

test('Escape arms, then confirms using the button arm state', async () => {
  const h = harness();
  assert.equal((await h.key()).defaultPrevented, true);
  assert.equal(h.interruptions, 0);
  assert.equal(h.button.getAttribute('aria-pressed'), 'true');
  await h.key();
  assert.equal(h.interruptions, 1);
  assert.equal(h.button.getAttribute('aria-pressed'), 'false');
  assert.equal(h.timers.size, 0);
});

test('click and Escape share arming in both directions', async () => {
  const h = harness();
  await h.click();
  await h.key();
  assert.equal(h.interruptions, 1);
  await h.key();
  await h.click();
  assert.equal(h.interruptions, 2);
});

test('held Escape, IME, modified keys and handled events cannot confirm', async () => {
  const h = harness();
  await h.key();
  for (const flags of [{ repeat: true }, { isComposing: true }, { ctrlKey: true },
    { altKey: true }, { metaKey: true }, { shiftKey: true }, { defaultPrevented: true }]) {
    await h.key(flags);
    assert.equal(h.interruptions, 0);
  }
  await h.key({}, (event) => event.preventDefault());
  assert.equal(h.interruptions, 0);
});

test('expiry returns Escape to first-arm behavior', async () => {
  const h = harness();
  await h.key();
  for (const timeout of [...h.timers.values()]) timeout();
  await h.key();
  assert.equal(h.interruptions, 0);
  assert.equal(h.button.getAttribute('aria-pressed'), 'true');
});

test('changing conversation never confirms the previous conversation arm', async () => {
  const h = harness();
  await h.key();
  h.state.conversationMeta.conversation_id = 'b';
  await h.key();
  assert.equal(h.interruptions, 0);
  await h.key();
  assert.equal(h.interruptions, 1);
});

test('Escape dismissing an overlay cannot fall through after it closes', async () => {
  for (const className of ['settings-overlay', 'picker-overlay', 'tribute-container', 'dropdown-list']) {
    const h = harness();
    const overlay = h.document.createElement('div');
    overlay.className = className;
    overlay.getClientRects = () => [{}];
    h.document.body.append(overlay);
    await h.key({}, () => overlay.remove());
    assert.equal(h.button.getAttribute('aria-pressed'), 'false');
    assert.equal(h.interruptions, 0);
  }
});

test('terminal, dialog and menu input is left to its owner', async () => {
  const h = harness();
  for (const [className, role] of [['xterm', null], ['', 'dialog'], ['', 'menu']]) {
    const owner = h.document.createElement('div');
    owner.className = className;
    if (role) owner.setAttribute('role', role);
    const target = h.document.createElement('textarea');
    owner.append(target);
    h.document.body.append(owner);
    if (role) owner.getClientRects = () => [];
    assert.equal((await h.key({ target })).defaultPrevented, false);
    owner.remove();
  }
  assert.equal(h.button.getAttribute('aria-pressed'), 'false');
});

test('no conversation, hidden or disabled button cannot arm from Escape', async () => {
  const h = harness();
  h.state.conversationMeta.conversation_id = null;
  await h.key();
  h.state.conversationMeta.conversation_id = 'a';
  h.button.hidden = true;
  await h.key();
  h.button.hidden = false;
  h.button.setAttribute('disabled', '');
  await h.key();
  assert.equal(h.button.getAttribute('aria-pressed'), 'false');
  assert.equal(h.interruptions, 0);
});
