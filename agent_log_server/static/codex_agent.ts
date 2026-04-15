import {
  createStreamingParser,
  renderEventMarkdownInto,
  highlightCode,
  renderMarkdownBlock,
  renderMarkdownItBlock,
  renderMarkdownInto,
  renderMarkdownSourceInto,
  setMarkdownLinkHandlers,
  streamEnd,
  streamWrite,
} from './js/codex_agent/markdown.ts';
import { bindShellRender } from './js/codex_agent/shell_render.ts';
import { bindToolRender } from './js/codex_agent/tool_render.ts';
import { bindConversationDrawer } from './js/codex_agent/conversation_drawer.ts';
import { bindTranscriptLoader } from './js/codex_agent/transcript_loader.ts';
import { bindTranscriptMetrics } from './js/codex_agent/transcript_metrics.ts';
import {
  isVisibleTranscriptCardRecord,
  parseTranscriptOrderId,
} from './js/codex_agent/transcript_card_metadata.ts';
import {
  DEFAULT_TRANSCRIPT_LIMIT,
  isTranscriptTailRange,
} from './js/codex_agent/transcript_config.ts';
import { bindSocketEvents } from './js/codex_agent/events/socket.ts';
import { bindEventRouter } from './js/codex_agent/events/router.ts';
import { bindTimelineStickyHeaders } from './js/codex_agent/timeline_sticky_headers.ts';
import { bindSessionFlow } from './js/codex_agent/orchestrator/session_flow.ts';
import { bindRpcFlow } from './js/codex_agent/orchestrator/rpc_flow.ts';
import { bindRequestCardRuntime } from './js/codex_agent/request_cards/runtime.ts';
import { bindShellSemantic } from './js/codex_agent/shell_semantic.ts';
import { formatJsonSetting, parseJsonSetting } from './js/codex_agent/settings/runtime_helpers.ts';
import { bindSettingsSaveFlow } from './js/codex_agent/settings/save_flow.ts';
import { bindSettingsUiFlow } from './js/codex_agent/settings/ui_flow.ts';
import { bindRuntimeFooter } from './js/codex_agent/runtime_footer.ts';
import { bindTranscriptCards } from './js/codex_agent/transcript_cards.ts';
import { bindBootInitFlow } from './js/codex_agent/boot/init_flow.ts';
import { bindInputFlow } from './js/codex_agent/boot/input_flow.ts';
import { bindComposerRuntime } from './js/codex_agent/composer/runtime.ts';
import { bindConversationRuntime } from './js/codex_agent/conversation/runtime.ts';
import { bindHostRuntime } from './js/codex_agent/host/runtime.ts';
import { bindWidescreenLayout } from './js/codex_agent/layout/widescreen.ts';
import { bindPlanRuntime } from './js/codex_agent/plan/runtime.ts';
import { bindRenderUtils } from './js/codex_agent/render/utils.ts';
import { bindSubagentsCollapsible } from './js/codex_agent/subagents/collapsible.ts';
import { bindTimelineRows } from './js/codex_agent/timeline/rows.ts';
import { bindTimelineLiveItems } from './js/codex_agent/timeline/live_items.ts';
import { bindTimelineReplay } from './js/codex_agent/timeline/replay.ts';
import { createConversationsRpcClient } from './js/codex_agent/rpc/conversations/client.ts';
import { createSettingsRpcClient } from './js/codex_agent/rpc/settings/client.ts';
import { createUiRpcClient } from './js/codex_agent/rpc/ui/client.ts';
import {
  readRpcTransportEnabledPreference,
  writeRpcTransportEnabledPreference,
} from './js/codex_agent/rpc/transport.ts';

declare const hljs: any;

type AnyRecord = Record<string, any>;

