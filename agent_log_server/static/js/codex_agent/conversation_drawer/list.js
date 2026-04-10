function conversationMatchesProject(meta, hostUi, splashTab) {
  if (splashTab !== 'project') return true;
  const projectRoot = hostUi?.projectRoot;
  if (!projectRoot || typeof projectRoot !== 'string') return false;
  const settings = meta?.settings || {};
  const cwd = typeof settings.cwd === 'string' ? settings.cwd : '';
  if (!cwd) return false;
  return cwd === projectRoot || cwd.startsWith(`${projectRoot}/`);
}

function normalizePreviewText(value) {
  if (typeof value === 'string') {
    return value.trim().replace(/\s+/g, ' ').slice(0, 220);
  }
  if (value && typeof value === 'object' && typeof value.text === 'string') {
    return value.text.trim().replace(/\s+/g, ' ').slice(0, 220);
  }
  return '';
}

function buildConversationDisplay(meta, getConversationPreview) {
  const conversationId = meta?.conversation_id || '';
  const settings = meta?.settings || {};
  const labelRaw = typeof settings.label === 'string' ? settings.label.trim() : '';
  const aliasRaw = typeof settings.alias === 'string' ? settings.alias.trim() : '';
  const alias = aliasRaw || conversationId;
  const agent = typeof settings.agent === 'string' && settings.agent.trim()
    ? settings.agent.trim()
    : '';
  const titleText = agent ? `${alias} · ${agent}` : alias;
  const status = typeof meta?.status === 'string' && meta.status.trim()
    ? meta.status.trim()
    : 'none';
  const pendingCount = meta?.pending_approvals && typeof meta.pending_approvals === 'object'
    ? Object.keys(meta.pending_approvals).length
    : 0;
  const previewText = normalizePreviewText(
    (typeof getConversationPreview === 'function' ? getConversationPreview(conversationId) : null) || meta?.last_preview,
  );
  const cwdText = typeof settings.cwd === 'string' ? settings.cwd : '';
  return {
    conversationId,
    titleText,
    labelText: labelRaw,
    statusText: status,
    pendingCount,
    previewText,
    cwdText,
  };
}

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
    setConversationPins,
    openSettingsModal,
    deleteConversation,
    documentRef,
  } = ctx;

  let draggingConversationId = null;

  function getConversationListState() {
    const state = getState();
    return Array.isArray(state?.conversationList) ? state.conversationList : [];
  }

  function getPinnedConversationIds() {
    return getConversationListState()
      .filter((meta) => meta?.pinned === true && typeof meta?.conversation_id === 'string' && meta.conversation_id)
      .map((meta) => meta.conversation_id);
  }

  function clearPinnedDragMarkers(doc) {
    doc?.querySelectorAll?.('.conversation-row.drag-over, .conversation-row.dragging, .conversation-mini-row.drag-over, .conversation-mini-row.dragging')
      ?.forEach((el) => {
        el.classList.remove('drag-over');
        el.classList.remove('dragging');
      });
  }

  function clearPinnedDragState(doc) {
    draggingConversationId = null;
    clearPinnedDragMarkers(doc);
  }

  async function persistPinnedConversationOrder(nextPinnedIds) {
    if (typeof setConversationPins !== 'function' || !Array.isArray(nextPinnedIds)) return;
    await setConversationPins(nextPinnedIds);
  }

  async function toggleConversationPinned(meta) {
    const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
    if (!conversationId) return;
    const pinnedIds = getPinnedConversationIds();
    let nextPinnedIds;
    if (meta?.pinned === true) {
      nextPinnedIds = pinnedIds.filter((id) => id !== conversationId);
    } else {
      nextPinnedIds = [...pinnedIds, conversationId];
    }
    await persistPinnedConversationOrder(nextPinnedIds);
  }

  function reorderPinnedConversationIds(targetConversationId) {
    const targetId = typeof targetConversationId === 'string' ? targetConversationId : '';
    const draggedId = typeof draggingConversationId === 'string' ? draggingConversationId : '';
    if (!draggedId || !targetId || draggedId === targetId) return null;
    const pinnedIds = getPinnedConversationIds();
    const fromIndex = pinnedIds.indexOf(draggedId);
    const toIndex = pinnedIds.indexOf(targetId);
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return null;
    const nextPinnedIds = [...pinnedIds];
    nextPinnedIds.splice(fromIndex, 1);
    nextPinnedIds.splice(toIndex, 0, draggedId);
    return nextPinnedIds;
  }

  function bindPinnedDropTarget(row, meta, doc) {
    const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
    if (!conversationId) return;
    row.addEventListener('dragover', (evt) => {
      if (!draggingConversationId || meta?.pinned !== true || draggingConversationId === conversationId) return;
      evt.preventDefault();
      evt.dataTransfer.dropEffect = 'move';
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', (evt) => {
      const nextTarget = evt.relatedTarget;
      if (nextTarget instanceof Element && row.contains(nextTarget)) return;
      row.classList.remove('drag-over');
    });
    row.addEventListener('drop', async (evt) => {
      if (!draggingConversationId || meta?.pinned !== true || draggingConversationId === conversationId) return;
      evt.preventDefault();
      const nextPinnedIds = reorderPinnedConversationIds(conversationId);
      clearPinnedDragState(doc);
      if (!nextPinnedIds) return;
      await persistPinnedConversationOrder(nextPinnedIds);
    });
  }

  function buildConversationInfo(doc, meta, { compact = false } = {}) {
    const display = buildConversationDisplay(meta, getConversationPreview);
    const info = doc.createElement('div');
    info.className = 'conversation-meta';

    const labelRow = doc.createElement('div');
    labelRow.className = 'conversation-label-line';
    labelRow.textContent = display.labelText || ' ';
    info.appendChild(labelRow);

    const title = doc.createElement('div');
    title.className = 'conversation-name';
    title.textContent = display.titleText;
    info.appendChild(title);

    const previewText = compact ? (display.previewText || display.cwdText) : display.previewText;
    const previewRow = doc.createElement('div');
    previewRow.className = `conversation-preview-line${compact ? ' compact' : ''}`;
    previewRow.textContent = previewText || '';
    if (previewText) previewRow.title = previewText;

    if (compact) {
      info.appendChild(previewRow);
      return info;
    }

    const statusRow = doc.createElement('div');
    statusRow.className = 'conversation-status';
    statusRow.textContent = `status:${display.statusText}`;
    info.appendChild(statusRow);

    if (!compact) {
      if (display.cwdText) {
        const cwd = doc.createElement('div');
        cwd.className = 'conversation-aux-line';
        cwd.textContent = display.cwdText;
        cwd.title = display.cwdText;
        info.appendChild(cwd);
      }

      if (display.pendingCount) {
        const pending = doc.createElement('div');
        pending.className = 'conversation-aux-line';
        pending.textContent = `${display.pendingCount} pending approval${display.pendingCount === 1 ? '' : 's'}`;
        info.appendChild(pending);
      }
    }

    info.appendChild(previewRow);
    return info;
  }

  function buildConversationCardControls(doc, meta, row, { compact = false } = {}) {
    const controls = doc.createElement('div');
    controls.className = compact ? 'conversation-card-controls conversation-mini-controls' : 'conversation-card-controls';

    const canPersistPins = typeof setConversationPins === 'function';

    const pinBtn = doc.createElement('button');
    pinBtn.type = 'button';
    pinBtn.className = `btn tiny conversation-pin-btn${meta?.pinned === true ? ' active' : ''}`;
    pinBtn.textContent = meta?.pinned === true ? '📌' : 'Pin';
    pinBtn.title = meta?.pinned === true ? 'Unpin conversation' : 'Pin conversation';
    pinBtn.disabled = !canPersistPins;
    pinBtn.addEventListener('click', async (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      await toggleConversationPinned(meta);
    });
    controls.appendChild(pinBtn);

    const dragHandle = doc.createElement('button');
    dragHandle.type = 'button';
    dragHandle.className = `btn tiny conversation-drag-handle${meta?.pinned === true ? '' : ' disabled'}`;
    dragHandle.textContent = '↕';
    dragHandle.title = meta?.pinned === true
      ? 'Drag to reorder pinned conversations'
      : 'Pin this conversation to enable drag reordering';
    dragHandle.disabled = !canPersistPins || meta?.pinned !== true;
    dragHandle.draggable = canPersistPins && meta?.pinned === true;
    dragHandle.addEventListener('click', (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
    });
    dragHandle.addEventListener('dragstart', (evt) => {
      if (!canPersistPins || meta?.pinned !== true || !meta?.conversation_id) {
        evt.preventDefault();
        return;
      }
      draggingConversationId = meta.conversation_id;
      evt.dataTransfer.effectAllowed = 'move';
      evt.dataTransfer.setData('text/plain', meta.conversation_id);
      row.classList.add('dragging');
    });
    dragHandle.addEventListener('dragend', () => {
      clearPinnedDragState(doc);
    });
    controls.appendChild(dragHandle);

    bindPinnedDropTarget(row, meta, doc);
    return controls;
  }

  function renderConversationList(list, activeConversationId) {
    if (!conversationListEl) return;
    const doc = documentRef || document;
    const hostUi = getHostUi();
    const splashTab = getSplashTab();
    conversationListEl.innerHTML = '';
    const items = Array.isArray(list) ? list.filter((meta) => conversationMatchesProject(meta, hostUi, splashTab)) : [];
    if (!items.length) {
      const empty = doc.createElement('div');
      empty.className = 'muted';
      empty.textContent = splashTab === 'project' ? 'No project conversations yet.' : 'No conversations yet.';
      conversationListEl.appendChild(empty);
      return;
    }

    items.forEach((meta) => {
      const row = doc.createElement('div');
      row.className = 'conversation-row';
      if (meta?.conversation_id === activeConversationId) row.classList.add('active');
      if (meta?.pinned === true) row.classList.add('pinned');

      const info = buildConversationInfo(doc, meta);
      row.appendChild(info);

      const actions = doc.createElement('div');
      actions.className = 'conversation-actions';
      actions.appendChild(buildConversationCardControls(doc, meta, row));

      const openBtn = doc.createElement('button');
      openBtn.className = 'btn tiny primary';
      openBtn.textContent = 'Open';
      openBtn.addEventListener('click', () => {
        selectConversation(meta.conversation_id);
      });
      actions.appendChild(openBtn);

      const settingsBtn = doc.createElement('button');
      settingsBtn.className = 'btn tiny';
      settingsBtn.textContent = 'Settings';
      settingsBtn.addEventListener('click', async () => {
        await selectConversationWithView(meta.conversation_id, 'splash');
        openSettingsModal();
      });
      actions.appendChild(settingsBtn);

      const deleteBtn = doc.createElement('button');
      deleteBtn.className = 'btn tiny decline';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', async () => {
        await deleteConversation(meta.conversation_id);
      });
      actions.appendChild(deleteBtn);

      row.appendChild(actions);
      conversationListEl.appendChild(row);
    });
  }

  function renderMiniConversationList(list, activeConversationId) {
    if (!conversationMiniListEl) return;
    const doc = documentRef || document;
    conversationMiniListEl.innerHTML = '';
    const items = Array.isArray(list) ? list : [];
    if (!items.length) {
      const empty = doc.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'No conversations yet.';
      conversationMiniListEl.appendChild(empty);
      return;
    }

    items.forEach((meta) => {
      const row = doc.createElement('div');
      row.className = 'conversation-mini-row';
      if (meta?.conversation_id === activeConversationId) row.classList.add('active');
      if (meta?.pinned === true) row.classList.add('pinned');

      const state = getState();
      const extensionCatalog = Array.isArray(state?.extensionCatalog) ? state.extensionCatalog : [];
      const agent = (meta?.settings || {}).agent || '';
      const extInfo = extensionCatalog.find((ext) => ext?.id === agent) || null;
      const unavailableDetail = extInfo && extInfo.active === false
        ? (extInfo.dependency_message || 'This extension is unavailable.')
        : '';

      const mainButton = doc.createElement('button');
      mainButton.type = 'button';
      mainButton.className = 'conversation-mini-main';
      if (unavailableDetail) {
        mainButton.disabled = true;
        mainButton.title = unavailableDetail;
      } else {
        mainButton.addEventListener('click', () => {
          selectConversation(meta.conversation_id);
        });
      }

      const info = buildConversationInfo(doc, meta, { compact: true });
      mainButton.appendChild(info);
      row.appendChild(mainButton);
      row.appendChild(buildConversationCardControls(doc, meta, row, { compact: true }));
      conversationMiniListEl.appendChild(row);
    });
  }

  function renderSplashTabs() {
    const doc = documentRef || document;
    const splashTabAllBtn = doc.getElementById('splash-tab-all');
    const splashTabProjectBtn = doc.getElementById('splash-tab-project');
    const activeTab = getSplashTab();
    splashTabAllBtn?.classList.toggle('active', activeTab === 'all');
    splashTabProjectBtn?.classList.toggle('active', activeTab === 'project');
  }

  return {
    renderConversationList,
    renderMiniConversationList,
    renderSplashTabs,
  };
}
