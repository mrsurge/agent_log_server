interface TranscriptState {
  transcriptStart?: number;
  transcriptTotal?: number;
  transcriptEnd?: number;
  transcriptLimit?: number;
  estimatedRowHeight?: number;
}

interface TranscriptSpacerElements {
  topSpacerEl: HTMLElement | null;
  bottomSpacerEl: HTMLElement | null;
}

interface TranscriptMetricsContext {
  timelineEl: HTMLElement | null;
  getSpacerEls(): TranscriptSpacerElements;
  getTranscriptState(): TranscriptState;
  setTranscriptState(nextState: Partial<TranscriptState>): void;
}

interface TranscriptMetricsBinding {
  updateSpacerHeights(): void;
  measureRowHeight(): void;
}

export function bindTranscriptMetrics(ctx: TranscriptMetricsContext): TranscriptMetricsBinding {
  const {
    timelineEl,
    getSpacerEls,
    getTranscriptState,
    setTranscriptState,
  } = ctx;
  const TRANSCRIPT_SCOPE_BUFFER_RATIO = 0.5;

  function updateSpacerHeights() {
    const { topSpacerEl, bottomSpacerEl } = getSpacerEls();
    if (!topSpacerEl || !bottomSpacerEl) return;
    const {
      transcriptStart,
      transcriptTotal,
      transcriptEnd,
      transcriptLimit,
      estimatedRowHeight,
    } = getTranscriptState();
    const above = Math.max(0, Number(transcriptStart) || 0);
    const below = Math.max(0, (Number(transcriptTotal) || 0) - (Number(transcriptEnd) || 0));
    const rowHeight = Math.max(1, Number(estimatedRowHeight) || 0);
    const bufferRows = Math.max(1, Math.floor(Math.max(1, Number(transcriptLimit) || 0) * TRANSCRIPT_SCOPE_BUFFER_RATIO));
    topSpacerEl.style.height = `${Math.max(0, Math.min(above, bufferRows) * rowHeight)}px`;
    bottomSpacerEl.style.height = `${Math.max(0, Math.min(below, bufferRows) * rowHeight)}px`;
  }

  function measureRowHeight() {
    if (!timelineEl) return;
    const rows = Array.from(timelineEl.querySelectorAll<HTMLElement>('.timeline-row'))
      .filter((row) => !row.classList.contains('activity') && !row.classList.contains('muted'));
    if (!rows.length) return;
    const total = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    if (total > 0) {
      setTranscriptState({ estimatedRowHeight: total / rows.length });
    }
  }

  return { updateSpacerHeights, measureRowHeight };
}
