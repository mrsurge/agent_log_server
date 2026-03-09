export function createConversationDrawerList(ctx) {
  const {
    conversationListEl,
    getHostUi,
    getSplashTab,
    getConversationPreview,
    selectConversation,
    selectConversationWithView,
    openSettingsModal,
    deleteConversation,
    documentRef,
    windowRef,
  } = ctx;

  function isConversationInProject(meta) {
    const hostUi = (typeof getHostUi === 'function' ? getHostUi() : null) || {};
    const root = hostUi.projectRoot;
    if (!root || typeof root !== 'string') return false;
    const cwd = meta?.settings?.cwd;
    if (!cwd || typeof cwd !== 'string') return false;
    if (cwd === root) return true;
    const rootNorm = root.endsWith('/') ? root : `${root}/`;
    return cwd.startsWith(rootNorm);
  }

  function renderSplashTabs() {
    const doc = documentRef || document;
    const splashTab = typeof getSplashTab === 'function' ? getSplashTab() : 'all';
    const allBtn = doc.getElementById('splash-tab-all');
    const projectBtn = doc.getElementById('splash-tab-project');
    if (allBtn) allBtn.classList.toggle('active', splashTab === 'all');
    if (projectBtn) projectBtn.classList.toggle('active', splashTab === 'project');
  }

  function renderConversationList(items, activeId) {
    if (!conversationListEl) return;
    conversationListEl.innerHTML = '';
    const hostUi = (typeof getHostUi === 'function' ? getHostUi() : null) || {};
    const splashTab = typeof getSplashTab === 'function' ? getSplashTab() : 'all';
    let list = items || [];
    if (hostUi.ideMode && splashTab === 'project') {
      list = list.filter(isConversationInProject);
    }
    if (!list || !list.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = splashTab === 'project' ? 'No project conversations yet.' : 'No conversations yet.';
      conversationListEl.appendChild(empty);
      return;
    }
    list.forEach((meta) => {
      if (!meta) return;
      const row = document.createElement('div');
      row.className = 'conversation-row';
      if (meta.conversation_id && meta.conversation_id === activeId) {
        row.classList.add('active');
      }
      const info = document.createElement('div');
      info.className = 'conversation-meta';
      const labelRow = document.createElement('div');
      labelRow.className = 'conversation-label-line';
      labelRow.textContent = (meta.settings && meta.settings.label) ? meta.settings.label : '';
      const title = document.createElement('div');
      const alias = typeof meta?.settings?.alias === 'string' ? meta.settings.alias.trim() : '';
      title.textContent = alias || meta.conversation_id || 'conversation';
      const threadText = meta.thread_id ? `thread: ${meta.thread_id}` : 'thread: (none)';
      const cwdText = meta.settings && meta.settings.cwd ? `cwd: ${meta.settings.cwd}` : 'cwd: (default)';
      const statusText = meta.status ? `status: ${meta.status}` : 'status: none';
      const threadRow = document.createElement('div');
      threadRow.textContent = threadText;
      const cwdRow = document.createElement('div');
      cwdRow.textContent = cwdText;
      const statusRow = document.createElement('div');
      statusRow.textContent = statusText;
      const preview = (typeof getConversationPreview === 'function' ? getConversationPreview(meta?.conversation_id) : null) || meta?.last_preview || null;
      const previewText = typeof preview?.text === 'string' ? preview.text.trim() : '';
      const previewRow = document.createElement('div');
      previewRow.className = 'conversation-preview-line';
      previewRow.textContent = previewText;
      if (previewText) {
        info.append(labelRow, title, threadRow, cwdRow, statusRow, previewRow);
      } else {
        info.append(labelRow, title, threadRow, cwdRow, statusRow);
      }

      const actions = document.createElement('div');
      actions.className = 'conversation-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'btn tiny primary';
      openBtn.textContent = 'Open';
      openBtn.addEventListener('click', () => selectConversation(meta.conversation_id));
      const settingsBtn = document.createElement('button');
      settingsBtn.className = 'btn tiny';
      settingsBtn.textContent = 'Settings';
      settingsBtn.addEventListener('click', async () => {
        await selectConversationWithView(meta.conversation_id, 'splash');
        openSettingsModal();
      });
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'btn tiny decline';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', () => {
        const win = windowRef || window;
        if (win.CodexAgent?.helpers?.openWarningModal) {
          win.CodexAgent.helpers.openWarningModal({
            title: 'Delete conversation?',
            body: 'This permanently removes the conversation and its transcript.',
            confirmText: 'Delete',
            onConfirm: async () => {
              await deleteConversation(meta.conversation_id);
            },
          });
        } else {
          deleteConversation(meta.conversation_id);
        }
      });
      actions.append(openBtn, settingsBtn, deleteBtn);

      row.append(info, actions);
      conversationListEl.appendChild(row);
    });
  }

  return {
    isConversationInProject,
    renderSplashTabs,
    renderConversationList,
  };
}
