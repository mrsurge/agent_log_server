export const TRANSCRIPT_CARD_NID = 'conversation.transcript';
const STATE_ONLY_ROLES = new Set(['mode', 'status', 'token_usage']);
const LIVE_EVENT_CARD_FAMILIES: Record<string, string> = {
  agent_block_begin: 'agent_pty',
  agent_block_delta: 'agent_pty',
  agent_block_end: 'agent_pty',
  approval: 'approval',
  approval_handoff: 'approval',
  assistant_delta: 'assistant',
  assistant_end: 'assistant',
  assistant_finalize: 'assistant',
  command_result: 'command',
  context_compacted: 'context_compacted',
  diff: 'diff',
  error: 'error',
  plan: 'plan',
  reasoning_delta: 'reasoning',
  reasoning_end: 'reasoning',
  reasoning_finalize: 'reasoning',
  screen_delta: 'agent_pty',
  search: 'search',
  shell_begin: 'command',
  shell_delta: 'command',
  shell_end: 'command',
  subagent_end: 'subagent_end',
  subagent_start: 'subagent_start',
  tool_begin: 'tool',
  tool_delta: 'tool',
  tool_end: 'tool',
  tool_interaction: 'tool',
  view: 'view',
  web_search: 'web_search',
};
const ROLE_CARD_FAMILY_ALIASES: Record<string, string> = {
  mcp_tool: 'tool',
};

export type TranscriptCardMetadata = {
  nid?: unknown;
  conversation_id?: unknown;
  conversationId?: unknown;
  order_id?: unknown;
  orderId?: unknown;
  card_id?: unknown;
  cardId?: unknown;
};

export type TranscriptAnchor = {
  orderId: number;
  edge: 'start' | 'end';
  offsetPx: number;
};

function cleanString(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function transcriptCardFamily(record: Record<string, unknown> | null | undefined): string | null {
  if (!record || typeof record !== 'object') {
    return null;
  }
  const role = cleanString(record.role);
  if (role) {
    const normalizedRole = ROLE_CARD_FAMILY_ALIASES[role.toLowerCase()] || role.toLowerCase();
    if (STATE_ONLY_ROLES.has(normalizedRole)) {
      return null;
    }
    return normalizedRole;
  }
  const eventType = cleanString(record.type);
  if (!eventType) {
    return null;
  }
  const normalizedEventType = eventType.toLowerCase();
  if (normalizedEventType === 'message') {
    const messageRole = cleanString(record.role);
    return messageRole ? transcriptCardFamily({ role: messageRole }) : 'message';
  }
  return LIVE_EVENT_CARD_FAMILIES[normalizedEventType] || null;
}

export function isVisibleTranscriptCardRecord(record: Record<string, unknown> | null | undefined): boolean {
  return transcriptCardFamily(record) !== null;
}

export function parseTranscriptOrderId(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

export function applyTranscriptCardMetadata(
  row: HTMLElement | null | undefined,
  metadata: TranscriptCardMetadata | null | undefined,
): void {
  if (!(row instanceof HTMLElement) || !metadata || typeof metadata !== 'object') {
    return;
  }
  const nid = typeof metadata.nid === 'string' && metadata.nid
    ? metadata.nid
    : TRANSCRIPT_CARD_NID;
  row.dataset.transcriptNid = nid;
  const conversationId = typeof metadata.conversation_id === 'string' && metadata.conversation_id
    ? metadata.conversation_id
    : (typeof metadata.conversationId === 'string' ? metadata.conversationId : '');
  if (conversationId) {
    row.dataset.transcriptConversationId = conversationId;
  }
  const orderId = parseTranscriptOrderId(metadata.order_id ?? metadata.orderId);
  if (orderId !== null) {
    row.dataset.transcriptOrderId = String(orderId);
  }
  const cardId = typeof metadata.card_id === 'string' && metadata.card_id
    ? metadata.card_id
    : (typeof metadata.cardId === 'string' ? metadata.cardId : '');
  if (cardId) {
    row.dataset.transcriptCardId = cardId;
  }
}

export function readTranscriptOrderId(
  row: Element | null | undefined,
): number | null {
  if (!(row instanceof HTMLElement)) {
    return null;
  }
  return parseTranscriptOrderId(row.dataset.transcriptOrderId);
}

export function readTranscriptCardId(
  row: Element | null | undefined,
): string | null {
  if (!(row instanceof HTMLElement)) {
    return null;
  }
  return typeof row.dataset.transcriptCardId === 'string' && row.dataset.transcriptCardId
    ? row.dataset.transcriptCardId
    : null;
}

export function findTranscriptCardRow(
  root: ParentNode | null | undefined,
  metadata: TranscriptCardMetadata | null | undefined,
): HTMLElement | null {
  if (!root || !metadata || typeof metadata !== 'object') {
    return null;
  }
  const expectedOrderId = parseTranscriptOrderId(metadata.order_id ?? metadata.orderId);
  const expectedCardId = typeof metadata.card_id === 'string' && metadata.card_id
    ? metadata.card_id
    : (typeof metadata.cardId === 'string' ? metadata.cardId : '');
  const expectedConversationId = typeof metadata.conversation_id === 'string' && metadata.conversation_id
    ? metadata.conversation_id
    : (typeof metadata.conversationId === 'string' ? metadata.conversationId : '');
  if (expectedOrderId === null && !expectedCardId) {
    return null;
  }
  const rows = Array.from(
    root.querySelectorAll<HTMLElement>('[data-transcript-order-id], [data-transcript-card-id]'),
  );
  let cardMatch: HTMLElement | null = null;
  for (const row of rows) {
    const rowConversationId = typeof row.dataset.transcriptConversationId === 'string'
      ? row.dataset.transcriptConversationId
      : '';
    if (expectedConversationId && rowConversationId && rowConversationId !== expectedConversationId) {
      continue;
    }
    const rowOrderId = parseTranscriptOrderId(row.dataset.transcriptOrderId);
    if (expectedOrderId !== null && rowOrderId === expectedOrderId) {
      return row;
    }
    if (expectedCardId && row.dataset.transcriptCardId === expectedCardId) {
      cardMatch = cardMatch || row;
    }
  }
  return cardMatch;
}
