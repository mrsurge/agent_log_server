import {
  createConversationsRpcClient,
  createConversationsRpcClientPlaceholder,
} from './rpc/conversations/client.ts';
import {
  parseTranscriptOrderId,
  type TranscriptAnchor,
} from './transcript_card_metadata.ts';
import { DEFAULT_TRANSCRIPT_LIMIT } from './transcript_config.ts';

const _conversationsReplayRpcPlaceholder = createConversationsRpcClientPlaceholder;
void _conversationsReplayRpcPlaceholder;

interface TranscriptLoaderState {
  transcriptLoading?: boolean;
  transcriptStart: number;
  transcriptLimit: number;
  transcriptTotal: number;
  transcriptEnd: number;
  transcriptGeneration?: number;
  transcriptHistoryMode?: boolean;
}

interface TranscriptRangeResponse {
  conversation_id: string | null;
  total: number;
  offset: number;
  items: unknown[];
}

interface TranscriptLoaderContext {
  getConversationId?(): string | null | undefined;
  sioCall(event: string, payload?: Record<string, unknown>): Promise<unknown>;
  getTranscriptState(): TranscriptLoaderState;
  setTranscriptState(patch: Partial<TranscriptLoaderState>): void;
  renderTranscriptEntries(items: unknown[], options: { prepend: boolean }): void;
  prepareTranscriptWindow?(): void;
  timelineEl?: HTMLElement | null;
  scrollContainer?: HTMLElement | null;
  setScrollProgrammatic(value: boolean): void;
  isSemanticShellRibbonEnabled(): boolean;
  ensureTreeSitterRibbonReady(): Promise<unknown>;
  maybeAutoScroll(force?: boolean): void;
  setLastEventType?(value: string | null): void;
  refreshPlanSurface?(): Promise<unknown> | unknown;
  restorePendingApprovals?(): void;
}

