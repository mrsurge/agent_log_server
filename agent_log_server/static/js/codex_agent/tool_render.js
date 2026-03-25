export function bindToolRender(ctx) {
  const {
    toolRows,
    clearPlaceholder,
    insertRow,
    makeCollapsible,
    getLiveEventParent,
    renderMarkdownSourceInto,
    formatDiff,
    toRelativePath,
    escapeHtml,
    renderShellCmdRibbon,
    maybeAutoScroll,
    setLastEventType,
    setStatusDot,
  } = ctx;

  function getToolRow(id, label, parentEl = null) {
    const key = id || `tool:${label || 'tool'}`;
    let entry = toolRows.get(key);
    if (!entry) {
      const row = document.createElement('div');
      row.className = 'timeline-row command-result mcp-tool-card';
      const body = document.createElement('div');
      body.className = 'body';
      const header = document.createElement('div');
      header.className = 'command-ribbon';
      header.textContent = label || 'tool';
      body.appendChild(header);
      const argsPre = document.createElement('pre');
      argsPre.className = 'mcp-tool-args';
      argsPre.textContent = '';
      body.appendChild(argsPre);
      row.appendChild(body);
      if (parentEl) {
        clearPlaceholder();
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      makeCollapsible(row, `tool:${key}`, false);
      entry = {
        row,
        body,
        argsPre,
        header,
        resultEl: null,
        streamEl: null,
        interactionEl: null,
        diffLabelEl: null,
        diffPre: null,
      };
      toolRows.set(key, entry);
    } else if (parentEl && entry.row.parentElement !== parentEl) {
      clearPlaceholder();
      parentEl.appendChild(entry.row);
      maybeAutoScroll();
    }
    return entry;
  }

  function resolveToolCardPath(toolName, payload = {}) {
    if (!payload || typeof payload !== 'object') return '';
    if (typeof payload.path === 'string' && payload.path.trim()) {
      return payload.path.trim();
    }
    const argumentsPayload = payload.arguments && typeof payload.arguments === 'object' ? payload.arguments : {};
    if (typeof argumentsPayload.path === 'string' && argumentsPayload.path.trim()) {
      return argumentsPayload.path.trim();
    }
    const candidateLists = [argumentsPayload.paths, payload.paths];
    for (const list of candidateLists) {
      if (!Array.isArray(list)) continue;
      const firstPath = list.find((value) => typeof value === 'string' && value.trim());
      if (typeof firstPath === 'string' && firstPath.trim()) {
        return firstPath.trim();
      }
    }
    return '';
  }

  function resolveToolCardDiff(toolName, payload = {}) {
    if (toolName !== 'apply_patch' || !payload || typeof payload !== 'object') return '';
    if (typeof payload.diff === 'string' && payload.diff.trim()) {
      return payload.diff;
    }
    const output = typeof payload.output === 'string' ? payload.output : '';
    if (/^(?:diff --git |@@ |\+\+\+ |--- )/m.test(output)) {
      return output;
    }
    return '';
  }

  function resolveToolCardOutcome(toolName, payload = {}) {
    if (toolName !== 'apply_patch' || !payload || typeof payload !== 'object') return '';
    const result = payload.result && typeof payload.result === 'object' ? payload.result : null;
    const status = typeof payload.status === 'string' ? payload.status.trim().toLowerCase() : '';
    const resultStatus = typeof result?.status === 'string' ? result.status.trim().toLowerCase() : '';
    const isError = payload.is_error === true || result?.isError === true || result?.success === false;
    if (isError || status === 'error' || status === 'failed' || resultStatus === 'error' || resultStatus === 'failed') {
      return 'error';
    }
    const isCompleted = (
      payload.duration_ms !== undefined
      || payload.output !== undefined
      || payload.result !== undefined
      || status === 'completed'
      || status === 'success'
      || resultStatus === 'completed'
      || resultStatus === 'success'
    );
    return isCompleted ? 'success' : '';
  }

  function applyPatchOutcomeEmoji(outcome) {
    if (outcome === 'success') return '🟢';
    if (outcome === 'error') return '🔴';
    return '';
  }

  function toolCardLabel(toolName, serverName = '', filePath = '') {
    if (toolName === 'apply_patch') {
      const resolvedPath = typeof filePath === 'string' && filePath.trim() ? filePath.trim() : '';
      const relPath = resolvedPath ? (toRelativePath(resolvedPath) || resolvedPath.split('/').pop() || resolvedPath) : '';
      const baseLabel = relPath ? `apply_patch ${relPath}` : 'apply_patch';
      return serverName ? `${serverName}:${baseLabel}` : baseLabel;
    }
    return serverName ? `${serverName}:${toolName}` : `tool:${toolName}`;
  }

  function renderToolCardHeader(headerEl, toolName, serverName = '', filePath = '', payload = {}) {
    if (!headerEl) return '';
    const label = toolCardLabel(toolName, serverName, filePath);
    if (toolName === 'apply_patch') {
      const outcomeEmoji = applyPatchOutcomeEmoji(resolveToolCardOutcome(toolName, payload));
      const ribbonCommand = outcomeEmoji ? `${label} ${outcomeEmoji}` : label;
      renderShellCmdRibbon(headerEl, ribbonCommand);
      return ribbonCommand;
    }
    const savedTwisty = headerEl.querySelector(':scope > .twisty') || headerEl.querySelector('.twisty');
    const savedToggle = headerEl.querySelector(':scope > .ribbon-toggle-zone') || headerEl.querySelector('.ribbon-toggle-zone');
    headerEl.textContent = label;
    if (savedTwisty) headerEl.appendChild(savedTwisty);
    if (savedToggle) headerEl.appendChild(savedToggle);
    return label;
  }

  function buildToolDiffPreview(diffText, filePath = '') {
    if (typeof diffText !== 'string' || !diffText.trim()) return null;
    const label = document.createElement('div');
    label.className = 'mcp-tool-arg-label mcp-tool-diff-label';
    const resolvedPath = typeof filePath === 'string' && filePath.trim() ? filePath.trim() : '';
    label.textContent = resolvedPath ? `Changes: ${toRelativePath(resolvedPath) || resolvedPath}` : 'Changes';
    const pre = document.createElement('pre');
    pre.className = 'diff-block mcp-tool-diff';
    pre.innerHTML = formatDiff(diffText, resolvedPath || null);
    return { label, pre };
  }

  function ensureToolDiffPreview(entry, diffText, filePath = '') {
    if (!entry || typeof diffText !== 'string' || !diffText.trim()) return;
    const preview = buildToolDiffPreview(diffText, filePath);
    if (!preview) return;
    if (!entry.diffLabelEl) {
      entry.diffLabelEl = preview.label;
      entry.body.insertBefore(entry.diffLabelEl, entry.argsPre);
    } else {
      entry.diffLabelEl.textContent = preview.label.textContent;
    }
    if (!entry.diffPre) {
      entry.diffPre = preview.pre;
      entry.body.insertBefore(entry.diffPre, entry.argsPre);
    } else {
      entry.diffPre.innerHTML = preview.pre.innerHTML;
    }
  }

  function isMarkdownishText(value) {
    return typeof value === 'string'
      && (value.includes('\n') || value.startsWith('#') || value.includes('**') || value.includes('`'));
  }

  function appendToolArguments(body, args) {
    if (!args || typeof args !== 'object' || Object.keys(args).length === 0) return;
    const argEntries = Object.entries(args);
    const hasMarkdownArg = argEntries.some(([, value]) => isMarkdownishText(value));
    if (hasMarkdownArg) {
      argEntries.forEach(([key, value]) => {
        const argLabel = document.createElement('div');
        argLabel.className = 'mcp-tool-arg-label';
        argLabel.textContent = `${key}:`;
        body.appendChild(argLabel);
        if (isMarkdownishText(value)) {
          const argContainer = document.createElement('div');
          argContainer.className = 'markdown-body mcp-tool-arg-value';
          renderMarkdownSourceInto(argContainer, value);
          body.appendChild(argContainer);
        } else {
          const argValue = document.createElement('pre');
          argValue.className = 'mcp-tool-arg-value-plain';
          argValue.textContent = typeof value === 'string' ? value : JSON.stringify(value);
          body.appendChild(argValue);
        }
      });
      return;
    }
    const argsPre = document.createElement('pre');
    argsPre.className = 'mcp-tool-args';
    const lines = [];
    argEntries.forEach(([key, value]) => {
      const renderedValue = typeof value === 'string' ? value : JSON.stringify(value);
      lines.push(`  ${key}: ${renderedValue}`);
    });
    argsPre.textContent = lines.join('\n');
    body.appendChild(argsPre);
  }

  function appendToolResult(body, result, isError) {
    if (result === undefined || result === null) return;
    const resultHeader = document.createElement('div');
    resultHeader.className = 'mcp-tool-result-header';
    resultHeader.textContent = '→';
    body.appendChild(resultHeader);

    if (typeof result === 'object') {
      const resultPre = document.createElement('pre');
      resultPre.className = 'mcp-tool-content';
      const lines = [];
      Object.entries(result).forEach(([key, value]) => {
        if (typeof value === 'object' && value !== null) {
          lines.push(`  ${key}:`);
          Object.entries(value).forEach(([innerKey, innerValue]) => {
            lines.push(`    ${innerKey}: ${JSON.stringify(innerValue)}`);
          });
        } else {
          lines.push(`  ${key}: ${JSON.stringify(value)}`);
        }
      });
      resultPre.textContent = lines.join('\n');
      if (isError) resultPre.classList.add('error-text');
      body.appendChild(resultPre);
      return resultPre;
    }

    if (isMarkdownishText(result)) {
      const resultContainer = document.createElement('div');
      resultContainer.className = 'markdown-body mcp-tool-result';
      renderMarkdownSourceInto(resultContainer, result);
      if (isError) resultContainer.classList.add('error-text');
      body.appendChild(resultContainer);
      return resultContainer;
    }

    const resultPre = document.createElement('pre');
    resultPre.className = 'mcp-tool-content';
    resultPre.textContent = String(result);
    if (isError) resultPre.classList.add('error-text');
    body.appendChild(resultPre);
    return resultPre;
  }

  function appendToolFooter(body, durationMs) {
    if (durationMs === undefined || durationMs === null) return;
    const footer = document.createElement('div');
    footer.className = 'command-footer';
    footer.textContent = `${durationMs}ms`;
    body.appendChild(footer);
  }

  function buildReplayToolRow(entry) {
    const row = document.createElement('div');
    row.className = 'timeline-row command-result mcp-tool-card';
    const body = document.createElement('div');
    body.className = 'body';

    const header = document.createElement('div');
    header.className = 'command-ribbon';
    const toolName = entry.tool || 'tool';
    const serverName = entry.server || '';
    const filePath = resolveToolCardPath(toolName, entry);
    renderToolCardHeader(header, toolName, serverName, filePath, entry);
    body.appendChild(header);

    appendToolArguments(body, entry.arguments);

    const diffText = resolveToolCardDiff(toolName, entry);
    if (diffText) {
      const preview = buildToolDiffPreview(diffText, filePath);
      if (preview) body.append(preview.label, preview.pre);
    }

    if (typeof entry.output === 'string' && entry.output) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'mcp-tool-content';
      outputPre.textContent = entry.output;
      if (entry.is_error && (entry.result === undefined || entry.result === null)) {
        outputPre.classList.add('error-text');
      }
      body.appendChild(outputPre);
    }

    appendToolResult(body, entry.result, entry.is_error === true);
    appendToolFooter(body, entry.duration_ms);

    row.appendChild(body);
    makeCollapsible(row, `tool:${entry.id || entry.item_id || `${entry.server || ''}:${entry.tool || ''}`}`, false);
    return row;
  }

  function renderToolBegin(evt) {
    const toolName = evt.tool || 'tool';
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const filePath = resolveToolCardPath(toolName, evt);
    const label = toolCardLabel(toolName, serverName, filePath);
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    renderToolCardHeader(entry.header, toolName, serverName, filePath, evt);

    const args = evt.arguments || evt.payload || {};
    const argEntries = Object.entries(args);
    const hasMarkdownArg = argEntries.some(([, value]) => isMarkdownishText(value));

    if (hasMarkdownArg) {
      entry.argsPre.style.display = 'none';
      argEntries.forEach(([key, value]) => {
        const argLabel = document.createElement('div');
        argLabel.className = 'mcp-tool-arg-label';
        argLabel.textContent = `${key}:`;
        entry.body.insertBefore(argLabel, entry.argsPre);
        if (isMarkdownishText(value)) {
          const argContainer = document.createElement('div');
          argContainer.className = 'markdown-body mcp-tool-arg-value';
          renderMarkdownSourceInto(argContainer, value);
          entry.body.insertBefore(argContainer, entry.argsPre);
        } else {
          const argValue = document.createElement('pre');
          argValue.className = 'mcp-tool-arg-value-plain';
          argValue.textContent = typeof value === 'string' ? value : JSON.stringify(value);
          entry.body.insertBefore(argValue, entry.argsPre);
        }
      });
    } else {
      const lines = [];
      argEntries.forEach(([key, value]) => {
        const renderedValue = typeof value === 'string' ? value : JSON.stringify(value);
        lines.push(`  ${key}: ${renderedValue}`);
      });
      if (lines.length) entry.argsPre.textContent = lines.join('\n');
    }

    ensureToolDiffPreview(entry, resolveToolCardDiff(toolName, evt), filePath);
    setLastEventType('tool');
  }

  function renderToolDelta(evt) {
    const toolName = evt.tool || 'tool';
    if (toolName === 'command' || toolName === 'shell') return;
    const entry = getToolRow(
      evt.id,
      toolCardLabel(toolName, evt.server || '', resolveToolCardPath(toolName, evt)),
      getLiveEventParent(evt),
    );
    const delta = evt.delta || '';
    if (delta) {
      if (!entry.streamEl) {
        const streamPre = document.createElement('pre');
        streamPre.className = 'mcp-tool-content';
        entry.body.appendChild(streamPre);
        entry.streamEl = streamPre;
      }
      entry.streamEl.textContent += delta;
    }
    setLastEventType('tool');
    maybeAutoScroll();
  }

  function renderToolEnd(evt) {
    const toolName = evt.tool || 'tool';
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const filePath = resolveToolCardPath(toolName, evt);
    const label = toolCardLabel(toolName, serverName, filePath);
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    renderToolCardHeader(entry.header, toolName, serverName, filePath, evt);

    const result = evt.result ?? evt.payload ?? null;
    const durationMs = evt.duration_ms ?? (result && result.duration_ms) ?? (result && result.durationMs);
    const isError = evt.is_error || (result && result.isError) || false;
    ensureToolDiffPreview(entry, resolveToolCardDiff(toolName, evt), filePath);
    entry.resultEl = appendToolResult(entry.body, result, isError);
    appendToolFooter(entry.body, durationMs);

    setLastEventType('tool');
    const exitCode = result && (result.exit_code ?? result.exitCode);
    if (!isError && (exitCode === 0 || exitCode === undefined || exitCode === null)) {
      setStatusDot('success');
    } else {
      setStatusDot('error');
    }
  }

  function renderToolInteraction(evt) {
    const entry = getToolRow(evt.id, `tool:${evt.tool || 'tool'}`, getLiveEventParent(evt));
    const payload = evt.payload || {};
    const stdin = payload.stdin ? `stdin: ${payload.stdin}` : '';
    const stdout = payload.stdout ? `stdout: ${payload.stdout}` : '';
    const pid = payload.pid ? `pid=${payload.pid}` : '';
    const parts = [pid, stdin, stdout].filter(Boolean);
    if (parts.length) {
      if (!entry.interactionEl) {
        const interactionPre = document.createElement('pre');
        interactionPre.className = 'mcp-tool-content';
        entry.body.appendChild(interactionPre);
        entry.interactionEl = interactionPre;
      }
      entry.interactionEl.textContent += `[io] ${parts.join(' ')}\n`;
    }
    setLastEventType('tool');
  }

  return {
    buildReplayToolRow,
    renderToolBegin,
    renderToolDelta,
    renderToolEnd,
    renderToolInteraction,
  };
}
