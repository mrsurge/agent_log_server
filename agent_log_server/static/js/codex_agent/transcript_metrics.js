export function bindTranscriptMetrics(ctx) {
  const {
    timelineEl,
    getSpacerEls,
    getTranscriptState,
    setTranscriptState,
  } = ctx;

  function updateSpacerHeights() {
    const { topSpacerEl, bottomSpacerEl } = getSpacerEls();
    if (!topSpacerEl || !bottomSpacerEl) return;
    const {
      transcriptStart,
      transcriptTotal,
      transcriptEnd,
      estimatedRowHeight,
    } = getTranscriptState();
    const above = Math.max(0, transcriptStart);
    const below = Math.max(0, transcriptTotal - transcriptEnd);
    topSpacerEl.style.height = `${Math.max(0, above * estimatedRowHeight)}px`;
    bottomSpacerEl.style.height = `${Math.max(0, below * estimatedRowHeight)}px`;
  }

  function measureRowHeight() {
    if (!timelineEl) return;
    const rows = Array.from(timelineEl.querySelectorAll('.timeline-row'))
      .filter((row) => !row.classList.contains('activity') && !row.classList.contains('muted'));
    if (!rows.length) return;
    const total = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    if (total > 0) {
      setTranscriptState({ estimatedRowHeight: total / rows.length });
    }
  }

  return { updateSpacerHeights, measureRowHeight };
}

