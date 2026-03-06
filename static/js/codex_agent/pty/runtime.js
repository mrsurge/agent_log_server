export function bindPtyRuntime(ctx) {
  const {
    getState,
    setState,
    getWindow,
    createXterm,
    maybeAutoScroll,
    getAgentBlockRows,
  } = ctx;

  function handleUserPtyOutput(chunk) {
    const state = getState();
    // Always keep the composer terminal in sync once created.
    // When not in terminal mode it's hidden, but continuing to stream output
    // avoids drift and removes the need for lossy rehydration on reopen.
    if (state.composerTerm) {
      if (state.composerPriming) {
        // Buffer live output until priming completes to avoid race with reset/rehydrate.
        if (state.composerPendingBytes < 256 * 1024) {
          state.composerPendingChunks.push(chunk);
          state.composerPendingBytes += chunk.length || 0;
          setState({ composerPendingBytes: state.composerPendingBytes });
        }
        return;
      }
      state.composerTerm.write(chunk);
      return; // Composer xterm is exclusive display for live PTY in terminal mode
    }

    const agentBlockRows = getAgentBlockRows();

    // Route raw PTY output to the active agent block (raw mode) - for agent PTY, not user terminal
    if (state.activeAgentPtyBlockId) {
      const entry = agentBlockRows.get(state.activeAgentPtyBlockId);
      if (entry) {
        entry.hasRawStream = true;
        if (!entry.term && state.useXterm) {
          entry.term = createXterm(entry.termEl, 12);
        }
        if (state.useXterm && entry.term) {
          entry.term.write(chunk);
        } else if (!state.useXterm && entry.termEl) {
          entry.text += chunk;
          entry.termEl.textContent = entry.text;
        }
        maybeAutoScroll();
        return;
      }
    }
    // Fallback: find any raw-mode block
    for (const [_, entry] of agentBlockRows) {
      if (entry.renderMode === 'raw' && entry.term && state.useXterm) {
        entry.hasRawStream = true;
        entry.term.write(chunk);
        maybeAutoScroll();
        return;
      }
    }
  }

  function connectPtyWebSocket() {
    const state = getState();
    const convoId = state.conversationMeta?.conversation_id;
    if (!convoId) return;

    // If we're already connected/connecting for this conversation, don't reconnect.
    // Reconnecting aggressively can create race conditions where an old socket's
    // onclose fires after a new one is created, nulling out the new connection
    // and/or duplicating streams.
    if (
      state.ptyWebSocket &&
      state.ptyWebSocketConvoId === convoId &&
      (state.ptyWebSocket.readyState === WebSocket.OPEN || state.ptyWebSocket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    // Close existing connection if any (conversation changed or prior socket dead)
    if (state.ptyWebSocket) {
      try {
        // Detach handlers to avoid late events mutating global state
        state.ptyWebSocket.onopen = null;
        state.ptyWebSocket.onmessage = null;
        state.ptyWebSocket.onerror = null;
        state.ptyWebSocket.onclose = null;
      } catch (_) {}
      try { state.ptyWebSocket.close(); } catch (_) {}
      setState({ ptyWebSocket: null, ptyWebSocketConvoId: null });
    }

    const win = getWindow();
    const protocol = win.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${win.location.host}/ws/pty/${encodeURIComponent(convoId)}`;

    try {
      const ws = new WebSocket(wsUrl);
      setState({ ptyWebSocket: ws, ptyWebSocketConvoId: convoId });

      ws.onopen = () => {
        const s = getState();
        if (s.ptyWebSocket !== ws) return;
        console.log('PTY WebSocket connected');
        if (s.activeAgentPtyBlockId) {
          const entry = getAgentBlockRows().get(s.activeAgentPtyBlockId);
          if (entry) entry.hasRawStream = true;
        }
      };

      ws.onmessage = (event) => {
        const s = getState();
        if (s.ptyWebSocket !== ws) return;
        // Raw PTY output - for user terminal xterm rendering
        // This is separate from agent transcript which uses screen_delta events
        const data = event.data;
        if (typeof data === 'string' && data) {
          handleUserPtyOutput(data);
        }
      };

      ws.onerror = (err) => {
        const s = getState();
        if (s.ptyWebSocket !== ws) return;
        console.error('PTY WebSocket error:', err);
      };

      ws.onclose = () => {
        const s = getState();
        if (s.ptyWebSocket !== ws) return;
        console.log('PTY WebSocket closed');
        setState({ ptyWebSocket: null, ptyWebSocketConvoId: null });
      };
    } catch (e) {
      console.error('Failed to connect PTY WebSocket:', e);
    }
  }

  return {
    connectPtyWebSocket,
    handleUserPtyOutput,
  };
}

