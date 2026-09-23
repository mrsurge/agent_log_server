import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import test from 'node:test';

import { build } from 'esbuild';

const result = await build({
  entryPoints: [resolve(
    import.meta.dirname,
    '../rust/crates/als-server/src/static/js/codex_agent/conversation_drawer/list.ts',
  )],
  bundle: true,
  format: 'esm',
  platform: 'neutral',
  target: 'es2020',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { filterMiniConversations } = await import(moduleUrl);

const conversations = [
  { conversation_id: 'root', settings: { cwd: '/work/project' } },
  { conversation_id: 'child', cwd: '/work/project/packages/app' },
  { conversation_id: 'other', settings: { cwd: '/work/other' } },
  { conversation_id: 'missing' },
  { conversation_id: 'corrupt', integrity: 'corrupt', cwd: '/work/other' },
];

test('standalone minibar keeps the complete canonical conversation list', () => {
  assert.deepEqual(
    filterMiniConversations(conversations, { ideMode: false, projectRoot: '/work/project' }, 'project'),
    conversations,
  );
});

test('TE2 minibar reuses project filtering and preserves integrity diagnostics', () => {
  const filtered = filterMiniConversations(conversations, {
    ideMode: true,
    projectRoot: '/work/project',
  }, 'project');
  assert.deepEqual(filtered.map((item) => item.conversation_id), ['root', 'child', 'corrupt']);
});

test('TE2 minibar inherits All from the shared splash setting', () => {
  assert.deepEqual(
    filterMiniConversations(conversations, { ideMode: true, projectRoot: '/work/project' }, 'all'),
    conversations,
  );
});

test('TE2 project scope waits for the sidebar project root', () => {
  assert.deepEqual(filterMiniConversations(conversations, { ideMode: true }, 'project'), [conversations[4]]);
});
