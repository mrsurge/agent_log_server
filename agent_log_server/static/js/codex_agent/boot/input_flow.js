export function bindInputFlow(ctx) {
  const {
    getState,
    setState,
    elements,
    sendShellCommand,
    sendUserMessage,
    getPromptText,
    clearPrompt,
    clearDraft,
    setTerminalMode,
    saveDraftDebounced,
    openPicker,
    sendHostCloseMessage,
    bindSplashTabHandlers,
    initTribute,
    requestContextCompact,
    interruptTurn,
    updateScrollButton,
    maybeAutoScroll,
    isNearBottom,
    loadOlderTranscript,
    fetchConversation,
    restorePendingApprovals,
    refreshPlanSurface,
    postTe2OpenRequest,
    setMarkdownEnabled,
    setTrackEditsEnabled,
    resetTimeline,
    replayTranscript,
    sioCall,
    documentRef,
    windowRef,
  } = ctx;

  const {
    sendBtn,
    footerTerminalToggleEl,
    promptEl,
    mentionPillEl,
    hostCloseTopEl,
    hostCloseDrawerEl,
    contextRemainingEl,
    interruptBtn,
    scrollContainer,
    scrollBtn,
    timelineEl,
    markdownToggleEl,
    trackEditsToggleEl,
    lineNumbersToggleEl,
    settingsCwdEl,
  } = elements;

  async function dispatchInput(text) {
    if (text.startsWith('!')) {
      await sendShellCommand(text.slice(1).trim());
    } else {
      await sendUserMessage(text);
    }
  }

  async function sendPtyStdin(data) {
    const state = getState();
    if (state.ptyWebSocket && state.ptyWebSocket.readyState === WebSocket.OPEN) {
      state.ptyWebSocket.send(data);
      return;
    }
    try {
      const result = await sioCall('pty_stdin', { data });
      if (result?.ok === false) {
        throw new Error(result.error || 'PTY stdin failed');
      }
    } catch (e) {
      console.error('Failed to send PTY stdin:', e);
    }
  }

  async function handlePromptKeydown(evt) {
    const state = getState();
    if (state.terminalMode && state.composerTerm) {
      evt.preventDefault();
      return;
    }
    if (state.commandRunning && state.terminalMode) {
      evt.preventDefault();
      let data = '';
      if (evt.key === 'Enter') {
        data = '\n';
      } else if (evt.key === 'Backspace') {
        data = '\x7f';
      } else if (evt.key === 'Tab') {
        data = '\t';
      } else if (evt.key === 'Escape') {
        data = '\x1b';
      } else if (evt.key === 'ArrowUp') {
        data = '\x1b[A';
      } else if (evt.key === 'ArrowDown') {
        data = '\x1b[B';
      } else if (evt.key === 'ArrowRight') {
        data = '\x1b[C';
      } else if (evt.key === 'ArrowLeft') {
        data = '\x1b[D';
      } else if (evt.ctrlKey && evt.key.length === 1) {
        const code = evt.key.toUpperCase().charCodeAt(0) - 64;
        if (code > 0 && code < 32) data = String.fromCharCode(code);
      } else if (evt.key.length === 1) {
        data = evt.key;
      }
      if (data) {
        await sendPtyStdin(data);
      }
      return;
    }

    if (evt.key === 'Enter' && !evt.shiftKey) {
      if (getState().isMobile && !getState().terminalMode) return;
      evt.preventDefault();
      const text = getPromptText().trim();
      if (!text) return;
      clearPrompt();
      clearDraft();
      await dispatchInput(text);
      return;
    }
    if (evt.key === 'Enter' && evt.shiftKey) {
      evt.preventDefault();
      documentRef.execCommand('insertLineBreak');
    }
  }

  function handlePromptInput() {
    const state = getState();
    if (state.terminalMode && state.composerTerm) {
      return;
    }
    if (!state.applyingDraft) {
      setState({ draftDirty: true });
    }
    if (state.commandRunning && state.terminalMode) {
      const text = getPromptText();
      if (text) {
        clearPrompt();
        sendPtyStdin(text);
      }
    }
    if (!state.commandRunning && !state.terminalMode) {
      saveDraftDebounced();
    }
  }

  function handlePromptClick(evt) {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    const state = getState();
    const removeBtn = target.closest('.mention-token-remove');
    if (removeBtn instanceof HTMLElement) {
      const token = removeBtn.closest('.mention-token');
      if (!(token instanceof HTMLElement)) return;
      evt.preventDefault();
      evt.stopPropagation();
      const next = token.nextSibling;
      const prev = token.previousSibling;
      token.remove();
      if (!state.applyingDraft) {
        setState({ draftDirty: true });
      }
      if (!state.commandRunning && !state.terminalMode) {
        saveDraftDebounced();
      }
      if (promptEl) {
        promptEl.focus();
        const selection = windowRef.getSelection?.();
        if (selection) {
          const range = documentRef.createRange();
          if (next && promptEl.contains(next)) {
            range.setStartBefore(next);
          } else if (prev && promptEl.contains(prev)) {
            range.setStartAfter(prev);
          } else {
            range.selectNodeContents(promptEl);
          }
          range.collapse(true);
          selection.removeAllRanges();
          selection.addRange(range);
        }
      }
      return;
    }
    const mention = target.closest('.mention-token');
    if (mention instanceof HTMLElement) {
      const path = mention.dataset.path || mention.title || mention.textContent || '';
      console.log('Mention path:', path);
    }
  }

  function handleScroll() {
    const state = getState();
    if (!scrollContainer) return;
    if (!state.transcriptLoading && state.transcriptStart > 0) {
      const topSpacerHeight = state.topSpacerEl ? state.topSpacerEl.getBoundingClientRect().height : 0;
      const preloadPx = Math.max(120, (Number(state.estimatedRowHeight) || 0) * 25);
      if (scrollContainer.scrollTop <= topSpacerHeight + preloadPx) {
        loadOlderTranscript();
      }
    }
    if (!state.scrollProgrammatic && state.autoScroll && !isNearBottom()) {
      setState({ autoScroll: false });
      updateScrollButton();
    }
    if (!state.scrollProgrammatic && !state.autoScroll && isNearBottom()) {
      setState({ autoScroll: true });
      updateScrollButton();
    }
  }

  function handleScrollToggle() {
    setState({ autoScroll: !getState().autoScroll });
    updateScrollButton();
    if (getState().autoScroll) maybeAutoScroll(true);
  }

  function handleResize() {
    if (getState().autoScroll) maybeAutoScroll(true);
  }

  function handleDiffClick(evt) {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    const lineEl = target.closest('.diff-line');
    if (!(lineEl instanceof HTMLElement)) return;
    const path = lineEl.getAttribute('data-path') || '';
    const newLine = lineEl.getAttribute('data-new-line') || '';
    const oldLine = lineEl.getAttribute('data-old-line') || '';
    const line = parseInt(newLine || oldLine, 10);
    if (!path || !Number.isFinite(line) || line <= 0) return;
    try {
      lineEl.classList.add('tap-flash');
      setTimeout(() => lineEl.classList.remove('tap-flash'), 180);
    } catch {}
    postTe2OpenRequest({ path, line, column: 1 });
  }

  async function handleMarkdownToggle() {
    const state = getState();
    const enabled = markdownToggleEl.checked;
    setMarkdownEnabled(enabled);
    if (state.conversationSettings && typeof state.conversationSettings === 'object') {
      state.conversationSettings.markdown = enabled;
    }
    if (state.conversationMeta?.conversation_id) {
      await sioCall('conversation_update', {
        conversation_id: state.conversationMeta.conversation_id,
        settings: { ...state.conversationSettings, markdown: enabled },
      });
      await fetchConversation(state.conversationMeta.conversation_id);
    }
    resetTimeline();
    await replayTranscript();
    await refreshPlanSurface?.();
    restorePendingApprovals();
  }

  async function handleTrackEditsToggle() {
    const state = getState();
    const enabled = trackEditsToggleEl.checked;
    setTrackEditsEnabled(enabled);
    if (state.conversationSettings && typeof state.conversationSettings === 'object') {
      state.conversationSettings.trackEdits = enabled;
    }
    if (state.conversationMeta?.conversation_id) {
      await sioCall('conversation_update', {
        conversation_id: state.conversationMeta.conversation_id,
        settings: { ...state.conversationSettings, trackEdits: enabled },
      });
    }
  }

  async function handleLineNumbersToggle() {
    const state = getState();
    const enabled = lineNumbersToggleEl?.checked === true;
    if (state.conversationSettings && typeof state.conversationSettings === 'object') {
      state.conversationSettings.lineNumbers = enabled;
    }
    if (state.conversationMeta?.conversation_id) {
      await sioCall('conversation_update', {
        conversation_id: state.conversationMeta.conversation_id,
        settings: { ...state.conversationSettings, lineNumbers: enabled },
      });
      await fetchConversation(state.conversationMeta.conversation_id);
    }
    resetTimeline();
    await replayTranscript();
    await refreshPlanSurface?.();
    restorePendingApprovals();
  }

  function syncMarkdownFromSettings() {
    const enabled = getState().conversationSettings?.markdown !== false;
    setMarkdownEnabled(enabled);
  }

  function bindInputHandlers() {
    sendBtn?.addEventListener('click', async () => {
      const text = getPromptText().trim();
      if (!text) return;
      clearPrompt();
      clearDraft();
      await dispatchInput(text);
    });

    footerTerminalToggleEl?.addEventListener('click', () => {
      setTerminalMode(!getState().terminalMode);
      promptEl?.focus();
    });

    promptEl?.addEventListener('keydown', handlePromptKeydown);
    promptEl?.addEventListener('input', handlePromptInput);
    promptEl?.addEventListener('click', handlePromptClick);

    mentionPillEl?.addEventListener('click', () => {
      const startPath = getState().conversationSettings?.cwd || settingsCwdEl?.value || '~';
      openPicker(startPath, 'mention');
    });

    hostCloseTopEl?.addEventListener('click', () => {
      sendHostCloseMessage();
    });
    hostCloseDrawerEl?.addEventListener('click', () => {
      sendHostCloseMessage();
    });

    bindSplashTabHandlers();
    initTribute();

    contextRemainingEl?.addEventListener('click', () => {
      if (windowRef.CodexAgent?.helpers?.openWarningModal) {
        windowRef.CodexAgent.helpers.openWarningModal({
          title: 'Compact context?',
          body: 'This will summarize the current conversation history to save context window.',
          confirmText: 'Compact',
          onConfirm: async () => {
            await requestContextCompact();
          },
        });
      }
    });

    interruptBtn?.addEventListener('click', async () => {
      await interruptTurn();
    });

    scrollContainer?.addEventListener('scroll', handleScroll);
    scrollBtn?.addEventListener('click', handleScrollToggle);
    windowRef.addEventListener('resize', handleResize);
    timelineEl?.addEventListener('click', handleDiffClick);
    markdownToggleEl?.addEventListener('change', handleMarkdownToggle);
    trackEditsToggleEl?.addEventListener('change', handleTrackEditsToggle);
    lineNumbersToggleEl?.addEventListener('change', handleLineNumbersToggle);
  }

  return {
    dispatchInput,
    sendPtyStdin,
    bindInputHandlers,
    syncMarkdownFromSettings,
  };
}
