export function createConversationDrawerActions(ctx) {
  const {
    sioCall,
    getState,
    setState,
    resetTimeline,
    fetchConversation,
    replayTranscript,
    setDrawerOpen,
    applyHostUi,
    openSettingsModal,
    renderConversationList,
    renderSplashTabs,
    updateActiveConversationLabel,
    documentRef,
  } = ctx;

  async function fetchConversations() {
    try {
      const data = await sioCall('conversations_list', {}, {
        fallbackUrl: '/api/appserver/conversations',
        fallbackMethod: 'GET',
      });
      const state = getState();
      const conversationList = data?.items || [];
      const ssotActiveId = data?.active_conversation_id || null;
      const highlightId = state.clientConversationId || state.conversationMeta?.conversation_id || ssotActiveId;
      const patch = { conversationList };
      if (!state.clientActiveView && data?.active_view) patch.clientActiveView = data.active_view;
      setState(patch);
      renderConversationList(conversationList, highlightId);
      renderSplashTabs();
      updateActiveConversationLabel();
    } catch {
      // ignore
    }
  }

  async function setActiveView(view) {
    try {
      await sioCall('set_view', { view }, { fallbackUrl: '/api/appserver/view' });
    } catch {
      // ignore - SSOT is best-effort for boot defaults
    }
    setState({ clientActiveView: view, activeView: view });
    setDrawerOpen(view === 'conversation');
    applyHostUi();
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
    setState({ lastDraftHash: null });
    resetTimeline();
    setState({ clientConversationId: conversationId, clientActiveView: view });
    try {
      await sioCall('conversation_select', { conversation_id: conversationId, view }, {
        fallbackUrl: '/api/appserver/conversations/select',
      });
    } catch {
      // ignore - SSOT is best-effort for boot defaults
    }
    await fetchConversation(conversationId);
    await fetchConversations();
    await replayTranscript();
    setDrawerOpen(view === 'conversation');
    setState({ activeView: view });
    applyHostUi();
  }

  async function createConversation() {
    const state = getState();
    // Cancel any pending draft save from previous conversation
    if (state.draftSaveTimer) {
      clearTimeout(state.draftSaveTimer);
      setState({ draftSaveTimer: null });
    }
    setState({ lastDraftHash: null });
    const meta = await sioCall('conversation_create', {}, {
      fallbackUrl: '/api/appserver/conversations',
    });
    if (meta?.conversation_id) {
      setState({
        clientConversationId: meta.conversation_id,
        clientActiveView: 'conversation',
        conversationMeta: meta,
        conversationSettings: meta?.settings || {},
      });
      try {
        await sioCall('conversation_select', { conversation_id: meta.conversation_id, view: 'conversation' }, {
          fallbackUrl: '/api/appserver/conversations/select',
        });
      } catch {
        // ignore
      }
    }
    await fetchConversation(getState().clientConversationId);
    await fetchConversations();
    resetTimeline();
    await replayTranscript();
    setDrawerOpen(true);
    setState({ activeView: 'conversation' });
    applyHostUi();
    openSettingsModal();
  }

  async function deleteConversation(conversationId) {
    if (!conversationId) return;
    await sioCall('conversation_delete', { conversation_id: conversationId }, {
      fallbackUrl: `/api/appserver/conversations/${conversationId}`,
      fallbackMethod: 'DELETE',
    });
    const state = getState();
    if (state.clientConversationId && state.clientConversationId === conversationId) {
      setState({
        clientConversationId: null,
        clientActiveView: 'splash',
        activeView: 'splash',
      });
      setDrawerOpen(false);
    }
    await fetchConversations();
    await fetchConversation();
    if (!getState().conversationMeta?.conversation_id) {
      setDrawerOpen(false);
      await setActiveView('splash');
    }
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
    bindSplashTabHandlers,
  };
}

