import { createSettingsRpcClient } from '../rpc/settings/client.ts';
import type { SocketLike, UnknownRecord } from '../shared_types.ts';

interface ConversationSettingsState {
  agent?: string;
  markdown?: boolean;
  trackEdits?: boolean;
  lineNumbers?: boolean;
  viewWrap?: boolean;
  diffSyntax?: boolean;
  semanticShellRibbon?: boolean;
  planOverlayCollapsed?: boolean;
  alias?: string;
  label?: string;
}

interface ConversationMetaState {
  conversation_id?: string | null;
  active_view?: string | null;
  settings?: ConversationSettingsState;
}

interface RuntimeOptionsState {
  agent?: string;
  has_plan?: boolean;
  has_todo?: boolean;
  [key: string]: unknown;
}

interface HostUiState {
  ideMode?: boolean;
}

interface ConversationsRpcClientLike {
  getConversation(options?: { conversationId?: string | null; timeoutMs?: number }): Promise<unknown>;
  compactConversation(options?: { conversationId?: string | null; timeoutMs?: number }): Promise<unknown>;
}

interface RequestCardRuntimeLike {
  preload(extensionId: string): Promise<unknown>;
}

interface ConversationRuntimeState {
  conversationMeta?: ConversationMetaState;
  conversationSettings?: ConversationSettingsState;
  clientConversationId?: string | null;
  clientActiveView?: string | null;
  activeView?: string | null;
  activeRuntimeOptionValues?: UnknownRecord;
  miniConversationDrawerOpen?: boolean;
  runtimeOptions?: RuntimeOptionsState;
  hostUi?: HostUiState;
  planCollapsed?: boolean;
}

interface ConversationRuntimeContext {
  getState(): ConversationRuntimeState;
  setState(patch: Partial<ConversationRuntimeState>): void;
  getSocket(): SocketLike | null;
  waitForWs(timeoutMs?: number): Promise<boolean>;
  statusEl: HTMLElement | null;
  setPill(el: HTMLElement | null, text: string, cls?: string): void;
  loadRuntimeOptions(extensionId: string | null, conversationId?: string | null): Promise<unknown>;
  requestCardRuntime: RequestCardRuntimeLike;
  closePlanModal(): void;
  createEmptyPlanState(hasPlan?: boolean, hasTodo?: boolean): UnknownRecord;
  applyAuthoritativePlanState(nextState: UnknownRecord): unknown;
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
  conversationsRpcClient: ConversationsRpcClientLike;
}

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
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
  const settingsRpcClient = createSettingsRpcClient({ sioCall });

  function currentExtensionId() {
    const { conversationSettings = {}, conversationMeta = {}, runtimeOptions = {} } = getState();
    const candidate = conversationSettings?.agent || conversationMeta?.settings?.agent || runtimeOptions?.agent || '';
    const resolved = typeof candidate === 'string' ? candidate.trim() : '';
    return resolved === 'codex' ? '' : resolved;
  }

  async function sioCall(
    event: string,
    data: UnknownRecord = {},
    options: UnknownRecord = {},
  ): Promise<UnknownRecord> {
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
      ? (options.timeoutMs === null ? null : (Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000))
      : 10000;
    const socket = getSocket();
    if (socket && socket.connected) {
      return new Promise<UnknownRecord>((resolve, reject) => {
        let timer: ReturnType<typeof setTimeout> | null = null;
        if (typeof timeoutMs === 'number' && Number.isFinite(timeoutMs)) {
          timer = setTimeout(() => {
            reject(new Error(`sioCall timeout: ${event}`));
          }, timeoutMs);
        }
        socket.emit(event, data, (ack: unknown) => {
          if (timer) clearTimeout(timer);
          if (isRecord(ack) && typeof ack.__error === 'string') {
            resolve({ ok: false, error: ack.__error });
            return;
          }
          resolve(isRecord(ack) ? ack : {});
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

  async function loadExtensionUiFeatures(extensionId = ''): Promise<UnknownRecord> {
    const resolvedExtensionId = typeof extensionId === 'string' && extensionId.trim()
      ? extensionId.trim()
      : currentExtensionId();
    if (!resolvedExtensionId) {
      setSemanticShellQuoteParsingEnabled(false);
      setActiveToolRenderPolicy(null);
      return {};
    }
    try {
      const data = await settingsRpcClient.getExtensionUiFeatures({
        extensionId: resolvedExtensionId,
      });
      const uiFeatures = isRecord(data.ui_features) ? data.ui_features : {};
      const semanticShellRibbon = isRecord(uiFeatures.semanticShellRibbon) ? uiFeatures.semanticShellRibbon : null;
      setSemanticShellQuoteParsingEnabled(semanticShellRibbon?.quoteParsing === true);
      setActiveToolRenderPolicy(uiFeatures.toolRenderPolicy ?? null);
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
      const result = await conversationsRpcClient.getConversation({
        conversationId: cid || null,
      });
      if (!isRecord(result) || result.ok === false) return;

      const nextConversationMeta: ConversationMetaState = result;
      const nextConversationSettings = isRecord(nextConversationMeta.settings)
        ? nextConversationMeta.settings as ConversationSettingsState
        : {};
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
      const data = await settingsRpcClient.getStatus();
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
      if (isRecord(result) && result.ok === false) {
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
