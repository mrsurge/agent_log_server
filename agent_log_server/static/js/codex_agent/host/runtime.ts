type AnyRecord = Record<string, any>;

interface HostRuntimeState {
  hostUi?: AnyRecord;
  conversationMeta?: AnyRecord;
  conversationSettings?: AnyRecord;
  activeView?: string | null;
  appConfig?: AnyRecord;
}

interface HostRuntimeContext {
  getState(): HostRuntimeState;
  setState(patch: Partial<HostRuntimeState>): void;
  sioCall(event: string, data?: Record<string, unknown>): Promise<any>;
  refreshMessageCardHeaders(): void;
  hostCloseTopEl: HTMLElement | null;
  hostCloseDrawerEl: HTMLElement | null;
  activeConversationEl: HTMLElement | null;
  conversationTitleEl: HTMLElement | null;
  splashSettingsModalEl: HTMLElement | null;
  splashSettingsUserNameEl: HTMLInputElement | null;
  splashSettingsTe2McpIntegrationEl: HTMLInputElement | null;
  documentRef: Document;
  windowRef: Window;
  getSocketConnected(): boolean;
}

export function bindHostRuntime(ctx: HostRuntimeContext) {
  const {
    getState,
    setState,
    sioCall,
    refreshMessageCardHeaders,
    hostCloseTopEl,
    hostCloseDrawerEl,
    activeConversationEl,
    conversationTitleEl,
    splashSettingsModalEl,
    splashSettingsUserNameEl,
    splashSettingsTe2McpIntegrationEl,
    documentRef,
    windowRef,
    getSocketConnected,
  } = ctx;

  function applyHostUi() {
    const { hostUi, activeView } = getState();
    const show = Boolean(hostUi?.showClose) && !Boolean(hostUi?.ideMode);
    if (hostCloseTopEl) {
      hostCloseTopEl.style.display = show && activeView !== 'conversation' ? 'inline-flex' : 'none';
    }
    if (hostCloseDrawerEl) {
      hostCloseDrawerEl.style.display = show && activeView === 'conversation' ? 'inline-flex' : 'none';
    }
    const tabsEl = documentRef.getElementById('splash-tabs');
    if (tabsEl instanceof HTMLElement) {
      tabsEl.style.display = Boolean(hostUi?.ideMode) ? 'flex' : 'none';
    }
  }

  function sendHostCloseMessage() {
    if (!windowRef.parent || windowRef.parent === windowRef) return;
    const { conversationMeta, activeView, hostUi } = getState();
    const payload = {
      type: 'codex_agent_close',
      conversation_id: conversationMeta?.conversation_id || null,
      active_view: activeView || null,
    };
    const origin = hostUi?.parentOrigin || '*';
    try {
      windowRef.parent.postMessage(payload, origin);
    } catch {
      // ignore
    }
  }

  async function fetchHostUi() {
    try {
      const data = await sioCall('get_host_ui', {});
      if (!data || data.ok === false) return;
      const ui = data?.host_ui || {};
      setState({
        hostUi: {
          showClose: Boolean(ui.show_close),
          parentOrigin: typeof ui.parent_origin === 'string' && ui.parent_origin ? ui.parent_origin : null,
          ideMode: Boolean(ui.ide_mode),
          projectRoot: typeof ui.project_root === 'string' && ui.project_root ? ui.project_root : null,
        },
      });
      applyHostUi();
    } catch {
      // ignore
    }
  }

  async function recheckSidebarConnection() {
    try {
      await sioCall('sidebar_recheck', {});
    } catch {
      // Best-effort only; host UI fetch still runs after this.
    }
  }

  async function postTe2OpenRequest({ path, line, column }: { path?: unknown; line?: unknown; column?: unknown }) {
    const { conversationMeta, conversationSettings } = getState();
    const payload: AnyRecord = {
      source: 'codex-agent',
      conversation_id: conversationMeta?.conversation_id || null,
    };
    if (typeof path === 'string' && path) {
      let nextPath = path;
      if (!nextPath.startsWith('/') && /^(?:data|home|tmp|usr|var|etc|storage)\//.test(nextPath)) {
        nextPath = `/${nextPath}`;
      }
      if (nextPath.startsWith('/')) {
        payload.path = nextPath;
      } else {
        const cwd = (conversationSettings?.cwd || '').replace(/\/+$/, '');
        payload.path = cwd ? `${cwd}/${nextPath}` : `/${nextPath}`;
      }
    }
    if (Number.isFinite(line)) payload.line = Number(line);
    if (Number.isFinite(column)) payload.column = Number(column);
    console.log('[TE2_OPEN] payload:', JSON.stringify(payload), 'socket_connected:', getSocketConnected());
    try {
      const result = await sioCall('te2_agent_open', payload);
      console.log('[TE2_OPEN] result:', JSON.stringify(result));
    } catch (err) {
      console.warn('[TE2_OPEN] error:', err);
    }
  }

  async function postExternalUrlOpenRequest(url: unknown) {
    const target = typeof url === 'string' ? url.trim() : '';
    if (!target) return;
    try {
      const { conversationMeta } = getState();
      const result = await sioCall('open_external_url', {
        url: target,
        source: 'codex-agent',
        conversation_id: conversationMeta?.conversation_id || null,
      });
      if (result?.ok === false) {
        console.warn('[OPEN_EXTERNAL_URL] failed:', JSON.stringify(result));
      }
    } catch (err) {
      console.warn('[OPEN_EXTERNAL_URL] error:', err);
    }
  }

  function updateActiveConversationLabel() {
    if (!activeConversationEl) return;
    activeConversationEl.textContent = '';
  }

  function getUserDisplayName() {
    const userName = typeof getState().appConfig?.user_name === 'string' ? getState().appConfig.user_name.trim() : '';
    return userName || 'user';
  }

  function getAssistantDisplayName() {
    const alias = typeof getState().conversationSettings?.alias === 'string' ? getState().conversationSettings.alias.trim() : '';
    return alias || 'assistant';
  }

  function getConversationHeaderTitle() {
    const alias = typeof getState().conversationSettings?.alias === 'string' ? getState().conversationSettings.alias.trim() : '';
    return alias || 'Conversation';
  }

  function updateConversationHeaderLabel() {
    if (conversationTitleEl) {
      conversationTitleEl.textContent = getConversationHeaderTitle();
    }
    const labelEl = documentRef.getElementById('conversation-label');
    if (labelEl instanceof HTMLElement) {
      labelEl.textContent = getState().conversationSettings?.label || '—';
    }
    refreshMessageCardHeaders();
  }

  function applyAppConfig(cfg: unknown) {
    const appConfig = cfg && typeof cfg === 'object' ? cfg : {};
    setState({ appConfig: appConfig as AnyRecord });
    if (splashSettingsUserNameEl) {
      splashSettingsUserNameEl.value = typeof (appConfig as AnyRecord)?.user_name === 'string' ? (appConfig as AnyRecord).user_name : '';
    }
    if (splashSettingsTe2McpIntegrationEl) {
      splashSettingsTe2McpIntegrationEl.checked = (appConfig as AnyRecord)?.te2_mcp_integration === true;
    }
    refreshMessageCardHeaders();
  }

  async function fetchAppConfig() {
    try {
      const data = await sioCall('get_config', {});
      if (!data || data.ok === false) return null;
      applyAppConfig(data);
      return data;
    } catch {
      return null;
    }
  }

  function openSplashSettingsModal() {
    if (!splashSettingsModalEl) return;
    const { appConfig } = getState();
    if (splashSettingsUserNameEl) {
      splashSettingsUserNameEl.value = typeof appConfig?.user_name === 'string' ? appConfig.user_name : '';
    }
    if (splashSettingsTe2McpIntegrationEl) {
      splashSettingsTe2McpIntegrationEl.checked = appConfig?.te2_mcp_integration === true;
    }
    splashSettingsModalEl.classList.remove('hidden');
  }

  function closeSplashSettingsModal() {
    if (!splashSettingsModalEl) return;
    splashSettingsModalEl.classList.add('hidden');
  }

  async function saveSplashSettings() {
    try {
      const data = await sioCall('update_config', {
        user_name: splashSettingsUserNameEl?.value?.trim() || null,
        te2_mcp_integration: splashSettingsTe2McpIntegrationEl?.checked === true,
      });
      if (!data || data.ok === false) return;
      applyAppConfig(data);
      closeSplashSettingsModal();
    } catch {
      // ignore
    }
  }

  return {
    applyHostUi,
    sendHostCloseMessage,
    fetchHostUi,
    recheckSidebarConnection,
    postTe2OpenRequest,
    postExternalUrlOpenRequest,
    updateActiveConversationLabel,
    getUserDisplayName,
    getAssistantDisplayName,
    updateConversationHeaderLabel,
    applyAppConfig,
    fetchAppConfig,
    openSplashSettingsModal,
    closeSplashSettingsModal,
    saveSplashSettings,
  };
}
