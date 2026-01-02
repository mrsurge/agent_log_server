document.addEventListener('DOMContentLoaded', () => {
  const statusEl = document.getElementById('agent-status');
  const wsStatusEl = document.getElementById('agent-ws');
  const timelineEl = document.getElementById('agent-timeline');
  const timelineWrapEl = timelineEl?.closest('.timeline-wrap');
  const scrollContainer = timelineWrapEl || timelineEl;
  const startBtn = document.getElementById('agent-start');
  const stopBtn = document.getElementById('agent-stop');
  const promptEl = document.getElementById('agent-prompt');
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
  let lastReasoningKey = null;
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
  const pending = new Map();

  const assistantRows = new Map();
  const reasoningRows = new Map();
  const diffRows = new Map();
  const toolRows = new Map();
  let activityRow = null;
  let activityTextEl = null;
  let activityLineEl = null;
  let topSpacerEl = null;
  let bottomSpacerEl = null;
  let placeholderCleared = false;
  let messageCount = 0;
  let tokenCount = 0;
  let contextWindow = null;
  let autoScroll = true;
  let normalizeTimer = null;
  let isNormalizing = false;
  let mentionTriggered = false;
  let transcriptTotal = 0;
  let transcriptStart = 0;
  let transcriptEnd = 0;
  let transcriptLimit = 120;
  let transcriptLoading = false;
  let estimatedRowHeight = 28;

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
    if (!promptEl || isNormalizing) return;
    const text = getPromptText();
    if (!text.includes('`')) return;
    isNormalizing = true;
    renderPromptFromText(text);
    if (promptEl instanceof HTMLElement) {
      moveCaretToEnd();
    }
    isNormalizing = false;
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

  function detectMentionTrigger() {
    if (!promptEl) return false;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return false;
    const range = selection.getRangeAt(0);
    if (!promptEl.contains(range.commonAncestorContainer)) return false;
    const text = getPromptText();
    return /\s@$/.test(text);
  }

  function insertMention(path) {
    if (!promptEl || !path) return;
    const token = createMentionToken(path);
    const selection = window.getSelection();
    const useSelection = selection && selection.rangeCount &&
      promptEl.contains(selection.getRangeAt(0).commonAncestorContainer);
    if (useSelection) {
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
    }
    promptEl.focus();
    showTapTip(path);
    if (mentionTriggered) {
      const text = getPromptText().replace(/\s@$/, ' ');
      renderPromptFromText(text);
      moveCaretToEnd();
      mentionTriggered = false;
    } else {
      moveCaretToEnd();
    }
  }

  function showTapTip(text) {
    if (!text || !promptEl) return;
    if (!('ontouchstart' in window)) return;
    let tip = document.getElementById('mention-tip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'mention-tip';
      tip.className = 'mention-tip';
      document.body.appendChild(tip);
    }
    tip.textContent = text;
    const rect = promptEl.getBoundingClientRect();
    tip.style.left = `${rect.left + 12}px`;
    tip.style.top = `${rect.top - 32}px`;
    tip.classList.add('show');
    setTimeout(() => tip.classList.remove('show'), 2000);
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
      if (settingsRolloutEl) settingsRolloutEl.value = pendingRollout?.id || '';
    } else {
      if (settingsCwdEl) settingsCwdEl.value = conversationSettings?.cwd || '';
      if (settingsApprovalEl) settingsApprovalEl.value = conversationSettings?.approvalPolicy || '';
      if (settingsSandboxEl) settingsSandboxEl.value = conversationSettings?.sandboxPolicy || '';
      if (settingsModelEl) settingsModelEl.value = conversationSettings?.model || '';
      if (settingsEffortEl) settingsEffortEl.value = conversationSettings?.effort || '';
      if (settingsSummaryEl) settingsSummaryEl.value = conversationSettings?.summary || '';
      if (settingsLabelEl) settingsLabelEl.value = conversationSettings?.label || '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = conversationSettings?.commandOutputLines || '20';
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

  function buildDropdown(listEl, options, inputEl) {
    if (!listEl) return;
    listEl.innerHTML = '';
    options.forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.textContent = opt;
      btn.addEventListener('click', () => {
        if (inputEl) inputEl.value = opt;
        closeDropdown(listEl);
      });
      listEl.appendChild(btn);
    });
  }

  function updateDropdownOptions(listEl, options, inputEl) {
    if (!listEl) return;
    listEl.innerHTML = '';
    const values = Array.from(new Set(options.filter(Boolean)));
    buildDropdown(listEl, values, inputEl);
  }

  async function loadModelOptions() {
    try {
      const r = await fetch('/api/appserver/models', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const items = data?.result?.data || data?.result?.models || data?.result || [];
      const names = [];
      if (Array.isArray(items)) {
        items.forEach((item) => {
          if (typeof item === 'string') names.push(item);
          else if (item && typeof item === 'object') {
            if (item.id) names.push(item.id);
            else if (item.name) names.push(item.name);
          }
        });
      }
      if (names.length) {
        updateDropdownOptions(settingsModelOptions, names, settingsModelEl);
      }
    } catch {
      // ignore
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
    if (activityRow) return;
    clearPlaceholder();
    const row = document.createElement('div');
    row.className = 'timeline-row activity';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = 'activity';
    const body = document.createElement('div');
    body.className = 'body';
    const line = document.createElement('div');
    line.className = 'activity-line';
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    const text = document.createElement('span');
    text.className = 'activity-text';
    text.textContent = 'idle';
    line.append(spinner, text);
    body.append(line);
    row.append(meta, body);
    timelineEl.append(row);
    activityRow = row;
    activityTextEl = text;
    activityLineEl = line;
    maybeAutoScroll(true);
  }

  function insertRow(row, beforeEl) {
    clearPlaceholder();
    if (beforeEl && beforeEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, beforeEl);
    } else if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else if (activityRow && activityRow.parentElement === timelineEl) {
      timelineEl.insertBefore(row, activityRow);
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
    ensureActivityRow();
    if (activityTextEl) activityTextEl.textContent = label || 'idle';
    if (activityLineEl) activityLineEl.classList.toggle('active', Boolean(active));
    maybeAutoScroll();
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
    if (Number.isFinite(contextWindow)) {
      updateContextRemaining(tokenCount, contextWindow);
    }
  }

  function updateContextRemaining(total, windowSize) {
    if (!contextRemainingEl) return;
    if (!Number.isFinite(total) || !Number.isFinite(windowSize)) {
      contextRemainingEl.textContent = '—';
      return;
    }
    const remaining = Math.max(0, Number(windowSize) - Number(total));
    contextRemainingEl.textContent = String(remaining);
  }

  function resetTimeline() {
    if (!timelineEl) return;
    timelineEl.innerHTML = '';
    assistantRows.clear();
    reasoningRows.clear();
    diffRows.clear();
    toolRows.clear();
    activityRow = null;
    activityTextEl = null;
    activityLineEl = null;
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
    lastReasoningKey = null;
    setCounter(counterMessagesEl, messageCount);
    setCounter(counterTokensEl, tokenCount);
    if (contextRemainingEl) contextRemainingEl.textContent = '—';
    timelineEl.appendChild(topSpacerEl);
    const placeholder = document.createElement('div');
    placeholder.id = 'timeline-placeholder';
    placeholder.className = 'timeline-row muted';
    placeholder.textContent = 'Waiting for events...';
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
    const { body } = createRow('message', label);
    const pre = document.createElement('pre');
    pre.textContent = text || '';
    body.append(pre);
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
        pre.innerHTML = formatDiff(entry.text || '');
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
        let cmdText = entry.command || '';
        if (entry.cwd) cmdText += `\ncwd: ${entry.cwd}`;
        cmdRibbon.textContent = cmdText;
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
          outputPre.textContent = displayOutput;
          if (truncated) {
            outputPre.textContent += `\n... (truncated, showing ${truncateLines} of ${lines.length} lines)`;
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
      const label = entry.role === 'assistant' ? 'assistant' : entry.role;
      const { row, body } = buildRow('message', label);
      const pre = document.createElement('pre');
      pre.textContent = entry.text || '';
      body.append(pre);
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
    measureRowHeight();
    updateSpacerHeights();
  }

  function getAssistantRow(id) {
    const key = id || 'assistant';
    let entry = assistantRows.get(key);
    if (!entry) {
      const { body } = createRow('message', 'assistant');
      const pre = document.createElement('pre');
      pre.textContent = '';
      body.append(pre);
      entry = { pre, counted: false };
      assistantRows.set(key, entry);
    }
    return entry;
  }

  function appendAssistantDelta(id, delta) {
    if (!delta) return;
    const entry = getAssistantRow(id);
    entry.pre.textContent += delta;
    maybeAutoScroll();
  }

  function finalizeAssistant(id, text) {
    const key = id || 'assistant';
    const entry = assistantRows.get(key);
    if (!entry) return;
    if (text) entry.pre.textContent = text;
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
    const useLast = lastEventType === 'reasoning' && lastReasoningKey;
    const key = useLast ? lastReasoningKey : (id || 'reasoning');
    const entry = getReasoningRow(key);
    entry.pre.textContent += delta;
    lastReasoningKey = key;
    lastEventType = 'reasoning';
    maybeAutoScroll();
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
    const entry = getToolRow(evt.id, `tool:${toolName}`);
    const payload = evt.payload || {};
    const cmd = payload.command || payload.cmd || payload.args;
    const cwd = payload.cwd;
    const parts = [];
    if (cmd) parts.push(String(cmd));
    if (cwd) parts.push(`cwd=${cwd}`);
    if (parts.length) {
      entry.pre.textContent += `[begin] ${parts.join(' ')}\n`;
    } else {
      entry.pre.textContent += '[begin]\n';
    }
    lastEventType = 'tool';
  }

  function renderToolDelta(evt) {
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`);
    const delta = evt.delta || '';
    if (delta) {
      entry.pre.textContent += delta;
    }
    lastEventType = 'tool';
    maybeAutoScroll();
  }

  function renderToolEnd(evt) {
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`);
    const payload = evt.payload || {};
    const exitCode = payload.exit_code ?? payload.exitCode;
    const duration = payload.duration_ms ?? payload.durationMs;
    const parts = [];
    if (exitCode !== undefined && exitCode !== null) parts.push(`exit=${exitCode}`);
    if (duration !== undefined && duration !== null) parts.push(`duration=${duration}ms`);
    entry.pre.textContent += `[end] ${parts.join(' ')}\n`;
    lastEventType = 'tool';
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
    // Just show the command, cwd on separate line if present
    let cmdText = command;
    if (cwd) cmdText += `\ncwd: ${cwd}`;
    cmdRibbon.textContent = cmdText;
    body.appendChild(cmdRibbon);
    
    // Output block (if any)
    if (displayOutput) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'command-output';
      outputPre.textContent = displayOutput;
      if (truncated) {
        outputPre.textContent += `\n... (truncated, showing ${truncateLines} of ${output.split('\n').length} lines)`;
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
    
    // Insert before activity row
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl.appendChild(row);
    }
    
    lastEventType = 'command';
    maybeAutoScroll();
  }

  function addDiff(id, text, path) {
    const entry = getDiffRow(id, path);
    entry.pre.innerHTML = formatDiff(text || '');
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
    pre.innerHTML = formatDiff(text || '');
    body.appendChild(pre);
    lastEventType = 'diff';
    maybeAutoScroll();
  }

  function formatDiff(text) {
    if (!text) return '';
    let oldLine = 0;
    let newLine = 0;
    let maxOld = 0;
    let maxNew = 0;
    text.split('\n').forEach((line) => {
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
        if (match) {
          const oldStart = parseInt(match[1], 10);
          const newStart = parseInt(match[3], 10);
          maxOld = Math.max(maxOld, String(oldStart).length);
          maxNew = Math.max(maxNew, String(newStart).length);
        }
      }
    });
    return text.split('\n').map((line) => {
      let cls = 'diff-context';
      let display = line;
      let gutter = ' ';
      let oldNo = '';
      let newNo = '';

      if (line.startsWith('@@')) {
        cls = 'diff-hunk';
        gutter = '@';
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
          const label = match[5] && match[5].trim() ? ` (${match[5].trim()})` : '';
          display = `Lines ${oldRange} → ${newRange}${label}`;
          oldLine = oldStart;
          newLine = newStart;
        }
      } else if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff --git')) {
        // Skip diff headers entirely - filename shown separately
        return '';
      } else if (line.startsWith('+') && !line.startsWith('+++')) {
        cls = 'diff-add';
        gutter = '+';
        newNo = String(newLine);
        newLine += 1;
        display = line.slice(1);
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        cls = 'diff-del';
        gutter = '-';
        oldNo = String(oldLine);
        oldLine += 1;
        display = line.slice(1);
      } else if (line.startsWith(' ')) {
        gutter = ' ';
        oldNo = String(oldLine);
        newNo = String(newLine);
        oldLine += 1;
        newLine += 1;
        display = line.slice(1);
      }
      const padOld = oldNo ? oldNo.padStart(maxOld, ' ') : ''.padStart(maxOld, ' ');
      const padNew = newNo ? newNo.padStart(maxNew, ' ') : ''.padStart(maxNew, ' ');
      const sep = oldNo || newNo ? '|' : ' ';
      const gutterText = `${padOld}${sep}${padNew}${gutter}`;
      return `<span class=\"diff-line ${cls}\"><span class=\"diff-gutter\">${escapeHtml(gutterText)}</span><span class=\"diff-text\">${escapeHtml(display)}</span></span>`;
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
      lines.push(`<pre class="diff-block">${formatDiff(payload.diff)}</pre>`);
    }
    if (payload.changes && Array.isArray(payload.changes)) {
      payload.changes.forEach((change) => {
        if (change && change.diff) {
          diffText = diffText || change.diff;
          filePath = filePath || change.path;
          lines.push(`<div><strong>${escapeHtml(toRelativePath(change.path) || 'file')}</strong></div><pre class="diff-block">${formatDiff(change.diff)}</pre>`);
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
    const settings = {
      cwd,
      approvalPolicy: normalizeApprovalValue(settingsApprovalEl?.value?.trim()) || null,
      sandboxPolicy: settingsSandboxEl?.value?.trim() || null,
      model: settingsModelEl?.value?.trim() || null,
      effort: settingsEffortEl?.value?.trim() || null,
      summary: settingsSummaryEl?.value?.trim() || null,
      label: settingsLabelEl?.value?.trim() || null,
      commandOutputLines: Number.isFinite(commandLinesVal) && commandLinesVal > 0 ? commandLinesVal : 20,
    };
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
        await sendRpc('thread/resume', { threadId: currentThreadId });
        return currentThreadId;
      } catch {
        currentThreadId = null;
      }
    }
    const params = buildCodexSettings();
    const result = await sendRpc('thread/start', params);
    const threadId = result?.thread?.id;
    if (threadId) {
      currentThreadId = threadId;
      return threadId;
    }
    throw new Error('thread/start failed');
  }

  function buildCodexSettings() {
    const settings = {};
    const allowed = [
      'cwd',
      'approvalPolicy',
      'sandboxPolicy',
      'model',
      'effort',
      'summary',
    ];
    allowed.forEach((key) => {
      if (conversationSettings && conversationSettings[key] !== undefined && conversationSettings[key] !== null && conversationSettings[key] !== '') {
        settings[key] = conversationSettings[key];
      }
    });
    return settings;
  }

  async function sendUserMessage(text) {
    if (!text) return;
    if (!conversationMeta?.conversation_id) {
      setActivity('save settings first', true);
      return;
    }
    setActivity('sending', true);
    await ensureInitialized();
    const threadId = await ensureThread();
    const settings = buildCodexSettings();
    const params = {
      threadId,
      input: [{ type: 'text', text }],
    };
    Object.assign(params, settings);
    await sendRpc('turn/start', params);
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
    switch (evt.type) {
      case 'activity':
        lastEventType = 'activity';
        setActivity(evt.label || 'idle', Boolean(evt.active));
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
        return;
      case 'reasoning_delta':
        appendReasoningDelta(evt.id, evt.delta || '');
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
      case 'token_count':
        lastEventType = 'token';
        if (Number.isFinite(evt.context_window)) {
          contextWindow = Number(evt.context_window);
        }
        updateTokens(evt.total);
        if (Number.isFinite(evt.context_window)) {
          updateContextRemaining(evt.total, evt.context_window);
        }
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

  sendBtn?.addEventListener('click', async () => {
    const text = getPromptText().trim();
    if (!text) return;
    clearPrompt();
    await sendUserMessage(text);
  });

  promptEl?.addEventListener('keydown', async (evt) => {
    if (evt.key === 'Enter' && !evt.shiftKey) {
      evt.preventDefault();
      const text = getPromptText().trim();
      if (!text) return;
      clearPrompt();
      await sendUserMessage(text);
      return;
    }
    if (evt.key === 'Enter' && evt.shiftKey) {
      evt.preventDefault();
      document.execCommand('insertLineBreak');
    }
  });

  promptEl?.addEventListener('input', () => {
    if (normalizeTimer) clearTimeout(normalizeTimer);
    normalizeTimer = setTimeout(normalizeMentions, 200);
    if (detectMentionTrigger()) {
      const text = getPromptText().replace(/\s@$/, ' ');
      renderPromptFromText(text);
      moveCaretToEnd();
      const startPath = conversationSettings?.cwd || settingsCwdEl?.value || '~';
      mentionTriggered = true;
      openPicker(startPath, 'mention');
    }
  });

  promptEl?.addEventListener('click', (evt) => {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.classList.contains('mention-token')) {
      const path = target.dataset.path || target.textContent || '';
      showTapTip(path);
    }
  });

  mentionPillEl?.addEventListener('click', () => {
    const startPath = conversationSettings?.cwd || settingsCwdEl?.value || '~';
    openPicker(startPath, 'mention');
  });

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
});
