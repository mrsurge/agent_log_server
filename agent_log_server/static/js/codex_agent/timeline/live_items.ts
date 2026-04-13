import { bindAssistantStream } from '../assistant_stream.ts';
import { bindDiffRendering } from '../diff/rendering.ts';
import { bindApprovalUi } from '../approvals/ui.ts';

type AnyRecord = Record<string, any>;

interface LiveItemsState {
  lastEventType?: string | null;
  activeAgentPtyBlockId?: string | null;
}

interface LiveItemsContext {
  getState(): LiveItemsState;
  setState(patch: Partial<LiveItemsState>): void;
  assistantRows: Map<any, any>;
  reasoningRows: Map<any, any>;
  diffRows: Map<any, any>;
  agentBlockRows: Map<any, any>;
  timelineEl: HTMLElement | null;
  buildMessageCard(role: string, text?: string): { row: HTMLElement; body: HTMLElement };
  updateMessageCardHeader(row: HTMLElement, role: string, text: string): void;
  insertRow(row: HTMLElement, beforeEl?: ChildNode | null): void;
  createRow(kind: string, title: string, beforeEl?: ChildNode | null, parentEl?: HTMLElement | null): { row: HTMLElement; body: HTMLElement };
  buildRow(kind: string, title: string): { row: HTMLElement; body: HTMLElement };
  makeCollapsible(row: HTMLElement, cardId: string, startExpanded: boolean, options?: AnyRecord): void;
  clearPlaceholder(): void;
  setActivity(label: string, active: boolean): void;
  setStatusDot(status: string | null): void;
  setCommandRunning(running: boolean): void;
  maybeAutoScroll(force?: boolean): void;
  isMarkdownEnabled(): boolean;
  createStreamingParser(target: HTMLElement): unknown;
  renderEventMarkdownInto(target: HTMLElement, markdown: string): void;
  streamWrite(parser: unknown, chunk: string): void;
  streamEnd(parser: unknown): void;
  highlightCode(target: HTMLElement): void;
  incrementMessages(): void;
  stripCitations(text: string): string;
  escapeHtml(text: string): string;
  toRelativePath(path: string): string;
  postTe2OpenRequest(target: { path?: unknown; line?: unknown; column?: unknown }): unknown;
  renderShellCmdRibbon(el: HTMLElement, cmd: string): unknown;
  highlightCodeAlways(text: string, lang: string): string;
  detectLangFromPath(path: string): string;
  resolveHljsLanguage(lang: string): string;
  detectLangFromCommand(command: string): string;
  isDiffSyntaxEnabled(): boolean;
  sioCall(event: string, data?: Record<string, unknown>): Promise<any>;
  getConversationId(): string | null;
  getConversationMeta(): AnyRecord;
  setConversationMeta(nextMeta: AnyRecord): void;
  getCurrentExtensionId(): string;
  getSubagentContainer(id: string, name: string, intent: string): AnyRecord;
  requestCardRuntime: AnyRecord;
  onAfterRender(): void;
}

