import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';
import { readFile } from 'node:fs/promises';

async function load(name) {
  const result = await build({
    entryPoints: [`rust/crates/als-server/src/static/js/codex_agent/${name}.ts`],
    bundle: true, format: 'esm', platform: 'node', write: false,
  });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

test('compact live/replay cards retain only the latest message and preserve independent turns', async () => {
  const { bindSubagentsCollapsible } = await load('subagents/collapsible');
  const { bindTimelineReplay } = await load('timeline/replay');
  const { document, window } = parseHTML('<html><body><div id="live"></div><div id="replay"></div></body></html>');
  const previous = { document: globalThis.document, window: globalThis.window,
    HTMLElement: globalThis.HTMLElement, Element: globalThis.Element };
  Object.assign(globalThis, { document, window, HTMLElement: window.HTMLElement, Element: window.Element });
  try {
    const live = document.getElementById('live');
    const replay = document.getElementById('replay');
    const runtime = bindSubagentsCollapsible({
      documentRef: document, clearPlaceholder() {}, maybeAutoScroll() {}, insertRow: row => live.append(row),
    });
    const base = { id: 'child:turn-1', name: 'audit', agent_path: '/root/audit', compact: true,
      status: 'running', text: '', projection_card_id: 'subagent:child:turn-1', projection_card_index: 0 };
    const snapshots = [base, { ...base, text: 'First message' },
      { ...base, text: '<script>literal</script>\nLatest message', status: 'completed' }];
    for (const snapshot of snapshots) {
      runtime.getSubagentContainer(snapshot.id, snapshot.name, '', snapshot);
    }
    assert.equal(live.children.length, 1);
    const row = live.firstElementChild;
    row._toggleCollapse(true);
    assert.equal(row.querySelector('.subagent-latest-message').textContent, snapshots[2].text);
    assert.equal(row.querySelector('script'), null);
    assert.equal(row.querySelector('.subagent-preview').textContent, '<script>literal</script> Latest message');
    assert.equal(row.querySelector('.subagent-status').textContent, 'completed');
    runtime.getSubagentContainer(base.id, base.name, '', snapshots[2]);
    assert.equal(row.classList.contains('expanded'), true);

    const noop = () => {};
    const replayContext = new Proxy({
      documentRef: document, timelineEl: replay, getState: () => ({}),
      makeCollapsible: runtime.makeCollapsible,
    }, { get: (object, key) => key in object ? object[key] : noop });
    const renderer = bindTimelineReplay(replayContext);
    const entries = snapshots.map((snapshot, i) => ({ ...snapshot, role: i === 2 ? 'subagent_end' : 'subagent_start' }));
    renderer.renderTranscriptEntries(entries);
    assert.equal(replay.children.length, 1);
    assert.equal(replay.querySelector('.subagent-latest-message').textContent, snapshots[2].text);
    assert.equal(replay.querySelector('.subagent-status').textContent, 'completed');
    // A separately hydrated update must find the existing card, not duplicate it.
    renderer.renderTranscriptEntries([entries[2]]);
    assert.equal(replay.children.length, 1);
    // Rehydrating a projection replaces DOM, but the live controller survives.
    live.replaceChildren(replay.firstElementChild.cloneNode(true));
    runtime.getSubagentContainer(base.id, base.name, '', { ...snapshots[2], text: 'After reconnect' });
    assert.equal(live.children.length, 1);
    assert.equal(live.querySelector('.subagent-latest-message').textContent, 'After reconnect');
    const followup = { ...base, id: 'child:turn-2', projection_card_id: 'subagent:child:turn-2', projection_card_index: 1 };
    runtime.getSubagentContainer(followup.id, followup.name, '', followup);
    renderer.renderTranscriptEntries([{ ...followup, role: 'subagent_start' }]);
    assert.equal(live.children.length, 2);
    assert.equal(replay.children.length, 2);
    assert.equal(replay.firstElementChild.querySelector('.subagent-latest-message').textContent, snapshots[2].text);
    assert.equal(replay.lastElementChild.querySelector('.subagent-latest-message').getAttribute('role'), 'region');
  } finally {
    Object.assign(globalThis, previous);
  }
});

test('latest-message viewport is fixed and scrollable without unbounded header text', async () => {
  const css = await readFile('rust/crates/als-server/src/static/codex_agent.css', 'utf8');
  const viewport = css.match(/\.subagent-latest-message\s*\{([^}]+)\}/)?.[1];
  assert.match(viewport, /height:\s*16rem/);
  assert.match(viewport, /max-height:\s*45vh/);
  assert.match(viewport, /overflow:\s*auto/);
  const preview = css.match(/\.subagent-compact \.subagent-preview\s*\{([^}]+)\}/)?.[1];
  assert.match(preview, /text-overflow:\s*ellipsis/);
});
