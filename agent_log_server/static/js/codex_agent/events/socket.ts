import type { createConversationsRpcClient } from '../rpc/conversations/client.ts';

type IoEventHandler = (...args: unknown[]) => void;
type ConversationsLiveTransport = 'legacy' | 'rpc';

interface AppserverSocket {
  connected?: boolean;
  emit(event: string, payload?: unknown, ack?: (response: unknown) => void): void;
  on(event: string, handler: IoEventHandler): void;
}

type IoFactory = (namespace: string, options: Record<string, unknown>) => AppserverSocket;

declare const io: IoFactory | undefined;

interface WsState {
  wsOpen: boolean;
  wsReconnectDelay?: number;
  wsReadyResolve?: ((ready: boolean) => void) | null;
  wsReadyPromise: Promise<boolean>;
}

interface BindSocketEventsContext {
  getWsState: () => WsState;
  setWsState: (patch: Partial<WsState>) => void;
  setSocket: (socket: AppserverSocket) => void;
  wsStatusEl?: Element | null;
  setPill: (element: Element | null | undefined, label: string, tone: string) => void;
  syncDraftFromServer: (conversationId: string | null | undefined) => void;
  getConversationId: () => string | null | undefined;
  getWindow: () => Window;
  conversationsRpcClient?: ReturnType<typeof createConversationsRpcClient> | null;
  isRpcTransportEnabled?: () => boolean;
}

export function bindSocketEvents(ctx: BindSocketEventsContext) {
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
  let unsubscribeRpcNotifications: (() => void) | null = null;
  let rpcNotificationsReady = false;
  let currentSocket: AppserverSocket | null = null;
  let lastSyncedConversationsLiveTransport: ConversationsLiveTransport | null = null;

  function resolveConversationsLiveTransport(): ConversationsLiveTransport {
    const rpcEnabled = typeof isRpcTransportEnabled === 'function' ? Boolean(isRpcTransportEnabled()) : false;
    return rpcEnabled && rpcNotificationsReady ? 'rpc' : 'legacy';
  }

  function syncConversationsLiveTransportOwnership(): void {
    if (!currentSocket?.connected) {
      return;
    }
    const nextTransport = resolveConversationsLiveTransport();
    if (lastSyncedConversationsLiveTransport === nextTransport) {
      return;
    }
    currentSocket.emit('set_conversations_live_transport', {
      transport: nextTransport,
    });
    lastSyncedConversationsLiveTransport = nextTransport;
  }

  function resetWsReady() {
    let wsReadyResolve: ((ready: boolean) => void) | null = null;
    const wsReadyPromise = new Promise<boolean>((resolve) => { wsReadyResolve = resolve; });
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
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<boolean>((resolve) => {
      timer = setTimeout(() => resolve(false), timeoutMs);
    });
    const ok = await Promise.race([state.wsReadyPromise, timeout]);
    if (timer) clearTimeout(timer);
    return Boolean(ok);
  }

  function socketIoPath() {
    const win = getWindow();
    const m = win.location.pathname.match(/^(\/api\/app\/[^/]+\/proxy)\//);
    if (m && m[1]) return `${m[1]}/socket.io`;
    return '/socket.io';
  }

  function connectWS(onEvent?: (event: unknown) => void) {
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
            syncConversationsLiveTransportOwnership();
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
        syncConversationsLiveTransportOwnership();
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
    currentSocket = sock;
    setSocket(sock);
    sock.on('connect', () => {
      markWsOpen();
      lastSyncedConversationsLiveTransport = null;
      setPill(wsStatusEl, '👍', 'ok');
      syncDraftFromServer(getConversationId());
      syncConversationsLiveTransportOwnership();
    });
    sock.on('disconnect', () => {
      resetWsReady();
      lastSyncedConversationsLiveTransport = null;
      setPill(wsStatusEl, '👎', 'err');
    });
    sock.on('connect_error', () => {
      resetWsReady();
      lastSyncedConversationsLiveTransport = null;
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
