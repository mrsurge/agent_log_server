(() => {
  const statusEl = document.getElementById('appserver-status');
  const wsStatusEl = document.getElementById('ws-status');
  const timelineEl = document.getElementById('timeline');
  const approvalsEl = document.getElementById('approvals-list');
  const diffsEl = document.getElementById('diffs-list');
  const threadListEl = document.getElementById('thread-list');
  const startBtn = document.getElementById('appserver-start');
  const stopBtn = document.getElementById('appserver-stop');
  const promptEl = document.getElementById('prompt');
  const sendBtn = document.getElementById('turn-send');

  let ws;
  let initialized = false;
  let rpcId = 1;

  function setPill(el, text, cls) {
    el.textContent = text;
    el.className = `pill ${cls || ''}`.trim();
  }

  function appendTimeline(text, kind = 'info') {
    const div = document.createElement('div');
    div.textContent = text;
    div.className = `timeline-item ${kind}`;
    timelineEl.appendChild(div);
    timelineEl.scrollTop = timelineEl.scrollHeight;
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

  async function fetchStatus() {
    try {
      const r = await fetch('/api/appserver/status', { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      if (data.running) {
        setPill(statusEl, 'running', 'ok');
      } else {
        setPill(statusEl, 'disconnected', 'warn');
      }
    } catch {
      setPill(statusEl, 'error', 'err');
    }
  }

  async function fetchConfig() {
    const r = await fetch('/api/appserver/config', { cache: 'no-store' });
    if (!r.ok) return {};
    return r.json();
  }

  function nextRpcId() {
    const id = rpcId;
    rpcId += 1;
    return id;
  }

  async function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function ensureInitialized() {
    if (initialized) return;
    try {
      await postJson('/api/appserver/initialize', null);
    } catch {
      // ignore init failures; app-server may already be initialized
    }
    initialized = true;
  }

  async function ensureThreadId() {
    const cfg = await fetchConfig();
    if (cfg.thread_id) return cfg.thread_id;
    await postJson('/api/appserver/rpc', {
      id: nextRpcId(),
      method: 'thread/start',
      params: {},
    });
    for (let i = 0; i < 20; i += 1) {
      await sleep(200);
      const nextCfg = await fetchConfig();
      if (nextCfg.thread_id) return nextCfg.thread_id;
    }
    return null;
  }

  async function sendPrompt() {
    const text = promptEl?.value?.trim();
    if (!text) return;
    promptEl.value = '';
    try {
      await postJson('/api/appserver/start', null);
      await ensureInitialized();
      let threadId = await ensureThreadId();
      if (!threadId) {
        appendTimeline('Unable to obtain thread id', 'error');
        return;
      }
      await postJson('/api/appserver/rpc', {
        id: nextRpcId(),
        method: 'turn/start',
        params: {
          threadId,
          input: [{ type: 'text', text }],
        },
      });
    } catch (err) {
      appendTimeline(`Send failed: ${err}`, 'error');
    }
  }

  function connectWS() {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/appserver?mode=raw`;
    ws = new WebSocket(wsUrl);

    setPill(wsStatusEl, 'connecting', 'warn');

    ws.onopen = () => setPill(wsStatusEl, 'connected', 'ok');
    ws.onclose = () => setPill(wsStatusEl, 'closed', 'err');
    ws.onerror = () => setPill(wsStatusEl, 'error', 'err');

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        appendTimeline(`[${msg.method || 'notify'}] ${JSON.stringify(msg.params || msg)}`);
      } catch {
        appendTimeline(evt.data);
      }
    };
  }

  connectWS();
  setPill(statusEl, 'disconnected', 'warn');
  fetchStatus();

  startBtn?.addEventListener('click', async () => {
    try {
      await postJson('/api/appserver/start', null);
      fetchStatus();
    } catch (err) {
      appendTimeline(`Start failed: ${err}`, 'error');
    }
  });

  stopBtn?.addEventListener('click', async () => {
    try {
      await postJson('/api/appserver/stop', null);
      fetchStatus();
    } catch (err) {
      appendTimeline(`Stop failed: ${err}`, 'error');
    }
  });

  sendBtn?.addEventListener('click', async () => {
    await sendPrompt();
  });

  promptEl?.addEventListener('keydown', async (evt) => {
    if (evt.key === 'Enter' && !evt.shiftKey) {
      evt.preventDefault();
      await sendPrompt();
    }
  });
})();
