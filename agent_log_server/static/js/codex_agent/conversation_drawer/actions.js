export function createConversationDrawerActions(ctx) {
  const {
    sioCall,
    getState,
    setState,
    resetTimeline,
    fetchConversation,
    replayTranscript,
    refreshPlanSurface,
    restorePendingApprovals,
    setDrawerOpen,
    applyHostUi,
    openSettingsModal,
    renderConversationList,
    renderMiniConversationList,
    renderSplashTabs,
    updateActiveConversationLabel,
    conversationTitleEl,
    conversationCreateBtn,
    conversationBackBtn,
    conversationSettingsBtn,
    conversationBodyEl,
    conversationMiniDrawerEl,
    conversationMiniCloseBtn,
    documentRef,
    windowRef,
  } = ctx;

  function getActiveConversationId() {
    const state = getState();
    return state.clientConversationId || state.conversationMeta?.conversation_id || null;
  }

  function syncMiniDrawerUi() {
    const state = getState();
    const isOpen = Boolean(state.miniConversationDrawerOpen) && state.activeView === 'conversation';
    if (conversationBodyEl) {
      conversationBodyEl.classList.toggle('mini-drawer-open', isOpen);
    }
    if (conversationMiniDrawerEl) {
      conversationMiniDrawerEl.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
    }
    if (conversationTitleEl) {
      conversationTitleEl.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      conversationTitleEl.setAttribute('title', isOpen ? 'Hide conversation switcher' : 'Show conversation switcher');
    }
    renderMiniConversationList(getState().conversationList, getActiveConversationId());
  }

  function setMiniDrawerOpen(open) {
    setState({ miniConversationDrawerOpen: Boolean(open) });
    syncMiniDrawerUi();
  }

  function toggleMiniDrawer() {
    const state = getState();
    if (state.activeView !== 'conversation') return;
    setMiniDrawerOpen(!state.miniConversationDrawerOpen);
  }

  async function fetchConversations() {
    try {
      let extensionCatalog = getState().extensionCatalog;
      try {
        const extData = await sioCall('get_extensions', {});
        extensionCatalog = Array.isArray(extData?.extensions) ? extData.extensions : [];
      } catch {
        // ignore
      }
      const data = await sioCall('conversations_list', {});
      const state = getState();
      const conversationList = data?.items || [];
      const ssotActiveId = data?.active_conversation_id || null;
      const highlightId = state.clientConversationId || state.conversationMeta?.conversation_id || ssotActiveId;
      const patch = { conversationList, extensionCatalog };
      if (!state.clientActiveView && data?.active_view) patch.clientActiveView = data.active_view;
      setState(patch);
      renderConversationList(conversationList, highlightId);
      renderSplashTabs();
      updateActiveConversationLabel();
      syncMiniDrawerUi();
    } catch {
      // ignore
    }
  }

  async function setActiveView(view) {
    try {
      await sioCall('set_view', { view });
    } catch {
      // ignore - SSOT is best-effort for boot defaults
    }
    setState({
      clientActiveView: view,
      activeView: view,
      miniConversationDrawerOpen: view === 'conversation' ? getState().miniConversationDrawerOpen : false,
    });
    setDrawerOpen(view === 'conversation');
    applyHostUi();
    syncMiniDrawerUi();
  }

  async function selectConversation(conversationId) {
    return selectConversationWithView(conversationId, 'conversation');
  }

  async function selectConversationWithView(conversationId, view) {
    if (!conversationId) return;
    const state = getState();
    // Cancel any pending draft save to avoid race condition
    if (state.draftSaveTimer) {
      clearTimeout(state.draftSaveTimer);
      setState({ draftSaveTimer: null });
    }
    setState({ lastDraftHash: null, miniConversationDrawerOpen: false });
    resetTimeline();
    setState({ clientConversationId: conversationId, clientActiveView: view });
    try {
      await sioCall('conversation_select', { conversation_id: conversationId, view });
    } catch {
      // ignore - SSOT is best-effort for boot defaults
    }
    await fetchConversation(conversationId);
    await fetchConversations();
    await replayTranscript();
    await refreshPlanSurface?.();
    restorePendingApprovals();
    setDrawerOpen(view === 'conversation');
    setState({
      activeView: view,
      miniConversationDrawerOpen: false,
    });
    applyHostUi();
    syncMiniDrawerUi();
  }

  async function createConversation() {
    const state = getState();
    // Cancel any pending draft save from previous conversation
      if (state.draftSaveTimer) {
        clearTimeout(state.draftSaveTimer);
        setState({ draftSaveTimer: null });
      }
      setState({ lastDraftHash: null });
    const meta = await sioCall('conversation_create', {});
    if (meta?.conversation_id) {
      setState({
        clientConversationId: meta.conversation_id,
        clientActiveView: 'conversation',
        conversationMeta: meta,
        conversationSettings: meta?.settings || {},
      });
      try {
        await sioCall('conversation_select', { conversation_id: meta.conversation_id, view: 'conversation' });
      } catch {
        // ignore
      }
    }
    await fetchConversation(getState().clientConversationId);
    await fetchConversations();
    resetTimeline();
    await replayTranscript();
    await refreshPlanSurface?.();
    restorePendingApprovals();
    setDrawerOpen(true);
    setState({ activeView: 'conversation' });
    applyHostUi();
    openSettingsModal();
  }

  async function deleteConversation(conversationId) {
    if (!conversationId) return;
    await sioCall('conversation_delete', { conversation_id: conversationId });
    const state = getState();
    if (state.clientConversationId && state.clientConversationId === conversationId) {
      setState({
        clientConversationId: null,
        clientActiveView: 'splash',
        activeView: 'splash',
        miniConversationDrawerOpen: false,
      });
      setDrawerOpen(false);
    }
    await fetchConversations();
    await fetchConversation();
    if (!getState().conversationMeta?.conversation_id) {
      setDrawerOpen(false);
      await setActiveView('splash');
    }
    syncMiniDrawerUi();
  }

  function bindHeaderHandlers() {
    conversationCreateBtn?.addEventListener('click', async () => {
      setState({ pendingNewConversation: true });
      setMiniDrawerOpen(false);
      await setActiveView('splash');
      openSettingsModal();
    });

    conversationBackBtn?.addEventListener('click', async () => {
      setMiniDrawerOpen(false);
      await setActiveView('splash');
    });

    conversationSettingsBtn?.addEventListener('click', () => {
      setState({ pendingNewConversation: false });
      openSettingsModal();
    });

    conversationMiniCloseBtn?.addEventListener('click', () => {
      setMiniDrawerOpen(false);
    });

    conversationTitleEl?.addEventListener('click', () => {
      toggleMiniDrawer();
    });

    conversationTitleEl?.addEventListener('keydown', (evt) => {
      if (evt.key !== 'Enter' && evt.key !== ' ') return;
      evt.preventDefault();
      toggleMiniDrawer();
    });

    documentRef?.addEventListener('click', (evt) => {
      const state = getState();
      if (!state.miniConversationDrawerOpen) return;
      const target = evt.target;
      if (!(target instanceof Element)) return;
      if (conversationMiniDrawerEl?.contains(target)) return;
      if (conversationTitleEl?.contains(target)) return;
      setMiniDrawerOpen(false);
    });

    windowRef?.addEventListener?.('codexagent:extensions-updated', async () => {
      await fetchConversations();
      await fetchConversation();
      const state = getState();
      if (!state.conversationMeta?.conversation_id) {
        setDrawerOpen(false);
        await setActiveView('splash');
      }
    });

    syncMiniDrawerUi();
  }

  function bindSplashTabHandlers() {
    const doc = documentRef || document;
    const splashTabAllBtn = doc.getElementById('splash-tab-all');
    const splashTabProjectBtn = doc.getElementById('splash-tab-project');
    splashTabAllBtn?.addEventListener('click', () => {
      setState({ splashTab: 'all' });
      const state = getState();
      renderSplashTabs();
      renderConversationList(state.conversationList, state.conversationMeta?.conversation_id || null);
    });
    splashTabProjectBtn?.addEventListener('click', async () => {
      setState({ splashTab: 'project' });
      const state = getState();
      renderSplashTabs();
      renderConversationList(state.conversationList, state.conversationMeta?.conversation_id || null);
    });
  }

  return {
    fetchConversations,
    setActiveView,
    selectConversation,
    selectConversationWithView,
    createConversation,
    deleteConversation,
    bindHeaderHandlers,
    bindSplashTabHandlers,
  };
}
