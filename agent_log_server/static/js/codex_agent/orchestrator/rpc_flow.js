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

export function bindRpcFlow(ctx) {
  const {
    waitForWs,
    sioCall,
    getPending,
    getConversationId,
    getCurrentExtensionId,
    createRow,
    getSubagentContainer,
    escapeHtml,
    formatDiff,
    toRelativePath,
    requestCardRuntime,
  } = ctx;

  let rpcId = 1;

  function nextRpcId() {
    const id = rpcId;
    rpcId += 1;
    return id;
  }

  async function sendRpc(method, params, options = {}) {
    const payload = { method };
    if (params !== undefined) payload.params = params;
    if (options.notify) {
      await sioCall('rpc', payload, { fallbackUrl: '/api/appserver/rpc' });
      return null;
    }
    const id = nextRpcId();
    payload.id = id;
    await waitForWs();
    await sioCall('rpc', payload, { fallbackUrl: '/api/appserver/rpc' });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        getPending().delete(id);
        reject(new Error('rpc timeout'));
      }, 15000);
      getPending().set(id, { resolve, reject, timer });
    });
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
      return await sioCall('approval_response', payload, {
        fallbackUrl: '/api/appserver/approval_response',
        fallbackMethod: 'POST',
      });
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error || 'approval failed') };
    }
  }

  async function recordApproval(requestId, result, meta = {}) {
    return await sioCall('approval_record', {
      status: approvalStatusFromResult(result),
      diff: meta.diff || null,
      path: meta.path || null,
      request_id: String(requestId),
      decision: result?.decision ?? null,
      result,
    }, {
      fallbackUrl: '/api/appserver/approval_record',
      fallbackMethod: 'POST',
    });
  }

  async function submitApproval(requestId, result, meta = {}) {
    const response = await respondApproval(requestId, result);
    if (!response || response.ok === false) return { ok: false, response };
    await recordApproval(requestId, result, meta);
    if (meta.row instanceof HTMLElement) {
      meta.row.remove();
    }
    return { ok: true, response };
  }

  function renderGenericApprovalBody(body, evt, helpers) {
    const payload = evt.payload || {};
    const lines = [];
    let diffText = null;
    let filePath = null;
    if (payload.command) {
      lines.push(`<div><strong>Command:</strong> ${escapeHtml(Array.isArray(payload.command) ? payload.command.join(' ') : String(payload.command))}</div>`);
    }
    if (payload.cwd) {
      lines.push(`<div><strong>CWD:</strong> ${escapeHtml(String(payload.cwd))}</div>`);
    }
    if (payload.reason) {
      lines.push(`<div><strong>Reason:</strong> ${escapeHtml(String(payload.reason))}</div>`);
    }
    if (payload.diff) {
      diffText = payload.diff;
      filePath = payload.path || filePath;
      lines.push(`<pre class="diff-block">${formatDiff(payload.diff, payload.path)}</pre>`);
    }
    if (payload.changes && Array.isArray(payload.changes)) {
      payload.changes.forEach((change) => {
        if (change && change.diff) {
          diffText = diffText || change.diff;
          filePath = filePath || change.path;
          lines.push(`<div><strong>${escapeHtml(toRelativePath(change.path) || 'file')}</strong></div><pre class="diff-block">${formatDiff(change.diff, change.path)}</pre>`);
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
        lines.push(`<div><strong>${escapeHtml(toRelativePath(resolvedPath) || 'file')}</strong></div><pre class="diff-block">${formatDiff(changeDiff, resolvedPath)}</pre>`);
      });
    }
    body.innerHTML = lines.join('') || `<pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;

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

  function renderApproval(evt) {
    const requestId = evt?.request_id || evt?.id;
    if (requestId === null || requestId === undefined || requestId === '') return null;
    const parentEl = evt?.subagent_id
      ? getSubagentContainer(evt.subagent_id, '', '').body
      : null;
    const { row, body } = createRow(
      evt.kind === 'diff' ? 'diff' : 'approval',
      'approval',
      undefined,
      parentEl,
    );
    row.dataset.approvalId = String(requestId);
    if (typeof evt?.request_method === 'string' && evt.request_method.trim()) {
      row.dataset.requestMethod = evt.request_method.trim();
    }

    const helpers = {
      escapeHtml,
      formatDiff,
      toRelativePath,
      normalizeDecisionLabel,
      submitResult: async (result, meta = {}) => submitApproval(requestId, result, { ...meta, row }),
      respondApproval,
      recordApproval: async (result, meta = {}) => recordApproval(requestId, result, meta),
    };

    const extensionId = typeof getCurrentExtensionId === 'function' ? getCurrentExtensionId() : 'codex';
    body.textContent = 'Loading approval…';

    const fallback = () => renderGenericApprovalBody(body, evt, helpers);

    if (!requestCardRuntime) {
      fallback();
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
    })();

    return row;
  }

  return {
    renderApproval,
    sendRpc,
    respondApproval,
  };
}
