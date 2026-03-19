import {
  createStreamingParser,
  highlightCode,
  renderMarkdownBlock,
  renderMarkdownInto,
  streamEnd,
  streamWrite,
} from './js/codex_agent/markdown.js';
import { bindAssistantStream } from './js/codex_agent/assistant_stream.js';
import { bindShellRender } from './js/codex_agent/shell_render.js';
import { bindConversationDrawer } from './js/codex_agent/conversation_drawer.js';
import { bindTranscriptLoader } from './js/codex_agent/transcript_loader.js';
import { bindTranscriptMetrics } from './js/codex_agent/transcript_metrics.js';
import { bindDiffRendering } from './js/codex_agent/diff/rendering.js';
import { bindSocketEvents } from './js/codex_agent/events/socket.js';
import { bindEventRouter } from './js/codex_agent/events/router.js';
import { bindPlanOverlay } from './js/codex_agent/plan_overlay.js';
import { bindPlanModal } from './js/codex_agent/plan_modal.js';
import { bindTimelineStickyHeaders } from './js/codex_agent/timeline_sticky_headers.js';
import { bindSessionFlow } from './js/codex_agent/orchestrator/session_flow.js';
import { bindRpcFlow } from './js/codex_agent/orchestrator/rpc_flow.js';
import { bindPtyRuntime } from './js/codex_agent/pty/runtime.js';
import { bindShellSemantic } from './js/codex_agent/shell_semantic.js';
import { formatJsonSetting, parseJsonSetting } from './js/codex_agent/settings/runtime_helpers.js';
import { bindSettingsSaveFlow } from './js/codex_agent/settings/save_flow.js';
import { bindSettingsUiFlow } from './js/codex_agent/settings/ui_flow.js';
import { bindBootInitFlow } from './js/codex_agent/boot/init_flow.js';
import { bindInputFlow } from './js/codex_agent/boot/input_flow.js';

