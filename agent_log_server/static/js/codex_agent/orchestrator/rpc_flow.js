export function bindRpcFlow(ctx) {
  const {
    waitForWs,
    sioCall,
    getPending,
    getConversationId,
    createRow,
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
    let id = requestId;
    if (typeof id === 'string' && /^\d+$/.test(id)) {
      id = parseInt(id, 10);
    }
    await sioCall('approval_response', {
      conversation_id: getConversationId() || null,
      id,
      decision,
    }, {
      fallbackUrl: '/api/appserver/rpc',
      fallbackMethod: 'POST',
    });
  }

  function renderApproval(evt) {
    const { row, body } = createRow(evt.kind === 'diff' ? 'diff' : 'approval', 'approval');
    row.dataset.approvalId = evt.id;
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
      await respondApproval(evt.id, 'accept');
      await sioCall('approval_record', {
        status: 'accepted',
        diff: diffText,
        path: filePath,
        item_id: evt.id,
      }, { fallbackUrl: '/api/appserver/approval_record' });
      row.remove();
    });
    decline.addEventListener('click', async () => {
      await respondApproval(evt.id, 'decline');
      await sioCall('approval_record', {
        status: 'declined',
        diff: diffText,
        path: filePath,
        item_id: evt.id,
      }, { fallbackUrl: '/api/appserver/approval_record' });
      row.remove();
    });
    actions.append(accept, decline);
    body.append(actions);
  }

  return {
    renderApproval,
    sendRpc,
    respondApproval,
  };
}
