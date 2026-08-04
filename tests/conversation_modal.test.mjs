import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { test } from 'node:test';

import { build } from 'esbuild';

async function loadConversationModal() {
  const result = await build({
    entryPoints: [resolve(
      import.meta.dirname,
      '../rust/crates/als-server/src/static/js/codex_agent/conversation_modal.ts',
    )],
    bundle: true,
    format: 'esm',
    platform: 'neutral',
    target: 'es2020',
    write: false,
  });
  const source = result.outputFiles[0].text;
  return import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
}

class FakeClassList {
  values = new Set();

  add(...names) {
    names.forEach((name) => this.values.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.values.delete(name));
  }

  contains(name) {
    return this.values.has(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : force;
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }
}

class FakeElement {
  constructor(id) {
    this.id = id;
  }

  classList = new FakeClassList();
  attributes = new Map();
  listeners = new Map();
  disabled = false;
  title = '';
  focused = false;

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name, init = {}) {
    const event = {
      target: this,
      currentTarget: this,
      preventDefault() {},
      ...init,
    };
    for (const listener of this.listeners.get(name) || []) listener(event);
  }

  click() {
    this.dispatch('click');
  }

  focus() {
    this.focused = true;
  }
}

class FakeDocument {
  constructor(elements) {
    this.elements = elements;
  }

  listeners = new Map();

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
}

function createFixture() {
  const ids = [
    'conversation-modal',
    'conversation-modal-settings-tab',
    'conversation-modal-project-tab',
    'conversation-modal-settings-panel',
    'conversation-modal-project-panel',
    'conversation-modal-project-header-actions',
    'conversation-modal-settings-footer',
    'conversation-modal-project-footer',
    'conversation-modal-close',
  ];
  const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
  elements.get('conversation-modal').classList.add('hidden');
  return { document: new FakeDocument(elements), elements };
}

test('shared modal switches panels and routes close through the active tab', async () => {
  const { bindConversationModal } = await loadConversationModal();
  const { document, elements } = createFixture();
  const requests = [];
  const closes = [];
  let modal;
  modal = bindConversationModal({
    documentRef: document,
    getProjectDisabled: () => false,
    onTabRequest: (tab) => {
      requests.push(tab);
      modal.setActiveTab(tab);
    },
    onCloseRequest: (tab) => {
      closes.push(tab);
      modal.hide();
    },
  });

  assert.equal(modal.show('settings'), true);
  assert.equal(elements.get('conversation-modal').classList.contains('hidden'), false);
  assert.equal(elements.get('conversation-modal-settings-panel').classList.contains('hidden'), false);
  assert.equal(elements.get('conversation-modal-project-panel').classList.contains('hidden'), true);

  elements.get('conversation-modal-project-tab').click();
  assert.deepEqual(requests, ['project']);
  assert.equal(modal.isTabActive('project'), true);
  assert.equal(elements.get('conversation-modal-project-header-actions').classList.contains('hidden'), false);
  assert.equal(elements.get('conversation-modal-settings-footer').classList.contains('hidden'), true);
  assert.equal(elements.get('conversation-modal-project-footer').classList.contains('hidden'), false);

  elements.get('conversation-modal-close').click();
  assert.deepEqual(closes, ['project']);
  assert.equal(modal.isOpen(), false);
});

test('project tab is unavailable during pending new-conversation setup', async () => {
  const { bindConversationModal } = await loadConversationModal();
  const { document, elements } = createFixture();
  const requests = [];
  let pendingNewConversation = true;
  const modal = bindConversationModal({
    documentRef: document,
    getProjectDisabled: () => pendingNewConversation,
    onTabRequest: (tab) => requests.push(tab),
    onCloseRequest() {},
  });

  modal.show('settings');
  const projectTab = elements.get('conversation-modal-project-tab');
  assert.equal(projectTab.disabled, true);
  assert.equal(projectTab.getAttribute('aria-disabled'), 'true');
  assert.equal(modal.setActiveTab('project'), false);
  projectTab.click();
  assert.deepEqual(requests, []);

  pendingNewConversation = false;
  modal.syncProjectAvailability();
  assert.equal(projectTab.disabled, false);
  assert.equal(projectTab.getAttribute('aria-disabled'), 'false');
});
