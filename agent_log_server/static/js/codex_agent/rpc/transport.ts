import {
  RPC_NAMESPACES,
  type RpcNamespaceName,
} from './namespaces.ts';

export const RPC_REQUEST_EVENT = 'rpc' as const;
export const RPC_NOTIFICATION_EVENT = 'rpc.notify' as const;
export const RPC_TRANSPORT_SESSION_STORAGE_KEY = 'codex_rpc_transport_enabled' as const;

export interface RpcTransportPlaceholderDescriptor {
  status: 'placeholder';
  compatibilityNamespace: string;
  requestEvent: typeof RPC_REQUEST_EVENT;
  notificationEvent: typeof RPC_NOTIFICATION_EVENT;
  defaultRpcEnabled: boolean;
  sessionStorageKey: typeof RPC_TRANSPORT_SESSION_STORAGE_KEY;
  publicNamespaces: {
    conversations: string;
    settings: string;
    ui: string;
  };
}

type SessionStorageWindow = Pick<Window, 'sessionStorage'> | null | undefined;
type LocationWindow = Pick<Window, 'location'> | null | undefined;
type RpcWindowRef = (Pick<Window, 'location' | 'sessionStorage'> & { io?: IoFactory }) | null | undefined;

interface SocketLike {
  connected?: boolean;
  emit(event: string, payload: unknown, ack?: (response: unknown) => void): void;
  on(event: string, handler: (...args: any[]) => void): void;
  off?(event: string, handler: (...args: any[]) => void): void;
}

type IoFactory = (namespace: string, options: Record<string, unknown>) => SocketLike;

export interface JsonRpcSuccessEnvelope<TResult = unknown> {
  jsonrpc: '2.0';
  id: string;
  result: TResult;
}

export interface JsonRpcErrorEnvelope {
  jsonrpc: '2.0';
  id: string | null;
  error: {
    code: number;
    message: string;
    data?: Record<string, unknown>;
  };
}

export interface CallRpcNamespaceOptions {
  namespace: string;
  method: string;
  params?: Record<string, unknown>;
  requestId?: string;
  timeoutMs?: number;
  windowRef?: RpcWindowRef;
}

const namespaceSockets = new Map<string, SocketLike>();
let rpcRequestCounter = 0;

