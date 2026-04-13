type ToolPayload = Record<string, any>;

export function bindToolRender(ctx) {
  const {
    toolRows,
    clearPlaceholder,
    insertRow,
    makeCollapsible,
    getLiveEventParent,
    renderEventMarkdownInto,
    formatDiff,
    renderDiffBlock,
    toRelativePath,
    renderShellCmdRibbon,
    maybeAutoScroll,
    setLastEventType,
    setStatusDot,
    getToolRenderPolicy,
    highlightCodeAlways,
  } = ctx;

  function getActiveToolRenderPolicy() {
    if (typeof getToolRenderPolicy === 'function') {
      const policy = getToolRenderPolicy();
      if (policy && typeof policy === 'object') return policy;
    }
    return {
      default: {
        request: { kind: 'plain' },
        response: { kind: 'plain' },
      },
      rules: [],
    };
  }

  function normalizeRenderSpec(spec) {
    if (!spec || typeof spec !== 'object') return { kind: 'plain' };
    const kind = typeof spec.kind === 'string' ? spec.kind.trim().toLowerCase() : 'plain';
    if (kind === 'markdown') return { kind: 'markdown' };
    if (kind === 'hljs') {
      const language = typeof spec.language === 'string' && spec.language.trim() ? spec.language.trim() : '';
      return language ? { kind: 'hljs', language } : { kind: 'hljs' };
    }
    return { kind: 'plain' };
  }

  function ruleMatches(rule, serverName, toolName) {
    if (!rule || typeof rule !== 'object') return false;
    const server = typeof serverName === 'string' ? serverName : '';
    const tool = typeof toolName === 'string' ? toolName : '';
    const exactServer = typeof rule.server === 'string' && rule.server ? rule.server : '';
    const exactTool = typeof rule.tool === 'string' && rule.tool ? rule.tool : '';
    const serverPrefix = typeof rule.serverPrefix === 'string' && rule.serverPrefix ? rule.serverPrefix : '';
    const toolPrefix = typeof rule.toolPrefix === 'string' && rule.toolPrefix ? rule.toolPrefix : '';
    const servers = Array.isArray(rule.servers) ? rule.servers.filter((value) => typeof value === 'string' && value) : [];
    const tools = Array.isArray(rule.tools) ? rule.tools.filter((value) => typeof value === 'string' && value) : [];
    if (exactServer && server !== exactServer) return false;
    if (exactTool && tool !== exactTool) return false;
    if (serverPrefix && !server.startsWith(serverPrefix)) return false;
    if (toolPrefix && !tool.startsWith(toolPrefix)) return false;
    if (servers.length && !servers.includes(server)) return false;
    if (tools.length && !tools.includes(tool)) return false;
    return Boolean(exactServer || exactTool || serverPrefix || toolPrefix || servers.length || tools.length);
  }

  function resolveRenderSpec(serverName, toolName, target, fieldName = '') {
    const policy = getActiveToolRenderPolicy();
    const defaults = policy.default && typeof policy.default === 'object' ? policy.default : {};
    const rules = Array.isArray(policy.rules) ? policy.rules : [];
    const matchedRule = rules.find((rule) => ruleMatches(rule, serverName, toolName)) || null;
    const targetConfig = target === 'request'
      ? {
        primary: 'request',
        legacy: ['args', 'arguments'],
        fieldPrimary: 'requestFields',
        fieldLegacy: ['argsFields', 'argumentsFields'],
      }
      : {
        primary: 'response',
        legacy: ['result'],
        fieldPrimary: 'responseFields',
        fieldLegacy: ['resultFields'],
      };
    if (fieldName) {
      const ruleFieldSources = [targetConfig.fieldPrimary, ...targetConfig.fieldLegacy];
      for (const fieldKey of ruleFieldSources) {
        const matchedField = matchedRule && matchedRule[fieldKey] && typeof matchedRule[fieldKey] === 'object'
          ? matchedRule[fieldKey][fieldName]
          : null;
        if (matchedField) return normalizeRenderSpec(matchedField);
      }
      for (const fieldKey of ruleFieldSources) {
        const defaultField = defaults[fieldKey] && typeof defaults[fieldKey] === 'object'
          ? defaults[fieldKey][fieldName]
          : null;
        if (defaultField) return normalizeRenderSpec(defaultField);
      }
    }
    const sourceKeys = [targetConfig.primary, ...targetConfig.legacy];
    for (const key of sourceKeys) {
      if (matchedRule?.[key]) return normalizeRenderSpec(matchedRule[key]);
    }
    for (const key of sourceKeys) {
      if (defaults?.[key]) return normalizeRenderSpec(defaults[key]);
    }
    return normalizeRenderSpec(null);
  }

  function buildRenderedTextElement(text, spec, className) {
    const normalizedText = text == null ? '' : String(text);
    const normalizedSpec = normalizeRenderSpec(spec);
    if (normalizedSpec.kind === 'markdown' && typeof renderEventMarkdownInto === 'function') {
      const container = document.createElement('div');
      container.className = `markdown-body ${className}`.trim();
      renderEventMarkdownInto(container, normalizedText);
      return container;
    }
    const pre = document.createElement('pre');
    pre.className = className;
    if (normalizedSpec.kind === 'hljs') {
      const code = document.createElement('code');
      const language = normalizedSpec.language || '';
      code.className = language ? `hljs language-${language}` : 'hljs';
      if (typeof highlightCodeAlways === 'function') {
        code.innerHTML = highlightCodeAlways(normalizedText, language);
      } else {
        code.textContent = normalizedText;
      }
      pre.appendChild(code);
      return pre;
    }
    pre.textContent = normalizedText;
    return pre;
  }

  function buildObjectText(result) {
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
    return lines.join('\n');
  }

  function removeNode(node) {
    if (node && node.parentElement) {
      node.parentElement.removeChild(node);
    }
  }

  function removeNodes(nodes) {
    if (!Array.isArray(nodes)) return;
    nodes.forEach((node) => removeNode(node));
  }

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
        header,
        argsEls: [],
        resultHeaderEl: null,
        resultEls: [],
        footerEl: null,
        streamEl: null,
        interactionEl: null,
        diffLabelEl: null,
        diffBlock: null,
      };
      toolRows.set(key, entry);
    } else if (parentEl && entry.row.parentElement !== parentEl) {
      clearPlaceholder();
      parentEl.appendChild(entry.row);
      maybeAutoScroll();
    }
    return entry;
  }

  function resolveToolCardPath(toolName, payload: ToolPayload = {}) {
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

  function resolveToolCardDiff(toolName, payload: ToolPayload = {}) {
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

  function resolveToolCardOutcome(toolName, payload: ToolPayload = {}) {
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

  function isApplyPatchNewFile(payload: ToolPayload = {}) {
    if (!payload || typeof payload !== 'object') return false;
    const args = payload.arguments && typeof payload.arguments === 'object' ? payload.arguments : {};
    const request = payload.request && typeof payload.request === 'object' ? payload.request : {};
    const response = payload.response && typeof payload.response === 'object' ? payload.response : {};
    const result = payload.result && typeof payload.result === 'object' ? payload.result : {};
    return payload.new_file === true
      || payload.newFile === true
      || args.new_file === true
      || args.newFile === true
      || request.new_file === true
      || request.newFile === true
      || response.new_file === true
      || response.newFile === true
      || result.new_file === true
      || result.newFile === true;
  }

  function toolCardLabel(toolName, serverName = '', filePath = '', payload = {}) {
    if (toolName === 'apply_patch') {
      const resolvedPath = typeof filePath === 'string' && filePath.trim() ? filePath.trim() : '';
      const relPath = resolvedPath ? (toRelativePath(resolvedPath) || resolvedPath.split('/').pop() || resolvedPath) : '';
      const baseLabel = isApplyPatchNewFile(payload)
        ? (relPath ? `new file ${relPath}` : 'new file')
        : (relPath ? `apply_patch ${relPath}` : 'apply_patch');
      return serverName ? `${serverName}:${baseLabel}` : baseLabel;
    }
    return serverName ? `${serverName}:${toolName}` : `tool:${toolName}`;
  }

  function renderToolCardHeader(headerEl, toolName, serverName = '', filePath = '', payload = {}) {
    if (!headerEl) return '';
    const label = toolCardLabel(toolName, serverName, filePath, payload);
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
    const block = document.createElement('div');
    block.className = 'diff-block mcp-tool-diff';
    if (typeof renderDiffBlock === 'function') {
      renderDiffBlock(block, diffText, resolvedPath || null);
    } else {
      block.innerHTML = formatDiff(diffText, resolvedPath || null);
    }
    return { label, block };
  }

  function ensureToolDiffPreview(entry, diffText, filePath = '') {
    if (!entry || typeof diffText !== 'string' || !diffText.trim()) return;
    const preview = buildToolDiffPreview(diffText, filePath);
    if (!preview) return;
    if (!entry.diffLabelEl) {
      entry.diffLabelEl = preview.label;
      entry.body.insertBefore(entry.diffLabelEl, entry.body.firstChild.nextSibling || null);
    } else {
      entry.diffLabelEl.textContent = preview.label.textContent;
    }
    if (!entry.diffBlock) {
      entry.diffBlock = preview.block;
      const anchor = entry.diffLabelEl.nextSibling;
      entry.body.insertBefore(entry.diffBlock, anchor || null);
    } else {
      entry.diffBlock.innerHTML = preview.block.innerHTML;
    }
  }

  function appendStructuredToolFields(body, values, serverName, toolName, target, isError = false) {
    if (!values || typeof values !== 'object') return null;
    const entries = Object.entries(values);
    if (!entries.length) return null;
    const hasDeclaredFieldPolicy = entries.some(([key]) => resolveRenderSpec(serverName, toolName, target, key).kind !== 'plain');
    if (!hasDeclaredFieldPolicy) return null;
    const inserted = [];
    const valueClass = target === 'args' ? 'mcp-tool-arg-value-plain' : 'mcp-tool-content';
    entries.forEach(([key, value]) => {
      const label = document.createElement('div');
      label.className = 'mcp-tool-arg-label';
      label.textContent = `${key}:`;
      body.appendChild(label);
      inserted.push(label);
      const renderSpec = resolveRenderSpec(serverName, toolName, target, key);
      const serializedValue = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      let rendered;
      if (typeof value === 'string' || renderSpec.kind === 'hljs') {
        rendered = buildRenderedTextElement(serializedValue, renderSpec, valueClass);
      } else {
        rendered = document.createElement('pre');
        rendered.className = valueClass;
        rendered.textContent = serializedValue;
      }
      if (isError) rendered.classList.add('error-text');
      body.appendChild(rendered);
      inserted.push(rendered);
    });
    return inserted;
  }

  function appendToolArguments(body, args, serverName = '', toolName = '') {
    if (!args || typeof args !== 'object' || Object.keys(args).length === 0) return [];
    const structured = appendStructuredToolFields(body, args, serverName, toolName, 'request');
    if (structured) return structured;
    const argsPre = document.createElement('pre');
    argsPre.className = 'mcp-tool-args';
    const lines = [];
    Object.entries(args).forEach(([key, value]) => {
      const renderedValue = typeof value === 'string' ? value : JSON.stringify(value);
      lines.push(`  ${key}: ${renderedValue}`);
    });
    argsPre.textContent = lines.join('\n');
    body.appendChild(argsPre);
    return [argsPre];
  }

  function appendToolResult(body, result, isError, serverName = '', toolName = '') {
    if (result === undefined || result === null) {
      return { headerEl: null, nodes: [] };
    }
    const resultHeader = document.createElement('div');
    resultHeader.className = 'mcp-tool-result-header';
    resultHeader.textContent = '→';
    body.appendChild(resultHeader);

    if (typeof result === 'object') {
      const structured = appendStructuredToolFields(body, result, serverName, toolName, 'response', isError);
      if (structured) {
        return { headerEl: resultHeader, nodes: structured };
      }
      const resultPre = document.createElement('pre');
      resultPre.className = 'mcp-tool-content';
      resultPre.textContent = buildObjectText(result);
      if (isError) resultPre.classList.add('error-text');
      body.appendChild(resultPre);
      return { headerEl: resultHeader, nodes: [resultPre] };
    }

    const rendered = buildRenderedTextElement(String(result), resolveRenderSpec(serverName, toolName, 'response'), 'mcp-tool-content');
    if (isError) rendered.classList.add('error-text');
    body.appendChild(rendered);
    return { headerEl: resultHeader, nodes: [rendered] };
  }

  function setToolArguments(entry, args, serverName, toolName) {
    removeNodes(entry.argsEls);
    entry.argsEls = appendToolArguments(entry.body, args, serverName, toolName);
  }

  function setToolResult(entry, result, isError, serverName, toolName) {
    removeNode(entry.resultHeaderEl);
    removeNodes(entry.resultEls);
    entry.resultHeaderEl = null;
    entry.resultEls = [];
    const rendered = appendToolResult(entry.body, result, isError, serverName, toolName);
    entry.resultHeaderEl = rendered.headerEl;
    entry.resultEls = rendered.nodes;
  }

  function setToolFooter(entry, durationMs) {
    removeNode(entry.footerEl);
    entry.footerEl = null;
    if (durationMs === undefined || durationMs === null) return;
    const footer = document.createElement('div');
    footer.className = 'command-footer';
    footer.textContent = `${durationMs}ms`;
    entry.body.appendChild(footer);
    entry.footerEl = footer;
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

    appendToolArguments(body, entry.request ?? entry.arguments, serverName, toolName);

    const diffText = resolveToolCardDiff(toolName, entry);
    if (diffText) {
      const preview = buildToolDiffPreview(diffText, filePath);
      if (preview) body.append(preview.label, preview.block);
    }

    if (typeof entry.output === 'string' && entry.output) {
      const outputPre = document.createElement('pre');
      outputPre.className = 'mcp-tool-content';
      outputPre.textContent = entry.output;
      if (entry.is_error && ((entry.response ?? entry.result) === undefined || (entry.response ?? entry.result) === null)) {
        outputPre.classList.add('error-text');
      }
      body.appendChild(outputPre);
    }

    appendToolResult(body, entry.response ?? entry.result, entry.is_error === true, serverName, toolName);
    setToolFooter({ body, footerEl: null }, entry.duration_ms);

    row.appendChild(body);
    makeCollapsible(row, `tool:${entry.id || entry.item_id || `${entry.server || ''}:${entry.tool || ''}`}`, false);
    return row;
  }

  function renderToolBegin(evt) {
    const toolName = evt.tool || 'tool';
    if (toolName === 'command' || toolName === 'shell') return;
    const serverName = evt.server || '';
    const filePath = resolveToolCardPath(toolName, evt);
    const label = toolCardLabel(toolName, serverName, filePath, evt);
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    renderToolCardHeader(entry.header, toolName, serverName, filePath, evt);
    setToolArguments(entry, evt.request ?? evt.arguments ?? evt.payload ?? {}, serverName, toolName);
    ensureToolDiffPreview(entry, resolveToolCardDiff(toolName, evt), filePath);
    setLastEventType('tool');
  }

  function renderToolDelta(evt) {
    const toolName = evt.tool || 'tool';
    if (toolName === 'command' || toolName === 'shell') return;
    const entry = getToolRow(
      evt.id,
      toolCardLabel(toolName, evt.server || '', resolveToolCardPath(toolName, evt), evt),
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
    const label = toolCardLabel(toolName, serverName, filePath, evt);
    const entry = getToolRow(evt.id, label, getLiveEventParent(evt));
    renderToolCardHeader(entry.header, toolName, serverName, filePath, evt);

    const request = evt.request ?? evt.arguments ?? evt.payload ?? {};
    if (!entry.argsEls.length && request && typeof request === 'object') {
      setToolArguments(entry, request, serverName, toolName);
    }
    const result = evt.response ?? evt.result ?? evt.payload ?? null;
    const durationMs = evt.duration_ms ?? (result && result.duration_ms) ?? (result && result.durationMs);
    const isError = evt.is_error || (result && result.isError) || false;
    ensureToolDiffPreview(entry, resolveToolCardDiff(toolName, evt), filePath);
    setToolResult(entry, result, isError, serverName, toolName);
    setToolFooter(entry, durationMs);

    setLastEventType('tool');
    const exitCode = result && (result.exit_code ?? result.exitCode);
    if (!isError && (exitCode === 0 || exitCode === undefined || exitCode === null)) {
      setStatusDot('success');
    } else {
      setStatusDot('error');
    }
  }

  function renderToolInteraction(evt) {
    const toolName = evt.tool || 'tool';
    const serverName = evt.server || '';
    const filePath = resolveToolCardPath(toolName, evt);
    const entry = getToolRow(evt.id, toolCardLabel(toolName, serverName, filePath, evt), getLiveEventParent(evt));
    renderToolCardHeader(entry.header, toolName, serverName, filePath, evt);
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