export function bindTranscriptLoader(ctx: TranscriptLoaderContext) {
  const {
    getConversationId,
    sioCall,
    getTranscriptState,
    setTranscriptState,
    renderTranscriptEntries,
    prepareTranscriptWindow,
    timelineEl,
    scrollContainer,
    setScrollProgrammatic,
    isSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    maybeAutoScroll,
    setLastEventType,
    refreshPlanSurface,
    restorePendingApprovals,
  } = ctx;
  const conversationsRpcClient = createConversationsRpcClient({ sioCall });

  function isStaleTranscriptResponse(
    expectedConversationId: string | null,
    expectedGeneration: number,
    responseConversationId: string | null,
  ): boolean {
    const currentGeneration = Number(getTranscriptState().transcriptGeneration) || 0;
    if (currentGeneration !== expectedGeneration) {
      return true;
    }
    const activeConversationId = getConversationId?.() || null;
    if (expectedConversationId && activeConversationId && expectedConversationId !== activeConversationId) {
      return true;
    }
    if (expectedConversationId && responseConversationId && expectedConversationId !== responseConversationId) {
      return true;
    }
    return false;
  }

  function captureTranscriptAnchor(edge: 'start' | 'end'): TranscriptAnchor | null {
    if (!scrollContainer || !timelineEl) {
      return null;
    }
    const containerRect = scrollContainer.getBoundingClientRect();
    const rows = Array.from(
      timelineEl.querySelectorAll<HTMLElement>('[data-transcript-order-id]'),
    );
    let bestRow: HTMLElement | null = null;
    let bestOffset = 0;
    if (edge === 'start') {
      let bestDistance = Number.POSITIVE_INFINITY;
      for (const row of rows) {
        const orderId = parseTranscriptOrderId(row.dataset.transcriptOrderId);
        if (orderId === null) continue;
        const rect = row.getBoundingClientRect();
        if (rect.bottom <= containerRect.top || rect.top >= containerRect.bottom) continue;
        const offsetPx = rect.top - containerRect.top;
        const distance = offsetPx >= 0 ? offsetPx : Math.abs(offsetPx) + containerRect.height;
        if (distance < bestDistance) {
          bestDistance = distance;
          bestRow = row;
          bestOffset = Math.max(0, offsetPx);
        }
      }
    } else {
      let bestDistance = Number.POSITIVE_INFINITY;
      for (const row of rows) {
        const orderId = parseTranscriptOrderId(row.dataset.transcriptOrderId);
        if (orderId === null) continue;
        const rect = row.getBoundingClientRect();
        if (rect.bottom <= containerRect.top || rect.top >= containerRect.bottom) continue;
        const offsetPx = containerRect.bottom - rect.bottom;
        const distance = offsetPx >= 0 ? offsetPx : Math.abs(offsetPx) + containerRect.height;
        if (distance < bestDistance) {
          bestDistance = distance;
          bestRow = row;
          bestOffset = Math.max(0, offsetPx);
        }
      }
    }
    const orderId = parseTranscriptOrderId(bestRow?.dataset.transcriptOrderId);
    if (orderId === null) {
      return null;
    }
    return {
      orderId,
      edge,
      offsetPx: bestOffset,
    };
  }

  function restoreTranscriptAnchor(anchor: TranscriptAnchor | null): void {
    if (!anchor || !scrollContainer || !timelineEl) {
      return;
    }
    const anchorRow = timelineEl.querySelector<HTMLElement>(
      `[data-transcript-order-id="${anchor.orderId}"]`,
    );
    if (!(anchorRow instanceof HTMLElement)) {
      return;
    }
    const containerRect = scrollContainer.getBoundingClientRect();
    const rowRect = anchorRow.getBoundingClientRect();
    const delta = anchor.edge === 'start'
      ? (rowRect.top - containerRect.top) - anchor.offsetPx
      : anchor.offsetPx - (containerRect.bottom - rowRect.bottom);
    setScrollProgrammatic(true);
    scrollContainer.scrollTop += delta;
    requestAnimationFrame(() => {
      setScrollProgrammatic(false);
    });
  }

  function shiftChunkSize(limit: number): number {
    return Math.max(1, Math.floor(Math.max(1, limit) / 2));
  }

  function replaceTranscriptWindow(
    items: unknown[],
    nextState: {
      total: number;
      start: number;
      end: number;
    },
    anchor: TranscriptAnchor | null,
  ): void {
    prepareTranscriptWindow?.();
    setTranscriptState({
      transcriptTotal: nextState.total,
      transcriptStart: nextState.start,
      transcriptEnd: nextState.end,
      transcriptHistoryMode: false,
    });
    renderTranscriptEntries(items, { prepend: false });
    restorePendingApprovals?.();
    requestAnimationFrame(() => {
      restoreTranscriptAnchor(anchor);
    });
  }

  async function loadLatestTranscriptWindow(
    requestConversationId: string | null,
    requestGeneration: number,
    options: { resetLastEventType?: boolean } = {},
  ): Promise<boolean> {
    const resetLastEventType = options.resetLastEventType === true;
    const { transcriptLimit } = getTranscriptState();
    const data = await fetchTranscriptRange(-1, transcriptLimit);
    if (isStaleTranscriptResponse(requestConversationId, requestGeneration, data.conversation_id)) {
      return false;
    }
    if (!data || !Array.isArray(data.items)) {
      return false;
    }
    const transcriptStart = data.offset || 0;
    const transcriptEnd = transcriptStart + (data.items?.length || 0);
    replaceTranscriptWindow(
      data.items,
      {
        total: data.total || 0,
        start: transcriptStart,
        end: transcriptEnd,
      },
      null,
    );
    if (refreshPlanSurface) {
      await refreshPlanSurface();
    }
    if (resetLastEventType && setLastEventType) {
      setLastEventType(null);
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => maybeAutoScroll(true));
    });
    return true;
  }

  async function fetchTranscriptRange(offset: number, limit: number): Promise<TranscriptRangeResponse> {
    const convoId = getConversationId?.() || null;
    const replay = await conversationsRpcClient.fetchReplayChunk({
      conversationId: convoId,
      offset,
      maxEntries: limit,
    });
    return {
      conversation_id: replay.conversation_id,
      total: replay.frame.total_count,
      offset: replay.frame.offset,
      items: replay.items,
    };
  }

  async function loadOlderTranscript() {
    const state = getTranscriptState();
    if (state.transcriptLoading) return;
    if (state.transcriptStart <= 0) return;
    const requestConversationId = getConversationId?.() || null;
    const requestGeneration = Number(state.transcriptGeneration) || 0;
    setTranscriptState({ transcriptLoading: true });
    try {
      const windowSize = Math.max(1, Number(state.transcriptLimit) || 0);
      const shift = shiftChunkSize(windowSize);
      const prevOffset = Math.max(0, state.transcriptStart - shift);
      const count = Math.min(windowSize, Math.max(0, (state.transcriptTotal || 0) - prevOffset));
      if (count <= 0) return;
      const data = await fetchTranscriptRange(prevOffset, count);
      if (isStaleTranscriptResponse(requestConversationId, requestGeneration, data.conversation_id)) {
        return;
      }
      if (data && Array.isArray(data.items) && data.items.length) {
        const transcriptStart = data.offset ?? prevOffset;
        const currentState = getTranscriptState();
        const currentStart = Math.max(0, Number(currentState.transcriptStart) || 0);
        const currentEnd = Math.max(0, Number(currentState.transcriptEnd) || 0);
        const prependCount = Math.max(0, currentStart - transcriptStart);
        if (prependCount <= 0) {
          setTranscriptState({
            transcriptStart,
            transcriptTotal: data.total || currentState.transcriptTotal,
          });
          return;
        }
        const itemsToPrepend = data.items.slice(0, prependCount);
        if (itemsToPrepend.length) {
          const anchor = captureTranscriptAnchor('start');
          renderTranscriptEntries(itemsToPrepend, { prepend: true });
          setTranscriptState({
            transcriptStart,
            transcriptEnd: currentEnd,
            transcriptTotal: data.total || currentState.transcriptTotal,
          });
          requestAnimationFrame(() => {
            restoreTranscriptAnchor(anchor);
          });
        }
      }
    } finally {
      if ((Number(getTranscriptState().transcriptGeneration) || 0) === requestGeneration) {
        setTranscriptState({ transcriptLoading: false });
      }
    }
  }

  async function collapseTranscriptToPinned(): Promise<boolean> {
    const state = getTranscriptState();
    if (state.transcriptLoading) return false;
    const requestConversationId = getConversationId?.() || null;
    const requestGeneration = Number(state.transcriptGeneration) || 0;
    setTranscriptState({ transcriptLoading: true });
    try {
      return await loadLatestTranscriptWindow(requestConversationId, requestGeneration);
    } finally {
      if ((Number(getTranscriptState().transcriptGeneration) || 0) === requestGeneration) {
        setTranscriptState({ transcriptLoading: false });
      }
    }
  }

  async function replayTranscript() {
    try {
      const requestConversationId = getConversationId?.() || null;
      const requestGeneration = Number(getTranscriptState().transcriptGeneration) || 0;
      if (isSemanticShellRibbonEnabled()) {
        await ensureTreeSitterRibbonReady();
      }
      await loadLatestTranscriptWindow(requestConversationId, requestGeneration, {
        resetLastEventType: true,
      });
    } catch (err) {
      const state = getTranscriptState();
      console.error('[replayTranscript] failed', {
        error: err,
        conversation_id: getConversationId?.() || null,
        transcriptLimit: state.transcriptLimit,
      });
      // Surface a real console error instead of silently swallowing replay failures.
      setTimeout(() => {
        throw (err instanceof Error ? err : new Error(String(err)));
      }, 0);
    }
  }

  return {
    fetchTranscriptRange,
    loadOlderTranscript,
    replayTranscript,
    collapseTranscriptToPinned,
  };
}
