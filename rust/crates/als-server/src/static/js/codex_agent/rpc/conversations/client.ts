import { getRpcRegistry } from '../registry.ts';
import {
  callRpcNamespace,
  type JsonRpcNotificationEnvelope,
  type RpcWindowRef,
  subscribeRpcNamespaceNotifications,
} from '../transport.ts';
import {
  type ConversationDraftResult,
  type ConversationForkResult,
  type ConversationListResult,
  type ConversationMetaRecord,
  type ConversationMetaResult,
  CONVERSATIONS_RPC_ANCHOR_MODULES,
  CONVERSATIONS_RPC_CANONICAL_EVENT_TYPE_BY_METHOD,
  CONVERSATIONS_RPC_IMPLEMENTATION_STATUS,
  CONVERSATIONS_RPC_METHODS,
  CONVERSATIONS_RPC_NAMESPACE,
  CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE,
  type ConversationControlResult,
  type ConversationSendResult,
  type ConversationsLiveEvent,
  type ConversationsRpcNotificationMethod,
  type JsonObject,
  type ReplayChunkResult,
} from './contract.ts';

export interface ConversationsRpcClientDescriptor {
  status: typeof CONVERSATIONS_RPC_IMPLEMENTATION_STATUS;
  namespace: typeof CONVERSATIONS_RPC_NAMESPACE;
  methods: typeof CONVERSATIONS_RPC_METHODS;
  anchorModules: readonly string[];
  notificationCount: number;
}

interface ConversationsRpcClientDeps {
  sioCall?: (event: string, payload?: JsonObject, options?: JsonObject) => Promise<unknown>;
  windowRef?: RpcWindowRef;
}

export function createConversationsRpcClientDescriptor(): ConversationsRpcClientDescriptor {
  const registry = getRpcRegistry();
  return {
    status: CONVERSATIONS_RPC_IMPLEMENTATION_STATUS,
    namespace: CONVERSATIONS_RPC_NAMESPACE,
    methods: CONVERSATIONS_RPC_METHODS,
    anchorModules: [...CONVERSATIONS_RPC_ANCHOR_MODULES],
    notificationCount: registry.namespaces.conversations.notifications.length,
  };
}

export const createConversationsRpcClientPlaceholder = createConversationsRpcClientDescriptor;
export type ConversationsRpcClientPlaceholder = ConversationsRpcClientDescriptor;

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as JsonObject;
}

function hasOwn<T extends object>(value: T, key: PropertyKey): key is keyof T {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function normalizeEventType(eventType: unknown): string {
  return typeof eventType === 'string' ? eventType.trim().toLowerCase() : '';
}

export function getConversationsRpcNotificationMethodForEventType(
  eventType: unknown,
): ConversationsRpcNotificationMethod | null {
  const normalized = normalizeEventType(eventType);
  if (!normalized || !hasOwn(CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE, normalized)) {
    return null;
  }
  return CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE[normalized];
}

export function isConversationsRpcBackedEventType(eventType: unknown): boolean {
  return Boolean(getConversationsRpcNotificationMethodForEventType(eventType));
}

function parseReplayJsonl(jsonl: string): JsonObject[] {
  if (!jsonl) return [];
  return jsonl
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parsed = JSON.parse(line);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Invalid replay JSONL record');
      }
      return parsed as JsonObject;
    });
}

function normalizeConversationSendResult(
  result: unknown,
  transport: 'rpc' | 'legacy',
  conversationId: string | null,
): ConversationSendResult {
  const payload = asObject(result) ?? {};
  return {
    ...payload,
    conversation_id: typeof payload.conversation_id === 'string' ? payload.conversation_id : conversationId,
    accepted: payload.accepted === true || (payload.accepted == null && payload.ok === true),
    transport,
  };
}

function normalizeConversationMetaResult(
  result: unknown,
  transport: 'rpc' | 'legacy',
): ConversationMetaResult {
  const payload = asObject(result) ?? {};
  return {
    ...payload,
    conversation_id: typeof payload.conversation_id === 'string' ? payload.conversation_id : null,
    active_view: typeof payload.active_view === 'string' ? payload.active_view : null,
    settings: asObject(payload.settings) ?? {},
    transport,
  };
}

