import * as smd from "https://cdn.jsdelivr.net/npm/streaming-markdown/smd.min.js";

document.addEventListener('DOMContentLoaded', () => {
  const statusEl = document.getElementById('agent-status');
  const wsStatusEl = document.getElementById('agent-ws');
  const timelineEl = document.getElementById('agent-timeline');
  const timelineWrapEl = timelineEl?.closest('.timeline-wrap');
  const scrollContainer = timelineWrapEl || timelineEl;
  const statusRibbonEl = document.getElementById('status-ribbon');
  const statusLabelEl = document.getElementById('status-label');
  const statusDotEl = document.getElementById('status-dot');
  const startBtn = document.getElementById('agent-start');
  const stopBtn = document.getElementById('agent-stop');
  const promptEl = document.getElementById('agent-prompt');
  const footerEl = document.querySelector('.composer');
  const footerTerminalToggleEl = document.getElementById('footer-terminal-toggle');
  const sendBtn = document.getElementById('agent-send');
  const interruptBtn = document.getElementById('turn-interrupt');
  const counterMessagesEl = document.getElementById('counter-messages');
  const counterTokensEl = document.getElementById('counter-tokens');
  const contextRemainingEl = document.getElementById('context-remaining');
  const scrollBtn = document.getElementById('scroll-pin');
  const activeConversationEl = document.getElementById('active-conversation');
  const splashViewEl = document.getElementById('splash-view');
  const drawerEl = document.getElementById('conversation-drawer');
  const conversationListEl = document.getElementById('conversation-list');
  const conversationCreateBtn = document.getElementById('conversation-create');
  const conversationBackBtn = document.getElementById('conversation-back');
  const conversationSettingsBtn = document.getElementById('conversation-settings');
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
  const settingsLabelEl = document.getElementById('settings-label');
  const settingsCommandLinesEl = document.getElementById('settings-command-lines');
  const settingsMarkdownEl = document.getElementById('settings-markdown');
  const settingsXtermEl = document.getElementById('settings-xterm');
  const settingsDiffSyntaxEl = document.getElementById('settings-diff-syntax');
  const markdownToggleEl = document.getElementById('markdown-toggle');
  const footerApprovalValue = document.getElementById('footer-approval-value');
  const footerApprovalToggle = document.getElementById('footer-approval-toggle');
  const footerApprovalOptions = document.getElementById('footer-approval-options');
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

  localStorage.setItem('last_tab', 'codex-agent');
  const mobileParam = new URLSearchParams(window.location.search).get('mobile');
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
  let activeView = 'splash';
  let currentThreadId = null;
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
  let wsReconnectTimer = null;
  let wsReconnectDelay = 1000;
  let rpcId = 1;
  let modelList = []; // Cached model list with supportedReasoningEfforts
  let markdownEnabled = true; // Toggle for markdown rendering
  let useXterm = true; // Toggle for xterm.js vs text box rendering
  let diffSyntaxHighlight = false; // Toggle for syntax highlighting in diffs
  let commandRunning = false; // Whether a PTY command is currently running
  let ptyWebSocket = null; // Raw PTY WebSocket connection
  let activeAgentPtyBlockId = null;
  let currentAppServerShellId = null;
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
  let normalizeTimer = null;
  let isNormalizing = false;
  let tributeInstance = null;
  let transcriptTotal = 0;
  let planOverlayEl = null;
  let planListEl = null;
  let planCollapsed = false;
  const planItems = new Map();
  let transcriptStart = 0;
  let transcriptEnd = 0;
  let transcriptLimit = 120;
  let transcriptLoading = false;
  let estimatedRowHeight = 28;
  let terminalMode = false;

  function setTerminalMode(enabled) {
    terminalMode = Boolean(enabled);
    document.body.classList.toggle('terminal-mode', terminalMode);
    if (sendBtn) sendBtn.style.display = terminalMode ? 'none' : '';
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

  function isMarkdownEnabled() {
    return markdownEnabled;
  }

  function setMarkdownEnabled(enabled) {
    markdownEnabled = enabled;
    if (markdownToggleEl) markdownToggleEl.checked = enabled;
    if (settingsMarkdownEl) settingsMarkdownEl.checked = enabled;
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
    // Default for shell commands that don't output code
    if (innerCmd.match(/\b(ls|cd|pwd|mkdir|rm|mv|cp|chmod|chown)\b/)) return 'bash';
    return null;
  }

  // Apply syntax highlighting to text if enabled and hljs available
  function highlightCode(text, lang) {
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
    // Try to extract just the filename if path is too long
    const parts = absPath.split('/');
    if (parts.length > 3) {
      return parts.slice(-2).join('/');
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
        .register('/codex-agent/sw.js', { scope: '/codex-agent/' })
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

  function createMentionToken(path) {
    const span = document.createElement('span');
    span.className = 'mention-token';
    span.dataset.path = path;
    const display = String(path || '').split('/').filter(Boolean).pop() || path;
    span.textContent = display;
    span.title = path;
    span.setAttribute('contenteditable', 'false');
    return span;
  }

  function renderPromptFromText(text) {
    if (!promptEl) return;
    promptEl.innerHTML = '';
    const parts = String(text || '').split(/(`[^`]+`)/g);
    parts.forEach((part) => {
      if (!part) return;
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
        const path = part.slice(1, -1);
        promptEl.appendChild(createMentionToken(path));
      } else {
        appendTextWithBreaks(promptEl, part);
      }
    });
  }

  function serializePromptNode(node) {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node;
    if (el.classList.contains('mention-token')) {
      const path = el.dataset.path || el.textContent || '';
      return path ? `\`${path}\`` : '';
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
        const relPath = getRelativePath(item.original.path, cwd);
        return '<span class="mention-token" contenteditable="false" data-path="' + 
               relPath + '" title="' + item.original.path + '">' + 
               item.original.name + '</span> ';
      },
      menuItemTemplate: function(item) {
        const icon = item.original.type === 'directory' ? '📁' : '📄';
        const typeClass = item.original.type === 'directory' ? 'tribute-dir' : 'tribute-file';
        return '<span class="' + typeClass + '">' + icon + ' ' + item.original.name + '</span>';
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
  }

  // Insert mention via button (manual insertion)
  function insertMention(path) {
    if (!promptEl || !path) return;
    const cwd = conversationSettings?.cwd || '';
    const relPath = getRelativePath(path, cwd);
    const display = String(relPath || '').split('/').filter(Boolean).pop() || relPath;
    
    const token = document.createElement('span');
    token.className = 'mention-token';
    token.contentEditable = 'false';
    token.dataset.path = relPath;
    token.title = path;
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

  function updateActiveConversationLabel() {
    if (!activeConversationEl) return;
    activeConversationEl.textContent = '';
  }

  function updateConversationHeaderLabel() {
    const el = document.getElementById('conversation-label');
    if (!el) return;
    const label = conversationSettings?.label || '—';
    el.textContent = label;
  }

  function openSettingsModal() {
    if (!settingsModalEl) return;
    if (pendingNewConversation) {
      if (settingsCwdEl) settingsCwdEl.value = '';
      if (settingsApprovalEl) settingsApprovalEl.value = '';
      if (settingsSandboxEl) settingsSandboxEl.value = '';
      if (settingsModelEl) settingsModelEl.value = '';
      if (settingsEffortEl) settingsEffortEl.value = '';
      if (settingsSummaryEl) settingsSummaryEl.value = '';
      if (settingsLabelEl) settingsLabelEl.value = '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = '20';
      if (settingsMarkdownEl) settingsMarkdownEl.checked = true;
      if (settingsRolloutEl) settingsRolloutEl.value = pendingRollout?.id || '';
    } else {
      if (settingsCwdEl) settingsCwdEl.value = conversationSettings?.cwd || '';
      if (settingsApprovalEl) settingsApprovalEl.value = conversationSettings?.approvalPolicy || '';
      if (settingsSandboxEl) settingsSandboxEl.value = conversationSettings?.sandboxPolicy || '';
      if (settingsModelEl) settingsModelEl.value = conversationSettings?.model || '';
      // Update effort options for the loaded model before setting effort value
      updateEffortOptionsForModel(conversationSettings?.model);
      if (settingsEffortEl) settingsEffortEl.value = conversationSettings?.effort || '';
      if (settingsSummaryEl) settingsSummaryEl.value = conversationSettings?.summary || '';
      if (settingsLabelEl) settingsLabelEl.value = conversationSettings?.label || '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = conversationSettings?.commandOutputLines || '20';
      if (settingsMarkdownEl) settingsMarkdownEl.checked = conversationSettings?.markdown !== false;
      if (settingsXtermEl) settingsXtermEl.checked = conversationSettings?.useXterm !== false;
      if (settingsDiffSyntaxEl) settingsDiffSyntaxEl.checked = conversationSettings?.diffSyntax === true;
      if (settingsRolloutEl) settingsRolloutEl.value = pendingRollout?.id || conversationSettings?.rolloutId || '';
    }
    if (settingsRolloutRowEl) {
      const hasSavedSettings = !pendingNewConversation && conversationMeta?.settings && Object.values(conversationMeta.settings).some((v) => v);
      const allowRollout = !hasSavedSettings;
      settingsRolloutRowEl.style.display = allowRollout ? 'block' : 'none';
    }
    settingsModalEl.classList.remove('hidden');
  }

  function closeSettingsModal() {
    if (!settingsModalEl) return;
    const cwdOk = Boolean(settingsCwdEl?.value?.trim());
    if (!cwdOk) {
      setActivity('CWD required', true);
      return;
    }
    pendingNewConversation = false;
    settingsModalEl.classList.add('hidden');
  }

  function normalizeApprovalValue(value) {
    if (!value) return value;
    if (value === 'unlessTrusted') return 'untrusted';
    return value;
  }

  async function saveApprovalQuick(value) {
    const approval = normalizeApprovalValue(value?.trim());
    if (!approval) return;
    await postJson('/api/appserver/conversation', { settings: { approvalPolicy: approval } });
    conversationSettings.approvalPolicy = approval;
    if (footerApprovalValue) footerApprovalValue.textContent = approval;
  }

  function openPicker(startPath, mode = 'cwd') {
    if (!pickerOverlayEl) return;
    pickerMode = mode || 'cwd';
    if (pickerTitleEl) {
      pickerTitleEl.textContent = pickerMode === 'mention' ? 'Mentioning' : 'Pick CWD';
    }
    pickerPath = startPath || settingsCwdEl?.value || '~';
    pickerOverlayEl.classList.remove('hidden');
    fetchPicker(pickerPath);
    if (pickerFilterEl) {
      pickerFilterEl.value = '';
      setTimeout(() => pickerFilterEl.focus(), 0);
    }
  }

  function closePicker() {
    if (!pickerOverlayEl) return;
    pickerOverlayEl.classList.add('hidden');
    pickerMode = 'cwd';
  }

  function bindPickerFilter() {
    if (!pickerFilterEl) return;
    pickerFilterEl.addEventListener('input', () => {
      if (filterTimer) clearTimeout(filterTimer);
      filterTimer = setTimeout(() => {
        applyPickerFilter();
      }, 150);
    });
  }

  function openRolloutPicker() {
    if (!rolloutOverlayEl) return;
    const cwdOk = Boolean(settingsCwdEl?.value?.trim());
    if (!cwdOk) {
      setActivity('select CWD first', true);
      return;
    }
    rolloutOverlayEl.classList.remove('hidden');
    fetchRollouts();
  }

  function closeRolloutPicker() {
    if (!rolloutOverlayEl) return;
    rolloutOverlayEl.classList.add('hidden');
  }

  function renderRolloutList(items) {
    if (!rolloutListEl) return;
    rolloutListEl.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-item';
      empty.textContent = 'No rollouts found';
      rolloutListEl.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'picker-item rollout-item';
      row.dataset.rolloutId = item?.id || '';
      const idSpan = document.createElement('span');
      idSpan.className = 'rollout-id';
      idSpan.textContent = item?.short_id || item?.id || '';
      const previewSpan = document.createElement('span');
      previewSpan.className = 'rollout-preview';
      previewSpan.textContent = item?.preview || '';
      row.append(idSpan, previewSpan);
      rolloutListEl.appendChild(row);
    });
  }

  async function fetchRollouts() {
    try {
      const r = await fetch('/api/appserver/rollouts', { cache: 'no-store' });
      if (!r.ok) throw new Error('failed to load rollouts');
      const data = await r.json();
      let items = Array.isArray(data?.items) ? data.items : [];
      const cwd = settingsCwdEl?.value?.trim();
      if (cwd) {
        items = items.filter((item) => {
          if (!item || !item.cwd) return false;
          return String(item.cwd) === cwd;
        });
      }
      renderRolloutList(items);
    } catch (err) {
      console.warn('rollout list failed', err);
      renderRolloutList([]);
    }
  }

  async function loadRolloutPreview(rolloutId) {
    if (!rolloutId) return;
    try {
      const r = await fetch(`/api/appserver/rollouts/${encodeURIComponent(rolloutId)}/preview`, { cache: 'no-store' });
      if (!r.ok) throw new Error('failed to load rollout preview');
      const data = await r.json();
      const items = Array.isArray(data?.items) ? data.items : [];
      pendingRollout = { id: rolloutId, items, token_total: data?.token_total ?? null };
      if (settingsRolloutEl) settingsRolloutEl.value = rolloutId;
      closeRolloutPicker();
    } catch (err) {
      console.warn('rollout preview failed', err);
      setActivity('rollout failed', true);
    }
  }

  function buildDropdown(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    listEl.innerHTML = '';
    options.forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.textContent = opt;
      btn.addEventListener('click', () => {
        if (inputEl) inputEl.value = opt;
        closeDropdownMenu(listEl);
        if (typeof onChange === 'function') onChange(opt);
      });
      listEl.appendChild(btn);
    });
  }

  function updateDropdownOptions(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    listEl.innerHTML = '';
    const values = Array.from(new Set(options.filter(Boolean)));
    buildDropdown(listEl, values, inputEl, onChange);
  }

  async function loadModelOptions() {
    try {
      const r = await fetch('/api/appserver/models', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const items = data?.result?.data || data?.result?.models || data?.data || data?.result || [];
      if (Array.isArray(items)) {
        modelList = items.filter(m => m && typeof m === 'object' && m.id);
        const names = modelList.map(m => m.id);
        if (names.length) {
          updateDropdownOptions(settingsModelOptions, names, settingsModelEl, updateEffortOptionsForModel);
        }
        // Update effort options for currently selected model
        updateEffortOptionsForModel(settingsModelEl?.value);
      }
    } catch {
      // ignore
    }
  }

  function updateEffortOptionsForModel(modelId) {
    if (!modelId || !modelList.length) return;
    const model = modelList.find(m => m.id === modelId);
    if (!model || !Array.isArray(model.supportedReasoningEfforts)) {
      // Fallback to default options if model not found
      updateDropdownOptions(settingsEffortOptions, ['low', 'medium', 'high'], settingsEffortEl);
      return;
    }
    const efforts = model.supportedReasoningEfforts.map(e => e.reasoningEffort).filter(Boolean);
    if (efforts.length) {
      updateDropdownOptions(settingsEffortOptions, efforts, settingsEffortEl);
      // If current effort is not supported, clear it or set to default
      const currentEffort = settingsEffortEl?.value;
      if (currentEffort && !efforts.includes(currentEffort)) {
        if (settingsEffortEl) {
          settingsEffortEl.value = model.defaultReasoningEffort || efforts[0] || '';
        }
      }
    }
  }

  function openDropdownMenu(listEl) {
    if (!listEl) return;
    if (openDropdownEl && openDropdownEl !== listEl) {
      closeDropdownMenu(openDropdownEl);
    }
    listEl.classList.add('open');
    openDropdownEl = listEl;
  }

  function closeDropdownMenu(listEl) {
    if (!listEl) return;
    listEl.classList.remove('open');
    if (openDropdownEl === listEl) openDropdownEl = null;
  }

  function toggleDropdownMenu(listEl) {
    if (!listEl) return;
    if (listEl.classList.contains('open')) {
      closeDropdownMenu(listEl);
    } else {
      openDropdownMenu(listEl);
    }
  }

  function setupDropdown(inputEl, toggleEl, listEl, options) {
    if (!listEl || !inputEl) return;
    buildDropdown(listEl, options, inputEl);
    toggleEl?.addEventListener('click', (evt) => {
      evt.preventDefault();
      toggleDropdownMenu(listEl);
    });
  }

  async function fetchPicker(path) {
    try {
      const url = `/api/fs/list?path=${encodeURIComponent(path || '~')}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      pickerPath = data?.path || path || '~';
      pickerItems = Array.isArray(data?.items) ? data.items : [];
      if (pickerPathEl) pickerPathEl.textContent = pickerPath;
      applyPickerFilter();
    } catch {
      // ignore
    }
  }

  async function fetchPickerSearch(query) {
    try {
      const root = conversationSettings?.cwd || settingsCwdEl?.value || pickerPath || '~';
      const url = `/api/fs/search?query=${encodeURIComponent(query)}&root=${encodeURIComponent(root)}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return [];
      const data = await r.json();
      return Array.isArray(data?.items) ? data.items : [];
    } catch {
      return [];
    }
  }

  function applyPickerFilter() {
    if (!pickerFilterEl) {
      renderPickerList(pickerItems || []);
      return;
    }
    const raw = pickerFilterEl.value || '';
    if (!raw.trim()) {
      renderPickerList(pickerItems || []);
      return;
    }
    if (pickerMode === 'mention') {
      fetchPickerSearch(raw).then(renderPickerList);
      return;
    }
    let regex = null;
    try {
      regex = new RegExp(raw, 'i');
    } catch {
      renderPickerList([]);
      return;
    }
    const items = (pickerItems || []).filter((item) => {
      const target = `${item?.name || ''} ${item?.path || ''}`;
      return regex.test(target);
    });
    renderPickerList(items);
  }

  function renderPickerList(items) {
    if (!pickerListEl) return;
    pickerListEl.innerHTML = '';
    items.forEach((item) => {
      if (!item) return;
      const row = document.createElement('div');
      row.className = 'picker-item';
      const icon = document.createElement('span');
      icon.textContent = item.type === 'directory' ? '📁' : '📄';
      const name = document.createElement('span');
      name.textContent = item.name || item.path;
      row.append(icon, name);
      row.addEventListener('click', () => {
        if (item.type === 'directory') {
          fetchPicker(item.path);
          return;
        }
        if (pickerMode === 'mention') {
          insertMention(item.path || item.name || '');
          closePicker();
        }
      });
      pickerListEl.appendChild(row);
    });
  }

  function renderConversationList(items, activeId) {
    if (!conversationListEl) return;
    conversationListEl.innerHTML = '';
    if (!items || !items.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'No conversations yet.';
      conversationListEl.appendChild(empty);
      return;
    }
    items.forEach((meta) => {
      if (!meta) return;
      const row = document.createElement('div');
      row.className = 'conversation-row';
      if (meta.conversation_id && meta.conversation_id === activeId) {
        row.classList.add('active');
      }
      const info = document.createElement('div');
      info.className = 'conversation-meta';
      const labelRow = document.createElement('div');
      labelRow.className = 'conversation-label-line';
      labelRow.textContent = (meta.settings && meta.settings.label) ? meta.settings.label : '';
      const title = document.createElement('div');
      title.textContent = meta.conversation_id || 'conversation';
      const threadText = meta.thread_id ? `thread: ${meta.thread_id}` : 'thread: (none)';
      const cwdText = meta.settings && meta.settings.cwd ? `cwd: ${meta.settings.cwd}` : 'cwd: (default)';
      const statusText = meta.status ? `status: ${meta.status}` : 'status: none';
      const threadRow = document.createElement('div');
      threadRow.textContent = threadText;
      const cwdRow = document.createElement('div');
      cwdRow.textContent = cwdText;
      const statusRow = document.createElement('div');
      statusRow.textContent = statusText;
      info.append(labelRow, title, threadRow, cwdRow, statusRow);

      const actions = document.createElement('div');
      actions.className = 'conversation-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'btn tiny primary';
      openBtn.textContent = 'Open';
      openBtn.addEventListener('click', () => selectConversation(meta.conversation_id));
      const settingsBtn = document.createElement('button');
      settingsBtn.className = 'btn tiny';
      settingsBtn.textContent = 'Settings';
      settingsBtn.addEventListener('click', async () => {
        await selectConversationWithView(meta.conversation_id, 'splash');
        openSettingsModal();
      });
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'btn tiny decline';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', () => {
        if (window.CodexAgent?.helpers?.openWarningModal) {
          window.CodexAgent.helpers.openWarningModal({
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

  function isNearBottom() {
    if (!scrollContainer) return true;
    const distance = scrollContainer.scrollHeight - scrollContainer.scrollTop - scrollContainer.clientHeight;
    return distance <= 24;
  }

  function maybeAutoScroll(force) {
    if (!scrollContainer) return;
    if (autoScroll || force) {
      scrollContainer.scrollTop = scrollContainer.scrollHeight;
    }
  }

  function updateScrollButton() {
    if (!scrollBtn) return;
    scrollBtn.textContent = autoScroll ? 'Pinned' : 'Free';
    scrollBtn.classList.toggle('active', autoScroll);
  }

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

  function createRow(kind, title, beforeEl) {
    const { row, body } = buildRow(kind, title);
    insertRow(row, beforeEl);
    return { row, body };
  }

  function setActivity(label, active) {
    // Update status ribbon instead of activity row
    if (statusLabelEl) statusLabelEl.textContent = label || 'idle';
    if (statusRibbonEl) statusRibbonEl.classList.toggle('active', Boolean(active));
  }

  function setStatusDot(status) {
    // status: 'success', 'error', 'warning', or null/'' for neutral
    if (!statusDotEl) return;
    statusDotEl.classList.remove('success', 'error', 'warning');
    if (status) statusDotEl.classList.add(status);
  }

  // Plan overlay (todo list) functions
  function ensurePlanOverlay() {
    if (planOverlayEl) return;
    if (!timelineEl) return;
    
    planOverlayEl = document.createElement('div');
    planOverlayEl.className = 'plan-overlay';
    planOverlayEl.style.display = 'none';
    
    const header = document.createElement('div');
    header.className = 'plan-header';
    
    const toggleBtn = document.createElement('span');
    toggleBtn.className = 'plan-toggle';
    toggleBtn.textContent = '[-]';
    toggleBtn.addEventListener('click', () => {
      planCollapsed = !planCollapsed;
      toggleBtn.textContent = planCollapsed ? '[+]' : '[-]';
      if (planListEl) planListEl.style.display = planCollapsed ? 'none' : 'block';
    });
    
    const title = document.createElement('span');
    title.className = 'plan-title';
    title.textContent = 'Plan';
    
    header.append(toggleBtn, title);
    
    planListEl = document.createElement('div');
    planListEl.className = 'plan-list';
    
    planOverlayEl.append(header, planListEl);
    
    // Insert at top of timeline (after spacer if present)
    if (topSpacerEl && topSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(planOverlayEl, topSpacerEl.nextSibling);
    } else {
      timelineEl.prepend(planOverlayEl);
    }
  }

  function updatePlanItem(step, status) {
    ensurePlanOverlay();
    if (!planListEl) return;
    
    let itemEl = planItems.get(step);
    if (!itemEl) {
      itemEl = document.createElement('div');
      itemEl.className = 'plan-item';
      
      const checkbox = document.createElement('span');
      checkbox.className = 'plan-checkbox';
      
      const text = document.createElement('span');
      text.className = 'plan-text';
      text.textContent = step;
      
      itemEl.append(checkbox, text);
      itemEl._checkbox = checkbox;
      planListEl.appendChild(itemEl);
      planItems.set(step, itemEl);
    }
    
    // Update status
    itemEl.classList.remove('pending', 'in_progress', 'completed');
    itemEl.classList.add(status || 'pending');
    
    const checkbox = itemEl._checkbox;
    if (checkbox) {
      if (status === 'completed') {
        checkbox.textContent = '☑';
      } else if (status === 'in_progress') {
        checkbox.textContent = '◐';
      } else {
        checkbox.textContent = '☐';
      }
    }
    
    // Show overlay
    if (planOverlayEl) planOverlayEl.style.display = 'block';
  }

  function clearPlanOverlay() {
    planItems.clear();
    if (planListEl) planListEl.innerHTML = '';
    if (planOverlayEl) planOverlayEl.style.display = 'none';
  }

  function finalizePlanToTranscript() {
    // Store completed plan to transcript if there are items
    if (planItems.size === 0) return;
    const items = [];
    planItems.forEach((el, step) => {
      const status = el.classList.contains('completed') ? 'completed' :
                     el.classList.contains('in_progress') ? 'in_progress' : 'pending';
      items.push({ step, status });
    });
    // Could POST to backend here if needed
    clearPlanOverlay();
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
    // Debug: log values to console
    console.log('updateContextRemaining:', { total, windowSize, pct: Math.round((total / windowSize) * 100) });
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
  }

  async function requestContextCompact() {
    try {
      await postJson('/api/appserver/compact', null);
      setActivity('compact requested', true);
    } catch (err) {
      console.warn('compact failed', err);
      setActivity('compact failed', true);
    }
  }

  function addMessage(role, text) {
    const label = role === 'assistant' ? 'assistant' : role;
    const { row, body } = buildRow('message', label);
    if (role === 'user') row.classList.add('user');
    insertRow(row);
    if ((role === 'assistant' || role === 'user') && isMarkdownEnabled()) {
      const container = document.createElement('div');
      container.className = 'markdown-body';
      const renderer = smd.default_renderer(container);
      const parser = smd.parser(renderer);
      smd.parser_write(parser, role === 'assistant' ? stripCitations(text || '') : (text || ''));
      smd.parser_end(parser);
      container.querySelectorAll('pre code').forEach((block) => {
        if (typeof hljs !== 'undefined') {
          hljs.highlightElement(block);
        }
      });
      body.append(container);
    } else {
      const pre = document.createElement('pre');
      pre.textContent = text || '';
      body.append(pre);
    }
    incrementMessages();
    lastEventType = 'message';
  }

  function updateSpacerHeights() {
    if (!topSpacerEl || !bottomSpacerEl) return;
    const above = Math.max(0, transcriptStart);
    const below = Math.max(0, transcriptTotal - transcriptEnd);
    topSpacerEl.style.height = `${Math.max(0, above * estimatedRowHeight)}px`;
    bottomSpacerEl.style.height = `${Math.max(0, below * estimatedRowHeight)}px`;
  }

  function measureRowHeight() {
    const rows = Array.from(timelineEl.querySelectorAll('.timeline-row'))
      .filter((row) => !row.classList.contains('activity') && !row.classList.contains('muted'));
    if (!rows.length) return;
    const total = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    if (total > 0) {
      estimatedRowHeight = total / rows.length;
    }
  }

  function renderTranscriptEntries(items, opts = {}) {
    if (!items || !items.length || !timelineEl) return;
    const fragment = document.createDocumentFragment();
    const truncateLines = conversationSettings?.commandOutputLines || 20;
    const pendingAgentPtyTerms = [];
    const agentPtyByBlock = new Map(); // blockId -> { row, termEl, cmd, buf }
    items.forEach((entry) => {
      if (!entry || !entry.role) return;
      if (entry.role === 'reasoning') {
        const { row, body } = buildRow('reasoning', 'reasoning');
        const pre = document.createElement('pre');
        pre.textContent = entry.text || '';
        body.append(pre);
        fragment.appendChild(row);
        return;
      }
      if (entry.role === 'diff') {
        const { row, body } = buildRow('diff', 'diff');
        // Show file path if available
        if (entry.path) {
          const pathDiv = document.createElement('div');
          pathDiv.className = 'diff-path';
          pathDiv.textContent = toRelativePath(entry.path);
          body.append(pathDiv);
        }
        const pre = document.createElement('pre');
        pre.className = 'diff-block';
        pre.innerHTML = formatDiff(entry.text || '', entry.path);
        body.append(pre);
        fragment.appendChild(row);
        return;
      }
      if (entry.role === 'command') {
        const row = document.createElement('div');
        row.className = 'timeline-row command-result';
        const body = document.createElement('div');
        body.className = 'body';
        // Command ribbon
        const cmdRibbon = document.createElement('div');
        cmdRibbon.className = 'command-ribbon';
        // Just show the command, skip cwd line (redundant)
        cmdRibbon.textContent = entry.command || '';
        body.appendChild(cmdRibbon);
        // Output
        if (entry.output) {
          const lines = entry.output.split('\n');
          let displayOutput = entry.output;
          let truncated = false;
          if (lines.length > truncateLines) {
            displayOutput = lines.slice(0, truncateLines).join('\n');
            truncated = true;
          }
          const outputPre = document.createElement('pre');
          outputPre.className = 'command-output';
          // Try syntax highlighting based on command
          const lang = detectLangFromCommand(entry.command);
          if (lang && isDiffSyntaxEnabled()) {
            outputPre.innerHTML = highlightCode(displayOutput, lang);
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
        fragment.appendChild(row);
        return;
      }
      // Plan entries (completed plan from turn) - collapsible
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
        
        fragment.appendChild(row);
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
        cmdRibbon.textContent = `$ ${entry.command || ''}`;
        body.appendChild(cmdRibbon);
        
        // Output
        const pre = document.createElement('pre');
        pre.className = 'command-output';
        if (entry.stdout) {
          pre.appendChild(document.createTextNode(entry.stdout));
        }
        if (entry.stderr) {
          const stderrEl = document.createElement('span');
          stderrEl.className = 'shell-stderr';
          stderrEl.textContent = entry.stderr;
          pre.appendChild(stderrEl);
        }
        if (!entry.stdout && !entry.stderr) {
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
        fragment.appendChild(row);
        
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
          fragment.appendChild(row);
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
            fragment.appendChild(row);
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
        fragment.appendChild(row);
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
        // Content area
        const content = document.createElement('pre');
        content.className = 'mcp-tool-content';
        const lines = [];
        // Format arguments as key: value
        if (entry.arguments && Object.keys(entry.arguments).length > 0) {
          Object.entries(entry.arguments).forEach(([k, v]) => {
            const val = typeof v === 'string' ? v : JSON.stringify(v);
            lines.push(`  ${k}: ${val}`);
          });
        }
        // Format result
        if (entry.result !== undefined && entry.result !== null) {
          lines.push('→');
          if (typeof entry.result === 'object') {
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
          } else {
            lines.push(`  ${entry.result}`);
          }
        }
        content.textContent = lines.join('\n');
        if (entry.is_error) content.classList.add('error-text');
        body.appendChild(content);
        // Footer with duration
        if (entry.duration_ms !== undefined && entry.duration_ms !== null) {
          const footer = document.createElement('div');
          footer.className = 'command-footer';
          footer.textContent = `${entry.duration_ms}ms`;
          body.appendChild(footer);
        }
        row.appendChild(body);
        fragment.appendChild(row);
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
        fragment.appendChild(row);
        return;
      }
      const label = entry.role === 'assistant' ? 'assistant' : entry.role;
      const { row, body } = buildRow('message', label);
      if (entry.role === 'user') row.classList.add('user');
      if ((entry.role === 'assistant' || entry.role === 'user') && isMarkdownEnabled()) {
        const container = document.createElement('div');
        container.className = 'markdown-body';
        const renderer = smd.default_renderer(container);
        const parser = smd.parser(renderer);
        smd.parser_write(parser, entry.role === 'assistant' ? stripCitations(entry.text || '') : (entry.text || ''));
        smd.parser_end(parser);
        // Highlight code blocks
        container.querySelectorAll('pre code').forEach((block) => {
          if (typeof hljs !== 'undefined') {
            hljs.highlightElement(block);
          }
        });
        body.append(container);
      } else {
        const pre = document.createElement('pre');
        pre.textContent = entry.role === 'assistant' ? stripCitations(entry.text || '') : (entry.text || '');
        body.append(pre);
      }
      fragment.appendChild(row);
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

  function getAssistantRow(id) {
    const key = id || 'assistant';
    let entry = assistantRows.get(key);
    if (!entry) {
      const { body } = createRow('message', 'assistant');
      const container = document.createElement('div');
      container.className = 'markdown-body';
      body.append(container);
      if (isMarkdownEnabled()) {
        // Create streaming markdown parser with default renderer
        const renderer = smd.default_renderer(container);
        const parser = smd.parser(renderer);
        entry = { container, parser, useMarkdown: true, counted: false };
      } else {
        // Plain text mode - use pre element
        const pre = document.createElement('pre');
        container.append(pre);
        entry = { container, pre, useMarkdown: false, counted: false };
      }
      assistantRows.set(key, entry);
    }
    return entry;
  }

  function appendAssistantDelta(id, delta) {
    if (!delta) return;
    const entry = getAssistantRow(id);
    const cleanDelta = stripCitations(delta);
    if (entry.useMarkdown && entry.parser) {
      smd.parser_write(entry.parser, cleanDelta);
    } else if (entry.pre) {
      entry.pre.textContent += cleanDelta;
    }
    maybeAutoScroll();
  }

  function finalizeAssistant(id, text) {
    const key = id || 'assistant';
    const entry = assistantRows.get(key);
    if (!entry) return;
    if (entry.useMarkdown && entry.parser) {
      // End the streaming parser
      smd.parser_end(entry.parser);
      // Highlight code blocks after parsing is complete
      entry.container.querySelectorAll('pre code').forEach((block) => {
        if (typeof hljs !== 'undefined') {
          hljs.highlightElement(block);
        }
      });
    }
    if (!entry.counted) {
      incrementMessages();
      entry.counted = true;
    }
  }

  function getReasoningRow(id) {
    const key = id || 'reasoning';
    let entry = reasoningRows.get(key);
    if (!entry) {
      const { body } = createRow('reasoning', 'reasoning');
      const pre = document.createElement('pre');
      pre.textContent = '';
      body.append(pre);
      entry = { pre };
      reasoningRows.set(key, entry);
    }
    return entry;
  }

  function appendReasoningDelta(id, delta) {
    if (delta === undefined || delta === null) return;
    const entry = getReasoningRow(id);
    entry.pre.textContent += delta;
    lastEventType = 'reasoning';
    maybeAutoScroll();
  }

  function finalizeReasoning(id, text) {
    const key = id || 'reasoning';
    const entry = reasoningRows.get(key);
    if (!entry) return;
    if (text) entry.pre.textContent = text;
  }

  function getDiffRow(id, path) {
    const key = id || 'diff';
    let entry = diffRows.get(key);
    if (!entry) {
      const { body } = createRow('diff', 'diff');
      if (path) {
        const pathLabel = document.createElement('div');
        pathLabel.className = 'diff-path-label';
        pathLabel.innerHTML = `<strong>${escapeHtml(toRelativePath(path))}</strong>`;
        body.append(pathLabel);
      }
      const pre = document.createElement('pre');
      pre.className = 'diff-block';
      body.append(pre);
      entry = { pre };
      diffRows.set(key, entry);
    }
    return entry;
  }

  function getToolRow(id, label) {
    const key = id || `tool:${label || 'tool'}`;
    let entry = toolRows.get(key);
    if (!entry) {
      const { body } = createRow('tool', label || 'tool');
      const pre = document.createElement('pre');
      pre.className = 'tool-block';
      pre.textContent = '';
      body.append(pre);
      entry = { pre };
      toolRows.set(key, entry);
    }
    return entry;
  }

  function renderToolBegin(evt) {
    const toolName = evt.tool || 'tool';
    // Skip command tools - they're redundant with command cards
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const label = serverName ? `${serverName}:${toolName}` : `tool:${toolName}`;
    const entry = getToolRow(evt.id, label);
    // Format arguments as indented key: value
    const args = evt.arguments || evt.payload || {};
    const lines = [];
    Object.entries(args).forEach(([k, v]) => {
      const val = typeof v === 'string' ? v : JSON.stringify(v);
      lines.push(`  ${k}: ${val}`);
    });
    if (lines.length) {
      entry.pre.textContent += lines.join('\n') + '\n';
    }
    lastEventType = 'tool';
  }

  function renderToolDelta(evt) {
    const toolName = evt.tool || 'tool';
    // Skip command tools - they're redundant with command cards
    if (toolName === 'command' || toolName === 'shell') return;
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`);
    const delta = evt.delta || '';
    if (delta) {
      entry.pre.textContent += delta;
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
    const entry = getToolRow(evt.id, label);
    // Handle both old payload format and new result format
    const result = evt.result || evt.payload || {};
    const durationMs = evt.duration_ms ?? result.duration_ms ?? result.durationMs;
    const isError = evt.is_error || result.isError || false;
    
    // Format result as indented key: value
    entry.pre.textContent += '→\n';
    if (result && typeof result === 'object') {
      Object.entries(result).forEach(([k, v]) => {
        if (typeof v === 'object' && v !== null) {
          entry.pre.textContent += `  ${k}:\n`;
          Object.entries(v).forEach(([k2, v2]) => {
            entry.pre.textContent += `    ${k2}: ${JSON.stringify(v2)}\n`;
          });
        } else {
          entry.pre.textContent += `  ${k}: ${JSON.stringify(v)}\n`;
        }
      });
    }
    
    // Duration footer
    if (durationMs !== undefined && durationMs !== null) {
      entry.pre.textContent += `${durationMs}ms\n`;
    }
    
    lastEventType = 'tool';
    // Update status dot based on error state
    const exitCode = result.exit_code ?? result.exitCode;
    if (!isError && (exitCode === 0 || exitCode === undefined || exitCode === null)) {
      setStatusDot('success');
    } else {
      setStatusDot('error');
    }
  }

  function renderToolInteraction(evt) {
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`);
    const payload = evt.payload || {};
    const stdin = payload.stdin ? `stdin: ${payload.stdin}` : '';
    const stdout = payload.stdout ? `stdout: ${payload.stdout}` : '';
    const pid = payload.pid ? `pid=${payload.pid}` : '';
    const parts = [pid, stdin, stdout].filter(Boolean);
    if (parts.length) {
      entry.pre.textContent += `[io] ${parts.join(' ')}\n`;
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
  function getShellRow(id) {
    let entry = shellRows.get(id);
    if (!entry) {
      clearPlaceholder();
      const row = document.createElement('div');
      row.className = 'timeline-row command-result terminal-card';
      row.dataset.shellId = id;
      
      const body = document.createElement('div');
      body.className = 'body';
      
      // Command ribbon (same as renderCommandResult)
      const cmdRibbon = document.createElement('div');
      cmdRibbon.className = 'command-ribbon';
      cmdRibbon.textContent = '$ ...';
      body.appendChild(cmdRibbon);
      
      // Output area (xterm container for streaming)
      const termEl = document.createElement('div');
      termEl.className = 'command-output';
      body.appendChild(termEl);
      
      row.appendChild(body);
      insertRow(row);
      
      const term = createXterm(termEl);
      entry = { row, cmdRibbon, term, termEl, text: '' };
      shellRows.set(id, entry);
    }
    return entry;
  }

  function renderShellBegin(evt) {
    const entry = getShellRow(evt.id);
    // Just show the command, skip cwd line (redundant)
    entry.cmdRibbon.textContent = `$ ${evt.command || ''}`;
    entry.text = '';
    if (entry.term) {
      entry.term.reset();
      entry.term.writeln(`$ ${evt.command || ''}`);
    }
    lastEventType = 'shell';
    setActivity('executing', true);
    maybeAutoScroll();
  }

  function renderShellDelta(evt) {
    const entry = shellRows.get(evt.id);
    if (!entry) return;
    const delta = evt.delta || '';
    if (delta) {
      entry.text += delta;
      if (entry.term) {
        entry.term.write(delta.replace(/\n/g, '\r\n'));
      } else {
        entry.termEl.textContent = entry.text;
      }
    }
    lastEventType = 'shell';
    maybeAutoScroll();
  }

  function renderShellEnd(evt) {
    const entry = shellRows.get(evt.id);
    if (!entry) {
      // No streaming happened, render batch result
      renderShellBatchResult(evt);
      return;
    }
    
    const exitCode = evt.exitCode ?? 0;
    
    // If no streaming output was received, show batch result
    if (!entry.text && (evt.stdout || evt.stderr)) {
      entry.text = String(evt.stdout || '') + String(evt.stderr || '');
      if (entry.term) {
        entry.term.reset();
        if (evt.command) entry.term.writeln(`$ ${evt.command}`);
        entry.term.write(entry.text.replace(/\n/g, '\r\n'));
      } else {
        entry.termEl.textContent = entry.text;
      }
    }
    
    // Add footer with exit code (same as renderCommandResult)
    if (exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      entry.row.querySelector('.body').appendChild(footer);
    }
    
    // Update status
    setStatusDot(exitCode === 0 ? 'success' : 'error');
    setActivity('idle', false);
    lastEventType = 'shell';
    maybeAutoScroll();
    
    // Clean up tracking
    shellRows.delete(evt.id);
  }

  function renderShellBatchResult(evt) {
    // Fallback - use same structure as renderCommandResult
    clearPlaceholder();
    const row = document.createElement('div');
    row.className = 'timeline-row command-result';
    
    const body = document.createElement('div');
    body.className = 'body';
    
    // Command ribbon
    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon';
    cmdRibbon.textContent = `$ ${evt.command || '(shell)'}`;
    body.appendChild(cmdRibbon);
    
    // Output
    const pre = document.createElement('pre');
    pre.className = 'command-output';
    if (evt.stdout) {
      pre.appendChild(document.createTextNode(evt.stdout));
    }
    if (evt.stderr) {
      const span = document.createElement('span');
      span.className = 'shell-stderr';
      span.textContent = evt.stderr;
      pre.appendChild(span);
    }
    if (!evt.stdout && !evt.stderr) {
      pre.textContent = '(no output)';
    }
    body.appendChild(pre);
    
    // Footer with exit code
    const exitCode = evt.exitCode ?? 0;
    if (exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      body.appendChild(footer);
    }
    
    row.appendChild(body);
    insertRow(row);
    
    setStatusDot(exitCode === 0 ? 'success' : 'error');
    setActivity('idle', false);
  }

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

  function renderCommandResult(evt) {
    const command = evt.command || '';
    const cwd = evt.cwd || '';
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
    const row = document.createElement('div');
    row.className = 'timeline-row command-result';
    
    // Body column (full width, no meta)
    const body = document.createElement('div');
    body.className = 'body';
    
    // Command ribbon (black background, white text)
    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon';
    // Just show the command, skip cwd line (redundant)
    cmdRibbon.textContent = command;
    body.appendChild(cmdRibbon);
    
    // Output block (if any)
    if (displayOutput) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'command-output';
      // Try syntax highlighting based on command
      const lang = detectLangFromCommand(command);
      if (lang && isDiffSyntaxEnabled()) {
        outputPre.innerHTML = highlightCode(displayOutput, lang);
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
    
    // Insert before bottom spacer
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
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

  function addDiff(id, text, path) {
    const entry = getDiffRow(id, path);
    entry.pre.innerHTML = formatDiff(text || '', path);
    lastEventType = 'diff';
    maybeAutoScroll();
  }

  function addDeclinedDiff(id, text, path) {
    const { row, body } = createRow('diff', 'diff-declined');
    row.classList.add('declined');
    if (path) {
      const pathLabel = document.createElement('div');
      pathLabel.className = 'declined-label';
      pathLabel.innerHTML = `<strong>DECLINED:</strong> ${escapeHtml(toRelativePath(path))}`;
      body.appendChild(pathLabel);
    }
    const pre = document.createElement('pre');
    pre.className = 'diff-block';
    pre.innerHTML = formatDiff(text || '', path);
    body.appendChild(pre);
    lastEventType = 'diff';
    maybeAutoScroll();
  }

  function formatDiff(text, filePath) {
    if (!text) return '';
    
    // First pass: compute max line numbers for proper padding
    let oldLine = 0;
    let newLine = 0;
    let maxOldLen = 1;
    let maxNewLen = 1;
    
    text.split('\n').forEach((line) => {
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
        if (match) {
          oldLine = parseInt(match[1], 10);
          const oldCount = parseInt(match[2] || '1', 10);
          newLine = parseInt(match[3], 10);
          const newCount = parseInt(match[4] || '1', 10);
          // Max possible line numbers in this hunk
          maxOldLen = Math.max(maxOldLen, String(oldLine + oldCount).length);
          maxNewLen = Math.max(maxNewLen, String(newLine + newCount).length);
        }
      }
    });
    
    // Reset for second pass
    oldLine = 0;
    newLine = 0;
    
    // Second pass: render lines
    return text.split('\n').map((line) => {
      let cls = 'diff-context';
      let display = line;
      let changeMarker = ' ';
      let oldNo = '';
      let newNo = '';

      if (line.startsWith('@@')) {
        cls = 'diff-hunk';
        const match = line.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)/);
        if (match) {
          const oldStart = parseInt(match[1], 10);
          const oldCount = parseInt(match[2] || '1', 10);
          const newStart = parseInt(match[3], 10);
          const newCount = parseInt(match[4] || '1', 10);
          const oldEnd = Math.max(oldStart, oldStart + oldCount - 1);
          const newEnd = Math.max(newStart, newStart + newCount - 1);
          const oldRange = oldCount === 1 ? `${oldStart}` : `${oldStart}-${oldEnd}`;
          const newRange = newCount === 1 ? `${newStart}` : `${newStart}-${newEnd}`;
          const label = match[5] && match[5].trim() ? ` ${match[5].trim()}` : '';
          display = `Lines ${oldRange} → ${newRange}${label}`;
          oldLine = oldStart;
          newLine = newStart;
        }
        // Hunk header gets special gutter
        const hunkGutter = ''.padStart(maxOldLen, ' ') + '│' + ''.padStart(maxNewLen, ' ') + ' ';
        return `<span class="diff-line ${cls}"><span class="diff-gutter">${escapeHtml(hunkGutter)}</span><span class="diff-text">${escapeHtml(display)}</span></span>`;
      } else if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff --git')) {
        // Skip diff headers entirely - filename shown separately
        return '';
      } else if (line.startsWith('+') && !line.startsWith('+++')) {
        cls = 'diff-add';
        changeMarker = '+';
        newNo = String(newLine);
        newLine += 1;
        display = line.slice(1);
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        cls = 'diff-del';
        changeMarker = '-';
        oldNo = String(oldLine);
        oldLine += 1;
        display = line.slice(1);
      } else if (line.startsWith(' ')) {
        changeMarker = ' ';
        oldNo = String(oldLine);
        newNo = String(newLine);
        oldLine += 1;
        newLine += 1;
        display = line.slice(1);
      } else {
        // Empty line or other content
        display = line;
      }
      
      // Build gutter: "OLD│NEW± "
      // Pad each number to max width, use spaces for missing numbers
      const padOld = oldNo ? oldNo.padStart(maxOldLen, ' ') : ''.padStart(maxOldLen, ' ');
      const padNew = newNo ? newNo.padStart(maxNewLen, ' ') : ''.padStart(maxNewLen, ' ');
      const gutterText = `${padOld}│${padNew}${changeMarker} `;
      
      // Optionally syntax highlight the code
      let codeHtml = escapeHtml(display);
      if (isDiffSyntaxEnabled() && typeof hljs !== 'undefined' && display.trim()) {
        try {
          const ext = filePath ? filePath.split('.').pop()?.toLowerCase() : '';
          // Map common extensions to hljs language names
          const extToLang = {
            'py': 'python', 'js': 'javascript', 'ts': 'typescript', 'tsx': 'typescript',
            'jsx': 'javascript', 'rb': 'ruby', 'rs': 'rust', 'go': 'go', 'sh': 'bash',
            'yml': 'yaml', 'md': 'markdown', 'htm': 'html',
          };
          const lang = extToLang[ext] || ext;
          if (lang && hljs.getLanguage(lang)) {
            codeHtml = hljs.highlight(display, { language: lang, ignoreIllegals: true }).value;
          } else if (display.length > 10) {
            // Only auto-detect for non-trivial lines
            const auto = hljs.highlightAuto(display);
            if (auto.relevance > 3) {
              codeHtml = auto.value;
            }
          }
        } catch (e) {
          // Fall back to escaped text
        }
      }
      
      return `<span class="diff-line ${cls}"><span class="diff-gutter">${escapeHtml(gutterText)}</span><span class="diff-text">${codeHtml}</span></span>`;
    }).filter(line => line !== '').join('');
  }

  function renderApproval(evt) {
    const { row, body } = createRow(evt.kind === 'diff' ? 'diff' : 'approval', 'approval');
    row.dataset.approvalId = evt.id;
    const payload = evt.payload || {};
    const lines = [];
    let diffText = null;
    let filePath = null;
    if (payload.command) {
      lines.push(`<div><strong>Command:</strong> ${escapeHtml(Array.isArray(payload.command) ? payload.command.join(' ') : String(payload.command))}</div>`);
    }
    if (payload.cwd) {
      lines.push(`<div><strong>CWD:</strong> ${escapeHtml(String(payload.cwd))}</div>`);
    }
    if (payload.diff) {
      diffText = payload.diff;
      lines.push(`<pre class="diff-block">${formatDiff(payload.diff, payload.path)}</pre>`);
    }
    if (payload.changes && Array.isArray(payload.changes)) {
      payload.changes.forEach((change) => {
        if (change && change.diff) {
          diffText = diffText || change.diff;
          filePath = filePath || change.path;
          lines.push(`<div><strong>${escapeHtml(toRelativePath(change.path) || 'file')}</strong></div><pre class="diff-block">${formatDiff(change.diff, change.path)}</pre>`);
        }
      });
    }
    body.innerHTML = lines.join('') || `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;

    const actions = document.createElement('div');
    actions.className = 'actions';
    const accept = document.createElement('button');
    accept.className = 'btn tiny approve';
    accept.textContent = 'Accept';
    const decline = document.createElement('button');
    decline.className = 'btn tiny decline';
    decline.textContent = 'Decline';
    accept.addEventListener('click', async () => {
      await respondApproval(evt.id, 'accept');
      // Record to transcript
      await postJson('/api/appserver/approval_record', {
        status: 'accepted',
        diff: diffText,
        path: filePath,
        item_id: evt.id,
      });
      // Remove the approval card
      row.remove();
    });
    decline.addEventListener('click', async () => {
      await respondApproval(evt.id, 'decline');
      // Record to transcript (will also broadcast diff_declined)
      await postJson('/api/appserver/approval_record', {
        status: 'declined',
        diff: diffText,
        path: filePath,
        item_id: evt.id,
      });
      // Remove the approval card
      row.remove();
    });
    actions.append(accept, decline);
    body.append(actions);
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

  async function fetchConversations() {
    try {
      const r = await fetch('/api/appserver/conversations', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      conversationList = data?.items || [];
      const activeId = data?.active_conversation_id || null;
      if (data?.active_view) activeView = data.active_view;
      renderConversationList(conversationList, activeId);
      updateActiveConversationLabel();
    } catch {
      // ignore
    }
  }

  async function setActiveView(view) {
    try {
      await postJson('/api/appserver/view', { view });
      activeView = view;
      setDrawerOpen(view === 'conversation');
    } catch {
      // ignore
    }
  }

  async function selectConversation(conversationId) {
    return selectConversationWithView(conversationId, 'conversation');
  }

  async function selectConversationWithView(conversationId, view) {
    if (!conversationId) return;
    resetTimeline();
    await postJson('/api/appserver/conversations/select', { conversation_id: conversationId, view });
    await fetchConversation();
    await fetchConversations();
    await replayTranscript();
    setDrawerOpen(view === 'conversation');
  }

  async function createConversation() {
    await postJson('/api/appserver/conversations', {});
    await fetchConversation();
    await fetchConversations();
    resetTimeline();
    await replayTranscript();
    setDrawerOpen(true);
    openSettingsModal();
  }

  async function deleteConversation(conversationId) {
    if (!conversationId) return;
    await fetch(`/api/appserver/conversations/${conversationId}`, { method: 'DELETE' });
    await fetchConversations();
    await fetchConversation();
    if (!conversationMeta?.conversation_id) {
      setDrawerOpen(false);
      await setActiveView('splash');
    }
  }

  async function saveSettings() {
    const cwd = settingsCwdEl?.value?.trim();
    if (!cwd) {
      setActivity('CWD required', true);
      return;
    }
    const commandLinesVal = parseInt(settingsCommandLinesEl?.value?.trim() || '20', 10);
    const mdEnabled = settingsMarkdownEl?.checked !== false;
    const xtermEnabled = settingsXtermEl?.checked !== false;
    const diffSyntaxEnabled = settingsDiffSyntaxEl?.checked === true;
    const settings = {
      cwd,
      approvalPolicy: normalizeApprovalValue(settingsApprovalEl?.value?.trim()) || null,
      sandboxPolicy: settingsSandboxEl?.value?.trim() || null,
      model: settingsModelEl?.value?.trim() || null,
      effort: settingsEffortEl?.value?.trim() || null,
      summary: settingsSummaryEl?.value?.trim() || null,
      label: settingsLabelEl?.value?.trim() || null,
      commandOutputLines: Number.isFinite(commandLinesVal) && commandLinesVal > 0 ? commandLinesVal : 20,
      markdown: mdEnabled,
      useXterm: xtermEnabled,
      diffSyntax: diffSyntaxEnabled,
    };
    // Update local markdown state
    setMarkdownEnabled(mdEnabled);
    // Update xterm mode
    setXtermEnabled(xtermEnabled);
    // Update diff syntax highlight state
    setDiffSyntaxEnabled(diffSyntaxEnabled);
    const isNewConversation = pendingNewConversation || !conversationMeta?.conversation_id;
    if (isNewConversation) {
      const meta = await postJson('/api/appserver/conversations', {});
      if (meta?.conversation_id) {
        await postJson('/api/appserver/conversations/select', { conversation_id: meta.conversation_id, view: 'conversation' });
      }
      pendingNewConversation = false;
    }
    await postJson('/api/appserver/conversation', { settings });
    if (pendingRollout?.id && Array.isArray(pendingRollout.items)) {
      setActivity('loading rollout', true);
      await postJson('/api/appserver/conversations/bind-rollout', {
        rollout_id: pendingRollout.id,
      });
      pendingRollout = null;
      setActivity('rollout loaded', false);
    }
    closeSettingsModal();
    await fetchConversation();
    await fetchConversations();
    if (isNewConversation) {
      resetTimeline(); // Clear old transcript when switching to new conversation
    }
    await replayTranscript();
    setDrawerOpen(true);
    updateConversationHeaderLabel();
  }

  function nextRpcId() {
    const id = rpcId;
    rpcId += 1;
    return id;
  }

  async function sendRpc(method, params, options = {}) {
    const payload = { method };
    if (params !== undefined) payload.params = params;
    if (options.notify) {
      await postJson('/api/appserver/rpc', payload);
      return null;
    }
    const id = nextRpcId();
    payload.id = id;
    await waitForWs();
    await postJson('/api/appserver/rpc', payload);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error('rpc timeout'));
      }, 15000);
      pending.set(id, { resolve, reject, timer });
    });
  }

  async function respondApproval(requestId, decision) {
    if (requestId === null || requestId === undefined) return;
    // Ensure ID is sent as integer if it looks like one (JSON-RPC requires matching type)
    let id = requestId;
    if (typeof id === 'string' && /^\d+$/.test(id)) {
      id = parseInt(id, 10);
    }
    await postJson('/api/appserver/rpc', {
      jsonrpc: '2.0',
      id: id,
      result: { decision },
    });
  }

  function resetWsReady() {
    wsOpen = false;
    wsReadyPromise = new Promise((resolve) => { wsReadyResolve = resolve; });
  }

  function markWsOpen() {
    wsOpen = true;
    wsReconnectDelay = 1000;
    if (wsReadyResolve) {
      wsReadyResolve(true);
      wsReadyResolve = null;
    }
  }

  async function waitForWs(timeoutMs = 3000) {
    if (wsOpen) return true;
    let timer;
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), timeoutMs);
    });
    const ok = await Promise.race([wsReadyPromise, timeout]);
    clearTimeout(timer);
    return Boolean(ok);
  }

  async function fetchConversation() {
    try {
      const r = await fetch('/api/appserver/conversation', { cache: 'no-store' });
      if (!r.ok) return;
      conversationMeta = await r.json();
      conversationSettings = conversationMeta?.settings || {};
      activeView = conversationMeta?.active_view || 'splash';
      setDrawerOpen(activeView === 'conversation');
      updateActiveConversationLabel();
      if (footerApprovalValue) footerApprovalValue.textContent = conversationSettings?.approvalPolicy || 'default';
      if (conversationMeta && conversationMeta.thread_id) {
        currentThreadId = conversationMeta.thread_id;
      } else {
        currentThreadId = null;
      }
      // Sync markdown toggle from settings
      setMarkdownEnabled(conversationSettings?.markdown !== false);
      // Sync xterm toggle from settings
      setXtermEnabled(conversationSettings?.useXterm !== false);
      // Sync diff syntax toggle from settings
      setDiffSyntaxEnabled(conversationSettings?.diffSyntax === true);
      // Connect PTY WebSocket for user terminal
      connectPtyWebSocket();
    } catch {
      // Don't touch statusEl here - it's for server status only
    }
    updateConversationHeaderLabel();
  }

  async function fetchStatus() {
    try {
      const r = await fetch('/api/appserver/status', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (data && data.shell_id) {
        currentAppServerShellId = data.shell_id;
      }
      if (data.running) {
        setPill(statusEl, 'running', 'ok');
      } else {
        setPill(statusEl, 'idle', 'warn');
      }
    } catch {
      setPill(statusEl, 'error', 'err');
    }
  }

  async function ensureInitialized() {
    if (initialized) return;
    await postJson('/api/appserver/start', null);
    await waitForWs();
    try {
      await sendRpc('initialize', {
        clientInfo: {
          name: 'agent_log_server',
          title: 'Agent Log Server',
          version: '0.1.0',
        }
      });
    } catch {
      // ignore already initialized
    }
    await sendRpc('initialized', {}, { notify: true });
    initialized = true;
  }

  function scheduleReconnect() {
    if (wsReconnectTimer) return;
    wsReconnectTimer = setTimeout(() => {
      wsReconnectTimer = null;
      wsReconnectDelay = Math.min(wsReconnectDelay * 1.6, 8000);
      connectWS();
    }, wsReconnectDelay);
  }

  async function ensureThread() {
    await fetchConversation();
    if (currentThreadId) {
      try {
        const savedShellId = conversationSettings?.thread_session_shell_id || null;
        const savedThreadId = conversationSettings?.thread_session_thread_id || null;
        if (currentAppServerShellId && savedShellId === currentAppServerShellId && savedThreadId === currentThreadId) {
          return currentThreadId;
        }
        await sendRpc('thread/resume', { threadId: currentThreadId });
        if (currentAppServerShellId) {
          await postJson('/api/appserver/conversation', {
            settings: { thread_session_shell_id: currentAppServerShellId, thread_session_thread_id: currentThreadId },
          });
        }
        return currentThreadId;
      } catch {
        currentThreadId = null;
      }
    }
    const result = await sendRpc('thread/start', {});
    const threadId = result?.thread?.id;
    if (threadId) {
      currentThreadId = threadId;
      if (currentAppServerShellId) {
        await postJson('/api/appserver/conversation', {
          settings: { thread_session_shell_id: currentAppServerShellId, thread_session_thread_id: threadId },
        });
      }
      return threadId;
    }
    throw new Error('thread/start failed');
  }

  // Validate conversation ID matches before sending RPC (guards against stale tabs/multi-device)
  async function validateConversationId() {
    try {
      const r = await fetch('/api/appserver/conversation', { cache: 'no-store' });
      if (!r.ok) return false;
      const serverMeta = await r.json();
      const serverConvoId = serverMeta?.conversation_id;
      const localConvoId = conversationMeta?.conversation_id;
      if (serverConvoId && localConvoId && serverConvoId !== localConvoId) {
        console.warn('Conversation ID mismatch - server:', serverConvoId, 'local:', localConvoId);
        setActivity('conversation changed - refresh', false);
        return false;
      }
      return true;
    } catch {
      return true; // Allow on network error
    }
  }

  async function sendUserMessage(text) {
    if (!text) return;
    if (!conversationMeta?.conversation_id) {
      setActivity('save settings first', true);
      return;
    }
    // Guard against stale tabs / multi-device conflicts
    if (!await validateConversationId()) {
      return;
    }
    setActivity('sending', true);
    await ensureInitialized();
    const threadId = await ensureThread();
    const params = {
      threadId,
      input: [{ type: 'text', text }],
    };
    await sendRpc('turn/start', params);
  }

  // Direct shell command execution via !command
  // Now uses streaming - shell_begin/delta/end events handle rendering
  async function sendShellCommand(command) {
    if (!command) return;
    // Normalize contenteditable/mobile whitespace (NBSP etc) to avoid "command not found" surprises.
    command = String(command)
      .replace(/\u00A0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .trim();
    if (!command) return;
    if (!conversationMeta?.conversation_id) {
      setActivity('save settings first', true);
      return;
    }
    // Activity and row creation handled by shell_begin event from backend
    // Just send the request and let events flow
    try {
      const endpoint = terminalMode ? '/api/mcp/agent-pty/exec' : '/api/appserver/shell/exec';
      const resp = await postJson(endpoint, { command });
      // Response includes callId - shell_end event will finalize the row
      // If error and no shell_end was received, show error
      if (resp.error && !shellRows.has(resp.callId)) {
        renderShellBatchResult({
          exitCode: resp.exitCode || 1,
          stdout: resp.stdout || '',
          stderr: resp.stderr || resp.error,
        });
      }
    } catch (err) {
      // Network error - show batch result
      renderShellBatchResult({
        exitCode: 1,
        stdout: '',
        stderr: String(err),
      });
      setStatusDot('error');
      setActivity('idle', false);
    }
  }

  async function interruptTurn() {
    try {
      setActivity('interrupt', true);
      await postJson('/api/appserver/interrupt', null);
      setActivity('interrupt sent', true);
    } catch (err) {
      console.warn('interrupt failed', err);
      setActivity('interrupt failed', true);
    }
  }

  async function fetchTranscriptRange(offset, limit) {
    const url = `/api/appserver/transcript/range?offset=${offset}&limit=${limit}`;
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return null;
    return r.json();
  }

  async function loadOlderTranscript() {
    if (transcriptLoading) return;
    if (transcriptStart <= 0) return;
    transcriptLoading = true;
    try {
      const prevOffset = Math.max(0, transcriptStart - transcriptLimit);
      const beforeHeight = scrollContainer?.scrollHeight || 0;
      const data = await fetchTranscriptRange(prevOffset, transcriptStart - prevOffset);
      if (data && Array.isArray(data.items)) {
        transcriptTotal = data.total || transcriptTotal;
        transcriptStart = data.offset ?? prevOffset;
        renderTranscriptEntries(data.items, { prepend: true });
        transcriptEnd = Math.max(transcriptEnd, transcriptStart + (data.items?.length || 0));
        const afterHeight = scrollContainer?.scrollHeight || 0;
        if (scrollContainer) {
          scrollContainer.scrollTop += (afterHeight - beforeHeight);
        }
      }
    } finally {
      transcriptLoading = false;
    }
  }

  async function replayTranscript() {
    try {
      const data = await fetchTranscriptRange(-1, transcriptLimit);
      if (!data || !Array.isArray(data.items)) return;
      transcriptTotal = data.total || 0;
      transcriptStart = data.offset || 0;
      transcriptEnd = transcriptStart + (data.items?.length || 0);
      renderTranscriptEntries(data.items, { prepend: false });
      transcriptEnd = transcriptStart + (data.items?.length || 0);
      lastEventType = null;
      // Delay scroll to ensure DOM is fully rendered
      requestAnimationFrame(() => {
        requestAnimationFrame(() => maybeAutoScroll(true));
      });
    } catch {
      // ignore replay failures
    }
  }

  function handleEvent(evt) {
    if (!evt || typeof evt !== 'object') return;
    
    // Filter events by conversation_id - only render events for active conversation
    const activeConvoId = conversationMeta?.conversation_id;
    if (evt.conversation_id && activeConvoId && evt.conversation_id !== activeConvoId) {
      // Event belongs to a different conversation - ignore for rendering
      return;
    }
    
    switch (evt.type) {
      case 'activity':
        lastEventType = 'activity';
        setActivity(evt.label || 'idle', Boolean(evt.active));
        // Update status dot based on activity
        if (!evt.active && evt.label === 'idle') {
          finalizePlanToTranscript();
          // Keep last status dot state (don't reset on idle)
        }
        return;
      case 'error':
        lastEventType = 'error';
        renderErrorCard(evt.message || 'Unknown error');
        setStatusDot('error');
        return;
      case 'warning':
        lastEventType = 'warning';
        renderWarningCard(evt.message || '');
        setStatusDot('warning');
        return;
      case 'status':
        // Status event from turn/completed - update ribbon dot
        if (evt.status) {
          setStatusDot(evt.status);
        }
        return;
      case 'message':
        lastEventType = 'message';
        addMessage(evt.role || 'message', evt.text || '');
        return;
      case 'assistant_delta':
        lastEventType = 'assistant';
        appendAssistantDelta(evt.id, evt.delta || '');
        return;
      case 'assistant_finalize':
        lastEventType = 'assistant';
        finalizeAssistant(evt.id, evt.text || '');
        setStatusDot('success');
        return;
      case 'reasoning_delta':
        lastEventType = 'reasoning';
        appendReasoningDelta(evt.id, evt.delta || '');
        return;
      case 'reasoning_finalize':
        lastEventType = 'reasoning';
        finalizeReasoning(evt.id, evt.text || '');
        return;
      case 'diff':
        lastEventType = 'diff';
        addDiff(evt.id, evt.text || '', evt.path || '');
        return;
      case 'diff_declined':
        lastEventType = 'diff';
        addDeclinedDiff(evt.id, evt.text || '', evt.path || '');
        return;
      case 'approval':
        lastEventType = 'approval';
        renderApproval(evt);
        return;
      case 'command_result':
        renderCommandResult(evt);
        return;
      case 'tool_begin':
        renderToolBegin(evt);
        return;
      case 'tool_delta':
        renderToolDelta(evt);
        return;
      case 'tool_end':
        renderToolEnd(evt);
        return;
      case 'tool_interaction':
        renderToolInteraction(evt);
        return;
      case 'agent_block_begin':
        renderAgentBlockBegin(evt);
        return;
      case 'agent_block_delta':
        renderAgentBlockDelta(evt);
        return;
      case 'agent_block_end':
        renderAgentBlockEnd(evt);
        return;
      case 'screen_delta':
        renderScreenDelta(evt);
        return;
      case 'agent_pty_raw':
        renderAgentPtyRaw(evt);
        return;
      case 'shell_begin':
        renderShellBegin(evt);
        return;
      case 'shell_delta':
        renderShellDelta(evt);
        return;
      case 'shell_end':
        renderShellEnd(evt);
        return;
      case 'plan_update':
        lastEventType = 'plan';
        updatePlanItem(evt.step, evt.status);
        return;
      case 'plan':
        lastEventType = 'plan';
        renderPlanCard(evt.steps || []);
        clearPlanOverlay();
        return;
      case 'token_count':
        lastEventType = 'token';
        if (Number.isFinite(evt.context_window)) {
          contextWindow = Number(evt.context_window);
        }
        updateTokens(evt.total);
        if (Number.isFinite(evt.context_window)) {
          // Use total tokens for percentage (matches CLI behavior)
          updateContextRemaining(evt.total, evt.context_window);
        }
        return;
      case 'context_compacted':
        lastEventType = 'system';
        renderContextCompactedCard();
        return;
      case 'mention_insert':
        insertMention(evt.path || '');
        return;
      case 'rpc_response': {
        const entry = pending.get(evt.id);
        if (entry) {
          clearTimeout(entry.timer);
          pending.delete(evt.id);
          entry.resolve(evt.result);
        }
        return;
      }
      case 'rpc_error': {
        const entry = pending.get(evt.id);
        if (entry) {
          clearTimeout(entry.timer);
          pending.delete(evt.id);
          if (String(evt.message || '').includes('Already initialized')) {
            entry.resolve(null);
          } else {
            entry.reject(new Error(evt.message || 'rpc error'));
          }
        }
        return;
      }
      default:
        return;
    }
  }

  function connectWS() {
    if (typeof io === 'undefined') {
      setPill(wsStatusEl, 'no-io', 'err');
      return;
    }
    setPill(wsStatusEl, 'connecting', 'warn');
    const socket = io('/appserver', {
      transports: ['websocket'],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 500,
      reconnectionDelayMax: 5000,
    });
    socket.on('connect', () => {
      markWsOpen();
      setPill(wsStatusEl, 'connected', 'ok');
    });
    socket.on('disconnect', () => {
      resetWsReady();
      setPill(wsStatusEl, 'disconnected', 'err');
    });
    socket.on('connect_error', () => {
      resetWsReady();
      setPill(wsStatusEl, 'error', 'err');
    });
    socket.on('appserver_event', (data) => {
      handleEvent(data);
    });
  }

  // Connect raw PTY WebSocket for user terminal (separate from agent transcript)
  function connectPtyWebSocket() {
    const convoId = conversationMeta?.conversation_id;
    if (!convoId) return;
    
    // Close existing connection if any
    if (ptyWebSocket) {
      ptyWebSocket.close();
      ptyWebSocket = null;
    }
    
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/pty/${encodeURIComponent(convoId)}`;
    
    try {
      ptyWebSocket = new WebSocket(wsUrl);
      
      ptyWebSocket.onopen = () => {
        console.log('PTY WebSocket connected');
        if (activeAgentPtyBlockId) {
          const entry = agentBlockRows.get(activeAgentPtyBlockId);
          if (entry) entry.hasRawStream = true;
        }
      };
      
      ptyWebSocket.onmessage = (event) => {
        // Raw PTY output - for user terminal xterm rendering
        // This is separate from agent transcript which uses screen_delta events
        const data = event.data;
        if (typeof data === 'string' && data) {
          // Find active user terminal entry and write to it
          // For now, we can use a dedicated user terminal or the active agent block in raw mode
          handleUserPtyOutput(data);
        }
      };
      
      ptyWebSocket.onerror = (err) => {
        console.error('PTY WebSocket error:', err);
      };
      
      ptyWebSocket.onclose = () => {
        console.log('PTY WebSocket closed');
        ptyWebSocket = null;
      };
    } catch (e) {
      console.error('Failed to connect PTY WebSocket:', e);
    }
  }

  // Handle raw PTY output for user terminal
  function handleUserPtyOutput(chunk) {
    // Route raw PTY output to the active agent block (raw mode)
    if (activeAgentPtyBlockId) {
      const entry = agentBlockRows.get(activeAgentPtyBlockId);
      if (entry) {
        entry.hasRawStream = true;
        if (!entry.term && useXterm) {
          entry.term = createXterm(entry.termEl, 12);
        }
        if (useXterm && entry.term) {
          entry.term.write(chunk);
        } else if (!useXterm && entry.termEl) {
          entry.text += chunk;
          entry.termEl.textContent = entry.text;
        }
        maybeAutoScroll();
        return;
      }
    }
    // Fallback: find any raw-mode block
    for (const [blockId, entry] of agentBlockRows) {
      if (entry.renderMode === 'raw' && entry.term && useXterm) {
        entry.hasRawStream = true;
        entry.term.write(chunk);
        maybeAutoScroll();
        return;
      }
    }
    // Fallback: if no raw-mode block, could create a dedicated user terminal
    // For now, just log
    // console.log('PTY output (no target):', chunk.substring(0, 50));
  }

  setPill(statusEl, 'idle', 'warn');
  setCounter(counterMessagesEl, messageCount);
  setCounter(counterTokensEl, tokenCount);
  updateScrollButton();
  resetWsReady();
  connectWS();
  bindPickerFilter();
  setDrawerOpen(false); // Start closed to avoid race conditions
  fetchConversation().then(async () => {
    await fetchConversations();
    if (activeView === 'conversation') {
      resetTimeline(); // Reset timeline to ensure proper DOM order
      await replayTranscript();
      // Small delay to ensure DOM is ready before opening and scrolling
      setTimeout(() => {
        setDrawerOpen(true);
        maybeAutoScroll(true);
      }, 50);
    } else {
      ensureActivityRow(); // Only create activity row for non-conversation view
    }
  });
  fetchStatus();

  setupDropdown(settingsApprovalEl, settingsApprovalToggle, settingsApprovalOptions, [
    'never',
    'on-failure',
    'untrusted',
  ]);
  setupDropdown(settingsSandboxEl, settingsSandboxToggle, settingsSandboxOptions, [
    'workspaceWrite',
    'readOnly',
    'dangerFullAccess',
    'externalSandbox',
  ]);
  setupDropdown(settingsModelEl, settingsModelToggle, settingsModelOptions, [
    'gpt-5.1-codex',
    'gpt-5-codex',
    'gpt-4.1-codex',
  ]);
  setupDropdown(settingsEffortEl, settingsEffortToggle, settingsEffortOptions, [
    'low',
    'medium',
    'high',
  ]);
  setupDropdown(settingsSummaryEl, settingsSummaryToggle, settingsSummaryOptions, [
    'concise',
    'detailed',
    'auto',
  ]);
  loadModelOptions();

  // Update effort options when model input changes (typing or paste)
  if (settingsModelEl) {
    settingsModelEl.addEventListener('input', () => {
      updateEffortOptionsForModel(settingsModelEl.value);
    });
    settingsModelEl.addEventListener('change', () => {
      updateEffortOptionsForModel(settingsModelEl.value);
    });
  }

  window.CodexAgent = {
    helpers: {
      openSettingsModal,
      closeSettingsModal,
      saveSettings,
      openPicker,
      closePicker,
      openRolloutPicker,
      closeRolloutPicker,
      loadRolloutPreview,
      setActiveView,
      setDrawerOpen,
      setPendingNewConversation: (val) => { pendingNewConversation = Boolean(val); },
      setPendingRollout: (val) => { pendingRollout = val; },
      fetchPicker,
      fetchRollouts,
      setActivity,
      insertMention,
      getPickerPath: () => pickerPath,
      setPickerPath: (val) => { pickerPath = val; },
      getPickerMode: () => pickerMode,
      setPickerMode: (val) => { pickerMode = val || 'cwd'; },
      saveApprovalQuick,
    },
    state: {
      get pendingNewConversation() { return pendingNewConversation; },
      set pendingNewConversation(val) { pendingNewConversation = Boolean(val); },
      get pendingRollout() { return pendingRollout; },
      get conversationMeta() { return conversationMeta; },
      get conversationSettings() { return conversationSettings; },
    },
  };

  startBtn?.addEventListener('click', async () => {
    await postJson('/api/appserver/start', null);
    fetchStatus();
  });

  stopBtn?.addEventListener('click', async () => {
    await postJson('/api/appserver/stop', null);
    fetchStatus();
  });
  (window.CodexAgentModules || []).forEach((fn) => {
    try { fn(window.CodexAgent); } catch (err) { console.warn('module init failed', err); }
  });

  document.addEventListener('click', (evt) => {
    if (!openDropdownEl) return;
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    if (openDropdownEl.contains(target)) return;
    if (target.classList.contains('dropdown-toggle')) return;
    closeDropdownMenu(openDropdownEl);
  });

  // Helper to dispatch message or shell command based on ! prefix
  async function dispatchInput(text) {
    if (terminalMode && !text.startsWith('!')) {
      await sendShellCommand(text.trim());
    } else if (text.startsWith('!')) {
      await sendShellCommand(text.slice(1).trim());
    } else {
      await sendUserMessage(text);
    }
  }

  sendBtn?.addEventListener('click', async () => {
    const text = getPromptText().trim();
    if (!text) return;
    clearPrompt();
    await dispatchInput(text);
  });

  footerTerminalToggleEl?.addEventListener('click', () => {
    setTerminalMode(!terminalMode);
    promptEl?.focus();
  });

  promptEl?.addEventListener('keydown', async (evt) => {
    // When command is running in terminal mode, stream keystrokes directly to PTY
    if (commandRunning && terminalMode) {
      evt.preventDefault();
      let data = '';
      if (evt.key === 'Enter') {
        data = '\n';
      } else if (evt.key === 'Backspace') {
        data = '\x7f'; // DEL character
      } else if (evt.key === 'Tab') {
        data = '\t';
      } else if (evt.key === 'Escape') {
        data = '\x1b';
      } else if (evt.key === 'ArrowUp') {
        data = '\x1b[A';
      } else if (evt.key === 'ArrowDown') {
        data = '\x1b[B';
      } else if (evt.key === 'ArrowRight') {
        data = '\x1b[C';
      } else if (evt.key === 'ArrowLeft') {
        data = '\x1b[D';
      } else if (evt.ctrlKey && evt.key.length === 1) {
        // Ctrl+C, Ctrl+D, etc.
        const code = evt.key.toUpperCase().charCodeAt(0) - 64;
        if (code > 0 && code < 32) {
          data = String.fromCharCode(code);
        }
      } else if (evt.key.length === 1) {
        data = evt.key;
      }
      if (data) {
        await sendPtyStdin(data);
      }
      return;
    }
    
    if (evt.key === 'Enter' && !evt.shiftKey) {
      if (isMobile && !terminalMode) {
        // On mobile, Enter inserts newline (let default happen)
        return;
      }
      evt.preventDefault();
      const text = getPromptText().trim();
      if (!text) return;
      clearPrompt();
      await dispatchInput(text);
      return;
    }
    if (evt.key === 'Enter' && evt.shiftKey) {
      evt.preventDefault();
      document.execCommand('insertLineBreak');
    }
  });

  // Send stdin to PTY
  async function sendPtyStdin(data) {
    if (ptyWebSocket && ptyWebSocket.readyState === WebSocket.OPEN) {
      ptyWebSocket.send(data);
    } else {
      // Fallback to HTTP POST
      try {
        await postJson('/api/pty/stdin', { data });
      } catch (e) {
        console.error('Failed to send PTY stdin:', e);
      }
    }
  }

  promptEl?.addEventListener('input', () => {
    // When in stdin mode, send each character as it's typed
    if (commandRunning && terminalMode) {
      const text = getPromptText();
      if (text) {
        clearPrompt();
        sendPtyStdin(text);
      }
    }
    // Tribute handles @ mentions automatically
  });

  promptEl?.addEventListener('click', (evt) => {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.classList.contains('mention-token')) {
      // Show full path on click/tap
      const path = target.dataset.path || target.title || target.textContent || '';
      console.log('Mention path:', path);
    }
  });

  mentionPillEl?.addEventListener('click', () => {
    const startPath = conversationSettings?.cwd || settingsCwdEl?.value || '~';
    openPicker(startPath, 'mention');
  });

  // Initialize Tribute for @ mentions
  initTribute();

  contextRemainingEl?.addEventListener('click', () => {
    if (window.CodexAgent?.helpers?.openWarningModal) {
      window.CodexAgent.helpers.openWarningModal({
        title: 'Compact context?',
        body: 'This will summarize the current conversation history to save context window.',
        confirmText: 'Compact',
        onConfirm: async () => {
          await requestContextCompact();
        },
      });
    }
  });

  interruptBtn?.addEventListener('click', async () => {
    await interruptTurn();
  });

  scrollContainer?.addEventListener('scroll', () => {
    if (scrollContainer) {
      const topSpacerHeight = topSpacerEl ? topSpacerEl.getBoundingClientRect().height : 0;
      if (scrollContainer.scrollTop <= topSpacerHeight + 120) {
        loadOlderTranscript();
      }
    }
  });

  scrollBtn?.addEventListener('click', () => {
    autoScroll = !autoScroll;
    updateScrollButton();
    if (autoScroll) maybeAutoScroll(true);
  });

  // Markdown toggle in header - syncs with settings and saves to SSOT
  markdownToggleEl?.addEventListener('change', async () => {
    const enabled = markdownToggleEl.checked;
    setMarkdownEnabled(enabled);
    // Save to SSOT if we have an active conversation
    if (conversationMeta?.conversation_id) {
      await postJson('/api/appserver/conversation', { 
        settings: { ...conversationSettings, markdown: enabled } 
      });
    }
  });

  // Sync markdown toggle when conversation loads
  function syncMarkdownFromSettings() {
    const enabled = conversationSettings?.markdown !== false;
    setMarkdownEnabled(enabled);
  }
});
