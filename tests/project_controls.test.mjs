import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

async function load(name) {
  const result = await build({ entryPoints: [`rust/crates/als-server/src/static/js/codex_agent/${name}.ts`], bundle: true, format: 'esm', platform: 'node', write: false });
  return import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
}

test('project headers open suppressed files, actions stay separate, and remote controls are ordered', async () => {
  const { bindProjectModal } = await load('project_modal');
  const { window, document } = parseHTML('<html><body><header><button id="project-te2-control"></button></header><div id="project-body"></div></body></html>');
  const previous = { window: globalThis.window, HTMLElement: globalThis.HTMLElement, Element: globalThis.Element };
  Object.assign(globalThis, { window, HTMLElement: window.HTMLElement, Element: window.Element });
  const opened = [], staged = [], remotes = [];
  const summary = { ok: true, root: '/repo', changed_files: 1, unstaged_files: 1, files: [{ path: 'new.rs', status: 'added', untracked: true, unstaged: true, additions: 2, deletions: 0, bytes: 25 }] };
  try {
    const modal = bindProjectModal({
      documentRef: document, getConversationId: () => 'conv', getConversationCwd: () => '/repo', getProjectRoot: () => '/repo',
      toRelativePath: path => path, detectLangFromPath: () => 'rust', renderDiffBlock() {}, makeCollapsible() {},
      confirmProjectAction: async () => true, showProjectModal() {}, closeConversationModal() {}, isProjectTabActive: () => true,
      uiRpc: {
        getProjectSummary: async () => summary,
        getTe2ProjectStatus: async () => ({ ok: true, connected: true, matches_current: true, action: 'current', target_path: '/repo', current_cwd: '/repo', known: true }),
        openFile: async payload => opened.push(payload),
        stageProjectPaths: async payload => staged.push(payload),
        remoteProject: async payload => { remotes.push(payload); throw new Error('Remote unavailable'); },
      },
    });
    await modal.openProjectModal();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(document.body.textContent.includes('No text diff available.'), false);
    assert.equal(document.querySelector('.project-file-meta .language-rust') !== null, true);
    const header = document.querySelector('.project-file-open-placeholder');
    assert.equal(header.getAttribute('role'), 'link');
    const click = element => element.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
    click(header);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(opened.length, 1);
    click(document.querySelector('[data-project-action="stage-file"]'));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(staged.length, 1);
    assert.equal(opened.length, 1);
    assert.deepEqual(Array.from(document.querySelector('header').children).map(el => el.dataset.remoteAction || el.id), ['fetch', 'pull', 'project-te2-control']);
    const push = document.querySelector('[data-remote-action="push"]');
    assert.equal(push.previousElementSibling.dataset.projectAction, 'commit-project');
    click(push.querySelector('path'));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(remotes, [{ path: '/repo', action: 'push' }]);
    assert.match(document.body.textContent, /Remote unavailable/);
    assert.equal(push.disabled, false);
  } finally { Object.assign(globalThis, previous); }
});

test('toasts preserve literal text, copy the original on repeated taps, and bound the stack', async () => {
  const { showToast } = await load('toast');
  const { document, window } = parseHTML('<html><body></body></html>');
  const copied = [];
  document.execCommand = () => { copied.push(document.querySelector('textarea').value); return true; };
  window.HTMLTextAreaElement.prototype.select = function () {};
  const message = '<b>not markup</b>\n  indented output';
  showToast(message, document);
  const toast = document.querySelector('.als-toast');
  assert.equal(toast.textContent, message);
  assert.equal(toast.querySelector('b'), null);
  for (let i = 0; i < 2; i++) {
    toast.click();
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.deepEqual(copied, [message, message]);
  assert.equal(toast.querySelector('.als-toast-feedback').textContent, 'Copied');
  for (let i = 0; i < 6; i++) showToast(String(i), document);
  assert.equal(document.querySelectorAll('.als-toast').length, 4);
});
