import assert from 'node:assert/strict';
import test from 'node:test';

import { build } from 'esbuild';
import hljs from 'highlight.js/lib/core';
import json from 'highlight.js/lib/languages/json';
import plaintext from 'highlight.js/lib/languages/plaintext';
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
  findImplicitJsonRanges,
} = await import(moduleUrl);

hljs.registerLanguage('json', json);
hljs.registerLanguage('plaintext', plaintext);
globalThis.hljs = hljs;
const utils = bindRenderUtils({ getState: () => ({}), documentRef: {} });

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
