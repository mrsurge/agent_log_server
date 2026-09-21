import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

const buildResult = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/shell_output_window.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const module = await import(`data:text/javascript;base64,${Buffer.from(buildResult.outputFiles[0].text).toString('base64')}`);
const pause = () => new Promise(resolve => setTimeout(resolve, 150));

test('shell window is independently pinned, does not grow while detached, and coalesces requests', async () => {
  const { document, window } = parseHTML('<html><body><div id="agent-timeline"><pre class="command-output"></pre></div></body></html>');
  const names = ['document', 'HTMLElement', 'IntersectionObserver', 'MutationObserver', 'requestAnimationFrame'];
  const saved = Object.fromEntries(names.map(key => [key, globalThis[key]]));
  let intersection;
  Object.assign(globalThis, {
    document, HTMLElement: window.HTMLElement,
    IntersectionObserver: class {
      constructor(callback) { intersection = callback; }
      observe() {} unobserve() {}
    },
    MutationObserver: class { observe() {} },
    requestAnimationFrame: callback => setTimeout(callback, 0),
  });
  const parent = document.getElementById('agent-timeline');
  parent.scrollTop = 913;
  const pre = parent.querySelector('pre');
  pre.scrollTop = 0;
  Object.defineProperties(pre, { scrollHeight: { get: () => 600 }, clientHeight: { get: () => 100 } });
  pre.getBoundingClientRect = () => ({ top: 0, bottom: 100 });
  window.HTMLElement.prototype.getBoundingClientRect = function () { return { top: 0, bottom: 20 }; };
  const calls = [];
  const text = Array.from({ length: 20 }, (_, i) => `line ${i}\n`).join('');
  const offsets = [];
  let byte = 0;
  for (const line of text.split('\n').slice(0, -1)) { offsets.push(byte); byte += line.length + 1; }
  const reference = { id: 'one', conversation_id: 'test', bytes: byte, running: true };
  module.configureShellOutputWindows(async params => {
    calls.push(params);
    return { text, offsets, start: 0, end: 20, start_byte: 0, end_byte: byte, bytes: byte, at_start: true, at_tail: true };
  });
  try {
    assert.equal(module.mountShellOutputWindow(pre, reference), true);
    intersection([{ target: pre, isIntersecting: true }]);
    for (let i = 0; i < 50; i++) module.mountShellOutputWindow(pre, reference);
    await pause();
    assert.equal(calls.length, 1);
    assert.equal(pre.textContent, text);
    assert.equal(pre.children.length, 20);
    assert.equal(parent.scrollTop, 913);
    const wheel = new window.Event('wheel'); wheel.deltaY = -10;
    pre.dispatchEvent(wheel);
    pre.scrollTop = 180;
    pre.dispatchEvent(new window.Event('scroll'));
    const follow = parent.querySelector('.shell-output-follow');
    assert.equal(follow.hidden, false);
    module.mountShellOutputWindow(pre, { ...reference, bytes: byte + 10000 });
    await pause();
    assert.equal(calls.length, 1);
    assert.equal(pre.scrollTop, 180);
    assert.equal(pre.textContent, text);
    follow.click();
    await pause();
    assert.equal(calls.length, 2);
    assert.equal(calls[1].action, 'tail');
    assert.equal(follow.hidden, true);
    assert.equal(parent.scrollTop, 913);
    intersection([{ target: pre, isIntersecting: false }]);
    module.mountShellOutputWindow(pre, { ...reference, bytes: byte + 20000 });
    await pause();
    assert.equal(calls.length, 2);
  } finally { Object.assign(globalThis, saved); }
});
