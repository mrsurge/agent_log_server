import { TRANSCRIPT_PRELOAD_ROWS } from '../transcript_config.ts';

type TextValueElement = HTMLElement & { value: string };

type ConversationSettingsState = {
  cwd?: string | null;
  markdown?: boolean;
  trackEdits?: boolean;
  lineNumbers?: boolean;
  [key: string]: unknown;
};

type ConversationMetaState = {
  conversation_id?: string | null;
};

type InputFlowState = {
  isMobile?: boolean;
  applyingDraft?: boolean;
  draftDirty?: boolean;
  transcriptLoading?: boolean;
  transcriptStart?: number;
  transcriptEnd?: number;
  transcriptTotal?: number;
  transcriptHistoryMode?: boolean;
  transcriptLiveDirty?: boolean;
  topSpacerEl?: HTMLElement | null;
  bottomSpacerEl?: HTMLElement | null;
  estimatedRowHeight?: number;
  scrollProgrammatic?: boolean;
  autoScroll?: boolean;
  conversationSettings?: ConversationSettingsState | null;
  conversationMeta?: ConversationMetaState | null;
};

type InputFlowElements = {
  sendBtn?: HTMLElement | null;
  promptEl?: HTMLElement | null;
  mentionPillEl?: HTMLElement | null;
  hostCloseTopEl?: HTMLElement | null;
  hostCloseDrawerEl?: HTMLElement | null;
  contextRemainingEl?: HTMLElement | null;
  interruptBtn?: HTMLElement | null;
  scrollContainer?: HTMLElement | null;
  scrollBtn?: HTMLElement | null;
  timelineEl?: HTMLElement | null;
  markdownToggleEl?: HTMLInputElement | null;
  trackEditsToggleEl?: HTMLInputElement | null;
  lineNumbersToggleEl?: HTMLInputElement | null;
  settingsCwdEl?: TextValueElement | null;
};

type WarningModalConfig = {
  title: string;
  body: string;
  confirmText: string;
  onConfirm: () => Promise<void>;
};

type CodexAgentWindow = Window & {
  CodexAgent?: {
    helpers?: {
      openWarningModal?: (config: WarningModalConfig) => void;
    };
  };
};

interface InputFlowContext {
  getState: () => InputFlowState;
  setState: (patch: Partial<InputFlowState>) => void;
  elements: InputFlowElements;
  sendShellCommand: (command: string) => Promise<unknown>;
  sendUserMessage: (text: string) => Promise<unknown>;
  getPromptText: () => string;
  clearPrompt: () => void;
  clearDraft: () => void;
  saveDraftDebounced: () => void;
  openPicker: (startPath: string, mode: string) => void;
  sendHostCloseMessage: () => void;
  bindSplashTabHandlers: () => void;
  initTribute: () => void;
  requestContextCompact: () => Promise<unknown>;
  interruptTurn: () => Promise<unknown>;
  updateScrollButton: () => void;
  maybeAutoScroll: (force?: boolean) => void;
  isNearBottom: () => boolean;
  loadOlderTranscript: () => void;
  loadNewerTranscript: () => void;
  fetchConversation: (conversationId: string) => Promise<unknown>;
  restorePendingApprovals: () => void;
  refreshPlanSurface?: () => Promise<unknown> | unknown;
  postTe2OpenRequest: (target: { path: string; line: number; column: number }) => void;
  setMarkdownEnabled: (enabled: boolean) => void;
  setTrackEditsEnabled: (enabled: boolean) => void;
  resetTimeline: () => void;
  replayTranscript: () => Promise<unknown>;
  sioCall: (event: string, payload?: Record<string, unknown>) => Promise<unknown>;
  documentRef: Document;
  windowRef: CodexAgentWindow;
}

export function bindInputFlow(ctx: InputFlowContext) {
  const {
    getState,
    setState,
    elements,
    sendShellCommand,
    sendUserMessage,
    getPromptText,
    clearPrompt,
    clearDraft,
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
    loadNewerTranscript,
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

  async function dispatchInput(text: string) {
    if (text.startsWith('!')) {
      await sendShellCommand(text.slice(1).trim());
    } else {
      await sendUserMessage(text);
    }
  }

  async function handlePromptKeydown(evt: KeyboardEvent) {
    if (evt.key === 'Enter' && !evt.shiftKey) {
      if (getState().isMobile) return;
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
    if (!state.applyingDraft) {
      setState({ draftDirty: true });
      saveDraftDebounced();
    }
  }

  function handlePromptClick(evt: MouseEvent) {
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
    if (!state.transcriptLoading && (state.transcriptStart ?? 0) > 0) {
      const topSpacerHeight = state.topSpacerEl ? state.topSpacerEl.getBoundingClientRect().height : 0;
      const preloadPx = Math.max(120, (Number(state.estimatedRowHeight) || 0) * TRANSCRIPT_PRELOAD_ROWS);
      if (scrollContainer.scrollTop <= topSpacerHeight + preloadPx) {
        loadOlderTranscript();
      }
    }
    if (
      !state.transcriptLoading
      && (
        (state.transcriptEnd ?? 0) < (state.transcriptTotal ?? 0)
        || (state.transcriptHistoryMode === true && state.transcriptLiveDirty === true)
      )
    ) {
      const bottomSpacerHeight = state.bottomSpacerEl ? state.bottomSpacerEl.getBoundingClientRect().height : 0;
      const preloadPx = Math.max(120, (Number(state.estimatedRowHeight) || 0) * TRANSCRIPT_PRELOAD_ROWS);
      const distanceFromBottom = scrollContainer.scrollHeight - (scrollContainer.scrollTop + scrollContainer.clientHeight);
      if (distanceFromBottom <= bottomSpacerHeight + preloadPx) {
        loadNewerTranscript();
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

  function handleDiffClick(evt: MouseEvent) {
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
    const enabled = markdownToggleEl?.checked === true;
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
    const enabled = trackEditsToggleEl?.checked === true;
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
    bindInputHandlers,
    syncMarkdownFromSettings,
  };
}
