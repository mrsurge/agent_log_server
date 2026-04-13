type CodexAgentHelpers = {
  getSchemaParsedValues?: () => unknown;
  getSchemaValues?: () => unknown;
};

type CodexAgentWindow = Window & typeof globalThis & {
  CodexAgent?: {
    helpers?: CodexAgentHelpers;
  };
};

export function bindSettingsSaveFlow(ctx) {
  const {
    getState,
    setState,
    elements,
    normalizeApprovalValue,
    setActivity,
    setMarkdownEnabled,
    setViewWrapEnabled,
    setDiffSyntaxEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    sioCall,
    closeSettingsModal,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript,
    refreshPlanSurface,
    restorePendingApprovals,
    setDrawerOpen,
    updateConversationHeaderLabel,
  } = ctx;

  async function saveSettings() {
    const codexWindow = window as CodexAgentWindow;
    const state = getState();
    const {
      settingsAgentEl,
      settingsCwdEl,
      settingsCommandLinesEl,
      settingsMarkdownEl,
      settingsDiffSyntaxEl,
      settingsSemanticShellRibbonEl,
      settingsTe2McpIntegrationEl,
      settingsApprovalEl,
      settingsSandboxEl,
      settingsModelEl,
      settingsEffortEl,
      settingsSummaryEl,
      settingsDeveloperInstructionsEl,
      settingsLabelEl,
      settingsAliasEl,
      settingsViewWrapEl,
    } = elements;

    const agentType = settingsAgentEl?.value?.trim() || state.runtimeOptions?.agent || '';
    if (!agentType) {
      setActivity('Agent required', true);
      return;
    }
    const normalizeStringSetting = (value) => {
      if (typeof value === 'string') {
        const trimmed = value.trim();
        return trimmed || null;
      }
      return value == null ? null : value;
    };

    const commandLinesVal = parseInt(settingsCommandLinesEl?.value?.trim() || '20', 10);
    const viewWrapEnabled = settingsViewWrapEl?.checked === true;
    const mdEnabled = settingsMarkdownEl?.checked !== false;
    const diffSyntaxEnabled = settingsDiffSyntaxEl?.checked === true;
    const semanticRibbonEnabled = settingsSemanticShellRibbonEl?.checked === true;

    let schemaValues = {};
    try {
      schemaValues =
        codexWindow.CodexAgent?.helpers?.getSchemaParsedValues?.()
        || codexWindow.CodexAgent?.helpers?.getSchemaValues?.()
        || {};
    } catch (err) {
      setActivity(err instanceof Error ? err.message : String(err), true);
      return;
    }
    if (!schemaValues || typeof schemaValues !== 'object' || Array.isArray(schemaValues)) {
      schemaValues = {};
    }
    const approvalKey = state.runtimeOptions?.approval?.settingKey || 'approvalPolicy';
    const sandboxKey = state.runtimeOptions?.sandbox?.settingKey || 'sandboxPolicy';
    const schemaManagedKeys = new Set(Object.keys(schemaValues));
    const normalizedSchemaSettings = Object.fromEntries(
      Object.entries(schemaValues).map(([key, value]) => {
        if (key === approvalKey) {
          const approvalValue = typeof value === 'string' ? normalizeApprovalValue(value.trim()) : '';
          return [key, approvalValue || null];
        }
        return [key, normalizeStringSetting(value)];
      })
    );
    const schemaManages = (key) => schemaManagedKeys.has(key);

    let cwd = schemaManages('cwd')
      ? normalizedSchemaSettings.cwd
      : normalizeStringSetting(settingsCwdEl?.value);
    if (!cwd && !schemaManages('cwd')) {
      cwd = normalizeStringSetting(state.conversationSettings?.cwd);
    }
    if (!cwd) {
      setActivity('CWD required', true);
      return;
    }

    const preservedSettings = { ...(state.conversationSettings || {}) };
    [
      'cwd',
      'approvalPolicy',
      'sandboxPolicy',
      'sandbox',
      'model',
      'effort',
      'summary',
      'developer_instructions',
      'label',
      'alias',
       'commandOutputLines',
       'viewWrap',
       'markdown',
       'diffSyntax',
      'semanticShellRibbon',
      'te2_mcp_integration',
      'trackEdits',
      'lineNumbers',
      'agent',
      approvalKey,
      sandboxKey,
      ...Object.keys(schemaValues),
    ].forEach((key) => {
      if (!key) return;
      delete preservedSettings[key];
    });
    const settings = {
      ...preservedSettings,
      ...normalizedSchemaSettings,
      cwd,
      label: normalizeStringSetting(settingsLabelEl?.value),
      alias: normalizeStringSetting(settingsAliasEl?.value),
      commandOutputLines: Number.isFinite(commandLinesVal) && commandLinesVal > 0 ? commandLinesVal : 20,
      viewWrap: viewWrapEnabled,
      markdown: mdEnabled,
      diffSyntax: diffSyntaxEnabled,
      semanticShellRibbon: semanticRibbonEnabled,
      te2_mcp_integration: settingsTe2McpIntegrationEl?.checked === true,
      trackEdits: state.trackEditsEnabled,
      lineNumbers: state.lineNumbersEnabled === true,
      agent: agentType,
    };
    if (!schemaManages(approvalKey)) {
      settings[approvalKey] = normalizeApprovalValue(settingsApprovalEl?.value?.trim()) || null;
    }
    if (!schemaManages(sandboxKey)) {
      settings[sandboxKey] = normalizeStringSetting(settingsSandboxEl?.value);
    }
    if (!schemaManages('model')) {
      settings.model = normalizeStringSetting(settingsModelEl?.value);
    }
    if (!schemaManages('reasoning_effort')) {
      settings.effort = normalizeStringSetting(settingsEffortEl?.value);
    }
    if (!schemaManages('summary')) {
      settings.summary = normalizeStringSetting(settingsSummaryEl?.value);
    }
    if (!schemaManages('developer_instructions')) {
      settings.developer_instructions = normalizeStringSetting(settingsDeveloperInstructionsEl?.value);
    }

    setMarkdownEnabled(mdEnabled);
    setViewWrapEnabled(viewWrapEnabled);
    setDiffSyntaxEnabled(diffSyntaxEnabled);
    setSemanticShellRibbonEnabled(semanticRibbonEnabled);
    if (semanticRibbonEnabled) {
      await ensureTreeSitterRibbonReady();
    }

    let nextState = getState();
    const isNewConversation = nextState.pendingNewConversation || !nextState.conversationMeta?.conversation_id;
    if (isNewConversation) {
      const meta = await sioCall('conversation_create', {});
      if (meta?.conversation_id) {
        setState({
          clientConversationId: meta.conversation_id,
          clientActiveView: 'conversation',
          conversationMeta: meta,
          conversationSettings: meta?.settings || {},
        });
      }
      setState({ pendingNewConversation: false });
    }

    nextState = getState();
    await sioCall('conversation_update', {
      conversation_id: nextState.conversationMeta?.conversation_id, settings,
    });

    closeSettingsModal();
    await fetchConversation(getState().conversationMeta?.conversation_id);
    await fetchConversations();
    if (isNewConversation) {
      resetTimeline();
    }
    await replayTranscript();
    await refreshPlanSurface?.();
    restorePendingApprovals();
    setDrawerOpen(true);
    updateConversationHeaderLabel();
  }

  return { saveSettings };
}
