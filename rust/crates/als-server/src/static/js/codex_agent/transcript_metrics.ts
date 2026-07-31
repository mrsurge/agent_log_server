import { TRANSCRIPT_TOP_SPACER_ROWS } from './transcript_config.ts';

interface TranscriptState {
  transcriptStart?: number;
  transcriptTotal?: number;
  transcriptEnd?: number;
  estimatedRowHeight?: number;
}

interface TranscriptSpacerElements {
  topSpacerEl: HTMLElement | null;
  bottomSpacerEl?: HTMLElement | null;
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
  function updateSpacerHeights() {
    const { topSpacerEl, bottomSpacerEl } = getSpacerEls();
    if (!topSpacerEl) return;
    const {
      transcriptStart,
      transcriptTotal,
      transcriptEnd,
      estimatedRowHeight,
    } = getTranscriptState();
    const above = Math.max(0, Number(transcriptStart) || 0);
    const below = Math.max(0, (Number(transcriptTotal) || 0) - (Number(transcriptEnd) || 0));
    const rowHeight = Math.max(1, Number(estimatedRowHeight) || 0);
    topSpacerEl.style.height = above > 0 ? `${TRANSCRIPT_TOP_SPACER_ROWS * rowHeight}px` : '0px';
    if (bottomSpacerEl) {
      bottomSpacerEl.style.height = '0px';
    }
  }

  function measureRowHeight() {
    if (!timelineEl) return;
    const rows = Array.from(timelineEl.querySelectorAll<HTMLElement>('.timeline-row'))
      .filter((row) => (
        !row.classList.contains('activity')
        && !row.classList.contains('muted')
        && !row.closest('.timeline-virtual-placeholder')
        && row.getBoundingClientRect().height > 0
      ));
    if (!rows.length) return;
    const total = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    if (total > 0) {
      setTranscriptState({ estimatedRowHeight: total / rows.length });
    }
  }

  return { updateSpacerHeights, measureRowHeight };
}
