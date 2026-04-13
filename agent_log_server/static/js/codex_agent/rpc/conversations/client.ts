import { RPC_NAMESPACES } from '../namespaces.ts';
import { getRpcRegistryPlaceholder } from '../registry.ts';
import {
  callRpcNamespace,
  type JsonRpcNotificationEnvelope,
  readRpcTransportEnabledPreference,
  subscribeRpcNamespaceNotifications,
} from '../transport.ts';

export interface ConversationsRpcClientPlaceholder {
  status: 'placeholder';
  namespace: string;
  methods: {
    send: 'conversation.send';
    interrupt: 'conversation.interrupt';
    compact: 'conversation.compact';
    replayGetChunk: 'conversation.replay.getChunk';
  };
  anchorModules: string[];
  notificationCount: number;
}

export interface ReplayChunkFrame {
  format: 'jsonl';
  offset: number;
  item_count: number;
  total_count: number;
  chunk_index: number;
  complete: boolean;
  next_cursor: {
    offset: number;
  } | null;
  jsonl: string;
}

export interface ReplayChunkResult {
  conversation_id: string | null;
  replay_id: string;
  frame: ReplayChunkFrame;
  items: Record<string, unknown>[];
  transport: 'rpc' | 'legacy';
}

export interface ConversationSendResult extends Record<string, unknown> {
  conversation_id: string | null;
  accepted: boolean;
  transport: 'rpc' | 'legacy';
}

export interface ConversationControlResult extends Record<string, unknown> {
  ok?: boolean;
  transport: 'rpc' | 'legacy';
}

export type ConversationsLiveEvent = Record<string, unknown> & {
  type?: string;
  conversation_id?: string;
};

interface ConversationsRpcClientDeps {
  sioCall: (event: string, payload?: Record<string, unknown>, options?: Record<string, unknown>) => Promise<any>;
  windowRef?: any;
}

const CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE: Record<string, string> = {
  activity: 'conversation.activity',
  approval: 'conversation.approval.request',
  approval_handoff: 'conversation.approval.handoff',
  assistant_delta: 'conversation.message.delta',
  assistant_end: 'conversation.message.final',
  assistant_finalize: 'conversation.message.final',
  command_result: 'conversation.command.result',
  context_compacted: 'conversation.context.compacted',
  diff: 'conversation.diff',
  diff_declined: 'conversation.diff.declined',
  draft_update: 'conversation.draft.updated',
  error: 'conversation.error',
  mention_insert: 'conversation.mention.inserted',
  message: 'conversation.user.message',
  meta_updated: 'conversation.meta.updated',
  mode: 'conversation.mode.changed',
  plan: 'conversation.plan',
  plan_state: 'conversation.plan.state',
  plan_update: 'conversation.plan.update',
  preview_updated: 'conversation.preview.updated',
  reasoning_delta: 'conversation.reasoning.delta',
  reasoning_end: 'conversation.reasoning.final',
  reasoning_finalize: 'conversation.reasoning.final',
  shell_begin: 'conversation.command.begin',
  shell_delta: 'conversation.command.delta',
  shell_end: 'conversation.command.end',
  status: 'conversation.status',
  subagent_end: 'conversation.subagent.end',
  subagent_start: 'conversation.subagent.start',
  thought: 'conversation.thought',
  toast: 'conversation.toast',
  token_count: 'conversation.token.updated',
  tool_begin: 'conversation.tool.begin',
  tool_delta: 'conversation.tool.delta',
  tool_end: 'conversation.tool.end',
  warning: 'conversation.warning',
};

const CONVERSATIONS_RPC_CANONICAL_EVENT_TYPE_BY_METHOD: Record<string, string> = {
  'conversation.activity': 'activity',
  'conversation.approval.handoff': 'approval_handoff',
  'conversation.approval.request': 'approval',
  'conversation.command.begin': 'shell_begin',
  'conversation.command.delta': 'shell_delta',
  'conversation.command.end': 'shell_end',
  'conversation.command.result': 'command_result',
  'conversation.context.compacted': 'context_compacted',
  'conversation.diff': 'diff',
  'conversation.diff.declined': 'diff_declined',
  'conversation.draft.updated': 'draft_update',
  'conversation.error': 'error',
  'conversation.mention.inserted': 'mention_insert',
  'conversation.message.delta': 'assistant_delta',
  'conversation.message.final': 'assistant_finalize',
  'conversation.meta.updated': 'meta_updated',
  'conversation.mode.changed': 'mode',
  'conversation.plan': 'plan',
  'conversation.plan.state': 'plan_state',
  'conversation.plan.update': 'plan_update',
  'conversation.preview.updated': 'preview_updated',
  'conversation.reasoning.delta': 'reasoning_delta',
  'conversation.reasoning.final': 'reasoning_finalize',
  'conversation.status': 'status',
  'conversation.subagent.end': 'subagent_end',
  'conversation.subagent.start': 'subagent_start',
  'conversation.thought': 'thought',
  'conversation.toast': 'toast',
  'conversation.token.updated': 'token_count',
  'conversation.tool.begin': 'tool_begin',
  'conversation.tool.delta': 'tool_delta',
  'conversation.tool.end': 'tool_end',
  'conversation.user.message': 'message',
  'conversation.warning': 'warning',
};

