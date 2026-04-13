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
import { bindSocketEvents } from './js/codex_agent/events/socket.ts';
import { bindEventRouter } from './js/codex_agent/events/router.ts';
import { bindPlanOverlay } from './js/codex_agent/plan_overlay.ts';
import { bindPlanModal } from './js/codex_agent/plan_modal.ts';
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
import { bindHostRuntime } from './js/codex_agent/host/runtime.ts';
import { bindWidescreenLayout } from './js/codex_agent/layout/widescreen.ts';
import { bindTimelineRows } from './js/codex_agent/timeline/rows.ts';
import { bindTimelineLiveItems } from './js/codex_agent/timeline/live_items.ts';
import { bindTimelineReplay } from './js/codex_agent/timeline/replay.ts';
import { createConversationsRpcClient } from './js/codex_agent/rpc/conversations/client.ts';
import {
  readRpcTransportEnabledPreference,
  writeRpcTransportEnabledPreference,
} from './js/codex_agent/rpc/transport.ts';

declare const hljs: any;

type AnyRecord = Record<string, any>;

document.addEventListener('DOMContentLoaded', () => {
  const getById = document.getElementById.bind(document);
  const queryOne = document.querySelector.bind(document);
  const byId = (id) => getById(id) as any;
  const query = (selector) => queryOne(selector) as any;

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
		  let conversationList: any[] = [];
  let conversationPreviewCache: AnyRecord = {};
  let appConfig: AnyRecord = {};
  let activeView = 'splash';
		  // Client-local selection (do not treat SSOT active conversation as an authority after boot).
		  let clientConversationId = null;
		  let clientActiveView = null;
      let miniConversationDrawerOpen = false;
		  let hostUi: AnyRecord = { showClose: false, parentOrigin: null };
  const SPLASH_TAB_STORAGE_KEY = 'codex_splash_tab';
  function normalizeSplashTab(value) {
    return value === 'project' ? 'project' : 'all';
  }
  function readSplashTabPreference() {
    try {
      return normalizeSplashTab(localStorage.getItem(SPLASH_TAB_STORAGE_KEY));
    } catch {
      return 'all';
    }
  }
  function writeSplashTabPreference(value) {
    try {
      localStorage.setItem(SPLASH_TAB_STORAGE_KEY, normalizeSplashTab(value));
    } catch {
      // Ignore storage failures; splash tab state still works in-memory.
    }
  }
		  let splashTab = readSplashTabPreference(); // 'all' | 'project'
  let rpcTransportEnabled = readRpcTransportEnabledPreference(window);
  let pendingNewConversation = false;
  let pendingRollout = null;
  let lastEventType = null;
  let pickerPath = null;
  let pickerMode = 'cwd';
  let pickerItems = [];
  let filterTimer = null;
  let openDropdownEl = null;
  let initialized = false;
  let wsOpen = false;
  let wsReadyResolve: any = null;
  let wsReadyPromise = new Promise((resolve) => { wsReadyResolve = resolve; });
  let wsReconnectDelay = 1000;
  let _socket: any = null; // Socket.IO instance (set in connectWS)
  let modelList: any[] = []; // Cached model list with supportedReasoningEfforts
  let runtimeOptions: AnyRecord = {};
  let activeRuntimeOptionValues: AnyRecord = {};
  let planDocState = { has_plan: false, plan_exists: false, plan_content: '', plan_path: null, plan_source: null };
  let todoState = { has_todo: false, plan_steps: [] };
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
  let activeToolRenderPolicy = {
    default: {
      request: { kind: 'plain' },
      response: { kind: 'plain' },
    },
    rules: [],
  };
  let commandRunning = false; // Whether a PTY command is currently running
  let activeAgentPtyBlockId = null;
  const pending = new Map();

  // Detect mobile for input behavior
  const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
                   ('ontouchstart' in window && window.innerWidth < 768);

  const assistantRows = new Map();
  const reasoningRows = new Map();
  const diffRows = new Map();
  const toolRows = new Map();
  const shellRows = new Map();  // Track streaming shell output rows
  let topSpacerEl = null;
  let bottomSpacerEl = null;
  let placeholderCleared = false;
  let messageCount = 0;
  let tokenCount = 0;
  let contextWindow = null;
  let autoScroll = true;
  let _scrollProgrammatic = false; // Guard: prevent programmatic scroll from unpinning
  let normalizeTimer = null;
  let isNormalizing = false;
  let tributeInstance: any = null;
  let transcriptTotal = 0;
  let planOverlayEl = null;
  let planListEl = null;
  let planCollapsed = false;
  let timelineStickyHeaders = null;
  const planItems = new Map();
  let transcriptStart = 0;
  let transcriptEnd = 0;
  let transcriptLimit = 500;
  let transcriptLoading = false;
  let estimatedRowHeight = 28;
  let draftSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let lastDraftHash: string | null = null;
  let draftDirty = false;
  let applyingDraft = false;

  function isMarkdownEnabled() {
    return markdownEnabled;
  }

  function setMarkdownEnabled(enabled) {
    markdownEnabled = enabled === true;
    if (markdownToggleEl) markdownToggleEl.checked = markdownEnabled;
    if (settingsMarkdownEl) settingsMarkdownEl.checked = markdownEnabled;
  }

  function setTrackEditsEnabled(enabled) {
    trackEditsEnabled = enabled === true;
    if (trackEditsToggleEl) trackEditsToggleEl.checked = trackEditsEnabled;
  }

  function setLineNumbersEnabled(enabled) {
    lineNumbersEnabled = enabled === true;
    if (lineNumbersToggleEl) lineNumbersToggleEl.checked = lineNumbersEnabled;
    document.body.classList.toggle('line-numbers-enabled', lineNumbersEnabled);
  }

  function setViewWrapEnabled(enabled) {
    viewWrapEnabled = enabled === true;
    if (settingsViewWrapEl) settingsViewWrapEl.checked = viewWrapEnabled;
  }


  function isDiffSyntaxEnabled() {
    return diffSyntaxHighlight;
  }

  function setDiffSyntaxEnabled(enabled) {
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

  // ── Subagent containers ──────────────────────────────────────────
  const subagentContainers = new Map(); // subagent_id -> { row, body, header, statusEl, items: [] }

  function getSubagentContainer(id, name, intent) {
    let sa = subagentContainers.get(id);
    if (!sa) {
      clearPlaceholder();
      const row = document.createElement('div');
      row.className = 'timeline-row subagent-card';
      row.dataset.subagentId = id;

      // Header is OUTSIDE body — always visible even when collapsed
      const header = document.createElement('div');
      header.className = 'subagent-header command-ribbon';
      const label = document.createElement('span');
      label.textContent = `${name || 'subagent'}: ${intent || 'working'}`;
      const statusEl = document.createElement('span');
      statusEl.className = 'subagent-status';
      statusEl.textContent = '⏳ running';
      header.append(label, statusEl);
      row.appendChild(header);

      const body = document.createElement('div');
      body.className = 'subagent-body';
      row.appendChild(body);

      insertRow(row);
      makeCollapsible(row, `subagent:${id}`, false, {
        headerEl: header,
        fullHeaderToggle: true,
      });
      sa = { row, body, header, statusEl, label, items: [] };
      subagentContainers.set(id, sa);
    }
    return sa;
  }

  function getLiveEventParent(evt) {
    if (!evt || !evt.subagent_id) return null;
    return getSubagentContainer(evt.subagent_id, '', '').body;
  }

  function finalizeSubagent(id, summary, success) {
    const sa = subagentContainers.get(id);
    if (!sa) return;
    sa.statusEl.textContent = success !== false ? '✓ done' : '✗ failed';
    if (summary) {
      const summaryEl = document.createElement('div');
      summaryEl.className = 'subagent-summary';
      summaryEl.style.cssText = 'padding: 4px 14px; font-size: 0.85em; opacity: 0.7; font-style: italic;';
      summaryEl.textContent = summary;
      sa.body.appendChild(summaryEl);
    }
  }

  // ── Collapsible card helpers ──────────────────────────────────────
  const _expandedCards = new Set(
    JSON.parse(localStorage.getItem('expandedCards') || '[]')
  );
  function _saveExpandedCards() {
    localStorage.setItem('expandedCards', JSON.stringify([..._expandedCards]));
  }
  function makeCollapsible(row, cardId, startExpanded, options: AnyRecord = {}) {
    if (!row) return;
    const {
      headerEl = row.querySelector('.command-ribbon') || row.querySelector('.diff-path-label'),
      persist = true,
      fullHeaderToggle = false,
      toggleZone = !fullHeaderToggle,
      onToggle = null,
    } = options;
    if (!headerEl) return;

    row.classList.add('collapsible');
    const isExpanded = Boolean(startExpanded || (persist && cardId && _expandedCards.has(cardId)));
    row.classList.toggle('expanded', isExpanded);

    let twistyEl = headerEl.querySelector(':scope > .twisty') || headerEl.querySelector('.twisty');
    if (!twistyEl) {
      twistyEl = document.createElement('span');
      twistyEl.className = 'twisty';
      twistyEl.textContent = '▶';
      headerEl.appendChild(twistyEl);
    }

    function syncExpandedState(expanded) {
      headerEl.dataset.expanded = expanded ? 'true' : 'false';
    }

    function persistExpandedState(expanded) {
      if (!persist || !cardId) return;
      if (expanded) _expandedCards.add(cardId);
      else _expandedCards.delete(cardId);
      _saveExpandedCards();
    }

    function toggleCollapse(forceExpanded?: boolean) {
      const expanded = typeof forceExpanded === 'boolean'
        ? forceExpanded
        : !row.classList.contains('expanded');
      row.classList.toggle('expanded', expanded);
      persistExpandedState(expanded);
      syncExpandedState(expanded);
      if (typeof onToggle === 'function') onToggle(expanded);
      maybeAutoScroll();
      return expanded;
    }

    (row as any)._toggleCollapse = toggleCollapse;
    syncExpandedState(isExpanded);

    twistyEl.style.pointerEvents = 'auto';
    twistyEl.style.cursor = 'pointer';
    twistyEl.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleCollapse();
    });

    if (toggleZone) {
      let toggleZoneEl = headerEl.querySelector(':scope > .ribbon-toggle-zone') || headerEl.querySelector('.ribbon-toggle-zone');
      if (!toggleZoneEl) {
        toggleZoneEl = document.createElement('span');
        toggleZoneEl.className = 'ribbon-toggle-zone';
        headerEl.appendChild(toggleZoneEl);
      }
      toggleZoneEl.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleCollapse();
      });
    }

    if (fullHeaderToggle) {
      headerEl.addEventListener('click', (e) => {
        if (e.target.closest('.twisty') || e.target.closest('.ribbon-toggle-zone')) return;
        toggleCollapse();
      });
    }
  }

  // Note: underscore emphasis is handled by the markdown renderer; do not escape underscores
  // in the raw text stream, otherwise users will see literal backslashes in output.

  const shellSemantic = bindShellSemantic({
    getEnabled: () => semanticShellRibbonEnabled,
    setEnabled: (enabled) => { semanticShellRibbonEnabled = enabled === true; },
    getQuoteParsingEnabled: () => semanticShellQuoteParsingEnabled,
    setQuoteParsingEnabled: (enabled) => { semanticShellQuoteParsingEnabled = enabled === true; },
    getCheckboxEl: () => byId('settings-semantic-shell-ribbon'),
    escapeHtml,
  });

  function isSemanticShellRibbonEnabled() {
    return shellSemantic.isSemanticShellRibbonEnabled();
  }

  function setSemanticShellRibbonEnabled(enabled) {
    shellSemantic.setSemanticShellRibbonEnabled(enabled);
  }

  function setSemanticShellQuoteParsingEnabled(enabled) {
    semanticShellQuoteParsingEnabled = enabled === true;
    shellSemantic.setSemanticShellQuoteParsingEnabled(enabled);
  }

  function setActiveToolRenderPolicy(policy) {
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

  async function ensureTreeSitterRibbonReady() {
    return shellSemantic.ensureTreeSitterRibbonReady();
  }

  function renderShellCmdRibbon(el, cmd) {
    return shellSemantic.renderShellCmdRibbon(el, cmd);
  }

  function setCommandRunning(running) {
    commandRunning = Boolean(running);
  }

  // Strip OpenAI citation markers like 'citeturn1file0L11-L26'
  function stripCitations(text) {
    if (!text) return text;
    // Match patterns like 'citeturn0file0' or 'citeturn1file0L11-L26'
    return text.replace(/'citeturn\d+file\d+(?:L\d+(?:-L\d+)?)?'/g, '');
  }

  const FILE_EXT_LANG_MAP = {
    'js': 'javascript', 'mjs': 'javascript', 'ts': 'typescript', 'tsx': 'typescript', 'jsx': 'javascript',
    'py': 'python', 'rb': 'ruby', 'rs': 'rust', 'go': 'go',
    'java': 'java', 'kt': 'kotlin', 'scala': 'scala',
    'c': 'c', 'h': 'c', 'cpp': 'cpp', 'cc': 'cpp', 'hpp': 'cpp',
    'cs': 'csharp', 'fs': 'fsharp',
    'php': 'php', 'swift': 'swift', 'r': 'r',
    'json': 'json', 'yaml': 'yaml', 'yml': 'yaml', 'toml': 'toml',
    'xml': 'xml', 'html': 'html', 'htm': 'html', 'css': 'css', 'scss': 'scss',
    'md': 'markdown', 'markdown': 'markdown',
    'sh': 'bash', 'bash': 'bash', 'zsh': 'bash', 'fish': 'bash',
    'sql': 'sql', 'graphql': 'graphql', 'gql': 'graphql',
    'dockerfile': 'dockerfile', 'makefile': 'makefile',
    'tf': 'hcl', 'hcl': 'hcl',
    'lua': 'lua', 'vim': 'vim', 'el': 'lisp', 'clj': 'clojure',
    'ex': 'elixir', 'exs': 'elixir', 'erl': 'erlang',
    'hs': 'haskell', 'ml': 'ocaml', 'nim': 'nim', 'zig': 'zig',
  };

  function detectLangFromPath(file) {
    if (!file) return null;
    const ext = file.split('.').pop()?.toLowerCase();
    if (ext && FILE_EXT_LANG_MAP[ext]) return FILE_EXT_LANG_MAP[ext];
    const basename = file.split('/').pop()?.toLowerCase();
    if (basename === 'dockerfile') return 'dockerfile';
    if (basename === 'makefile' || basename === 'gnumakefile') return 'makefile';
    if (basename?.endsWith('rc') || basename?.startsWith('.')) return 'bash';
    return null;
  }

  function resolveHljsLanguage(lang) {
    if (typeof hljs === 'undefined' || !lang) return null;
    const requested = String(lang).trim().toLowerCase();
    if (!requested) return null;
    const fallbackMap = {
      javascript: ['javascript', 'typescript'],
      jsx: ['javascript', 'typescript'],
      typescript: ['typescript', 'javascript'],
      tsx: ['typescript', 'javascript'],
      html: ['html', 'xml'],
      htm: ['html', 'xml'],
      xml: ['xml', 'html'],
      markdown: ['markdown'],
      md: ['markdown'],
      json: ['json'],
      css: ['css', 'scss'],
      scss: ['scss', 'css'],
      yaml: ['yaml'],
      yml: ['yaml'],
      toml: ['ini'],
      bash: ['bash'],
      sh: ['bash'],
    };
    const candidates = fallbackMap[requested] || [requested];
    for (const candidate of candidates) {
      if (candidate && hljs.getLanguage(candidate)) return candidate;
    }
    return null;
  }

  function buildViewCardTitle(path, viewRange, fallbackTitle = '') {
    const shortPath = path ? String(path).split('/').pop() : '';
    if (Array.isArray(viewRange) && viewRange.length >= 2 && Number.isFinite(Number(viewRange[0])) && Number.isFinite(Number(viewRange[1]))) {
      return `${shortPath || fallbackTitle || 'view'}  Lines ${Number(viewRange[0])}–${Number(viewRange[1])}`;
    }
    if (Array.isArray(viewRange) && viewRange.length === 1 && Number.isFinite(Number(viewRange[0]))) {
      return `${shortPath || fallbackTitle || 'view'}  Line ${Number(viewRange[0])}+`;
    }
    return shortPath || fallbackTitle || 'view';
  }

  function detectLangFromCommand(command) {
    if (!command) return null;

    const shCMatch = command.match(/sh\s+-[lc]+\s+['"](.+)['"]\s*$/);
    const innerCmd = shCMatch ? shCMatch[1] : command;

    const catMatch = innerCmd.match(/\b(?:cat|head|tail|less|more|bat)\s+['"]*([^\s'"]+)/);
    if (catMatch) {
      const lang = detectLangFromPath(catMatch[1]);
      if (lang) return lang;
    }

    const sedMatch = innerCmd.match(/\bsed\s+(?:-[^\s]+\s+)*'[^']+'\s+([^\s'"]+)\s*$/);
    if (sedMatch) {
      const lang = detectLangFromPath(sedMatch[1]);
      if (lang) return lang;
    }

    const awkGrepMatch = innerCmd.match(/\b(?:awk|grep)\s+(?:-[^\s]+\s+)*(?:'[^']+'|"[^"]+")\s+([^\s'"]+)\s*$/);
    if (awkGrepMatch) {
      const lang = detectLangFromPath(awkGrepMatch[1]);
      if (lang) return lang;
    }

    const segments = innerCmd.split(/\s*(?:\|\||&&|\||;)\s*/g);
    let best = null;
    for (const seg of segments) {
      const toks = seg.match(/(?:'[^']*'|"[^"]*"|`[^`]*`|[^\s]+)/g) || [];
      for (const t of toks) {
        const raw = String(t || '').trim();
        if (!raw) continue;
        const unq = (raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'")) || (raw.startsWith('`') && raw.endsWith('`'))
          ? raw.slice(1, -1)
          : raw;
        if (unq.startsWith('-')) continue;
        const m = unq.match(/([^\s'"]+\.\w+)$/);
        if (m) {
          const lang = detectLangFromPath(m[1]);
          if (lang) best = lang;
        }
      }
    }
    if (best) return best;

    const anyFileMatch = innerCmd.match(/([^\s'"]+\.\w+)\s*$/);
    if (anyFileMatch) {
      const lang = detectLangFromPath(anyFileMatch[1]);
      if (lang) return lang;
    }

    if (innerCmd.includes('python') || innerCmd.includes('python3')) return 'python';
    if (innerCmd.includes('node ') || innerCmd.includes('npx ')) return 'javascript';
    if (innerCmd.includes('ruby ')) return 'ruby';
    if (innerCmd.includes('go run')) return 'go';
    if (innerCmd.includes('rustc') || innerCmd.includes('cargo')) return 'rust';
    return null;
  }

  function highlightCodeAlways(text, lang) {
    if (typeof hljs === 'undefined' || !text?.trim()) {
      return escapeHtml(text || '');
    }
    try {
      const resolvedLang = resolveHljsLanguage(lang);
      if (resolvedLang) {
        return hljs.highlight(text, { language: resolvedLang, ignoreIllegals: true }).value;
      }
      const result = hljs.highlightAuto(text);
      if (result.relevance > 5) {
        return result.value;
      }
    } catch (_) {
      // fall through
    }
    return escapeHtml(text || '');
  }

  function normalizeStructuredViewLines(lines) {
    if (!Array.isArray(lines) || !lines.length) return null;
    const normalized = [];
    for (const entry of lines) {
      if (!entry || typeof entry !== 'object') return null;
      const rawLineNo = entry.line_no ?? entry.lineNo;
      const lineNo = Number(rawLineNo);
      if (!Number.isFinite(lineNo)) return null;
      normalized.push({
        line_no: lineNo,
        content: entry.content === null || entry.content === undefined ? '' : String(entry.content),
      });
    }
    return normalized;
  }

  function synthesizeStructuredViewLines(content, viewRange) {
    if (typeof content !== 'string') return null;
    if (!Array.isArray(viewRange) || !viewRange.length) return null;
    const startLine = Number(viewRange[0]);
    if (!Number.isFinite(startLine)) return null;
    if (!content) return [];
    const normalizedContent = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const rawLines = normalizedContent.split('\n');
    if (rawLines.length && rawLines[rawLines.length - 1] === '') {
      rawLines.pop();
    }
    return rawLines.map((rawLine, idx) => ({
      line_no: startLine + idx,
      content: rawLine,
    }));
  }

  function splitHighlightedHtmlIntoLines(highlightedHtml) {
    const html = String(highlightedHtml || '');
    const tokens = html.split(/(<[^>]+>)/g);
    const openTags = [];
    const lines = [];
    let current = '';

    const closeAllTags = () => openTags.slice().reverse().map((tag) => `</${tag.name}>`).join('');
    const reopenAllTags = () => openTags.map((tag) => tag.open).join('');

    for (const token of tokens) {
      if (!token) continue;
      if (token.startsWith('<')) {
        current += token;
        if (token.startsWith('<!--') || token.startsWith('<!')) {
          continue;
        }
        const match = token.match(/^<\s*(\/?)\s*([a-zA-Z0-9:-]+)/);
        if (!match) continue;
        const isClosing = Boolean(match[1]);
        const tagName = String(match[2] || '').toLowerCase();
        const selfClosing = /\/\s*>$/.test(token) || ['br', 'hr', 'img', 'input', 'meta', 'link'].includes(tagName);
        if (isClosing) {
          for (let idx = openTags.length - 1; idx >= 0; idx -= 1) {
            if (openTags[idx].name === tagName) {
              openTags.splice(idx, 1);
              break;
            }
          }
        } else if (!selfClosing) {
          openTags.push({ name: tagName, open: token });
        }
        continue;
      }

      const textParts = token.split('\n');
      for (let idx = 0; idx < textParts.length; idx += 1) {
        current += textParts[idx];
        if (idx < textParts.length - 1) {
          lines.push(current + closeAllTags());
          current = reopenAllTags();
        }
      }
    }

    lines.push(current);
    return lines;
  }

  function buildHighlightedViewLineHtml(lines, lang) {
    if (!Array.isArray(lines) || !lines.length) return [];
    const highlighted = highlightCodeAlways(lines.map((line) => line.content).join('\n'), lang);
    const htmlLines = splitHighlightedHtmlIntoLines(highlighted);
    return htmlLines.length === lines.length ? htmlLines : [];
  }

  function getStructuredViewGutterDigits(lines) {
    if (!Array.isArray(lines) || !lines.length) return 1;
    let maxDigits = 1;
    lines.forEach((line) => {
      const raw = Number(line?.line_no);
      const digits = Number.isFinite(raw) ? String(Math.abs(Math.trunc(raw))).length : 1;
      if (digits > maxDigits) maxDigits = digits;
    });
    return maxDigits;
  }

  function renderStructuredViewLineTable(lines, path) {
    const output = document.createElement('div');
    output.className = 'command-output view-card-lines';
    output.classList.toggle('wrap-enabled', viewWrapEnabled === true);

    const table = document.createElement('table');
    table.className = 'view-card-table';
    const gutterDigits = getStructuredViewGutterDigits(lines);
    table.style.setProperty('--view-card-gutter-ch', String(gutterDigits));
    const tableBody = document.createElement('tbody');

    const lang = detectLangFromPath(path);
    const highlightedLines = typeof hljs !== 'undefined' ? buildHighlightedViewLineHtml(lines, lang) : [];

    lines.forEach((line, idx) => {
      const row = document.createElement('tr');
      row.className = 'view-card-line';
      row.dataset.lineNo = String(line.line_no);

      const gutter = document.createElement('td');
      gutter.className = 'view-card-line-no transcript-line-no';
      gutter.dataset.lineNo = String(line.line_no);
      gutter.textContent = String(line.line_no).padStart(gutterDigits, ' ');

      const content = document.createElement('td');
      content.className = 'view-card-line-content transcript-line-content';
      content.dataset.lineNo = String(line.line_no);
      const lineHtml = highlightedLines[idx];
      if (typeof lineHtml === 'string') {
        content.innerHTML = lineHtml;
      } else {
        content.textContent = line.content;
      }

      row.appendChild(gutter);
      row.appendChild(content);
      tableBody.appendChild(row);
    });

    table.appendChild(tableBody);
    output.appendChild(table);
    return output;
  }

  // Render text with code block highlighting
  function renderWithHighlighting(container, text) {
    if (!text) return;
    text = stripCitations(text);
    
    // Check if text contains code blocks
    const codeBlockRegex = /```(\w*)\n([\s\S]*?)```/g;
    let lastIndex = 0;
    let match;
    let hasCodeBlocks = false;
    
    while ((match = codeBlockRegex.exec(text)) !== null) {
      hasCodeBlocks = true;
      // Add text before code block
      if (match.index > lastIndex) {
        const textBefore = text.slice(lastIndex, match.index);
        const span = document.createElement('span');
        span.textContent = textBefore;
        container.appendChild(span);
      }
      
      // Add code block
      const lang = match[1] || '';
      const code = match[2];
      const pre = document.createElement('pre');
      const codeEl = document.createElement('code');
      if (lang) codeEl.className = `language-${lang}`;
      codeEl.textContent = code;
      pre.appendChild(codeEl);
      container.appendChild(pre);
      
      // Highlight if hljs available
      if (typeof hljs !== 'undefined') {
        hljs.highlightElement(codeEl);
      }
      
      lastIndex = match.index + match[0].length;
    }
    
    // Add remaining text
    if (hasCodeBlocks) {
      if (lastIndex < text.length) {
        const span = document.createElement('span');
        span.textContent = text.slice(lastIndex);
        container.appendChild(span);
      }
    } else {
      // No code blocks, just set text content
      container.textContent = text;
    }
  }

  // Convert absolute path to relative path based on cwd
  function toRelativePath(absPath) {
    if (!absPath) return '';
    const cwd = conversationSettings.cwd || conversationMeta.cwd || '';
    if (cwd && absPath.startsWith(cwd)) {
      let rel = absPath.slice(cwd.length);
      if (rel.startsWith('/')) rel = rel.slice(1);
      return rel || absPath;
    }
    // Try expanding ~ to match against home-relative cwd
    const home = '/data/data/com.termux/files/home';
    if (absPath.startsWith(home + '/')) {
      const cwdExpanded = cwd.startsWith('~') ? home + cwd.slice(1) : cwd;
      if (cwdExpanded && absPath.startsWith(cwdExpanded)) {
        let rel = absPath.slice(cwdExpanded.length);
        if (rel.startsWith('/')) rel = rel.slice(1);
        return rel || absPath;
      }
      // Show relative to home as ~/...
      return '~/' + absPath.slice(home.length + 1);
    }
    return absPath;
  }

  function setPill(el, text, cls) {
    if (!el) return;
    el.textContent = text;
    el.className = `pill ${cls || ''}`.trim();
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

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
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
    stripCitations,
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
    setDrawerOpen,
    applyHostUi,
    openSettingsModal,
    updateActiveConversationLabel,
    documentRef: document,
    windowRef: window,
  }) as any;

	  function toProjectRelativePath(path) {
	    if (!path || typeof path !== 'string') return null;
	    if (!path.startsWith('/')) return path;
	    const root = hostUi?.projectRoot;
	    if (!root || typeof root !== 'string') return null;
	    const rootNorm = root.endsWith('/') ? root : `${root}/`;
	    if (path === root) return '.';
	    if (!path.startsWith(rootNorm)) return null;
	    return path.slice(rootNorm.length);
	  }

	  async function ensureProjectRootLoaded() {
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

  async function openSettingsModal(...args) {
    return settingsUi?.openSettingsModal(...args);
  }

  function closeSettingsModal(...args) {
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
    setState: (patch) => {
      if (patch.conversationSettings !== undefined) conversationSettings = patch.conversationSettings;
      if (patch.runtimeOptions !== undefined) runtimeOptions = patch.runtimeOptions || {};
      if (patch.activeRuntimeOptionValues !== undefined) activeRuntimeOptionValues = patch.activeRuntimeOptionValues || {};
    },
    footerRuntimeControlsEl,
    closeDropdownMenu: (...args) => closeDropdownMenu(...args),
    toggleDropdownMenu: (...args) => toggleDropdownMenu(...args),
    sioCall,
  });

  const {
    normalizeApprovalValue,
    renderFooterRuntimeControls,
    saveApprovalQuick,
    applyRuntimeMode,
  } = runtimeFooter;

  function openPicker(...args) {
    return settingsUi?.openPicker(...args);
  }

  function closePicker(...args) {
    return settingsUi?.closePicker(...args);
  }

  function bindPickerFilter(...args) {
    return settingsUi?.bindPickerFilter(...args);
  }

  function openRolloutPicker(...args) {
    return settingsUi?.openRolloutPicker(...args);
  }

  function closeRolloutPicker(...args) {
    return settingsUi?.closeRolloutPicker(...args);
  }

  async function loadRolloutPreview(...args) {
    return settingsUi?.loadRolloutPreview(...args);
  }

  async function fetchRollouts(...args) {
    return settingsUi?.fetchRollouts(...args);
  }

  function buildDropdown(...args) {
    return settingsUi?.buildDropdown(...args);
  }

  function updateDropdownOptions(...args) {
    return settingsUi?.updateDropdownOptions(...args);
  }

  async function loadModelOptions(...args) {
    return settingsUi?.loadModelOptions(...args);
  }

  async function loadRuntimeOptions(...args) {
    return settingsUi?.loadRuntimeOptions(...args);
  }

  async function loadAgentOptions(...args) {
    return settingsUi?.loadAgentOptions(...args);
  }

  async function onAgentSelectionChange(...args) {
    return settingsUi?.onAgentSelectionChange(...args);
  }

  function updateEffortOptionsForModel(...args) {
    return settingsUi?.updateEffortOptionsForModel(...args);
  }

  function openDropdownMenu(...args) {
    return settingsUi?.openDropdownMenu(...args);
  }

  function closeDropdownMenu(...args) {
    return settingsUi?.closeDropdownMenu(...args);
  }

  function toggleDropdownMenu(...args) {
    return settingsUi?.toggleDropdownMenu(...args);
  }

  function setupDropdown(...args) {
    return settingsUi?.setupDropdown(...args);
  }

  async function fetchPicker(...args) {
    return settingsUi?.fetchPicker(...args);
  }

  async function fetchPickerSearch(...args) {
    return settingsUi?.fetchPickerSearch(...args);
  }

  function applyPickerFilter(...args) {
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
    setState: (patch) => {
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
    getRelativePath: (absolutePath, cwd) => {
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

  function scrollRowToTop(row, { clearPinned = false } = {}) {
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

  const planOverlay = bindPlanOverlay({
    timelineEl,
    getState: () => ({
      planOverlayEl,
      planListEl,
      planCollapsed,
      planItems,
      planState: currentPlanState(),
      topSpacerEl,
    }),
    setState: (patch) => {
      if (patch.planOverlayEl !== undefined) planOverlayEl = patch.planOverlayEl;
      if (patch.planListEl !== undefined) planListEl = patch.planListEl;
      if (patch.planCollapsed !== undefined) planCollapsed = patch.planCollapsed;
    },
    persistCollapsedState: (collapsed) => persistPlanCollapsedState(collapsed),
    openPlanModal: () => openPlanModal(),
  });

  function ensurePlanOverlay() {
    return planOverlay.ensurePlanOverlay();
  }

  function updatePlanItem(step, status) {
    return planOverlay.updatePlanItem(step, status);
  }

  function clearPlanOverlay() {
    return planOverlay.clearPlanOverlay();
  }

  function finalizePlanToTranscript() {
    return planOverlay.finalizePlanToTranscript();
  }

  function syncPlanOverlayUi() {
    return planOverlay.syncPlanOverlayUi();
  }

  function restorePlanOverlay(snapshot) {
    return planOverlay.restorePlanOverlay(snapshot);
  }

  function createEmptyPlanDocumentState(hasPlan = Boolean(runtimeOptions?.has_plan)) {
    return {
      has_plan: Boolean(hasPlan),
      plan_exists: false,
      plan_content: '',
      plan_path: null,
      plan_source: null,
    };
  }

  function createEmptyTodoState(hasTodo = Boolean(runtimeOptions?.has_todo)) {
    return {
      has_todo: Boolean(hasTodo),
      plan_steps: [],
    };
  }

  function createEmptyPlanState(
    hasPlan = Boolean(runtimeOptions?.has_plan),
    hasTodo = Boolean(runtimeOptions?.has_todo),
  ) {
    return {
      ...createEmptyPlanDocumentState(hasPlan),
      ...createEmptyTodoState(hasTodo),
    };
  }

  function currentPlanState() {
    return {
      has_plan: Boolean(planDocState?.has_plan ?? runtimeOptions?.has_plan),
      has_todo: Boolean(todoState?.has_todo ?? runtimeOptions?.has_todo),
      plan_exists: Boolean(planDocState?.plan_exists),
      plan_content: typeof planDocState?.plan_content === 'string' ? planDocState.plan_content : '',
      plan_steps: Array.isArray(todoState?.plan_steps) ? todoState.plan_steps : [],
      plan_path: typeof planDocState?.plan_path === 'string' ? planDocState.plan_path : null,
      plan_source: typeof planDocState?.plan_source === 'string' ? planDocState.plan_source : null,
    };
  }

  function normalizePlanDocumentState(nextState) {
    const next = nextState && typeof nextState === 'object' ? nextState : {};
    const hasPlan = next.has_plan ?? planDocState.has_plan ?? Boolean(runtimeOptions?.has_plan);
    const planContent = typeof next.plan_content === 'string'
      ? next.plan_content
      : (next.plan_exists === false ? '' : (planDocState.plan_content || ''));
    const planExists = next.plan_exists ?? (Boolean(hasPlan) && Boolean(planContent.trim()));
    return {
      ...planDocState,
      has_plan: Boolean(hasPlan),
      plan_exists: Boolean(planExists),
      plan_content: Boolean(planExists) ? planContent : '',
      plan_path: Boolean(planExists)
        ? (typeof next.plan_path === 'string' ? next.plan_path : (planDocState.plan_path || null))
        : null,
      plan_source: typeof next.plan_source === 'string' ? next.plan_source : (planDocState.plan_source || null),
    };
  }

  function normalizeTodoState(nextState) {
    const next = nextState && typeof nextState === 'object' ? nextState : {};
    const hasTodo = next.has_todo ?? todoState.has_todo ?? Boolean(runtimeOptions?.has_todo);
    const rawSteps = Array.isArray(next.plan_steps)
      ? next.plan_steps
      : (Array.isArray(next.steps) ? next.steps : (todoState.plan_steps || []));
    const steps = rawSteps
      .map((item) => {
        if (!item || typeof item !== 'object') return null;
        const step = typeof item.step === 'string' ? item.step : '';
        if (!step) return null;
        return {
          step,
          status: typeof item.status === 'string' ? item.status : 'pending',
        };
      })
      .filter(Boolean);
    return {
      has_todo: Boolean(hasTodo),
      plan_steps: steps,
    };
  }

  function currentExtensionId() {
    const candidate = conversationSettings?.agent || conversationMeta?.settings?.agent || runtimeOptions?.agent || '';
    const resolved = typeof candidate === 'string' ? candidate.trim() : '';
    return resolved === 'codex' ? '' : resolved;
  }

  async function loadExtensionUiFeatures(extensionId) {
    const resolvedExtensionId = typeof extensionId === 'string' && extensionId.trim()
      ? extensionId.trim()
      : currentExtensionId();
    if (!resolvedExtensionId) {
      setSemanticShellQuoteParsingEnabled(false);
      setActiveToolRenderPolicy(null);
      return {};
    }
    try {
      const data = await sioCall('get_extension_ui_features', {
        extension_id: resolvedExtensionId,
      });
      const uiFeatures = data?.ui_features && typeof data.ui_features === 'object' ? data.ui_features : {};
      const semanticShellRibbon = uiFeatures.semanticShellRibbon;
      setSemanticShellQuoteParsingEnabled(semanticShellRibbon?.quoteParsing === true);
      setActiveToolRenderPolicy(uiFeatures.toolRenderPolicy);
      return uiFeatures;
    } catch (_) {
      setSemanticShellQuoteParsingEnabled(false);
      setActiveToolRenderPolicy(null);
      return {};
    }
  }

  const requestCardRuntime = bindRequestCardRuntime({
    sioCall,
  });

  function syncPlanSurface({ renderModal = false } = {}) {
    const mergedState = currentPlanState();
    if (mergedState.plan_exists || (mergedState.has_todo && mergedState.plan_steps.length > 0)) {
      ensurePlanOverlay();
    }
    if (mergedState.has_todo && mergedState.plan_steps.length > 0) {
      restorePlanOverlay({ steps: mergedState.plan_steps });
    } else {
      clearPlanOverlay();
    }
    syncPlanOverlayUi();
    if (renderModal && planModal.isPlanModalOpen()) {
      renderPlanModal();
    }
    return mergedState;
  }

  function applyAuthoritativePlanState(nextState) {
    planDocState = normalizePlanDocumentState(nextState);
    todoState = normalizeTodoState(nextState);
    planDocDirty = false;
    return syncPlanSurface({ renderModal: true });
  }

  function applyTodoState(nextState) {
    todoState = normalizeTodoState(nextState);
    return syncPlanSurface();
  }

  function updateTodoStateStep(step, status) {
    const normalizedStep = typeof step === 'string' ? step : '';
    if (!normalizedStep) return currentPlanState();
    const normalizedStatus = typeof status === 'string' && status ? status : 'pending';
    const steps = Array.isArray(todoState.plan_steps) ? [...todoState.plan_steps] : [];
    const existingIndex = steps.findIndex((item) => item && item.step === normalizedStep);
    const nextItem = { step: normalizedStep, status: normalizedStatus };
    if (existingIndex >= 0) {
      steps[existingIndex] = nextItem;
    } else {
      steps.push(nextItem);
    }
    todoState = {
      has_todo: true,
      plan_steps: steps,
    };
    return syncPlanSurface();
  }

  function handleLiveTodoUpdate(nextState) {
    const next = nextState && typeof nextState === 'object' ? nextState : {};
    if (Array.isArray(next.plan_steps) || Array.isArray(next.steps)) {
      return applyTodoState(next);
    }
    if (typeof next.step === 'string' && next.step) {
      return updateTodoStateStep(next.step, next.status);
    }
    return currentPlanState();
  }

  function handleLivePlanState(nextState) {
    const next = nextState && typeof nextState === 'object' ? nextState : {};
    handleLiveTodoUpdate(next);
    const operation = typeof next.plan_operation === 'string' ? next.plan_operation.trim().toLowerCase() : '';
    const modalOpen = planModal.isPlanModalOpen();
    const hasPlanCapability = Boolean(next.has_plan ?? planDocState.has_plan ?? runtimeOptions?.has_plan);
    if (operation === 'update' && !modalOpen && hasPlanCapability) {
      planDocDirty = true;
      return currentPlanState();
    }
    if (operation === 'create' || operation === 'delete' || (operation === 'update' && modalOpen)) {
      const refreshPromise = refreshPlanSurface(true);
      if (refreshPromise && typeof refreshPromise.catch === 'function') {
        refreshPromise.catch((err) => console.warn('failed to refresh authoritative plan state', err));
      }
      return refreshPromise;
    }
    return currentPlanState();
  }

  async function fetchPlanState(force = false) {
    const convoId = conversationMeta?.conversation_id || null;
    const extensionId = currentExtensionId();
    const hasPlanCapability = Boolean(runtimeOptions?.has_plan);
    const hasTodoCapability = Boolean(runtimeOptions?.has_todo);
    const hasStateCapability = hasPlanCapability || hasTodoCapability;
    if (!convoId || !extensionId || !hasStateCapability) {
      return applyAuthoritativePlanState(createEmptyPlanState(hasPlanCapability, hasTodoCapability));
    }
    const requestSerial = ++planFetchSerial;
    try {
      const data = await sioCall('get_extension_plan', {
        extension_id: extensionId,
        conversation_id: convoId,
        force,
      });
      if (requestSerial !== planFetchSerial) return currentPlanState();
      if (!data || data.ok === false) {
        console.warn('failed to fetch plan state', data?.error || 'unknown error');
        return currentPlanState();
      }
      return applyAuthoritativePlanState(data);
    } catch (err) {
      if (requestSerial !== planFetchSerial) return currentPlanState();
      console.warn('failed to refresh plan state', err);
      return currentPlanState();
    }
  }

  async function refreshPlanSurface(force = false) {
    return fetchPlanState(force);
  }

  const planModal = bindPlanModal({
    elements: {
      planModalEl,
      planCloseBtn,
      planDismissBtn,
      planBodyEl,
    },
    getState: () => ({ planState: currentPlanState() }),
    renderMarkdownInto,
    highlightCode,
  });

  async function openPlanModal() {
    if (planDocDirty) {
      try {
        await refreshPlanSurface(true);
      } catch (err) {
        console.warn('failed to refresh stale plan state before opening modal', err);
      }
    }
    return planModal.openPlanModal();
  }

  function closePlanModal() {
    return planModal.closePlanModal();
  }

  function renderPlanModal() {
    return planModal.renderPlanModal();
  }

  async function persistPlanCollapsedState(collapsed) {
    planCollapsed = Boolean(collapsed);
    conversationSettings = {
      ...(conversationSettings || {}),
      planOverlayCollapsed: planCollapsed,
    };
    if (conversationMeta && typeof conversationMeta === 'object') {
      conversationMeta = {
        ...conversationMeta,
        settings: conversationSettings,
      };
    }
    const convoId = conversationMeta?.conversation_id || null;
    if (!convoId) return;
    try {
      await sioCall('conversation_update', {
        conversation_id: convoId,
        settings: {
          planOverlayCollapsed: planCollapsed,
        },
      });
    } catch (err) {
      console.warn('failed to persist plan overlay collapse state', err);
    }
  }

  async function requestContextCompact() {
    try {
      const convoId = conversationMeta?.conversation_id || null;
      const result = await conversationsRpcClient.compactConversation({ conversationId: convoId });
      if (result && result.ok === false) {
        throw new Error(String(result.error || 'compact failed'));
      }
    } catch (err) {
      console.warn('compact failed', err);
    }
  }

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
    setLastEventType: (value) => { lastEventType = value; },
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
      transcriptLimit,
      estimatedRowHeight,
    }),
    setTranscriptState: (patch) => {
      if (patch.estimatedRowHeight !== undefined) estimatedRowHeight = patch.estimatedRowHeight;
    },
  });
  const agentBlockRows = new Map();

  const timelineLiveItems = bindTimelineLiveItems({
    getState: () => ({
      lastEventType,
      activeAgentPtyBlockId,
    }),
    setState: (patch) => {
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
    stripCitations,
    escapeHtml,
    toRelativePath,
    postTe2OpenRequest,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromPath,
    resolveHljsLanguage,
    detectLangFromCommand,
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
    setLastEventType: (v) => { lastEventType = v; },
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
    setLastEventType: (value) => { lastEventType = value; },
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
      planDocState = createEmptyPlanDocumentState(Boolean(runtimeOptions?.has_plan));
      todoState = createEmptyTodoState(Boolean(runtimeOptions?.has_todo));
      closePlanModal();
    },
    syncPlanOverlayUi,
    timelineStickyUpdate: () => timelineStickyHeaders?.update?.(),
    currentExtensionId,
    buildRow,
    appendErrorContent,
    renderCommandResult,
    renderViewCard,
    renderSearchCard,
    renderApproval,
    buildReplayToolRow,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromCommand,
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
    waitForWs: (...args) => waitForWs(...args),
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
    setState: (patch) => {
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
    replayTranscript: (...args: any[]) => (replayTranscript as any)(...args),
    refreshPlanSurface: (...args) => refreshPlanSurface(...args),
    restorePendingApprovals,
    setDrawerOpen,
    updateConversationHeaderLabel,
  });

  async function sendRpc(method, params, options = {}) {
    return rpcFlow.sendRpc(method, params, options);
  }

  async function respondApproval(requestId, decision) {
    return respondApprovalImpl(requestId, decision);
  }

  const conversationsRpcClient = createConversationsRpcClient({
    sioCall,
    windowRef: window,
  });

  const { resetWsReady, markWsOpen, waitForWs, connectWS } = bindSocketEvents({
    getWsState: () => ({ wsOpen, wsReadyResolve, wsReadyPromise, wsReconnectDelay }),
    setWsState: (patch) => {
      if (patch.wsOpen !== undefined) wsOpen = patch.wsOpen;
      if (patch.wsReadyResolve !== undefined) wsReadyResolve = patch.wsReadyResolve;
      if (patch.wsReadyPromise !== undefined) wsReadyPromise = patch.wsReadyPromise;
      if (patch.wsReconnectDelay !== undefined) wsReconnectDelay = patch.wsReconnectDelay;
    },
    setSocket: (sock) => { _socket = sock; },
    wsStatusEl,
    setPill,
    syncDraftFromServer,
    getConversationId: () => conversationMeta?.conversation_id,
    getWindow: () => window,
    conversationsRpcClient,
    isRpcTransportEnabled: () => rpcTransportEnabled,
  });

  /**
   * Send a Socket.IO event with ack only.
   * @param {string} event - SIO event name (e.g. 'send_message')
   * @param {object} data - Payload to send
   * @param {object} [options] - { timeoutMs } where timeoutMs: null disables the ack timeout
   * @returns {Promise<any>} Server response (ack value)
   */
  async function sioCall(event, data = {}, options: AnyRecord = {}): Promise<any> {
    if (options && (Object.prototype.hasOwnProperty.call(options, 'fallbackUrl') || Object.prototype.hasOwnProperty.call(options, 'fallbackMethod'))) {
      throw new Error(`HTTP fallbacks are disabled for Socket.IO contract: ${event}`);
    }
    const hasExplicitTimeout = Boolean(options) && Object.prototype.hasOwnProperty.call(options, 'timeoutMs');
    const timeoutMs = hasExplicitTimeout
      ? (options.timeoutMs === null ? null : (Number.isFinite(options.timeoutMs) ? options.timeoutMs : 10000))
      : 10000;
    if (_socket && _socket.connected) {
      return new Promise((resolve, reject) => {
        let timer = null;
        if (Number.isFinite(timeoutMs)) {
          timer = setTimeout(() => {
            reject(new Error(`sioCall timeout: ${event}`));
          }, timeoutMs);
        }
        _socket.emit(event, data, (ack) => {
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
    if (ready && _socket && _socket.connected) {
      return sioCall(event, data, options);
    }
    return { ok: false, error: 'Socket.IO not connected' };
  }
	  async function fetchConversation(conversationId = null) {
	    try {
	      const cid = conversationId || clientConversationId;
	      const data = await sioCall('conversation_get', {
	        conversation_id: cid || null,
	      });
	      if (!data || data.ok === false) return;
	      conversationMeta = data;
	      conversationSettings = conversationMeta?.settings || {};
          planCollapsed = conversationSettings?.planOverlayCollapsed === true;
          syncPlanOverlayUi();
	      if (!clientConversationId && conversationMeta?.conversation_id) {
	        clientConversationId = conversationMeta.conversation_id;
	      }
	      if (!clientActiveView && conversationMeta?.active_view) {
	        clientActiveView = conversationMeta.active_view;
	      }
	      activeView = clientActiveView || conversationMeta?.active_view || 'splash';
        activeRuntimeOptionValues = {};
      if (activeView !== 'conversation') {
        miniConversationDrawerOpen = false;
      }
      await loadRuntimeOptions(
        currentExtensionId() || null,
        conversationMeta?.conversation_id,
      );
      await loadExtensionUiFeatures(currentExtensionId());
      await requestCardRuntime.preload(currentExtensionId());
      closePlanModal();
      applyAuthoritativePlanState(createEmptyPlanState(Boolean(runtimeOptions?.has_plan), Boolean(runtimeOptions?.has_todo)));
      setDrawerOpen(activeView === 'conversation');
	      applyHostUi();
	      updateActiveConversationLabel();
      renderFooterRuntimeControls();
      // Sync markdown toggle from settings
      setMarkdownEnabled(conversationSettings?.markdown !== false);
      // Sync track-edits toggle from settings
      setTrackEditsEnabled(conversationSettings?.trackEdits === true);
      // Sync line-number toggle from settings
      setLineNumbersEnabled(conversationSettings?.lineNumbers === true);
      // Sync view-card wrap toggle from settings
      setViewWrapEnabled(conversationSettings?.viewWrap === true);
      // Sync diff syntax toggle from settings
      setDiffSyntaxEnabled(conversationSettings?.diffSyntax === true);
      // Sync semantic shell ribbon toggle from settings (Tree-sitter)
      setSemanticShellRibbonEnabled(conversationSettings?.semanticShellRibbon === true);
      if (conversationSettings?.semanticShellRibbon === true) {
        ensureTreeSitterRibbonReady();
      }
      // Restore draft from conversation meta
	      restoreDraft();
	    } catch {
	      // Don't touch statusEl here - it's for server status only
	    }
	    updateConversationHeaderLabel();
	  }

  async function fetchStatus() {
    try {
      const data = await sioCall('get_status', {});
      if (data.running) {
        setPill(statusEl, 'running', 'ok');
      } else {
        setPill(statusEl, 'idle', 'warn');
      }
    } catch {
      setPill(statusEl, 'error', 'err');
    }
  }

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
    }),
    setState: (patch) => {
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
  });

  const {
    fetchTranscriptRange,
    loadOlderTranscript,
    replayTranscript,
  } = bindTranscriptLoader({
    getConversationId: () => conversationMeta?.conversation_id || null,
    sioCall,
    getTranscriptState: () => ({
      transcriptTotal,
      transcriptStart,
      transcriptEnd,
      transcriptLimit,
      transcriptLoading,
    }),
    setTranscriptState: (patch) => {
      if (patch.transcriptTotal !== undefined) transcriptTotal = patch.transcriptTotal;
      if (patch.transcriptStart !== undefined) transcriptStart = patch.transcriptStart;
      if (patch.transcriptEnd !== undefined) transcriptEnd = patch.transcriptEnd;
      if (patch.transcriptLimit !== undefined) transcriptLimit = patch.transcriptLimit;
      if (patch.transcriptLoading !== undefined) transcriptLoading = patch.transcriptLoading;
    },
    renderTranscriptEntries,
    scrollContainer,
    setScrollProgrammatic: (v) => { _scrollProgrammatic = Boolean(v); },
    isSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    maybeAutoScroll,
    setLastEventType: (v) => { lastEventType = v; },
    refreshPlanSurface,
  });

  const { handleEvent } = bindEventRouter({
    getState: () => ({
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
    setState: (patch) => {
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
    setLastEventType: (v) => { lastEventType = v; },
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
    setState: (patch) => {
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
      onAgentChange: async (agentId) => { await loadExtensionUiFeatures(agentId); },
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
  initializeBoot(handleEvent);
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
      topSpacerEl,
      estimatedRowHeight,
      scrollProgrammatic: _scrollProgrammatic,
      autoScroll,
    }),
    setState: (patch) => {
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
