import assert from 'node:assert/strict';
import test from 'node:test';

import { build } from 'esbuild';
import hljs from 'highlight.js/lib/core';
import json from 'highlight.js/lib/languages/json';
import plaintext from 'highlight.js/lib/languages/plaintext';
import python from 'highlight.js/lib/languages/python';
import diffModule from 'highlightjs-diff';
import { parseHTML } from 'linkedom';

const result = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/render/utils.ts'],
  bundle: true,
  format: 'esm',
  platform: 'node',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const {
  bindRenderUtils,
  detectsImplicitJsonOutput,
  findGitDiffRanges,
  findImplicitJsonRanges,
} = await import(moduleUrl);

const diffLanguage = typeof diffModule === 'function' ? diffModule : diffModule.default;
hljs.registerLanguage('json', json);
hljs.registerLanguage('plaintext', plaintext);
hljs.registerLanguage('python', python);
hljs.registerLanguage('diff', diffLanguage);
globalThis.hljs = hljs;
const renderDocument = parseHTML('<html><body></body></html>').document;
const utils = bindRenderUtils({ getState: () => ({}), documentRef: renderDocument });

test('implicit JSON accepts complete objects and arrays regardless of HLJS relevance', () => {
  assert.equal(detectsImplicitJsonOutput('{"a":1}'), true);
  assert.equal(detectsImplicitJsonOutput('[1,2,3]'), true);
  assert.equal(detectsImplicitJsonOutput('\n  {"ok":true}\n'), true);
});

test('implicit JSON finds object and array spans inside mixed output', () => {
  const output = 'starting\n{"id":1}\nstatus: ready\n[2,3]\ndone';
  assert.equal(detectsImplicitJsonOutput(output), true);
  assert.deepEqual(
    findImplicitJsonRanges(output).map(({ start, end }) => output.slice(start, end)),
    ['{"id":1}', '[2,3]'],
  );
});

test('implicit JSON finds balanced multi-line values with nested delimiters in strings', () => {
  const output = 'before\n{\n  "nested": {"value": "} ]"},\n  "items": [1, 2]\n}\nafter';
  const ranges = findImplicitJsonRanges(output);
  assert.equal(ranges.length, 1);
  assert.equal(JSON.parse(output.slice(ranges[0].start, ranges[0].end)).items.length, 2);
});

test('malformed prefixes do not hide later valid JSON spans', () => {
  const output = `bad {"unterminated\n${'{'.repeat(256)}{"ok":true}`;
  const ranges = findImplicitJsonRanges(output);
  assert.equal(ranges.length, 1);
  assert.equal(output.slice(ranges[0].start, ranges[0].end), '{"ok":true}');
});

test('implicit JSON rejects scalars, malformed fragments, and bracketed log labels', () => {
  for (const value of [
    'true',
    '42',
    '"text"',
    '{not json}',
    'error: command failed with status 1',
    '[INFO] request complete',
    'prefix {"unfinished": true',
  ]) {
    assert.equal(detectsImplicitJsonOutput(value), false, value);
  }
});

test('mixed rendering highlights only JSON spans and preserves exact text', () => {
  const output = 'status <ready>\n{"a":1}\nrequest complete';
  const html = utils.highlightShellOutput(output, null);
  assert.match(html, /status &lt;ready&gt;/);
  assert.match(html, /class="hljs-attr"/);
  assert.match(html, /request complete$/);
  const document = parseHTML('<html><body><div></div></body></html>').document;
  const host = document.querySelector('div');
  host.innerHTML = html;
  assert.equal(host.textContent, output);
});

test('explicit shell language wins before mixed JSON span rendering', () => {
  const html = utils.highlightShellOutput('{"a":1}', 'plaintext');
  assert.doesNotMatch(html, /class="hljs-attr"/);
  assert.equal(utils.highlightShellOutput('ordinary output', null), null);
});

test('git diff ranges exclude status, stat, and trailing command output', () => {
  const patch = [
    'diff --git a/demo.py b/demo.py',
    'index 1111111..2222222 100644',
    '--- a/demo.py',
    '+++ b/demo.py',
    '@@ -1,5 +1,6 @@',
    ' def run():',
    '-    return False',
    '+    return True',
    '+    print("done")',
  ].join('\n') + '\n';
  const output = [
    ' M demo.py',
    ' demo.py | 3 ++-',
    ' 1 file changed, 2 insertions(+), 1 deletion(-)',
    patch,
    'xrsurge',
    'origin\thttps://example.invalid/repo (fetch)',
  ].join('\n');
  const ranges = findGitDiffRanges(output);
  assert.equal(ranges.length, 1);
  assert.equal(output.slice(ranges[0].start, ranges[0].end), patch);

  const html = utils.highlightShellOutput(output, 'diff');
  const host = renderDocument.createElement('div');
  host.innerHTML = html;
  assert.equal(host.textContent, output);
  assert.match(html, /class="hljs-addition"/);
  assert.match(html, /class="hljs-deletion"/);
  assert.match(html, /class="hljs-keyword"/);
  assert.match(html, /^ M demo\.py/);
  assert.match(html, /xrsurge\norigin/);
});

test('diff highlighting requires a structural patch span', () => {
  assert.equal(utils.highlightShellOutput(' demo.py | 3 ++-\n1 file changed', 'diff'), null);
});

test('each git diff file is isolated when an earlier hunk is truncated', () => {
  const first = [
    'diff --git a/first.py b/first.py',
    '--- a/first.py',
    '+++ b/first.py',
    '@@ -1,4 +1,4 @@',
    '-return False',
    '+return True',
  ].join('\n') + '\n';
  const second = [
    'diff --git a/second.py b/second.py',
    '--- a/second.py',
    '+++ b/second.py',
    '@@ -1 +1 @@',
    '-value = False',
    '+value = True',
  ].join('\n') + '\n';
  const output = `${first}${second}xrsurge\n`;
  const ranges = findGitDiffRanges(output);
  assert.deepEqual(ranges.map(range => output.slice(range.start, range.end)), [first, second]);

  const html = utils.highlightShellOutput(output, 'diff');
  const host = renderDocument.createElement('div');
  host.innerHTML = html;
  assert.equal(host.textContent, output);
  assert.equal((html.match(/hljs-addition/g) || []).length, 2);
  assert.equal((html.match(/hljs-deletion/g) || []).length, 2);
  assert.match(html, /class="hljs-keyword"/);
});
