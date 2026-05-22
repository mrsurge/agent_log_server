import { getRpcRegistry } from '../registry.ts';
import {
  callRpcNamespace,
  readRpcTransportEnabledPreference,
  subscribeRpcNamespaceNotifications,
  type JsonRpcNotificationEnvelope,
  type RpcWindowRef,
} from '../transport.ts';
import {
  UI_RPC_ANCHOR_MODULES,
  UI_RPC_IMPLEMENTATION_STATUS,
  UI_RPC_METHODS,
  UI_RPC_NAMESPACE,
  UI_RPC_NOTIFICATION_METHODS,
  type JsonObject,
} from './contract.ts';

export interface UiRpcClientDescriptor {
  status: typeof UI_RPC_IMPLEMENTATION_STATUS;
  namespace: typeof UI_RPC_NAMESPACE;
  methods: typeof UI_RPC_METHODS;
  anchorModules: readonly string[];
  notificationCount: number;
}

interface UiRpcClientDeps {
  sioCall: (event: string, payload?: JsonObject, options?: JsonObject) => Promise<unknown>;
  windowRef?: RpcWindowRef;
}

type TransportTag = 'rpc' | 'legacy';

export function createUiRpcClientDescriptor(): UiRpcClientDescriptor {
  const registry = getRpcRegistry();
  return {
    status: UI_RPC_IMPLEMENTATION_STATUS,
    namespace: UI_RPC_NAMESPACE,
    methods: UI_RPC_METHODS,
    anchorModules: [...UI_RPC_ANCHOR_MODULES],
    notificationCount: registry.namespaces.ui.notifications.length,
  };
}

export const createUiRpcClientPlaceholder = createUiRpcClientDescriptor;
export type UiRpcClientPlaceholder = UiRpcClientDescriptor;

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as JsonObject;
}

function normalizeLegacyResult(result: unknown): JsonObject {
  const payload = asObject(result) ?? {};
  const legacyError = typeof payload.__error === 'string' ? payload.__error.trim() : '';
  if (legacyError) {
    throw new Error(legacyError);
  }
  return payload;
}

function normalizeTransport<T extends JsonObject>(result: T, transport: TransportTag): T & { transport: TransportTag } {
  return {
    ...result,
    transport,
  };
}

function listFromKey(result: unknown, key: string): JsonObject[] {
  const payload = asObject(result);
  const items = payload?.[key];
  if (!Array.isArray(items)) return [];
  return items.map((item) => asObject(item)).filter((item): item is JsonObject => Boolean(item));
}

function getWindowRef(windowRef?: RpcWindowRef): RpcWindowRef {
  return windowRef ?? (typeof window !== 'undefined' ? window : null);
}

function normalizeNotificationMethod(method: unknown): typeof UI_RPC_NOTIFICATION_METHODS[number] | null {
  if (typeof method !== 'string') return null;
  return UI_RPC_NOTIFICATION_METHODS.find((candidate) => candidate === method) ?? null;
}