export function bindTimelineLiveItems(ctx: LiveItemsContext) {
  const {
    getState,
    setState,
    assistantRows,
    reasoningRows,
    diffRows,
    agentBlockRows,
    timelineEl,
    buildMessageCard,
    updateMessageCardHeader,
    insertRow,
    createRow,
    buildRow,
    makeCollapsible,
    clearPlaceholder,
    setActivity,
    setStatusDot,
    setCommandRunning,
    maybeAutoScroll,
    isMarkdownEnabled,
    createStreamingParser,
    renderEventMarkdownInto,
    streamWrite,
    streamEnd,
    highlightCode,
    incrementMessages,
    stripCitations,
    escapeHtml,
    toRelativePath,
    postTe2OpenRequest,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromPath,
    resolveHljsLanguage,
    detectLangFromCommand,
    isDiffSyntaxEnabled,
    sioCall,
    getConversationId,
    getConversationMeta,
    setConversationMeta,
    getCurrentExtensionId,
    getSubagentContainer,
    requestCardRuntime,
    onAfterRender,
  } = ctx;

  function setLastEventType(value: string) {
    setState({ lastEventType: value });
  }

  const assistantStream = bindAssistantStream({
    assistantRows,
    buildMessageCard,
    updateMessageCardHeader,
    insertRow: (row) => insertRow(row),
    isMarkdownEnabled,
    createStreamingParser,
    renderEventMarkdownInto,
    streamWrite,
    streamEnd,
    highlightCode,
    incrementMessages,
    stripCitations,
    maybeAutoScroll: () => {
      setLastEventType('message');
      maybeAutoScroll();
    },
  });

  function appendAssistantDelta(id: string | null | undefined, delta: string, parentEl?: HTMLElement | null) {
    setLastEventType('message');
    assistantStream.appendAssistantDelta(id, delta, parentEl);
  }

  function finalizeAssistant(id: string | null | undefined, text: string, parentEl?: HTMLElement | null) {
    setLastEventType('message');
    assistantStream.finalizeAssistant(id, text, parentEl);
  }

  function getReasoningRow(id: string | null | undefined, parentEl: HTMLElement | null = null) {
    const key = id || 'reasoning';
    let entry = reasoningRows.get(key);
    if (!entry) {
      const { row, body } = createRow('reasoning', 'reasoning', undefined, parentEl);
      const pre = document.createElement('pre');
      pre.textContent = '';
      body.append(pre);
      entry = { row, body, pre };
      reasoningRows.set(key, entry);
    } else if (parentEl && entry.row && entry.row.parentElement !== parentEl) {
      parentEl.appendChild(entry.row);
    }
    return entry;
  }

  function appendReasoningDelta(id: string | null | undefined, delta: string, parentEl: HTMLElement | null = null) {
    if (delta === undefined || delta === null) return;
    const entry = getReasoningRow(id, parentEl);
    entry.pre.textContent += delta;
    setLastEventType('reasoning');
    maybeAutoScroll();
  }

  function finalizeReasoning(id: string | null | undefined, text: string, parentEl: HTMLElement | null = null) {
    const entry = getReasoningRow(id, parentEl);
    if (text) entry.pre.textContent = text;
    setLastEventType('reasoning');
    maybeAutoScroll();
  }

  function getDiffRow(id: string | null | undefined, path: string, parentEl?: HTMLElement | null) {
    const key = id || 'diff';
    let entry = diffRows.get(key);
    if (!entry) {
      const { row, body } = buildRow('diff', 'diff');
      const pathLabel = document.createElement('div');
      pathLabel.className = 'diff-path-label command-ribbon';
      if (path) {
        pathLabel.innerHTML = `<strong>${escapeHtml(toRelativePath(path))}</strong>`;
        pathLabel.style.cursor = 'pointer';
        pathLabel.dataset.hasClickHandler = 'true';
        pathLabel.addEventListener('click', (evt) => {
          const target = evt.target;
          if (target instanceof Element && target.closest('.twisty')) return;
          void postTe2OpenRequest({ path, line: 1, column: 1 });
        });
      } else {
        pathLabel.innerHTML = '<strong>diff</strong>';
      }
      body.append(pathLabel);
      const block = document.createElement('div');
      block.className = 'diff-block';
      body.append(block);
      if (parentEl) {
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `diff:${key}`, false);
      entry = { block, row };
      diffRows.set(key, entry);
    }
    return entry;
  }

  const diffRendering = bindDiffRendering({
    getDiffRow,
    createRow,
    escapeHtml,
    toRelativePath,
    isDiffSyntaxEnabled,
    detectLangFromPath,
    resolveHljsLanguage,
    setLastEventType,
    maybeAutoScroll,
    timelineEl,
    postTe2OpenRequest,
  });

  function addDiff(id: string, text: string, path: string, parentEl?: HTMLElement | null) {
    return diffRendering.addDiff(id, text, path, parentEl);
  }

  function addDeclinedDiff(id: string, text: string, path: string) {
    return diffRendering.addDeclinedDiff(id, text, path);
  }

  function formatDiff(text: string, filePath: string) {
    return diffRendering.formatDiff(text, filePath);
  }

  function renderDiffBlock(block: HTMLElement, text: string, filePath: string) {
    return diffRendering.renderDiffBlock(block, text, filePath);
  }

  function getDiffRenderState() {
    return diffRendering.getDiffRenderState();
  }

  function setDiffRenderMode(mode: string) {
    return diffRendering.setDiffRenderMode(mode);
  }

  function getAgentBlockRow(blockId: string | null | undefined, label: string) {
    const key = blockId || `agent-block:${label || 'agent'}`;
    let entry = agentBlockRows.get(key);
    if (!entry) {
      clearPlaceholder();
      const row = document.createElement('div');
      row.className = 'timeline-row command-result terminal-card';
      row.dataset.agentBlockId = key;

      const body = document.createElement('div');
      body.className = 'body';

      const cmdRibbon = document.createElement('div');
      cmdRibbon.className = 'command-ribbon';
      cmdRibbon.textContent = label ? `[agent] ${label}` : '[agent]';
      body.appendChild(cmdRibbon);

      const termEl = document.createElement('div');
      termEl.className = 'command-output';
      body.appendChild(termEl);

      row.appendChild(body);
      insertRow(row);
      entry = { row, cmdRibbon, termEl, text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
      agentBlockRows.set(key, entry);
    }
    return entry;
  }

  function renderAgentBlockBegin(evt: AnyRecord) {
    const block = evt.block || {};
    const blockId = evt.block_id || block.block_id || evt.blockId || 'agent';
    const cmd = block.cmd || '';
    const label = cmd ? `$ ${cmd}` : 'agent pty';
    const entry = getAgentBlockRow(blockId, label);
    entry.cmdRibbon.textContent = cmd ? `$ ${cmd}` : '';
    entry.text = '';
    entry.screenRows = null;
    entry.renderMode = 'raw';
    entry.hasRawStream = false;
    setState({ activeAgentPtyBlockId: blockId });
    entry.termEl.textContent = '';
    setLastEventType('shell');
    setActivity('agent pty', true);
    setCommandRunning(true);
    maybeAutoScroll();
  }

  function renderAgentBlockDelta(evt: AnyRecord) {
    const blockId = evt.block_id || evt.blockId || 'agent';
    const entry = agentBlockRows.get(blockId) || getAgentBlockRow(blockId, 'agent pty');
    if (entry.renderMode === 'screen' || entry.hasRawStream) return;
    const delta = evt.delta || '';
    if (!delta) return;
    entry.text += delta;
    entry.termEl.textContent = entry.text;
    setLastEventType('shell');
    maybeAutoScroll();
  }

  function renderScreenDelta(evt: AnyRecord) {
    const blockId = evt.block_id || evt.blockId;
    if (!blockId) return;
    const entry = agentBlockRows.get(blockId) || getAgentBlockRow(blockId, 'agent pty');
    if (entry.renderMode !== 'screen') return;
    if (entry.renderMode !== 'screen') {
      entry.renderMode = 'screen';
      entry.text = '';
      entry.buf = '';
      if (entry.term) {
        entry.term.reset();
      }
    }
    const rowCount = Number.isFinite(evt.rows_count) ? evt.rows_count : 40;
    if (!entry.screenRows || entry.screenRows.length !== rowCount) {
      entry.screenRows = new Array(rowCount).fill('');
    }
    const rows = Array.isArray(evt.rows) ? evt.rows : [];
    rows.forEach((rowData) => {
      if (!rowData || !Number.isFinite(rowData.row)) return;
      const idx = rowData.row;
      if (idx >= 0 && idx < entry.screenRows.length) {
        entry.screenRows[idx] = rowData.text || '';
      }
    });
    entry.termEl.textContent = entry.screenRows.join('\n');
    setLastEventType('shell');
    maybeAutoScroll();
  }

  function renderAgentBlockEnd(evt: AnyRecord) {
    const block = evt.block || {};
    const blockId = evt.block_id || block.block_id || evt.blockId || 'agent';
    const entry = agentBlockRows.get(blockId);
    if (!entry) return;
    const exitCode = block.exit_code ?? block.exitCode;
    if (exitCode !== undefined && exitCode !== null && exitCode !== 0) {
      const footer = document.createElement('div');
      footer.className = 'command-footer';
      footer.textContent = `exit ${exitCode}`;
      entry.row.querySelector('.body')?.appendChild(footer);
      setStatusDot('error');
    } else {
      setStatusDot('success');
    }
    setActivity('idle', false);
    setCommandRunning(false);
    setLastEventType('shell');
    maybeAutoScroll();
    if (getState().activeAgentPtyBlockId === blockId) {
      setState({ activeAgentPtyBlockId: null });
    }
  }

  function renderPlanCard(steps: AnyRecord[], parentEl: HTMLElement | null = null) {
    if (!steps || !steps.length) return;
    const { row, body } = createRow('plan', 'plan', undefined, parentEl);

    const header = document.createElement('div');
    header.className = 'plan-card-header';
    let collapsed = false;

    const toggleBtn = document.createElement('span');
    toggleBtn.className = 'plan-toggle';
    toggleBtn.textContent = '[-]';

    const title = document.createElement('span');
    title.className = 'plan-title';
    title.textContent = 'Plan';

    header.append(toggleBtn, title);
    body.appendChild(header);

    const list = document.createElement('div');
    list.className = 'plan-list';

    steps.forEach((item) => {
      const stepEl = document.createElement('div');
      stepEl.className = `plan-item ${item.status || 'pending'}`;

      const checkbox = document.createElement('span');
      checkbox.className = 'plan-checkbox';
      if (item.status === 'completed') {
        checkbox.textContent = '☑';
      } else if (item.status === 'in_progress') {
        checkbox.textContent = '◐';
      } else {
        checkbox.textContent = '☐';
      }

      const textEl = document.createElement('span');
      textEl.className = 'plan-text';
      textEl.textContent = item.step || '';

      stepEl.append(checkbox, textEl);
      list.appendChild(stepEl);
    });

    body.appendChild(list);

    toggleBtn.addEventListener('click', () => {
      collapsed = !collapsed;
      toggleBtn.textContent = collapsed ? '[+]' : '[-]';
      list.style.display = collapsed ? 'none' : 'flex';
    });

    setLastEventType('plan');
    maybeAutoScroll();
  }

  const approvalUi = bindApprovalUi({
    sioCall,
    getConversationId,
    getConversationMeta,
    setConversationMeta,
    getCurrentExtensionId,
    createRow,
    getSubagentContainer,
    escapeHtml,
    formatDiff,
    renderDiffBlock,
    renderEventMarkdownInto,
    toRelativePath,
    requestCardRuntime,
    timelineEl,
    onAfterRender,
  });

  function renderApproval(evt: AnyRecord, options: AnyRecord = {}) {
    return approvalUi.renderApproval(evt, options);
  }

  function respondApproval(requestId: string, decision: unknown) {
    return approvalUi.respondApproval(requestId, decision);
  }

  function handoffApproval(evt: AnyRecord) {
    return approvalUi.handoffApproval(evt);
  }

  function restorePendingApprovals() {
    approvalUi.restorePendingApprovals();
  }

  return {
    getAssistantRow: assistantStream.getAssistantRow,
    appendAssistantDelta,
    finalizeAssistant,
    getReasoningRow,
    appendReasoningDelta,
    finalizeReasoning,
    getDiffRow,
    addDiff,
    addDeclinedDiff,
    formatDiff,
    renderDiffBlock,
    getDiffRenderState,
    setDiffRenderMode,
    getAgentBlockRow,
    renderAgentBlockBegin,
    renderAgentBlockDelta,
    renderScreenDelta,
    renderAgentBlockEnd,
    renderPlanCard,
    renderApproval,
    respondApproval,
    handoffApproval,
    restorePendingApprovals,
  };
}
