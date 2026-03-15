export function bindRpcFlow(ctx) {
  const {
    waitForWs,
    sioCall,
    getPending,
    getConversationId,
    createRow,
    getSubagentContainer,
    escapeHtml,
    formatDiff,
    toRelativePath,
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

  async function respondApproval(requestId, decision) {
    if (requestId === null || requestId === undefined) return;
    try {
      return await sioCall('approval_response', {
        conversation_id: getConversationId() || null,
        request_id: String(requestId),
        decision,
      }, {
        fallbackUrl: '/api/appserver/approval_response',
        fallbackMethod: 'POST',
      });
    } catch (error) {
      return { ok: false, error: error instanceof Error ? error.message : String(error || 'approval failed') };
    }
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
      const response = await respondApproval(requestId, 'accept');
      if (!response || response.ok === false) return;
      await sioCall('approval_record', {
        status: 'accepted',
        diff: diffText,
        path: filePath,
        request_id: String(requestId),
      }, { fallbackUrl: '/api/appserver/approval_record' });
      row.remove();
    });
    decline.addEventListener('click', async () => {
      const response = await respondApproval(requestId, 'decline');
      if (!response || response.ok === false) return;
      await sioCall('approval_record', {
        status: 'declined',
        diff: diffText,
        path: filePath,
        request_id: String(requestId),
      }, { fallbackUrl: '/api/appserver/approval_record' });
      row.remove();
    });
    actions.append(accept, decline);
    body.append(actions);
    return row;
  }

  return {
    renderApproval,
    sendRpc,
    respondApproval,
  };
}
