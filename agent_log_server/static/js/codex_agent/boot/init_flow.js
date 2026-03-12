export function bindBootInitFlow(ctx) {
  const {
    getState,
    setState,
    elements,
    setPill,
    setCounter,
    updateScrollButton,
    resetWsReady,
    connectWS,
    fetchHostUi,
    fetchAppConfig,
    bindPickerFilter,
    setDrawerOpen,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript,
    restorePendingApprovals,
    maybeAutoScroll,
    ensureActivityRow,
    fetchStatus,
    setupDropdown,
    loadAgentOptions,
    loadModelOptions,
    updateEffortOptionsForModel,
    helperFns,
    closeDropdownMenu,
    sioCall,
    documentRef,
    windowRef,
  } = ctx;

  const {
    statusEl,
    counterMessagesEl,
    counterTokensEl,
    settingsApprovalEl,
    settingsApprovalToggle,
    settingsApprovalOptions,
    settingsSandboxEl,
    settingsSandboxToggle,
    settingsSandboxOptions,
    settingsModelEl,
    settingsModelToggle,
    settingsModelOptions,
    settingsEffortEl,
    settingsEffortToggle,
    settingsEffortOptions,
    settingsSummaryEl,
    settingsSummaryToggle,
    settingsSummaryOptions,
    settingsAgentEl,
    settingsAgentToggle,
    settingsAgentOptions,
    startBtn,
    stopBtn,
  } = elements;

  function initializeBoot(handleEvent) {
    const state = getState();
    setPill(statusEl, 'idle', 'warn');
    setCounter(counterMessagesEl, state.messageCount);
    setCounter(counterTokensEl, state.tokenCount);
    updateScrollButton();
    resetWsReady();
    connectWS(handleEvent);
    fetchHostUi();
    fetchAppConfig();
    bindPickerFilter();
    setDrawerOpen(false);
    fetchConversation().then(async () => {
      await fetchConversations();
      if (getState().activeView === 'conversation') {
        resetTimeline();
        await replayTranscript();
        restorePendingApprovals();
        setTimeout(() => {
          setDrawerOpen(true);
          maybeAutoScroll(true);
        }, 50);
      } else {
        ensureActivityRow();
      }
    });
    fetchStatus();
  }

  function setupSettingsBoot() {
    setupDropdown(settingsApprovalEl, settingsApprovalToggle, settingsApprovalOptions, [
      'never',
      'on-failure',
      'untrusted',
    ]);
    setupDropdown(settingsSandboxEl, settingsSandboxToggle, settingsSandboxOptions, [
      'workspaceWrite',
      'readOnly',
      'dangerFullAccess',
      'externalSandbox',
    ]);
    setupDropdown(settingsModelEl, settingsModelToggle, settingsModelOptions, [
      'gpt-5.1-codex',
      'gpt-5-codex',
      'gpt-4.1-codex',
    ]);
    setupDropdown(settingsEffortEl, settingsEffortToggle, settingsEffortOptions, [
      'low',
      'medium',
      'high',
    ]);
    setupDropdown(settingsSummaryEl, settingsSummaryToggle, settingsSummaryOptions, [
      'concise',
      'detailed',
      'auto',
    ]);
    setupDropdown(settingsAgentEl, settingsAgentToggle, settingsAgentOptions, ['codex']);
    loadAgentOptions();
    loadModelOptions();
    if (settingsModelEl) {
      settingsModelEl.addEventListener('input', () => {
        updateEffortOptionsForModel(settingsModelEl.value);
      });
      settingsModelEl.addEventListener('change', () => {
        updateEffortOptionsForModel(settingsModelEl.value);
      });
    }
  }

  function installCodexAgentGlobal() {
    const api = {
      helpers: {
        ...helperFns,
        setPendingNewConversation: (val) => { setState({ pendingNewConversation: Boolean(val) }); },
        setPendingRollout: (val) => { setState({ pendingRollout: val }); },
        getPickerPath: () => getState().pickerPath,
        setPickerPath: (val) => { setState({ pickerPath: val }); },
        getPickerMode: () => getState().pickerMode,
        setPickerMode: (val) => { setState({ pickerMode: val || 'cwd' }); },
      },
      state: {
        get pendingNewConversation() { return getState().pendingNewConversation; },
        set pendingNewConversation(val) { setState({ pendingNewConversation: Boolean(val) }); },
        get pendingRollout() { return getState().pendingRollout; },
        get conversationMeta() { return getState().conversationMeta; },
        get conversationSettings() { return getState().conversationSettings; },
        get splashTab() { return getState().splashTab; },
        get hostUi() { return getState().hostUi; },
        get appConfig() { return getState().appConfig; },
      },
    };
    windowRef.CodexAgent = api;
    return api;
  }

  function bindStartStopButtons() {
    startBtn?.addEventListener('click', async () => {
      await sioCall('app_start', {}, { fallbackUrl: '/api/appserver/start' });
      fetchStatus();
    });

    stopBtn?.addEventListener('click', async () => {
      await sioCall('app_stop', {}, { fallbackUrl: '/api/appserver/stop' });
      fetchStatus();
    });
  }

  function initExternalModules() {
    (windowRef.CodexAgentModules || []).forEach((fn) => {
      try {
        fn(windowRef.CodexAgent);
      } catch (err) {
        console.warn('module init failed', err);
      }
    });
  }

  function bindDropdownClose() {
    documentRef.addEventListener('click', (evt) => {
      const openDropdownEl = getState().openDropdownEl;
      if (!openDropdownEl) return;
      const target = evt.target;
      if (!(target instanceof HTMLElement)) return;
      if (openDropdownEl.contains(target)) return;
      if (target.classList.contains('dropdown-toggle')) return;
      closeDropdownMenu(openDropdownEl);
    });
  }

  return {
    initializeBoot,
    setupSettingsBoot,
    installCodexAgentGlobal,
    bindStartStopButtons,
    initExternalModules,
    bindDropdownClose,
  };
}
