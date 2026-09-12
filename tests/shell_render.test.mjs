import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const result = await build({
  bundle: true,
  entryPoints: [
    'rust/crates/als-server/src/static/js/codex_agent/shell_render.ts',
  ],
  format: 'esm',
  platform: 'node',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { scrollShellOutputToTail } = await import(moduleUrl);

test('streaming shell output advances its internal viewport to the tail', () => {
  const output = { scrollTop: 12, scrollHeight: 640 };
  scrollShellOutputToTail(output);
  assert.equal(output.scrollTop, 640);
});
