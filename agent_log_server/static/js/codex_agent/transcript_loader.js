export function bindTranscriptLoader(ctx) {
  const {
    getConversationId,
    sioCall,
    getTranscriptState,
    setTranscriptState,
    renderTranscriptEntries,
    scrollContainer,
    setScrollProgrammatic,
    isSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    maybeAutoScroll,
    setLastEventType,
    refreshPlanSurface,
  } = ctx;

  async function fetchTranscriptRange(offset, limit) {
    const convoId = getConversationId?.() || null;
    const cid = convoId ? `&conversation_id=${encodeURIComponent(convoId)}` : '';
    const url = `/api/appserver/transcript/range?offset=${offset}&limit=${limit}${cid}`;
    const data = await sioCall('get_transcript_range', {
      conversation_id: convoId,
      offset,
      limit,
    }, { fallbackUrl: url, fallbackMethod: 'GET' });
    if (!data || data.ok === false) {
      throw new Error(`get_transcript_range failed: ${data?.error || 'no data'}`);
    }
    return data;
  }

  async function loadOlderTranscript() {
    const state = getTranscriptState();
    if (state.transcriptLoading) return;
    if (state.transcriptStart <= 0) return;
    setTranscriptState({ transcriptLoading: true });
    try {
      const prevOffset = Math.max(0, state.transcriptStart - state.transcriptLimit);
      const count = state.transcriptStart - prevOffset;
      if (count <= 0) return;
      const data = await fetchTranscriptRange(prevOffset, count);
      if (data && Array.isArray(data.items) && data.items.length) {
        setTranscriptState({
          transcriptTotal: data.total || getTranscriptState().transcriptTotal,
          transcriptStart: data.offset ?? prevOffset,
        });
        const afterStart = getTranscriptState().transcriptStart;
        // Snapshot scroll position BEFORE rendering (spacer will shrink, content will grow)
        const oldScrollTop = scrollContainer?.scrollTop || 0;
        const oldScrollHeight = scrollContainer?.scrollHeight || 0;
        renderTranscriptEntries(data.items, { prepend: true });
        setTranscriptState({
          transcriptEnd: Math.max(getTranscriptState().transcriptEnd, afterStart + (data.items?.length || 0)),
        });
        // Compensate: keep the same content in view despite spacer resize + content insert
        if (scrollContainer) {
          const newScrollHeight = scrollContainer.scrollHeight;
          setScrollProgrammatic(true);
          scrollContainer.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
          requestAnimationFrame(() => { setScrollProgrammatic(false); });
        }
      }
    } finally {
      setTranscriptState({ transcriptLoading: false });
    }
  }

  async function replayTranscript() {
    try {
      if (isSemanticShellRibbonEnabled()) {
        await ensureTreeSitterRibbonReady();
      }
      const { transcriptLimit } = getTranscriptState();
      const data = await fetchTranscriptRange(-1, transcriptLimit);
      if (!data || !Array.isArray(data.items)) return;
      const transcriptStart = data.offset || 0;
      const transcriptEnd = transcriptStart + (data.items?.length || 0);
      setTranscriptState({
        transcriptTotal: data.total || 0,
        transcriptStart,
        transcriptEnd,
      });
      renderTranscriptEntries(data.items, { prepend: false });
      setTranscriptState({ transcriptEnd });
      if (refreshPlanSurface) await refreshPlanSurface();
      if (setLastEventType) setLastEventType(null);
      // Delay scroll to ensure DOM is fully rendered
      requestAnimationFrame(() => {
        requestAnimationFrame(() => maybeAutoScroll(true));
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
  };
}
