export function createConversationDrawerList(ctx) {
  const {
    conversationListEl,
    conversationMiniListEl,
    getState,
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

  function getExtensionStatus(agentId) {
    const state = typeof getState === 'function' ? getState() : {};
    const catalog = Array.isArray(state?.extensionCatalog) ? state.extensionCatalog : [];
    const defaultAgent = catalog.find((item) => item?.active === true && item?.id)?.id || '';
    const agent = typeof agentId === 'string' && agentId.trim() ? agentId.trim() : defaultAgent;
    if (agent === 'codex') {
      return { active: false, dependency_message: 'Legacy builtin Codex is disabled' };
    }
    if (!agent) return null;
    return catalog.find((item) => item?.id === agent) || null;
  }

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

  function getConversationAgentText(meta) {
    const agent = typeof meta?.settings?.agent === 'string' ? meta.settings.agent.trim() : '';
    if (!agent || agent === 'codex') return '';
    return agent;
  }

  function getConversationDisplay(meta) {
    const alias = typeof meta?.settings?.alias === 'string' ? meta.settings.alias.trim() : '';
    const labelText = (meta?.settings && meta.settings.label) ? meta.settings.label : '';
    const titleBase = alias || meta?.conversation_id || 'conversation';
    const agentText = getConversationAgentText(meta);
    const preview = (typeof getConversationPreview === 'function' ? getConversationPreview(meta?.conversation_id) : null) || meta?.last_preview || null;
    const previewText = typeof preview?.text === 'string' ? preview.text.trim() : '';
    return {
      labelText,
      titleText: agentText ? `${titleBase} - ${agentText}` : titleBase,
      threadText: meta?.thread_id ? `thread: ${meta.thread_id}` : 'thread: (none)',
      cwdText: meta?.settings?.cwd ? `cwd: ${meta.settings.cwd}` : 'cwd: (default)',
      statusText: meta?.status ? `status: ${meta.status}` : 'status: none',
      previewText,
    };
  }

  function buildConversationInfo(doc, display, compact = false) {
    const info = doc.createElement('div');
    info.className = 'conversation-meta';

    const labelRow = doc.createElement('div');
    labelRow.className = 'conversation-label-line';
    labelRow.textContent = display.labelText;

    const title = doc.createElement('div');
    title.textContent = display.titleText;

    info.append(labelRow, title);

    if (compact) {
      const detailText = display.previewText || display.cwdText;
      if (detailText) {
        const detailRow = doc.createElement('div');
        detailRow.className = 'conversation-preview-line';
        detailRow.textContent = detailText;
        info.append(detailRow);
      }
      return info;
    }

    const threadRow = doc.createElement('div');
    threadRow.textContent = display.threadText;
    const cwdRow = doc.createElement('div');
    cwdRow.textContent = display.cwdText;
    const statusRow = doc.createElement('div');
    statusRow.textContent = display.statusText;
    info.append(threadRow, cwdRow, statusRow);

    if (display.previewText) {
      const previewRow = doc.createElement('div');
      previewRow.className = 'conversation-preview-line';
      previewRow.textContent = display.previewText;
      info.append(previewRow);
    }

    return info;
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
      const doc = documentRef || document;
      const row = document.createElement('div');
      row.className = 'conversation-row';
      if (meta.conversation_id && meta.conversation_id === activeId) {
        row.classList.add('active');
      }
      const info = buildConversationInfo(doc, getConversationDisplay(meta));
      const agentId = typeof meta?.settings?.agent === 'string' && meta.settings.agent.trim() ? meta.settings.agent.trim() : '';
      const extensionStatus = getExtensionStatus(agentId);
      const isUnavailable = extensionStatus && extensionStatus.active !== true;
      const disabledTitle = typeof extensionStatus?.dependency_message === 'string' && extensionStatus.dependency_message.trim()
        ? extensionStatus.dependency_message.trim()
        : 'Extension disabled';

      const actions = document.createElement('div');
      actions.className = 'conversation-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'btn tiny primary';
      openBtn.textContent = 'Open';
      openBtn.disabled = Boolean(isUnavailable);
      if (isUnavailable) openBtn.title = disabledTitle;
      openBtn.addEventListener('click', () => selectConversation(meta.conversation_id));
      const settingsBtn = document.createElement('button');
      settingsBtn.className = 'btn tiny';
      settingsBtn.textContent = 'Settings';
      settingsBtn.disabled = Boolean(isUnavailable);
      if (isUnavailable) settingsBtn.title = disabledTitle;
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

  function renderMiniConversationList(items, activeId) {
    if (!conversationMiniListEl) return;
    conversationMiniListEl.innerHTML = '';
    const list = items || [];
    if (!list.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'No conversations yet.';
      conversationMiniListEl.appendChild(empty);
      return;
    }
    list.forEach((meta) => {
      if (!meta?.conversation_id) return;
      const agentId = typeof meta?.settings?.agent === 'string' && meta.settings.agent.trim() ? meta.settings.agent.trim() : '';
      const extensionStatus = getExtensionStatus(agentId);
      const isUnavailable = extensionStatus && extensionStatus.active !== true;
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'conversation-mini-row';
      row.disabled = Boolean(isUnavailable);
      if (meta.conversation_id === activeId) {
        row.classList.add('active');
      }
      row.append(buildConversationInfo(document, getConversationDisplay(meta), true));
      row.addEventListener('click', () => selectConversation(meta.conversation_id));
      conversationMiniListEl.appendChild(row);
    });
  }

  return {
    isConversationInProject,
    renderSplashTabs,
    renderConversationList,
    renderMiniConversationList,
  };
}
