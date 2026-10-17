import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

const compiled = await build({
  entryPoints: ['rust/crates/als-server/src/static/js/codex_agent/project_modal.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const { bindProjectModal } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
const settle = () => new Promise((done) => setImmediate(done));

async function harness(t, { path = '/repo/file.rs', action = 'switch', choice = 'external', hunk = false,
  switchOk = true, delayedProjection = false } = {}) {
  const { window, document } = parseHTML('<html><body><button id="project-te2-control"></button><div id="project-body"></div></body></html>');
  const previous = { window: globalThis.window, HTMLElement: globalThis.HTMLElement, Element: globalThis.Element };
  Object.assign(globalThis, { window, HTMLElement: window.HTMLElement, Element: window.Element });
  t.after(() => Object.assign(globalThis, previous));
  const calls = [], questions = [];
  const state = { action, conversation: 'conversation' };
  const modal = bindProjectModal({
    documentRef: document, getConversationId: () => state.conversation,
    getConversationCwd: () => '/repo', getProjectRoot: () => '/other',
    toRelativePath: (value) => value, detectLangFromPath: () => 'rust',
    renderDiffBlock(block) {
      const line = document.createElement('div');
      line.className = 'diff-line'; line.dataset.path = path; line.dataset.newLine = '23';
      block.append(line);
    },
    makeCollapsible() {}, showProjectModal() {}, closeConversationModal() {},
    isProjectTabActive: () => true,
    confirmProjectAction: async () => { calls.push('explicit-confirm'); return true; },
    chooseProjectFileAction: async (options) => { questions.push(options); return typeof choice === 'function' ? choice() : choice; },
    uiRpc: {
      getProjectSummary: async () => ({ ok: true, root: '/repo', changed_files: 1,
        files: [{ path, status: 'added', untracked: true, unstaged: true, diff_text: hunk ? '+line' : '' }] }),
      getTe2ProjectStatus: async () => {
        calls.push('status');
        return { ok: true, connected: true, action: state.action, target_path: '/repo', current_cwd: '/other' };
      },
      openTe2Project: async () => {
        calls.push('switch');
        if (switchOk && !delayedProjection) state.action = 'current';
        return { ok: switchOk };
      },
      createTe2Project: async () => { calls.push('create'); state.action = 'current'; return { ok: true }; },
      openFile: async (payload) => { calls.push(payload); return { ok: true }; },
    },
  });
  await modal.openProjectModal(); await settle(); calls.length = 0;
  return { document, state, calls, questions, async open() {
    const target = document.querySelector(hunk ? '.diff-line' : '.project-file-open-placeholder');
    assert.ok(target, 'real file/hunk renderer supplied an open target');
    target.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
    await settle();
  } };
}

for (const path of ['/outside/file.rs', '/repo-other/file.rs', '/repo/../outside/file.rs']) {
  test(`outside-CWD file bypasses project-switch gating: ${path}`, async (t) => {
    const h = await harness(t, { path });
    await h.open();
    assert.deepEqual(h.calls, [{ path, line: 1, column: 1 }]);
    assert.equal(h.questions.length, 0);
  });
}

for (const choice of ['switch', 'external', 'cancel']) {
  for (const hunk of [false, true]) {
    test(`${hunk ? 'hunk' : 'header'} offers ${choice} when TE2 is on another project`, async (t) => {
      const h = await harness(t, { choice, hunk });
      await h.open();
      assert.equal(h.questions.length, 1);
      assert.match(h.questions[0].body, /switch TE2 to \/repo/);
      const opened = h.calls.filter((value) => typeof value === 'object');
      assert.deepEqual(opened, choice === 'cancel' ? [] : [{ path: '/repo/file.rs', line: hunk ? 23 : 1, column: 1 }]);
      assert.equal(h.calls.includes('switch'), choice === 'switch');
      assert.equal(h.calls.includes('explicit-confirm'), false, 'no second switch prompt');
    });
  }
}

test('already-current project opens without a question', async (t) => {
  const h = await harness(t, { action: 'current' }); await h.open();
  assert.equal(h.questions.length, 0);
  assert.equal(h.calls.filter((value) => typeof value === 'object').length, 1);
});

test('unknown project uses explicit create consent, with no second prompt', async (t) => {
  const h = await harness(t, { action: 'create', choice: 'switch' }); await h.open();
  assert.equal(h.questions[0].confirmText, 'Create project and open');
  assert.equal(h.calls.includes('create'), true);
  assert.equal(h.calls.includes('explicit-confirm'), false);
});

test('conversation change while the question is open cancels stale navigation', async (t) => {
  let answer;
  const h = await harness(t, { choice: () => new Promise((done) => { answer = done; }) });
  await h.open(); h.state.conversation = 'another'; answer('switch'); await settle();
  assert.deepEqual(h.calls, ['status']);
});

test('explicit Switch TE2 control retains its own confirmation', async (t) => {
  const h = await harness(t);
  h.document.getElementById('project-te2-control').click(); await settle();
  assert.equal(h.calls.includes('explicit-confirm'), true);
  assert.equal(h.calls.includes('switch'), true);
  assert.equal(h.calls.some((value) => typeof value === 'object'), false);
});

test('successful backend switch permits opening before host-UI projection catches up', async (t) => {
  const h = await harness(t, { choice: 'switch', delayedProjection: true }); await h.open();
  assert.equal(h.calls.includes('switch'), true);
  assert.equal(h.calls.filter((value) => typeof value === 'object').length, 1);
});

test('failed switch never opens the file or retries the mutation', async (t) => {
  const h = await harness(t, { choice: 'switch', switchOk: false }); await h.open();
  assert.equal(h.calls.filter((value) => value === 'switch').length, 1);
  assert.equal(h.calls.some((value) => typeof value === 'object'), false);
});
