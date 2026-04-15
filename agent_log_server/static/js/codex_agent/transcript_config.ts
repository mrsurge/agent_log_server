export const DEFAULT_TRANSCRIPT_LIMIT = 200;
export const TRANSCRIPT_PRELOAD_ROWS = 25;
export const TRANSCRIPT_TOP_SPACER_ROWS = 50;
export const TRANSCRIPT_LIVE_TAIL_ROWS = 50;

export function isTranscriptTailRange(end: number, total: number): boolean {
  const safeEnd = Math.max(0, Math.trunc(Number(end) || 0));
  const safeTotal = Math.max(0, Math.trunc(Number(total) || 0));
  if (safeTotal <= 0) {
    return true;
  }
  return safeEnd >= Math.max(0, safeTotal - TRANSCRIPT_LIVE_TAIL_ROWS);
}
