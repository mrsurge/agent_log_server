import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const result = await build({
  bundle: true,
  entryPoints: [
    'rust/crates/als-server/src/static/js/codex_agent/settings/ui_flow.ts',
  ],
  format: 'esm',
  platform: 'node',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { mentionPickerItemPath } = await import(moduleUrl);

test('mention picker resolves selected files and directories by path', () => {
  assert.equal(mentionPickerItemPath({ path: '/repo/src', type: 'directory' }), '/repo/src');
  assert.equal(mentionPickerItemPath({ path: '/repo/src/main.rs', type: 'file' }), '/repo/src/main.rs');
});

test('mention picker falls back to the item name and rejects empty targets', () => {
  assert.equal(mentionPickerItemPath({ name: 'README.md' }), 'README.md');
  assert.equal(mentionPickerItemPath({ path: '   ' }), '');
  assert.equal(mentionPickerItemPath(null), '');
});