function normalizeConversationListResult(
  result: unknown,
  transport: 'rpc' | 'legacy',
): ConversationListResult {
  const payload = asObject(result) ?? {};
  const items = Array.isArray(payload.items)
    ? payload.items.map((item) => asObject(item)).filter((item): item is ConversationMetaRecord => Boolean(item))
    : [];
  const pinned = Array.isArray(payload.pinned_conversations)
    ? payload.pinned_conversations.filter((item): item is string => typeof item === 'string')
    : [];
  return {
    ...payload,
    items,
    active_conversation_id: typeof payload.active_conversation_id === 'string' ? payload.active_conversation_id : null,
    active_view: typeof payload.active_view === 'string' ? payload.active_view : null,
    pinned_conversations: pinned,
    revision: typeof payload.revision === 'number' && Number.isFinite(payload.revision)
      ? payload.revision
      : undefined,
    reason: typeof payload.reason === 'string' ? payload.reason : undefined,
    changed_conversation_id: typeof payload.changed_conversation_id === 'string'
      ? payload.changed_conversation_id
      : undefined,
    deleted_conversation_id: typeof payload.deleted_conversation_id === 'string'
      ? payload.deleted_conversation_id
      : undefined,
    transport,
  };
}

function normalizeConversationControlResult(
  result: unknown,
  transport: 'rpc' | 'legacy',
): ConversationControlResult {
  const payload = asObject(result);
  if (!payload) {
    return {
      ok: false,
      error: 'Invalid response',
      transport,
    };
  }
  return {
    ...payload,
    transport,
  };
}

function normalizeRpcObjectResult(result: unknown): JsonObject {
  return asObject(result) ?? {};
}

function normalizeLiveNotificationEvent(
  notification: JsonRpcNotificationEnvelope<unknown>,
): ConversationsLiveEvent | null {
  const method = typeof notification.method === 'string' ? notification.method.trim() : '';
  if (!method) {
    return null;
  }
  if (!hasOwn(CONVERSATIONS_RPC_CANONICAL_EVENT_TYPE_BY_METHOD, method)) {
    return null;
  }
  const canonicalEventType = CONVERSATIONS_RPC_CANONICAL_EVENT_TYPE_BY_METHOD[method];
  const params = asObject(notification.params) ?? {};
  return {
    ...params,
    type: canonicalEventType,
    };
}

function normalizeReplayChunkResult(result: unknown): ReplayChunkResult {
  const payload = asObject(result);
  const frame = asObject(payload?.frame);
  if (!frame || frame.format !== 'jsonl') {
    throw new Error('Invalid conversation.replay.getChunk result');
  }

  const jsonl = typeof frame.jsonl === 'string' ? frame.jsonl : '';
  const nextCursor = asObject(frame.next_cursor);
  return {
    conversation_id: typeof payload?.conversation_id === 'string' ? payload.conversation_id : null,
    replay_id: typeof payload?.replay_id === 'string' ? payload.replay_id : 'replay',
    frame: {
      format: 'jsonl',
      offset: Number.isFinite(frame.offset) ? Number(frame.offset) : 0,
      item_count: Number.isFinite(frame.item_count) ? Number(frame.item_count) : 0,
      total_count: Number.isFinite(frame.total_count) ? Number(frame.total_count) : 0,
      chunk_index: Number.isFinite(frame.chunk_index) ? Number(frame.chunk_index) : 0,
      complete: frame.complete !== false,
      next_cursor: nextCursor && Number.isFinite(nextCursor.offset)
        ? { offset: Number(nextCursor.offset) }
        : null,
      jsonl,
    },
    items: parseReplayJsonl(jsonl),
    transport: 'rpc',
  };
}

async function requestReplayChunkRpc(
  params: {
    conversationId: string | null;
    offset: number;
    maxEntries: number;
    maxBytes: number;
    timeoutMs: number;
    windowRef?: RpcWindowRef;
  },
): Promise<ReplayChunkResult> {
  const result = await callRpcNamespace({
    namespace: CONVERSATIONS_RPC_NAMESPACE,
    method: CONVERSATIONS_RPC_METHODS.replayGetChunk,
    params: {
      conversation_id: params.conversationId,
      cursor: { offset: params.offset },
      max_entries: params.maxEntries,
      max_bytes: params.maxBytes,
      format: 'jsonl',
    },
    timeoutMs: params.timeoutMs,
    windowRef: params.windowRef ?? (typeof window !== 'undefined' ? window : null),
  });
  return normalizeReplayChunkResult(result);
}

