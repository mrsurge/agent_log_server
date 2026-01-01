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
  const rolloutOverlayEl = document.getElementById('rollout-picker');
  const rolloutCloseBtn = document.getElementById('rollout-close');
  const rolloutListEl = document.getElementById('rollout-list');

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
  let activityRow = null;
  let activityTextEl = null;
  let activityLineEl = null;
  let placeholderCleared = false;
  let messageCount = 0;
  let tokenCount = 0;
  let autoScroll = true;

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
      if (settingsRolloutEl) settingsRolloutEl.value = pendingRollout?.id || '';
    } else {
      if (settingsCwdEl) settingsCwdEl.value = conversationSettings?.cwd || '';
      if (settingsApprovalEl) settingsApprovalEl.value = conversationSettings?.approvalPolicy || '';
      if (settingsSandboxEl) settingsSandboxEl.value = conversationSettings?.sandboxPolicy || '';
      if (settingsModelEl) settingsModelEl.value = conversationSettings?.model || '';
      if (settingsEffortEl) settingsEffortEl.value = conversationSettings?.effort || '';
      if (settingsSummaryEl) settingsSummaryEl.value = conversationSettings?.summary || '';
      if (settingsLabelEl) settingsLabelEl.value = conversationSettings?.label || '';
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

  async function saveApprovalQuick(value) {
    const approval = value?.trim();
    if (!approval) return;
    await postJson('/api/appserver/conversation', { settings: { approvalPolicy: approval } });
    conversationSettings.approvalPolicy = approval;
    if (footerApprovalValue) footerApprovalValue.textContent = approval;
  }

  function openPicker(startPath) {
    if (!pickerOverlayEl) return;
    pickerPath = startPath || settingsCwdEl?.value || '~';
    pickerOverlayEl.classList.remove('hidden');
    fetchPicker(pickerPath);
  }

  function closePicker() {
    if (!pickerOverlayEl) return;
    pickerOverlayEl.classList.add('hidden');
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
      setActivity('loading rollout', true);
      const r = await fetch(`/api/appserver/rollouts/${encodeURIComponent(rolloutId)}/preview`, { cache: 'no-store' });
      if (!r.ok) throw new Error('failed to load rollout preview');
      const data = await r.json();
      resetTimeline();
      const items = Array.isArray(data?.items) ? data.items : [];
      items.forEach((entry) => {
        if (!entry || !entry.role || !entry.text) return;
        if (entry.role === 'reasoning') {
          appendReasoningDelta(entry.item_id || 'reasoning', entry.text + '\n');
          return;
        }
        if (entry.role === 'diff') {
          addDiff(entry.item_id || 'diff', entry.text || '');
          return;
        }
        addMessage(entry.role, entry.text);
      });
      if (Number.isFinite(data?.token_total)) {
        updateTokens(data.token_total);
      }
      pendingRollout = { id: rolloutId, items, token_total: data?.token_total ?? null };
      if (settingsRolloutEl) settingsRolloutEl.value = rolloutId;
      setActivity('rollout loaded', false);
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
      if (pickerPathEl) pickerPathEl.textContent = pickerPath;
      renderPickerList(data?.items || []);
    } catch {
      // ignore
    }
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
      deleteBtn.addEventListener('click', () => deleteConversation(meta.conversation_id));
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

  function insertRow(row) {
    clearPlaceholder();
    if (activityRow && activityRow.parentElement === timelineEl) {
      timelineEl.insertBefore(row, activityRow);
    } else {
      timelineEl.appendChild(row);
    }
    maybeAutoScroll();
  }

  function createRow(kind, title) {
    const row = document.createElement('div');
    row.className = `timeline-row ${kind || ''}`.trim();
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = title || '';
    const body = document.createElement('div');
    body.className = 'body';
    row.append(meta, body);
    insertRow(row);
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
  }

  function resetTimeline() {
    if (!timelineEl) return;
    timelineEl.innerHTML = '';
    assistantRows.clear();
    reasoningRows.clear();
    diffRows.clear();
    activityRow = null;
    activityTextEl = null;
    activityLineEl = null;
    placeholderCleared = false;
    messageCount = 0;
    tokenCount = 0;
    lastEventType = null;
    lastReasoningKey = null;
    setCounter(counterMessagesEl, messageCount);
    setCounter(counterTokensEl, tokenCount);
    const placeholder = document.createElement('div');
    placeholder.id = 'timeline-placeholder';
    placeholder.className = 'timeline-row muted';
    placeholder.textContent = 'Waiting for events...';
    timelineEl.appendChild(placeholder);
    ensureActivityRow();
    maybeAutoScroll(true);
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

  function getDiffRow(id) {
    const key = id || 'diff';
    let entry = diffRows.get(key);
    if (!entry) {
      const { body } = createRow('diff', 'diff');
      const pre = document.createElement('pre');
      pre.className = 'diff-block';
      body.append(pre);
      entry = { pre };
      diffRows.set(key, entry);
    }
    return entry;
  }

  function addDiff(id, text) {
    const entry = getDiffRow(id);
    entry.pre.innerHTML = formatDiff(text || '');
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
      } else if (line.startsWith('+++') || line.startsWith('---')) {
        cls = 'diff-file';
        display = `File: ${line.replace(/^(\+\+\+|---)\s+/, '')}`;
      } else if (line.startsWith('diff --git')) {
        cls = 'diff-meta';
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
    }).join('');
  }

  function renderApproval(evt) {
    const { body } = createRow(evt.kind === 'diff' ? 'diff' : 'approval', 'approval');
    const payload = evt.payload || {};
    const lines = [];
    if (payload.command) {
      lines.push(`<div><strong>Command:</strong> ${escapeHtml(Array.isArray(payload.command) ? payload.command.join(' ') : String(payload.command))}</div>`);
    }
    if (payload.cwd) {
      lines.push(`<div><strong>CWD:</strong> ${escapeHtml(String(payload.cwd))}</div>`);
    }
    if (payload.diff) {
      lines.push(`<pre>${escapeHtml(payload.diff)}</pre>`);
    }
    if (payload.changes && Array.isArray(payload.changes)) {
      payload.changes.forEach((change) => {
        if (change && change.diff) {
          lines.push(`<div><strong>${escapeHtml(change.path || 'file')}</strong></div><pre>${escapeHtml(change.diff)}</pre>`);
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
      actions.remove();
    });
    decline.addEventListener('click', async () => {
      await respondApproval(evt.id, 'decline');
      actions.remove();
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
    resetTimeline();
    await postJson('/api/appserver/conversations', {});
    await fetchConversation();
    await fetchConversations();
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
    const settings = {
      cwd,
      approvalPolicy: settingsApprovalEl?.value?.trim() || null,
      sandboxPolicy: settingsSandboxEl?.value?.trim() || null,
      model: settingsModelEl?.value?.trim() || null,
      effort: settingsEffortEl?.value?.trim() || null,
      summary: settingsSummaryEl?.value?.trim() || null,
      label: settingsLabelEl?.value?.trim() || null,
    };
    if (pendingNewConversation || !conversationMeta?.conversation_id) {
      const meta = await postJson('/api/appserver/conversations', {});
      if (meta?.conversation_id) {
        await postJson('/api/appserver/conversations/select', { conversation_id: meta.conversation_id, view: 'conversation' });
      }
      pendingNewConversation = false;
    }
    await postJson('/api/appserver/conversation', { settings });
    if (pendingRollout?.id && Array.isArray(pendingRollout.items)) {
      await postJson('/api/appserver/conversations/bind-rollout', {
        rollout_id: pendingRollout.id,
        items: pendingRollout.items,
      });
      pendingRollout = null;
    }
    closeSettingsModal();
    await fetchConversation();
    await fetchConversations();
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
    await postJson('/api/appserver/rpc', {
      id: requestId,
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
        setPill(statusEl, 'pinned', 'ok');
      } else {
        currentThreadId = null;
        setPill(statusEl, 'draft', 'warn');
      }
    } catch {
      setPill(statusEl, 'error', 'err');
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
      setPill(statusEl, 'pinned', 'ok');
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

  async function replayTranscript() {
    try {
      const r = await fetch('/api/appserver/transcript', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (!data || !Array.isArray(data.items)) return;
      data.items.forEach((entry) => {
        if (!entry || !entry.role || !entry.text) return;
        if (entry.role === 'reasoning') {
          appendReasoningDelta(entry.item_id || 'reasoning', entry.text + '\n');
          return;
        }
        if (entry.role === 'diff') {
          addDiff(entry.item_id || 'diff', entry.text || '');
          return;
        }
        addMessage(entry.role, entry.text);
      });
      lastEventType = null;
      maybeAutoScroll(true);
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
        addDiff(evt.id, evt.text || '');
        return;
      case 'approval':
        lastEventType = 'approval';
        renderApproval(evt);
        return;
      case 'token_count':
        lastEventType = 'token';
        updateTokens(evt.total);
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
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/appserver`;
    const ws = new WebSocket(wsUrl);
    setPill(wsStatusEl, 'connecting', 'warn');
    ws.onopen = () => {
      markWsOpen();
      setPill(wsStatusEl, 'connected', 'ok');
    };
    ws.onclose = () => {
      resetWsReady();
      setPill(wsStatusEl, 'closed', 'err');
      scheduleReconnect();
    };
    ws.onerror = () => {
      setPill(wsStatusEl, 'error', 'err');
      scheduleReconnect();
    };
    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        handleEvent(data);
      } catch {
        // ignore malformed
      }
    };
  }

  setPill(statusEl, 'idle', 'warn');
  ensureActivityRow();
  setCounter(counterMessagesEl, messageCount);
  setCounter(counterTokensEl, tokenCount);
  updateScrollButton();
  resetWsReady();
  connectWS();
  fetchConversation().then(async () => {
    await fetchConversations();
    if (activeView === 'conversation') {
      await replayTranscript();
      setDrawerOpen(true);
    } else {
      setDrawerOpen(false);
    }
  });
  fetchStatus();

  setupDropdown(settingsApprovalEl, settingsApprovalToggle, settingsApprovalOptions, [
    'never',
    'on-failure',
    'unlessTrusted',
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
      getPickerPath: () => pickerPath,
      setPickerPath: (val) => { pickerPath = val; },
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
    const text = promptEl?.value?.trim();
    if (!text) return;
    if (promptEl) promptEl.value = '';
    await sendUserMessage(text);
  });

  promptEl?.addEventListener('keydown', async (evt) => {
    if (evt.key === 'Enter' && !evt.shiftKey) {
      evt.preventDefault();
      const text = promptEl.value.trim();
      if (!text) return;
      promptEl.value = '';
      await sendUserMessage(text);
    }
  });

  interruptBtn?.addEventListener('click', async () => {
    await interruptTurn();
  });

  scrollContainer?.addEventListener('scroll', () => {
    const nearBottom = isNearBottom();
    if (autoScroll !== nearBottom) {
      autoScroll = nearBottom;
      updateScrollButton();
    }
  });

  scrollBtn?.addEventListener('click', () => {
    autoScroll = !autoScroll;
    updateScrollButton();
    if (autoScroll) maybeAutoScroll(true);
  });
});
