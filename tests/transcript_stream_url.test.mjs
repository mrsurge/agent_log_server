import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const result = await build({
  bundle: true,
  entryPoints: [
    'rust/crates/als-server/src/static/js/codex_agent/transcript_stream_url.ts',
  ],
  format: 'esm',
  platform: 'node',
  write: false,
});
const source = result.outputFiles[0].text;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const { buildTranscriptStreamUrl } = await import(moduleUrl);

test('uses the direct transcript route for standalone ALS', () => {
  const location = new URL('http://127.0.0.1:3000/');
  assert.equal(
    buildTranscriptStreamUrl(location),
    'ws://127.0.0.1:3000/ws/transcript',
  );
});

test('keeps the TE2 proxy-shell mount exactly once', () => {
  const location = new URL(
    'http://100.91.80.45:8089/api/app/als-rs/proxy/?conversation_id=example',
  );
  assert.equal(
    buildTranscriptStreamUrl(location),
    'ws://100.91.80.45:8089/api/app/als-rs/proxy/ws/transcript',
  );
});

test('uses secure WebSockets for HTTPS pages', () => {
  const location = new URL('https://example.test/api/app/als-rs/proxy/');
  assert.equal(
    buildTranscriptStreamUrl(location),
    'wss://example.test/api/app/als-rs/proxy/ws/transcript',
  );
});
