// Shell streaming card rendering helpers extracted from static/codex_agent.js

import {
  applyTranscriptCardMetadata,
  type TranscriptCardMetadata,
} from './transcript_card_metadata.ts';

declare const hljs: any;

type ShellRowEntry = {
  row: HTMLDivElement;
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
  makeCollapsible: (row: HTMLElement, key: string, startExpanded: boolean) => void;
  getSubagentContainer: (id: string, name: string, intent: string) => SubagentContainer;
  renderShellCmdRibbon: (el: HTMLElement | null, cmd: string) => void;
  postTe2OpenRequest: (target: { path: string; line: number; column: number }) => void;
  detectLangFromCommand: (command: string) => string | null;
  highlightCodeAlways: (text: string, language: string) => string;
  setStatusDot: (value: string) => void;
  setActivity: (message: string, active: boolean) => void;
  maybeAutoScroll: (force?: boolean) => void;
  setLastEventType?: (value: string) => void;
  _dbg?: boolean;
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

  // Uses same styling as command-result (renderCommandResult)
  function getShellRow(
    id: string,
    parentEl: HTMLElement | null,
    metadata: TranscriptCardMetadata | null = null,
  ): ShellRowEntry {
    let entry = shellRows.get(id);
    if (!entry) {
      clearPlaceholder();
      const row = document.createElement('div');
      row.className = 'timeline-row command-result terminal-card';
      row.dataset.shellId = id;
      applyTranscriptCardMetadata(row, metadata);

      const body = document.createElement('div');
      body.className = 'body';

      // Command ribbon (same as renderCommandResult)
      const cmdRibbon = document.createElement('div');
      cmdRibbon.className = 'command-ribbon';
      cmdRibbon.textContent = '$ ...';
      body.appendChild(cmdRibbon);

      // Output area - plain terminal text.
      const termEl = document.createElement('pre');
      termEl.className = 'command-output';
      body.appendChild(termEl);

      row.appendChild(body);
      if (parentEl) {
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `shell:${id}`, false);

      entry = { row, cmdRibbon, termEl, text: '' };
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
    // Just show the command, skip cwd line (redundant)
    renderShellCmdRibbon(entry.cmdRibbon, evt.command || '');

    if (_dbg) console.log('[SHELL_BEGIN] id=', evt.id, 'path=', evt.path, 'command=', evt.command, 'hasCmdRibbon=', !!entry.cmdRibbon);

    // If event includes a file path, make the ribbon clickable (jump-to-file)
    if (evt.path && entry.cmdRibbon) {
      entry.cmdRibbon.style.cursor = 'pointer';
      entry.cmdRibbon.title = evt.path;
      entry.cmdRibbon.addEventListener('click', (e: MouseEvent) => {
        const target = e.target;
        if (_dbg && target instanceof HTMLElement) console.log('[RIBBON_CLICK] FIRED', target.tagName, target.className);
        if (_dbg && target instanceof Element) console.log('[RIBBON_CLICK] twisty?', !!target.closest('.twisty'), 'toggle?', !!target.closest('.ribbon-toggle-zone'));
        if (target instanceof Element && (target.closest('.twisty') || target.closest('.ribbon-toggle-zone'))) return;
        const line = evt.line || 1;
        if (_dbg) console.log('[RIBBON_CLICK] calling postTe2OpenRequest path=', evt.path, 'line=', line);
        postTe2OpenRequest({ path: evt.path || '', line, column: 1 });
      });
      if (_dbg) console.log('[RIBBON_CLICK] handler wired for path=', evt.path);
    }

    entry.text = '';
    // Plain text mode.
    entry.termEl.textContent = '';
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
    if (cmd && entry.cmdRibbon) {
      renderShellCmdRibbon(entry.cmdRibbon, cmd);
      // Add path click handler if provided and not already wired
      if (evt.path && !entry.cmdRibbon.dataset.hasClickHandler) {
        entry.cmdRibbon.style.cursor = 'pointer';
        entry.cmdRibbon.title = evt.path;
        entry.cmdRibbon.dataset.hasClickHandler = 'true';
        entry.cmdRibbon.addEventListener('click', (e: MouseEvent) => {
          if (e.target instanceof Element && e.target.closest('.twisty')) return;
          postTe2OpenRequest({ path: evt.path || '', line: evt.line || 1, column: 1 });
        });
      }
    }

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
      entry.row.querySelector<HTMLElement>('.body')?.appendChild(footer);
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
    const row = document.createElement('div');
    row.className = 'timeline-row command-result';
    applyTranscriptCardMetadata(row, evt);

    const body = document.createElement('div');
    body.className = 'body';

    // Command ribbon — same polish as replay/shell_begin
    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon';
    const cmd = String(evt.command || '(shell)');
    renderShellCmdRibbon(cmdRibbon, cmd);

    // If event includes a file path, make ribbon clickable
    if (evt.path) {
      cmdRibbon.style.cursor = 'pointer';
      cmdRibbon.title = evt.path;
      cmdRibbon.dataset.hasClickHandler = 'true';
      cmdRibbon.addEventListener('click', (e: MouseEvent) => {
        if (e.target instanceof Element && e.target.closest('.twisty')) return;
        postTe2OpenRequest({ path: evt.path || '', line: evt.line || 1, column: 1 });
      });
    }
    body.appendChild(cmdRibbon);

    // Route into subagent container if tagged
    let parentEl = null;
    if (evt.subagent_id) {
      const sa = getSubagentContainer(evt.subagent_id, '', '');
      parentEl = sa.body;
    }

    // Output
    const pre = document.createElement('pre');
    pre.className = 'command-output';
    const stdout = String(evt.stdout || '');
    const stderr = String(evt.stderr || '');
    const lang = detectLangFromCommand(cmd);
    if (stdout || stderr) {
      if (lang && typeof hljs !== 'undefined') {
        try {
          pre.innerHTML = highlightCodeAlways(stdout, lang);
        } catch {
          pre.textContent = stdout;
        }
      } else {
        pre.textContent = stdout;
      }
      if (stderr) {
        const span = document.createElement('span');
        span.className = 'shell-stderr';
        span.textContent = stderr;
        pre.appendChild(span);
      }
    } else {
      pre.textContent = '(no output)';
    }
    body.appendChild(pre);

    // Footer with exit code
    const exitCode = evt.exitCode ?? 0;
    if (exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      body.appendChild(footer);
    }

    row.appendChild(body);
    if (parentEl) {
      parentEl.appendChild(row);
    } else {
      insertRow(row);
    }
    makeCollapsible(row, `shell-batch:${evt.id || cmd.slice(0, 40)}`, false);

    setStatusDot(exitCode === 0 ? 'success' : 'error');
  }

  return { getShellRow, renderShellBegin, renderShellDelta, renderShellEnd, renderShellBatchResult };
}
