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
  const counterMessagesEl = document.getElementById('counter-messages');
  const counterTokensEl = document.getElementById('counter-tokens');
  const scrollBtn = document.getElementById('scroll-pin');
  const activeConversationEl = document.getElementById('active-conversation');
  const splashViewEl = document.getElementById('splash-view');
  const drawerEl = document.getElementById('conversation-drawer');
  const conversationListEl = document.getElementById('conversation-list');
  const conversationCreateBtn = document.getElementById('conversation-create');
  const conversationBackBtn = document.getElementById('conversation-back');

  localStorage.setItem('last_tab', 'codex-agent');

  let conversationMeta = {};
  let conversationSettings = {};
  let conversationList = [];
  let activeView = 'splash';
  let currentThreadId = null;
  let lastEventType = null;
  let lastReasoningKey = null;
  let initialized = false;
  let wsOpen = false;
  let wsReadyResolve = null;
  let wsReadyPromise = new Promise((resolve) => { wsReadyResolve = resolve; });
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
    const label = conversationMeta?.conversation_id || 'none';
    activeConversationEl.textContent = label;
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
      const title = document.createElement('div');
      title.textContent = meta.conversation_id || 'conversation';
      const sub = document.createElement('div');
      const threadText = meta.thread_id ? `thread: ${meta.thread_id}` : 'thread: (none)';
      const cwdText = meta.settings && meta.settings.cwd ? `cwd: ${meta.settings.cwd}` : 'cwd: (default)';
      const statusText = meta.status ? `status: ${meta.status}` : 'status: none';
      sub.textContent = `${threadText} • ${cwdText} • ${statusText}`;
      info.append(title, sub);

      const actions = document.createElement('div');
      actions.className = 'conversation-actions';
      const openBtn = document.createElement('button');
      openBtn.className = 'btn tiny primary';
      openBtn.textContent = 'Open';
      openBtn.addEventListener('click', () => selectConversation(meta.conversation_id));
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'btn tiny decline';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', () => deleteConversation(meta.conversation_id));
      actions.append(openBtn, deleteBtn);

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

      const sep = oldNo || newNo ? '|' : '';
      return `<span class=\"diff-line ${cls}\"><span class=\"diff-lineno\"><span class=\"ln old\">${escapeHtml(oldNo)}</span><span class=\"ln sep\">${escapeHtml(sep)}</span><span class=\"ln new\">${escapeHtml(newNo)}</span></span><span class=\"diff-gutter\">${escapeHtml(gutter)}</span><span class=\"diff-text\">${escapeHtml(display)}</span></span>`;
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
    if (!conversationId) return;
    resetTimeline();
    await postJson('/api/appserver/conversations/select', { conversation_id: conversationId });
    await fetchConversation();
    await fetchConversations();
    await replayTranscript();
    setDrawerOpen(true);
  }

  async function createConversation() {
    resetTimeline();
    await postJson('/api/appserver/conversations', {});
    await fetchConversation();
    await fetchConversations();
    await replayTranscript();
    setDrawerOpen(true);
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
    };
    ws.onerror = () => setPill(wsStatusEl, 'error', 'err');
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

  startBtn?.addEventListener('click', async () => {
    await postJson('/api/appserver/start', null);
    fetchStatus();
  });

  stopBtn?.addEventListener('click', async () => {
    await postJson('/api/appserver/stop', null);
    fetchStatus();
  });

  conversationCreateBtn?.addEventListener('click', async () => {
    await createConversation();
  });

  conversationBackBtn?.addEventListener('click', async () => {
    await setActiveView('splash');
    setDrawerOpen(false);
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
