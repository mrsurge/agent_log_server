import { applyTranscriptCardMetadata } from '../transcript_card_metadata.ts';

type AnyRecord = Record<string, any>;

interface TimelineReplayState {
  conversationSettings?: AnyRecord;
  runtimeOptions?: AnyRecord;
  planOverlayEl?: HTMLElement | null;
  topSpacerEl?: HTMLElement | null;
  bottomSpacerEl?: HTMLElement | null;
  transcriptTotal?: number;
  transcriptStart?: number;
  transcriptEnd?: number;
  transcriptLoading?: boolean;
  transcriptGeneration?: number;
  debugEnabled?: boolean;
}

interface TimelineReplayContext {
  getState(): TimelineReplayState;
  setState(patch: Record<string, unknown>): void;
  timelineEl: HTMLElement | null;
  counterMessagesEl: HTMLElement | null;
  counterTokensEl: HTMLElement | null;
  contextRemainingEl: HTMLElement | null;
  assistantRows: Map<any, any>;
  reasoningRows: Map<any, any>;
  diffRows: Map<any, any>;
  toolRows: Map<any, any>;
  shellRows: Map<any, any>;
  agentBlockRows: Map<any, any>;
  documentRef: Document;
  clearPlaceholder(): void;
  setPlaceholderCleared(value: boolean): void;
  ensureActivityRow(): void;
  setCounter(el: HTMLElement | null, value: number): void;
  setActivity(label: string, active: boolean): void;
  clearReasoningRibbon(): void;
  setStatusDot(status: string | null): void;
  maybeAutoScroll(force?: boolean): void;
  resetPlanState(): void;
  syncPlanOverlayUi(): void;
  timelineStickyUpdate(): void;
  currentExtensionId(): string;
  buildRow(kind: string, title: string): { row: HTMLElement; body: HTMLElement };
  appendErrorContent(body: HTMLElement, entry: AnyRecord): void;
  renderCommandResult(entry: AnyRecord, parentEl: HTMLElement, options?: AnyRecord): void;
  renderViewCard(entry: AnyRecord, parentEl: HTMLElement): void;
  renderSearchCard(entry: AnyRecord, parentEl: HTMLElement): void;
  renderApproval(entry: AnyRecord, options?: AnyRecord): void;
  buildReplayToolRow(entry: AnyRecord): HTMLElement;
  renderShellCmdRibbon(el: HTMLElement, cmd: string): void;
  highlightCodeAlways(text: string, lang: string): string;
  detectLangFromCommand(command: string): string;
  escapeHtml(text: string): string;
  toRelativePath(path: string): string;
  postTe2OpenRequest(target: { path?: unknown; line?: unknown; column?: unknown }): unknown;
  makeCollapsible(row: HTMLElement, cardId: string, startExpanded: boolean, options?: AnyRecord): void;
  addMessage(role: string, text: string, parentEl?: HTMLElement | null, metadata?: AnyRecord | null): void;
  finalizeReasoning(id: string | null | undefined, text: string, parentEl?: HTMLElement | null, metadata?: AnyRecord | null): void;
  addDiff(id: string, text: string, path: string, parentEl?: HTMLElement | null, metadata?: AnyRecord | null): void;
  renderPlanCard(steps: AnyRecord[], parentEl?: HTMLElement | null, metadata?: AnyRecord | null): void;
  updateTokens(total: number): void;
  updateContextRemaining(total: number, windowSize: number): void;
  applyRuntimeMode(kind: string): void;
  measureRowHeight(): void;
  updateSpacerHeights(): void;
}

