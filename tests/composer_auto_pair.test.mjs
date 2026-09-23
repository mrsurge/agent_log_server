import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import test from 'node:test';

import { build } from 'esbuild';

const result = await build({
  entryPoints: [resolve(
    import.meta.dirname,
    '../rust/crates/als-server/src/static/js/codex_agent/composer/runtime.ts',
  )],
  bundle: true,
  format: 'esm',
  platform: 'neutral',
  target: 'es2020',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { planComposerAutoPairEdit } = await import(moduleUrl);

test('opening brackets and quotes insert pairs around the caret', () => {
  for (const [opening, closing] of Object.entries({
    '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`',
  })) {
    const edit = planComposerAutoPairEdit(opening, 'a b', { anchor: 2, focus: 2 });
    assert.deepEqual(edit, {
      draft: `a ${opening}${closing}b`,
      selection: { anchor: 3, focus: 3 },
      action: 'insert',
    });
  }
});

test('auto-pairs wrap selections and preserve selection direction', () => {
  assert.deepEqual(planComposerAutoPairEdit('[', 'alpha', { anchor: 4, focus: 1 }), {
    draft: 'a[lph]a',
    selection: { anchor: 5, focus: 2 },
    action: 'insert',
  });
});

test('typing an existing closer advances without duplication', () => {
  assert.deepEqual(planComposerAutoPairEdit(')', 'call()', { anchor: 5, focus: 5 }), {
    draft: 'call()',
    selection: { anchor: 6, focus: 6 },
    action: 'skip',
  });
});

test('single quotes after word characters remain apostrophes', () => {
  assert.equal(planComposerAutoPairEdit("'", 'dont', { anchor: 3, focus: 3 }), null);
  assert.equal(planComposerAutoPairEdit("'", '123', { anchor: 3, focus: 3 }), null);
});