export function createConversationsRpcClientPlaceholder(): ConversationsRpcClientPlaceholder {
  const registry = getRpcRegistryPlaceholder();
  return {
    status: 'placeholder',
    namespace: RPC_NAMESPACES.conversations,
    methods: {
      send: 'conversation.send',
      interrupt: 'conversation.interrupt',
      compact: 'conversation.compact',
      replayGetChunk: 'conversation.replay.getChunk',
    },
    anchorModules: [
      'orchestrator/session_flow.js',
      'transcript_loader.js',
    ],
    notificationCount: registry.namespaces.conversations.notifications.length,
  };
}

function asObject(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as Record<string, unknown>;
}

function normalizeEventType(eventType: unknown): string {
  return typeof eventType === 'string' ? eventType.trim().toLowerCase() : '';
}

export function getConversationsRpcNotificationMethodForEventType(eventType: unknown): string | null {
  const normalized = normalizeEventType(eventType);
  return normalized ? (CONVERSATIONS_RPC_NOTIFICATION_METHOD_BY_EVENT_TYPE[normalized] ?? null) : null;
}

export function isConversationsRpcBackedEventType(eventType: unknown): boolean {
  return Boolean(getConversationsRpcNotificationMethodForEventType(eventType));
}

function parseReplayJsonl(jsonl: string): Record<string, unknown>[] {
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
      return parsed as Record<string, unknown>;
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
  notification: JsonRpcNotificationEnvelope,
): ConversationsLiveEvent | null {
  const method = typeof notification.method === 'string' ? notification.method.trim() : '';
  if (!method) {
    return null;
  }
  const canonicalEventType = CONVERSATIONS_RPC_CANONICAL_EVENT_TYPE_BY_METHOD[method];
  if (!canonicalEventType) {
    return null;
  }
  const params = asObject(notification.params) ?? {};
  return {
    ...params,
    type: canonicalEventType,
  };
}

function toLegacyReplayResult(data: any): ReplayChunkResult {
  const items = Array.isArray(data?.items) ? data.items : [];
  const offset = Number.isFinite(data?.offset) ? Number(data.offset) : 0;
  const totalCount = Number.isFinite(data?.total) ? Number(data.total) : items.length;
  const jsonl = items.map((item) => JSON.stringify(item)).join('\n');
  const nextOffset = offset + items.length;
  const complete = nextOffset >= totalCount;
  return {
    conversation_id: typeof data?.conversation_id === 'string' ? data.conversation_id : null,
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

function normalizeReplayChunkResult(result: any): ReplayChunkResult {
  const frame = (result && typeof result === 'object' && result.frame && typeof result.frame === 'object')
    ? result.frame
    : null;
  if (!frame || frame.format !== 'jsonl') {
    throw new Error('Invalid conversation.replay.getChunk result');
  }

  const jsonl = typeof frame.jsonl === 'string' ? frame.jsonl : '';
  return {
    conversation_id: typeof result?.conversation_id === 'string' ? result.conversation_id : null,
    replay_id: typeof result?.replay_id === 'string' ? result.replay_id : 'replay',
    frame: {
      format: 'jsonl',
      offset: Number.isFinite(frame.offset) ? Number(frame.offset) : 0,
      item_count: Number.isFinite(frame.item_count) ? Number(frame.item_count) : 0,
      total_count: Number.isFinite(frame.total_count) ? Number(frame.total_count) : 0,
      chunk_index: Number.isFinite(frame.chunk_index) ? Number(frame.chunk_index) : 0,
      complete: frame.complete !== false,
      next_cursor: frame.next_cursor && Number.isFinite(frame.next_cursor.offset)
        ? { offset: Number(frame.next_cursor.offset) }
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
    windowRef?: any;
  },
): Promise<ReplayChunkResult> {
  const result = await callRpcNamespace({
    namespace: RPC_NAMESPACES.conversations,
    method: 'conversation.replay.getChunk',
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
    windowRef?: any;
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
      if (!legacy || legacy.ok === false) {
        throw new Error(`get_transcript_range failed: ${legacy?.error || 'no data'}`);
      }
      return toLegacyReplayResult(legacy);
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
    toastContext?: Record<string, unknown> | null;
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
      namespace: RPC_NAMESPACES.conversations,
      method: 'conversation.send',
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
      namespace: RPC_NAMESPACES.conversations,
      method: 'conversation.interrupt',
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
      namespace: RPC_NAMESPACES.conversations,
      method: 'conversation.compact',
      params: {
        conversation_id: conversationId,
      },
      timeoutMs,
      windowRef: deps.windowRef ?? (typeof window !== 'undefined' ? window : null),
    });
    return normalizeConversationControlResult(result, 'rpc');
  }

  function subscribeLiveNotifications(options: {
    onEvent: (event: ConversationsLiveEvent, notification: JsonRpcNotificationEnvelope) => void;
    onError?: (error: unknown) => void;
    onConnectionChange?: (connected: boolean) => void;
    enabled?: () => boolean;
  }): () => void {
    const enabled = typeof options.enabled === 'function' ? options.enabled : rpcEnabled;
    return subscribeRpcNamespaceNotifications({
      namespace: RPC_NAMESPACES.conversations,
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
