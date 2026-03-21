export function bindSessionFlow(ctx) {
  const {
    getState,
    setState,
    sioCall,
    waitForWs,
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
    await waitForWs();
    setState({ initialized: true });
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
    });
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
      const resp = await sioCall('shell_exec', {
        conversation_id: state.conversationMeta?.conversation_id,
        command,
        terminal_mode: !!state.terminalMode,
      });
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
      await sioCall('interrupt', convoId ? { conversation_id: convoId } : {});
      setActivity('interrupt sent', true);
    } catch (err) {
      console.warn('interrupt failed', err);
      setActivity('interrupt failed', true);
    }
  }

  return {
    ensureInitialized,
    sendUserMessage,
    sendShellCommand,
    interruptTurn,
  };
}
