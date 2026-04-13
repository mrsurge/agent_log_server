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
    conversationsRpcClient,
    isRpcTransportEnabled,
  } = ctx;
  let unsubscribeRpcNotifications = null;
  let rpcNotificationsReady = false;

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
    if (typeof unsubscribeRpcNotifications === 'function') {
      unsubscribeRpcNotifications();
      unsubscribeRpcNotifications = null;
    }
    if (conversationsRpcClient && typeof conversationsRpcClient.subscribeLiveNotifications === 'function') {
      try {
        unsubscribeRpcNotifications = conversationsRpcClient.subscribeLiveNotifications({
          enabled: () => (typeof isRpcTransportEnabled === 'function' ? Boolean(isRpcTransportEnabled()) : false),
          onConnectionChange: (connected) => {
            rpcNotificationsReady = connected === true;
          },
          onError: (error) => {
            console.warn('conversation rpc notify error', error);
          },
          onEvent: (event) => {
            if (typeof onEvent === 'function') onEvent(event);
          },
        });
      } catch (error) {
        rpcNotificationsReady = false;
        console.warn('conversation rpc notify subscribe failed', error);
      }
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
      if (
        typeof isRpcTransportEnabled === 'function'
        && isRpcTransportEnabled()
        && rpcNotificationsReady
        && conversationsRpcClient
        && typeof conversationsRpcClient.isRpcBackedLiveEvent === 'function'
        && conversationsRpcClient.isRpcBackedLiveEvent(data)
      ) {
        return;
      }
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