document.addEventListener('DOMContentLoaded', () => {
  const getById = document.getElementById.bind(document);
  const queryOne = document.querySelector.bind(document);
  const byId = (id: string) => getById(id) as any;
  const query = (selector: string) => queryOne(selector) as any;

  const statusEl = byId('agent-status');
  const wsStatusEl = byId('agent-ws');
  const timelineEl = byId('agent-timeline');
  const timelineWrapEl = timelineEl?.closest('.timeline-wrap');
  const scrollContainer = timelineWrapEl || timelineEl;
  const statusRibbonEl = byId('status-ribbon');
  const statusLabelEl = byId('status-label');
  const statusReasoningEl = byId('status-reasoning');
  const statusDotEl = byId('status-dot');
  const startBtn = byId('agent-start');
  const stopBtn = byId('agent-stop');
  const promptEl = byId('agent-prompt');
  const footerEl = query('.composer');
  const footerRuntimeControlsEl = byId('footer-runtime-controls');
  const sendBtn = byId('agent-send');
  const interruptBtn = byId('turn-interrupt');
  const counterMessagesEl = byId('counter-messages');
  const counterTokensEl = byId('counter-tokens');
  const contextRemainingEl = byId('context-remaining');
  const scrollBtn = byId('scroll-pin');
  const activeConversationEl = byId('active-conversation');
  const conversationTitleEl = byId('conversation-title');
  const splashViewEl = byId('splash-view');
  const widescreenResizerEl = byId('widescreen-resizer');
  const drawerEl = byId('conversation-drawer');
  const conversationBodyEl = byId('conversation-body');
  const conversationListEl = byId('conversation-list');
  const conversationMiniDrawerEl = byId('conversation-mini-drawer');
  const conversationMiniListEl = byId('conversation-mini-list');
  const conversationMiniCloseBtn = byId('conversation-mini-close');
  const conversationCreateBtn = byId('conversation-create');
  const conversationBackBtn = byId('conversation-back');
  const conversationSettingsBtn = byId('conversation-settings');
  const splashSettingsModalEl = byId('splash-settings-modal');
  const splashSettingsUserNameEl = byId('splash-settings-user-name');
  const splashSettingsTe2McpIntegrationEl = byId('splash-settings-te2-mcp-integration');
  const settingsModalEl = byId('settings-modal');
  const settingsCloseBtn = byId('settings-close');
  const settingsCancelBtn = byId('settings-cancel');
  const settingsSaveBtn = byId('settings-save');
  const settingsCwdEl = byId('settings-cwd');
  const settingsApprovalEl = byId('settings-approval');
  const settingsSandboxEl = byId('settings-sandbox');
  const settingsModelEl = byId('settings-model');
  const settingsEffortEl = byId('settings-effort');
  const settingsSummaryEl = byId('settings-summary');
  const settingsDeveloperInstructionsEl = byId('settings-developer-instructions');
  const settingsLabelEl = byId('settings-label');
  const settingsAliasEl = byId('settings-alias');
  const settingsCommandLinesEl = byId('settings-command-lines');
  const settingsViewWrapEl = byId('settings-view-wrap');
  const settingsMarkdownEl = byId('settings-markdown');
  const settingsDiffSyntaxEl = byId('settings-diff-syntax');
  const settingsSemanticShellRibbonEl = byId('settings-semantic-shell-ribbon');
  const settingsTe2McpIntegrationEl = byId('settings-te2-mcp-integration');
  const markdownToggleEl = byId('markdown-toggle');
  const trackEditsToggleEl = byId('track-edits-toggle');
  const lineNumbersToggleEl = byId('line-numbers-toggle');
  const settingsAgentEl = byId('settings-agent');
  const settingsAgentToggle = byId('settings-agent-toggle');
  const settingsAgentOptions = byId('settings-agent-options');
  const settingsAgentRowEl = byId('settings-agent-row');
  const settingsRolloutEl = byId('settings-rollout');
  const settingsRolloutRowEl = byId('settings-rollout-row');
  const settingsApprovalToggle = byId('settings-approval-toggle');
  const settingsSandboxToggle = byId('settings-sandbox-toggle');
  const settingsModelToggle = byId('settings-model-toggle');
  const settingsEffortToggle = byId('settings-effort-toggle');
  const settingsSummaryToggle = byId('settings-summary-toggle');
  const settingsApprovalOptions = byId('settings-approval-options');
  const settingsSandboxOptions = byId('settings-sandbox-options');
  const settingsModelOptions = byId('settings-model-options');
  const settingsEffortOptions = byId('settings-effort-options');
  const settingsSummaryOptions = byId('settings-summary-options');
  const settingsCwdBrowseBtn = byId('settings-cwd-browse');
  const settingsRolloutBrowseBtn = byId('settings-rollout-browse');
  const pickerOverlayEl = byId('cwd-picker');
  const pickerCloseBtn = byId('picker-close');
  const pickerPathEl = byId('picker-path');
  const pickerListEl = byId('picker-list');
  const pickerUpBtn = byId('picker-up');
  const pickerSelectBtn = byId('picker-select');
  const pickerTitleEl = byId('picker-title');
  const pickerFilterEl = byId('picker-filter');
  const rolloutOverlayEl = byId('rollout-picker');
	  const rolloutCloseBtn = byId('rollout-close');
	  const rolloutListEl = byId('rollout-list');
  const mentionPillEl = byId('mention-pill');
  const hostCloseTopEl = byId('host-close-top');
  const hostCloseDrawerEl = byId('host-close-drawer');
  const planModalEl = byId('plan-modal');
  const planCloseBtn = byId('plan-close');
  const planDismissBtn = byId('plan-dismiss');
  const planBodyEl = byId('plan-body');

  localStorage.setItem('last_tab', 'codex-agent');
  const mobileParam = new URLSearchParams(window.location.search).get('mobile');
  const _dbg = new URLSearchParams(window.location.search).get('debug') === '1';
  if (mobileParam === '1' || mobileParam === 'true') {
    localStorage.setItem('codex_mobile_scale', '1');
  } else if (mobileParam === '0' || mobileParam === 'false') {
    localStorage.setItem('codex_mobile_scale', '0');
  }
  const storedMobile = localStorage.getItem('codex_mobile_scale');
  const enableMobileScale = storedMobile === '1';
  document.body.classList.toggle('mobile-scale', enableMobileScale);

  let conversationMeta: AnyRecord = {};
  let conversationSettings: AnyRecord = {};
  let conversationList: AnyRecord[] = [];
  let conversationPreviewCache: AnyRecord = {};
  let appConfig: AnyRecord = {};
  let activeView = 'splash';
		  // Client-local selection (do not treat SSOT active conversation as an authority after boot).
		  let clientConversationId: string | null = null;
		  let clientActiveView: string | null = null;
      let miniConversationDrawerOpen = false;
		  let hostUi: AnyRecord = { showClose: false, parentOrigin: null };
  const SPLASH_TAB_STORAGE_KEY = 'codex_splash_tab';
  function normalizeSplashTab(value: unknown): 'all' | 'project' {
    return value === 'project' ? 'project' : 'all';
  }
  function readSplashTabPreference(): 'all' | 'project' {
    try {
      return normalizeSplashTab(localStorage.getItem(SPLASH_TAB_STORAGE_KEY));
    } catch {
      return 'all';
    }
  }
  function writeSplashTabPreference(value: unknown): void {
    try {
      localStorage.setItem(SPLASH_TAB_STORAGE_KEY, normalizeSplashTab(value));
    } catch {
      // Ignore storage failures; splash tab state still works in-memory.
    }
  }
		  let splashTab = readSplashTabPreference(); // 'all' | 'project'
  let rpcTransportEnabled = readRpcTransportEnabledPreference(window);
  let pendingNewConversation = false;
  let pendingRollout: AnyRecord | null = null;
  let lastEventType: string | null = null;
  let pickerPath: string | null = null;
  let pickerMode = 'cwd';
  let pickerItems: AnyRecord[] = [];
  let filterTimer: ReturnType<typeof setTimeout> | null = null;
  let openDropdownEl: HTMLElement | null = null;
  let initialized = false;
  let wsOpen = false;
  let wsReadyResolve: ((value: boolean) => void) | null = null;
  let wsReadyPromise: Promise<boolean> = new Promise((resolve) => { wsReadyResolve = resolve; });
  let wsReconnectDelay = 1000;
  let _socket: any = null; // Socket.IO instance (set in connectWS)
  let modelList: AnyRecord[] = []; // Cached model list with supportedReasoningEfforts
  let runtimeOptions: AnyRecord = {};
  let activeRuntimeOptionValues: AnyRecord = {};
  let planDocState: AnyRecord = { has_plan: false, plan_exists: false, plan_content: '', plan_path: null, plan_source: null };
  let todoState: AnyRecord = { has_todo: false, plan_steps: [] };
  let planDocDirty = false;
  let planFetchSerial = 0;
  let settingsUi: any = null;
  let markdownEnabled = true; // Toggle for markdown rendering
  let trackEditsEnabled = false; // Toggle for TE2 edit tracking per conversation
  let lineNumbersEnabled = false; // Toggle for transcript gutter line numbers
  let viewWrapEnabled = false; // Toggle for wrapped view/read cards
  let diffSyntaxHighlight = false; // Toggle for syntax highlighting in diffs
  let semanticShellRibbonEnabled = false; // Tree-sitter semantic highlighting for shell command ribbons
  let semanticShellQuoteParsingEnabled = false; // Extension-gated quote segmentation for semantic shell ribbons
  let activeToolRenderPolicy: AnyRecord = {
    default: {
      request: { kind: 'plain' },
      response: { kind: 'plain' },
    },
    rules: [],
  };
  let commandRunning = false; // Whether a PTY command is currently running
  let activeAgentPtyBlockId: string | null = null;
  type PendingRpcEntry = {
    resolve: (value: unknown) => void;
    reject: (reason?: unknown) => void;
    timer: ReturnType<typeof setTimeout>;
  };
  const pending = new Map<string | number, PendingRpcEntry>();

  // Detect mobile for input behavior
  const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
                   ('ontouchstart' in window && window.innerWidth < 768);

  const assistantRows = new Map();
  const reasoningRows = new Map();
  const diffRows = new Map();
  const toolRows = new Map();
  const shellRows = new Map();  // Track streaming shell output rows
  let topSpacerEl: HTMLElement | null = null;
  let bottomSpacerEl: HTMLElement | null = null;
  let placeholderCleared = false;
  let messageCount = 0;
  let tokenCount = 0;
  let contextWindow: number | null = null;
  let transcriptGeneration = 0;
  let autoScroll = true;
  let _scrollProgrammatic = false; // Guard: prevent programmatic scroll from unpinning
  let normalizeTimer: ReturnType<typeof setTimeout> | null = null;
  let isNormalizing = false;
  let tributeInstance: any = null;
  let transcriptTotal = 0;
  let planOverlayEl: HTMLDivElement | null = null;
  let planListEl: HTMLDivElement | null = null;
  let planCollapsed = false;
  let timelineStickyHeaders: AnyRecord | null = null;
  const planItems = new Map();
  let transcriptStart = 0;
  let transcriptEnd = 0;
  let transcriptLimit = DEFAULT_TRANSCRIPT_LIMIT;
  let transcriptLoading = false;
  let transcriptHistoryMode = false;
  let transcriptLiveDirty = false;
  let estimatedRowHeight = 28;
  let draftSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let lastDraftHash: string | null = null;
  let draftDirty = false;
  let applyingDraft = false;
  let currentExtensionIdImpl: () => string = () => '';
  let loadExtensionUiFeaturesImpl: (_extensionId?: string) => Promise<AnyRecord> = async (_extensionId = '') => ({});
  let sioCallImpl: (event: string, data?: AnyRecord, options?: AnyRecord) => Promise<any> = async (
    _event: string,
    _data: AnyRecord = {},
    _options: AnyRecord = {},
  ) => ({ ok: false, error: 'Socket.IO not connected' });
  let fetchConversationImpl: (_conversationId?: string | null) => Promise<void> = async (_conversationId: string | null = null) => {};
  let fetchStatusImpl: () => Promise<void> = async () => {};
  let requestContextCompactImpl: () => Promise<void> = async () => {};

  function currentExtensionId(): string {
    return currentExtensionIdImpl();
  }

  async function loadExtensionUiFeatures(extensionId = ''): Promise<AnyRecord> {
    return loadExtensionUiFeaturesImpl(extensionId);
  }

  async function sioCall(event: string, data: AnyRecord = {}, options: AnyRecord = {}): Promise<any> {
    return sioCallImpl(event, data, options);
  }

  async function fetchConversation(conversationId: string | null = null): Promise<void> {
    return fetchConversationImpl(conversationId);
  }

  async function fetchStatus(): Promise<void> {
    return fetchStatusImpl();
  }

  async function requestContextCompact(): Promise<void> {
    return requestContextCompactImpl();
  }

  function isMarkdownEnabled(): boolean {
    return markdownEnabled;
  }

  function setMarkdownEnabled(enabled: boolean): void {
    markdownEnabled = enabled === true;
    if (markdownToggleEl) markdownToggleEl.checked = markdownEnabled;
    if (settingsMarkdownEl) settingsMarkdownEl.checked = markdownEnabled;
  }

  function setTrackEditsEnabled(enabled: boolean): void {
    trackEditsEnabled = enabled === true;
    if (trackEditsToggleEl) trackEditsToggleEl.checked = trackEditsEnabled;
  }

  function setLineNumbersEnabled(enabled: boolean): void {
    lineNumbersEnabled = enabled === true;
    if (lineNumbersToggleEl) lineNumbersToggleEl.checked = lineNumbersEnabled;
    document.body.classList.toggle('line-numbers-enabled', lineNumbersEnabled);
  }

  function setViewWrapEnabled(enabled: boolean): void {
    viewWrapEnabled = enabled === true;
    if (settingsViewWrapEl) settingsViewWrapEl.checked = viewWrapEnabled;
  }


  function isDiffSyntaxEnabled(): boolean {
    return diffSyntaxHighlight;
  }

  function setDiffSyntaxEnabled(enabled: boolean): void {
    diffSyntaxHighlight = enabled === true;
    if (settingsDiffSyntaxEl) settingsDiffSyntaxEl.checked = diffSyntaxHighlight;
  }

  const widescreenLayoutUi = bindWidescreenLayout({
    drawerEl,
    splashViewEl,
    widescreenResizerEl,
    getActiveView: () => activeView,
    documentRef: document,
    windowRef: window,
  });

  const {
    setDrawerOpen,
    updateWidescreenLayout,
    bindWidescreenResizer,
  } = widescreenLayoutUi;

  let getSubagentContainer = (
    _id: string,
    _name: string,
    _intent: string,
    _metadata?: AnyRecord | null,
  ): AnyRecord => ({ body: null });
  let getLiveEventParent = (_evt: AnyRecord | null | undefined): HTMLElement | null => null;
  let finalizeSubagent = (_id: string, _summary: string, _success: boolean): void => {};
  let makeCollapsible = (_row: HTMLElement | null, _cardId: string, _startExpanded: boolean, _options: AnyRecord = {}): void => {};

  // Note: underscore emphasis is handled by the markdown renderer; do not escape underscores
  // in the raw text stream, otherwise users will see literal backslashes in output.

  const renderUtils = bindRenderUtils({
    getState: () => ({
      conversationSettings,
      conversationMeta,
      viewWrapEnabled,
    }),
    documentRef: document,
  });

  const {
    escapeHtml,
    stripCitations,
    detectLangFromPath,
    resolveHljsLanguage,
    buildViewCardTitle,
    detectLangFromCommand,
    highlightCodeAlways,
    normalizeStructuredViewLines,
    synthesizeStructuredViewLines,
    renderStructuredViewLineTable,
    toRelativePath,
    setPill,
  } = renderUtils;

  const shellSemantic = bindShellSemantic({
    getEnabled: () => semanticShellRibbonEnabled,
      setEnabled: (enabled: boolean) => { semanticShellRibbonEnabled = enabled === true; },
      getQuoteParsingEnabled: () => semanticShellQuoteParsingEnabled,
      setQuoteParsingEnabled: (enabled: boolean) => { semanticShellQuoteParsingEnabled = enabled === true; },
      getCheckboxEl: () => byId('settings-semantic-shell-ribbon'),
      escapeHtml,
  });

  function isSemanticShellRibbonEnabled(): boolean {
    return shellSemantic.isSemanticShellRibbonEnabled();
  }

  function setSemanticShellRibbonEnabled(enabled: boolean): void {
    shellSemantic.setSemanticShellRibbonEnabled(enabled);
  }

  function setSemanticShellQuoteParsingEnabled(enabled: boolean): void {
    semanticShellQuoteParsingEnabled = enabled === true;
    shellSemantic.setSemanticShellQuoteParsingEnabled(enabled);
  }

  function setActiveToolRenderPolicy(policy: AnyRecord | null | undefined): void {
    if (policy && typeof policy === 'object') {
      activeToolRenderPolicy = policy;
      return;
    }
    activeToolRenderPolicy = {
      default: {
        request: { kind: 'plain' },
        response: { kind: 'plain' },
      },
      rules: [],
    };
  }

  async function ensureTreeSitterRibbonReady(): Promise<unknown> {
    return shellSemantic.ensureTreeSitterRibbonReady();
  }

  function renderShellCmdRibbon(el: HTMLElement | null, cmd: string): unknown {
    return shellSemantic.renderShellCmdRibbon(el, cmd);
  }

  function setCommandRunning(running: unknown): void {
    commandRunning = Boolean(running);
  }
  const jsStatusEl = byId('js-status');
  if (jsStatusEl) setPill(jsStatusEl, 'loaded', 'ok');

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker
        .register('/sw.js', { scope: '/' })
        .catch((err) => console.warn('Service worker registration failed', err));
    });
  }

  const composerRuntime = bindComposerRuntime({
    getState: () => ({
      conversationMeta,
      conversationSettings,
      draftSaveTimer,
      lastDraftHash,
      draftDirty,
      applyingDraft,
    }),
    setState: (patch) => {
      if (patch.draftSaveTimer !== undefined) draftSaveTimer = patch.draftSaveTimer;
      if (patch.lastDraftHash !== undefined) lastDraftHash = patch.lastDraftHash;
      if (patch.draftDirty !== undefined) draftDirty = patch.draftDirty === true;
      if (patch.applyingDraft !== undefined) applyingDraft = patch.applyingDraft === true;
    },
    promptEl,
    mentionPillEl,
    documentRef: document,
    windowRef: window,
    sioCall,
    escapeHtml,
  });

  const {
    saveDraftDebounced,
    renderPromptFromText,
    getPromptText,
    getPromptDraftText,
    clearPrompt,
    bindComposerSelectionTracking,
    restoreDraft,
    clearDraft,
    syncDraftFromServer,
    initTribute,
    insertMention,
  } = composerRuntime;

  let getUserDisplayName = () => 'user';
  let getAssistantDisplayName = () => 'assistant';
  let refreshMessageCardHeaders = () => {};
  let resetTimeline = () => {};
  let renderTranscriptEntries = (_items: AnyRecord[], _opts: AnyRecord = {}) => {};
  let restorePendingApprovals = () => {};

  const timelineRows = bindTimelineRows({
    getState: () => ({
      bottomSpacerEl,
      placeholderCleared,
      messageCount,
      tokenCount,
    }),
    setState: (patch) => {
      if (patch.bottomSpacerEl !== undefined) bottomSpacerEl = patch.bottomSpacerEl;
      if (patch.placeholderCleared !== undefined) placeholderCleared = patch.placeholderCleared === true;
      if (patch.messageCount !== undefined) messageCount = Number(patch.messageCount || 0);
      if (patch.tokenCount !== undefined) tokenCount = Number(patch.tokenCount || 0);
    },
    timelineEl,
    counterMessagesEl,
    counterTokensEl,
    contextRemainingEl,
    statusRibbonEl,
    statusLabelEl,
    statusReasoningEl,
    statusDotEl,
    documentRef: document,
    getUserDisplayName: () => getUserDisplayName(),
    getAssistantDisplayName: () => getAssistantDisplayName(),
    isMarkdownEnabled,
    renderMarkdownItBlock,
    stripCitations: (text: string) => stripCitations(text) || '',
    maybeAutoScroll,
  });

  const {
    clearPlaceholder,
    ensureActivityRow,
    insertRow,
    buildRow,
    updateMessageCardHeader,
    refreshMessageCardHeaders: refreshMessageCardHeadersImpl,
    buildMessageCard,
    createRow,
    setActivity,
    setReasoningRibbon,
    clearReasoningRibbon,
    setStatusDot,
    setCounter,
    incrementMessages,
    updateTokens,
    updateContextRemaining,
    addMessage,
  } = timelineRows;

  refreshMessageCardHeaders = refreshMessageCardHeadersImpl;

  const subagentsCollapsible = bindSubagentsCollapsible({
    clearPlaceholder,
    insertRow,
    maybeAutoScroll,
    documentRef: document,
    storage: window.localStorage,
  });

  getSubagentContainer = subagentsCollapsible.getSubagentContainer;
  getLiveEventParent = subagentsCollapsible.getLiveEventParent;
  finalizeSubagent = subagentsCollapsible.finalizeSubagent;
  makeCollapsible = subagentsCollapsible.makeCollapsible;

  const hostRuntime = bindHostRuntime({
    getState: () => ({
      hostUi,
      conversationMeta,
      conversationSettings,
      activeView,
      appConfig,
    }),
    setState: (patch) => {
      if (patch.hostUi !== undefined) hostUi = patch.hostUi;
      if (patch.conversationMeta !== undefined) conversationMeta = patch.conversationMeta;
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.activeView !== undefined) activeView = patch.activeView || activeView;
      if (patch.appConfig !== undefined) appConfig = patch.appConfig;
    },
    sioCall,
    refreshMessageCardHeaders,
    hostCloseTopEl,
    hostCloseDrawerEl,
    activeConversationEl,
    conversationTitleEl,
    splashSettingsModalEl,
    splashSettingsUserNameEl,
    splashSettingsTe2McpIntegrationEl,
    documentRef: document,
    windowRef: window,
    getSocketConnected: () => Boolean(_socket && _socket.connected),
  });

  const {
    applyHostUi,
    sendHostCloseMessage,
    fetchHostUi,
    recheckSidebarConnection,
    postTe2OpenRequest,
    postExternalUrlOpenRequest,
    updateActiveConversationLabel,
    getUserDisplayName: getUserDisplayNameImpl,
    getAssistantDisplayName: getAssistantDisplayNameImpl,
    updateConversationHeaderLabel,
    applyAppConfig,
    fetchAppConfig,
    openSplashSettingsModal,
    closeSplashSettingsModal,
    saveSplashSettings,
  } = hostRuntime;

  getUserDisplayName = getUserDisplayNameImpl;
  getAssistantDisplayName = getAssistantDisplayNameImpl;

  const {
    isConversationInProject,
    renderSplashTabs,
    renderConversationList,
    renderMiniConversationList,
    fetchConversations,
    setActiveView,
    selectConversation,
    selectConversationWithView,
    createConversation,
    deleteConversation,
    bindSplashTabHandlers,
  } = bindConversationDrawer({
    conversationListEl,
    conversationMiniListEl,
    conversationTitleEl,
    conversationCreateBtn,
    conversationBackBtn,
    conversationSettingsBtn,
    conversationBodyEl,
    conversationMiniDrawerEl,
    conversationMiniCloseBtn,
    getHostUi: () => hostUi,
    getSplashTab: () => splashTab,
    getConversationPreview: (conversationId) => {
      if (!conversationId) return null;
      return conversationPreviewCache?.[conversationId] || null;
    },
    sioCall,
    getState: () => ({
      conversationList,
      conversationPreviewCache,
      appConfig,
      clientConversationId,
      conversationMeta,
      clientActiveView,
      activeView,
      conversationSettings,
      draftSaveTimer,
      lastDraftHash,
      splashTab,
      rpcTransportEnabled,
      pendingNewConversation,
      miniConversationDrawerOpen,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.conversationList !== undefined) conversationList = patch.conversationList;
      if (patch.conversationPreviewCache !== undefined) conversationPreviewCache = patch.conversationPreviewCache;
      if (patch.appConfig !== undefined) appConfig = patch.appConfig;
      if (patch.clientConversationId !== undefined) clientConversationId = patch.clientConversationId;
      if (patch.conversationMeta !== undefined) conversationMeta = patch.conversationMeta;
      if (patch.clientActiveView !== undefined) clientActiveView = patch.clientActiveView;
      if (patch.activeView !== undefined) activeView = patch.activeView;
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.draftSaveTimer !== undefined) draftSaveTimer = patch.draftSaveTimer;
      if (patch.lastDraftHash !== undefined) lastDraftHash = patch.lastDraftHash;
      if (patch.splashTab !== undefined) {
        splashTab = normalizeSplashTab(patch.splashTab);
        writeSplashTabPreference(splashTab);
      }
      if (patch.rpcTransportEnabled !== undefined) {
        rpcTransportEnabled = writeRpcTransportEnabledPreference(patch.rpcTransportEnabled, window);
      }
      if (patch.pendingNewConversation !== undefined) pendingNewConversation = patch.pendingNewConversation;
      if (patch.miniConversationDrawerOpen !== undefined) miniConversationDrawerOpen = patch.miniConversationDrawerOpen;
    },
    resetTimeline,
    fetchConversation,
    replayTranscript: (...args) => replayTranscript(...args),
    refreshPlanSurface: (...args) => refreshPlanSurface(...args),
    restorePendingApprovals,
    resetConversationUiState: () => {
      resetRuntimeFooterState();
    },
    setDrawerOpen,
    applyHostUi,
    openSettingsModal,
    updateActiveConversationLabel,
    documentRef: document,
    windowRef: window,
  }) as any;

  function toProjectRelativePath(path: string | null | undefined): string | null {
	    if (!path || typeof path !== 'string') return null;
	    if (!path.startsWith('/')) return path;
	    const root = hostUi?.projectRoot;
	    if (!root || typeof root !== 'string') return null;
	    const rootNorm = root.endsWith('/') ? root : `${root}/`;
	    if (path === root) return '.';
	    if (!path.startsWith(rootNorm)) return null;
	    return path.slice(rootNorm.length);
	  }

  async function ensureProjectRootLoaded(): Promise<string | null> {
	    if (hostUi?.projectRoot) return hostUi.projectRoot;
	    if (!hostUi?.ideMode) return null;
	    return null;
	  }

  setMarkdownLinkHandlers({
    openFilePath: (target) => {
      const next: AnyRecord = target && typeof target === 'object' ? target : { path: target };
      postTe2OpenRequest({
        path: next.path,
        line: Number.isFinite(next.line) ? next.line : 1,
        column: Number.isFinite(next.column) ? next.column : 1,
      });
    },
    openExternalUrl: (url) => {
      postExternalUrlOpenRequest(url);
    },
  });

  async function openSettingsModal(...args: unknown[]) {
    return settingsUi?.openSettingsModal(...args);
  }

  function closeSettingsModal(...args: unknown[]) {
    return settingsUi?.closeSettingsModal(...args);
  }

  const runtimeFooter = bindRuntimeFooter({
    getState: () => ({
      conversationMeta,
      conversationSettings,
      runtimeOptions,
      activeRuntimeOptionValues,
      openDropdownEl,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
      if (patch.activeRuntimeOptionValues !== undefined) activeRuntimeOptionValues = patch.activeRuntimeOptionValues || {};
      if (patch.openDropdownEl !== undefined) openDropdownEl = patch.openDropdownEl || null;
    },
    footerRuntimeControlsEl,
    closeDropdownMenu: (...args: unknown[]) => closeDropdownMenu(...args),
    toggleDropdownMenu: (...args: unknown[]) => toggleDropdownMenu(...args),
    sioCall,
  });

  const {
    normalizeApprovalValue,
    renderFooterRuntimeControls,
    saveApprovalQuick,
    applyRuntimeMode,
    resetRuntimeFooterState,
  } = runtimeFooter;

  function openPicker(...args: unknown[]) {
    return settingsUi?.openPicker(...args);
  }

  function closePicker(...args: unknown[]) {
    return settingsUi?.closePicker(...args);
  }

  function bindPickerFilter(...args: unknown[]) {
    return settingsUi?.bindPickerFilter(...args);
  }

  function openRolloutPicker(...args: unknown[]) {
    return settingsUi?.openRolloutPicker(...args);
  }

  function closeRolloutPicker(...args: unknown[]) {
    return settingsUi?.closeRolloutPicker(...args);
  }

  async function loadRolloutPreview(...args: unknown[]) {
    return settingsUi?.loadRolloutPreview(...args);
  }

  async function fetchRollouts(...args: unknown[]) {
    return settingsUi?.fetchRollouts(...args);
  }

  function buildDropdown(...args: unknown[]) {
    return settingsUi?.buildDropdown(...args);
  }

  function updateDropdownOptions(...args: unknown[]) {
    return settingsUi?.updateDropdownOptions(...args);
  }

  async function loadModelOptions(...args: unknown[]) {
    return settingsUi?.loadModelOptions(...args);
  }

  async function loadRuntimeOptions(...args: unknown[]) {
    return settingsUi?.loadRuntimeOptions(...args);
  }

  async function loadAgentOptions(...args: unknown[]) {
    return settingsUi?.loadAgentOptions(...args);
  }

  async function onAgentSelectionChange(...args: unknown[]) {
    return settingsUi?.onAgentSelectionChange(...args);
  }

  function updateEffortOptionsForModel(...args: unknown[]) {
    return settingsUi?.updateEffortOptionsForModel(...args);
  }

  function openDropdownMenu(...args: unknown[]) {
    return settingsUi?.openDropdownMenu(...args);
  }

  function closeDropdownMenu(...args: unknown[]) {
    return settingsUi?.closeDropdownMenu(...args);
  }

  function toggleDropdownMenu(...args: unknown[]) {
    return settingsUi?.toggleDropdownMenu(...args);
  }

  function setupDropdown(...args: unknown[]) {
    return settingsUi?.setupDropdown(...args);
  }

  async function fetchPicker(...args: unknown[]) {
    return settingsUi?.fetchPicker(...args);
  }

  async function fetchPickerSearch(...args: unknown[]) {
    return settingsUi?.fetchPickerSearch(...args);
  }

  function applyPickerFilter(...args: unknown[]) {
    return settingsUi?.applyPickerFilter(...args);
  }

  settingsUi = bindSettingsUiFlow({
    getState: () => ({
      conversationMeta,
      conversationSettings,
      pendingNewConversation,
      pendingRollout,
      hostUi,
      splashTab,
      pickerPath,
      pickerMode,
      pickerItems,
      filterTimer,
      openDropdownEl,
      modelList,
      runtimeOptions,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.pendingNewConversation !== undefined) pendingNewConversation = patch.pendingNewConversation;
      if (patch.pendingRollout !== undefined) pendingRollout = patch.pendingRollout;
      if (patch.pickerPath !== undefined) pickerPath = patch.pickerPath;
      if (patch.pickerMode !== undefined) pickerMode = patch.pickerMode;
      if (patch.pickerItems !== undefined) pickerItems = patch.pickerItems;
      if (patch.filterTimer !== undefined) filterTimer = patch.filterTimer;
      if (patch.openDropdownEl !== undefined) openDropdownEl = patch.openDropdownEl;
      if (patch.modelList !== undefined) modelList = patch.modelList;
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
    },
    elements: {
      settingsModalEl,
      settingsCwdEl,
      settingsApprovalEl,
      settingsSandboxEl,
      settingsModelEl,
      settingsEffortEl,
      settingsSummaryEl,
      settingsDeveloperInstructionsEl,
      settingsLabelEl,
      settingsAliasEl,
      settingsCommandLinesEl,
      settingsViewWrapEl,
      settingsMarkdownEl,
      settingsDiffSyntaxEl,
      settingsSemanticShellRibbonEl,
      settingsTe2McpIntegrationEl,
      settingsAgentEl,
      settingsAgentOptions,
      settingsAgentRowEl,
      settingsRolloutEl,
      settingsRolloutRowEl,
      settingsApprovalOptions,
      settingsSandboxOptions,
      settingsModelOptions,
      settingsEffortOptions,
      settingsSummaryOptions,
      pickerOverlayEl,
      pickerPathEl,
      pickerListEl,
      pickerTitleEl,
      pickerFilterEl,
      rolloutOverlayEl,
      rolloutListEl,
    },
    sioCall,
    setActivity,
    getRelativePath: (absolutePath: string | null | undefined, cwd: string | null | undefined) => {
      if (!absolutePath || !cwd) return absolutePath;
      const cwdNorm = cwd.endsWith('/') ? cwd : `${cwd}/`;
      if (absolutePath.startsWith(cwdNorm)) {
        return absolutePath.slice(cwdNorm.length);
      }
      return absolutePath;
    },
    insertMention,
    getWindow: () => window,
  });

  function isNearBottom() {
    if (!scrollContainer) return true;
    const distance = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
    return distance <= 24;
  }

  function maybeAutoScroll(force = false) {
    if (!scrollContainer) return;
    if (autoScroll || force) {
      _scrollProgrammatic = true;
      scrollContainer.scrollTop = scrollContainer.scrollHeight;
      requestAnimationFrame(() => { _scrollProgrammatic = false; });
    }
  }

  function updateScrollButton() {
    if (!scrollBtn) return;
    scrollBtn.textContent = autoScroll ? 'Pinned' : 'Free';
    scrollBtn.classList.toggle('active', autoScroll);
  }

  function getPlanOverlayOffset() {
    if (!planOverlayEl || planOverlayEl.style.display === 'none') return 0;
    const rect = planOverlayEl.getBoundingClientRect();
    if (!rect.height) return 0;
    const styles = window.getComputedStyle(planOverlayEl);
    const marginBottom = parseFloat(styles.marginBottom || '0') || 0;
    return rect.height + marginBottom;
  }

  function scrollRowToTop(row: HTMLElement | null, { clearPinned = false }: { clearPinned?: boolean } = {}) {
    if (!row || !scrollContainer) return;
    const wrapRect = scrollContainer.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    const stickyOffset = timelineStickyHeaders?.getVisibleHeight?.() || 0;
    const delta = rowRect.top - wrapRect.top - getPlanOverlayOffset() - stickyOffset;
    _scrollProgrammatic = true;
    scrollContainer.scrollTop += delta;
    if (clearPinned) {
      autoScroll = false;
      updateScrollButton();
    }
    requestAnimationFrame(() => {
      _scrollProgrammatic = false;
      timelineStickyHeaders?.update?.();
    });
  }

  timelineStickyHeaders = bindTimelineStickyHeaders({
    timelineWrapEl,
    timelineEl,
    getTopOffset: () => getPlanOverlayOffset(),
    onMessageHeaderClick: (row) => {
      scrollRowToTop(row, { clearPinned: true });
    },
    onCollapsibleHeaderClick: (row) => {
      (row as any)?._toggleCollapse?.();
    },
    documentRef: document,
    windowRef: window,
  });

  const requestCardRuntime = bindRequestCardRuntime({
    sioCall,
  });

  const planRuntime = bindPlanRuntime({
    getState: () => ({
      conversationMeta,
      conversationSettings,
      runtimeOptions,
      planOverlayEl,
      planListEl,
      planCollapsed,
      planDocState,
      todoState,
      planDocDirty,
      planFetchSerial,
      topSpacerEl,
    }),
    setState: (patch) => {
      if (patch.conversationMeta !== undefined) conversationMeta = patch.conversationMeta;
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
      if (patch.planOverlayEl !== undefined) planOverlayEl = patch.planOverlayEl;
      if (patch.planListEl !== undefined) planListEl = patch.planListEl;
      if (patch.planCollapsed !== undefined) planCollapsed = patch.planCollapsed === true;
      if (patch.planDocState !== undefined) planDocState = patch.planDocState || {};
      if (patch.todoState !== undefined) todoState = patch.todoState || {};
      if (patch.planDocDirty !== undefined) planDocDirty = patch.planDocDirty === true;
      if (patch.planFetchSerial !== undefined) planFetchSerial = Number(patch.planFetchSerial || 0);
      if (patch.topSpacerEl !== undefined) topSpacerEl = patch.topSpacerEl;
    },
    timelineEl,
    planItems,
    sioCall,
    currentExtensionId,
    planModalEl,
    planCloseBtn,
    planDismissBtn,
    planBodyEl,
    renderMarkdownInto,
    highlightCode,
  });

  const {
    clearPlanOverlay,
    finalizePlanToTranscript,
    syncPlanOverlayUi,
    createEmptyPlanState,
    applyAuthoritativePlanState,
    handleLiveTodoUpdate,
    handleLivePlanState,
    refreshPlanSurface,
    closePlanModal,
  } = planRuntime;

  const {
    appendErrorContent,
    renderCommandResult,
    renderViewCard,
    renderSearchCard,
    renderErrorCard,
    renderWarningCard,
    renderContextCompactedCard,
    renderMetaEnvelopeInjected,
  } = bindTranscriptCards({
    getConversationSettings: () => conversationSettings,
    clearPlaceholder,
    createRow,
    makeCollapsible,
    getLiveEventParent,
    getBottomSpacerEl: () => bottomSpacerEl,
    timelineEl,
    maybeAutoScroll,
    setLastEventType: (value: string) => { lastEventType = value; },
    setStatusDot,
    renderShellCmdRibbon,
    detectLangFromCommand,
    highlightCodeAlways,
    detectLangFromPath,
    toRelativePath,
    postTe2OpenRequest,
    buildViewCardTitle,
    normalizeStructuredViewLines,
    synthesizeStructuredViewLines,
    renderStructuredViewLineTable,
    openSplashSettingsModal,
    addMessage,
    escapeHtml,
  });

  const { updateSpacerHeights, measureRowHeight } = bindTranscriptMetrics({
    timelineEl,
    getSpacerEls: () => ({ topSpacerEl, bottomSpacerEl }),
    getTranscriptState: () => ({
      transcriptStart,
      transcriptTotal,
      transcriptEnd,
      estimatedRowHeight,
    }),
    setTranscriptState: (patch: AnyRecord) => {
      if (patch.estimatedRowHeight !== undefined) estimatedRowHeight = patch.estimatedRowHeight;
    },
  });
  const agentBlockRows = new Map();

  const timelineLiveItems = bindTimelineLiveItems({
    getState: () => ({
      lastEventType,
      activeAgentPtyBlockId,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.lastEventType !== undefined) lastEventType = patch.lastEventType || null;
      if (patch.activeAgentPtyBlockId !== undefined) activeAgentPtyBlockId = patch.activeAgentPtyBlockId || null;
    },
    assistantRows,
    reasoningRows,
    diffRows,
    agentBlockRows,
    timelineEl,
    buildMessageCard,
    updateMessageCardHeader,
    insertRow,
    createRow,
    buildRow,
    makeCollapsible,
    clearPlaceholder,
    setActivity,
    setStatusDot,
    setCommandRunning,
    maybeAutoScroll,
    isMarkdownEnabled,
    createStreamingParser,
    renderEventMarkdownInto,
    streamWrite,
    streamEnd,
    highlightCode,
    incrementMessages,
    stripCitations: (text: string) => stripCitations(text) || '',
    escapeHtml,
    toRelativePath,
    postTe2OpenRequest,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromPath: (path: string) => detectLangFromPath(path) || '',
    resolveHljsLanguage: (lang: string) => resolveHljsLanguage(lang) || '',
    detectLangFromCommand: (command: string) => detectLangFromCommand(command) || '',
    isDiffSyntaxEnabled,
    sioCall,
    getConversationId: () => conversationMeta?.conversation_id || null,
    getConversationMeta: () => conversationMeta,
    setConversationMeta: (nextMeta) => { conversationMeta = nextMeta; },
    getCurrentExtensionId: () => currentExtensionId(),
    getSubagentContainer,
    requestCardRuntime,
    onAfterRender: () => {
      timelineStickyHeaders?.update?.();
      maybeAutoScroll();
    },
  });

  const {
    getAssistantRow,
    appendAssistantDelta,
    finalizeAssistant,
    getReasoningRow,
    appendReasoningDelta,
    finalizeReasoning,
    getDiffRow,
    addDiff,
    addDeclinedDiff,
    formatDiff,
    renderDiffBlock,
    getDiffRenderState,
    setDiffRenderMode,
    getAgentBlockRow,
    renderAgentBlockBegin,
    renderAgentBlockDelta,
    renderScreenDelta,
    renderAgentBlockEnd,
    renderPlanCard,
    renderApproval,
    respondApproval: respondApprovalImpl,
    handoffApproval,
    restorePendingApprovals: restorePendingApprovalsImpl,
  } = timelineLiveItems;

  restorePendingApprovals = restorePendingApprovalsImpl;

  const {
    getShellRow,
    renderShellBegin,
    renderShellDelta,
    renderShellEnd,
    renderShellBatchResult,
  } = bindShellRender({
    shellRows,
    clearPlaceholder,
    insertRow,
    makeCollapsible,
    getSubagentContainer,
    renderShellCmdRibbon,
    postTe2OpenRequest,
    detectLangFromCommand,
    highlightCodeAlways,
    setStatusDot,
    setActivity,
    maybeAutoScroll,
    setLastEventType: (v: string) => { lastEventType = v; },
    _dbg,
  });

  const {
    buildReplayToolRow,
    renderToolBegin,
    renderToolDelta,
    renderToolEnd,
    renderToolInteraction,
  } = bindToolRender({
    toolRows,
    clearPlaceholder,
    insertRow,
    makeCollapsible,
    getLiveEventParent,
    renderEventMarkdownInto,
    formatDiff,
    renderDiffBlock,
    toRelativePath,
    escapeHtml,
    renderShellCmdRibbon,
    maybeAutoScroll,
    setLastEventType: (value: string) => { lastEventType = value; },
    setStatusDot,
    getToolRenderPolicy: () => activeToolRenderPolicy,
    highlightCodeAlways,
  });

  const timelineReplay = bindTimelineReplay({
    getState: () => ({
      conversationSettings,
      runtimeOptions,
      planOverlayEl,
      topSpacerEl,
      bottomSpacerEl,
      transcriptTotal,
      transcriptStart,
      transcriptEnd,
      transcriptLoading,
      transcriptGeneration,
      transcriptHistoryMode,
      transcriptLiveDirty,
      debugEnabled: _dbg,
    }),
    setState: (patch) => {
      if (patch.topSpacerEl !== undefined) topSpacerEl = patch.topSpacerEl as HTMLElement | null;
      if (patch.bottomSpacerEl !== undefined) bottomSpacerEl = patch.bottomSpacerEl as HTMLElement | null;
      if (patch.messageCount !== undefined) messageCount = Number(patch.messageCount || 0);
      if (patch.tokenCount !== undefined) tokenCount = Number(patch.tokenCount || 0);
      if (patch.transcriptTotal !== undefined) transcriptTotal = Number(patch.transcriptTotal || 0);
      if (patch.transcriptStart !== undefined) transcriptStart = Number(patch.transcriptStart || 0);
      if (patch.transcriptEnd !== undefined) transcriptEnd = Number(patch.transcriptEnd || 0);
      if (patch.transcriptLoading !== undefined) transcriptLoading = patch.transcriptLoading === true;
      if (patch.transcriptGeneration !== undefined) transcriptGeneration = Number(patch.transcriptGeneration || 0);
      if (patch.transcriptHistoryMode !== undefined) transcriptHistoryMode = patch.transcriptHistoryMode === true;
      if (patch.transcriptLiveDirty !== undefined) transcriptLiveDirty = patch.transcriptLiveDirty === true;
      if (patch.lastEventType !== undefined) lastEventType = patch.lastEventType as string | null;
      if (patch.contextWindow !== undefined) {
        contextWindow = typeof patch.contextWindow === 'number' && Number.isFinite(patch.contextWindow)
          ? patch.contextWindow
          : null;
      }
    },
    timelineEl,
    counterMessagesEl,
    counterTokensEl,
    contextRemainingEl,
    assistantRows,
    reasoningRows,
    diffRows,
    toolRows,
    shellRows,
    agentBlockRows,
    documentRef: document,
    clearPlaceholder,
    setPlaceholderCleared: (value) => { placeholderCleared = value === true; },
    ensureActivityRow,
    setCounter,
    setActivity,
    clearReasoningRibbon,
    setStatusDot,
    maybeAutoScroll,
    resetPlanState: () => {
      planOverlayEl = null;
      planListEl = null;
      planItems.clear();
      const emptyPlanState = createEmptyPlanState(
        Boolean(runtimeOptions?.has_plan),
        Boolean(runtimeOptions?.has_todo),
      );
      planDocState = {
        has_plan: emptyPlanState.has_plan,
        plan_exists: emptyPlanState.plan_exists,
        plan_content: emptyPlanState.plan_content,
        plan_path: emptyPlanState.plan_path,
        plan_source: emptyPlanState.plan_source,
      };
      todoState = {
        has_todo: emptyPlanState.has_todo,
        plan_steps: emptyPlanState.plan_steps,
      };
      closePlanModal();
    },
    syncPlanOverlayUi,
    timelineStickyUpdate: () => timelineStickyHeaders?.update?.(),
    currentExtensionId,
    buildRow,
    appendErrorContent,
    renderCommandResult: (entry: AnyRecord, parentEl: HTMLElement, options: AnyRecord = {}) => renderCommandResult(entry, parentEl, options),
    renderViewCard: (entry: AnyRecord, parentEl: HTMLElement) => renderViewCard(entry, parentEl),
    renderSearchCard: (entry: AnyRecord, parentEl: HTMLElement) => renderSearchCard(entry, parentEl),
    renderApproval,
    buildReplayToolRow,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromCommand: (command: string) => detectLangFromCommand(command) || '',
    escapeHtml,
    toRelativePath,
    postTe2OpenRequest,
    makeCollapsible,
    addMessage,
    finalizeReasoning,
    addDiff,
    renderPlanCard,
    updateTokens,
    updateContextRemaining,
    applyRuntimeMode,
    measureRowHeight,
    updateSpacerHeights,
  });

  resetTimeline = timelineReplay.resetTimeline;
  renderTranscriptEntries = timelineReplay.renderTranscriptEntries;

  const rpcFlow = bindRpcFlow({
    waitForWs: (...args: Parameters<typeof waitForWs>) => waitForWs(...args),
    sioCall,
    getPending: () => pending,
    getConversationId: () => conversationMeta?.conversation_id || null,
  });

  const { saveSettings } = bindSettingsSaveFlow({
    getState: () => ({
      conversationSettings,
      conversationMeta,
      pendingNewConversation,
      pendingRollout,
      trackEditsEnabled,
      lineNumbersEnabled,
      runtimeOptions,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.conversationMeta !== undefined) conversationMeta = patch.conversationMeta;
      if (patch.clientConversationId !== undefined) clientConversationId = patch.clientConversationId;
      if (patch.clientActiveView !== undefined) clientActiveView = patch.clientActiveView;
      if (patch.pendingNewConversation !== undefined) pendingNewConversation = patch.pendingNewConversation;
      if (patch.pendingRollout !== undefined) pendingRollout = patch.pendingRollout;
    },
    elements: {
      settingsAgentEl,
      settingsCwdEl,
      settingsCommandLinesEl,
      settingsViewWrapEl,
      settingsMarkdownEl,
      settingsDiffSyntaxEl,
      settingsSemanticShellRibbonEl,
      settingsTe2McpIntegrationEl,
      settingsApprovalEl,
      settingsSandboxEl,
      settingsModelEl,
      settingsEffortEl,
      settingsSummaryEl,
      settingsDeveloperInstructionsEl,
      settingsLabelEl,
      settingsAliasEl,
    },
    normalizeApprovalValue,
    setActivity,
    setMarkdownEnabled,
    setViewWrapEnabled,
    setDiffSyntaxEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    sioCall,
    closeSettingsModal,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript: (...args: Parameters<typeof replayTranscript>) => replayTranscript(...args),
    refreshPlanSurface: (...args: Parameters<typeof refreshPlanSurface>) => refreshPlanSurface(...args),
    restorePendingApprovals,
    setDrawerOpen,
    updateConversationHeaderLabel,
  });

  async function sendRpc(method: string, params: AnyRecord, options: AnyRecord = {}) {
    return rpcFlow.sendRpc(method, params, options);
  }

  async function respondApproval(requestId: string, decision: AnyRecord) {
    return respondApprovalImpl(requestId, decision);
  }

  const conversationsRpcClient = createConversationsRpcClient({
    sioCall,
    windowRef: window,
  });
  const settingsRpcClient = createSettingsRpcClient({
    sioCall,
    windowRef: window,
  });
  const uiRpcClient = createUiRpcClient({
    sioCall,
    windowRef: window,
  });

  const { resetWsReady, markWsOpen, waitForWs, connectWS } = bindSocketEvents({
    getWsState: () => ({ wsOpen, wsReadyResolve, wsReadyPromise, wsReconnectDelay }),
    setWsState: (patch: AnyRecord) => {
      if (patch.wsOpen !== undefined) wsOpen = patch.wsOpen;
      if (patch.wsReadyResolve !== undefined) wsReadyResolve = patch.wsReadyResolve;
      if (patch.wsReadyPromise !== undefined) wsReadyPromise = patch.wsReadyPromise;
      if (patch.wsReconnectDelay !== undefined) wsReconnectDelay = patch.wsReconnectDelay;
    },
    setSocket: (sock: AnyRecord) => { _socket = sock; },
    wsStatusEl,
    setPill,
    syncDraftFromServer,
    getConversationId: () => conversationMeta?.conversation_id,
    getWindow: () => window,
    conversationsRpcClient,
    isRpcTransportEnabled: () => rpcTransportEnabled,
  });

  const conversationRuntime = bindConversationRuntime({
    getState: () => ({
      conversationMeta,
      conversationSettings,
      clientConversationId,
      clientActiveView,
      activeView,
      activeRuntimeOptionValues,
      miniConversationDrawerOpen,
      runtimeOptions,
      hostUi,
      planCollapsed,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.conversationMeta !== undefined) conversationMeta = patch.conversationMeta;
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.clientConversationId !== undefined) clientConversationId = patch.clientConversationId;
      if (patch.clientActiveView !== undefined) clientActiveView = patch.clientActiveView;
      if (patch.activeView !== undefined) activeView = patch.activeView || activeView;
      if (patch.activeRuntimeOptionValues !== undefined) activeRuntimeOptionValues = patch.activeRuntimeOptionValues || {};
      if (patch.miniConversationDrawerOpen !== undefined) {
        miniConversationDrawerOpen = patch.miniConversationDrawerOpen === true;
      }
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
      if (patch.hostUi !== undefined) hostUi = patch.hostUi;
      if (patch.planCollapsed !== undefined) planCollapsed = patch.planCollapsed === true;
    },
    getSocket: () => _socket,
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
  });

  currentExtensionIdImpl = conversationRuntime.currentExtensionId;
  loadExtensionUiFeaturesImpl = conversationRuntime.loadExtensionUiFeatures;
  sioCallImpl = conversationRuntime.sioCall;
  fetchConversationImpl = conversationRuntime.fetchConversation;
  fetchStatusImpl = conversationRuntime.fetchStatus;
  requestContextCompactImpl = conversationRuntime.requestContextCompact;

  const {
    ensureInitialized,
    sendUserMessage,
    sendShellCommand,
    interruptTurn,
  } = bindSessionFlow({
    getState: () => ({
      initialized,
      conversationSettings,
      conversationMeta,
      autoScroll,
      rpcTransportEnabled,
      transcriptHistoryMode,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.initialized !== undefined) initialized = patch.initialized;
      if (patch.autoScroll !== undefined) autoScroll = patch.autoScroll;
    },
    sioCall,
    waitForWs,
    conversationsRpcClient,
    setActivity,
    updateScrollButton,
    maybeAutoScroll,
    renderShellBatchResult,
    setStatusDot,
    shellRows,
    snapTranscriptToLive: async () => {
      const cid = clientConversationId || conversationMeta?.conversation_id || null;
      const gen = transcriptGeneration || 0;
      await resumeLiveTail(cid, gen);
    },
  });

  const {
    fetchTranscriptRange,
    loadOlderTranscript,
    loadNewerTranscript,
    replayTranscript,
    resumeLiveTail,
  } = bindTranscriptLoader({
    getConversationId: () => clientConversationId || conversationMeta?.conversation_id || null,
    sioCall,
    getTranscriptState: () => ({
      transcriptTotal,
      transcriptStart,
      transcriptEnd,
      transcriptLimit,
      transcriptLoading,
      transcriptGeneration,
      transcriptHistoryMode,
      transcriptLiveDirty,
    }),
    setTranscriptState: (patch: AnyRecord) => {
      if (patch.transcriptTotal !== undefined) transcriptTotal = patch.transcriptTotal;
      if (patch.transcriptStart !== undefined) transcriptStart = patch.transcriptStart;
      if (patch.transcriptEnd !== undefined) transcriptEnd = patch.transcriptEnd;
      if (patch.transcriptLimit !== undefined) transcriptLimit = patch.transcriptLimit;
      if (patch.transcriptLoading !== undefined) transcriptLoading = patch.transcriptLoading;
      if (patch.transcriptGeneration !== undefined) transcriptGeneration = patch.transcriptGeneration;
      if (patch.transcriptHistoryMode !== undefined) transcriptHistoryMode = patch.transcriptHistoryMode === true;
      if (patch.transcriptLiveDirty !== undefined) transcriptLiveDirty = patch.transcriptLiveDirty === true;
    },
    renderTranscriptEntries,
    prepareTranscriptWindow: timelineReplay.prepareTranscriptWindow,
    timelineEl,
    scrollContainer,
    setScrollProgrammatic: (v: unknown) => { _scrollProgrammatic = Boolean(v); },
    isSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    maybeAutoScroll,
    setLastEventType: (v: string) => { lastEventType = v; },
    refreshPlanSurface,
    restorePendingApprovals,
  });

  const { handleEvent } = bindEventRouter({
    getState: () => ({
      clientConversationId,
      conversationMeta,
      hostUi,
      activeView,
      splashTab,
      conversationList,
      conversationPreviewCache,
      appConfig,
      lastDraftHash,
      draftDirty,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.hostUi !== undefined) hostUi = patch.hostUi;
      if (patch.conversationPreviewCache !== undefined) conversationPreviewCache = patch.conversationPreviewCache;
      if (patch.appConfig !== undefined) appConfig = patch.appConfig;
      if (patch.contextWindow !== undefined) contextWindow = patch.contextWindow;
      if (patch.lastDraftHash !== undefined) lastDraftHash = patch.lastDraftHash;
      if (patch.draftDirty !== undefined) draftDirty = patch.draftDirty;
    },
    getPending: () => pending,
    promptEl,
    debugEnabled: _dbg,
    setLastEventType: (v: string) => { lastEventType = v; },
    setActivity,
    finalizePlanToTranscript,
    renderErrorCard,
    setStatusDot,
    renderWarningCard,
    clearReasoningRibbon,
    setReasoningRibbon,
    addMessage,
    getSubagentContainer,
    appendAssistantDelta,
    finalizeAssistant,
    appendReasoningDelta,
    finalizeReasoning,
    addDiff,
    addDeclinedDiff,
    renderApproval,
    handoffApproval,
    renderCommandResult,
    renderViewCard,
    renderSearchCard,
    renderToolBegin,
    renderToolDelta,
    renderToolEnd,
    renderToolInteraction,
    renderAgentBlockBegin,
    renderAgentBlockDelta,
    renderAgentBlockEnd,
    renderScreenDelta,
    renderShellBegin,
    renderShellDelta,
    renderShellEnd,
    finalizeSubagent,
    maybeAutoScroll,
    handleLivePlanState,
    handleLiveTodoUpdate,
    renderPlanCard,
    clearPlanOverlay,
    updateTokens,
    updateContextRemaining,
    renderContextCompactedCard,
    renderMetaEnvelopeInjected,
    applyHostUi,
    renderSplashTabs,
    renderConversationList,
    renderMiniConversationList,
    insertMention,
    renderPromptFromText,
    applyRuntimeMode,
  });

  function transcriptEventType(evt: AnyRecord | null): string {
    if (!evt || typeof evt !== 'object') return '';
    return typeof evt.type === 'string' ? evt.type.trim().toLowerCase() : '';
  }

  function isTranscriptMutationLiveEvent(evt: AnyRecord | null): boolean {
    const evtType = transcriptEventType(evt);
    if (!evtType || !evt || typeof evt !== 'object') {
      return false;
    }
    if (isVisibleTranscriptCardRecord(evt)) {
      return true;
    }
    return evtType === 'status' || evtType === 'token_count' || evtType === 'mode';
  }

  function isCurrentConversationEvent(evt: AnyRecord | null): boolean {
    if (!evt || typeof evt !== 'object') {
      return false;
    }
    const eventConversationId = typeof evt.conversation_id === 'string'
      ? evt.conversation_id
      : (typeof evt.conversationId === 'string' ? evt.conversationId : '');
    const activeConversationId = clientConversationId || conversationMeta?.conversation_id || '';
    return Boolean(eventConversationId && activeConversationId && eventConversationId === activeConversationId);
  }

  function recordLiveTranscriptOrder(evt: AnyRecord): void {
    const orderId = parseTranscriptOrderId(evt.order_id ?? evt.orderId);
    if (orderId === null || orderId < 0) {
      return;
    }
    const nextOrder = orderId + 1;
    transcriptTotal = Math.max(Number(transcriptTotal) || 0, nextOrder);
    if (!transcriptHistoryMode) {
      transcriptEnd = Math.max(Number(transcriptEnd) || 0, nextOrder);
      transcriptLiveDirty = false;
      transcriptHistoryMode = !isTranscriptTailRange(transcriptEnd, transcriptTotal);
    }
  }

  function trimTranscriptHead(): void {
    if (!timelineEl) return;
    const limit = transcriptLimit || DEFAULT_TRANSCRIPT_LIMIT;
    const rows = timelineEl.querySelectorAll('[data-transcript-order-id]');
    if (rows.length <= limit) return;
    const excess = rows.length - limit;
    for (let i = 0; i < excess; i++) {
      rows[i].remove();
    }
    transcriptStart += excess;
    updateSpacerHeights();
  }

  function handleSocketEvent(event: unknown): void {
    const evt = event && typeof event === 'object' ? event as AnyRecord : null;
    let isTranscriptMutation = false;
    if (isCurrentConversationEvent(evt) && isTranscriptMutationLiveEvent(evt)) {
      isTranscriptMutation = true;
      recordLiveTranscriptOrder(evt as AnyRecord);
      if (transcriptHistoryMode) {
        transcriptLiveDirty = true;
        if (isVisibleTranscriptCardRecord(evt)) {
          return;
        }
      }
    }
    handleEvent(event);
    if (isTranscriptMutation && !transcriptHistoryMode) {
      if (autoScroll) {
        trimTranscriptHead();
      } else {
        transcriptHistoryMode = true;
        transcriptLiveDirty = true;
      }
    }
  }

  const {
    initializeBoot,
    setupSettingsBoot,
    installCodexAgentGlobal,
    bindStartStopButtons,
    initExternalModules,
    bindDropdownClose,
  } = bindBootInitFlow({
    getState: () => ({
      messageCount,
      tokenCount,
      activeView,
      pendingNewConversation,
      pendingRollout,
      conversationMeta,
      conversationSettings,
      appConfig,
      splashTab,
      hostUi,
      pickerPath,
      pickerMode,
      openDropdownEl,
      runtimeOptions,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.pendingNewConversation !== undefined) pendingNewConversation = patch.pendingNewConversation;
      if (patch.pendingRollout !== undefined) pendingRollout = patch.pendingRollout;
      if (patch.appConfig !== undefined) appConfig = patch.appConfig;
      if (patch.pickerPath !== undefined) pickerPath = patch.pickerPath;
      if (patch.pickerMode !== undefined) pickerMode = patch.pickerMode;
      if (patch.openDropdownEl !== undefined) openDropdownEl = patch.openDropdownEl;
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
    },
    elements: {
      statusEl,
      counterMessagesEl,
      counterTokensEl,
      settingsApprovalEl,
      settingsApprovalToggle,
      settingsApprovalOptions,
      settingsSandboxEl,
      settingsSandboxToggle,
      settingsSandboxOptions,
      settingsModelEl,
      settingsModelToggle,
      settingsModelOptions,
      settingsEffortEl,
      settingsEffortToggle,
      settingsEffortOptions,
      settingsSummaryEl,
      settingsSummaryToggle,
      settingsSummaryOptions,
      settingsAgentEl,
      settingsAgentToggle,
      settingsAgentOptions,
      startBtn,
      stopBtn,
    },
    setPill,
    setCounter,
    updateScrollButton,
    resetWsReady,
    connectWS,
    waitForWs,
    recheckSidebarConnection,
    fetchHostUi,
    fetchAppConfig,
    bindPickerFilter,
    setDrawerOpen,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript,
    refreshPlanSurface,
    restorePendingApprovals,
    maybeAutoScroll,
    ensureActivityRow,
    fetchStatus,
    setupDropdown,
    loadAgentOptions,
    loadModelOptions,
    loadRuntimeOptions,
    updateEffortOptionsForModel,
    helperFns: {
      openSettingsModal,
      closeSettingsModal,
      saveSettings,
      onAgentChange: async (agentId: string) => { await loadExtensionUiFeatures(agentId); },
      openSplashSettingsModal,
      closeSplashSettingsModal,
      saveSplashSettings,
      openPicker,
      closePicker,
      openRolloutPicker,
      closeRolloutPicker,
      loadRolloutPreview,
      setActiveView,
      setDrawerOpen,
      fetchPicker,
      fetchRollouts,
        setActivity,
        insertMention,
      saveApprovalQuick,
      getDiffRenderState,
      setDiffRenderMode,
      sioCall,
      settingsRpc: settingsRpcClient,
      uiRpc: uiRpcClient,
      waitForWs,
      fetchAppConfig,
      formatJsonSetting,
      parseJsonSetting,
        openDropdownMenu,
        closeDropdownMenu,
        toggleDropdownMenu,
    },
    closeDropdownMenu,
    sioCall,
    documentRef: document,
    windowRef: window,
  });

  bindWidescreenResizer();
  updateWidescreenLayout();
  initializeBoot(handleSocketEvent);
  setupSettingsBoot();
  installCodexAgentGlobal();
  bindStartStopButtons();
  initExternalModules();
  bindDropdownClose();
  const { dispatchInput, bindInputHandlers, syncMarkdownFromSettings } = bindInputFlow({
    getState: () => ({
      commandRunning,
      applyingDraft,
      draftDirty,
      conversationSettings,
      conversationMeta,
      isMobile,
      transcriptLoading,
      transcriptStart,
      transcriptEnd,
      transcriptTotal,
      transcriptHistoryMode,
      transcriptLiveDirty,
      topSpacerEl,
      bottomSpacerEl,
      estimatedRowHeight,
      scrollProgrammatic: _scrollProgrammatic,
      autoScroll,
    }),
    setState: (patch: AnyRecord) => {
      if (patch.draftDirty !== undefined) draftDirty = patch.draftDirty;
      if (patch.autoScroll !== undefined) autoScroll = patch.autoScroll;
      if (patch.pendingNewConversation !== undefined) pendingNewConversation = patch.pendingNewConversation;
      if (patch.pendingRollout !== undefined) pendingRollout = patch.pendingRollout;
      if (patch.pickerPath !== undefined) pickerPath = patch.pickerPath;
      if (patch.pickerMode !== undefined) pickerMode = patch.pickerMode;
    },
    elements: {
      sendBtn,
      promptEl,
      mentionPillEl,
      hostCloseTopEl,
      hostCloseDrawerEl,
      contextRemainingEl,
      interruptBtn,
      scrollContainer,
      scrollBtn,
      timelineEl,
      markdownToggleEl,
      trackEditsToggleEl,
      lineNumbersToggleEl,
      settingsCwdEl,
    },
    sendShellCommand,
    sendUserMessage,
    getPromptText,
    clearPrompt,
    clearDraft,
    saveDraftDebounced,
    openPicker,
    sendHostCloseMessage,
    bindSplashTabHandlers,
    initTribute,
    requestContextCompact,
    interruptTurn,
    updateScrollButton,
    maybeAutoScroll,
    isNearBottom,
    loadOlderTranscript,
    loadNewerTranscript,
    fetchConversation,
    restorePendingApprovals,
    refreshPlanSurface,
    postTe2OpenRequest,
    setMarkdownEnabled,
    setTrackEditsEnabled,
    resetTimeline,
    replayTranscript,
    sioCall,
    documentRef: document,
    windowRef: window,
  });

  bindInputHandlers();
  bindComposerSelectionTracking();
});
