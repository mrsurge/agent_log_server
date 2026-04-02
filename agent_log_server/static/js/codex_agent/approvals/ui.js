function normalizeDecisionLabel(decision) {
  if (typeof decision === 'string') {
    switch (decision) {
      case 'accept':
        return 'Accept';
      case 'acceptForSession':
        return 'Accept for session';
      case 'decline':
        return 'Decline';
      case 'cancel':
        return 'Cancel';
      default:
        return decision;
    }
  }
  if (!decision || typeof decision !== 'object') {
    return 'Submit';
  }
  if (decision.acceptWithExecpolicyAmendment) {
    return 'Accept + exec policy amendment';
  }
  if (decision.applyNetworkPolicyAmendment) {
    return 'Apply network policy amendment';
  }
  return 'Submit';
}

function decisionKey(result) {
  const decision = result?.decision;
  if (typeof decision === 'string') {
    return decision;
  }
  if (!decision || typeof decision !== 'object') {
    return '';
  }
  if (decision.acceptWithExecpolicyAmendment) {
    return 'acceptWithExecpolicyAmendment';
  }
  if (decision.applyNetworkPolicyAmendment) {
    return 'applyNetworkPolicyAmendment';
  }
  return '';
}

function approvalStatusFromResult(result) {
  const key = decisionKey(result);
  if (key === 'decline') return 'declined';
  if (key === 'cancel') return 'cancelled';
  const action = typeof result?.action === 'string' ? result.action.trim().toLowerCase() : '';
  if (action === 'decline') return 'declined';
  if (action === 'cancel') return 'cancelled';
  if (result && typeof result === 'object' && result.success === false) return 'declined';
  return 'accepted';
}