async function fetchReplayChunkRpcAccumulated(
  params: {
    conversationId: string | null;
    offset: number;
    maxEntries: number;
    maxBytes: number;
    timeoutMs: number;
    windowRef?: RpcWindowRef;
  },
): Promise<ReplayChunkResult> {
  const firstChunk = await requestReplayChunkRpc(params);
  const firstOffset = firstChunk.frame.offset;
  const totalCount = firstChunk.frame.total_count;
  const remainingAvailable = Math.max(0, totalCount - firstOffset);
  const desiredCount = Math.min(Math.max(1, params.maxEntries), remainingAvailable || params.maxEntries);
  if (
    firstChunk.frame.complete
    || !firstChunk.frame.next_cursor
    || firstChunk.items.length >= desiredCount
  ) {
    return firstChunk;
  }

  const items = [...firstChunk.items];
  const jsonlParts = [firstChunk.frame.jsonl];
  const seenOffsets = new Set<number>([firstOffset]);
  let lastChunk = firstChunk;

  while (lastChunk.frame.next_cursor && items.length < desiredCount) {
    const nextOffset = Number(lastChunk.frame.next_cursor.offset);
    if (!Number.isFinite(nextOffset) || seenOffsets.has(nextOffset)) {
      break;
    }
    seenOffsets.add(nextOffset);

    const nextChunk = await requestReplayChunkRpc({
      ...params,
      offset: nextOffset,
      maxEntries: Math.max(1, desiredCount - items.length),
    });

    if (!nextChunk.items.length) {
      lastChunk = nextChunk;
      break;
    }

    items.push(...nextChunk.items);
    if (nextChunk.frame.jsonl) {
      jsonlParts.push(nextChunk.frame.jsonl);
    }
    lastChunk = nextChunk;
  }

  const loadedCount = Math.min(items.length, desiredCount);
  const loadedEnd = firstOffset + loadedCount;
  const complete = loadedEnd >= totalCount;
  return {
    conversation_id: firstChunk.conversation_id,
    replay_id: firstChunk.replay_id,
    frame: {
      format: 'jsonl',
      offset: firstOffset,
      item_count: loadedCount,
      total_count: totalCount,
      chunk_index: firstChunk.frame.chunk_index,
      complete,
      next_cursor: complete ? null : (lastChunk.frame.next_cursor ?? { offset: loadedEnd }),
      jsonl: jsonlParts.join(''),
    },
    items: items.slice(0, loadedCount),
    transport: 'rpc',
  };
}

