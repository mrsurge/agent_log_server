import { createConversationsRpcClient } from '../rpc/conversations/client.ts';

type CodexAgentHelpers = {
  getSchemaParsedValues?: () => unknown;
  getSchemaValues?: () => unknown;
};

type CodexAgentWindow = Window & typeof globalThis & {
  CodexAgent?: {
    helpers?: CodexAgentHelpers;
  };
};

type TextValueInput = HTMLElement & { value: string };
type ToggleInput = HTMLInputElement | null;

type RuntimeSettingDescriptor = {
  settingKey?: string | null;
};

type RuntimeOptionsState = {
  agent?: string | null;
  approval?: RuntimeSettingDescriptor | null;
  sandbox?: RuntimeSettingDescriptor | null;
};

type ConversationSettingsState = Record<string, unknown> & {
  cwd?: string | null;
};

type ConversationMetaState = Record<string, unknown> & {
  conversation_id?: string | null;
  settings?: ConversationSettingsState | null;
};

type SettingsSaveState = {
  runtimeOptions?: RuntimeOptionsState | null;
  conversationSettings?: ConversationSettingsState | null;
  conversationMeta?: ConversationMetaState | null;
  trackEditsEnabled?: boolean;
  lineNumbersEnabled?: boolean;
  pendingNewConversation?: boolean;
  clientConversationId?: string | null;
  clientActiveView?: string | null;
};

type SettingsSaveElements = {
  settingsAgentEl?: TextValueInput | null;
  settingsCwdEl?: TextValueInput | null;
  settingsCommandLinesEl?: TextValueInput | null;
  settingsMarkdownEl?: ToggleInput;
  settingsDiffSyntaxEl?: ToggleInput;
  settingsSemanticShellRibbonEl?: ToggleInput;
  settingsTe2McpIntegrationEl?: ToggleInput;
  settingsApprovalEl?: TextValueInput | null;
  settingsSandboxEl?: TextValueInput | null;
  settingsModelEl?: TextValueInput | null;
  settingsEffortEl?: TextValueInput | null;
  settingsSummaryEl?: TextValueInput | null;
  settingsDeveloperInstructionsEl?: TextValueInput | null;
  settingsLabelEl?: TextValueInput | null;
  settingsAliasEl?: TextValueInput | null;
  settingsViewWrapEl?: ToggleInput;
};

interface SettingsSaveFlowContext {
  getState(): SettingsSaveState;
  setState(patch: Partial<SettingsSaveState>): void;
  elements: SettingsSaveElements;
  normalizeApprovalValue(value: string | undefined): string | undefined;
  setActivity(message: string, isError: boolean): void;
  setMarkdownEnabled(enabled: boolean): void;
  setViewWrapEnabled(enabled: boolean): void;
  setDiffSyntaxEnabled(enabled: boolean): void;
  setSemanticShellRibbonEnabled(enabled: boolean): void;
  ensureTreeSitterRibbonReady(): Promise<unknown>;
  sioCall(event: string, payload?: Record<string, unknown>): Promise<ConversationMetaState | null>;
  closeSettingsModal(): void;
  fetchConversation(conversationId?: string | null): Promise<unknown>;
  fetchConversations(): Promise<unknown>;
  resetTimeline(): void;
  replayTranscript(): Promise<unknown>;
  refreshPlanSurface?(): Promise<unknown> | unknown;
  restorePendingApprovals(): void;
  setDrawerOpen(open: boolean): void;
  updateConversationHeaderLabel(): void;
}

export function bindSettingsSaveFlow(ctx: SettingsSaveFlowContext) {
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
  const conversationsRpcClient = createConversationsRpcClient({
    windowRef: typeof window !== 'undefined' ? window : null,
  });

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
    const normalizeStringSetting = (value: unknown): unknown | null => {
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

    let schemaValues: Record<string, unknown> = {};
    try {
      const rawSchemaValues =
        codexWindow.CodexAgent?.helpers?.getSchemaParsedValues?.()
        || codexWindow.CodexAgent?.helpers?.getSchemaValues?.()
        || {};
      schemaValues = rawSchemaValues && typeof rawSchemaValues === 'object' && !Array.isArray(rawSchemaValues)
        ? rawSchemaValues as Record<string, unknown>
        : {};
    } catch (err) {
      setActivity(err instanceof Error ? err.message : String(err), true);
      return;
    }
    const approvalKey = state.runtimeOptions?.approval?.settingKey || 'approvalPolicy';
    const sandboxKey = state.runtimeOptions?.sandbox?.settingKey || 'sandboxPolicy';
    const schemaManagedKeys = new Set(Object.keys(schemaValues));
    const normalizedSchemaSettings: Record<string, unknown> = Object.fromEntries(
      Object.entries(schemaValues).map(([key, value]) => {
        if (key === approvalKey) {
          const approvalValue = typeof value === 'string' ? normalizeApprovalValue(value.trim()) : '';
          return [key, approvalValue || null];
        }
        return [key, normalizeStringSetting(value)];
      })
    );
    const schemaManages = (key: string) => schemaManagedKeys.has(key);

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

    const preservedSettings: Record<string, unknown> = { ...(state.conversationSettings || {}) };
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
    const settings: Record<string, unknown> = {
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
      const meta = await conversationsRpcClient.createConversation();
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
    await conversationsRpcClient.updateConversation({
      conversationId: nextState.conversationMeta?.conversation_id || null,
      settings,
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