export function bindApprovalUi(ctx) {
  const {
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
  } = ctx;

  function approvalRowSource(options = {}, evt = {}) {
    if (typeof options.source === 'string' && options.source.trim()) return options.source.trim();
    if (options.readOnly) {
      if (evt.type === 'approval_handoff') return 'resolved';
      if (evt.replay === true) return 'replay';
      return 'resolved';
    }
    return 'live';
  }

  function approvalTurnId(evt = {}) {
    return typeof evt?.turn_id === 'string' ? evt.turn_id.trim() : '';
  }

  function approvalRowKey(evt = {}) {
    const requestId = evt?.request_id ?? evt?.id;
    if (requestId === null || requestId === undefined || requestId === '') return '';
    const turnId = approvalTurnId(evt);
    return turnId ? `${turnId}::${String(requestId)}` : String(requestId);
  }

  function findApprovalRow(evt = {}) {
    if (!timelineEl) return null;
    const requestId = evt?.request_id ?? evt?.id;
    if (requestId === null || requestId === undefined || requestId === '') return null;
    const wantedRequestId = String(requestId);
    const wantedTurnId = approvalTurnId(evt);
    const wantedKey = approvalRowKey(evt);
    const rows = Array.from(timelineEl.querySelectorAll('.timeline-row[data-approval-id]'));
    if (wantedKey) {
      const exact = rows.find((row) => row.dataset.approvalKey === wantedKey);
      if (exact) return exact;
    }
    if (wantedTurnId) {
      return rows.find((row) => (
        row.dataset.approvalId === wantedRequestId
        && String(row.dataset.turnId || '').trim() === wantedTurnId
      )) || null;
    }
    return rows.find((row) => (
      row.dataset.approvalId === wantedRequestId
      && !String(row.dataset.turnId || '').trim()
    )) || null;
  }

  function ensureApprovalRow(evt, options = {}) {
    const requestId = evt?.request_id || evt?.id;
    const parentEl = options.parentEl || (evt?.subagent_id
      ? getSubagentContainer(evt.subagent_id, '', '').body
      : null);
    const existingRow = options.row instanceof HTMLElement
      ? options.row
      : (options.useExisting === false ? null : findApprovalRow(evt));
    let row;
    let body;
    if (existingRow) {
      row = existingRow;
      const meta = row.querySelector(':scope > .meta') || document.createElement('div');
      body = row.querySelector(':scope > .body') || document.createElement('div');
      if (!meta.parentElement || !body.parentElement) {
        meta.className = 'meta';
        body.className = 'body';
        row.replaceChildren(meta, body);
      }
      meta.textContent = 'approval';
      body.textContent = '';
      if (parentEl && row.parentElement !== parentEl) {
        parentEl.appendChild(row);
      }
    } else {
      ({ row, body } = createRow(
        evt.kind === 'diff' ? 'diff' : 'approval',
        'approval',
        undefined,
        parentEl,
      ));
    }
    row.classList.add('timeline-row');
    row.classList.remove('diff', 'approval', 'resolved');
    row.classList.add(evt.kind === 'diff' ? 'diff' : 'approval');
    if (options.readOnly) {
      row.classList.add('resolved');
    }
    row.dataset.approvalId = String(requestId);
    const approvalKey = approvalRowKey(evt);
    if (approvalKey) {
      row.dataset.approvalKey = approvalKey;
    } else {
      delete row.dataset.approvalKey;
    }
    row.dataset.approvalSource = approvalRowSource(options, evt);
    if (typeof evt?.request_method === 'string' && evt.request_method.trim()) {
      row.dataset.requestMethod = evt.request_method.trim();
    } else {
      delete row.dataset.requestMethod;
    }
    if (typeof evt?.turn_id === 'string' && evt.turn_id.trim()) {
      row.dataset.turnId = evt.turn_id.trim();
    } else {
      delete row.dataset.turnId;
    }
    if (options.readOnly && evt.replay === true) {
      row.dataset.replay = 'true';
    } else {
      delete row.dataset.replay;
    }
    return { row, body };
  }

  function prunePendingApproval(requestId) {
    if (requestId === null || requestId === undefined || requestId === '') return;
    const currentMeta = getConversationMeta?.();
    if (!currentMeta || typeof currentMeta !== 'object') return;
    const pending = currentMeta.pending_approvals;
    const key = String(requestId);
    if (!pending || typeof pending !== 'object' || !Object.prototype.hasOwnProperty.call(pending, key)) return;
    const nextPending = { ...pending };
    delete nextPending[key];
    setConversationMeta?.({
      ...currentMeta,
      pending_approvals: nextPending,
    });
  }

  function renderApprovalMarkdown(container, text, extraClass = '') {
    if (!(container instanceof HTMLElement)) return;
    if (typeof extraClass === 'string' && extraClass.trim()) {
      extraClass.trim().split(/\s+/).forEach((cls) => {
        if (cls) container.classList.add(cls);
      });
    }
    if (typeof renderEventMarkdownInto === 'function') {
      container.classList.add('markdown-body', 'approval-markdown');
      renderEventMarkdownInto(container, text);
      return;
    }
    container.textContent = String(text || '');
  }

  function appendMarkdownValue(container, label, value) {
    if (!(container instanceof HTMLElement)) return;
    if (value === null || value === undefined || value === '') return;
    const row = document.createElement('div');
    const title = document.createElement('div');
    title.innerHTML = `<strong>${escapeHtml(label)}:</strong>`;
    const content = document.createElement('div');
    renderApprovalMarkdown(content, String(value));
    row.append(title, content);
    container.append(row);
  }

  async function respondApproval(requestId, result) {
    if (requestId === null || requestId === undefined) return;
    const resultPayload = typeof result === 'string'
      ? { decision: result }
      : (result && typeof result === 'object' ? result : {});
    const payload = {
      conversation_id: getConversationId() || null,
      request_id: String(requestId),
    };
    if (Object.keys(resultPayload).length) {
      payload.result = resultPayload;
    }
    if (typeof resultPayload.decision === 'string') {
      payload.decision = resultPayload.decision;
    }
    try {
      return await sioCall('approval_response', payload);
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error || 'approval failed') };
    }
  }

  async function submitApproval(requestId, result, meta = {}) {
    const response = await respondApproval(requestId, result);
    if (!response || response.ok === false) return { ok: false, response };
    if (response?.handoff_event && typeof response.handoff_event === 'object') {
      handoffApproval(response.handoff_event, {
        row: meta.row,
        extensionId: meta.extensionId,
      });
    }
    return { ok: true, response };
  }

  function renderGenericApprovalBody(body, evt, helpers) {
    const payload = evt.payload || {};
    let diffText = null;
    let filePath = null;
    let renderedAny = false;
    body.textContent = '';
    const appendPlainValue = (label, value) => {
      if (value === null || value === undefined || value === '') return;
      const row = document.createElement('div');
      row.innerHTML = `<strong>${escapeHtml(label)}:</strong> ${escapeHtml(String(value))}`;
      body.append(row);
      renderedAny = true;
    };
    const appendNarrativeValue = (label, value) => {
      if (value === null || value === undefined || value === '') return;
      appendMarkdownValue(body, label, value);
      renderedAny = true;
    };
    if (payload.command) {
      appendPlainValue('Command', Array.isArray(payload.command) ? payload.command.join(' ') : String(payload.command));
    }
    if (payload.cwd) {
      appendPlainValue('CWD', String(payload.cwd));
    }
    if (payload.reason) {
      appendNarrativeValue('Reason', String(payload.reason));
    }
    if (payload.question) {
      appendNarrativeValue('Question', String(payload.question));
    }
    if (payload.message) {
      appendNarrativeValue('Message', String(payload.message));
    }
    if (payload.warning) {
      const warningNode = document.createElement('div');
      renderApprovalMarkdown(warningNode, String(payload.warning), 'approval-feedback');
      body.append(warningNode);
      renderedAny = true;
    }
    if (payload.diff) {
      diffText = payload.diff;
      filePath = payload.path || filePath;
      const diffBlock = document.createElement('div');
      diffBlock.className = 'diff-block';
      if (typeof renderDiffBlock === 'function') {
        renderDiffBlock(diffBlock, payload.diff, payload.path);
      } else {
        diffBlock.innerHTML = formatDiff(payload.diff, payload.path);
      }
      body.append(diffBlock);
      renderedAny = true;
    }
    if (payload.changes && Array.isArray(payload.changes)) {
      payload.changes.forEach((change) => {
        if (change && change.diff) {
          diffText = diffText || change.diff;
          filePath = filePath || change.path;
          const label = document.createElement('div');
          label.innerHTML = `<strong>${escapeHtml(toRelativePath(change.path) || 'file')}</strong>`;
          const diffBlock = document.createElement('div');
          diffBlock.className = 'diff-block';
          if (typeof renderDiffBlock === 'function') {
            renderDiffBlock(diffBlock, change.diff, change.path);
          } else {
            diffBlock.innerHTML = formatDiff(change.diff, change.path);
          }
          body.append(label, diffBlock);
          renderedAny = true;
        }
      });
    }
    if (payload.changes && payload.changes.constructor === Object) {
      Object.entries(payload.changes).forEach(([changePath, change]) => {
        if (!change || typeof change !== 'object') return;
        const changeDiff = change.diff || change.unified_diff || change.patch || '';
        if (!changeDiff) return;
        const resolvedPath = change.path || change.file_path || changePath;
        diffText = diffText || changeDiff;
        filePath = filePath || resolvedPath;
        const label = document.createElement('div');
        label.innerHTML = `<strong>${escapeHtml(toRelativePath(resolvedPath) || 'file')}</strong>`;
        const diffBlock = document.createElement('div');
        diffBlock.className = 'diff-block';
        if (typeof renderDiffBlock === 'function') {
          renderDiffBlock(diffBlock, changeDiff, resolvedPath);
        } else {
          diffBlock.innerHTML = formatDiff(changeDiff, resolvedPath);
        }
        body.append(label, diffBlock);
        renderedAny = true;
      });
    }
    if (!renderedAny) {
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(payload, null, 2);
      body.append(pre);
    }

    if (helpers.readOnly) {
      const feedback = document.createElement('div');
      feedback.className = 'approval-feedback approval-feedback-static';
      const parts = [];
      if (typeof evt?.status === 'string' && evt.status.trim()) parts.push(evt.status.trim());
      if (typeof evt?.decision === 'string' && evt.decision.trim()) parts.push(evt.decision.trim());
      feedback.textContent = parts.length ? `Recorded response: ${parts.join(' / ')}` : 'Recorded response';
      body.append(feedback);
      if (evt?.result && typeof evt.result === 'object') {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'Recorded result';
        const pre = document.createElement('pre');
        pre.className = 'approval-extra';
        pre.textContent = JSON.stringify(evt.result, null, 2);
        details.append(summary, pre);
        body.append(details);
      }
      return;
    }

    const actions = document.createElement('div');
    actions.className = 'actions';
    const accept = document.createElement('button');
    accept.className = 'btn tiny approve';
    accept.textContent = 'Accept';
    const decline = document.createElement('button');
    decline.className = 'btn tiny decline';
    decline.textContent = 'Decline';
    accept.addEventListener('click', async () => {
      await helpers.submitResult({ decision: 'accept' }, { diff: diffText, path: filePath });
    });
    decline.addEventListener('click', async () => {
      await helpers.submitResult({ decision: 'decline' }, { diff: diffText, path: filePath });
    });
    actions.append(accept, decline);
    body.append(actions);
  }

  function renderApproval(evt, options = {}) {
    const requestId = evt?.request_id || evt?.id;
    if (requestId === null || requestId === undefined || requestId === '') return null;
    const { row, body } = ensureApprovalRow(evt, options);

    const helpers = {
      escapeHtml,
      formatDiff,
      toRelativePath,
      normalizeDecisionLabel,
      renderMarkdown: (container, text, extraClass = '') => renderApprovalMarkdown(container, text, extraClass),
      readOnly: options.readOnly === true,
      submitResult: async (result, meta = {}) => submitApproval(requestId, result, {
        requestMethod: evt?.request_method || evt?.requestMethod || null,
        payload: evt?.payload || null,
        extensionId: options.extensionId,
        ...meta,
        row,
      }),
      respondApproval,
      recordApproval: async () => ({ ok: false, error: 'approval_record is deprecated in the UI flow' }),
    };
    if (options.readOnly) {
      helpers.submitResult = async () => ({ ok: false, response: { error: 'Replayed approval is read-only' } });
      helpers.respondApproval = async () => ({ ok: false, error: 'Replayed approval is read-only' });
    }

    const extensionId = options.extensionId
      || (typeof getCurrentExtensionId === 'function' ? getCurrentExtensionId() : 'codex');
    body.textContent = 'Loading approval…';

    const fallback = () => renderGenericApprovalBody(body, evt, helpers);

    if (!requestCardRuntime) {
      fallback();
      onAfterRender?.();
      return row;
    }

    void (async () => {
      const handled = await requestCardRuntime.render(evt, {
        extensionId,
        row,
        body,
        helpers,
      }).catch(() => false);
      if (!handled) {
        fallback();
      }
      onAfterRender?.();
    })();

    return row;
  }

  function buildApprovalEventFromPending(entry) {
    const conversationId = getConversationId() || null;
    const requestId = entry?.request_id || entry?.id;
    if (!requestId) return null;
    const liveEvent = entry.render_event && typeof entry.render_event === 'object'
      ? { ...entry.render_event }
      : {
          type: 'approval',
          id: requestId,
          request_id: requestId,
          kind: entry.kind || entry.payload?.kind || 'unknown',
          payload: entry.payload || {},
          turn_id: entry.turn_id || '',
          conversation_id: conversationId,
        };
    liveEvent.type = 'approval';
    liveEvent.id = liveEvent.id ?? requestId;
    liveEvent.request_id = liveEvent.request_id ?? requestId;
    liveEvent.kind = liveEvent.kind || entry.kind || liveEvent.payload?.kind || 'unknown';
    liveEvent.request_method = liveEvent.request_method || entry.request_method || null;
    liveEvent.request_params = (liveEvent.request_params && typeof liveEvent.request_params === 'object')
      ? liveEvent.request_params
      : (entry.request_params || {});
    liveEvent.payload = (liveEvent.payload && typeof liveEvent.payload === 'object') ? liveEvent.payload : (entry.payload || {});
    liveEvent.turn_id = liveEvent.turn_id || entry.turn_id || '';
    liveEvent.conversation_id = liveEvent.conversation_id || conversationId;
    return liveEvent;
  }

  function restorePendingApprovals() {
    if (!timelineEl) return;
    timelineEl.querySelectorAll('.timeline-row[data-approval-source="pending"]').forEach((row) => row.remove());
    const conversationMeta = getConversationMeta?.();
    const pending = conversationMeta?.pending_approvals;
    if (!pending || typeof pending !== 'object') {
      onAfterRender?.();
      return;
    }
    const items = Object.values(pending)
      .filter((entry) => entry && typeof entry === 'object' && (entry.request_id || entry.id))
      .sort((a, b) => String(a?.created_at || a?.render_event?.created_at || '').localeCompare(String(b?.created_at || b?.render_event?.created_at || '')));
    items.forEach((entry) => {
      const liveEvent = buildApprovalEventFromPending(entry);
      if (!liveEvent) return;
      renderApproval(liveEvent, { source: 'pending', useExisting: true });
    });
    onAfterRender?.();
  }

  function handoffApproval(evt, options = {}) {
    const requestId = evt?.request_id || evt?.id;
    if (requestId === null || requestId === undefined || requestId === '') return null;
    prunePendingApproval(requestId);
    const handoffEvent = {
      ...evt,
      type: 'approval',
      request_id: evt?.request_id || evt?.id,
      id: evt?.id || evt?.request_id,
      replay: false,
    };
    return renderApproval(handoffEvent, {
      ...options,
      readOnly: true,
      useExisting: true,
      source: 'resolved',
    });
  }

  return {
    approvalStatusFromResult,
    renderApproval,
    handoffApproval,
    restorePendingApprovals,
    respondApproval,
  };
}
