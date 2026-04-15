import { getRpcRegistry } from '../registry.ts';
import {
  callRpcNamespace,
  JsonRpcCallError,
  readRpcTransportEnabledPreference,
  type RpcWindowRef,
} from '../transport.ts';
import {
  SETTINGS_RPC_ANCHOR_MODULES,
  SETTINGS_RPC_IMPLEMENTATION_STATUS,
  SETTINGS_RPC_METHODS,
  SETTINGS_RPC_NAMESPACE,
  type JsonObject,
} from './contract.ts';

export interface SettingsRpcClientDescriptor {
  status: typeof SETTINGS_RPC_IMPLEMENTATION_STATUS;
  namespace: typeof SETTINGS_RPC_NAMESPACE;
  methods: typeof SETTINGS_RPC_METHODS;
  anchorModules: readonly string[];
  notificationCount: number;
}

interface SettingsRpcClientDeps {
  sioCall: (event: string, payload?: JsonObject, options?: JsonObject) => Promise<unknown>;
  windowRef?: RpcWindowRef;
}

type TransportTag = 'rpc' | 'legacy';

export function createSettingsRpcClientDescriptor(): SettingsRpcClientDescriptor {
  const registry = getRpcRegistry();
  return {
    status: SETTINGS_RPC_IMPLEMENTATION_STATUS,
    namespace: SETTINGS_RPC_NAMESPACE,
    methods: SETTINGS_RPC_METHODS,
    anchorModules: [...SETTINGS_RPC_ANCHOR_MODULES],
    notificationCount: registry.namespaces.settings.notifications.length,
  };
}

export const createSettingsRpcClientPlaceholder = createSettingsRpcClientDescriptor;
export type SettingsRpcClientPlaceholder = SettingsRpcClientDescriptor;

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as JsonObject;
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

function normalizeLegacyResult(result: unknown): JsonObject {
  const payload = asObject(result) ?? {};
  const legacyError = typeof payload.__error === 'string' ? payload.__error.trim() : '';
  if (legacyError) {
    throw new Error(legacyError);
  }
  return payload;
}

function getWindowRef(windowRef?: RpcWindowRef): RpcWindowRef {
  return windowRef ?? (typeof window !== 'undefined' ? window : null);
}

export function createSettingsRpcClient(deps: SettingsRpcClientDeps) {
  function rpcEnabled(): boolean {
    return readRpcTransportEnabledPreference(getWindowRef(deps.windowRef));
  }

  async function callLegacy(event: string, payload: JsonObject = {}): Promise<JsonObject> {
    return normalizeLegacyResult(await deps.sioCall(event, payload));
  }

  async function getConfig(): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('get_config', {}), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.configGet,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function updateConfig(patch: JsonObject): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('update_config', patch), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.configUpdate,
      params: patch,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function listExtensions(): Promise<{ extensions: JsonObject[]; transport: TransportTag } & JsonObject> {
    if (!rpcEnabled()) {
      const legacy = await callLegacy('get_extensions', {});
      return normalizeTransport(
        {
          ...legacy,
          extensions: listFromKey(legacy, 'extensions'),
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionsList,
      params: {},
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(
      {
        ...(asObject(result) ?? {}),
        extensions: listFromKey(result, 'extensions'),
      },
      'rpc',
    );
  }

  async function reloadExtensions(payload: JsonObject = {}): Promise<JsonObject & { transport: TransportTag }> {
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('extensions_reload', payload), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionsReload,
      params: payload,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function getExtensionSettingsSchema(extensionId: string): Promise<JsonObject & { transport: TransportTag }> {
    const params = { extension_id: extensionId };
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('get_extension_settings_schema', params), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionSettingsSchemaGet,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function getRuntimeOptions(options: {
    conversationId?: string | null;
    agent?: string | null;
  }): Promise<JsonObject & { transport: TransportTag }> {
    const params: JsonObject = {
      conversation_id: options.conversationId ?? null,
      agent: options.agent ?? null,
    };
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('get_runtime_options', params), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionRuntimeOptionsGet,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  async function listExtensionModels(options: {
    extensionId: string;
  }): Promise<{ models: JsonObject[]; transport: TransportTag } & JsonObject> {
    const params = { extension_id: options.extensionId };
    if (!rpcEnabled()) {
      const rawLegacy = await deps.sioCall('get_extension_models', params);
      const legacy = Array.isArray(rawLegacy) ? null : normalizeLegacyResult(rawLegacy);
      const models = Array.isArray(rawLegacy)
        ? rawLegacy.map((item) => asObject(item)).filter((item): item is JsonObject => Boolean(item))
        : Array.isArray(legacy?.models)
          ? legacy.models.map((item) => asObject(item)).filter((item): item is JsonObject => Boolean(item))
          : [];
      return normalizeTransport(
        {
          ...(legacy ?? {}),
          models,
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionModelsList,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(
      {
        ...(asObject(result) ?? {}),
        models: listFromKey(result, 'models'),
      },
      'rpc',
    );
  }

  async function listExtensionSessions(options: {
    extensionId: string;
    cwd?: string | null;
  }): Promise<{ sessions: JsonObject[]; transport: TransportTag } & JsonObject> {
    const params: JsonObject = {
      extension_id: options.extensionId,
    };
    if (typeof options.cwd === 'string' && options.cwd.trim()) {
      params.cwd = options.cwd.trim();
    }
    if (!rpcEnabled()) {
      const rawLegacy = await deps.sioCall('get_sessions', params);
      const legacy = Array.isArray(rawLegacy) ? null : normalizeLegacyResult(rawLegacy);
      const sessions = Array.isArray(rawLegacy)
        ? rawLegacy.map((item) => asObject(item)).filter((item): item is JsonObject => Boolean(item))
        : listFromKey(legacy, 'sessions');
      return normalizeTransport(
        {
          ...(legacy ?? {}),
          sessions,
        },
        'legacy',
      );
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionSessionsList,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(
      {
        ...(asObject(result) ?? {}),
        sessions: listFromKey(result, 'sessions'),
      },
      'rpc',
    );
  }

  async function bindExtensionSession(options: {
    extensionId: string;
    sessionId: string;
    conversationId?: string | null;
    cwd?: string | null;
    model?: string | null;
    settings?: JsonObject | null;
  }): Promise<JsonObject & { transport: TransportTag }> {
    const params: JsonObject = {
      extension_id: options.extensionId,
      session_id: options.sessionId,
    };
    if (typeof options.conversationId === 'string' && options.conversationId.trim()) {
      params.conversation_id = options.conversationId.trim();
    }
    if (typeof options.cwd === 'string' && options.cwd.trim()) params.cwd = options.cwd.trim();
    if (typeof options.model === 'string' && options.model.trim()) params.model = options.model.trim();
    if (options.settings && typeof options.settings === 'object') params.settings = options.settings;
    if (!rpcEnabled()) {
      return normalizeTransport(await callLegacy('session_resume', params), 'legacy');
    }
    const result = await callRpcNamespace<JsonObject>({
      namespace: SETTINGS_RPC_NAMESPACE,
      method: SETTINGS_RPC_METHODS.extensionSessionBind,
      params,
      windowRef: getWindowRef(deps.windowRef),
    });
    return normalizeTransport(asObject(result) ?? {}, 'rpc');
  }

  return {
    rpcEnabled,
    getConfig,
    updateConfig,
    listExtensions,
    reloadExtensions,
    getExtensionSettingsSchema,
    getRuntimeOptions,
    listExtensionModels,
    listExtensionSessions,
    bindExtensionSession,
    JsonRpcCallError,
  };
}
