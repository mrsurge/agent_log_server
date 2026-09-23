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
const {
  COMPOSER_PAIR_GESTURE_TIMEOUT_MS,
  planComposerAutoPairEdit,
} = await import(moduleUrl);

const insert = (data) => ({ inputType: 'insertText', data });
const backspace = { inputType: 'deleteContentBackward', data: null };

function plan(event, draft, selection, state = null, now = 1_000) {
  return planComposerAutoPairEdit(event, draft, selection, state, now);
}

test('opening brackets, double quotes, and backticks insert fresh pairs', () => {
  for (const [opening, closing] of Object.entries({
    '(': ')', '[': ']', '{': '}', '"': '"', '`': '`',
  })) {
    const result = plan(insert(opening), 'a b', { anchor: 2, focus: 2 });
    assert.deepEqual(result.edit, {
      draft: `a ${opening}${closing}b`,
      selection: { anchor: 3, focus: 3 },
      action: 'insert',
    });
    assert.deepEqual(result.state, {
      kind: 'fresh_pair',
      opening,
      closing,
      start: 2,
      expiresAt: 1_000 + COMPOSER_PAIR_GESTURE_TIMEOUT_MS,
    });
  }
});

test('all pair characters wrap selections and preserve selection direction', () => {
  for (const [opening, closing] of Object.entries({
    '(': ')', '[': ']', '{': '}', '"': '"', "'": "'", '`': '`',
  })) {
    assert.deepEqual(plan(insert(opening), 'alpha', { anchor: 4, focus: 1 }), {
      edit: {
        draft: `a${opening}lph${closing}a`,
        selection: { anchor: 5, focus: 2 },
        action: 'insert',
      },
      state: null,
    });
  }
});

test('typing an existing closer advances without duplication', () => {
  assert.deepEqual(plan(insert(')'), 'call()', { anchor: 5, focus: 5 }), {
    edit: {
      draft: 'call()',
      selection: { anchor: 6, focus: 6 },
      action: 'skip',
    },
    state: null,
  });
});

test('ordinary single quote insertion stays native', () => {
  assert.deepEqual(plan(insert("'"), 'dont', { anchor: 3, focus: 3 }), {
    edit: null,
    state: {
      kind: 'single_quote_start',
      start: 3,
      expiresAt: 1_000 + COMPOSER_PAIR_GESTURE_TIMEOUT_MS,
    },
  });
});

test('two native single quotes form a pair and a fresh third quote moves after it', () => {
  const first = plan(insert("'"), '', { anchor: 0, focus: 0 });
  const second = plan(insert("'"), "'", { anchor: 1, focus: 1 }, first.state, 1_100);
  assert.deepEqual(second.edit, {
    draft: "''",
    selection: { anchor: 1, focus: 1 },
    action: 'insert',
  });
  const third = plan(insert("'"), "''", second.edit.selection, second.state, 1_200);
  assert.deepEqual(third, {
    edit: {
      draft: "'''",
      selection: { anchor: 3, focus: 3 },
      action: 'insert',
    },
    state: null,
  });
});

test('fresh double quote and backtick pairs append a third character', () => {
  for (const character of ['"', '`']) {
    const pair = plan(insert(character), '', { anchor: 0, focus: 0 });
    const triple = plan(insert(character), `${character}${character}`, pair.edit.selection, pair.state, 1_100);
    assert.deepEqual(triple, {
      edit: {
        draft: `${character}${character}${character}`,
        selection: { anchor: 3, focus: 3 },
        action: 'insert',
      },
      state: null,
    });
  }
});

test('expired quote gesture skips the existing closer instead of creating a triple', () => {
  const pair = plan(insert('"'), '', { anchor: 0, focus: 0 });
  assert.deepEqual(
    plan(
      insert('"'),
      '""',
      pair.edit.selection,
      pair.state,
      1_000 + COMPOSER_PAIR_GESTURE_TIMEOUT_MS + 1,
    ),
    {
      edit: {
        draft: '""',
        selection: { anchor: 2, focus: 2 },
        action: 'skip',
      },
      state: null,
    },
  );
});

test('immediate bracket backspace removes both sides and enables character-specific single entry', () => {
  const pair = plan(insert('('), 'ab', { anchor: 1, focus: 1 });
  const deleted = plan(backspace, 'a()b', pair.edit.selection, pair.state, 1_100);
  assert.deepEqual(deleted.edit, {
    draft: 'ab',
    selection: { anchor: 1, focus: 1 },
    action: 'delete',
  });
  assert.deepEqual(deleted.state, {
    kind: 'single_entry',
    character: '(',
    expiresAt: 1_100 + COMPOSER_PAIR_GESTURE_TIMEOUT_MS,
  });

  const repeated = plan(insert('('), 'ab', deleted.edit.selection, deleted.state, 1_200);
  assert.equal(repeated.edit, null);
  assert.deepEqual(repeated.state, deleted.state);

  const cancelled = plan(insert('x'), 'a(b', { anchor: 2, focus: 2 }, repeated.state, 1_300);
  assert.deepEqual(cancelled, { edit: null, state: null });
});

test('single-entry suppression expires and pairing resumes', () => {
  const state = {
    kind: 'single_entry',
    character: '[',
    expiresAt: 1_800,
  };
  const result = plan(insert('['), '', { anchor: 0, focus: 0 }, state, 1_801);
  assert.equal(result.edit.draft, '[]');
  assert.equal(result.state.kind, 'fresh_pair');
});

test('immediate double quote and backtick backspace retain the closer and move after it', () => {
  for (const character of ['"', '`']) {
    const pair = plan(insert(character), 'ab', { anchor: 1, focus: 1 });
    assert.deepEqual(plan(backspace, `a${character}${character}b`, pair.edit.selection, pair.state, 1_100), {
      edit: {
        draft: `a${character}b`,
        selection: { anchor: 2, focus: 2 },
        action: 'delete',
      },
      state: null,
    });
  }
});

test('typing content cancels the fresh-pair delete gesture', () => {
  const pair = plan(insert('{'), '', { anchor: 0, focus: 0 });
  const content = plan(insert('x'), '{}', pair.edit.selection, pair.state, 1_100);
  assert.deepEqual(content, { edit: null, state: null });
  assert.deepEqual(plan(backspace, '{x}', { anchor: 2, focus: 2 }, content.state, 1_200), {
    edit: null,
    state: null,
  });
});

test('moved caret prevents a stale fresh-pair deletion', () => {
  const pair = plan(insert('['), 'ab', { anchor: 1, focus: 1 });
  assert.deepEqual(plan(backspace, 'a[]b', { anchor: 3, focus: 3 }, pair.state, 1_100), {
    edit: null,
    state: null,
  });
});
