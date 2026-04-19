// Shell streaming card rendering helpers extracted from static/codex_agent.js

import {
  applyTranscriptCardMetadata,
  type TranscriptCardMetadata,
} from './transcript_card_metadata.ts';

declare const hljs: any;

type ShellRowEntry = {
  row: HTMLDivElement;
  summaryTextEl: HTMLSpanElement;
  detailEl: HTMLDivElement;
  cmdRibbon: HTMLDivElement;
  termEl: HTMLPreElement;
  text: string;
};

type SubagentContainer = Record<string, unknown> & {
  body?: HTMLElement | null;
  label?: HTMLElement | null;
};

type ShellRenderEvent = {
  id?: string;
  card_id?: string;
  order_id?: number;
  nid?: string;
  command?: string;
  subagent_id?: string;
  path?: string;
  line?: number;
  activity?: string;
  delta?: string;
  stdout?: string;
  stderr?: string;
  exitCode?: number;
};

interface ShellRenderContext {
  shellRows: Map<string, ShellRowEntry>;
  clearPlaceholder: () => void;
  insertRow: (row: HTMLElement) => void;
  makeCollapsible: (row: HTMLElement, key: string, startExpanded: boolean, options?: Record<string, unknown>) => void;
  getSubagentContainer: (id: string, name: string, intent: string) => SubagentContainer;
  renderShellCmdRibbon: (el: HTMLElement | null, cmd: string, options?: { promptPrefix?: string }) => void;
  postTe2OpenRequest: (target: { path: string; line: number; column: number }) => void;
  detectLangFromCommand: (command: string) => string | null;
  highlightCodeAlways: (text: string, language: string) => string;
  setStatusDot: (value: string) => void;
  setActivity: (message: string, active: boolean) => void;
  maybeAutoScroll: (force?: boolean) => void;
  setLastEventType?: (value: string) => void;
  _dbg?: boolean;
}

const DEFAULT_SHELL_PREVIEW_LENGTH = 120;

export function buildShellCommandPreview(
  command: unknown,
  maxLength = DEFAULT_SHELL_PREVIEW_LENGTH,
  prefix = '$ ',
): string {
  const normalized = String(command || '').replace(/\s+/g, ' ').trim();
  const normalizedPrefix = String(prefix || '');
  if (!normalized) {
    return normalizedPrefix ? `${normalizedPrefix}(shell)` : '(shell)';
  }
  const limit = Math.max(24, Number(maxLength) || DEFAULT_SHELL_PREVIEW_LENGTH);
  const preview = `${normalizedPrefix}${normalized}`;
  if (preview.length <= limit) return preview;
  return `${preview.slice(0, Math.max(0, limit - 3)).trimEnd()}...`;
}

