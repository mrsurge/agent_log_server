import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

const compiled = await build({
  stdin: {
    contents: `export { bindComposerRuntime } from './rust/crates/als-server/src/static/js/codex_agent/composer/runtime.ts';
      export { projectComposerText } from './rust/crates/als-server/src/static/js/codex_agent/composer/text_projection.ts';`,
    resolveDir: process.cwd(),
  },
  bundle: true, write: false, format: 'esm', platform: 'node',
});
const { bindComposerRuntime, projectComposerText } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`,
);

function harness(html = '') {
  const { document, window } = parseHTML('<html><body><div id="prompt"></div></body></html>');
  globalThis.Node = window.Node;
  const prompt = document.getElementById('prompt');
  prompt.innerHTML = html;
  const state = { conversationMeta: { conversation_id: 'test' } };
  const selection = {
    anchorNode: prompt, focusNode: prompt, anchorOffset: 0, focusOffset: 0, rangeCount: 1,
    setBaseAndExtent(a, ao, f, fo) {
      Object.assign(this, { anchorNode: a, anchorOffset: ao, focusNode: f, focusOffset: fo });
    },
    getRangeAt() {
      return { commonAncestorContainer: prompt, startContainer: this.anchorNode,
        endContainer: this.focusNode, cloneRange() { return this; } };
    },
  };
  window.getSelection = () => selection;
  const runtime = bindComposerRuntime({
    getState: () => state, setState: (patch) => Object.assign(state, patch),
    promptEl: prompt, mentionPillEl: null, documentRef: document, windowRef: window,
    sioCall: async () => ({}), escapeHtml: (value) => value,
  });
  return { document, prompt, state, selection, runtime };
}

for (const [html, expected] of [
  ['hello<div>world</div>', 'hello\nworld'],
  ['hello<div><br></div>', 'hello\n'],
  ['<div>hello<br></div><div>world<br></div>', 'hello\nworld'],
  ['<div>hello</div><div><br></div><div>world</div>', 'hello\n\nworld'],
  ['<p>hello</p><p>world</p>', 'hello\nworld'],
  ['<div><div>a</div><div>b</div></div><div>c</div>', 'a\nb\nc'],
  ['a<br><br>b', 'a\n\nb'],
  ['a<br><br>', 'a\n'],
  ['<br>', ''],
  ['<div><br></div><div><br></div>', '\n'],
  ['α😀<div>終</div>', 'α😀\n終'],
  ['a<span>x<br></span>b', 'ax\nb'],
  ['a<span>x<br></span><div>b</div>', 'ax\nb'],
  ['<span></span><div>a</div><span></span><div>b</div>', 'a\nb'],
  ['a<br><span></span>', 'a'],
]) {
  test(`canonical native contenteditable lines: ${html}`, () => {
    const { runtime, prompt } = harness(html);
    assert.equal(runtime.getPromptDraftText(), expected);
    assert.equal(runtime.getPromptText(), expected);
    for (let cycle = 0; cycle < 5; cycle += 1) {
      runtime.renderPromptFromText(runtime.getPromptDraftText());
      assert.equal(runtime.getPromptDraftText(), expected);
      assert.equal(prompt.querySelectorAll('br').length, expected.endsWith('\n') ? 1 : 0,
        'only a trailing caret placeholder is rendered as BR');
    }
  });
}

test('caret offsets use the same full-DOM projection, not cloned partial block newlines', () => {
  const { prompt } = harness('α😀<div>line<span>two</span></div><div><br></div>');
  const projection = projectComposerText(prompt, () => '');
  assert.equal(projection.text, 'α😀\nlinetwo\n');
  for (let offset = 0; offset <= projection.text.length; offset += 1) {
    const point = projection.pointAt(offset);
    assert.equal(projection.offsetForPoint(point.node, point.offset), offset, `offset ${offset}`);
  }
  const second = prompt.querySelector('div').firstChild;
  assert.equal(projection.offsetForPoint(second, 2), 'α😀\nli'.length);
});

test('pairing at the mobile tail does not add a newline; restore and remote sync are stable', () => {
  const { prompt, runtime, selection, state } = harness('hello<div>tail<br></div>');
  const tail = prompt.querySelector('div').firstChild;
  selection.setBaseAndExtent(tail, 4, tail, 4);
  let prevented = false;
  assert.equal(runtime.handleComposerAutoPairInput({
    cancelable: true, isComposing: false, inputType: 'insertText', data: '(',
    preventDefault() { prevented = true; },
  }), 'insert');
  assert.equal(prevented, true);
  assert.equal(runtime.getPromptDraftText(), 'hello\ntail()');
  const projected = projectComposerText(prompt, () => '');
  assert.equal(projected.offsetForPoint(selection.anchorNode, selection.anchorOffset), 'hello\ntail('.length);
  for (let revision = 1; revision <= 5; revision += 1) {
    const draft = runtime.getPromptDraftText();
    state.conversationMeta.draft = draft;
    runtime.restoreDraft();
    runtime.applyDraftUpdate({ conversation_id: 'test', draft, draft_revision: revision,
      origin_client_id: 'remote', draft_selection: { anchor: 4, focus: 4 } });
    assert.equal(runtime.getPromptDraftText(), 'hello\ntail()');
  }
});

test('authored blank lines and trailing whitespace survive draft restoration', () => {
  const { runtime, state } = harness();
  for (const draft of ['\n\n', ' hello\n\nworld\n\n', 'x\n', '   ']) {
    state.conversationMeta.draft = draft;
    runtime.restoreDraft();
    assert.equal(runtime.getPromptDraftText(), draft);
  }
});

test('pair insertion on an empty mobile tail and backward multiline selections keeps exact text', () => {
  for (const [opening, closing] of [['(', ')'], ['[', ']'], ['{', '}'], ['"', '"'], ['`', '`']]) {
    const { prompt, runtime, selection } = harness('hello<div><br></div>');
    const tail = prompt.querySelector('div');
    selection.setBaseAndExtent(tail, 0, tail, 0);
    runtime.handleComposerAutoPairInput({ cancelable: true, isComposing: false,
      inputType: 'insertText', data: opening, preventDefault() {} });
    assert.equal(runtime.getPromptDraftText(), `hello\n${opening}${closing}`);
  }
  const { prompt, runtime, selection } = harness('a<div>b<br></div>');
  selection.setBaseAndExtent(prompt.querySelector('div').firstChild, 1, prompt.firstChild, 0);
  runtime.handleComposerAutoPairInput({ cancelable: true, isComposing: false,
    inputType: 'insertText', data: '[', preventDefault() {} });
  assert.equal(runtime.getPromptDraftText(), '[a\nb]');
  const projection = projectComposerText(prompt, () => '');
  assert.equal(projection.offsetForPoint(selection.anchorNode, selection.anchorOffset), 4);
  assert.equal(projection.offsetForPoint(selection.focusNode, selection.focusOffset), 1);
});

test('another client receiving a native mobile draft repeatedly cannot multiply blank lines', () => {
  const author = harness('<div>first<br></div><div><br></div><div>second<br></div>');
  const receiver = harness('old draft');
  for (let revision = 1; revision <= 5; revision += 1) {
    const draft = author.runtime.getPromptDraftText();
    receiver.runtime.applyDraftUpdate({ conversation_id: 'test', draft,
      draft_revision: revision, origin_client_id: 'other-mobile-client',
      draft_selection: { anchor: draft.length, focus: draft.length } });
    assert.equal(receiver.runtime.getPromptDraftText(), 'first\n\nsecond');
    author.state.conversationMeta.draft = receiver.runtime.getPromptDraftText();
    author.runtime.restoreDraft();
    assert.equal(author.runtime.getPromptDraftText(), 'first\n\nsecond');
  }
});

test('mention envelopes survive native blocks, rendering and caret movement as atomic tokens', () => {
  const { prompt, runtime } = harness('a<div><span class="mention-token" data-abs="/project/example.py">example</span></div>');
  const draft = runtime.getPromptDraftText();
  assert.match(draft, /^a\n\x1eCODEX_MENTION /);
  runtime.renderPromptFromText(draft);
  assert.equal(runtime.getPromptDraftText(), draft);
  assert.equal(prompt.querySelectorAll('.mention-token').length, 1);
  assert.equal(runtime.getPromptText(), 'a\n`/project/example.py`');
});
