import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const result = await build({
  bundle: true,
  entryPoints: [
    'rust/crates/als-server/src/static/js/codex_agent/boot/input_flow.ts',
  ],
  format: 'esm',
  platform: 'node',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { bindInputFlow } = await import(moduleUrl);

class FakeElement {
  listeners = new Map();

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  async emit(name, event = {}) {
    await Promise.all((this.listeners.get(name) || []).map((listener) => listener(event)));
  }
}

function createInputFlow() {
  const order = [];
  const sendButton = new FakeElement();
  const prompt = new FakeElement();
  const windowRef = new FakeElement();
  windowRef.setTimeout = setTimeout;
  windowRef.clearTimeout = clearTimeout;
  prompt.focus = (options) => order.push(['focus', options]);
  let text = 'hello';
  const flow = bindInputFlow({
    getState: () => ({ isMobile: false }),
    setState() {},
    elements: {
      sendBtn: sendButton,
      promptEl: prompt,
    },
    getPromptText: () => text,
    clearPrompt: () => order.push(['clearPrompt']),
    clearDraft: () => order.push(['clearDraft']),
    saveDraftDebounced() {},
    sendUserMessage: async (message) => order.push(['send', message]),
    sendShellCommand: async () => {},
    openPicker() {},
    sendHostCloseMessage() {},
    bindSplashTabHandlers() {},
    initTribute() {},
    requestContextCompact: async () => {},
    interruptTurn: async () => {},
    updateScrollButton() {},
    maybeAutoScroll() {},
    isNearBottom: () => true,
    documentRef: {},
    windowRef,
  });
  flow.bindInputHandlers();
  return {
    order,
    prompt,
    sendButton,
    setText(value) {
      text = value;
    },
  };
}

test('send button preserves composer focus before dispatching', async () => {
  const harness = createInputFlow();
  let pointerDefaultPrevented = false;
  await harness.sendButton.emit('pointerdown', {
    preventDefault() {
      pointerDefaultPrevented = true;
    },
  });
  await harness.sendButton.emit('click');

  assert.equal(pointerDefaultPrevented, true);
  assert.deepEqual(harness.order, [
    ['clearPrompt'],
    ['clearDraft'],
    ['focus', { preventScroll: true }],
    ['send', 'hello'],
  ]);
});

test('enter submission restores the empty composer focus before dispatching', async () => {
  const harness = createInputFlow();
  harness.setText('from enter');
  let defaultPrevented = false;
  await harness.prompt.emit('keydown', {
    key: 'Enter',
    shiftKey: false,
    preventDefault() {
      defaultPrevented = true;
    },
  });

  assert.equal(defaultPrevented, true);
  assert.deepEqual(harness.order.at(-2), ['focus', { preventScroll: true }]);
  assert.deepEqual(harness.order.at(-1), ['send', 'from enter']);
});
