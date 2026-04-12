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
    waitForWs,
    recheckSidebarConnection,
    fetchHostUi,
    fetchAppConfig,
    bindPickerFilter,
    setDrawerOpen,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript,
    refreshPlanSurface,
    restorePendingApprovals,
    maybeAutoScroll,
    ensureActivityRow,
    fetchStatus,
    setupDropdown,
    loadAgentOptions,
    loadModelOptions,
    loadRuntimeOptions,
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
    bindPickerFilter();
    setDrawerOpen(false);
    void (async () => {
      const ready = await waitForWs(10000);
      if (!ready) {
        console.warn('Socket.IO not ready during initial boot hydration');
        ensureActivityRow();
        return;
      }
      await recheckSidebarConnection?.();
      await fetchHostUi();
      await fetchAppConfig();
      await fetchConversation();
      await fetchConversations();
      if (getState().activeView === 'conversation') {
        resetTimeline();
        await replayTranscript();
        await refreshPlanSurface?.();
        restorePendingApprovals();
        setTimeout(() => {
          setDrawerOpen(true);
          maybeAutoScroll(true);
        }, 50);
      } else {
        ensureActivityRow();
      }
    })();
  }

  function setupSettingsBoot() {
    setupDropdown(settingsApprovalEl, settingsApprovalToggle, settingsApprovalOptions, []);
    setupDropdown(settingsSandboxEl, settingsSandboxToggle, settingsSandboxOptions, []);
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
    setupDropdown(settingsModelEl, settingsModelToggle, settingsModelOptions, []);
    setupDropdown(settingsAgentEl, settingsAgentToggle, settingsAgentOptions, []);
    loadAgentOptions();
    loadModelOptions();
    loadRuntimeOptions(
      getState().conversationSettings?.agent || getState().conversationMeta?.settings?.agent || null,
      getState().conversationMeta?.conversation_id,
    );
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
        setRolloutPickerProvider: (provider) => { setState({ rolloutPickerProvider: provider || null }); },
        getRolloutPickerProvider: () => getState().rolloutPickerProvider || null,
        getPickerPath: () => getState().pickerPath,
        setPickerPath: (val) => { setState({ pickerPath: val }); },
        getPickerMode: () => getState().pickerMode,
        setPickerMode: (val) => { setState({ pickerMode: val || 'cwd' }); },
      },
      state: {
        get pendingNewConversation() { return getState().pendingNewConversation; },
        set pendingNewConversation(val) { setState({ pendingNewConversation: Boolean(val) }); },
        get pendingRollout() { return getState().pendingRollout; },
        get rolloutPickerProvider() { return getState().rolloutPickerProvider || null; },
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
    // Legacy builtin app-server controls removed from the codex-agent UI.
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
