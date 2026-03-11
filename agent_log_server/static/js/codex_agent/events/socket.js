export function bindSocketEvents(ctx) {
  const {
    getWsState,
    setWsState,
    setSocket,
    wsStatusEl,
    setPill,
    syncDraftFromServer,
    getConversationId,
    getWindow,
  } = ctx;

  function resetWsReady() {
    let wsReadyResolve = null;
    const wsReadyPromise = new Promise((resolve) => { wsReadyResolve = resolve; });
    setWsState({
      wsOpen: false,
      wsReadyResolve,
      wsReadyPromise,
    });
  }

  function markWsOpen() {
    const state = getWsState();
    setWsState({
      wsOpen: true,
      wsReconnectDelay: 1000,
    });
    if (state.wsReadyResolve) {
      state.wsReadyResolve(true);
      setWsState({ wsReadyResolve: null });
    }
  }

  async function waitForWs(timeoutMs = 3000) {
    const state = getWsState();
    if (state.wsOpen) return true;
    let timer;
    const timeout = new Promise((resolve) => {
      timer = setTimeout(() => resolve(false), timeoutMs);
    });
    const ok = await Promise.race([state.wsReadyPromise, timeout]);
    clearTimeout(timer);
    return Boolean(ok);
  }

  function socketIoPath() {
    const win = getWindow();
    const m = win.location.pathname.match(/^(\/api\/app\/[^/]+\/proxy)\//);
    if (m && m[1]) return `${m[1]}/socket.io`;
    return '/socket.io';
  }

  function connectWS(onEvent) {
    if (typeof io === 'undefined') {
      setPill(wsStatusEl, 'no-io', 'err');
      return;
    }
    setPill(wsStatusEl, '…', 'warn');
    const sock = io('/appserver', {
      path: socketIoPath(),
      transports: ['websocket'],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 500,
      reconnectionDelayMax: 5000,
    });
    setSocket(sock);
    sock.on('connect', () => {
      markWsOpen();
      setPill(wsStatusEl, '👍', 'ok');
      syncDraftFromServer(getConversationId());
    });
    sock.on('disconnect', () => {
      resetWsReady();
      setPill(wsStatusEl, '👎', 'err');
    });
    sock.on('connect_error', () => {
      resetWsReady();
      setPill(wsStatusEl, '👎', 'err');
    });
    sock.on('appserver_event', (data) => {
      if (typeof onEvent === 'function') onEvent(data);
    });
  }

  return {
    resetWsReady,
    markWsOpen,
    waitForWs,
    connectWS,
  };
}