export function createUiRpcClient(deps: UiRpcClientDeps) {
  function rpcEnabled(): boolean {
    return readRpcTransportEnabledPreference(getWindowRef(deps.windowRef));
  }

  async function callLegacy(event: string, payload: JsonObject = {}): Promise<JsonObject> {
    return normalizeLegacyResult(await deps.sioCall(event, payload));
  }

  async function getView(): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      const config = await callLegacy('get_config', {});
      return normalizeTransport(
        {
          active_view: typeof config.active_view === 'string' ? config.active_view : null,
          conversation_id: typeof config.conversation_id === 'string' ? config.conversation_id : null,
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.viewGet,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function setView(view: string): Promise<JsonObject & { transport: TransportTag }> {
    const payload = { view };
    if (!rpcEnabled()) {
      const result = await callLegacy('set_view', payload);
      return normalizeTransport(
        {
          active_view: typeof result.active_view === 'string' ? result.active_view : view,
          conversation_id: typeof result.conversation_id === 'string' ? result.conversation_id : null,
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.viewSet,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function getHostUi(): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('get_host_ui', {}), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.hostUiGet,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function recheckHostUi(): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      const recheck = await callLegacy('sidebar_recheck', {});
      const hostUi = await callLegacy('get_host_ui', {});
      return normalizeTransport({ recheck, ...hostUi }, 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.hostUiRecheck,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function listFilesystem(path?: string | null): Promise<JsonObject & { transport: TransportTag }> {
    const payload: JsonObject = {};
    if (typeof path === 'string' && path.trim()) payload.path = path.trim();
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('fs_list', payload), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.filesystemList,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function getFilesystemHome(): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      const listed = await callLegacy('fs_list', { path: '~' });
      const path = typeof listed.path === 'string' && listed.path.trim() ? listed.path.trim() : null;
      return normalizeTransport(
        {
          ok: Boolean(path),
          home: path,
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.filesystemHome,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function searchFilesystem(options: {
    query: string;
    root?: string | null;
    limit?: number;
  }): Promise<{ items: JsonObject[]; transport: TransportTag } & JsonObject> {
    const payload: JsonObject = {
      query: options.query,
    };
    if (typeof options.root === 'string' && options.root.trim()) payload.root = options.root.trim();
    if (Number.isFinite(options.limit)) payload.limit = Number(options.limit);
    if (!rpcEnabled()) {
      const legacy = await callLegacy('fs_search', payload);
      return normalizeTransport(
        {
          ...legacy,
          items: listFromKey(legacy, 'items'),
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.filesystemSearch,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(
      {
        ...(asObject(result) ?? {}),
        items: listFromKey(result, 'items'),
      },
      'rpc',
    );
  }

  async function getProjectSummary(options: {
    conversationId?: string | null;
    path?: string | null;
    maxDiffBytes?: number;
  } = {}): Promise<JsonObject & { transport: TransportTag }> {
    const payload: JsonObject = {};
    if (typeof options.conversationId === 'string' && options.conversationId.trim()) {
      payload.conversation_id = options.conversationId.trim();
    }
    if (typeof options.path === 'string' && options.path.trim()) payload.path = options.path.trim();
    if (Number.isFinite(options.maxDiffBytes)) payload.max_diff_bytes = Number(options.maxDiffBytes);
    if (!rpcEnabled()) {
      return normalizeTransport(
        {
          ok: false,
          error: 'Project summary requires /rpc/ui',
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.projectSummaryGet,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function openFile(payload: JsonObject): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('te2_agent_open', payload), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.fileOpen,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function openUrl(payload: { url: string; source?: string | null; conversation_id?: string | null }): Promise<JsonObject & { transport: TransportTag }> {
    const params: JsonObject = {
      url: payload.url,
    };
    if (typeof payload.source === 'string' && payload.source.trim()) params.source = payload.source.trim();
    if (typeof payload.conversation_id === 'string' && payload.conversation_id.trim()) {
      params.conversation_id = payload.conversation_id.trim();
    }
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('open_external_url', params), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: UI_RPC_NAMESPACE,
      method: UI_RPC_METHODS.urlOpen,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  function subscribeLiveNotifications(options: {
    onNotification: (
      method: typeof UI_RPC_NOTIFICATION_METHODS[number],
      params: JsonObject,
      notification: JsonRpcNotificationEnvelope<unknown>,
    ) => void;
    onError?: (error: unknown) => void;
    onConnectionChange?: (connected: boolean) => void;
  }): () => void {
    return subscribeRpcNamespaceNotifications({
      namespace: UI_RPC_NAMESPACE,
      windowRef: getWindowRef(deps.windowRef),
      onConnectionChange: (connected) => {
        options.onConnectionChange?.(connected);
      },
      onNotification: (notification) => {
        try {
          const method = normalizeNotificationMethod(notification.method);
          if (!method) return;
          options.onNotification(method, asObject(notification.params) ?? {}, notification);
        } catch (error) {
          options.onError?.(error);
        }
      },
    });
  }

  return {
    rpcEnabled,
    getView,
    setView,
    getHostUi,
    recheckHostUi,
    getFilesystemHome,
    listFilesystem,
    searchFilesystem,
    getProjectSummary,
    openFile,
    openUrl,
    subscribeLiveNotifications,
  };
}