export function createConversationsRpcClient(
  deps: ConversationsRpcClientDeps,
) {
  function getWindowRef(): RpcWindowRef {
    return deps.windowRef ?? (typeof window !== 'undefined' ? window : null);
  }

  async function getConversation(options: {
    conversationId?: string | null;
    timeoutMs?: number;
  } = {}): Promise<ConversationMetaResult> {
    const conversationId = typeof options.conversationId === 'string' && options.conversationId
      ? options.conversationId
      : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.get,
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationMetaResult(result, 'rpc');
  }

  async function listConversations(options: {
    timeoutMs?: number;
  } = {}): Promise<ConversationListResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.list,
      params: {},
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationListResult(result, 'rpc');
  }

  async function createConversation(options: {
    settings?: JsonObject | null;
    timeoutMs?: number;
  } = {}): Promise<ConversationMetaResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload = options.settings ? { settings: options.settings } : {};
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.create,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationMetaResult(result, 'rpc');
  }

  async function selectConversation(options: {
    conversationId: string;
    view?: string | null;
    timeoutMs?: number;
  }): Promise<ConversationMetaResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload: JsonObject = {
      conversation_id: options.conversationId,
    };
    if (typeof options.view === 'string' && options.view.trim()) {
      payload.view = options.view.trim();
    }
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.select,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationMetaResult(result, 'rpc');
  }

  async function updateConversation(options: {
    conversationId?: string | null;
    settings?: JsonObject | null;
    threadId?: string | null;
    timeoutMs?: number;
  }): Promise<ConversationMetaResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload: JsonObject = {};
    if (typeof options.conversationId === 'string' && options.conversationId.trim()) {
      payload.conversation_id = options.conversationId.trim();
    }
    if (options.settings && typeof options.settings === 'object') {
      payload.settings = options.settings;
    }
    if (typeof options.threadId === 'string' && options.threadId.trim()) {
      payload.thread_id = options.threadId.trim();
    }
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.update,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationMetaResult(result, 'rpc');
  }

  async function deleteConversation(options: {
    conversationId: string;
    timeoutMs?: number;
  }): Promise<ConversationControlResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload = { conversation_id: options.conversationId };
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.delete,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  async function forkConversation(options: {
    conversationId: string;
    title?: string | null;
    timeoutMs?: number;
  }): Promise<ConversationForkResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 30000;
    const payload: JsonObject = {
      conversation_id: options.conversationId,
    };
    if (typeof options.title === 'string' && options.title.trim()) {
      payload.title = options.title.trim();
    }
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.fork,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationMetaResult(result, 'rpc') as ConversationForkResult;
  }

  async function setConversationPins(options: {
    pinnedConversationIds: string[];
    timeoutMs?: number;
  }): Promise<ConversationControlResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload = { pinned_conversations: options.pinnedConversationIds };
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.pinsSet,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  async function setDraft(options: {
    conversationId?: string | null;
    draft: string;
    timeoutMs?: number;
  }): Promise<ConversationDraftResult> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const payload: JsonObject = {
      draft: typeof options.draft === 'string' ? options.draft : '',
    };
    if (typeof options.conversationId === 'string' && options.conversationId.trim()) {
      payload.conversation_id = options.conversationId.trim();
    }
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.draftSet,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    const normalized = asObject(result) ?? {};
    return {
      ...normalized,
      conversation_id: typeof normalized.conversation_id === 'string' ? normalized.conversation_id : null,
      transport: 'rpc',
    };
  }

  async function fetchReplayChunk(options: {
    conversationId?: string | null;
    offset: number;
    maxEntries: number;
    maxBytes?: number;
    timeoutMs?: number;
  }): Promise<ReplayChunkResult> {
    const {
      conversationId = null,
      offset,
      maxEntries,
      maxBytes = 524288,
      timeoutMs = 10000,
    } = options;
    return fetchReplayChunkRpcAccumulated({
      conversationId,
      offset,
      maxEntries,
      maxBytes,
      timeoutMs,
      windowRef: getWindowRef(),
    });
  }

  async function sendMessage(options: {
    conversationId?: string | null;
    text: string;
    timeoutMs?: number;
  }): Promise<ConversationSendResult> {
    const conversationId = typeof options.conversationId === 'string' && options.conversationId
      ? options.conversationId
      : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const params: JsonObject = {
      conversation_id: conversationId,
      text: options.text,
    };
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.send,
      params,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationSendResult(result, 'rpc', conversationId);
  }

  async function interruptConversation(options: {
    conversationId?: string | null;
    timeoutMs?: number;
  }): Promise<ConversationControlResult> {
    const conversationId = typeof options.conversationId === 'string' && options.conversationId
      ? options.conversationId
      : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.interrupt,
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  async function compactConversation(options: {
    conversationId?: string | null;
    timeoutMs?: number;
  }): Promise<ConversationControlResult> {
    const conversationId = typeof options.conversationId === 'string' && options.conversationId
      ? options.conversationId
      : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.compact,
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  async function respondApproval(options: {
    requestId: string;
    conversationId?: string | null;
    result?: JsonObject | null;
    decision?: string | null;
    timeoutMs?: number | null;
  }): Promise<JsonObject> {
    const payload: JsonObject = {
      request_id: options.requestId,
      conversation_id: options.conversationId ?? null,
    };
    if (options.result && typeof options.result === 'object') {
      payload.result = options.result;
    }
    if (typeof options.decision === 'string' && options.decision.trim()) {
      payload.decision = options.decision.trim();
    }
    const timeoutMs = options.timeoutMs === null
      ? 30000
      : (Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000);
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.approvalRespond,
      params: payload,
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeRpcObjectResult(result);
  }

  async function executeShellCommand(options: {
    conversationId: string;
    command: string;
    timeoutMs?: number;
  }): Promise<JsonObject> {
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.shellExec,
      params: {
        conversation_id: options.conversationId,
        command: options.command,
      },
      timeoutMs,
      windowRef: getWindowRef(),
    });
    return normalizeRpcObjectResult(result);
  }

  function subscribeLiveNotifications(options: {
    onEvent: (event: ConversationsLiveEvent, notification: JsonRpcNotificationEnvelope<unknown>) => void;
    onError?: (error: unknown) => void;
    onConnectionChange?: (connected: boolean) => void;
  }): () => void {
    return subscribeRpcNamespaceNotifications({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      windowRef: getWindowRef(),
      onConnectionChange: (connected) => {
        options.onConnectionChange?.(connected);
      },
      onNotification: (notification) => {
        try {
          const event = normalizeLiveNotificationEvent(notification);
          if (!event) {
            return;
          }
          options.onEvent(event, notification);
        } catch (error) {
          options.onError?.(error);
        }
      },
    });
  }

  function isRpcBackedLiveEvent(eventOrType: unknown): boolean {
    if (typeof eventOrType === 'string') {
      return isConversationsRpcBackedEventType(eventOrType);
    }
    return isConversationsRpcBackedEventType(asObject(eventOrType)?.type);
  }

  return {
    getConversation,
    listConversations,
    createConversation,
    selectConversation,
    updateConversation,
    deleteConversation,
    forkConversation,
    setConversationPins,
    setDraft,
    fetchReplayChunk,
    sendMessage,
    interruptConversation,
    compactConversation,
    respondApproval,
    executeShellCommand,
    subscribeLiveNotifications,
    isRpcBackedLiveEvent,
  };
}
