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

test('parent rows pass running children; completed child freezes before later work', async () => {
  const { bindTimelineRows } = await load('timeline/rows');
  const { bindSubagentsCollapsible } = await load('subagents/collapsible');
  const { document, window } = parseHTML('<html><body><div id="timeline"><div id="bottom" class="timeline-spacer"></div></div></body></html>');
  const previous = { HTMLElement: globalThis.HTMLElement, Element: globalThis.Element };
  Object.assign(globalThis, { HTMLElement: window.HTMLElement, Element: window.Element });
  try {
    const timeline = document.getElementById('timeline');
    const bottom = document.getElementById('bottom');
    const rows = bindTimelineRows({ getState: () => ({ bottomSpacerEl: bottom, placeholderCleared: true }),
      timelineEl: timeline, documentRef: document, maybeAutoScroll() {} });
    const children = bindSubagentsCollapsible({ documentRef: document, clearPlaceholder() {},
      maybeAutoScroll() {}, insertRow: rows.insertRow });
    const running = { compact: true, status: 'running', text: '', activity: 'Agent running' };
    const child = children.getSubagentContainer('child:1', 'audit', '', running).row;
    const other = children.getSubagentContainer('other:1', 'other', '', running).row;
    assert.equal(child.querySelector('.subagent-latest-message').textContent, 'Agent running');
    const parent = document.createElement('div'); parent.id = 'parent-readme';
    rows.insertRow(parent);
    assert.deepEqual(Array.from(timeline.children), [parent, child, other, bottom]);
    children.getSubagentContainer('child:1', 'audit', '', { ...running, text: 'Shell: cat README.md' });
    assert.deepEqual(Array.from(timeline.children), [parent, child, other, bottom]);
    children.getSubagentContainer('child:1', 'audit', '', { ...running, status: 'completed', text: 'Done' });
    const later = document.createElement('div'); later.id = 'later-parent-command';
    rows.insertRow(later);
    assert.deepEqual(Array.from(timeline.children), [parent, child, later, other, bottom]);
  } finally { Object.assign(globalThis, previous); }
});

test('outer sub-agent header is never selected by the sticky overlay', async () => {
  const { bindTimelineStickyHeaders } = await load('timeline_sticky_headers');
  const { document, window } = parseHTML('<html><body><div id="wrap"><div id="timeline"><div class="timeline-row subagent-card expanded"><div class="subagent-header">child</div><div class="subagent-body"></div></div></div></div></body></html>');
  const previous = { HTMLElement: globalThis.HTMLElement, MutationObserver: globalThis.MutationObserver };
  Object.assign(globalThis, { HTMLElement: window.HTMLElement, MutationObserver: window.MutationObserver });
  const frames = [];
  window.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  window.cancelAnimationFrame = () => {};
  const wrap = document.getElementById('wrap');
  const timeline = document.getElementById('timeline');
  const rect = { top: -100, bottom: 300, left: 0, right: 600, width: 600, height: 400 };
  wrap.getBoundingClientRect = () => ({ ...rect, top: 0, bottom: 400 });
  timeline.firstElementChild.getBoundingClientRect = () => rect;
  timeline.querySelector('.subagent-header').getBoundingClientRect = () => ({ ...rect, height: 30, bottom: -70 });
  try {
    const sticky = bindTimelineStickyHeaders({ timelineWrapEl: wrap, timelineEl: timeline, documentRef: document, windowRef: window });
    for (let index = 0; index < 5 && frames.length; index++) frames.shift()();
    assert.equal(wrap.querySelectorAll('.timeline-sticky-overlay .subagent-header').length, 0);
    assert.equal(sticky.getVisibleHeight(), 0);
    sticky.destroy();
  } finally { Object.assign(globalThis, previous); }
});