export function bindTimelineReplay(ctx: TimelineReplayContext) {
  const {
    getState,
    setState,
    timelineEl,
    counterMessagesEl,
    counterTokensEl,
    contextRemainingEl,
    assistantRows,
    reasoningRows,
    diffRows,
    toolRows,
    shellRows,
    agentBlockRows,
    documentRef,
    clearPlaceholder,
    setPlaceholderCleared,
    ensureActivityRow,
    setCounter,
    setActivity,
    clearReasoningRibbon,
    setStatusDot,
    maybeAutoScroll,
    resetPlanState,
    syncPlanOverlayUi,
    timelineStickyUpdate,
    currentExtensionId,
    buildRow,
    appendErrorContent,
    renderCommandResult,
    renderViewCard,
    renderSearchCard,
    renderApproval,
    buildReplayToolRow,
    renderShellCmdRibbon,
    highlightCodeAlways,
    detectLangFromCommand,
    escapeHtml,
    toRelativePath,
    postTe2OpenRequest,
    makeCollapsible,
    addMessage,
    finalizeReasoning,
    addDiff,
    renderPlanCard,
    updateTokens,
    updateContextRemaining,
    applyRuntimeMode,
    measureRowHeight,
    updateSpacerHeights,
  } = ctx;

  function resetTimeline() {
    initializeReplayWindow({ bumpGeneration: true, showPlaceholder: true });
  }

  function initializeReplayWindow(options: { bumpGeneration: boolean; showPlaceholder: boolean }) {
    if (!timelineEl) return;
    timelineEl.innerHTML = '';
    assistantRows.clear();
    reasoningRows.clear();
    diffRows.clear();
    toolRows.clear();
    shellRows.clear();
    agentBlockRows.clear();
    resetPlanState();
    const topSpacerEl = documentRef.createElement('div');
    topSpacerEl.className = 'timeline-spacer';
    const currentGeneration = Number(getState().transcriptGeneration) || 0;
    const nextState: Record<string, unknown> = {
      topSpacerEl,
      bottomSpacerEl: null,
      messageCount: 0,
      tokenCount: 0,
      lastEventType: null,
      contextWindow: null,
      transcriptHistoryMode: false,
      transcriptLiveDirty: false,
    };
    if (options.bumpGeneration) {
      nextState.transcriptTotal = 0;
      nextState.transcriptStart = 0;
      nextState.transcriptEnd = 0;
      nextState.transcriptLoading = false;
      nextState.transcriptGeneration = currentGeneration + 1;
    }
    setPlaceholderCleared(!options.showPlaceholder);
    setState(nextState);
    setCounter(counterMessagesEl, 0);
    setCounter(counterTokensEl, 0);
    if (contextRemainingEl) contextRemainingEl.textContent = '—';
    setActivity('idle', false);
    setStatusDot(null);
    clearReasoningRibbon();
    timelineEl.appendChild(topSpacerEl);
    if (options.showPlaceholder) {
      const placeholder = documentRef.createElement('div');
      placeholder.id = 'timeline-placeholder';
      placeholder.className = 'timeline-row muted';
      placeholder.textContent = 'Waiting for events...';
      timelineEl.appendChild(placeholder);
    }
    ensureActivityRow();
    if (options.showPlaceholder || options.bumpGeneration) {
      maybeAutoScroll(true);
    }
    timelineStickyUpdate();
  }

  function prepareTranscriptWindow() {
    initializeReplayWindow({ bumpGeneration: false, showPlaceholder: false });
  }

  function isInternalTranscriptItem(entry: AnyRecord) {
    if (!entry || typeof entry !== 'object') return false;
    if (entry.internal === true) return true;
    if (typeof entry.internal === 'string' && ['1', 'true', 'yes', 'on'].includes(entry.internal.trim().toLowerCase())) {
      return true;
    }
    return typeof entry.visibility === 'string' && entry.visibility.trim().toLowerCase() === 'internal';
  }

  function renderTranscriptEntries(items: AnyRecord[], opts: AnyRecord = {}) {
    if (!items || !items.length || !timelineEl) return;
    const state = getState();
    const fragment = documentRef.createDocumentFragment();
    const pendingAgentPtyTerms: AnyRecord[] = [];
    const agentPtyByBlock = new Map();
    const replaySubagents = new Map();

    items.forEach((entry) => {
      if (isInternalTranscriptItem(entry)) return;
      if (!entry || !entry.role) return;

      if (entry.role === 'subagent_start') {
        const existing = timelineEl.querySelector(`.subagent-card[data-subagent-id="${entry.id}"]`);
        if (existing instanceof HTMLElement) {
          const labelEl = existing.querySelector('.subagent-header span:first-child');
          if (labelEl instanceof HTMLElement) {
            labelEl.textContent = `${entry.name || 'subagent'}: ${entry.intent || 'working'}`;
          }
          return;
        }
        const row = documentRef.createElement('div');
        row.className = 'timeline-row subagent-card';
        row.dataset.subagentId = entry.id;
        const header = documentRef.createElement('div');
        header.className = 'subagent-header command-ribbon';
        const label = documentRef.createElement('span');
        label.textContent = `${entry.name || 'subagent'}: ${entry.intent || 'working'}`;
        const statusEl = documentRef.createElement('span');
        statusEl.className = 'subagent-status';
        statusEl.textContent = '⏳ running';
        header.append(label, statusEl);
        row.appendChild(header);
        const body = documentRef.createElement('div');
        body.className = 'subagent-body';
        row.appendChild(body);
        makeCollapsible(row, `subagent:${entry.id}`, false, {
          headerEl: header,
          fullHeaderToggle: true,
        });
        applyTranscriptCardMetadata(row, entry);
        replaySubagents.set(entry.id, { row, body, statusEl, label });
        fragment.appendChild(row);
        return;
      }

      if (entry.role === 'subagent_end') {
        let subagent = replaySubagents.get(entry.id);
        if (!subagent) {
          const existing = timelineEl.querySelector(`.subagent-card[data-subagent-id="${entry.id}"]`);
          if (existing instanceof HTMLElement) {
            subagent = {
              statusEl: existing.querySelector('.subagent-status'),
              body: existing.querySelector('.subagent-body'),
            };
          }
        }
        if (subagent) {
          if (subagent.statusEl instanceof HTMLElement) {
            subagent.statusEl.textContent = entry.success !== false ? '✓ done' : '✗ failed';
          }
          if (entry.summary) {
            const summaryEl = documentRef.createElement('div');
            summaryEl.className = 'subagent-summary';
            summaryEl.style.cssText = 'padding: 4px 14px; font-size: 0.85em; opacity: 0.7; font-style: italic;';
            summaryEl.textContent = entry.summary;
            if (subagent.body instanceof HTMLElement) subagent.body.appendChild(summaryEl);
          }
        }
        return;
      }

      function getTarget(): HTMLElement {
        if (entry.subagent_id) {
          if (state.debugEnabled) console.log('[SUBAGENT-REPLAY] entry has subagent_id:', entry.subagent_id, 'role:', entry.role, 'map has:', replaySubagents.has(entry.subagent_id));
          let subagent = replaySubagents.get(entry.subagent_id);
          if (!subagent) {
            if (state.debugEnabled) console.log('[SUBAGENT-REPLAY] Creating synthetic container for:', entry.subagent_id);
            const row = documentRef.createElement('div');
            row.className = 'timeline-row subagent-card';
            row.dataset.subagentId = entry.subagent_id;
            const header = documentRef.createElement('div');
            header.className = 'subagent-header command-ribbon';
            const label = documentRef.createElement('span');
            label.textContent = 'subagent: (earlier in transcript)';
            const statusEl = documentRef.createElement('span');
            statusEl.className = 'subagent-status';
            statusEl.textContent = '✓ done';
            header.append(label, statusEl);
            row.appendChild(header);
            const body = documentRef.createElement('div');
            body.className = 'subagent-body';
            row.appendChild(body);
            makeCollapsible(row, `subagent:${entry.subagent_id}`, true, {
              headerEl: header,
              fullHeaderToggle: true,
            });
            applyTranscriptCardMetadata(row, entry);
            subagent = { row, body, statusEl, label };
            replaySubagents.set(entry.subagent_id, subagent);
            fragment.appendChild(row);
          }
          return subagent.body;
        }
        return fragment as unknown as HTMLElement;
      }

      if (entry.role === 'reasoning') {
        finalizeReasoning(entry.id || entry.item_id || 'reasoning', entry.text || '', getTarget(), entry);
        return;
      }

      if (entry.role === 'diff') {
        let diffPath = entry.path || '';
        if (!diffPath && entry.text) {
          const match = entry.text.match(/^diff --git a\/.+ b\/(.+)$/m);
          if (match) diffPath = match[1];
        }
        addDiff(entry.id || entry.item_id || diffPath || 'diff', entry.text || '', diffPath, getTarget(), entry);
        return;
      }

      if (entry.role === 'view') {
        renderViewCard({
          id: entry.id || entry.item_id || '',
          title: entry.title || '',
          path: entry.path || '',
          content: entry.content ?? entry.output ?? '',
          lines: Array.isArray(entry.lines) ? entry.lines : null,
          view_range: entry.view_range ?? entry.viewRange ?? null,
        }, getTarget());
        return;
      }

      if (entry.role === 'search') {
        renderSearchCard({
          id: entry.id || entry.item_id || '',
          title: entry.title || '',
          mode: entry.mode || entry.tool || 'search',
          path: entry.path || '',
          pattern: entry.pattern || '',
          arguments: entry.arguments && typeof entry.arguments === 'object' ? entry.arguments : {},
          content: entry.content ?? entry.result ?? '',
        }, getTarget());
        return;
      }

      if (entry.role === 'command') {
        renderCommandResult(entry, getTarget(), {
          linkPathFromRibbon: Boolean(entry.path),
          updateLiveState: false,
          autoScroll: false,
        });
        return;
      }

      if (entry.role === 'plan') {
        renderPlanCard(entry.steps || [], getTarget(), entry);
        return;
      }

      if (entry.role === 'token_usage') {
        if (Number.isFinite(entry.total)) {
          setState({ tokenCount: Number(entry.total) });
          updateTokens(Number(entry.total));
        }
        if (Number.isFinite(entry.context_window)) {
          setState({ contextWindow: Number(entry.context_window) });
          updateContextRemaining(entry.total, entry.context_window);
        }
        return;
      }

      if (entry.role === 'mode') {
        if (typeof entry.kind === 'string') {
          applyRuntimeMode(entry.kind);
        }
        return;
      }

      if (entry.role === 'status') {
        if (entry.status) setStatusDot(entry.status);
        return;
      }

      if (entry.role === 'context_compacted') {
        const row = documentRef.createElement('div');
        row.className = 'timeline-row system';
        applyTranscriptCardMetadata(row, entry);
        const meta = documentRef.createElement('div');
        meta.className = 'meta';
        meta.textContent = 'context compacted';
        const body = documentRef.createElement('div');
        body.className = 'body';
        const msg = documentRef.createElement('div');
        msg.className = 'system-message';
        msg.textContent = 'Context was compacted to fit within the model\'s context window.';
        body.appendChild(msg);
        row.append(meta, body);
        fragment.appendChild(row);
        return;
      }

      if (entry.role === 'shell') {
        const exitCode = entry.exit_code || 0;
        const row = documentRef.createElement('div');
        row.className = 'timeline-row command-result';
        applyTranscriptCardMetadata(row, entry);
        const body = documentRef.createElement('div');
        body.className = 'body';
        const cmdRibbon = documentRef.createElement('div');
        cmdRibbon.className = 'command-ribbon';
        const shellCmd = String(entry.command || '');
        renderShellCmdRibbon(cmdRibbon, shellCmd);
        body.appendChild(cmdRibbon);
        const pre = documentRef.createElement('pre');
        pre.className = 'command-output';
        const stdout = String(entry.stdout || '');
        const stderr = String(entry.stderr || '');
        const outLang = detectLangFromCommand(shellCmd);
        if (stdout) {
          if (outLang) {
            pre.innerHTML = highlightCodeAlways(stdout, outLang);
          } else {
            pre.appendChild(documentRef.createTextNode(stdout));
          }
        }
        if (entry.stderr) {
          const stderrEl = documentRef.createElement('span');
          stderrEl.className = 'shell-stderr';
          stderrEl.textContent = stderr;
          pre.appendChild(stderrEl);
        }
        if (!stdout && !stderr) {
          pre.textContent = '(no output)';
        }
        body.appendChild(pre);
        if (exitCode !== 0) {
          const footer = documentRef.createElement('div');
          footer.className = 'command-footer';
          footer.textContent = `exit ${exitCode}`;
          body.appendChild(footer);
        }
        row.appendChild(body);
        getTarget().appendChild(row);
        setStatusDot(exitCode === 0 ? 'success' : 'error');
        return;
      }

      if (entry.role === 'agent_pty') {
        const eventType = entry.event || entry.type;
        const block = entry.block || {};
        const blockId = entry.block_id || block.block_id || entry.blockId || 'agent';
        if (eventType === 'agent_block_begin') {
          const cmd = block.cmd || '';
          const row = documentRef.createElement('div');
          row.className = 'timeline-row command-result terminal-card';
          row.dataset.agentBlockId = blockId;
          applyTranscriptCardMetadata(row, entry);

          const body = documentRef.createElement('div');
          body.className = 'body';

          const cmdRibbon = documentRef.createElement('div');
          cmdRibbon.className = 'command-ribbon';
          cmdRibbon.textContent = cmd ? `$ ${cmd}` : '';
          body.appendChild(cmdRibbon);

          const termEl = documentRef.createElement('div');
          termEl.className = 'command-output';
          body.appendChild(termEl);

          row.appendChild(body);
          getTarget().appendChild(row);
          const rec = { row, termEl, cmdRibbon, cmd, buf: '', text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
          agentPtyByBlock.set(blockId, rec);
          agentBlockRows.set(blockId, rec);
          pendingAgentPtyTerms.push(rec);
          return;
        }
        if (eventType === 'agent_block_delta') {
          const delta = entry.delta || '';
          if (!delta) return;
          let rec = agentPtyByBlock.get(blockId) || agentBlockRows.get(blockId);
          if (!rec) {
            const row = documentRef.createElement('div');
            row.className = 'timeline-row command-result terminal-card';
            row.dataset.agentBlockId = blockId;
            applyTranscriptCardMetadata(row, entry);
            const body = documentRef.createElement('div');
            body.className = 'body';
            const cmdRibbon = documentRef.createElement('div');
            cmdRibbon.className = 'command-ribbon';
            cmdRibbon.textContent = '';
            body.appendChild(cmdRibbon);
            const termEl = documentRef.createElement('div');
            termEl.className = 'command-output';
            body.appendChild(termEl);
            row.appendChild(body);
            getTarget().appendChild(row);
            rec = { row, termEl, cmdRibbon, cmd: '', buf: '', text: '', screenRows: null, renderMode: 'raw', hasRawStream: false };
            agentPtyByBlock.set(blockId, rec);
            agentBlockRows.set(blockId, rec);
            pendingAgentPtyTerms.push(rec);
          }
          rec.buf += delta;
          return;
        }
        if (eventType === 'agent_block_end') {
          const rec = agentPtyByBlock.get(blockId) || agentBlockRows.get(blockId);
          if (rec && !rec.cmd && (block.cmd || '')) {
            rec.cmd = block.cmd || '';
            if (rec.cmdRibbon) {
              rec.cmdRibbon.textContent = `$ ${rec.cmd}`;
            }
          }
          const exitCode = block.exit_code ?? block.exitCode;
          if (rec && exitCode !== undefined && exitCode !== null && exitCode !== 0) {
            const footer = documentRef.createElement('div');
            footer.className = 'command-footer';
            footer.textContent = `exit ${exitCode}`;
            rec.row.querySelector('.body')?.appendChild(footer);
          }
          return;
        }
        return;
      }

      if (entry.role === 'error') {
        const { row, body } = buildRow('error', 'error');
        applyTranscriptCardMetadata(row, entry);
        appendErrorContent(body, entry);
        getTarget().appendChild(row);
        return;
      }

      if (entry.role === 'mcp_tool' || entry.role === 'tool') {
        const row = buildReplayToolRow(entry);
        getTarget().appendChild(row);
        return;
      }

      if (entry.role === 'web_search') {
        const row = documentRef.createElement('div');
        row.className = 'timeline-row command-result web-search-card';
        applyTranscriptCardMetadata(row, entry);
        const body = documentRef.createElement('div');
        body.className = 'body';
        const header = documentRef.createElement('div');
        header.className = 'command-ribbon';
        header.textContent = '🔍 web_search';
        body.appendChild(header);
        if (entry.query) {
          const queryPre = documentRef.createElement('pre');
          queryPre.textContent = entry.query;
          body.appendChild(queryPre);
        }
        row.appendChild(body);
        makeCollapsible(row, `web:${entry.call_id || entry.id || entry.query || 'search'}`, false);
        getTarget().appendChild(row);
        return;
      }

      if (entry.role === 'approval') {
        const requestId = entry.request_id || entry.id || entry.item_id;
        const askUserMsgId = entry.ask_user_msg_id ?? entry.askUserMsgId;
        const resolvedCardId = entry.card_id || entry.item_id || entry.id || requestId;
        renderApproval({
          ...entry,
          id: entry.id || resolvedCardId,
          request_id: requestId,
          card_id: resolvedCardId,
          ask_user_msg_id: askUserMsgId,
          request_method: entry.request_method || entry.requestMethod || '',
          request_params: entry.payload && typeof entry.payload === 'object' ? entry.payload : {},
          payload: entry.payload && typeof entry.payload === 'object' ? entry.payload : {},
          replay: true,
        }, {
          parentEl: getTarget(),
          readOnly: true,
          source: 'replay',
          useExisting: false,
          extensionId: currentExtensionId(),
        });
        return;
      }

      addMessage(entry.role, entry.text || '', getTarget(), entry);
    });

    clearPlaceholder();
    const nextState = getState();
    const insertBefore = opts.prepend
      ? ((nextState.planOverlayEl && nextState.planOverlayEl.parentElement === timelineEl) ? nextState.planOverlayEl.nextSibling : nextState.topSpacerEl?.nextSibling)
      : nextState.bottomSpacerEl;
    if (insertBefore && insertBefore.parentElement === timelineEl) {
      timelineEl.insertBefore(fragment, insertBefore);
    } else {
      timelineEl.appendChild(fragment);
    }
    syncPlanOverlayUi();

    if (pendingAgentPtyTerms.length) {
      requestAnimationFrame(() => {
        pendingAgentPtyTerms.forEach((rec) => {
          rec.termEl.textContent = rec.buf || '';
        });
      });
    }
    measureRowHeight();
    updateSpacerHeights();
  }

  return {
    prepareTranscriptWindow,
    resetTimeline,
    renderTranscriptEntries,
  };
}
