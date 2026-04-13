type Awaitable = Promise<unknown> | unknown;

interface ConversationSettings extends Record<string, unknown> {
  agent?: string;
}

interface ConversationMeta extends Record<string, unknown> {
  conversation_id?: string;
  settings?: ConversationSettings;
}

interface DrawerState {
  clientConversationId?: string | null;
  conversationMeta?: ConversationMeta | null;
  miniConversationDrawerOpen?: boolean;
  activeView?: string | null;
  draftSaveTimer?: ReturnType<typeof setTimeout> | null;
  lastDraftHash?: string | null;
  clientActiveView?: string | null;
  conversationList?: ConversationMeta[];
  extensionCatalog?: unknown[];
  pendingNewConversation?: boolean;
  conversationSettings?: ConversationSettings;
  splashTab?: string;
  rpcTransportEnabled?: boolean;
}

interface ConversationListResponse {
  items: ConversationMeta[];
  activeConversationId: string | null;
  activeView: string | null;
}

interface ConversationCreateResponse extends ConversationMeta {
  conversation_id: string | null;
  settings: ConversationSettings;
}

interface ConversationDrawerActionsContext {
  sioCall(event: string, payload: Record<string, unknown>): Promise<unknown>;
  getState(): DrawerState;
  setState(nextState: Partial<DrawerState>): void;
  resetTimeline(): void;
  fetchConversation(conversationId?: string | null): Awaitable;
  replayTranscript(): Awaitable;
  refreshPlanSurface?(): Awaitable;
  restorePendingApprovals(): void;
  setDrawerOpen(open: boolean): void;
  applyHostUi(): void;
  openSettingsModal(): void;
  renderConversationList(list: ConversationMeta[], activeConversationId: string | null): void;
  renderMiniConversationList(list: ConversationMeta[], activeConversationId: string | null): void;
  renderSplashTabs(): void;
  updateActiveConversationLabel(): void;
  conversationTitleEl: HTMLElement | null;
  conversationCreateBtn: HTMLElement | null;
  conversationBackBtn: HTMLElement | null;
  conversationSettingsBtn: HTMLElement | null;
  conversationBodyEl: HTMLElement | null;
  conversationMiniDrawerEl: HTMLElement | null;
  conversationMiniCloseBtn: HTMLElement | null;
  documentRef?: Document;
  windowRef?: Window;
}

interface ConversationDrawerActionsBinding {
  fetchConversations(): Promise<void>;
  setActiveView(view: string): Promise<void>;
  selectConversation(conversationId: string): Promise<void>;
  selectConversationWithView(conversationId: string, view: string): Promise<void>;
  createConversation(): Promise<void>;
  deleteConversation(conversationId: string): Promise<void>;
  setConversationPins(pinnedConversationIds: string[]): Promise<void>;
  bindHeaderHandlers(): void;
  bindSplashTabHandlers(): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

function normalizeConversationListResponse(data: unknown): ConversationListResponse {
  const payload = isRecord(data) ? data : null;
  const items = Array.isArray(payload?.items)
    ? payload.items.filter((item): item is ConversationMeta => isRecord(item))
    : [];
  return {
    items,
    activeConversationId: typeof payload?.active_conversation_id === 'string' ? payload.active_conversation_id : null,
    activeView: typeof payload?.active_view === 'string' ? payload.active_view : null,
  };
}

function normalizeConversationCreateResponse(data: unknown): ConversationCreateResponse {
  const payload = isRecord(data) ? data : {};
  const settings = isRecord(payload.settings) ? payload.settings as ConversationSettings : {};
  return {
    ...payload,
    conversation_id: typeof payload.conversation_id === 'string' ? payload.conversation_id : null,
    settings,
  };
}

export function createConversationDrawerActions(
  ctx: ConversationDrawerActionsContext,
): ConversationDrawerActionsBinding {
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

  function getActiveConversationId(): string | null {
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

  function setMiniDrawerOpen(open: boolean): void {
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
        const extPayload = isRecord(extData) ? extData : null;
        extensionCatalog = Array.isArray(extPayload?.extensions) ? extPayload.extensions : [];
      } catch {
        // ignore
      }
      const data = normalizeConversationListResponse(await sioCall('conversations_list', {}));
      const state = getState();
      const conversationList = data.items;
      const ssotActiveId = data.activeConversationId;
      const highlightId = state.clientConversationId || state.conversationMeta?.conversation_id || ssotActiveId;
      const patch: Partial<DrawerState> = { conversationList, extensionCatalog };
      if (!state.clientActiveView && data.activeView) patch.clientActiveView = data.activeView;
      setState(patch);
      renderConversationList(conversationList, highlightId);
      renderSplashTabs();
      updateActiveConversationLabel();
      syncMiniDrawerUi();
    } catch {
      // ignore
    }
  }

  async function setActiveView(view: string): Promise<void> {
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

  async function selectConversation(conversationId: string): Promise<void> {
    return selectConversationWithView(conversationId, 'conversation');
  }

  async function selectConversationWithView(conversationId: string, view: string): Promise<void> {
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

  async function createConversation(): Promise<void> {
    const state = getState();
    // Cancel any pending draft save from previous conversation
    if (state.draftSaveTimer) {
      clearTimeout(state.draftSaveTimer);
      setState({ draftSaveTimer: null });
    }
    setState({ lastDraftHash: null });
    const meta = normalizeConversationCreateResponse(await sioCall('conversation_create', {}));
    if (meta.conversation_id) {
      setState({
        clientConversationId: meta.conversation_id,
        clientActiveView: 'conversation',
        conversationMeta: meta,
        conversationSettings: meta.settings,
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

  async function deleteConversation(conversationId: string): Promise<void> {
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

  async function setConversationPins(pinnedConversationIds: string[]): Promise<void> {
    if (!Array.isArray(pinnedConversationIds)) return;
    await sioCall('conversation_pins_update', { pinned_conversations: pinnedConversationIds });
    await fetchConversations();
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
    const splashRpcToggleEl = doc.getElementById('splash-rpc-toggle');
    const splashGoConversationBtn = doc.getElementById('splash-go-conversation');
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
    if (splashRpcToggleEl instanceof HTMLInputElement) {
      splashRpcToggleEl.addEventListener('change', () => {
        setState({ rpcTransportEnabled: splashRpcToggleEl.checked });
        renderSplashTabs();
      });
    }
    splashGoConversationBtn?.addEventListener('click', async () => {
      const activeConversationId = getActiveConversationId();
      if (!activeConversationId) return;
      await selectConversationWithView(activeConversationId, 'conversation');
    });
    renderSplashTabs();
  }

  return {
    fetchConversations,
    setActiveView,
    selectConversation,
    selectConversationWithView,
    createConversation,
    deleteConversation,
    setConversationPins,
    bindHeaderHandlers,
    bindSplashTabHandlers,
  };
}