function getSessionStorage(win: SessionStorageWindow): Storage | null {
  if (!win) return null;
  try {
    return win.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function getDefaultWindowRef(): RpcWindowRef {
  return typeof window !== 'undefined' ? window : null;
}

function getIoFactory(win: RpcWindowRef): IoFactory {
  const factory = win?.io ?? (globalThis as { io?: IoFactory }).io;
  if (typeof factory !== 'function') {
    throw new Error('Socket.IO client unavailable');
  }
  return factory;
}

function resolveSocketIoPath(win: LocationWindow): string {
  const pathname = win?.location?.pathname || '/';
  const proxiedMatch = pathname.match(/^\/api\/app\/[^/]+\/proxy\b/);
  if (proxiedMatch) {
    return `${proxiedMatch[0]}/socket.io`;
  }
  return '/socket.io';
}

function getNamespaceSocket(namespace: string, win: RpcWindowRef): SocketLike {
  const existing = namespaceSockets.get(namespace);
  if (existing) return existing;
  const ioFactory = getIoFactory(win);
  const socket = ioFactory(namespace, {
    path: resolveSocketIoPath(win),
    transports: ['websocket'],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 500,
    reconnectionDelayMax: 5000,
  });
  namespaceSockets.set(namespace, socket);
  return socket;
}

function removeSocketListener(
  socket: SocketLike,
  event: string,
  handler: (...args: any[]) => void,
): void {
  if (typeof socket.off === 'function') {
    socket.off(event, handler);
  }
}

function nextRpcRequestId(): string {
  rpcRequestCounter += 1;
  return `rpc-${Date.now()}-${rpcRequestCounter}`;
}

export class JsonRpcCallError extends Error {
  code: number;
  data: Record<string, unknown> | null;

  constructor(code: number, message: string, data?: Record<string, unknown>) {
    super(message);
    this.name = 'JsonRpcCallError';
    this.code = code;
    this.data = data ?? null;
  }
}

export function normalizeRpcTransportEnabled(value: unknown): boolean {
  if (value === false) return false;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return true;
    if (normalized === '0' || normalized === 'false' || normalized === 'legacy' || normalized === 'off') {
      return false;
    }
  }
  return true;
}

export function readRpcTransportEnabledPreference(
  win: SessionStorageWindow = getDefaultWindowRef(),
): boolean {
  const storage = getSessionStorage(win);
  if (!storage) return true;
  try {
    return normalizeRpcTransportEnabled(storage.getItem(RPC_TRANSPORT_SESSION_STORAGE_KEY));
  } catch {
    return true;
  }
}

export function writeRpcTransportEnabledPreference(
  enabled: unknown,
  win: SessionStorageWindow = getDefaultWindowRef(),
): boolean {
  const normalized = normalizeRpcTransportEnabled(enabled);
  const storage = getSessionStorage(win);
  if (!storage) return normalized;
  try {
    storage.setItem(RPC_TRANSPORT_SESSION_STORAGE_KEY, normalized ? '1' : '0');
  } catch {
    // Ignore storage failures; the in-memory UI state still works.
  }
  return normalized;
}

export function resolveRpcNamespace(name: RpcNamespaceName): string {
  return RPC_NAMESPACES[name];
}

export async function waitForRpcNamespace(
  namespace: string,
  options: {
    timeoutMs?: number;
    windowRef?: RpcWindowRef;
  } = {},
): Promise<SocketLike> {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
  const win = options.windowRef ?? getDefaultWindowRef();
  const socket = getNamespaceSocket(namespace, win);
  if (socket.connected) {
    return socket;
  }

  return await new Promise<SocketLike>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      removeSocketListener(socket, 'connect', onConnect);
      removeSocketListener(socket, 'connect_error', onConnectError);
      reject(new Error(`Timed out connecting to ${namespace}`));
    }, timeoutMs);

    const onConnect = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      removeSocketListener(socket, 'connect', onConnect);
      removeSocketListener(socket, 'connect_error', onConnectError);
      resolve(socket);
    };

    const onConnectError = (error: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      removeSocketListener(socket, 'connect', onConnect);
      removeSocketListener(socket, 'connect_error', onConnectError);
      reject(error instanceof Error ? error : new Error(String(error || `Failed connecting to ${namespace}`)));
    };

    socket.on('connect', onConnect);
    socket.on('connect_error', onConnectError);
    if (socket.connected) {
      onConnect();
    }
  });
}

export async function callRpcNamespace<TResult = unknown>(
  options: CallRpcNamespaceOptions,
): Promise<TResult> {
  const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
  const requestId = options.requestId || nextRpcRequestId();
  const socket = await waitForRpcNamespace(options.namespace, {
    timeoutMs,
    windowRef: options.windowRef,
  });

  return await new Promise<TResult>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`Timed out waiting for ${options.method}`));
    }, timeoutMs);

    socket.emit(
      RPC_REQUEST_EVENT,
      {
        jsonrpc: '2.0',
        id: requestId,
        method: options.method,
        params: options.params ?? {},
      },
      (ack: unknown) => {
        clearTimeout(timer);
        if (!ack || typeof ack !== 'object') {
          reject(new Error(`Invalid RPC ack for ${options.method}`));
          return;
        }
        if ('__error' in (ack as Record<string, unknown>)) {
          reject(new Error(String((ack as Record<string, unknown>).__error || `RPC failed: ${options.method}`)));
          return;
        }
        const envelope = ack as JsonRpcSuccessEnvelope<TResult> | JsonRpcErrorEnvelope;
        if (envelope.jsonrpc !== '2.0') {
          reject(new Error(`Invalid JSON-RPC envelope for ${options.method}`));
          return;
        }
        if ('error' in envelope) {
          reject(new JsonRpcCallError(
            envelope.error.code,
            envelope.error.message || `RPC failed: ${options.method}`,
            envelope.error.data,
          ));
          return;
        }
        resolve(envelope.result);
      },
    );
  });
}

export function describeRpcTransportPlaceholder(): RpcTransportPlaceholderDescriptor {
  return {
    status: 'placeholder',
    compatibilityNamespace: RPC_NAMESPACES.legacyAppserver,
    requestEvent: RPC_REQUEST_EVENT,
    notificationEvent: RPC_NOTIFICATION_EVENT,
    defaultRpcEnabled: true,
    sessionStorageKey: RPC_TRANSPORT_SESSION_STORAGE_KEY,
    publicNamespaces: {
      conversations: RPC_NAMESPACES.conversations,
      settings: RPC_NAMESPACES.settings,
      ui: RPC_NAMESPACES.ui,
    },
  };
}
