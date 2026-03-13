export function bindSettingsSaveFlow(ctx) {
  const {
    getState,
    setState,
    elements,
    normalizeApprovalValue,
    setActivity,
    setMarkdownEnabled,
    setXtermEnabled,
    setDiffSyntaxEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    sioCall,
    closeSettingsModal,
    fetchConversation,
    fetchConversations,
    resetTimeline,
    replayTranscript,
    restorePendingApprovals,
    setDrawerOpen,
    updateConversationHeaderLabel,
  } = ctx;

  async function saveSettings() {
    const state = getState();
    const {
      settingsAgentEl,
      settingsCwdEl,
      settingsCommandLinesEl,
      settingsMarkdownEl,
      settingsXtermEl,
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
    } = elements;

    const agentType = settingsAgentEl?.value?.trim() || 'codex';
    let cwd;
    if (agentType === 'codex') {
      cwd = settingsCwdEl?.value?.trim();
    } else {
      const schemaVals =
        window.CodexAgent?.helpers?.getSchemaRawValues?.()
        || window.CodexAgent?.helpers?.getSchemaValues?.()
        || {};
      cwd = schemaVals.cwd?.trim();
    }
    if (!cwd) cwd = state.conversationSettings?.cwd?.trim();
    if (!cwd) {
      setActivity('CWD required', true);
      return;
    }
    const commandLinesVal = parseInt(settingsCommandLinesEl?.value?.trim() || '20', 10);
    const mdEnabled = settingsMarkdownEl?.checked !== false;
    const xtermEnabled = settingsXtermEl?.checked !== false;
    const diffSyntaxEnabled = settingsDiffSyntaxEl?.checked === true;
    const semanticRibbonEnabled = settingsSemanticShellRibbonEl?.checked === true;

    let settings;
    if (agentType === 'codex') {
      const approvalKey = state.runtimeOptions?.approval?.settingKey || 'approvalPolicy';
      const sandboxKey = state.runtimeOptions?.sandbox?.settingKey || 'sandboxPolicy';
      settings = {
        cwd,
        [approvalKey]: normalizeApprovalValue(settingsApprovalEl?.value?.trim()) || null,
        [sandboxKey]: settingsSandboxEl?.value?.trim() || null,
        model: settingsModelEl?.value?.trim() || null,
        effort: settingsEffortEl?.value?.trim() || null,
        summary: settingsSummaryEl?.value?.trim() || null,
        developer_instructions: settingsDeveloperInstructionsEl?.value?.trim() || null,
        label: settingsLabelEl?.value?.trim() || null,
        alias: settingsAliasEl?.value?.trim() || null,
        commandOutputLines: Number.isFinite(commandLinesVal) && commandLinesVal > 0 ? commandLinesVal : 20,
        markdown: mdEnabled,
        useXterm: xtermEnabled,
        diffSyntax: diffSyntaxEnabled,
        semanticShellRibbon: semanticRibbonEnabled,
        te2_mcp_integration: settingsTe2McpIntegrationEl?.checked === true,
        trackEdits: state.trackEditsEnabled,
        agent: agentType,
      };
    } else {
      let schemaRaw;
      try {
        schemaRaw =
          window.CodexAgent?.helpers?.getSchemaParsedValues?.()
          || window.CodexAgent?.helpers?.getSchemaValues?.()
          || {};
      } catch (err) {
        setActivity(err instanceof Error ? err.message : String(err), true);
        return;
      }
      const schemaVals = Object.fromEntries(
        Object.entries(schemaRaw).filter(([_, v]) => v !== '' && v != null)
      );
      settings = {
        ...schemaVals,
        cwd: schemaVals.cwd?.trim() || cwd,
        label: settingsLabelEl?.value?.trim() || null,
        alias: settingsAliasEl?.value?.trim() || null,
        commandOutputLines: Number.isFinite(commandLinesVal) && commandLinesVal > 0 ? commandLinesVal : 20,
        markdown: mdEnabled,
        useXterm: xtermEnabled,
        diffSyntax: diffSyntaxEnabled,
        semanticShellRibbon: semanticRibbonEnabled,
        te2_mcp_integration: settingsTe2McpIntegrationEl?.checked === true,
        trackEdits: state.trackEditsEnabled,
        agent: agentType,
      };
    }

    setMarkdownEnabled(mdEnabled);
    setXtermEnabled(xtermEnabled);
    setDiffSyntaxEnabled(diffSyntaxEnabled);
    setSemanticShellRibbonEnabled(semanticRibbonEnabled);
    if (semanticRibbonEnabled) {
      await ensureTreeSitterRibbonReady();
    }

    let nextState = getState();
    const isNewConversation = nextState.pendingNewConversation || !nextState.conversationMeta?.conversation_id;
    if (isNewConversation) {
      const meta = await sioCall('conversation_create', {}, {
        fallbackUrl: '/api/appserver/conversations',
      });
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
    }, { fallbackUrl: '/api/appserver/conversation' });

    nextState = getState();
    if (nextState.pendingRollout?.id && Array.isArray(nextState.pendingRollout.items)) {
      setActivity('loading rollout', true);
      await sioCall('conversation_bind_rollout', {
        rollout_id: nextState.pendingRollout.id,
      }, { fallbackUrl: '/api/appserver/conversations/bind-rollout' });
      setState({ pendingRollout: null });
      setActivity('rollout loaded', false);
    }

    closeSettingsModal();
    await fetchConversation(getState().conversationMeta?.conversation_id);
    await fetchConversations();
    if (isNewConversation) {
      resetTimeline();
    }
    await replayTranscript();
    restorePendingApprovals();
    setDrawerOpen(true);
    updateConversationHeaderLabel();
  }

  return { saveSettings };
}
