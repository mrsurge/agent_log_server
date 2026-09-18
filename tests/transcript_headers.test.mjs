import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

async function load(name) {
  const result = await build({ entryPoints: [`rust/crates/als-server/src/static/js/codex_agent/${name}.ts`], bundle: true, format: 'esm', platform: 'node', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

test('shell headers colour only the command and running state ends with shell_end', async () => {
  const { bindShellRender, renderShellSummary } = await load('shell_render');
  const { document, window } = parseHTML('<html><body><div id="agent-timeline"></div></body></html>');
  const previous = { document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  Object.assign(globalThis, { document, HTMLElement: window.HTMLElement });
  try {
    const summary = document.createElement('span');
    renderShellSummary(summary, 'python script.py --help');
    assert.equal(summary.querySelector('.shell-command-name').textContent, 'python');
    assert.equal(summary.textContent, '$ python script.py --help');
    const timeline = document.getElementById('agent-timeline');
    const shells = new Map();
    const renderer = bindShellRender({
      shellRows: shells, clearPlaceholder() {}, insertRow: row => timeline.append(row), makeCollapsible() {},
      renderShellCmdRibbon: (el, cmd) => { el.textContent = cmd; }, postTe2OpenRequest() {},
      detectLangFromCommand: () => null, highlightCodeAlways: text => text, setStatusDot() {}, setActivity() {}, maybeAutoScroll() {},
    });
    renderer.renderShellBegin({ id: 'one', command: 'python script.py' });
    const row = timeline.firstElementChild;
    assert.equal(row.classList.contains('shell-running'), true);
    assert.equal(row.getAttribute('aria-busy'), 'true');
    renderer.renderShellDelta({ id: 'one', delta: 'hi' });
    assert.equal(row.classList.contains('shell-running'), true);
    renderer.renderShellEnd({ id: 'one', command: 'python script.py', exitCode: 0, stdout: 'hi' });
    assert.equal(row.classList.contains('shell-running'), false);
    assert.equal(shells.size, 0);
  } finally { Object.assign(globalThis, previous); }
});

test('diff path basename styling preserves literal path text in DOM and HTML renderers', async () => {
  const { createPathScrollLabel, pathScrollLabelHtml } = await load('path_label');
  const { document, window } = parseHTML('<html><body></body></html>');
  const previous = globalThis.HTMLElement;
  globalThis.HTMLElement = window.HTMLElement;
  try {
    const path = 'src/nested/a<b>.ts';
    const label = createPathScrollLabel(document, path, { basenameClass: 'diff-filename', title: path });
    assert.equal(label.textContent, path);
    assert.equal(label.querySelector('.diff-filename').textContent, 'a<b>.ts');
    assert.equal(label.title, path);
    const escape = value => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
    const host = document.createElement('div');
    host.innerHTML = pathScrollLabelHtml(path, escape, { basenameClass: 'diff-filename' });
    assert.equal(host.textContent, path);
    assert.equal(host.querySelector('.diff-filename').textContent, 'a<b>.ts');
    assert.equal(host.querySelector('b'), null);
  } finally { globalThis.HTMLElement = previous; }
});

test('idle placeholder never overwrites active activity or spinner state', async () => {
  const { bindTimelineRows } = await load('timeline/rows');
  const { document } = parseHTML('<html><body><div id="ribbon"><span id="label"></span></div></body></html>');
  const ribbon = document.getElementById('ribbon');
  const label = document.getElementById('label');
  const rows = bindTimelineRows({ documentRef: document, statusRibbonEl: ribbon, statusLabelEl: label });
  rows.setIdleLabel('als_rs / main / abc123');
  assert.equal(label.textContent, 'als_rs / main / abc123');
  rows.setActivity('executing', true);
  rows.setIdleLabel('als_rs / feature / def456');
  assert.equal(label.textContent, 'executing');
  assert.equal(ribbon.classList.contains('active'), true);
  rows.setActivity('idle', false);
  assert.equal(label.textContent, 'als_rs / feature / def456');
  rows.setActivity('Transcript import failed', false);
  rows.setIdleLabel('als_rs / main / abc123');
  assert.equal(label.textContent, 'Transcript import failed');
});

test('tool headers use separate prefix and command, with meaningful shell interaction labels', async () => {
  const { bindToolRender } = await load('tool_render');
  const { document, window } = parseHTML('<html><body></body></html>');
  const previous = { document: globalThis.document, HTMLElement: globalThis.HTMLElement };
  Object.assign(globalThis, { document, HTMLElement: window.HTMLElement });
  try {
    const renderer = bindToolRender({
      makeCollapsible() {}, toRelativePath: path => path,
      renderShellCmdRibbon: (el, text) => { el.innerHTML = ''; const code = document.createElement('code'); code.textContent = text; el.append(code); },
    });
    const tool = renderer.buildReplayToolRow({ id: 'tool-1', server: 'mcp', tool: 'search' });
    assert.equal(tool.querySelector('.tool-command-prefix').textContent, 'mcp');
    assert.equal(tool.querySelector('.command-ribbon').textContent, 'mcp:search');
    const read = renderer.buildReplayToolRow({ id: 'read-1', tool: 'read_shell' });
    assert.equal(read.querySelector('.command-ribbon').textContent, 'Reading shell');
    const patch = renderer.buildReplayToolRow({ id: 'patch-1', tool: 'apply_patch', path: 'src/file.ts' });
    assert.equal(patch.querySelector('.patch-filename').textContent, 'file.ts');
    assert.match(patch.querySelector('.patch-collapsed-summary').textContent, /apply_patch src\/file.ts/);
  } finally { Object.assign(globalThis, previous); }
});

test('expanded cards retain their state while only the latest expansion is emphasized', async () => {
  const { bindSubagentsCollapsible } = await load('subagents/collapsible');
  const { document, window } = parseHTML('<html><body><div id="agent-timeline"></div></body></html>');
  const old = { HTMLElement: globalThis.HTMLElement, Element: globalThis.Element };
  Object.assign(globalThis, { HTMLElement: window.HTMLElement, Element: window.Element });
  try {
    const runtime = bindSubagentsCollapsible({ documentRef: document, maybeAutoScroll() {} });
    const timeline = document.getElementById('agent-timeline');
    const cards = ['one', 'two'].map(id => {
      const row = document.createElement('div');
      row.innerHTML = '<div class="body"><div class="command-ribbon">command</div></div>';
      timeline.append(row);
      runtime.makeCollapsible(row, id, false);
      return row;
    });
    cards[0]._toggleCollapse();
    cards[1]._toggleCollapse();
    assert.equal(cards[0].classList.contains('expanded'), true);
    assert.equal(cards[0].classList.contains('expanded-mru'), false);
    assert.equal(cards[1].classList.contains('expanded-mru'), true);
    assert.equal(cards[1].querySelector('.ribbon-toggle-zone').getAttribute('aria-expanded'), 'true');
    cards[0]._toggleCollapse(true);
    assert.equal(cards[0].classList.contains('expanded-mru'), false);
    assert.equal(cards[1].classList.contains('expanded-mru'), true);
    cards[1]._toggleCollapse(false);
    assert.equal(cards[1].classList.contains('expanded-mru'), false);
  } finally { Object.assign(globalThis, old); }
});
