type AnyRecord = Record<string, any>;

interface ConversationRuntimeState {
  conversationMeta?: AnyRecord;
  conversationSettings?: AnyRecord;
  clientConversationId?: string | null;
  clientActiveView?: string | null;
  activeView?: string | null;
  activeRuntimeOptionValues?: AnyRecord;
  miniConversationDrawerOpen?: boolean;
  runtimeOptions?: AnyRecord;
  hostUi?: AnyRecord;
  planCollapsed?: boolean;
}

interface ConversationRuntimeContext {
  getState(): ConversationRuntimeState;
  setState(patch: Partial<ConversationRuntimeState>): void;
  getSocket(): AnyRecord;
  waitForWs(timeoutMs?: number): Promise<boolean>;
  statusEl: HTMLElement | null;
  setPill(el: HTMLElement | null, text: string, cls?: string): void;
  loadRuntimeOptions(extensionId: string | null, conversationId?: string | null): Promise<any>;
  requestCardRuntime: AnyRecord;
  closePlanModal(): void;
  createEmptyPlanState(hasPlan?: boolean, hasTodo?: boolean): AnyRecord;
  applyAuthoritativePlanState(nextState: AnyRecord): unknown;
  syncPlanOverlayUi(): void;
  setDrawerOpen(open: boolean): void;
  applyHostUi(): void;
  updateActiveConversationLabel(): void;
  renderFooterRuntimeControls(): void;
  setMarkdownEnabled(enabled: boolean): void;
  setTrackEditsEnabled(enabled: boolean): void;
  setLineNumbersEnabled(enabled: boolean): void;
  setViewWrapEnabled(enabled: boolean): void;
  setDiffSyntaxEnabled(enabled: boolean): void;
  setSemanticShellRibbonEnabled(enabled: boolean): void;
  ensureTreeSitterRibbonReady(): unknown;
  restoreDraft(): void;
  updateConversationHeaderLabel(): void;
  setSemanticShellQuoteParsingEnabled(enabled: boolean): void;
  setActiveToolRenderPolicy(policy: unknown): void;
  conversationsRpcClient: AnyRecord;
}

