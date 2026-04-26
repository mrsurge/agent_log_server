import type { createConversationsRpcClient } from '../rpc/conversations/client.ts';

interface AppserverSocket {
  connected?: boolean;
}

interface WsState {
  wsOpen: boolean;
  wsReconnectDelay?: number;
  wsReadyResolve?: ((ready: boolean) => void) | null;
  wsReadyPromise: Promise<boolean>;
}

interface BindSocketEventsContext {
  getWsState: () => WsState;
  setWsState: (patch: Partial<WsState>) => void;
  setSocket: (socket: AppserverSocket | null) => void;
  wsStatusEl?: HTMLElement | null;
  setPill: (element: HTMLElement | null, label: string, tone?: string) => void;
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

  function connectWS(onEvent?: (event: unknown) => void) {
    if (!conversationsRpcClient || typeof conversationsRpcClient.subscribeLiveNotifications !== 'function') {
      setPill(wsStatusEl ?? null, 'no-rpc', 'err');
      return;
    }
    if (typeof unsubscribeRpcNotifications === 'function') {
      unsubscribeRpcNotifications();
      unsubscribeRpcNotifications = null;
    }
    setPill(wsStatusEl ?? null, '…', 'warn');
    setSocket(null);
    try {
      unsubscribeRpcNotifications = conversationsRpcClient.subscribeLiveNotifications({
        onConnectionChange: (connected) => {
          if (connected) {
            markWsOpen();
            setPill(wsStatusEl ?? null, '👍', 'ok');
            syncDraftFromServer(getConversationId());
            return;
          }
          resetWsReady();
          setPill(wsStatusEl ?? null, '👎', 'err');
        },
        onError: (error) => {
          console.warn('conversation rpc notify error', error);
        },
        onEvent: (event) => {
          if (typeof onEvent === 'function') onEvent(event);
        },
      });
    } catch (error) {
      console.warn('conversation rpc notify subscribe failed', error);
      resetWsReady();
      setPill(wsStatusEl ?? null, '👎', 'err');
    }
  }

  return {
    resetWsReady,
    markWsOpen,
    waitForWs,
    connectWS,
  };
}