export function bindShellRender(ctx: ShellRenderContext) {
  const {
    shellRows,
    clearPlaceholder,
    insertRow,
    makeCollapsible,
    getSubagentContainer,
    renderShellCmdRibbon,
    postTe2OpenRequest,
    detectLangFromCommand,
    highlightCodeAlways,
    setStatusDot,
    setActivity,
    maybeAutoScroll,
    setLastEventType,
    _dbg,
  } = ctx;

  function createShellCardElements(metadata: TranscriptCardMetadata | null = null) {
    const row = document.createElement('div');
    row.className = 'timeline-row command-result terminal-card shell-card';
    applyTranscriptCardMetadata(row, metadata);

    const body = document.createElement('div');
    body.className = 'body';

    const summaryRibbon = document.createElement('div');
    summaryRibbon.className = 'command-ribbon shell-card-summary';
    const summaryTextEl = document.createElement('span');
    summaryTextEl.className = 'shell-card-summary-text';
    summaryTextEl.textContent = '$ ...';
    summaryRibbon.appendChild(summaryTextEl);
    body.appendChild(summaryRibbon);

    const detailEl = document.createElement('div');
    detailEl.className = 'shell-card-detail';

    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon shell-card-command';
    cmdRibbon.addEventListener('click', (event: MouseEvent) => {
      const path = String(row.dataset.shellPath || '').trim();
      if (!path) return;
      event.stopPropagation();
      const line = Number(row.dataset.shellLine || '1');
      postTe2OpenRequest({ path, line: Number.isFinite(line) ? line : 1, column: 1 });
    });
    detailEl.appendChild(cmdRibbon);

    const termEl = document.createElement('pre');
    termEl.className = 'command-output';
    detailEl.appendChild(termEl);

    body.appendChild(detailEl);
    row.appendChild(body);

    return { row, summaryRibbon, summaryTextEl, detailEl, cmdRibbon, termEl };
  }

  function syncShellCommandLink(
    row: HTMLElement,
    cmdRibbon: HTMLElement,
    path: string | undefined,
    line: number | undefined,
  ): void {
    const normalizedPath = typeof path === 'string' ? path.trim() : '';
    if (!normalizedPath) {
      delete row.dataset.shellPath;
      delete row.dataset.shellLine;
      cmdRibbon.style.removeProperty('cursor');
      cmdRibbon.removeAttribute('title');
      delete cmdRibbon.dataset.hasClickHandler;
      return;
    }
    row.dataset.shellPath = normalizedPath;
    row.dataset.shellLine = String(Number.isFinite(Number(line)) ? Number(line) : 1);
    cmdRibbon.style.cursor = 'pointer';
    cmdRibbon.title = normalizedPath;
    cmdRibbon.dataset.hasClickHandler = 'true';
  }

  // Uses same styling as command-result (renderCommandResult)
  function getShellRow(
    id: string,
    parentEl: HTMLElement | null,
    metadata: TranscriptCardMetadata | null = null,
  ): ShellRowEntry {
    let entry = shellRows.get(id);
    if (!entry) {
      clearPlaceholder();
      const {
        row,
        summaryRibbon,
        summaryTextEl,
        detailEl,
        cmdRibbon,
        termEl,
      } = createShellCardElements(metadata);
      row.dataset.shellId = id;
      if (parentEl) {
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `shell:${id}`, false, { headerEl: summaryRibbon });

      entry = { row, summaryTextEl, detailEl, cmdRibbon, termEl, text: '' };
      shellRows.set(id, entry);
    } else {
      applyTranscriptCardMetadata(entry.row, metadata);
    }
    return entry;
  }

  function renderShellBegin(evt: ShellRenderEvent) {
    // Route into subagent container if tagged
    let parentEl = null;
    if (evt.subagent_id) {
      const sa = getSubagentContainer(evt.subagent_id, '', '');
      parentEl = sa.body || null;
      // Update subagent header with current action
      if (sa.label) {
        sa.label.textContent = `${(sa.label.textContent || '').split(':')[0]}: ${evt.command || 'working'}`;
      }
    }
    const entry = getShellRow(evt.id || '', parentEl, evt);
    entry.summaryTextEl.textContent = buildShellCommandPreview(evt.command || '');
    renderShellCmdRibbon(entry.cmdRibbon, evt.command || '', { promptPrefix: '' });
    syncShellCommandLink(entry.row, entry.cmdRibbon, evt.path, evt.line);

    if (_dbg) console.log('[SHELL_BEGIN] id=', evt.id, 'path=', evt.path, 'command=', evt.command, 'hasCmdRibbon=', !!entry.cmdRibbon);

    entry.text = '';
    // Plain text mode.
    entry.termEl.textContent = '';
    entry.detailEl.querySelector('.command-footer')?.remove();
    if (setLastEventType) setLastEventType('shell');
    if (!evt.subagent_id) {
      setActivity(evt.activity || 'executing', true);
    }
    maybeAutoScroll();
  }

  function renderShellDelta(evt: ShellRenderEvent) {
    const entry = shellRows.get(evt.id || '');
    if (!entry) return;
    applyTranscriptCardMetadata(entry.row, evt);
    const delta = evt.delta || '';
    if (delta) {
      entry.text += delta;
      // Plain text mode
      entry.termEl.textContent = entry.text;
    }
    if (setLastEventType) setLastEventType('shell');
    maybeAutoScroll();
  }

  function renderShellEnd(evt: ShellRenderEvent) {
    const entry = shellRows.get(evt.id || '');
    if (!entry) {
      // No streaming happened, render batch result
      renderShellBatchResult(evt);
      return;
    }
    applyTranscriptCardMetadata(entry.row, evt);

    const exitCode = evt.exitCode ?? 0;

    // Update command ribbon if shell_end carries a refined label
    const cmd = String(evt.command || '');
    entry.summaryTextEl.textContent = buildShellCommandPreview(cmd);
    if (cmd && entry.cmdRibbon) {
      renderShellCmdRibbon(entry.cmdRibbon, cmd, { promptPrefix: '' });
    }
    syncShellCommandLink(entry.row, entry.cmdRibbon, evt.path, evt.line);
    entry.detailEl.querySelector('.command-footer')?.remove();

    // Prefer final stdout/stderr from the event so we can do syntax highlighting.
    const stdout = String(evt.stdout || '');
    const stderr = String(evt.stderr || '');
    const lang = detectLangFromCommand(cmd);
    if (stdout || stderr) {
      if (lang && typeof hljs !== 'undefined') {
        try {
          entry.termEl.innerHTML = highlightCodeAlways(stdout, lang);
        } catch {
          entry.termEl.textContent = stdout;
        }
      } else {
        entry.termEl.textContent = stdout;
      }
      if (stderr) {
        const stderrEl = document.createElement('span');
        stderrEl.className = 'shell-stderr';
        stderrEl.textContent = stderr;
        entry.termEl.appendChild(stderrEl);
      }
    } else if (entry.text) {
      // Streaming-only path (no stdout/stderr attached): keep plain text.
      entry.termEl.textContent = entry.text;
    } else {
      entry.termEl.textContent = '(no output)';
    }

    // Add footer with exit code (same as renderCommandResult)
    if (exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      entry.detailEl.appendChild(footer);
    }

    // Update status
    setStatusDot(exitCode === 0 ? 'success' : 'error');
    // Don't clear activity label — let it persist until turn end or next tool overwrites it
    if (setLastEventType) setLastEventType('shell');
    maybeAutoScroll();

    // Clean up tracking
    shellRows.delete(evt.id || '');
  }

  function renderShellBatchResult(evt: ShellRenderEvent) {
    // Fallback - shell_end without prior shell_begin
    clearPlaceholder();
    const {
      row,
      summaryRibbon,
      summaryTextEl,
      detailEl,
      cmdRibbon,
      termEl,
    } = createShellCardElements(evt);
    const cmd = String(evt.command || '(shell)');
    summaryTextEl.textContent = buildShellCommandPreview(cmd);
    renderShellCmdRibbon(cmdRibbon, cmd, { promptPrefix: '' });
    syncShellCommandLink(row, cmdRibbon, evt.path, evt.line);

    // Route into subagent container if tagged
    let parentEl = null;
    if (evt.subagent_id) {
      const sa = getSubagentContainer(evt.subagent_id, '', '');
      parentEl = sa.body;
    }

    // Output
    const stdout = String(evt.stdout || '');
    const stderr = String(evt.stderr || '');
    const lang = detectLangFromCommand(cmd);
    if (stdout || stderr) {
      if (lang && typeof hljs !== 'undefined') {
        try {
          termEl.innerHTML = highlightCodeAlways(stdout, lang);
        } catch {
          termEl.textContent = stdout;
        }
      } else {
        termEl.textContent = stdout;
      }
      if (stderr) {
        const span = document.createElement('span');
        span.className = 'shell-stderr';
        span.textContent = stderr;
        termEl.appendChild(span);
      }
    } else {
      termEl.textContent = '(no output)';
    }

    // Footer with exit code
    const exitCode = evt.exitCode ?? 0;
    if (exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      detailEl.appendChild(footer);
    }

    if (parentEl) {
      parentEl.appendChild(row);
    } else {
      insertRow(row);
    }
    makeCollapsible(row, `shell-batch:${evt.id || cmd.slice(0, 40)}`, false, { headerEl: summaryRibbon });

    setStatusDot(exitCode === 0 ? 'success' : 'error');
  }

  return { getShellRow, renderShellBegin, renderShellDelta, renderShellEnd, renderShellBatchResult };
}
