import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import hljs from 'highlight.js/lib/core';
import rust from 'highlight.js/lib/languages/rust';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import { parseHTML } from 'linkedom';

const result = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/diff/summary.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const { countPatchChanges, diffFileIcon } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);

test('counts patch lines, including header-like content, across files and hunks', () => {
  const patch = 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,3 @@\n context\n---removed\n+++added\n+second\n\\ No newline at end of file\ndiff --git a/b b/b\n--- /dev/null\n+++ b/b\n@@ -0,0 +1 @@\n+new\n';
  assert.deepEqual(countPatchChanges(patch), { additions: 3, deletions: 1 });
  assert.deepEqual(countPatchChanges('--- a/a\n+++ b/a\nBinary files differ'), { additions: 0, deletions: 0 });
  assert.deepEqual(countPatchChanges('@@ -1 +0,0 @@\n-gone'), { additions: 0, deletions: 1 });
});

test('file icons use the detected language with an unknown-file fallback', () => {
  assert.equal(diffFileIcon('python'), 'language-python');
  assert.equal(diffFileIcon('rust'), 'language-rust');
  assert.equal(diffFileIcon('typescript'), 'language-typescript');
  assert.equal(diffFileIcon('javascript'), 'language-javascript');
  assert.equal(diffFileIcon('json'), 'json');
  assert.equal(diffFileIcon('markdown'), 'markdown');
  assert.equal(diffFileIcon(null), 'file');
});

test('diff syntax follows file extensions and the summary is appended after the body', async () => {
  async function load(path) {
    const built = await build({ entryPoints: [path], bundle: true, format: 'esm', platform: 'node', write: false });
    return import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);
  }
  const { bindRenderUtils } = await load('rust/crates/als-server/src/static/js/codex_agent/render/utils.ts');
  const { bindDiffRendering } = await load('rust/crates/als-server/src/static/js/codex_agent/diff/rendering.ts');
  hljs.registerLanguage('rust', rust);
  hljs.registerLanguage('typescript', typescript);
  hljs.registerLanguage('python', python);
  const previous = { hljs: globalThis.hljs, window: globalThis.window, HTMLElement: globalThis.HTMLElement, document: globalThis.document };
  globalThis.document = parseHTML('<html><body></body></html>').document;
  globalThis.hljs = hljs;
  globalThis.window = { localStorage: { getItem: () => 'heuristic-only' } };
  globalThis.HTMLElement = class {};
  try {
    const utils = bindRenderUtils({ getState: () => ({}), documentRef: {} });
    assert.equal(utils.detectLangFromPath('/src/main.rs'), 'rust');
    assert.equal(utils.detectLangFromPath('/src/main.tsx'), 'typescript');
    assert.equal(utils.detectLangFromPath('/src/main.mts'), 'typescript');
    assert.equal(utils.detectLangFromPath('/src/.unknown'), null);
    const meta = { classList: { add() {} }, setAttribute() {}, innerHTML: '' };
    const body = {};
    const row = { children: [meta, body], querySelector: () => meta,
      appendChild(node) { this.children = this.children.filter(child => child !== node); this.children.push(node); } };
    const renderer = bindDiffRendering({
      ...utils, getDiffRow: () => ({ row, block: {} }), isDiffSyntaxEnabled: () => true,
      setLastEventType() {}, maybeAutoScroll() {},
    });
    const patch = '@@ -0,0 +1 @@\n+pub fn main() {}';
    assert.match(renderer.formatDiff(patch, '/src/main.rs'), /hljs-keyword/);
    assert.match(renderer.formatDiff('@@ -0,0 +1 @@\n+interface Example { value: string; }', '/src/main.ts'), /hljs-keyword/);
    assert.doesNotMatch(renderer.formatDiff(patch, '/src/.unknown'), /hljs-/);
    const oldLine = '        from .explorer.contracts.git import parse_git_push_params';
    const newLine = '        from .explorer.contracts.git import parse_git_fetch_params';
    const paired = renderer.formatDiff(`@@ -843 +843 @@\n-${oldLine}\n+${newLine}`, '/src/explorer_runtime.py');
    const table = globalThis.document.createElement('div');
    table.innerHTML = paired;
    const changedLines = [...table.querySelectorAll('.diff-text')].filter(e => e.textContent.includes('parse_git_'));
    assert.equal(changedLines.length, 2);
    for (const line of changedLines) {
      assert.deepEqual([...line.querySelectorAll('.hljs-keyword')].map(e => e.textContent), ['from', 'import']);
      assert.ok(line.querySelector('.diff-intraline-change'));
    }
    assert.equal(changedLines[0].textContent, oldLine);
    assert.equal(changedLines[1].textContent, newLine);
    const escaped = renderer.formatDiff('@@ -1 +1 @@\n-value = "<old>&😀"\n+value = "<new>&😀"', '/src/example.py');
    table.innerHTML = escaped;
    const values = [...table.querySelectorAll('.diff-text')].filter(e => e.textContent.includes('value ='));
    assert.equal(values[0].textContent, 'value = "<old>&😀"');
    assert.equal(values[1].textContent, 'value = "<new>&😀"');
    assert.ok(values[0].querySelector('.hljs-string .diff-intraline-change'));
    renderer.addDiff('test', patch, '/src/main.rs');
    assert.deepEqual(row.children, [body, meta]);
    assert.match(meta.innerHTML, />rust</);
    assert.match(meta.innerHTML, />\+1</);
  } finally {
    Object.assign(globalThis, previous);
  }
});