export function bindConversationRuntime(ctx: ConversationRuntimeContext) {
  const {
    getState,
    setState,
    getSocket,
    waitForWs,
    statusEl,
    setPill,
    loadRuntimeOptions,
    requestCardRuntime,
    closePlanModal,
    createEmptyPlanState,
    applyAuthoritativePlanState,
    syncPlanOverlayUi,
    setDrawerOpen,
    applyHostUi,
    updateActiveConversationLabel,
    renderFooterRuntimeControls,
    setMarkdownEnabled,
    setTrackEditsEnabled,
    setLineNumbersEnabled,
    setViewWrapEnabled,
    setDiffSyntaxEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    restoreDraft,
    updateConversationHeaderLabel,
    setSemanticShellQuoteParsingEnabled,
    setActiveToolRenderPolicy,
    conversationsRpcClient,
  } = ctx;

  function currentExtensionId() {
    const { conversationSettings = {}, conversationMeta = {}, runtimeOptions = {} } = getState();
    const candidate = conversationSettings?.agent || conversationMeta?.settings?.agent || runtimeOptions?.agent || '';
    const resolved = typeof candidate === 'string' ? candidate.trim() : '';
    return resolved === 'codex' ? '' : resolved;
  }

  async function sioCall(event: string, data: AnyRecord = {}, options: AnyRecord = {}): Promise<AnyRecord> {
    if (
      options
      && (
        Object.prototype.hasOwnProperty.call(options, 'fallbackUrl')
        || Object.prototype.hasOwnProperty.call(options, 'fallbackMethod')
      )
    ) {
      throw new Error(`HTTP fallbacks are disabled for Socket.IO contract: ${event}`);
    }
    const hasExplicitTimeout = Boolean(options) && Object.prototype.hasOwnProperty.call(options, 'timeoutMs');
    const timeoutMs = hasExplicitTimeout
      ? (options.timeoutMs === null ? null : (Number.isFinite(options.timeoutMs) ? options.timeoutMs : 10000))
      : 10000;
    const socket = getSocket();
    if (socket && socket.connected) {
      return new Promise<AnyRecord>((resolve, reject) => {
        let timer: ReturnType<typeof setTimeout> | null = null;
        if (Number.isFinite(timeoutMs)) {
          timer = setTimeout(() => {
            reject(new Error(`sioCall timeout: ${event}`));
          }, timeoutMs);
        }
        socket.emit(event, data, (ack: AnyRecord) => {
          if (timer) clearTimeout(timer);
          if (ack && ack.__error) {
            resolve({ ok: false, error: ack.__error });
          } else {
            resolve(ack);
          }
        });
      });
    }
    const ready = await waitForWs(3000);
    const nextSocket = getSocket();
    if (ready && nextSocket && nextSocket.connected) {
      return sioCall(event, data, options);
    }
    return { ok: false, error: 'Socket.IO not connected' };
  }

  async function loadExtensionUiFeatures(extensionId: string) {
    const resolvedExtensionId = typeof extensionId === 'string' && extensionId.trim()
      ? extensionId.trim()
      : currentExtensionId();
    if (!resolvedExtensionId) {
      setSemanticShellQuoteParsingEnabled(false);
      setActiveToolRenderPolicy(null);
      return {};
    }
    try {
      const data: AnyRecord = await sioCall('get_extension_ui_features', {
        extension_id: resolvedExtensionId,
      });
      const uiFeatures = data?.ui_features && typeof data.ui_features === 'object' ? data.ui_features : {};
      const semanticShellRibbon = uiFeatures.semanticShellRibbon;
      setSemanticShellQuoteParsingEnabled(semanticShellRibbon?.quoteParsing === true);
      setActiveToolRenderPolicy(uiFeatures.toolRenderPolicy);
      return uiFeatures;
    } catch {
      setSemanticShellQuoteParsingEnabled(false);
      setActiveToolRenderPolicy(null);
      return {};
    }
  }

  async function fetchConversation(conversationId: string | null = null) {
    try {
      const state = getState();
      const cid = conversationId || state.clientConversationId;
      const data: AnyRecord = await sioCall('conversation_get', {
        conversation_id: cid || null,
      });
      if (!data || data.ok === false) return;

      const nextConversationMeta = data;
      const nextConversationSettings = nextConversationMeta?.settings || {};
      const nextPlanCollapsed = nextConversationSettings?.planOverlayCollapsed === true;
      const nextClientConversationId = state.clientConversationId || nextConversationMeta?.conversation_id || null;
      const nextClientActiveView = state.clientActiveView || nextConversationMeta?.active_view || null;
      const nextActiveView = nextClientActiveView || nextConversationMeta?.active_view || 'splash';

      setState({
        conversationMeta: nextConversationMeta,
        conversationSettings: nextConversationSettings,
        clientConversationId: nextClientConversationId,
        clientActiveView: nextClientActiveView,
        activeView: nextActiveView,
        activeRuntimeOptionValues: {},
        planCollapsed: nextPlanCollapsed,
        miniConversationDrawerOpen: nextActiveView !== 'conversation' ? false : state.miniConversationDrawerOpen,
      });
      syncPlanOverlayUi();

      await loadRuntimeOptions(
        currentExtensionId() || null,
        nextConversationMeta?.conversation_id,
      );
      await loadExtensionUiFeatures(currentExtensionId());
      await requestCardRuntime.preload(currentExtensionId());
      closePlanModal();
      applyAuthoritativePlanState(
        createEmptyPlanState(
          Boolean(getState().runtimeOptions?.has_plan),
          Boolean(getState().runtimeOptions?.has_todo),
        ),
      );
      setDrawerOpen(nextActiveView === 'conversation');
      applyHostUi();
      updateActiveConversationLabel();
      renderFooterRuntimeControls();
      setMarkdownEnabled(nextConversationSettings?.markdown !== false);
      setTrackEditsEnabled(nextConversationSettings?.trackEdits === true);
      setLineNumbersEnabled(nextConversationSettings?.lineNumbers === true);
      setViewWrapEnabled(nextConversationSettings?.viewWrap === true);
      setDiffSyntaxEnabled(nextConversationSettings?.diffSyntax === true);
      setSemanticShellRibbonEnabled(nextConversationSettings?.semanticShellRibbon === true);
      if (nextConversationSettings?.semanticShellRibbon === true) {
        ensureTreeSitterRibbonReady();
      }
      restoreDraft();
    } catch {
      // Don't touch statusEl here - it's for server status only
    }
    updateConversationHeaderLabel();
  }

  async function fetchStatus() {
    try {
      const data: AnyRecord = await sioCall('get_status', {});
      if (data.running) {
        setPill(statusEl, 'running', 'ok');
      } else {
        setPill(statusEl, 'idle', 'warn');
      }
    } catch {
      setPill(statusEl, 'error', 'err');
    }
  }

  async function requestContextCompact() {
    try {
      const convoId = getState().conversationMeta?.conversation_id || null;
      const result = await conversationsRpcClient.compactConversation({ conversationId: convoId });
      if (result && result.ok === false) {
        throw new Error(String(result.error || 'compact failed'));
      }
    } catch (err) {
      console.warn('compact failed', err);
    }
  }

  return {
    currentExtensionId,
    sioCall,
    loadExtensionUiFeatures,
    fetchConversation,
    fetchStatus,
    requestContextCompact,
  };
}
