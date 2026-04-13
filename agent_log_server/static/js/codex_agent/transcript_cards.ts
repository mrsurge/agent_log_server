export function bindTranscriptCards(ctx) {
  const {
    getConversationSettings,
    clearPlaceholder,
    createRow,
    makeCollapsible,
    getLiveEventParent,
    getBottomSpacerEl,
    timelineEl,
    maybeAutoScroll,
    setLastEventType,
    setStatusDot,
    renderShellCmdRibbon,
    detectLangFromCommand,
    highlightCodeAlways,
    detectLangFromPath,
    toRelativePath,
    postTe2OpenRequest,
    buildViewCardTitle,
    normalizeStructuredViewLines,
    synthesizeStructuredViewLines,
    renderStructuredViewLineTable,
    openSplashSettingsModal,
    addMessage,
    escapeHtml,
  } = ctx;

  function getCommandOutputLineLimit() {
    const settings = typeof getConversationSettings === 'function' ? getConversationSettings() : {};
    return settings?.commandOutputLines || 20;
  }

  function mountRow(row, parentEl = null, evt = null) {
    const targetEl = parentEl || (evt ? getLiveEventParent(evt) : null);
    clearPlaceholder();
    if (targetEl) {
      targetEl.appendChild(row);
      return;
    }
    const bottomSpacerEl = typeof getBottomSpacerEl === 'function' ? getBottomSpacerEl() : null;
    if (bottomSpacerEl && bottomSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(row, bottomSpacerEl);
    } else {
      timelineEl?.appendChild(row);
    }
  }

  function appendTruncationNote(container, text, asSpan = false) {
    if (!container || !text) return;
    const note = document.createElement(asSpan ? 'span' : 'div');
    note.className = 'truncation-note';
    note.textContent = text;
    container.appendChild(note);
  }

  function ansiToHtml(text) {
    const input = String(text || '');
    const sgrRe = /\x1b\[([0-9;]*)m/g;
    let lastIndex = 0;
    let html = '';
    let state = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false };

    function cssFor(st) {
      const styles = [];
      if (st.bold) styles.push('font-weight:600');
      if (st.dim) styles.push('opacity:0.8');
      if (st.italic) styles.push('font-style:italic');
      if (st.underline) styles.push('text-decoration:underline');
      const fgMap = {
        30: '#000000', 31: '#e06c75', 32: '#98c379', 33: '#e5c07b', 34: '#61afef', 35: '#c678dd', 36: '#56b6c2', 37: '#abb2bf',
        90: '#5c6370', 91: '#ff7a85', 92: '#b7f39b', 93: '#ffd68a', 94: '#7ab7ff', 95: '#e79aff', 96: '#7ae8f5', 97: '#ffffff',
      };
      const bgMap = {
        40: '#000000', 41: '#e06c75', 42: '#98c379', 43: '#e5c07b', 44: '#61afef', 45: '#c678dd', 46: '#56b6c2', 47: '#abb2bf',
        100: '#5c6370', 101: '#ff7a85', 102: '#b7f39b', 103: '#ffd68a', 104: '#7ab7ff', 105: '#e79aff', 106: '#7ae8f5', 107: '#ffffff',
      };
      let fg = st.fg;
      let bg = st.bg;
      if (st.inverse) {
        const tmp = fg;
        fg = bg;
        bg = tmp;
      }
      if (fg != null && fgMap[fg]) styles.push(`color:${fgMap[fg]}`);
      if (bg != null && bgMap[bg]) styles.push(`background-color:${bgMap[bg]}`);
      return styles.join(';');
    }

    function applyCodes(codes) {
      const parts = codes.length ? codes.split(';') : ['0'];
      for (const part of parts) {
        const n = Number(part || '0');
        if (!Number.isFinite(n)) continue;
        if (n === 0) state = { fg: null, bg: null, bold: false, dim: false, italic: false, underline: false, inverse: false };
        else if (n === 1) state.bold = true;
        else if (n === 2) state.dim = true;
        else if (n === 3) state.italic = true;
        else if (n === 4) state.underline = true;
        else if (n === 7) state.inverse = true;
        else if (n === 22) {
          state.bold = false;
          state.dim = false;
        } else if (n === 23) state.italic = false;
        else if (n === 24) state.underline = false;
        else if (n === 27) state.inverse = false;
        else if (n === 39) state.fg = null;
        else if (n === 49) state.bg = null;
        else if ((n >= 30 && n <= 37) || (n >= 90 && n <= 97)) state.fg = n;
        else if ((n >= 40 && n <= 47) || (n >= 100 && n <= 107)) state.bg = n;
      }
    }

    function emitChunk(chunk) {
      if (!chunk) return;
      const css = cssFor(state);
      const escaped = escapeHtml(chunk);
      if (css) html += `<span style="${css}">${escaped}</span>`;
      else html += escaped;
    }

    let match;
    while ((match = sgrRe.exec(input)) !== null) {
      emitChunk(input.slice(lastIndex, match.index));
      applyCodes(match[1] || '');
      lastIndex = sgrRe.lastIndex;
    }
    emitChunk(input.slice(lastIndex));
    return html;
  }

  function renderCommandResult(evt, parentEl = null, options: Record<string, any> = {}) {
    const {
      linkPathFromRibbon = false,
      updateLiveState = true,
      autoScroll = updateLiveState,
    } = options;
    const command = evt.command || '';
    const prompt = evt.prompt || '';
    const agentBlockId = evt.agent_block_id || evt.agentBlockId || '';
    const output = evt.output || '';
    const exitCode = evt.exit_code ?? evt.exitCode;
    const durationMs = evt.duration_ms ?? evt.durationMs;
    const truncateLines = getCommandOutputLineLimit();

    let displayOutput = output;
    let truncated = false;
    let totalLines = 0;
    if (output) {
      const lines = output.split('\n');
      totalLines = lines.length;
      if (lines.length > truncateLines) {
        displayOutput = lines.slice(0, truncateLines).join('\n');
        truncated = true;
      }
    }

    if (agentBlockId) {
      try {
        const dup = timelineEl?.querySelector(`.timeline-row.terminal-card[data-agent-block-id="${CSS.escape(agentBlockId)}"]`);
        if (dup && dup.parentElement) dup.parentElement.removeChild(dup);
      } catch (_) {}
    }

    const row = document.createElement('div');
    row.className = 'timeline-row command-result';

    const body = document.createElement('div');
    body.className = 'body';

    const cmdRibbon = document.createElement('div');
    cmdRibbon.className = 'command-ribbon';
    const isUserTerminal = evt.source === 'user_terminal' || evt.source === 'user-terminal';
    const ribbonText = prompt ? `${prompt}${command}` : command;
    if (isUserTerminal && typeof ribbonText === 'string' && ribbonText.includes('\x1b[')) {
      cmdRibbon.innerHTML = ansiToHtml(ribbonText);
    } else if (!isUserTerminal) {
      renderShellCmdRibbon(cmdRibbon, command);
    } else {
      cmdRibbon.textContent = ribbonText;
    }
    if (linkPathFromRibbon && evt.path) {
      cmdRibbon.style.cursor = 'pointer';
      cmdRibbon.title = evt.path;
      cmdRibbon.dataset.hasClickHandler = 'true';
      const path = evt.path;
      const line = Number.isFinite(Number(evt.line)) ? Number(evt.line) : 1;
      cmdRibbon.addEventListener('click', (e) => {
        const target = e.target;
        if (target instanceof Element && (target.closest('.twisty') || target.closest('.ribbon-toggle-zone'))) return;
        postTe2OpenRequest({ path, line, column: 1 });
      });
    }
    body.appendChild(cmdRibbon);

    if (displayOutput) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'command-output';
      const hasAnsi = typeof displayOutput === 'string' && displayOutput.includes('\x1b[');
      if (isUserTerminal && hasAnsi) {
        outputPre.innerHTML = ansiToHtml(displayOutput);
        if (truncated) {
          appendTruncationNote(outputPre, `\n... (truncated, showing ${truncateLines} of ${totalLines} lines)`, true);
        }
      } else {
        const lang = detectLangFromCommand(command);
        if (lang) {
          outputPre.innerHTML = highlightCodeAlways(displayOutput, lang);
        } else {
          outputPre.textContent = displayOutput;
        }
        if (truncated) {
          if (lang) {
            appendTruncationNote(outputPre, `\n... (truncated, showing ${truncateLines} of ${totalLines} lines)`, true);
          } else {
            outputPre.textContent += `\n... (truncated, showing ${truncateLines} of ${totalLines} lines)`;
          }
        }
      }
      body.appendChild(outputPre);
    }

    const footer = document.createElement('div');
    footer.className = 'command-footer';
    const parts = [];
    if (exitCode !== undefined && exitCode !== null && exitCode !== 0) {
      parts.push(`Exit: ${exitCode}`);
    }
    if (durationMs !== undefined && durationMs !== null) {
      parts.push(`Duration: ${durationMs}ms`);
    }
    if (parts.length) {
      footer.textContent = parts.join(' | ');
      body.appendChild(footer);
    }

    row.appendChild(body);
    makeCollapsible(row, `cmd:${evt.id || agentBlockId || command.slice(0, 40)}`, false);
    mountRow(row, parentEl, evt);

    if (updateLiveState) {
      setLastEventType('command');
      if (autoScroll) maybeAutoScroll();
      if (exitCode === 0 || exitCode === undefined || exitCode === null) {
        setStatusDot('success');
      } else {
        setStatusDot('error');
      }
    }
  }

  function renderViewCard(evt, parentEl = null) {
    const content = evt.content ?? evt.output ?? '';
    const path = typeof evt.path === 'string' ? evt.path : '';
    const viewRange = Array.isArray(evt.view_range) ? evt.view_range : (Array.isArray(evt.viewRange) ? evt.viewRange : null);
    const structuredLines = normalizeStructuredViewLines(evt.lines) ?? synthesizeStructuredViewLines(content, viewRange);
    const title = evt.title || buildViewCardTitle(path, viewRange, 'view');
    const truncateLines = getCommandOutputLineLimit();

    let displayContent = typeof content === 'string' ? content : String(content ?? '');
    let displayLines = structuredLines;
    let truncated = false;
    let totalLineCount = 0;
    if (displayLines) {
      totalLineCount = displayLines.length;
      if (displayLines.length > truncateLines) {
        displayLines = displayLines.slice(0, truncateLines);
        truncated = true;
      }
    } else if (displayContent) {
      const lines = displayContent.split('\n');
      totalLineCount = lines.length;
      if (lines.length > truncateLines) {
        displayContent = lines.slice(0, truncateLines).join('\n');
        truncated = true;
      }
    }

    const row = document.createElement('div');
    row.className = 'timeline-row command-result view-card';

    const body = document.createElement('div');
    body.className = 'body';

    const ribbon = document.createElement('div');
    ribbon.className = 'command-ribbon';
    ribbon.textContent = title;
    body.appendChild(ribbon);

    if (path) {
      const pathLine = document.createElement('div');
      pathLine.className = 'view-card-path';
      pathLine.textContent = toRelativePath(path);
      pathLine.title = path;
      pathLine.style.cursor = 'pointer';
      pathLine.dataset.hasClickHandler = 'true';
      pathLine.addEventListener('click', (e) => {
        e.stopPropagation();
        const preferredLine = displayLines?.[0]?.line_no
          ?? (Array.isArray(viewRange) && Number.isFinite(Number(viewRange[0])) ? Number(viewRange[0]) : 1);
        postTe2OpenRequest({ path, line: preferredLine, column: 1 });
      });
      body.appendChild(pathLine);
    }

    if (displayLines) {
      body.appendChild(renderStructuredViewLineTable(displayLines, path));
    } else {
      const outputPre = document.createElement('pre');
      outputPre.className = 'command-output';
      const lang = detectLangFromPath(path);
      if (lang) {
        outputPre.innerHTML = highlightCodeAlways(displayContent, lang);
      } else {
        outputPre.textContent = displayContent;
      }
      body.appendChild(outputPre);
    }

    if (truncated) {
      appendTruncationNote(body, `... (truncated, showing ${truncateLines} of ${totalLineCount} lines)`);
    }

    row.appendChild(body);
    makeCollapsible(row, `view:${evt.id || path || title}`, false);
    mountRow(row, parentEl, evt);

    setLastEventType('view');
    maybeAutoScroll();
    setStatusDot('success');
  }

  function resolveSearchEntryPath(rawPath, rootPath) {
    if (!rawPath) return '';
    if (rawPath.startsWith('/')) return rawPath;
    if (!rootPath) return rawPath;
    return `${rootPath.replace(/\/+$/, '')}/${rawPath.replace(/^\.?\//, '')}`;
  }

  function shortenSearchTarget(path) {
    const relativePath = toRelativePath(path || '');
    if (!relativePath) return '';
    const parts = relativePath.split('/').filter(Boolean);
    if (parts.length <= 3) return relativePath;
    return `.../${parts.slice(-3).join('/')}`;
  }

  function formatSearchArgumentValue(key, value) {
    if (value === undefined || value === null) return '';
    if (value === '') return '';
    if (Array.isArray(value) && value.length === 0) return '';
    if (typeof value === 'object') {
      try {
        return JSON.stringify(value);
      } catch (_) {
        return String(value);
      }
    }
    if ((key === 'path' || key === 'root' || key === 'cwd') && typeof value === 'string') {
      return toRelativePath(value);
    }
    return String(value);
  }

  function buildSearchDetailText(mode, rootPath, pattern, args) {
    const merged: Record<string, any> = {};
    if (args && typeof args === 'object') {
      Object.entries(args).forEach(([key, value]) => {
        if (value === undefined || value === null || value === '') return;
        if (Array.isArray(value) && value.length === 0) return;
        merged[key] = value;
      });
    }
    if (!merged.path && rootPath) merged.path = rootPath;
    if (!merged.pattern && pattern) merged.pattern = pattern;

    const preferredKeys = ['pattern', 'path', 'glob', 'type', 'output_mode', 'n', 'i', 'A', 'B', 'C', 'head_limit', 'multiline'];
    const detailLines = [`mode: ${mode}`];

    preferredKeys.forEach((key) => {
      if (!Object.prototype.hasOwnProperty.call(merged, key)) return;
      const formatted = formatSearchArgumentValue(key, merged[key]);
      if (formatted) detailLines.push(`${key}: ${formatted}`);
      delete merged[key];
    });

    Object.entries(merged).forEach(([key, value]) => {
      const formatted = formatSearchArgumentValue(key, value);
      if (formatted) detailLines.push(`${key}: ${formatted}`);
    });

    return detailLines.join('\n');
  }

  function parseSearchCardEntries(mode, content, rootPath = '') {
    const text = typeof content === 'string' ? content : String(content ?? '');
    const lines = text.split('\n').map((line) => line.replace(/\r$/, ''));
    if (mode === 'glob') {
      return { entries: [], plainText: text };
    }

    const entries = [];
    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      const match = line.match(/^(.+?):(\d+)(?::(\d+))?:(.*)$/);
      if (!match) continue;
      const resolvedPath = resolveSearchEntryPath(match[1], rootPath);
      entries.push({
        path: resolvedPath,
        line: Number(match[2]),
        column: match[3] ? Number(match[3]) : 1,
        preview: match[4] || '',
      });
    }
    return {
      entries,
      plainText: entries.length ? '' : text,
    };
  }

  function renderSearchCard(evt, parentEl = null) {
    const mode = evt.mode || evt.tool || 'search';
    const pattern = typeof evt.pattern === 'string' ? evt.pattern : '';
    const rootPath = typeof evt.path === 'string' ? evt.path : '';
    const searchArgs = evt.arguments && typeof evt.arguments === 'object' ? evt.arguments : {};
    const { entries, plainText } = parseSearchCardEntries(mode, evt.content ?? evt.result ?? '', rootPath);

    const row = document.createElement('div');
    row.className = 'timeline-row command-result search-card';

    const body = document.createElement('div');
    body.className = 'body';

    const ribbon = document.createElement('div');
    ribbon.className = 'command-ribbon';
    const shortTarget = shortenSearchTarget(rootPath);
    const ribbonBase = mode === 'web_search' ? 'web_search' : 'search';
    ribbon.textContent = shortTarget ? `${ribbonBase} ${shortTarget}` : ribbonBase;
    if (rootPath) ribbon.title = rootPath;
    body.appendChild(ribbon);

    const detailLine = document.createElement('pre');
    detailLine.className = 'search-card-detail';
    detailLine.textContent = buildSearchDetailText(mode, rootPath, pattern, searchArgs);
    body.appendChild(detailLine);

    if (entries.length) {
      const list = document.createElement('div');
      list.className = 'search-card-list';
      entries.forEach((entry) => {
        const item = document.createElement('div');
        item.className = 'search-card-entry';

        const pathLine = document.createElement('div');
        pathLine.className = 'search-card-path';
        pathLine.textContent = entry.line ? `${toRelativePath(entry.path)}:${entry.line}` : toRelativePath(entry.path);
        pathLine.title = entry.path;
        pathLine.style.cursor = 'pointer';
        pathLine.dataset.hasClickHandler = 'true';
        pathLine.addEventListener('click', (e) => {
          e.stopPropagation();
          postTe2OpenRequest({ path: entry.path, line: entry.line || 1, column: entry.column || 1 });
        });
        item.appendChild(pathLine);

        if (entry.preview) {
          const preview = document.createElement('pre');
          preview.className = 'search-card-preview';
          const lang = detectLangFromPath(entry.path);
          if (lang) {
            preview.innerHTML = highlightCodeAlways(entry.preview, lang);
          } else {
            preview.textContent = entry.preview;
          }
          item.appendChild(preview);
        }

        list.appendChild(item);
      });
      body.appendChild(list);
    } else {
      const plain = document.createElement('pre');
      plain.className = 'search-card-plain';
      const lang = detectLangFromPath(rootPath);
      if (lang && plainText) {
        plain.innerHTML = highlightCodeAlways(plainText, lang);
      } else {
        plain.textContent = plainText;
      }
      body.appendChild(plain);
    }

    row.appendChild(body);
    makeCollapsible(row, `search:${evt.id || pattern || mode}`, false);
    mountRow(row, parentEl, evt);

    setLastEventType('search');
    maybeAutoScroll();
    setStatusDot('success');
  }

  function normalizeErrorPayload(raw) {
    if (typeof raw === 'string') {
      return {
        message: raw,
        errorType: '',
        statusCode: null,
        providerCallId: '',
        stack: '',
        details: '',
        source: '',
        code: null,
      };
    }
    const payload = raw && typeof raw === 'object' ? raw : {};
    const message = typeof payload.message === 'string' && payload.message
      ? payload.message
      : (typeof payload.text === 'string' ? payload.text : '');
    const errorType = typeof payload.error_type === 'string' && payload.error_type
      ? payload.error_type
      : (typeof payload.errorType === 'string' ? payload.errorType : '');
    const statusRaw = payload.status_code ?? payload.statusCode;
    const statusCode = Number.isFinite(Number(statusRaw)) ? Number(statusRaw) : null;
    const providerCallId = typeof payload.provider_call_id === 'string' && payload.provider_call_id
      ? payload.provider_call_id
      : (typeof payload.providerCallId === 'string' ? payload.providerCallId : '');
    const stack = typeof payload.stack === 'string' ? payload.stack : '';
    const details = typeof payload.details === 'string' && payload.details
      ? payload.details
      : (typeof payload.additional_details === 'string' ? payload.additional_details : '');
    const source = typeof payload.source === 'string' && payload.source
      ? payload.source
      : (typeof payload.event === 'string' ? payload.event : '');
    const code = payload.code ?? null;
    return {
      message,
      errorType,
      statusCode,
      providerCallId,
      stack,
      details,
      source,
      code,
    };
  }

  function appendErrorContent(body, raw) {
    if (!body) return;
    const payload = normalizeErrorPayload(raw);
    if (payload.message) {
      const pre = document.createElement('pre');
      pre.className = 'error-text';
      pre.textContent = payload.message;
      body.appendChild(pre);
    }

    const metaParts = [];
    if (payload.errorType) metaParts.push(`type: ${payload.errorType}`);
    if (payload.statusCode !== null) metaParts.push(`status: ${payload.statusCode}`);
    if (payload.code !== null && payload.code !== '') metaParts.push(`code: ${payload.code}`);
    if (payload.providerCallId) metaParts.push(`provider_call_id: ${payload.providerCallId}`);
    if (payload.source) metaParts.push(`source: ${payload.source}`);
    if (metaParts.length) {
      const meta = document.createElement('div');
      meta.className = 'command-footer';
      meta.textContent = metaParts.join(' | ');
      body.appendChild(meta);
    }

    if (payload.details && payload.details !== payload.message) {
      const detailPre = document.createElement('pre');
      detailPre.className = 'error-text';
      detailPre.textContent = payload.details;
      body.appendChild(detailPre);
    }

    if (payload.stack && payload.stack !== payload.message && payload.stack !== payload.details) {
      const stackPre = document.createElement('pre');
      stackPre.className = 'error-text';
      stackPre.textContent = payload.stack;
      body.appendChild(stackPre);
    }
  }

  function renderErrorCard(raw) {
    const payload = normalizeErrorPayload(raw);
    if (!payload.message && !payload.details && !payload.stack) return;
    clearPlaceholder();
    const { body } = createRow('error', 'error');
    appendErrorContent(body, payload);
    setLastEventType('error');
    maybeAutoScroll();
  }

  function handleWarningAction(action) {
    if (!action || typeof action !== 'object') return;
    const actionId = typeof action.id === 'string' ? action.id.trim() : '';
    if (actionId === 'open_splash_settings') {
      openSplashSettingsModal?.();
    }
  }

  function renderWarningCard(message, action = null) {
    if (!message) return;
    clearPlaceholder();
    const { body } = createRow('warning', 'warning');
    const pre = document.createElement('pre');
    pre.className = 'warning-text';
    pre.textContent = message;
    body.appendChild(pre);
    if (action && typeof action === 'object') {
      const label = typeof action.label === 'string' ? action.label.trim() : '';
      if (label) {
        const actions = document.createElement('div');
        actions.className = 'warning-actions';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn tiny';
        button.textContent = label;
        button.addEventListener('click', () => handleWarningAction(action));
        actions.appendChild(button);
        body.appendChild(actions);
      }
    }
    setLastEventType('warning');
    maybeAutoScroll();
  }

  function renderContextCompactedCard() {
    clearPlaceholder();
    const { body } = createRow('system', 'context compacted');
    const msg = document.createElement('div');
    msg.className = 'system-message';
    msg.textContent = 'Context was compacted to fit within the model\'s context window. Some earlier conversation history may have been summarized or dropped.';
    body.appendChild(msg);
    setLastEventType('system');
    maybeAutoScroll();
  }

  function renderMetaEnvelopeInjected(evt) {
    const commandCount = evt.command_count ?? evt.commandCount ?? 0;
    const envelopeJson = evt.envelope_json ?? evt.envelopeJson ?? '';
    const pretty = (() => {
      try {
        return JSON.stringify(JSON.parse(envelopeJson), null, 2);
      } catch {
        return String(envelopeJson || '');
      }
    })();
    const text = [
      'CODEX_META injected (debug):',
      `commands: ${commandCount}`,
      '',
      '\\u001eCODEX_META ' + pretty + '\\u001f',
    ].join('\n');
    if (typeof addMessage === 'function') {
      addMessage('meta', text);
    }
  }

  return {
    appendErrorContent,
    renderCommandResult,
    renderViewCard,
    renderSearchCard,
    renderErrorCard,
    renderWarningCard,
    renderContextCompactedCard,
    renderMetaEnvelopeInjected,
  };
}