document.addEventListener('DOMContentLoaded', () => {
  const statusEl = document.getElementById('agent-status');
  const wsStatusEl = document.getElementById('agent-ws');
  const timelineEl = document.getElementById('agent-timeline');
  const timelineWrapEl = timelineEl?.closest('.timeline-wrap');
  const scrollContainer = timelineWrapEl || timelineEl;
  const statusRibbonEl = document.getElementById('status-ribbon');
  const statusLabelEl = document.getElementById('status-label');
  const statusReasoningEl = document.getElementById('status-reasoning');
  const statusDotEl = document.getElementById('status-dot');
  const startBtn = document.getElementById('agent-start');
  const stopBtn = document.getElementById('agent-stop');
  const promptEl = document.getElementById('agent-prompt');
  const footerEl = document.querySelector('.composer');
  const footerTerminalToggleEl = document.getElementById('footer-terminal-toggle');
  const composerTerminalEl = document.getElementById('composer-terminal');
  const sendBtn = document.getElementById('agent-send');
  const interruptBtn = document.getElementById('turn-interrupt');
  const counterMessagesEl = document.getElementById('counter-messages');
  const counterTokensEl = document.getElementById('counter-tokens');
  const contextRemainingEl = document.getElementById('context-remaining');
  const scrollBtn = document.getElementById('scroll-pin');
  const activeConversationEl = document.getElementById('active-conversation');
  const conversationTitleEl = document.getElementById('conversation-title');
  const splashViewEl = document.getElementById('splash-view');
  const drawerEl = document.getElementById('conversation-drawer');
  const conversationBodyEl = document.getElementById('conversation-body');
  const conversationListEl = document.getElementById('conversation-list');
  const conversationMiniDrawerEl = document.getElementById('conversation-mini-drawer');
  const conversationMiniListEl = document.getElementById('conversation-mini-list');
  const conversationMiniCloseBtn = document.getElementById('conversation-mini-close');
  const conversationCreateBtn = document.getElementById('conversation-create');
  const conversationBackBtn = document.getElementById('conversation-back');
  const conversationSettingsBtn = document.getElementById('conversation-settings');
  const splashSettingsModalEl = document.getElementById('splash-settings-modal');
  const splashSettingsUserNameEl = document.getElementById('splash-settings-user-name');
  const splashSettingsTe2McpIntegrationEl = document.getElementById('splash-settings-te2-mcp-integration');
  const settingsModalEl = document.getElementById('settings-modal');
  const settingsCloseBtn = document.getElementById('settings-close');
  const settingsCancelBtn = document.getElementById('settings-cancel');
  const settingsSaveBtn = document.getElementById('settings-save');
  const settingsCwdEl = document.getElementById('settings-cwd');
  const settingsApprovalEl = document.getElementById('settings-approval');
  const settingsSandboxEl = document.getElementById('settings-sandbox');
  const settingsModelEl = document.getElementById('settings-model');
  const settingsEffortEl = document.getElementById('settings-effort');
  const settingsSummaryEl = document.getElementById('settings-summary');
  const settingsDeveloperInstructionsEl = document.getElementById('settings-developer-instructions');
  const settingsLabelEl = document.getElementById('settings-label');
  const settingsAliasEl = document.getElementById('settings-alias');
  const settingsCommandLinesEl = document.getElementById('settings-command-lines');
  const settingsMarkdownEl = document.getElementById('settings-markdown');
  const settingsXtermEl = document.getElementById('settings-xterm');
  const settingsDiffSyntaxEl = document.getElementById('settings-diff-syntax');
  const settingsSemanticShellRibbonEl = document.getElementById('settings-semantic-shell-ribbon');
  const settingsTe2McpIntegrationEl = document.getElementById('settings-te2-mcp-integration');
  const markdownToggleEl = document.getElementById('markdown-toggle');
  const trackEditsToggleEl = document.getElementById('track-edits-toggle');
  const footerApprovalValue = document.getElementById('footer-approval-value');
  const footerApprovalToggle = document.getElementById('footer-approval-toggle');
  const footerApprovalOptions = document.getElementById('footer-approval-options');
  const settingsAgentEl = document.getElementById('settings-agent');
  const settingsAgentToggle = document.getElementById('settings-agent-toggle');
  const settingsAgentOptions = document.getElementById('settings-agent-options');
  const settingsAgentRowEl = document.getElementById('settings-agent-row');
  const settingsRolloutEl = document.getElementById('settings-rollout');
  const settingsRolloutRowEl = document.getElementById('settings-rollout-row');
  const settingsApprovalToggle = document.getElementById('settings-approval-toggle');
  const settingsSandboxToggle = document.getElementById('settings-sandbox-toggle');
  const settingsModelToggle = document.getElementById('settings-model-toggle');
  const settingsEffortToggle = document.getElementById('settings-effort-toggle');
  const settingsSummaryToggle = document.getElementById('settings-summary-toggle');
  const settingsApprovalOptions = document.getElementById('settings-approval-options');
  const settingsSandboxOptions = document.getElementById('settings-sandbox-options');
  const settingsModelOptions = document.getElementById('settings-model-options');
  const settingsEffortOptions = document.getElementById('settings-effort-options');
  const settingsSummaryOptions = document.getElementById('settings-summary-options');
  const settingsCwdBrowseBtn = document.getElementById('settings-cwd-browse');
  const settingsRolloutBrowseBtn = document.getElementById('settings-rollout-browse');
  const pickerOverlayEl = document.getElementById('cwd-picker');
  const pickerCloseBtn = document.getElementById('picker-close');
  const pickerPathEl = document.getElementById('picker-path');
  const pickerListEl = document.getElementById('picker-list');
  const pickerUpBtn = document.getElementById('picker-up');
  const pickerSelectBtn = document.getElementById('picker-select');
  const pickerTitleEl = document.getElementById('picker-title');
  const pickerFilterEl = document.getElementById('picker-filter');
  const rolloutOverlayEl = document.getElementById('rollout-picker');
	  const rolloutCloseBtn = document.getElementById('rollout-close');
	  const rolloutListEl = document.getElementById('rollout-list');
  const mentionPillEl = document.getElementById('mention-pill');
  const hostCloseTopEl = document.getElementById('host-close-top');
  const hostCloseDrawerEl = document.getElementById('host-close-drawer');
  const planModalEl = document.getElementById('plan-modal');
  const planCloseBtn = document.getElementById('plan-close');
  const planDismissBtn = document.getElementById('plan-dismiss');
  const planBodyEl = document.getElementById('plan-body');

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

	  let conversationMeta = {};
		  let conversationSettings = {};
		  let conversationList = [];
		  let conversationPreviewCache = {};
		  let appConfig = {};
		  let activeView = 'splash';
		  // Client-local selection (do not treat SSOT active conversation as an authority after boot).
		  let clientConversationId = null;
		  let clientActiveView = null;
      let miniConversationDrawerOpen = false;
		  let hostUi = { showClose: false, parentOrigin: null };
		  let splashTab = 'all'; // 'all' | 'project'
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
  let wsReadyResolve = null;
  let wsReadyPromise = new Promise((resolve) => { wsReadyResolve = resolve; });
  let wsReconnectDelay = 1000;
  let _socket = null; // Socket.IO instance (set in connectWS)
  let modelList = []; // Cached model list with supportedReasoningEfforts
  let runtimeOptions = {};
  let planDocState = { has_plan: false, plan_exists: false, plan_content: '', plan_path: null, plan_source: null };
  let todoState = { has_todo: false, plan_steps: [] };
  let planFetchSerial = 0;
  let settingsUi = null;
  let markdownEnabled = true; // Toggle for markdown rendering
  let trackEditsEnabled = false; // Toggle for TE2 edit tracking per conversation
  let useXterm = true; // Toggle for xterm.js vs text box rendering
  let diffSyntaxHighlight = false; // Toggle for syntax highlighting in diffs
  let semanticShellRibbonEnabled = false; // Tree-sitter semantic highlighting for shell command ribbons
  let commandRunning = false; // Whether a PTY command is currently running
  let ptyWebSocket = null; // Raw PTY WebSocket connection
  let ptyWebSocketConvoId = null; // conversation_id currently bound to ptyWebSocket
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
  let tributeInstance = null;
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
  let terminalMode = false;
  let composerTerm = null;        // xterm instance for composer terminal
  let composerFitAddon = null;    // FitAddon for auto-sizing
  let composerResizeObserver = null;
  let composerPrimedConvoId = null;
  let composerPriming = false;
  let composerPendingChunks = [];
  let composerPendingBytes = 0;
  let composerPrimedWithTail = false;
  let composerResizeSyncTimer = null;
  let composerFitRaf = null;
  let composerFitFramesRemaining = 0;
  let composerResizeSuppressed = false; // Suppress resize sync during initial open
  let composerLastResizeKey = null;
  let draftSaveTimer = null;
  let lastDraftHash = null;
  let draftDirty = false;
  let applyingDraft = false;

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
  function makeCollapsible(row, cardId, startExpanded, options = {}) {
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

    function toggleCollapse(forceExpanded) {
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

    row._toggleCollapse = toggleCollapse;
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

  // Debounced draft save to persist composer content
  function saveDraftDebounced() {
    if (draftSaveTimer) clearTimeout(draftSaveTimer);
    // Capture conversation_id NOW to avoid race condition on conversation switch
    const convoId = conversationMeta?.conversation_id;
    if (!convoId) return;

    draftSaveTimer = setTimeout(async () => {
      const text = getPromptText();
      // Simple hash to avoid sending unchanged drafts
      const hash = text.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0).toString(16);
      if (hash === lastDraftHash) return;
      lastDraftHash = hash;
      try {
        await sioCall('conversation_draft', {
          conversation_id: convoId,
          draft: text
        }, { fallbackUrl: '/api/appserver/conversation/draft' });
        if (conversationMeta && conversationMeta.conversation_id === convoId) {
          conversationMeta.draft = text;
        }
        draftDirty = false;
      } catch (e) {
        console.warn('Draft save failed:', e);
      }
    }, 500);
  }

  function restoreDraft() {
    if (!promptEl) return;
    const draft = conversationMeta?.draft;
    if (draft && typeof draft === 'string' && draft.trim()) {
      renderPromptFromText(draft);
      draftDirty = false;
      // Update hash to match restored draft
      lastDraftHash = draft.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0).toString(16);
    } else {
      // No draft - clear composer
      clearPrompt();
      draftDirty = false;
      lastDraftHash = null;
    }
  }

  function clearDraft() {
    lastDraftHash = null;
    draftDirty = false;
    // Fire and forget - clear the draft from storage
    const convoId = conversationMeta?.conversation_id;
    if (convoId) {
      sioCall('conversation_draft', {
        conversation_id: convoId,
        draft: ''
      }).catch(() => {});
    }
  }

  async function syncDraftFromServer(convoId) {
    if (!convoId) return;
    if (!promptEl) return;
    if (draftDirty) return;
    try {
      const meta = await sioCall('conversation_get', { conversation_id: convoId }, {
        fallbackUrl: '/api/appserver/conversation',
        fallbackMethod: 'GET',
      });
      if (!meta || meta.ok === false || meta.conversation_id !== convoId) return;
      const serverDraft = meta.draft;
      if (typeof serverDraft !== 'string') return;
      const localText = getPromptText();
      if (serverDraft === localText) return;
      renderPromptFromText(serverDraft);
      if (conversationMeta) conversationMeta.draft = serverDraft;
      draftDirty = false;
      lastDraftHash = serverDraft.split('').reduce((a, c) => ((a << 5) - a + c.charCodeAt(0)) | 0, 0).toString(16);
    } catch {
      // ignore
    }
  }

  function setTerminalMode(enabled) {
    terminalMode = Boolean(enabled);
    document.body.classList.toggle('terminal-mode', terminalMode);
    footerEl?.classList.toggle('terminal-active', terminalMode);
    
    if (terminalMode) {
      // Hide send button, show terminal
      if (sendBtn) sendBtn.style.display = 'none';
      initComposerTerminal();
    } else {
      // Show send button, focus prompt
      if (sendBtn) sendBtn.style.display = '';
      promptEl?.focus();
      
      // Close WebSocket so next open gets a fresh connection with proper resize
      if (ptyWebSocket) {
        try {
          ptyWebSocket.onopen = null;
          ptyWebSocket.onmessage = null;
          ptyWebSocket.onerror = null;
          ptyWebSocket.onclose = null;
        } catch (_) {}
        try { ptyWebSocket.close(); } catch (_) {}
        ptyWebSocket = null;
        ptyWebSocketConvoId = null;
      }
    }
    
    if (promptEl) {
      promptEl.setAttribute(
        'data-placeholder',
        terminalMode ? 'Command… (Enter to run)' : '@ to mention files'
      );
    }
    if (footerTerminalToggleEl) {
      footerTerminalToggleEl.classList.toggle('active', terminalMode);
      footerTerminalToggleEl.textContent = terminalMode ? 'chat' : '>_';
    }
  }

  // Initialize composer terminal (xterm in footer)
  function initComposerTerminal() {
    if (!composerTerminalEl) return;
    const convoId = conversationMeta?.conversation_id;
    
    // Create xterm if not exists
    const createdNow = !composerTerm;
    if (createdNow && typeof Terminal !== 'undefined') {
      composerTerm = new Terminal({
        convertEol: true,
        cursorBlink: true,
        scrollback: 5000,
        fontFamily: 'JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
        fontSize: 12,
        theme: { background: '#000000', foreground: '#c9d1d9' },
      });
      composerTerm.open(composerTerminalEl);
      
      // FitAddon DISABLED - it causes readline redraw cascades
      // if (typeof FitAddon !== 'undefined') {
      //   composerFitAddon = new FitAddon.FitAddon();
      //   composerTerm.loadAddon(composerFitAddon);
      // }
      
      // ResizeObserver DISABLED - it triggers fit which causes issues
      // if (typeof ResizeObserver !== 'undefined') {
      //   composerResizeObserver = new ResizeObserver(() => fitComposerTerminal());
      //   composerResizeObserver.observe(composerTerminalEl);
      // }
      
      // Send input to PTY via WebSocket
      composerTerm.onData((data) => {
        if (ptyWebSocket && ptyWebSocket.readyState === WebSocket.OPEN) {
          ptyWebSocket.send(data);
        }
      });
      
      // Sync resize to backend (but respect suppression flag during open)
      composerTerm.onResize(({ cols, rows }) => {
        if (composerResizeSuppressed) {
          console.log('onResize suppressed during open:', cols, 'x', rows);
          return;
        }
        scheduleComposerTerminalResizeSync(cols, rows);
      });
    }
    
    // Keep the composer terminal continuously in sync with the live PTY stream.
    // This avoids needing to rehydrate on every open/close (which is inherently
    // lossy without full screen state) and prevents cursor/prompt drift.
    const needsPrime = Boolean(createdNow) || (convoId && composerPrimedConvoId !== convoId);
    if (needsPrime) {
      composerPriming = true;
      composerPendingChunks = [];
      composerPendingBytes = 0;
      composerPrimedWithTail = false;
    }
    
    // Suppress auto-resize during open sequence to prevent FitAddon/ResizeObserver interference
    composerResizeSuppressed = true;
    composerLastResizeKey = null; // Reset so we always send resize on open
    
    // Fit and focus after DOM update
    requestAnimationFrame(async () => {
      // Ensure font is loaded before fit so cols/rows are stable.
      await ensureFontLoaded('JetBrains Mono', 900);

      if (needsPrime) {
        // Start from a blank viewport, then hydrate once, then stream live forever.
        try { composerTerm?.reset(); } catch (_) {}
        const didPrime = await primeComposerTerminalFromRawTail();
        composerPrimedWithTail = Boolean(didPrime);
        composerPriming = false;
        // If we primed from tail, buffered chunks likely overlap; drop them.
        if (!composerPrimedWithTail && composerPendingChunks.length && composerTerm) {
          for (const chunk of composerPendingChunks) {
            try { composerTerm.write(chunk); } catch (_) {}
          }
        }
        composerPendingChunks = [];
        composerPendingBytes = 0;
      }

      // Connect PTY WebSocket
      connectPtyWebSocket();
      
      // Send resize with SIGWINCH after short delay to let layout settle
      setTimeout(() => {
        const cols = composerTerm?.cols || 80;
        const rows = composerTerm?.rows || 24;
        console.log('Sending resize on open:', cols, 'x', rows);
        syncComposerTerminalSize(cols, rows);
      }, 150);
      
      // Re-enable auto-resize (FitAddon is disabled anyway)
      composerResizeSuppressed = false;
      
      composerTerm?.focus();
    });
  }

  function fitComposerTerminal() {
    if (composerFitAddon && composerTerm) {
      try { composerFitAddon.fit(); } catch (_) {}
    }
  }

  function requestComposerFit(frames = 8) {
    if (!composerTerm || !composerFitAddon) return;
    composerFitFramesRemaining = Math.max(composerFitFramesRemaining, Math.max(1, Number(frames) || 0));
    if (composerFitRaf) return;
    const step = () => {
      composerFitRaf = null;
      if (!composerTerm || !composerFitAddon) return;
      try { composerFitAddon.fit(); } catch (_) {}
      composerFitFramesRemaining = Math.max(0, composerFitFramesRemaining - 1);
      if (composerFitFramesRemaining > 0) {
        composerFitRaf = requestAnimationFrame(step);
      }
    };
    composerFitRaf = requestAnimationFrame(step);
  }

  async function ensureFontLoaded(fontFamily, timeoutMs = 900) {
    const fam = String(fontFamily || '').trim();
    if (!fam) return;
    if (!document.fonts || typeof document.fonts.load !== 'function') return;
    try {
      await Promise.race([
        document.fonts.load(`12px "${fam}"`),
        new Promise(resolve => setTimeout(resolve, Math.max(0, Number(timeoutMs) || 0))),
      ]);
    } catch (_) {}
  }

  function syncComposerTerminalSize(cols, rows) {
    const convoId = conversationMeta?.conversation_id;
    if (!convoId || !cols || !rows) return;
    fetch('/api/mcp/agent-pty/resize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: convoId, cols, rows }),
    }).catch(() => {});
  }

  function scheduleComposerTerminalResizeSync(cols, rows, opts = {}) {
    const convoId = conversationMeta?.conversation_id;
    if (!convoId) return;
    const c = Math.max(1, Number(cols) || 0);
    const r = Math.max(1, Number(rows) || 0);
    if (!c || !r) return;

    const key = `${convoId}:${c}x${r}`;
    if (!opts.force && composerLastResizeKey === key) return;
    composerLastResizeKey = key;

    if (composerResizeSyncTimer) {
      try { clearTimeout(composerResizeSyncTimer); } catch (_) {}
      composerResizeSyncTimer = null;
    }

    let attempts = 0;
    const tryOnce = async () => {
      attempts += 1;
      // Conversation may have switched mid-retry; stop.
      if (conversationMeta?.conversation_id !== convoId) return;
      try {
        const resp = await fetch('/api/mcp/agent-pty/resize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversation_id: convoId, cols: c, rows: r }),
        });
        if (resp && resp.ok) return;
      } catch (_) {}
      if (attempts >= 12) return;
      const delay = Math.min(1500, 80 * attempts);
      composerResizeSyncTimer = setTimeout(tryOnce, delay);
    };

    // If not forced and we already have a websocket + a stable terminal, one attempt is enough.
    void tryOnce();
  }

  async function primeComposerTerminalFromRawTail() {
    const convoId = conversationMeta?.conversation_id;
    if (!convoId) return;
    if (!terminalMode || !composerTerm) return;
    if (composerPrimedConvoId === convoId) return;

    try {
      // Prefer framework_shells log tail for UI rehydration (matches what the user sees).
      const r1 = await fetch(`/api/pty/fws_tail?conversation_id=${encodeURIComponent(convoId)}&tail_lines=200`, { cache: 'no-store' });
      if (r1.ok) {
        const data1 = await r1.json();
        if (data1 && data1.ok && Array.isArray(data1.stdout_tail)) {
          const text1 = data1.stdout_tail.join('');
          if (text1) {
            composerTerm.write(text1);
            composerPrimedConvoId = convoId;
            return true;
          }
        }
      }

      // Fallback to conversation-local raw tail (lossless byte log).
      const r2 = await fetch(`/api/pty/raw_tail?conversation_id=${encodeURIComponent(convoId)}&max_bytes=65536`, { cache: 'no-store' });
      if (!r2.ok) return;
      const data2 = await r2.json();
      if (!data2 || !data2.ok) return;
      const b64 = data2.data_b64 || '';
      if (!b64) {
        composerPrimedConvoId = convoId;
        return false;
      }
      const text2 = _decodeBase64ToUtf8(b64);
      if (text2) {
        composerTerm.write(text2);
      }
      composerPrimedConvoId = convoId;
      return Boolean(text2);
    } catch (_) {
      // Best-effort; live WS will still work.
    }
    return false;
  }

  function isMarkdownEnabled() {
    return markdownEnabled;
  }

  function setMarkdownEnabled(enabled) {
    markdownEnabled = enabled;
    if (markdownToggleEl) markdownToggleEl.checked = enabled;
    if (settingsMarkdownEl) settingsMarkdownEl.checked = enabled;
  }

  function setTrackEditsEnabled(enabled) {
    trackEditsEnabled = enabled;
    if (trackEditsToggleEl) trackEditsToggleEl.checked = enabled;
  }

  function isXtermEnabled() {
    return useXterm;
  }

  function setXtermEnabled(enabled) {
    useXterm = enabled;
    if (settingsXtermEl) settingsXtermEl.checked = enabled;
  }

  function isDiffSyntaxEnabled() {
    return diffSyntaxHighlight;
  }

  function setDiffSyntaxEnabled(enabled) {
    diffSyntaxHighlight = enabled;
    const el = document.getElementById('settings-diff-syntax');
    if (el) el.checked = enabled;
  }

  // Detect language from command for syntax highlighting
  function detectLangFromCommand(command) {
    if (!command) return null;
    
    // Handle sh -c 'command' wrapper - extract inner command
    const shCMatch = command.match(/sh\s+-[lc]+\s+['"](.+)['"]\s*$/);
    const innerCmd = shCMatch ? shCMatch[1] : command;
    
    const extMap = {
      'js': 'javascript', 'ts': 'typescript', 'tsx': 'typescript', 'jsx': 'javascript',
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
    
    // Helper to extract lang from file path
    function langFromFile(file) {
      if (!file) return null;
      const ext = file.split('.').pop()?.toLowerCase();
      if (ext && extMap[ext]) return extMap[ext];
      const basename = file.split('/').pop()?.toLowerCase();
      if (basename === 'dockerfile') return 'dockerfile';
      if (basename === 'makefile' || basename === 'gnumakefile') return 'makefile';
      if (basename?.endsWith('rc') || basename?.startsWith('.')) return 'bash';
      return null;
    }
    
    // Pattern 1: cat/head/tail/less + file
    const catMatch = innerCmd.match(/\b(?:cat|head|tail|less|more|bat)\s+['"]*([^\s'"]+)/);
    if (catMatch) {
      const lang = langFromFile(catMatch[1]);
      if (lang) return lang;
    }
    
    // Pattern 2: sed -n 'range' file (file is last argument)
    const sedMatch = innerCmd.match(/\bsed\s+(?:-[^\s]+\s+)*'[^']+'\s+([^\s'"]+)\s*$/);
    if (sedMatch) {
      const lang = langFromFile(sedMatch[1]);
      if (lang) return lang;
    }
    
    // Pattern 3: awk/grep with file argument
    const awkGrepMatch = innerCmd.match(/\b(?:awk|grep)\s+(?:-[^\s]+\s+)*(?:'[^']+'|"[^"]+")\s+([^\s'"]+)\s*$/);
    if (awkGrepMatch) {
      const lang = langFromFile(awkGrepMatch[1]);
      if (lang) return lang;
    }

    // Pattern 3.5: "best effort" file token anywhere in command (helps with pipes/&& chains)
    // Example: `... mcp_agent_pty_server.py | sed -n ...` or `... web-tree-sitter.js | head -n 40`
    // Strategy: split by pipes/&&/||/; and prefer the last file-like token in the first segment(s).
    const segments = innerCmd.split(/\s*(?:\|\||&&|\||;)\s*/g);
    let best = null;
    for (const seg of segments) {
      const toks = seg.match(/(?:'[^']*'|"[^"]*"|`[^`]*`|[^\s]+)/g) || [];
      for (const t of toks) {
        const raw = String(t || '').trim();
        if (!raw) continue;
        // Strip wrapping quotes for file detection only.
        const unq = (raw.startsWith('"') && raw.endsWith('"')) || (raw.startsWith("'") && raw.endsWith("'")) || (raw.startsWith('`') && raw.endsWith('`'))
          ? raw.slice(1, -1)
          : raw;
        // Ignore obvious flags/ops
        if (unq.startsWith('-')) continue;
        const m = unq.match(/([^\s'"]+\.\w+)$/);
        if (m) {
          const lang = langFromFile(m[1]);
          if (lang) best = lang;
        }
      }
      // We prefer the first segment that yields a result (usually contains the file), but allow update if later segments also yield.
    }
    if (best) return best;
    
    // Pattern 4: Any file path with known extension at end of command
    const anyFileMatch = innerCmd.match(/([^\s'"]+\.\w+)\s*$/);
    if (anyFileMatch) {
      const lang = langFromFile(anyFileMatch[1]);
      if (lang) return lang;
    }
    
    // Check for inline code execution (use innerCmd)
    if (innerCmd.includes('python') || innerCmd.includes('python3')) return 'python';
    if (innerCmd.includes('node ') || innerCmd.includes('npx ')) return 'javascript';
    if (innerCmd.includes('ruby ')) return 'ruby';
    if (innerCmd.includes('go run')) return 'go';
    if (innerCmd.includes('rustc') || innerCmd.includes('cargo')) return 'rust';
    return null;
  }

  // Apply syntax highlighting to text if enabled and hljs available
  function highlightCodeText(text, lang) {
    if (!isDiffSyntaxEnabled() || typeof hljs === 'undefined' || !text?.trim()) {
      return escapeHtml(text || '');
    }
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
      }
      // Try auto-detection
      const result = hljs.highlightAuto(text);
      if (result.relevance > 5) {
        return result.value;
      }
    } catch (e) {
      // Fall back to escaped text
    }
    return escapeHtml(text);
  }

  // Syntax highlight for command outputs (independent of diffSyntaxHighlight toggle).
  // This is used for shell command ribbons/outputs, not for diff rendering.
  function highlightCodeAlways(text, lang) {
    if (typeof hljs === 'undefined' || !text?.trim()) {
      return escapeHtml(text || '');
    }
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
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

  // --- Tree-sitter semantic shell ribbon (optional) ---
  const shellSemantic = bindShellSemantic({
    getEnabled: () => semanticShellRibbonEnabled,
    setEnabled: (enabled) => { semanticShellRibbonEnabled = enabled === true; },
    getCheckboxEl: () => document.getElementById('settings-semantic-shell-ribbon'),
    escapeHtml,
  });

  function isSemanticShellRibbonEnabled() {
    return shellSemantic.isSemanticShellRibbonEnabled();
  }

  function setSemanticShellRibbonEnabled(enabled) {
    shellSemantic.setSemanticShellRibbonEnabled(enabled);
  }

  async function ensureTreeSitterRibbonReady() {
    return shellSemantic.ensureTreeSitterRibbonReady();
  }

  function renderShellCmdRibbon(el, cmd) {
    return shellSemantic.renderShellCmdRibbon(el, cmd);
  }

  function setCommandRunning(running) {
    commandRunning = running;
    // Visual indicator: composer background goes black when stdin is active
    if (promptEl) {
      promptEl.classList.toggle('stdin-mode', running && terminalMode);
    }
    if (footerEl) {
      footerEl.classList.toggle('stdin-mode', running && terminalMode);
    }
  }

  // Strip OpenAI citation markers like 'citeturn1file0L11-L26'
  function stripCitations(text) {
    if (!text) return text;
    // Match patterns like 'citeturn0file0' or 'citeturn1file0L11-L26'
    return text.replace(/'citeturn\d+file\d+(?:L\d+(?:-L\d+)?)?'/g, '');
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

  const jsStatusEl = document.getElementById('js-status');
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

  function appendTextWithBreaks(parent, text) {
    if (!parent || text === null || text === undefined) return;
    const parts = String(text).split('\n');
    parts.forEach((part, idx) => {
      if (part) parent.appendChild(document.createTextNode(part));
      if (idx < parts.length - 1) parent.appendChild(document.createElement('br'));
    });
  }

  function isAbsPath(p) {
    return typeof p === 'string' && p.startsWith('/');
  }

  function joinPath(a, b) {
    if (!a) return b || '';
    if (!b) return a || '';
    if (a.endsWith('/')) return a + b;
    return `${a}/${b}`;
  }

  function toMentionAbsAndBestPath(rawPath) {
    const cwd = conversationSettings?.cwd || conversationMeta?.cwd || '';
    const absPath = isAbsPath(rawPath) ? rawPath : (cwd ? joinPath(cwd, rawPath) : String(rawPath || ''));
    const bestPath = (cwd && isAbsPath(absPath)) ? getRelativePath(absPath, cwd) : absPath;
    return { absPath, bestPath, cwd };
  }

  function createMentionToken(rawPath, opts) {
    opts = opts || {};
    let pathOnly = String(rawPath || '');
    let parsedLine, parsedEndLine;
    // Parse line info from path string like "path:42-50"
    const lineMatch = pathOnly.match(/^(.+):(\d+)(?:-(\d+))?$/);
    if (lineMatch) {
      pathOnly = lineMatch[1];
      parsedLine = lineMatch[2];
      parsedEndLine = lineMatch[3] || null;
    }

    const { absPath, bestPath } = toMentionAbsAndBestPath(pathOnly);
    const span = document.createElement('span');
    span.className = 'mention-token';
    span.dataset.abs = absPath || '';
    span.dataset.path = bestPath || '';
    span.setAttribute('contenteditable', 'false');
    span.title = absPath || bestPath || '';

    const line = opts.line || parsedLine;
    const endLine = opts.endLine || parsedEndLine;
    if (line) span.dataset.line = String(line);
    if (endLine) span.dataset.endLine = String(endLine);
    if (opts.col) span.dataset.col = String(opts.col);
    if (opts.endCol) span.dataset.endCol = String(opts.endCol);

    const display = String(bestPath || '').split('/').filter(Boolean).pop() || bestPath || absPath;
    let displayText = display;
    if (line) {
      displayText += ':' + line;
      if (endLine && endLine !== line) displayText += '-' + endLine;
    }

    const content = opts.content || '';
    if (content) {
      span.dataset.content = content;
      // Pill text as a text node so the code block is separate
      span.appendChild(document.createTextNode(displayText));
      // Visual code block preview inside the token
      const codeEl = document.createElement('code');
      codeEl.className = 'mention-content-preview';
      codeEl.textContent = content;
      span.appendChild(codeEl);
    } else {
      span.textContent = displayText;
    }

    return span;
  }

  function renderPromptFromText(text) {
    if (!promptEl) return;
    applyingDraft = true;
    promptEl.innerHTML = '';
    // Match `path` tokens, optionally followed by a fenced code block
    const tokenPattern = /`([^`]+)`(?:\n```\n([\s\S]*?)\n```)?/g;
    const str = String(text || '');
    let lastIndex = 0;
    let match;

    while ((match = tokenPattern.exec(str)) !== null) {
      const before = str.slice(lastIndex, match.index);
      if (before) appendTextWithBreaks(promptEl, before);

      const rawPath = match[1];
      const content = match[2] || '';
      promptEl.appendChild(createMentionToken(rawPath, content ? { content } : undefined));
      lastIndex = tokenPattern.lastIndex;
    }

    const remaining = str.slice(lastIndex);
    if (remaining) appendTextWithBreaks(promptEl, remaining);
    applyingDraft = false;
  }

  function serializePromptNode(node) {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node;
    if (el.classList.contains('mention-token')) {
      const absPath = el.dataset.abs || '';
      const fallback = el.dataset.path || el.textContent || '';
      const cwd = conversationSettings?.cwd || conversationMeta?.cwd || '';
      let pathStr = '';
      if (absPath && cwd) {
        const best = getRelativePath(absPath, cwd);
        pathStr = (best !== absPath) ? best : absPath;
      } else {
        pathStr = fallback;
      }
      if (!pathStr) return '';
      // Append line info: path:42 or path:42-48
      const line = el.dataset.line;
      const endLine = el.dataset.endLine;
      if (line) {
        pathStr += ':' + line;
        if (endLine && endLine !== line) pathStr += '-' + endLine;
      }
      const content = el.dataset.content || '';
      if (content) {
        return '`' + pathStr + '`\n```\n' + content + '\n```';
      }
      return '`' + pathStr + '`';
    }
    if (el.tagName === 'BR') return '\n';
    let out = '';
    el.childNodes.forEach((child) => { out += serializePromptNode(child); });
    if (el.tagName === 'DIV' || el.tagName === 'P') out += '\n';
    return out;
  }

  function getPromptText() {
    if (!promptEl) return '';
    let text = '';
    promptEl.childNodes.forEach((child) => { text += serializePromptNode(child); });
    return text;
  }

  function clearPrompt() {
    if (!promptEl) return;
    promptEl.innerHTML = '';
  }

  function normalizeMentions() {
    // No longer needed with Tribute - kept as no-op for compatibility
  }

  function moveCaretToEnd() {
    if (!promptEl) return;
    promptEl.focus();
    const range = document.createRange();
    range.selectNodeContents(promptEl);
    range.collapse(false);
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
  }

  // Get relative path from CWD
  function getRelativePath(absolutePath, cwd) {
    if (!absolutePath || !cwd) return absolutePath;
    const cwdNorm = cwd.endsWith('/') ? cwd : cwd + '/';
    if (absolutePath.startsWith(cwdNorm)) {
      return absolutePath.slice(cwdNorm.length);
    }
    return absolutePath;
  }

  // Initialize Tribute.js for @ mentions
  function initTribute() {
    if (!promptEl || typeof Tribute === 'undefined') return;
    if (tributeInstance) {
      tributeInstance.detach(promptEl);
    }
    
    tributeInstance = new Tribute({
      trigger: '@',
      allowSpaces: false,
      menuShowMinLength: 1, // Need at least 1 char to search
      noMatchTemplate: '<li class="tribute-no-match">No files found</li>',
      selectTemplate: function(item) {
        if (!item) return '';
        const cwd = conversationSettings?.cwd || '';
        const absPath = item.original.path || '';
        const relPath = getRelativePath(absPath, cwd);
        const bestPath = relPath || absPath;
        return '<span class="mention-token" contenteditable="false" data-abs="' +
               absPath + '" data-path="' + bestPath + '" title="' + absPath + '">' +
               item.original.name + '</span>';
      },
      menuItemTemplate: function(item) {
        const icon = item.original.type === 'directory' ? '📁' : '📄';
        const typeClass = item.original.type === 'directory' ? 'tribute-dir' : 'tribute-file';
        const cwd = conversationSettings?.cwd || '';
        const relPath = getRelativePath(item.original.path, cwd) || item.original.path || '';
        const safeName = escapeHtml(item.original.name || '');
        const safePath = escapeHtml(relPath);
        return '<div class="' + typeClass + '">' +
                 '<div class="tribute-item-name">' + icon + ' ' + safeName + '</div>' +
                 '<div class="tribute-item-path">' + safePath + '</div>' +
               '</div>';
      },
      values: async function(text, cb) {
        if (!text || !text.trim()) { cb([]); return; }
        try {
          const cwd = conversationSettings?.cwd || '~';
          const res = await fetch(`/api/fs/search?query=${encodeURIComponent(text)}&root=${encodeURIComponent(cwd)}&limit=30`);
          if (!res.ok) { cb([]); return; }
          const data = await res.json();
          // Items already sorted: directories first, then files
          cb(data.items || []);
        } catch (e) {
          console.warn('Tribute fetch error:', e);
          cb([]);
        }
      },
      lookup: 'name',
      fillAttr: 'path',
    });
    
    // Add separator between directories and files after menu renders
    promptEl.addEventListener('tribute-active-true', () => {
      setTimeout(() => {
        const menu = document.querySelector('.tribute-container ul');
        if (!menu) return;
        const items = menu.querySelectorAll('li');
        let lastWasDir = false;
        let firstFile = null;
        items.forEach(li => {
          const isDir = li.querySelector('.tribute-dir');
          if (lastWasDir && !isDir && !firstFile) {
            firstFile = li;
          }
          lastWasDir = !!isDir;
        });
        if (firstFile && !firstFile.previousElementSibling?.classList.contains('tribute-separator')) {
          const sep = document.createElement('li');
          sep.className = 'tribute-separator';
          sep.innerHTML = '<hr>';
          firstFile.parentNode.insertBefore(sep, firstFile);
        }
      }, 10);
    });
    
    tributeInstance.attach(promptEl);

    // Strip formatting on paste — keep only plain text (mention tokens are inserted programmatically)
    promptEl.addEventListener('paste', (e) => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData('text/plain');
      if (text) {
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
          const range = sel.getRangeAt(0);
          range.deleteContents();
          range.insertNode(document.createTextNode(text));
          range.collapse(false);
        }
      }
    });
  }

  // Insert mention via button (manual insertion)
  function insertMention(path, opts) {
    if (!promptEl || !path) return;
    opts = opts || {};
    const { absPath, bestPath, cwd } = toMentionAbsAndBestPath(String(path || ''));
    const relPath = getRelativePath(absPath, cwd);
    const displayPath = relPath || bestPath || absPath;
    const filename = String(displayPath || '').split('/').filter(Boolean).pop() || displayPath;
    // Build display: file.js:42-48 or file.js:42 or file.js
    let display = filename;
    if (opts.lineNo != null) {
      display += ':' + opts.lineNo;
      if (opts.endLineNo != null && opts.endLineNo !== opts.lineNo) {
        display += '-' + opts.endLineNo;
      }
    }
    
    const token = document.createElement('span');
    token.className = 'mention-token';
    token.contentEditable = 'false';
    token.dataset.abs = absPath || '';
    token.dataset.path = displayPath || '';
    token.title = absPath || '';
    if (opts.lineNo != null) token.dataset.line = String(opts.lineNo);
    if (opts.endLineNo != null) token.dataset.endLine = String(opts.endLineNo);
    if (opts.col != null) token.dataset.col = String(opts.col);
    if (opts.endCol != null) token.dataset.endCol = String(opts.endCol);
    if (opts.content) token.dataset.content = String(opts.content);
    token.textContent = display;
    
    const selection = window.getSelection();
    if (selection && selection.rangeCount > 0 && promptEl.contains(selection.getRangeAt(0).commonAncestorContainer)) {
      const range = selection.getRangeAt(0);
      range.deleteContents();
      range.insertNode(token);
      const space = document.createTextNode(' ');
      range.setStartAfter(token);
      range.insertNode(space);
      range.setStartAfter(space);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
    } else {
      promptEl.appendChild(token);
      promptEl.appendChild(document.createTextNode(' '));
      moveCaretToEnd();
    }
    promptEl.focus();
  }

  function clearPlaceholder() {
    if (placeholderCleared) return;
    const placeholder = document.getElementById('timeline-placeholder') ||
      timelineEl.querySelector('.timeline-row.muted');
    if (placeholder) placeholder.remove();
    placeholderCleared = true;
  }

  function setDrawerOpen(open) {
    if (!drawerEl) return;
    drawerEl.classList.toggle('open', Boolean(open));
    document.body.classList.toggle('drawer-open', Boolean(open));
  }

	  function applyHostUi() {
	    const show = Boolean(hostUi?.showClose) && !Boolean(hostUi?.ideMode);
	    if (hostCloseTopEl) {
	      hostCloseTopEl.style.display = (show && activeView !== 'conversation') ? 'inline-flex' : 'none';
	    }
	    if (hostCloseDrawerEl) {
	      hostCloseDrawerEl.style.display = (show && activeView === 'conversation') ? 'inline-flex' : 'none';
	    }
	    const tabsEl = document.getElementById('splash-tabs');
	    if (tabsEl) {
	      const ideMode = Boolean(hostUi?.ideMode);
	      tabsEl.style.display = ideMode ? 'flex' : 'none';
	    }
	  }

  function sendHostCloseMessage() {
    if (!window.parent || window.parent === window) return;
    const payload = {
      type: 'codex_agent_close',
      conversation_id: conversationMeta?.conversation_id || null,
      active_view: activeView || null,
    };
    const origin = hostUi?.parentOrigin || '*';
    try {
      window.parent.postMessage(payload, origin);
    } catch {
      // ignore
    }
  }

	  async function fetchHostUi() {
	    try {
	      const r = await fetch('/api/host/ui', { cache: 'no-store' });
	      if (!r.ok) return;
	      const data = await r.json();
	      const ui = data?.host_ui || {};
	      hostUi = {
	        showClose: Boolean(ui.show_close),
	        parentOrigin: (typeof ui.parent_origin === 'string' && ui.parent_origin) ? ui.parent_origin : null,
	        ideMode: Boolean(ui.ide_mode),
	        projectRoot: (typeof ui.project_root === 'string' && ui.project_root) ? ui.project_root : null,
	      };
	      applyHostUi();
	    } catch {
	      // ignore
	    }
	  }

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
      pendingNewConversation,
      miniConversationDrawerOpen,
    }),
    setState: (patch) => {
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
      if (patch.splashTab !== undefined) splashTab = patch.splashTab;
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
  });

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
	    if (!hostUi?.parentOrigin) return null;
	    try {
	      const r = await fetch(`${hostUi.parentOrigin}/api/app/file_editor_cm6/agent/cwd`, { cache: 'no-store' });
	      if (!r.ok) return null;
	      const data = await r.json();
	      const cwd = data?.data?.cwd || data?.data?.path || null;
	      if (typeof cwd === 'string' && cwd) {
	        hostUi.projectRoot = cwd;
	        return cwd;
	      }
	    } catch {
	      // ignore
	    }
	    return null;
	  }

		  async function postTe2OpenRequest({ path, line, column }) {
		    const payload = {
		      source: 'codex-agent',
		      conversation_id: conversationMeta?.conversation_id || null,
		    };
		    if (typeof path === 'string' && path) {
		      // Normalize: ensure absolute paths start with /
		      let p = path;
		      if (!p.startsWith('/') && /^(?:data|home|tmp|usr|var|etc|storage)\//.test(p)) {
		        p = '/' + p;
		      }
		      if (p.startsWith('/')) {
		        payload.path = p;
		      } else {
		        // Relative path — prepend conversation CWD to make absolute
		        const cwd = (conversationSettings?.cwd || '').replace(/\/+$/, '');
		        payload.path = cwd ? cwd + '/' + p : '/' + p;
		      }
		    }
		    if (Number.isFinite(line)) payload.line = Number(line);
		    if (Number.isFinite(column)) payload.column = Number(column);
		    console.log('[TE2_OPEN] payload:', JSON.stringify(payload), 'socket_connected:', !!(_socket && _socket.connected));
		    try {
		      const result = await sioCall('te2_agent_open', payload);
		      console.log('[TE2_OPEN] result:', JSON.stringify(result));
		    } catch (e) {
		      console.warn('[TE2_OPEN] error:', e);
		    }
		  }

  function updateActiveConversationLabel() {
    if (!activeConversationEl) return;
    activeConversationEl.textContent = '';
  }

  function getUserDisplayName() {
    const userName = typeof appConfig?.user_name === 'string' ? appConfig.user_name.trim() : '';
    return userName || 'user';
  }

  function getAssistantDisplayName() {
    const alias = typeof conversationSettings?.alias === 'string' ? conversationSettings.alias.trim() : '';
    return alias || 'assistant';
  }

  function getConversationHeaderTitle() {
    const alias = typeof conversationSettings?.alias === 'string' ? conversationSettings.alias.trim() : '';
    return alias || 'Conversation';
  }

  function updateConversationHeaderLabel() {
    if (conversationTitleEl) {
      conversationTitleEl.textContent = getConversationHeaderTitle();
    }
    const el = document.getElementById('conversation-label');
    if (!el) return;
    const label = conversationSettings?.label || '—';
    el.textContent = label;
    refreshMessageCardHeaders();
  }

  async function openSettingsModal(...args) {
    return settingsUi?.openSettingsModal(...args);
  }

  function applyAppConfig(cfg) {
    appConfig = (cfg && typeof cfg === 'object') ? cfg : {};
    if (splashSettingsUserNameEl) {
      splashSettingsUserNameEl.value = typeof appConfig?.user_name === 'string' ? appConfig.user_name : '';
    }
    if (splashSettingsTe2McpIntegrationEl) {
      splashSettingsTe2McpIntegrationEl.checked = appConfig?.te2_mcp_integration === true;
    }
    refreshMessageCardHeaders();
  }

  async function fetchAppConfig() {
    try {
      const data = await sioCall('get_config', {}, {
        fallbackUrl: '/api/appserver/config',
        fallbackMethod: 'GET',
      });
      if (!data || data.ok === false) return null;
      applyAppConfig(data);
      return data;
    } catch {
      return null;
    }
  }

  function openSplashSettingsModal() {
    if (!splashSettingsModalEl) return;
    if (splashSettingsUserNameEl) {
      splashSettingsUserNameEl.value = typeof appConfig?.user_name === 'string' ? appConfig.user_name : '';
    }
    if (splashSettingsTe2McpIntegrationEl) {
      splashSettingsTe2McpIntegrationEl.checked = appConfig?.te2_mcp_integration === true;
    }
    splashSettingsModalEl.classList.remove('hidden');
  }

  function closeSplashSettingsModal() {
    if (!splashSettingsModalEl) return;
    splashSettingsModalEl.classList.add('hidden');
  }

  async function saveSplashSettings() {
    try {
      const data = await sioCall('update_config', {
        user_name: splashSettingsUserNameEl?.value?.trim() || null,
        te2_mcp_integration: splashSettingsTe2McpIntegrationEl?.checked === true,
      }, {
        fallbackUrl: '/api/appserver/config',
      });
      if (!data || data.ok === false) return;
      applyAppConfig(data);
      closeSplashSettingsModal();
    } catch {
      // ignore
    }
  }

  function closeSettingsModal(...args) {
    return settingsUi?.closeSettingsModal(...args);
  }

  function normalizeApprovalValue(value) {
    if (!value) return value;
    if (value === 'unlessTrusted') return 'untrusted';
    return value;
  }

  function normalizeRuntimeOptionDescriptor(kind) {
    const raw = runtimeOptions?.[kind];
    if (!raw || typeof raw !== 'object') return null;
    const settingKey = typeof raw.settingKey === 'string' ? raw.settingKey.trim() : '';
    const options = Array.isArray(raw.options)
      ? raw.options
          .map((item) => {
            if (typeof item === 'string') {
              const text = item.trim();
              return text ? { value: text, label: text } : null;
            }
            if (!item || typeof item !== 'object') return null;
            const value = typeof item.value === 'string' ? item.value.trim() : '';
            if (!value) return null;
            const label = typeof item.label === 'string' && item.label.trim() ? item.label.trim() : value;
            return { value, label };
          })
          .filter(Boolean)
      : [];
    return {
      settingKey,
      options,
      current: typeof raw.current === 'string' ? raw.current.trim() : '',
      default: typeof raw.default === 'string' ? raw.default.trim() : '',
    };
  }

  function getRuntimeSettingKey(kind, fallbackKey) {
    return normalizeRuntimeOptionDescriptor(kind)?.settingKey || fallbackKey;
  }

  function getConversationSettingByRuntimeKey(kind, fallbackKey) {
    const key = getRuntimeSettingKey(kind, fallbackKey);
    if (!key || !conversationSettings || typeof conversationSettings !== 'object') return '';
    const value = conversationSettings[key];
    return typeof value === 'string' ? value : '';
  }

  function getRuntimeOptionLabel(kind, value) {
    if (!value) return '';
    const descriptor = normalizeRuntimeOptionDescriptor(kind);
    const match = descriptor?.options?.find((option) => option.value === value);
    return match?.label || value;
  }

  function renderFooterApprovalOptions() {
    if (!footerApprovalValue || !footerApprovalOptions) return;
    footerApprovalOptions.innerHTML = '';
    const descriptor = normalizeRuntimeOptionDescriptor('approval');
    const options = descriptor?.options || [];
    options.forEach((option) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.dataset.value = option.value;
      btn.textContent = option.label;
      footerApprovalOptions.appendChild(btn);
    });
    const currentValue = getConversationSettingByRuntimeKey('approval', 'approvalPolicy')
      || descriptor?.current
      || descriptor?.default
      || '';
    footerApprovalValue.textContent = getRuntimeOptionLabel('approval', currentValue) || currentValue || 'default';
  }

  async function saveApprovalQuick(value) {
    const approval = normalizeApprovalValue(value?.trim());
    if (!approval) return;
    const settingKey = getRuntimeSettingKey('approval', 'approvalPolicy');
    await sioCall('conversation_update', {
      conversation_id: conversationMeta?.conversation_id,
      settings: { [settingKey]: approval },
    }, { fallbackUrl: '/api/appserver/conversation' });
    conversationSettings = {
      ...(conversationSettings || {}),
      [settingKey]: approval,
    };
    if (runtimeOptions?.approval && typeof runtimeOptions.approval === 'object') {
      runtimeOptions = {
        ...runtimeOptions,
        approval: {
          ...runtimeOptions.approval,
          current: approval,
        },
      };
    }
    renderFooterApprovalOptions();
  }

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
      settingsMarkdownEl,
      settingsXtermEl,
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
    getRelativePath,
    insertMention,
    getWindow: () => window,
  });

  function isNearBottom() {
    if (!scrollContainer) return true;
    const distance = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
    return distance <= 24;
  }

  function maybeAutoScroll(force) {
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
      row?._toggleCollapse?.();
    },
    documentRef: document,
    windowRef: window,
  });

  function ensureActivityRow() {
    // No longer needed - status ribbon is always present in HTML
    // Kept as no-op for compatibility
  }

  function insertRow(row, beforeEl) {
    clearPlaceholder();
    if (beforeEl && beforeEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, beforeEl);
    } else if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    maybeAutoScroll();
  }

  function buildRow(kind, title) {
    const row = document.createElement('div');
    row.className = `timeline-row ${kind || ''}`.trim();
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = title || '';
    const body = document.createElement('div');
    body.className = 'body';
    row.append(meta, body);
    return { row, body };
  }

  function getMessageRoleLabel(role) {
    if (role === 'assistant') return getAssistantDisplayName();
    if (role === 'user') return getUserDisplayName();
    return role || 'message';
  }

  function updateMessageCardHeader(row, role, text) {
    if (!row) return;
    row.dataset.messageRole = role || 'message';
    row._messageText = text || '';
    const headerEl = row.querySelector(':scope > .message-header');
    if (!headerEl) return;
    const titleEl = headerEl.querySelector('.message-header-title');
    if (titleEl) titleEl.textContent = getMessageRoleLabel(role);
    headerEl.dataset.expanded = 'true';
  }

  function refreshMessageCardHeaders() {
    if (!timelineEl) return;
    timelineEl.querySelectorAll('.message-card').forEach((row) => {
      updateMessageCardHeader(
        row,
        row.dataset.messageRole || row._messageRole || 'message',
        row._messageText || '',
      );
    });
  }

  function buildMessageCard(role, text = '') {
    const row = document.createElement('div');
    row.className = `timeline-row message message-card ${role === 'user' ? 'user' : ''}`.trim();

    const header = document.createElement('div');
    header.className = 'message-header command-ribbon';
    const title = document.createElement('span');
    title.className = 'message-header-title';
    header.append(title);

    const body = document.createElement('div');
    body.className = 'body message-body';

    row.append(header, body);
    row._messageRole = role || 'message';
    updateMessageCardHeader(row, role, text);
    return { row, body, header, title };
  }

  function createRow(kind, title, beforeEl, parentEl = null) {
    const { row, body } = buildRow(kind, title);
    if (parentEl) {
      clearPlaceholder();
      if (row.parentElement !== parentEl) parentEl.appendChild(row);
      maybeAutoScroll();
    } else {
      insertRow(row, beforeEl);
    }
    return { row, body };
  }

  function setActivity(label, active) {
    // Update status ribbon instead of activity row
    if (statusLabelEl) statusLabelEl.textContent = label || 'idle';
    if (statusRibbonEl) statusRibbonEl.classList.toggle('active', Boolean(active));
  }

  function setReasoningRibbon(text) {
    if (!statusReasoningEl) return;
    if (!text) {
      clearReasoningRibbon();
      return;
    }
    statusReasoningEl.textContent = text;
    statusReasoningEl.classList.add('active');
  }

  function clearReasoningRibbon() {
    if (!statusReasoningEl) return;
    statusReasoningEl.textContent = '';
    statusReasoningEl.classList.remove('active');
  }

  function setStatusDot(status) {
    // status: 'success', 'error', 'warning', or null/'' for neutral
    if (!statusDotEl) return;
    statusDotEl.classList.remove('success', 'error', 'warning');
    if (status) statusDotEl.classList.add(status);
  }

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
    return conversationSettings?.agent || conversationMeta?.settings?.agent || 'codex';
  }

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

  function openPlanModal() {
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
      }, {
        fallbackUrl: '/api/appserver/conversation',
      });
    } catch (err) {
      console.warn('failed to persist plan overlay collapse state', err);
    }
  }

  function setCounter(el, value) {
    if (!el) return;
    el.textContent = String(value);
  }

  function incrementMessages() {
    messageCount += 1;
    setCounter(counterMessagesEl, messageCount);
  }

  function updateTokens(total) {
    if (!Number.isFinite(total)) return;
    tokenCount = Number(total);
    setCounter(counterTokensEl, tokenCount);
    // Don't update context percentage here - only update when we get both
    // total and context_window from the same event to avoid stale data
  }

  function updateContextRemaining(total, windowSize) {
    if (!contextRemainingEl) return;
    if (!Number.isFinite(total) || !Number.isFinite(windowSize) || windowSize <= 0) {
      contextRemainingEl.textContent = '—';
      return;
    }
    const pct = Math.min(100, Math.round((Number(total) / Number(windowSize)) * 100));
    contextRemainingEl.textContent = `${pct}%`;
    // Color code based on usage
    if (pct >= 90) {
      contextRemainingEl.classList.add('critical');
      contextRemainingEl.classList.remove('warn');
    } else if (pct >= 70) {
      contextRemainingEl.classList.add('warn');
      contextRemainingEl.classList.remove('critical');
    } else {
      contextRemainingEl.classList.remove('warn', 'critical');
    }
  }

  function resetTimeline() {
    if (!timelineEl) return;
    timelineEl.innerHTML = '';
    assistantRows.clear();
    reasoningRows.clear();
    diffRows.clear();
    toolRows.clear();
    shellRows.clear();
    planOverlayEl = null;
    planListEl = null;
    planItems.clear();
    planDocState = createEmptyPlanDocumentState(Boolean(runtimeOptions?.has_plan));
    todoState = createEmptyTodoState(Boolean(runtimeOptions?.has_todo));
    closePlanModal();
    topSpacerEl = document.createElement('div');
    topSpacerEl.className = 'timeline-spacer';
    bottomSpacerEl = document.createElement('div');
    bottomSpacerEl.className = 'timeline-spacer';
    placeholderCleared = false;
    messageCount = 0;
    tokenCount = 0;
    transcriptTotal = 0;
    transcriptStart = 0;
    transcriptEnd = 0;
    lastEventType = null;
    setCounter(counterMessagesEl, messageCount);
    setCounter(counterTokensEl, tokenCount);
    if (contextRemainingEl) contextRemainingEl.textContent = '—';
    // Reset status ribbon
    setActivity('idle', false);
    setStatusDot(null);
    clearReasoningRibbon();
    timelineEl.appendChild(topSpacerEl);
    const placeholder = document.createElement('div');
    placeholder.id = 'timeline-placeholder';
    placeholder.className = 'timeline-row muted';
    placeholder.textContent = 'Waiting for events...';
    timelineEl.appendChild(placeholder);
    timelineEl.appendChild(placeholder);
    timelineEl.appendChild(bottomSpacerEl);
    ensureActivityRow();
    maybeAutoScroll(true);
    timelineStickyHeaders?.update?.();
  }

  async function requestContextCompact() {
    try {
      const convoId = conversationMeta?.conversation_id || null;
      const result = await sioCall('compact', convoId ? { conversation_id: convoId } : {}, {
        fallbackUrl: '/api/appserver/compact',
      });
      if (result && result.ok === false) {
        throw new Error(result.error || 'compact failed');
      }
    } catch (err) {
      console.warn('compact failed', err);
    }
  }

  function addMessage(role, text, parentEl = null) {
    const cleanText = role === 'assistant' ? stripCitations(text || '') : (text || '');
    const useMessageCard = role === 'assistant' || role === 'user';
    const { row, body } = useMessageCard
      ? buildMessageCard(role, cleanText)
      : buildRow('message', role === 'assistant' ? 'assistant' : role);
    if (!useMessageCard && role === 'user') row.classList.add('user');
    if (parentEl) {
      clearPlaceholder();
      parentEl.appendChild(row);
    } else {
      insertRow(row);
    }
    if ((role === 'assistant' || role === 'user') && isMarkdownEnabled()) {
      const rendered = renderMarkdownBlock(cleanText);
      body.append(rendered);
    } else {
      const pre = document.createElement('pre');
      pre.textContent = cleanText;
      body.append(pre);
    }
    incrementMessages();
    lastEventType = 'message';
    // Scroll after content is fully added
    maybeAutoScroll();
  }

  const { updateSpacerHeights, measureRowHeight } = bindTranscriptMetrics({
    timelineEl,
    getSpacerEls: () => ({ topSpacerEl, bottomSpacerEl }),
    getTranscriptState: () => ({
      transcriptStart,
      transcriptTotal,
      transcriptEnd,
      estimatedRowHeight,
    }),
    setTranscriptState: (patch) => {
      if (patch.estimatedRowHeight !== undefined) estimatedRowHeight = patch.estimatedRowHeight;
    },
  });

  function isInternalTranscriptItem(entry) {
    if (!entry || typeof entry !== 'object') return false;
    if (entry.internal === true) return true;
    if (typeof entry.internal === 'string' && ['1', 'true', 'yes', 'on'].includes(entry.internal.trim().toLowerCase())) {
      return true;
    }
    return typeof entry.visibility === 'string' && entry.visibility.trim().toLowerCase() === 'internal';
  }

  function renderTranscriptEntries(items, opts = {}) {
    if (!items || !items.length || !timelineEl) return;
    const fragment = document.createDocumentFragment();
    const truncateLines = conversationSettings?.commandOutputLines || 20;
    const pendingAgentPtyTerms = [];
    const agentPtyByBlock = new Map(); // blockId -> { row, termEl, cmd, buf }
    // Track subagent containers for replay grouping
    const replaySubagents = new Map(); // id -> { row, body, statusEl, label }
    items.forEach((entry) => {
      if (isInternalTranscriptItem(entry)) return;
      if (!entry || !entry.role) return;

      // Subagent lifecycle entries
      if (entry.role === 'subagent_start') {
        // Check if a synthetic container already exists in the DOM
        // (created by getTarget when subagent_start was outside the window)
        const existing = timelineEl.querySelector(`.subagent-card[data-subagent-id="${entry.id}"]`);
        if (existing) {
          // Update the synthetic container's label with real name/intent
          const lbl = existing.querySelector('.subagent-header span:first-child');
          if (lbl) lbl.textContent = `${entry.name || 'subagent'}: ${entry.intent || 'working'}`;
          return;
        }
        const row = document.createElement('div');
        row.className = 'timeline-row subagent-card';
        row.dataset.subagentId = entry.id;
        // Header OUTSIDE body — always visible when collapsed
        const header = document.createElement('div');
        header.className = 'subagent-header command-ribbon';
        const label = document.createElement('span');
        label.textContent = `${entry.name || 'subagent'}: ${entry.intent || 'working'}`;
        const statusEl = document.createElement('span');
        statusEl.className = 'subagent-status';
        statusEl.textContent = '⏳ running';
        header.append(label, statusEl);
        row.appendChild(header);
        const body = document.createElement('div');
        body.className = 'subagent-body';
        row.appendChild(body);
        makeCollapsible(row, `subagent:${entry.id}`, false, {
          headerEl: header,
          fullHeaderToggle: true,
        });
        replaySubagents.set(entry.id, { row, body, statusEl, label });
        fragment.appendChild(row);
        return;
      }
      if (entry.role === 'subagent_end') {
        let sa = replaySubagents.get(entry.id);
        // Also check DOM for synthetic container from a prior render batch
        if (!sa) {
          const existing = timelineEl.querySelector(`.subagent-card[data-subagent-id="${entry.id}"]`);
          if (existing) {
            sa = {
              statusEl: existing.querySelector('.subagent-status'),
              body: existing.querySelector('.subagent-body'),
            };
          }
        }
        if (sa) {
          if (sa.statusEl) sa.statusEl.textContent = entry.success !== false ? '✓ done' : '✗ failed';
          if (entry.summary) {
            const summaryEl = document.createElement('div');
            summaryEl.className = 'subagent-summary';
            summaryEl.style.cssText = 'padding: 4px 14px; font-size: 0.85em; opacity: 0.7; font-style: italic;';
            summaryEl.textContent = entry.summary;
            if (sa.body) sa.body.appendChild(summaryEl);
          }
        }
        return;
      }

      // Helper: resolve target container (subagent body or main fragment)
      // If subagent_start was outside the pagination window, create a
      // synthetic container so child events still group correctly.
      function getTarget() {
        if (entry.subagent_id) {
          if (_dbg) console.log('[SUBAGENT-REPLAY] entry has subagent_id:', entry.subagent_id, 'role:', entry.role, 'map has:', replaySubagents.has(entry.subagent_id));
          let sa = replaySubagents.get(entry.subagent_id);
          if (!sa) {
            if (_dbg) console.log('[SUBAGENT-REPLAY] Creating synthetic container for:', entry.subagent_id);
            // Synthetic subagent container (subagent_start was outside window)
            const row = document.createElement('div');
            row.className = 'timeline-row subagent-card';
            row.dataset.subagentId = entry.subagent_id;
            const header = document.createElement('div');
            header.className = 'subagent-header command-ribbon';
            const label = document.createElement('span');
            label.textContent = 'subagent: (earlier in transcript)';
            const statusEl = document.createElement('span');
            statusEl.className = 'subagent-status';
            statusEl.textContent = '✓ done';
            header.append(label, statusEl);
            row.appendChild(header);
            const body = document.createElement('div');
            body.className = 'subagent-body';
            row.appendChild(body);
            makeCollapsible(row, `subagent:${entry.subagent_id}`, true, {
              headerEl: header,
              fullHeaderToggle: true,
            });
            sa = { row, body, statusEl, label };
            replaySubagents.set(entry.subagent_id, sa);
            fragment.appendChild(row);
          }
          return sa.body;
        }
        return fragment;
      }

      if (entry.role === 'reasoning') {
        const { row, body } = buildRow('reasoning', 'reasoning');
        const pre = document.createElement('pre');
        pre.textContent = entry.text || '';
        body.append(pre);
        getTarget().appendChild(row);
        return;
      }
      if (entry.role === 'diff') {
        const { row, body } = buildRow('diff', 'diff');
        // Build ribbon — extract path from diff header if not provided
        let diffPath = entry.path || '';
        if (!diffPath && entry.text) {
          const m = entry.text.match(/^diff --git a\/.+ b\/(.+)$/m);
          if (m) diffPath = m[1];
        }
        const pathDiv = document.createElement('div');
        pathDiv.className = 'diff-path-label command-ribbon';
        if (diffPath) {
          pathDiv.innerHTML = `<strong>${escapeHtml(toRelativePath(diffPath))}</strong>`;
          pathDiv.style.cursor = 'pointer';
          pathDiv.dataset.hasClickHandler = 'true';
          pathDiv.addEventListener('click', (e) => {
            if (e.target.closest('.twisty')) return;
            postTe2OpenRequest({ path: diffPath, line: 1, column: 1 });
          });
        } else {
          pathDiv.innerHTML = '<strong>diff</strong>';
        }
        body.append(pathDiv);
        const pre = document.createElement('pre');
        pre.className = 'diff-block';
        pre.innerHTML = formatDiff(entry.text || '', diffPath);
        body.append(pre);
        makeCollapsible(row, `diff:${entry.id || diffPath || 'diff'}`, false);
        getTarget().appendChild(row);
        return;
      }
      if (entry.role === 'command') {
        const row = document.createElement('div');
        row.className = 'timeline-row command-result';
        // If this command replaces a noisy agent PTY block card, remove it on replay.
        if (entry.agent_block_id) {
          try {
            const dup = timelineEl?.querySelector(`.timeline-row.terminal-card[data-agent-block-id="${CSS.escape(entry.agent_block_id)}"]`);
            if (dup && dup.parentElement) dup.parentElement.removeChild(dup);
          } catch (_) {}
        }
        const body = document.createElement('div');
        body.className = 'body';
        // Command ribbon
        const cmdRibbon = document.createElement('div');
        cmdRibbon.className = 'command-ribbon';
        // For user terminal commands, include the prompt in the ribbon.
        const isUserTerminal = entry.source === 'user_terminal' || entry.source === 'user-terminal';
        const prompt = entry.prompt || '';
        const cmd = entry.command || '';
        const ribbonText = (prompt ? `${prompt}${cmd}` : cmd);
        if (isUserTerminal && typeof ribbonText === 'string' && ribbonText.includes('\x1b[')) {
          cmdRibbon.innerHTML = ansiToHtml(ribbonText);
        } else if (!isUserTerminal) {
          renderShellCmdRibbon(cmdRibbon, cmd);
        } else {
          cmdRibbon.textContent = ribbonText;
        }
        body.appendChild(cmdRibbon);
        // If entry has a path, make ribbon clickable (jump-to-file)
        if (entry.path) {
          cmdRibbon.style.cursor = 'pointer';
          cmdRibbon.title = entry.path;
          cmdRibbon.dataset.hasClickHandler = 'true';
          const ePath = entry.path;
          const eLine = entry.line || 1;
          cmdRibbon.addEventListener('click', (e) => {
            if (e.target.closest('.twisty') || e.target.closest('.ribbon-toggle-zone')) return;
            postTe2OpenRequest({ path: ePath, line: eLine, column: 1 });
          });
        }
        // Output
        if (entry.output) {
          const hasAnsi = typeof entry.output === 'string' && entry.output.includes('\x1b[');
          const lines = entry.output.split('\n');
          let displayOutput = entry.output;
          let truncated = false;
          if (lines.length > truncateLines) {
            displayOutput = lines.slice(0, truncateLines).join('\n');
            truncated = true;
          }
          const outputPre = document.createElement('pre');
          outputPre.className = 'command-output';
          if (isUserTerminal && hasAnsi) {
            outputPre.innerHTML = ansiToHtml(displayOutput);
            if (truncated) {
              const truncNote = document.createElement('span');
              truncNote.className = 'truncation-note';
              truncNote.textContent = `\n... (truncated, showing ${truncateLines} of ${lines.length} lines)`;
              outputPre.appendChild(truncNote);
            }
          } else {
            // Try syntax highlighting based on command
            const lang = detectLangFromCommand(cmd);
            if (lang && typeof hljs !== 'undefined') {
              outputPre.innerHTML = highlightCodeAlways(displayOutput, lang);
              if (truncated) {
                const truncNote = document.createElement('span');
                truncNote.className = 'truncation-note';
                truncNote.textContent = `\n... (truncated, showing ${truncateLines} of ${lines.length} lines)`;
                outputPre.appendChild(truncNote);
              }
            } else {
              outputPre.textContent = displayOutput;
              if (truncated) {
                outputPre.textContent += `\n... (truncated, showing ${truncateLines} of ${lines.length} lines)`;
              }
            }
          }
          body.appendChild(outputPre);
        }
        // Footer
        const footer = document.createElement('div');
        footer.className = 'command-footer';
        const parts = [];
        if (entry.exit_code !== undefined && entry.exit_code !== null && entry.exit_code !== 0) {
          parts.push(`Exit: ${entry.exit_code}`);
        }
        if (entry.duration_ms !== undefined && entry.duration_ms !== null) {
          parts.push(`Duration: ${entry.duration_ms}ms`);
        }
        if (parts.length) {
          footer.textContent = parts.join(' | ');
          body.appendChild(footer);
        }
        row.appendChild(body);
        makeCollapsible(row, `cmd:${entry.id || entry.agent_block_id || cmd.slice(0, 40)}`, false);
        getTarget().appendChild(row);
        return;
      }
      if (entry.role === 'plan') {
        const { row, body } = buildRow('plan', 'plan');
        
        // Collapsible header
        const header = document.createElement('div');
        header.className = 'plan-card-header';
        let collapsed = false;
        
        const toggleBtn = document.createElement('span');
        toggleBtn.className = 'plan-toggle';
        toggleBtn.textContent = '[-]';
        
        const title = document.createElement('span');
        title.className = 'plan-title';
        title.textContent = 'Plan';
        
        header.append(toggleBtn, title);
        body.appendChild(header);
        
        const list = document.createElement('div');
        list.className = 'plan-list';
        const steps = entry.steps || [];
        steps.forEach((item) => {
          const stepEl = document.createElement('div');
          stepEl.className = `plan-item ${item.status || 'pending'}`;
          const checkbox = document.createElement('span');
          checkbox.className = 'plan-checkbox';
          if (item.status === 'completed') {
            checkbox.textContent = '☑';
          } else if (item.status === 'in_progress') {
            checkbox.textContent = '◐';
          } else {
            checkbox.textContent = '☐';
          }
          const text = document.createElement('span');
          text.className = 'plan-text';
          text.textContent = item.step || '';
          stepEl.append(checkbox, text);
          list.appendChild(stepEl);
        });
        body.appendChild(list);
        
        // Toggle collapse
        toggleBtn.addEventListener('click', () => {
          collapsed = !collapsed;
          toggleBtn.textContent = collapsed ? '[+]' : '[-]';
          list.style.display = collapsed ? 'none' : 'flex';
        });
        
        getTarget().appendChild(row);
        return;
      }
      // Token usage entries - update context display on replay
      if (entry.role === 'token_usage') {
        if (Number.isFinite(entry.total)) {
          tokenCount = Number(entry.total);
          setCounter(counterTokensEl, tokenCount);
        }
        if (Number.isFinite(entry.context_window)) {
          contextWindow = Number(entry.context_window);
          // Use total tokens for percentage (matches CLI behavior)
          updateContextRemaining(entry.total, entry.context_window);
        }
        // Don't render token_usage as a visible row
        return;
      }
      // Status entries - update ribbon dot on replay
      if (entry.role === 'status') {
        if (entry.status) {
          setStatusDot(entry.status);
        }
        // Don't render status as a visible row
        return;
      }
      // Context compacted entries
      if (entry.role === 'context_compacted') {
        const row = document.createElement('div');
        row.className = 'timeline-row system';
        const meta = document.createElement('div');
        meta.className = 'meta';
        meta.textContent = 'context compacted';
        const body = document.createElement('div');
        body.className = 'body';
        const msg = document.createElement('div');
        msg.className = 'system-message';
        msg.textContent = 'Context was compacted to fit within the model\'s context window.';
        body.appendChild(msg);
        row.append(meta, body);
        fragment.appendChild(row);
        return;
      }
      // Shell command entries
      if (entry.role === 'shell') {
        // Match live render: single command-result row with ribbon + output
        const exitCode = entry.exit_code || 0;
        
        const row = document.createElement('div');
        row.className = 'timeline-row command-result';
        
        const body = document.createElement('div');
        body.className = 'body';
        
        // Command ribbon
        const cmdRibbon = document.createElement('div');
        cmdRibbon.className = 'command-ribbon';
        const shellCmd = String(entry.command || '');
        renderShellCmdRibbon(cmdRibbon, shellCmd);
        body.appendChild(cmdRibbon);
        
        // Output
        const pre = document.createElement('pre');
        pre.className = 'command-output';
        const stdout = String(entry.stdout || '');
        const stderr = String(entry.stderr || '');
        const outLang = detectLangFromCommand(shellCmd);
        if (stdout) {
          if (outLang && typeof hljs !== 'undefined') {
            pre.innerHTML = highlightCodeAlways(stdout, outLang);
          } else {
            pre.appendChild(document.createTextNode(stdout));
          }
        }
        if (entry.stderr) {
          const stderrEl = document.createElement('span');
          stderrEl.className = 'shell-stderr';
          stderrEl.textContent = stderr;
          pre.appendChild(stderrEl);
        }
        if (!stdout && !stderr) {
          pre.textContent = '(no output)';
        }
        body.appendChild(pre);
        
        // Footer with exit code if non-zero
        if (exitCode !== 0) {
          const footer = document.createElement('div');
          footer.className = 'command-footer';
          footer.textContent = `exit ${exitCode}`;
          body.appendChild(footer);
        }
        
        row.appendChild(body);
        getTarget().appendChild(row);
        
        setStatusDot(exitCode === 0 ? 'success' : 'error');
        return;
      }
      // Agent PTY block entries (replay)
      if (entry.role === 'agent_pty') {
        const eventType = entry.event || entry.type;
        const block = entry.block || {};
        const blockId = entry.block_id || block.block_id || entry.blockId || 'agent';
        if (eventType === 'agent_block_begin') {
          const cmd = block.cmd || '';
          const row = document.createElement('div');
          row.className = 'timeline-row command-result terminal-card';
          row.dataset.agentBlockId = blockId;

          const body = document.createElement('div');
          body.className = 'body';

          const cmdRibbon = document.createElement('div');
          cmdRibbon.className = 'command-ribbon';
          cmdRibbon.textContent = cmd ? `$ ${cmd}` : '';
          body.appendChild(cmdRibbon);

          const termEl = document.createElement('div');
          termEl.className = 'command-output';
          body.appendChild(termEl);

          row.appendChild(body);
          getTarget().appendChild(row);
          // Don't create xterm yet - element not in DOM. Will be created in RAF callback.
          const rec = { row, termEl, cmdRibbon, term: null, cmd, buf: '', text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
          agentPtyByBlock.set(blockId, rec);
          // Also register in global map so live handlers don't duplicate
          agentBlockRows.set(blockId, rec);
          pendingAgentPtyTerms.push(rec);
          return;
        }
        if (eventType === 'agent_block_delta') {
          const delta = entry.delta || '';
          if (!delta) return;
          // Check global map first (from previous replay or live)
          let rec = agentPtyByBlock.get(blockId) || agentBlockRows.get(blockId);
          if (!rec) {
            // If we got deltas without a begin (paging/replay edge), create a minimal row.
            const row = document.createElement('div');
            row.className = 'timeline-row command-result terminal-card';
            row.dataset.agentBlockId = blockId;

            const body = document.createElement('div');
            body.className = 'body';

            const cmdRibbon = document.createElement('div');
            cmdRibbon.className = 'command-ribbon';
            cmdRibbon.textContent = '';
            body.appendChild(cmdRibbon);

            const termEl = document.createElement('div');
            termEl.className = 'command-output';
            body.appendChild(termEl);

            row.appendChild(body);
            getTarget().appendChild(row);
            // Don't create xterm yet - element not in DOM
            rec = { row, termEl, cmdRibbon, term: null, cmd: '', buf: '', text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
            agentPtyByBlock.set(blockId, rec);
            agentBlockRows.set(blockId, rec);
            pendingAgentPtyTerms.push(rec);
          }
          rec.buf += delta;
          return;
        }
        if (eventType === 'agent_block_end') {
          // Footer + exit code (optional)
          const rec = agentPtyByBlock.get(blockId) || agentBlockRows.get(blockId);
          if (rec && !rec.cmd && (block.cmd || '')) {
            rec.cmd = block.cmd || '';
            // Update ribbon if cmd was set from end event
            if (rec.cmdRibbon) {
              rec.cmdRibbon.textContent = `$ ${rec.cmd}`;
            }
          }
          const exitCode = block.exit_code ?? block.exitCode;
          if (rec && exitCode !== undefined && exitCode !== null && exitCode !== 0) {
            const footer = document.createElement('div');
            footer.className = 'command-footer';
            footer.textContent = `exit ${exitCode}`;
            rec.row.querySelector('.body')?.appendChild(footer);
          }
          return;
        }
        // Unknown agent_pty event: skip rendering rather than showing noisy role labels.
        return;
      }
      // Error entries
      if (entry.role === 'error') {
        const { row, body } = buildRow('error', 'error');
        const pre = document.createElement('pre');
        pre.className = 'error-text';
        pre.textContent = entry.text || '';
        body.appendChild(pre);
        getTarget().appendChild(row);
        return;
      }
      // MCP tool call entries
      if (entry.role === 'mcp_tool') {
        const row = document.createElement('div');
        row.className = 'timeline-row command-result mcp-tool-card';
        const body = document.createElement('div');
        body.className = 'body';
        // Tool header
        const header = document.createElement('div');
        header.className = 'command-ribbon';
        const toolName = entry.tool || 'mcp_tool';
        const serverName = entry.server || '';
        header.textContent = serverName ? `${serverName}:${toolName}` : toolName;
        body.appendChild(header);
        // Arguments section
        if (entry.arguments && Object.keys(entry.arguments).length > 0) {
          const argEntries = Object.entries(entry.arguments);
          // Check if any argument looks like markdown
          const hasMarkdownArg = argEntries.some(([k, v]) => 
            typeof v === 'string' && (v.includes('\n') || v.startsWith('#') || v.includes('**') || v.includes('`'))
          );
          
          if (hasMarkdownArg) {
            argEntries.forEach(([k, v]) => {
              const argLabel = document.createElement('div');
              argLabel.className = 'mcp-tool-arg-label';
              argLabel.textContent = `${k}:`;
              body.appendChild(argLabel);
              
              if (typeof v === 'string' && (v.includes('\n') || v.startsWith('#') || v.includes('**') || v.includes('`'))) {
                const argContainer = document.createElement('div');
                argContainer.className = 'markdown-body mcp-tool-arg-value';
                renderMarkdownInto(argContainer, v);
                highlightCode(argContainer);
                body.appendChild(argContainer);
              } else {
                const argValue = document.createElement('pre');
                argValue.className = 'mcp-tool-arg-value-plain';
                argValue.textContent = typeof v === 'string' ? v : JSON.stringify(v);
                body.appendChild(argValue);
              }
            });
          } else {
            const argsPre = document.createElement('pre');
            argsPre.className = 'mcp-tool-args';
            const argLines = [];
            argEntries.forEach(([k, v]) => {
              const val = typeof v === 'string' ? v : JSON.stringify(v);
              argLines.push(`  ${k}: ${val}`);
            });
            argsPre.textContent = argLines.join('\n');
            body.appendChild(argsPre);
          }
        }
        // Result section
        if (entry.result !== undefined && entry.result !== null) {
          const resultHeader = document.createElement('div');
          resultHeader.className = 'mcp-tool-result-header';
          resultHeader.textContent = '→';
          body.appendChild(resultHeader);
          
          if (typeof entry.result === 'object') {
            // Object result: use pre-formatted display
            const resultPre = document.createElement('pre');
            resultPre.className = 'mcp-tool-content';
            const lines = [];
            Object.entries(entry.result).forEach(([k, v]) => {
              if (typeof v === 'object' && v !== null) {
                lines.push(`  ${k}:`);
                Object.entries(v).forEach(([k2, v2]) => {
                  lines.push(`    ${k2}: ${JSON.stringify(v2)}`);
                });
              } else {
                lines.push(`  ${k}: ${JSON.stringify(v)}`);
              }
            });
            resultPre.textContent = lines.join('\n');
            if (entry.is_error) resultPre.classList.add('error-text');
            body.appendChild(resultPre);
          } else {
            // String result: render as markdown
            const resultContainer = document.createElement('div');
            resultContainer.className = 'markdown-body mcp-tool-result';
            renderMarkdownInto(resultContainer, String(entry.result));
            highlightCode(resultContainer);
            if (entry.is_error) resultContainer.classList.add('error-text');
            body.appendChild(resultContainer);
          }
        }
        // Footer with duration
        if (entry.duration_ms !== undefined && entry.duration_ms !== null) {
          const footer = document.createElement('div');
          footer.className = 'command-footer';
          footer.textContent = `${entry.duration_ms}ms`;
          body.appendChild(footer);
        }
        row.appendChild(body);
        getTarget().appendChild(row);
        return;
      }
      // Web search entries
      if (entry.role === 'web_search') {
        const row = document.createElement('div');
        row.className = 'timeline-row command-result web-search-card';
        const body = document.createElement('div');
        body.className = 'body';
        const header = document.createElement('div');
        header.className = 'command-ribbon';
        header.textContent = `🔍 web_search`;
        body.appendChild(header);
        if (entry.query) {
          const queryPre = document.createElement('pre');
          queryPre.textContent = entry.query;
          body.appendChild(queryPre);
        }
        row.appendChild(body);
        getTarget().appendChild(row);
        return;
      }
      const cleanText = entry.role === 'assistant' ? stripCitations(entry.text || '') : (entry.text || '');
      const useMessageCard = entry.role === 'assistant' || entry.role === 'user';
      const { row, body } = useMessageCard
        ? buildMessageCard(entry.role, cleanText)
        : buildRow('message', entry.role === 'assistant' ? 'assistant' : entry.role);
      if (!useMessageCard && entry.role === 'user') row.classList.add('user');
      if ((entry.role === 'assistant' || entry.role === 'user') && isMarkdownEnabled()) {
        const container = renderMarkdownBlock(cleanText);
        body.append(container);
      } else {
        const pre = document.createElement('pre');
        pre.textContent = cleanText;
        body.append(pre);
      }
      getTarget().appendChild(row);
      incrementMessages();
    });
    clearPlaceholder();
    const insertBefore = opts.prepend ? topSpacerEl?.nextSibling : bottomSpacerEl;
    if (insertBefore && insertBefore.parentElement === timelineEl) {
      timelineEl.insertBefore(fragment, insertBefore);
    } else {
      timelineEl.appendChild(fragment);
    }

    // Initialize content after rows are in DOM (xterm needs DOM presence)
    if (pendingAgentPtyTerms.length) {
      requestAnimationFrame(() => {
        pendingAgentPtyTerms.forEach((rec) => {
          if (useXterm) {
            try {
              // Create xterm now that element is in DOM
              if (!rec.term) {
                const lineCount = (rec.buf || '').split('\n').length;
                const rows = Math.min(Math.max(lineCount, 3), 30);
                rec.term = createXterm(rec.termEl, rows);
              }
              if (rec.buf && rec.term && rec.renderMode !== 'screen') {
                const normalized = rec.buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n/g, '\r\n');
                rec.term.write(normalized);
              }
            } catch (e) {
              // Fallback to text
              rec.termEl.textContent = rec.buf || '';
            }
          } else {
            // Text box fallback
            rec.termEl.textContent = rec.buf || '';
          }
        });
      });
    }
    measureRowHeight();
    updateSpacerHeights();
  }

  const { getAssistantRow, appendAssistantDelta, finalizeAssistant } = bindAssistantStream({
    assistantRows,
    buildMessageCard,
    updateMessageCardHeader,
    insertRow,
    isMarkdownEnabled,
    createStreamingParser,
    streamWrite,
    streamEnd,
    highlightCode,
    incrementMessages,
    stripCitations,
    maybeAutoScroll,
  });

  function getReasoningRow(id, parentEl = null) {
    const key = id || 'reasoning';
    let entry = reasoningRows.get(key);
    if (!entry) {
      const { row, body } = createRow('reasoning', 'reasoning', undefined, parentEl);
      const pre = document.createElement('pre');
      pre.textContent = '';
      body.append(pre);
      entry = { row, body, pre };
      reasoningRows.set(key, entry);
    } else if (parentEl && entry.row && entry.row.parentElement !== parentEl) {
      parentEl.appendChild(entry.row);
    }
    return entry;
  }

  function appendReasoningDelta(id, delta, parentEl = null) {
    if (delta === undefined || delta === null) return;
    const entry = getReasoningRow(id, parentEl);
    entry.pre.textContent += delta;
    lastEventType = 'reasoning';
    maybeAutoScroll();
  }

  function finalizeReasoning(id, text, parentEl = null) {
    const entry = getReasoningRow(id, parentEl);
    if (text) entry.pre.textContent = text;
    lastEventType = 'reasoning';
    maybeAutoScroll();
  }

  function getDiffRow(id, path, parentEl) {
    const key = id || 'diff';
    let entry = diffRows.get(key);
    if (!entry) {
      const { row, body } = buildRow('diff', 'diff');
      const pathLabel = document.createElement('div');
      pathLabel.className = 'diff-path-label command-ribbon';
      if (path) {
        pathLabel.innerHTML = `<strong>${escapeHtml(toRelativePath(path))}</strong>`;
        pathLabel.style.cursor = 'pointer';
        pathLabel.dataset.hasClickHandler = 'true';
        pathLabel.addEventListener('click', (e) => {
          if (e.target.closest('.twisty')) return;
          postTe2OpenRequest({ path, line: 1, column: 1 });
        });
      } else {
        pathLabel.innerHTML = '<strong>diff</strong>';
      }
      body.append(pathLabel);
      const pre = document.createElement('pre');
      pre.className = 'diff-block';
      body.append(pre);
      if (parentEl) {
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `diff:${key}`, false);
      entry = { pre, row };
      diffRows.set(key, entry);
    }
    return entry;
  }

  function getToolRow(id, label, parentEl = null) {
    const key = id || `tool:${label || 'tool'}`;
    let entry = toolRows.get(key);
    if (!entry) {
      // Match playback structure: mcp-tool-card with command-ribbon header
      const row = document.createElement('div');
      row.className = 'timeline-row command-result mcp-tool-card';
      const body = document.createElement('div');
      body.className = 'body';
      const header = document.createElement('div');
      header.className = 'command-ribbon';
      header.textContent = label || 'tool';
      body.appendChild(header);
      // Args pre (for arguments)
      const argsPre = document.createElement('pre');
      argsPre.className = 'mcp-tool-args';
      argsPre.textContent = '';
      body.appendChild(argsPre);
      row.appendChild(body);
      if (parentEl) {
        clearPlaceholder();
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `tool:${key}`, false);
      entry = { row, body, argsPre, header, resultEl: null, streamEl: null, interactionEl: null };
      toolRows.set(key, entry);
    } else if (parentEl && entry.row.parentElement !== parentEl) {
      clearPlaceholder();
      parentEl.appendChild(entry.row);
      maybeAutoScroll();
    }
    return entry;
  }

  function renderToolBegin(evt) {
    const toolName = evt.tool || 'tool';
    // Skip command tools - they're redundant with command cards
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const label = serverName ? `${serverName}:${toolName}` : `tool:${toolName}`;
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    // Format arguments
    const args = evt.arguments || evt.payload || {};
    const argEntries = Object.entries(args);
    
    // Check if any argument looks like it should be markdown (multiline string)
    const hasMarkdownArg = argEntries.some(([k, v]) => 
      typeof v === 'string' && (v.includes('\n') || v.startsWith('#') || v.includes('**') || v.includes('`'))
    );
    
    if (hasMarkdownArg) {
      // Render with markdown for multiline/formatted content
      entry.argsPre.style.display = 'none'; // Hide the pre element
      argEntries.forEach(([k, v]) => {
        const argLabel = document.createElement('div');
        argLabel.className = 'mcp-tool-arg-label';
        argLabel.textContent = `${k}:`;
        entry.body.insertBefore(argLabel, entry.argsPre);
        
        if (typeof v === 'string' && (v.includes('\n') || v.startsWith('#') || v.includes('**') || v.includes('`'))) {
          // Render as markdown
          const argContainer = document.createElement('div');
          argContainer.className = 'markdown-body mcp-tool-arg-value';
          renderMarkdownInto(argContainer, v);
          highlightCode(argContainer);
          entry.body.insertBefore(argContainer, entry.argsPre);
        } else {
          // Render as plain value
          const argValue = document.createElement('pre');
          argValue.className = 'mcp-tool-arg-value-plain';
          argValue.textContent = typeof v === 'string' ? v : JSON.stringify(v);
          entry.body.insertBefore(argValue, entry.argsPre);
        }
      });
    } else {
      // Simple key: value format for basic args
      const lines = [];
      argEntries.forEach(([k, v]) => {
        const val = typeof v === 'string' ? v : JSON.stringify(v);
        lines.push(`  ${k}: ${val}`);
      });
      if (lines.length) {
        entry.argsPre.textContent = lines.join('\n');
      }
    }
    lastEventType = 'tool';
  }

  function renderToolDelta(evt) {
    const toolName = evt.tool || 'tool';
    // Skip command tools - they're redundant with command cards
    if (toolName === 'command' || toolName === 'shell') return;
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`, getLiveEventParent(evt));
    const delta = evt.delta || '';
    if (delta) {
      if (!entry.streamEl) {
        const streamPre = document.createElement('pre');
        streamPre.className = 'mcp-tool-content';
        entry.body.appendChild(streamPre);
        entry.streamEl = streamPre;
      }
      entry.streamEl.textContent += delta;
    }
    lastEventType = 'tool';
    maybeAutoScroll();
  }

  function renderToolEnd(evt) {
    const toolName = evt.tool || 'tool';
    // Skip command tools - they're redundant with command cards
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const label = serverName ? `${serverName}:${toolName}` : `tool:${toolName}`;
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    // Handle both old payload format and new result format
    const result = evt.result ?? evt.payload ?? null;
    const durationMs = evt.duration_ms ?? (result && result.duration_ms) ?? (result && result.durationMs);
    const isError = evt.is_error || (result && result.isError) || false;
    
    // Result header
    const resultHeader = document.createElement('div');
    resultHeader.className = 'mcp-tool-result-header';
    resultHeader.textContent = '→';
    entry.body.appendChild(resultHeader);
    
    // Format result
    if (result && typeof result === 'object') {
      // Object result: use pre-formatted display
      const resultPre = document.createElement('pre');
      resultPre.className = 'mcp-tool-content';
      const lines = [];
      Object.entries(result).forEach(([k, v]) => {
        if (typeof v === 'object' && v !== null) {
          lines.push(`  ${k}:`);
          Object.entries(v).forEach(([k2, v2]) => {
            lines.push(`    ${k2}: ${JSON.stringify(v2)}`);
          });
        } else {
          lines.push(`  ${k}: ${JSON.stringify(v)}`);
        }
      });
      resultPre.textContent = lines.join('\n');
      if (isError) resultPre.classList.add('error-text');
      entry.body.appendChild(resultPre);
      entry.resultEl = resultPre;
    } else if (result) {
      // String result: render as markdown
      const resultContainer = document.createElement('div');
      resultContainer.className = 'markdown-body mcp-tool-result';
      renderMarkdownInto(resultContainer, String(result));
      highlightCode(resultContainer);
      if (isError) resultContainer.classList.add('error-text');
      entry.body.appendChild(resultContainer);
      entry.resultEl = resultContainer;
    }
    
    // Duration footer
    if (durationMs !== undefined && durationMs !== null) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `${durationMs}ms`;
      entry.body.appendChild(footer);
    }
    
    lastEventType = 'tool';
    // Update status dot based on error state
    const exitCode = result && (result.exit_code ?? result.exitCode);
    if (!isError && (exitCode === 0 || exitCode === undefined || exitCode === null)) {
      setStatusDot('success');
    } else {
      setStatusDot('error');
    }
  }

  function renderToolInteraction(evt) {
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`, getLiveEventParent(evt));
    const payload = evt.payload || {};
    const stdin = payload.stdin ? `stdin: ${payload.stdin}` : '';
    const stdout = payload.stdout ? `stdout: ${payload.stdout}` : '';
    const pid = payload.pid ? `pid=${payload.pid}` : '';
    const parts = [pid, stdin, stdout].filter(Boolean);
    if (parts.length) {
      if (!entry.interactionEl) {
        const interactionPre = document.createElement('pre');
        interactionPre.className = 'mcp-tool-content';
        entry.body.appendChild(interactionPre);
        entry.interactionEl = interactionPre;
      }
      entry.interactionEl.textContent += `[io] ${parts.join(' ')}\n`;
    }
    lastEventType = 'tool';
  }

  function createXterm(container, rows) {
    if (typeof Terminal === 'undefined') return null;
    const term = new Terminal({
      convertEol: false,
      cursorBlink: false,
      disableStdin: true,
      fontFamily: 'JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
      fontSize: 12,
      rows: rows || 10,
      scrollback: 5000,
      theme: { background: '#000000', foreground: '#c9d1d9' },
    });
    term.open(container);
    return term;
  }

  // --- Agent PTY block streaming (from MCP sidecar) ---
  const agentBlockRows = new Map();

  function getAgentBlockRow(blockId, label) {
    const key = blockId || `agent-block:${label || 'agent'}`;
    let entry = agentBlockRows.get(key);
    if (!entry) {
      clearPlaceholder();
      const row = document.createElement('div');
      row.className = 'timeline-row command-result terminal-card';
      row.dataset.agentBlockId = key;

      const body = document.createElement('div');
      body.className = 'body';

      const cmdRibbon = document.createElement('div');
      cmdRibbon.className = 'command-ribbon';
      cmdRibbon.textContent = label ? `[agent] ${label}` : '[agent]';
      body.appendChild(cmdRibbon);

      const termEl = document.createElement('div');
      termEl.className = 'command-output';
      body.appendChild(termEl);

      row.appendChild(body);
      insertRow(row);
      // Only create xterm if setting enabled
      const term = useXterm ? createXterm(termEl) : null;
      entry = { row, cmdRibbon, term, termEl, text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
      agentBlockRows.set(key, entry);
    }
    return entry;
  }

  function renderAgentBlockBegin(evt) {
    const block = evt.block || {};
    const blockId = evt.block_id || block.block_id || evt.blockId || 'agent';
    const cmd = block.cmd || '';
    const label = cmd ? `$ ${cmd}` : 'agent pty';
    const entry = getAgentBlockRow(blockId, label);
    // Show command in styled ribbon (white on gray), not inside xterm
    entry.cmdRibbon.textContent = cmd ? `$ ${cmd}` : '';
    entry.text = '';
    entry.screenRows = null;
    entry.renderMode = 'raw';
    entry.hasRawStream = Boolean(ptyWebSocket && ptyWebSocket.readyState === WebSocket.OPEN);
    activeAgentPtyBlockId = blockId;
    if (entry.term) {
      entry.term.reset();
    }
    lastEventType = 'shell';
    setActivity('agent pty', true);
    setCommandRunning(true);
    maybeAutoScroll();
  }

  function renderAgentBlockDelta(evt) {
    const blockId = evt.block_id || evt.blockId || 'agent';
    const entry = agentBlockRows.get(blockId) || getAgentBlockRow(blockId, 'agent pty');
    if (entry.renderMode === 'screen' || entry.hasRawStream) return;
    const delta = evt.delta || '';
    if (!delta) return;
    entry.text += delta;
    if (useXterm && entry.term) {
      const normalized = delta.replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\n/g, '\r\n');
      entry.term.write(normalized);
    } else {
      // Fallback: append text if xterm isn't available or disabled
      entry.termEl.textContent = entry.text;
    }
    lastEventType = 'shell';
    maybeAutoScroll();
  }

  function renderScreenDelta(evt) {
    const blockId = evt.block_id || evt.blockId;
    if (!blockId) return;
    const entry = agentBlockRows.get(blockId) || getAgentBlockRow(blockId, 'agent pty');
    if (entry.renderMode !== 'screen') return;
    if (entry.renderMode !== 'screen') {
      entry.renderMode = 'screen';
      entry.text = '';
      entry.buf = '';
      if (entry.term) {
        entry.term.reset();
      }
    }
    const rowCount = Number.isFinite(evt.rows_count) ? evt.rows_count : 40;
    if (!entry.screenRows || entry.screenRows.length !== rowCount) {
      entry.screenRows = new Array(rowCount).fill('');
    }
    const rows = Array.isArray(evt.rows) ? evt.rows : [];
    rows.forEach((r) => {
      if (!r || !Number.isFinite(r.row)) return;
      const idx = r.row;
      if (idx >= 0 && idx < entry.screenRows.length) {
        entry.screenRows[idx] = r.text || '';
      }
    });
    if (useXterm) {
      if (!entry.term) {
        entry.term = createXterm(entry.termEl, rowCount);
      }
      if (entry.term) {
        const content = entry.screenRows.join('\r\n');
        entry.term.write('\x1b[2J\x1b[H' + content);
      }
    } else {
      entry.termEl.textContent = entry.screenRows.join('\n');
    }
    lastEventType = 'shell';
    maybeAutoScroll();
  }

  function _decodeBase64ToUtf8(b64) {
    try {
      const binary = atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
      }
      return new TextDecoder('utf-8').decode(bytes);
    } catch (e) {
      return '';
    }
  }

  function renderAgentPtyRaw(evt) {
    // Raw PTY events are now handled by PTY WebSocket for user terminal
    // Agent transcript should use screen_delta events for clean rendering
    // Skip processing here to avoid duplicate/noisy output
    return;
    
    // Original code kept for reference:
    // const blockId = evt.block_id || evt.blockId;
    // if (!blockId) return;
    // const entry = agentBlockRows.get(blockId) || getAgentBlockRow(blockId, 'agent pty');
    // if (entry.renderMode === 'screen') return;
    // ...
  }

  function renderAgentBlockEnd(evt) {
    const block = evt.block || {};
    const blockId = evt.block_id || block.block_id || evt.blockId || 'agent';
    const entry = agentBlockRows.get(blockId);
    if (!entry) return;
    const exitCode = block.exit_code ?? block.exitCode;
    if (exitCode !== undefined && exitCode !== null && exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      entry.row.querySelector('.body').appendChild(footer);
      setStatusDot('error');
    } else {
      setStatusDot('success');
    }
    setActivity('idle', false);
    setCommandRunning(false);
    lastEventType = 'shell';
    maybeAutoScroll();
    if (activeAgentPtyBlockId === blockId) {
      activeAgentPtyBlockId = null;
    }
    // keep row in map for later deltas (should not happen) but don't delete yet
  }

  // --- Shell streaming functions ---
  // Uses same styling as command-result (renderCommandResult)
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

  // Render a plan card (completed plan from turn) - collapsible
  function renderPlanCard(steps) {
    if (!steps || !steps.length) return;
    
    const { row, body } = createRow('plan', 'plan');
    
    // Collapsible header
    const header = document.createElement('div');
    header.className = 'plan-card-header';
    let collapsed = false;
    
    const toggleBtn = document.createElement('span');
    toggleBtn.className = 'plan-toggle';
    toggleBtn.textContent = '[-]';
    
    const title = document.createElement('span');
    title.className = 'plan-title';
    title.textContent = 'Plan';
    
    header.append(toggleBtn, title);
    body.appendChild(header);
    
    const list = document.createElement('div');
    list.className = 'plan-list';
    
    steps.forEach((item) => {
      const stepEl = document.createElement('div');
      stepEl.className = `plan-item ${item.status || 'pending'}`;
      
      const checkbox = document.createElement('span');
      checkbox.className = 'plan-checkbox';
      if (item.status === 'completed') {
        checkbox.textContent = '☑';
      } else if (item.status === 'in_progress') {
        checkbox.textContent = '◐';
      } else {
        checkbox.textContent = '☐';
      }
      
      const text = document.createElement('span');
      text.className = 'plan-text';
      text.textContent = item.step || '';
      
      stepEl.append(checkbox, text);
      list.appendChild(stepEl);
    });
    
    body.appendChild(list);
    
    // Toggle collapse on header click
    toggleBtn.addEventListener('click', () => {
      collapsed = !collapsed;
      toggleBtn.textContent = collapsed ? '[+]' : '[-]';
      list.style.display = collapsed ? 'none' : 'flex';
    });
    
    // Insert before bottom spacer
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    
    lastEventType = 'plan';
    maybeAutoScroll();
  }

  // Render error card
  function renderErrorCard(message) {
    if (!message) return;
    clearPlaceholder();
    
    const { row, body } = createRow('error', 'error');
    const pre = document.createElement('pre');
    pre.className = 'error-text';
    pre.textContent = message;
    body.appendChild(pre);
    
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    
    lastEventType = 'error';
    maybeAutoScroll();
  }

  // Render warning card
  function renderWarningCard(message) {
    if (!message) return;
    clearPlaceholder();
    
    const { row, body } = createRow('warning', 'warning');
    const pre = document.createElement('pre');
    pre.className = 'warning-text';
    pre.textContent = message;
    body.appendChild(pre);
    
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    
    lastEventType = 'warning';
    maybeAutoScroll();
  }

  function renderContextCompactedCard() {
    clearPlaceholder();
    const { row, body } = createRow('system', 'context compacted');
    const msg = document.createElement('div');
    msg.className = 'system-message';
    msg.textContent = 'Context was compacted to fit within the model\'s context window. Some earlier conversation history may have been summarized or dropped.';
    body.appendChild(msg);
    lastEventType = 'system';
    maybeAutoScroll();
  }

  function renderMetaEnvelopeInjected(evt) {
    const commandCount = evt.command_count ?? evt.commandCount ?? 0;
    const envelopeJson = evt.envelope_json ?? evt.envelopeJson ?? '';
    const pretty = (() => {
      try {
        return JSON.stringify(JSON.parse(envelopeJson), null, 2);
      } catch {
        return String(envelopeJson || '');
      }
    })();
    // Show the exact prefix/suffix the model sees (as escaped literals so it is visible).
    const text = [
      'CODEX_META injected (debug):',
      `commands: ${commandCount}`,
      '',
      '\\u001eCODEX_META ' + pretty + '\\u001f',
    ].join('\n');
    addMessage('meta', text);
  }

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/\"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function ansiToHtml(text) {
    // Minimal ANSI SGR -> HTML span renderer (keeps output selectable inside <pre>).
    const input = String(text || '');
    const sgrRe = /\x1b\[([0-9;]*)m/g;
    let lastIndex = 0;
    let html = '';
    let state = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false };

    function cssFor(st) {
      const styles = [];
      if (st.bold) styles.push('font-weight:600');
      if (st.dim) styles.push('opacity:0.8');
      if (st.italic) styles.push('font-style:italic');
      if (st.underline) styles.push('text-decoration:underline');
      const fgMap = {
        30: '#000000', 31: '#e06c75', 32: '#98c379', 33: '#e5c07b', 34: '#61afef', 35: '#c678dd', 36: '#56b6c2', 37: '#abb2bf',
        90: '#5c6370', 91: '#ff7a85', 92: '#b7f39b', 93: '#ffd68a', 94: '#7ab7ff', 95: '#e79aff', 96: '#7ae8f5', 97: '#ffffff',
      };
      const bgMap = {
        40: '#000000', 41: '#e06c75', 42: '#98c379', 43: '#e5c07b', 44: '#61afef', 45: '#c678dd', 46: '#56b6c2', 47: '#abb2bf',
        100: '#5c6370', 101: '#ff7a85', 102: '#b7f39b', 103: '#ffd68a', 104: '#7ab7ff', 105: '#e79aff', 106: '#7ae8f5', 107: '#ffffff',
      };
      let fg = st.fg;
      let bg = st.bg;
      if (st.inverse) {
        const tmp = fg; fg = bg; bg = tmp;
      }
      if (fg != null && fgMap[fg]) styles.push(`color:${fgMap[fg]}`);
      if (bg != null && bgMap[bg]) styles.push(`background-color:${bgMap[bg]}`);
      return styles.join(';');
    }

    function applyCodes(codes) {
      const parts = codes.length ? codes.split(';') : ['0'];
      for (const p of parts) {
        const n = Number(p || '0');
        if (!Number.isFinite(n)) continue;
        if (n === 0) state = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false };
        else if (n === 1) state.bold = true;
        else if (n === 2) state.dim = true;
        else if (n === 3) state.italic = true;
        else if (n === 4) state.underline = true;
        else if (n === 7) state.inverse = true;
        else if (n === 22) { state.bold = false; state.dim = false; }
        else if (n === 23) state.italic = false;
        else if (n === 24) state.underline = false;
        else if (n === 27) state.inverse = false;
        else if (n === 39) state.fg = null;
        else if (n === 49) state.bg = null;
        else if ((n >= 30 && n <= 37) || (n >= 90 && n <= 97)) state.fg = n;
        else if ((n >= 40 && n <= 47) || (n >= 100 && n <= 107)) state.bg = n;
      }
    }

    function emitChunk(s) {
      if (!s) return;
      const css = cssFor(state);
      const escaped = escapeHtml(s);
      if (css) html += `<span style="${css}">${escaped}</span>`;
      else html += escaped;
    }

    let m;
    while ((m = sgrRe.exec(input)) !== null) {
      emitChunk(input.slice(lastIndex, m.index));
      applyCodes(m[1] || '');
      lastIndex = sgrRe.lastIndex;
    }
    emitChunk(input.slice(lastIndex));
    return html;
  }

  function renderCommandResult(evt) {
    const command = evt.command || '';
    const cwd = evt.cwd || '';
    const prompt = evt.prompt || '';
    const agentBlockId = evt.agent_block_id || evt.agentBlockId || '';
    const output = evt.output || '';
    const exitCode = evt.exit_code;
    const durationMs = evt.duration_ms;
    
    // Get truncation limit from settings (default 20 lines)
    const truncateLines = conversationSettings?.commandOutputLines || 20;
    
    // Truncate output if needed
    let displayOutput = output;
    let truncated = false;
    if (output) {
      const lines = output.split('\n');
      if (lines.length > truncateLines) {
        displayOutput = lines.slice(0, truncateLines).join('\n');
        truncated = true;
      }
    }
    
    // Build the row
    clearPlaceholder();
    // If this command corresponds to an agent PTY block card (noise), remove it.
    if (agentBlockId) {
      try {
        const dup = timelineEl?.querySelector(`.timeline-row.terminal-card[data-agent-block-id="${CSS.escape(agentBlockId)}"]`);
        if (dup && dup.parentElement) dup.parentElement.removeChild(dup);
      } catch (_) {}
    }
    const row = document.createElement('div');
    row.className = 'timeline-row command-result';
    
    // Body column (full width, no meta)
    const body = document.createElement('div');
    body.className = 'body';
    
    // Command ribbon (black background, white text)
    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon';
    // For user terminal commands, include the prompt in the ribbon.
    const isUserTerminal = evt.source === 'user_terminal' || evt.source === 'user-terminal';
    const ribbonText = (prompt ? `${prompt}${command}` : command);
    if (isUserTerminal && typeof ribbonText === 'string' && ribbonText.includes('\x1b[')) {
      cmdRibbon.innerHTML = ansiToHtml(ribbonText);
    } else if (!isUserTerminal) {
      renderShellCmdRibbon(cmdRibbon, command);
    } else {
      cmdRibbon.textContent = ribbonText;
    }
    body.appendChild(cmdRibbon);
    
    // Output block (if any)
    if (displayOutput) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'command-output';
      const hasAnsi = typeof displayOutput === 'string' && displayOutput.includes('\x1b[');
      if (isUserTerminal && hasAnsi) {
        outputPre.innerHTML = ansiToHtml(displayOutput);
        if (truncated) {
          const truncNote = document.createElement('span');
          truncNote.className = 'truncation-note';
          truncNote.textContent = `\n... (truncated, showing ${truncateLines} of ${output.split('\n').length} lines)`;
          outputPre.appendChild(truncNote);
        }
      } else {
        // Try syntax highlighting based on command
        const lang = detectLangFromCommand(command);
        if (lang && typeof hljs !== 'undefined') {
          outputPre.innerHTML = highlightCodeAlways(displayOutput, lang);
          if (truncated) {
            const truncNote = document.createElement('span');
            truncNote.className = 'truncation-note';
            truncNote.textContent = `\n... (truncated, showing ${truncateLines} of ${output.split('\n').length} lines)`;
            outputPre.appendChild(truncNote);
          }
        } else {
          outputPre.textContent = displayOutput;
          if (truncated) {
            outputPre.textContent += `\n... (truncated, showing ${truncateLines} of ${output.split('\n').length} lines)`;
          }
        }
      }
      body.appendChild(outputPre);
    }
    
    // Duration footer
    const footer = document.createElement('div');
    footer.className = 'command-footer';
    const parts = [];
    if (exitCode !== undefined && exitCode !== null && exitCode !== 0) {
      parts.push(`Exit: ${exitCode}`);
    }
    if (durationMs !== undefined && durationMs !== null) {
      parts.push(`Duration: ${durationMs}ms`);
    }
    if (parts.length) {
      footer.textContent = parts.join(' | ');
      body.appendChild(footer);
    }
    
    row.appendChild(body);
    makeCollapsible(row, `cmd:${agentBlockId || command.slice(0, 40)}`, false);

    const parentEl = getLiveEventParent(evt);
    if (parentEl) {
      clearPlaceholder();
      parentEl.appendChild(row);
      maybeAutoScroll();
    } else if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    
    lastEventType = 'command';
    maybeAutoScroll();
    
    // Update status dot based on exit code
    if (exitCode === 0 || exitCode === undefined || exitCode === null) {
      setStatusDot('success');
    } else {
      setStatusDot('error');
    }
  }

  const diffRendering = bindDiffRendering({
    getDiffRow,
    createRow,
    escapeHtml,
    toRelativePath,
    isDiffSyntaxEnabled: () => diffSyntaxHighlight === true,
    setLastEventType: (value) => { lastEventType = value; },
    maybeAutoScroll,
    timelineEl,
    postTe2OpenRequest,
  });

  function addDiff(id, text, path, parentEl) {
    return diffRendering.addDiff(id, text, path, parentEl);
  }

  function addDeclinedDiff(id, text, path) {
    return diffRendering.addDeclinedDiff(id, text, path);
  }

  function formatDiff(text, filePath) {
    return diffRendering.formatDiff(text, filePath);
  }

  const rpcFlow = bindRpcFlow({
    waitForWs: (...args) => waitForWs(...args),
    sioCall,
    getPending: () => pending,
    getConversationId: () => conversationMeta?.conversation_id || null,
    createRow,
    getSubagentContainer,
    escapeHtml,
    formatDiff: (...args) => formatDiff(...args),
    toRelativePath,
  });

  function renderApproval(evt) {
    return rpcFlow.renderApproval(evt);
  }

  function restorePendingApprovals() {
    if (!timelineEl) return;
    timelineEl.querySelectorAll('.timeline-row[data-approval-id]').forEach((row) => row.remove());
    const pending = conversationMeta?.pending_approvals;
    if (!pending || typeof pending !== 'object') return;
    const items = Object.values(pending)
      .filter((entry) => entry && typeof entry === 'object' && (entry.request_id || entry.id))
      .sort((a, b) => String(a?.created_at || a?.render_event?.created_at || '').localeCompare(String(b?.created_at || b?.render_event?.created_at || '')));
    items.forEach((entry) => {
      const requestId = entry.request_id || entry.id;
      if (!requestId) return;
      const liveEvent = entry.render_event && typeof entry.render_event === 'object'
        ? { ...entry.render_event }
        : {
            type: 'approval',
            id: requestId,
            request_id: requestId,
            kind: entry.kind || entry.payload?.kind || 'unknown',
            payload: entry.payload || {},
            turn_id: entry.turn_id || '',
            conversation_id: conversationMeta?.conversation_id || null,
          };
      liveEvent.type = 'approval';
      liveEvent.id = liveEvent.id ?? requestId;
      liveEvent.request_id = liveEvent.request_id ?? requestId;
      liveEvent.kind = liveEvent.kind || entry.kind || liveEvent.payload?.kind || 'unknown';
      liveEvent.payload = (liveEvent.payload && typeof liveEvent.payload === 'object') ? liveEvent.payload : (entry.payload || {});
      liveEvent.turn_id = liveEvent.turn_id || entry.turn_id || '';
      liveEvent.conversation_id = liveEvent.conversation_id || conversationMeta?.conversation_id || null;
      renderApproval(liveEvent);
    });
    timelineStickyHeaders?.update?.();
  }

  async function postJson(url, payload) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload ? JSON.stringify(payload) : '{}',
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const text = await r.text();
    if (!text) return null;
    try { return JSON.parse(text); } catch { return text; }
  }

  const { saveSettings } = bindSettingsSaveFlow({
    getState: () => ({
      conversationSettings,
      conversationMeta,
      pendingNewConversation,
      pendingRollout,
      trackEditsEnabled,
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
      settingsMarkdownEl,
      settingsXtermEl,
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
    setXtermEnabled,
    setDiffSyntaxEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    sioCall,
    closeSettingsModal,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript: (...args) => replayTranscript(...args),
    refreshPlanSurface: (...args) => refreshPlanSurface(...args),
    restorePendingApprovals,
    setDrawerOpen,
    updateConversationHeaderLabel,
  });

  async function sendRpc(method, params, options = {}) {
    return rpcFlow.sendRpc(method, params, options);
  }

  async function respondApproval(requestId, decision) {
    return rpcFlow.respondApproval(requestId, decision);
  }

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
  });

  /**
   * Send a Socket.IO event with ack and HTTP fallback.
   * @param {string} event - SIO event name (e.g. 'send_message')
   * @param {object} data - Payload to send
   * @param {object} [options] - { fallbackUrl, fallbackMethod, timeoutMs }
   * @returns {Promise<any>} Server response (ack value or HTTP JSON)
   */
  async function sioCall(event, data = {}, options = {}) {
    const timeoutMs = options.timeoutMs || 10000;
    // Try Socket.IO first if connected
    if (_socket && _socket.connected) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          reject(new Error(`sioCall timeout: ${event}`));
        }, timeoutMs);
        _socket.emit(event, data, (ack) => {
          clearTimeout(timer);
          if (ack && ack.__error) {
            resolve({ ok: false, error: ack.__error });
          } else {
            resolve(ack);
          }
        });
      });
    }
    // Fallback to HTTP
    if (options.fallbackUrl) {
      const method = (options.fallbackMethod || 'POST').toUpperCase();
      if (method === 'GET') {
        const r = await fetch(options.fallbackUrl, { cache: 'no-store' });
        return r.ok ? await r.json() : { ok: false, error: `HTTP ${r.status}` };
      }
      return postJson(options.fallbackUrl, data);
    }
    // No fallback — wait briefly for socket then retry
    const ready = await waitForWs(3000);
    if (ready && _socket && _socket.connected) {
      return sioCall(event, data, { ...options, fallbackUrl: null });
    }
    return { ok: false, error: 'Socket.IO not connected and no fallback URL' };
  }
	  async function fetchConversation(conversationId = null) {
	    try {
	      const cid = conversationId || clientConversationId;
	      const data = await sioCall('conversation_get', {
	        conversation_id: cid || null,
	      }, {
	        fallbackUrl: cid
	          ? `/api/appserver/conversations/${encodeURIComponent(cid)}/meta`
	          : '/api/appserver/conversation',
	        fallbackMethod: 'GET',
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
        if (activeView !== 'conversation') {
          miniConversationDrawerOpen = false;
        }
      await loadRuntimeOptions(
        conversationSettings?.agent || conversationMeta?.settings?.agent || 'codex',
        conversationMeta?.conversation_id,
      );
      closePlanModal();
      applyAuthoritativePlanState(createEmptyPlanState(Boolean(runtimeOptions?.has_plan), Boolean(runtimeOptions?.has_todo)));
      setDrawerOpen(activeView === 'conversation');
	      applyHostUi();
	      updateActiveConversationLabel();
	      renderFooterApprovalOptions();
      // Sync markdown toggle from settings
      setMarkdownEnabled(conversationSettings?.markdown !== false);
      // Sync track-edits toggle from settings
      setTrackEditsEnabled(conversationSettings?.trackEdits === true);
      // Sync xterm toggle from settings
      setXtermEnabled(conversationSettings?.useXterm !== false);
      // Sync diff syntax toggle from settings
      setDiffSyntaxEnabled(conversationSettings?.diffSyntax === true);
      // Sync semantic shell ribbon toggle from settings (Tree-sitter)
      setSemanticShellRibbonEnabled(conversationSettings?.semanticShellRibbon === true);
      if (conversationSettings?.semanticShellRibbon === true) {
        ensureTreeSitterRibbonReady();
      }
      // If conversation switched, reset composer terminal state so we don't mix streams.
      const convoId = conversationMeta?.conversation_id;
      if (convoId && ptyWebSocketConvoId && ptyWebSocketConvoId !== convoId) {
        composerPrimedConvoId = null;
        if (composerTerm && terminalMode) {
          try { composerTerm.reset(); } catch (_) {}
        }
      }
	      // Connect PTY WebSocket for user terminal
	      connectPtyWebSocket();
	      // Restore draft from conversation meta
	      restoreDraft();
	    } catch {
	      // Don't touch statusEl here - it's for server status only
	    }
	    updateConversationHeaderLabel();
	  }

  async function fetchStatus() {
    try {
      const data = await sioCall('get_status', {}, {
        fallbackUrl: '/api/appserver/status',
        fallbackMethod: 'GET',
      });
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
      terminalMode,
    }),
    setState: (patch) => {
      if (patch.initialized !== undefined) initialized = patch.initialized;
      if (patch.autoScroll !== undefined) autoScroll = patch.autoScroll;
    },
    sioCall,
    waitForWs,
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
    renderCommandResult,
    renderToolBegin,
    renderToolDelta,
    renderToolEnd,
    renderToolInteraction,
    renderAgentBlockBegin,
    renderAgentBlockDelta,
    renderAgentBlockEnd,
    renderScreenDelta,
    renderAgentPtyRaw,
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
  });

  const { connectPtyWebSocket, handleUserPtyOutput } = bindPtyRuntime({
    getState: () => ({
      conversationMeta,
      ptyWebSocket,
      ptyWebSocketConvoId,
      activeAgentPtyBlockId,
      composerTerm,
      composerPriming,
      composerPendingBytes,
      composerPendingChunks,
      useXterm,
    }),
    setState: (patch) => {
      if (patch.ptyWebSocket !== undefined) ptyWebSocket = patch.ptyWebSocket;
      if (patch.ptyWebSocketConvoId !== undefined) ptyWebSocketConvoId = patch.ptyWebSocketConvoId;
      if (patch.composerPendingBytes !== undefined) composerPendingBytes = patch.composerPendingBytes;
    },
    getWindow: () => window,
    createXterm,
    maybeAutoScroll,
    getAgentBlockRows: () => agentBlockRows,
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
    fetchHostUi,
    fetchAppConfig,
    bindPickerFilter,
    setDrawerOpen,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript: (...args) => replayTranscript(...args),
    refreshPlanSurface: (...args) => refreshPlanSurface(...args),
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
        sioCall,
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

  initializeBoot(handleEvent);
  setupSettingsBoot();
  installCodexAgentGlobal();
  bindStartStopButtons();
  initExternalModules();
  bindDropdownClose();
  const { dispatchInput, sendPtyStdin, bindInputHandlers, syncMarkdownFromSettings } = bindInputFlow({
    getState: () => ({
      terminalMode,
      composerTerm,
      commandRunning,
      applyingDraft,
      draftDirty,
      conversationSettings,
      conversationMeta,
      isMobile,
      transcriptLoading,
      transcriptStart,
      topSpacerEl,
      scrollProgrammatic: _scrollProgrammatic,
      autoScroll,
      ptyWebSocket,
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
      footerTerminalToggleEl,
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
      settingsCwdEl,
    },
    sendShellCommand,
    sendUserMessage,
    getPromptText,
    clearPrompt,
    clearDraft,
    setTerminalMode,
    postJson,
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
    refreshPlanSurface: (...args) => refreshPlanSurface(...args),
    postTe2OpenRequest,
    setMarkdownEnabled,
    setTrackEditsEnabled,
    resetTimeline,
    replayTranscript: (...args) => replayTranscript(...args),
    sioCall,
    documentRef: document,
    windowRef: window,
  });

  bindInputHandlers();
});
