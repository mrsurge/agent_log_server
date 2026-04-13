import { RPC_NAMESPACES } from '../namespaces.ts';
import { getRpcRegistryPlaceholder } from '../registry.ts';
import {
  callRpcNamespace,
  readRpcTransportEnabledPreference,
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

interface ConversationsRpcClientDeps {
  sioCall: (event: string, payload?: Record<string, unknown>, options?: Record<string, unknown>) => Promise<any>;
  windowRef?: any;
}

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

    if (!readRpcTransportEnabledPreference(deps.windowRef ?? (typeof window !== 'undefined' ? window : null))) {
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

  return {
    fetchReplayChunk,
  };
}
