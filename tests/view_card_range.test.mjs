import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

const result = await build({
  stdin: { contents: `
    export { bindRenderUtils } from './rust/crates/als-server/src/static/js/codex_agent/render/utils.ts';
    export { bindTranscriptCards } from './rust/crates/als-server/src/static/js/codex_agent/transcript_cards.ts';
  `, resolveDir: process.cwd() },
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const { bindRenderUtils, bindTranscriptCards } = await import(
  `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`
);

function render(event, { plain = false } = {}) {
  const { document, window } = parseHTML('<html><body><main></main></body></html>');
  globalThis.document = document;
  globalThis.HTMLElement = window.HTMLElement;
  globalThis.requestAnimationFrame = (callback) => { callback(); return 0; };
  const utils = bindRenderUtils({ getState: () => ({}), documentRef: document });
  const timelineEl = document.querySelector('main');
  const noop = () => {};
  const cards = bindTranscriptCards({
    ...utils, timelineEl,
    getConversationSettings: () => ({ commandOutputLines: 1 }),
    clearPlaceholder: noop, createRow: noop, getLiveEventParent: () => null,
    makeCollapsible: (row, _id, expanded) => { row.dataset.expanded = String(expanded); },
    maybeAutoScroll: noop, setLastEventType: noop, setStatusDot: noop,
    postTe2OpenRequest: noop,
    ...(plain ? { synthesizeStructuredViewLines: () => null } : {}),
  });
  cards.renderViewCard(event);
  return timelineEl;
}

test('custom-title collapsed plain View still advertises its requested range', () => {
  const el = render({ path: '/project/file.py', title: 'Read source',
    view_range: [323, 350], content: 'hello\nworld\n' }, { plain: true });
  assert.equal(el.querySelector('.command-ribbon').textContent, 'Read source  Lines 323–350');
  assert.equal(el.querySelector('.view-card').dataset.expanded, 'false');
  assert.ok(el.querySelector('pre'));
});

test('existing title range appears once and does not shrink to displayed rows', () => {
  const el = render({ path: '/project/file.py', title: 'file.py  Lines 323–350',
    view_range: [323, 350], content: 'hello\nworld\n' });
  assert.equal(el.querySelector('.command-ribbon').textContent, 'file.py  Lines 323–350');
});

test('real structured source lines provide a range before truncation', () => {
  const el = render({ path: '/project/file.py', title: 'file.py', content: '',
    lines: [{ line_no: 101, content: 'hello' }, { line_no: 102, content: 'world' }] });
  assert.equal(el.querySelector('.command-ribbon').textContent, 'file.py  Lines 101–102');
});

test('open-ended and unknown ranges retain their meaning', () => {
  assert.equal(render({ path: 'file.py', view_range: [20], content: 'x' })
    .querySelector('.command-ribbon').textContent, 'file.py  Line 20+');
  assert.equal(render({ path: 'file.py', title: 'Read source', content: 'x\ny' })
    .querySelector('.command-ribbon').textContent, 'Read source');
});
