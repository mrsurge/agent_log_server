export function bindSessionFlow(ctx) {
  const {
    getState,
    setState,
    sioCall,
    waitForWs,
    sendRpc,
    fetchConversation,
    setActivity,
    updateScrollButton,
    maybeAutoScroll,
    renderShellBatchResult,
    setStatusDot,
    shellRows,
  } = ctx;

  async function ensureInitialized() {
    const state = getState();
    if (state.initialized) return;
    const agentType = state.conversationSettings?.agent || 'codex';
    if (agentType === 'codex') {
      await sioCall('app_start', {}, { fallbackUrl: '/api/appserver/start' });
      await waitForWs();
      try {
        await sendRpc('initialize', {
          clientInfo: {
            name: 'agent_log_server',
            title: 'Agent Log Server',
            version: '0.1.0',
          }
        });
      } catch {
        // ignore already initialized
      }
      await sendRpc('initialized', {}, { notify: true });
    } else {
      await waitForWs();
    }
    setState({ initialized: true });
  }

  async function ensureThread() {
    await fetchConversation();
    let state = getState();
    if (state.currentThreadId) {
      try {
        const savedShellId = state.conversationSettings?.thread_session_shell_id || null;
        const savedThreadId = state.conversationSettings?.thread_session_thread_id || null;
        if (state.currentAppServerShellId && savedShellId === state.currentAppServerShellId && savedThreadId === state.currentThreadId) {
          return state.currentThreadId;
        }
        await sendRpc('thread/resume', { threadId: state.currentThreadId });
        if (state.currentAppServerShellId) {
          await sioCall('conversation_update', {
            conversation_id: state.conversationMeta?.conversation_id,
            settings: { thread_session_shell_id: state.currentAppServerShellId, thread_session_thread_id: state.currentThreadId },
          }, { fallbackUrl: '/api/appserver/conversation' });
        }
        return state.currentThreadId;
      } catch {
        setState({ currentThreadId: null });
      }
    }
    const result = await sendRpc('thread/start', {});
    const threadId = result?.thread?.id;
    state = getState();
    if (threadId) {
      setState({ currentThreadId: threadId });
      if (state.currentAppServerShellId) {
        await sioCall('conversation_update', {
          conversation_id: state.conversationMeta?.conversation_id,
          settings: { thread_session_shell_id: state.currentAppServerShellId, thread_session_thread_id: threadId },
        }, { fallbackUrl: '/api/appserver/conversation' });
      }
      return threadId;
    }
    throw new Error('thread/start failed');
  }

  async function sendUserMessage(text) {
    if (!text) return;
    const state = getState();
    const convoId = state.conversationMeta?.conversation_id;
    if (!convoId) {
      setActivity('save settings first', true);
      return;
    }
    setState({ autoScroll: true });
    updateScrollButton();
    maybeAutoScroll(true);
    setActivity('sending', true);
    await ensureInitialized();
    const result = await sioCall('send_message', {
      conversation_id: convoId,
      text,
    }, { fallbackUrl: '/api/appserver/message' });
    if (!result?.ok) {
      console.error('sendUserMessage failed:', result?.error);
      setActivity(result?.error || 'send failed', true);
    }
  }

  async function sendShellCommand(command) {
    if (!command) return;
    command = String(command)
      .replace(/\u00A0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .trim();
    if (!command) return;
    const state = getState();
    if (!state.conversationMeta?.conversation_id) {
      setActivity('save settings first', true);
      return;
    }
    try {
      const endpoint = state.terminalMode ? '/api/mcp/agent-pty/exec' : '/api/appserver/shell/exec';
      const resp = await sioCall('shell_exec', {
        conversation_id: state.conversationMeta?.conversation_id,
        command,
        terminal_mode: !!state.terminalMode,
      }, { fallbackUrl: endpoint });
      if (resp.error && !shellRows.has(resp.callId)) {
        renderShellBatchResult({
          exitCode: resp.exitCode || 1,
          stdout: resp.stdout || '',
          stderr: resp.stderr || resp.error,
        });
      }
    } catch (err) {
      renderShellBatchResult({
        exitCode: 1,
        stdout: '',
        stderr: String(err),
      });
      setStatusDot('error');
      setActivity('idle', false);
    }
  }

  async function interruptTurn() {
    try {
      setActivity('interrupt', true);
      const state = getState();
      const convoId = state.conversationMeta?.conversation_id || null;
      await sioCall('interrupt', convoId ? { conversation_id: convoId } : {}, {
        fallbackUrl: '/api/appserver/interrupt',
      });
      setActivity('interrupt sent', true);
    } catch (err) {
      console.warn('interrupt failed', err);
      setActivity('interrupt failed', true);
    }
  }

  return {
    ensureInitialized,
    ensureThread,
    sendUserMessage,
    sendShellCommand,
    interruptTurn,
  };
}

