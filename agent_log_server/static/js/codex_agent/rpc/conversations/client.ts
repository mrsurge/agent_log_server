import { getRpcRegistry } from '../registry.ts';
import {
  callRpcNamespace,
  type JsonRpcNotificationEnvelope,
  readRpcTransportEnabledPreference,
  type RpcWindowRef,
  subscribeRpcNamespaceNotifications,
} from '../transport.ts';
import {
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
  sioCall: (event: string, payload?: JsonObject, options?: JsonObject) => Promise<unknown>;
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

function toLegacyReplayResult(data: unknown): ReplayChunkResult {
  const payload = asObject(data) ?? {};
  const items = Array.isArray(payload.items)
    ? payload.items
      .map((item) => asObject(item))
      .filter((item): item is JsonObject => Boolean(item))
    : [];
  const offset = Number.isFinite(payload.offset) ? Number(payload.offset) : 0;
  const totalCount = Number.isFinite(payload.total) ? Number(payload.total) : items.length;
  const jsonl = items.map((item) => JSON.stringify(item)).join('\n');
  const nextOffset = offset + items.length;
  const complete = nextOffset >= totalCount;
  return {
    conversation_id: typeof payload.conversation_id === 'string' ? payload.conversation_id : null,
    replay_id: `legacy-replay-${offset}`,
    frame: {
      format: 'jsonl',
      offset,
      item_count: items.length,
      total_count: totalCount,
      chunk_index: 0,
      complete,
      next_cursor: complete ? null : { offset: nextOffset },
      jsonl: jsonl ? `${jsonl}\n` : '',
    },
    items,
    transport: 'legacy',
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
  function rpcEnabled(): boolean {
    return readRpcTransportEnabledPreference(deps.windowRef ?? (typeof window !== 'undefined' ? window : null));
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

    if (!rpcEnabled()) {
      const legacy = await deps.sioCall('get_transcript_range', {
        conversation_id: conversationId,
        offset,
        limit: maxEntries,
      }, { timeoutMs });
      const legacyPayload = asObject(legacy);
      if (!legacyPayload || legacyPayload.ok === false) {
        throw new Error(`get_transcript_range failed: ${legacyPayload?.error || 'no data'}`);
      }
      return toLegacyReplayResult(legacyPayload);
    }

    return fetchReplayChunkRpcAccumulated({
      conversationId,
      offset,
      maxEntries,
      maxBytes,
      timeoutMs,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
    });
  }

  async function sendMessage(options: {
    conversationId?: string | null;
    text: string;
    toastContext?: JsonObject | null;
    timeoutMs?: number;
  }): Promise<ConversationSendResult> {
    const conversationId = typeof options.conversationId === 'string' && options.conversationId
      ? options.conversationId
      : null;
    const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : 10000;
    if (!rpcEnabled()) {
      const legacy = await deps.sioCall('send_message', {
        conversation_id: conversationId,
        text: options.text,
      }, { timeoutMs });
      return normalizeConversationSendResult(legacy, 'legacy', conversationId);
    }

    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.send,
      params: {
        conversation_id: conversationId,
        text: options.text,
        toast_context: options.toastContext ?? undefined,
      },
      timeoutMs,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
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
    if (!rpcEnabled()) {
      const legacy = await deps.sioCall('interrupt', conversationId ? { conversation_id: conversationId } : {}, { timeoutMs });
      return normalizeConversationControlResult(legacy, 'legacy');
    }

    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.interrupt,
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
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
    if (!rpcEnabled()) {
      const legacy = await deps.sioCall('compact', conversationId ? { conversation_id: conversationId } : {}, { timeoutMs });
      return normalizeConversationControlResult(legacy, 'legacy');
    }

    const result = await callRpcNamespace({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      method: CONVERSATIONS_RPC_METHODS.compact,
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  function subscribeLiveNotifications(options: {
    onEvent: (event: ConversationsLiveEvent, notification: JsonRpcNotificationEnvelope<unknown>) => void;
    onError?: (error: unknown) => void;
    onConnectionChange?: (connected: boolean) => void;
    enabled?: () => boolean;
  }): () => void {
    const enabled = typeof options.enabled === 'function' ? options.enabled : rpcEnabled;
    return subscribeRpcNamespaceNotifications({
      namespace: CONVERSATIONS_RPC_NAMESPACE,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
      onConnectionChange: (connected) => {
        options.onConnectionChange?.(connected);
      },
      onNotification: (notification) => {
        if (!enabled()) {
          return;
        }
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
    fetchReplayChunk,
    sendMessage,
    interruptConversation,
    compactConversation,
    subscribeLiveNotifications,
    isRpcBackedLiveEvent,
  };
}
