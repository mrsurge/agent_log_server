import { createSettingsRpcClientPlaceholder } from '../rpc/settings/client.ts';

const _settingsRpcClientPlaceholder = createSettingsRpcClientPlaceholder;
void _settingsRpcClientPlaceholder;

export function bindSettingsUiFlow(ctx) {
  const {
    getState,
    setState,
    elements,
    sioCall,
    setActivity,
    getRelativePath,
    insertMention,
    getWindow,
  } = ctx;

  const {
    settingsModalEl,
    settingsCwdEl,
    settingsApprovalEl,
    settingsSandboxEl,
    settingsModelEl,
    settingsEffortEl,
    settingsSummaryEl,
    settingsDeveloperInstructionsEl,
    settingsLabelEl,
    settingsAliasEl,
    settingsCommandLinesEl,
    settingsViewWrapEl,
    settingsMarkdownEl,
    settingsDiffSyntaxEl,
    settingsSemanticShellRibbonEl,
    settingsTe2McpIntegrationEl,
    settingsAgentEl,
    settingsAgentOptions,
    settingsAgentRowEl,
    settingsRolloutEl,
    settingsRolloutRowEl,
    settingsApprovalOptions,
    settingsSandboxOptions,
    settingsModelOptions,
    settingsEffortOptions,
    settingsSummaryOptions,
    pickerOverlayEl,
    pickerPathEl,
    pickerListEl,
    pickerTitleEl,
    pickerFilterEl,
    rolloutOverlayEl,
    rolloutListEl,
  } = elements;
  const windowRef = getWindow ? getWindow() : window;
  const pickerCloseBtn = elements.pickerCloseBtn || windowRef?.document?.getElementById('picker-close');
  const pickerUpBtn = elements.pickerUpBtn || windowRef?.document?.getElementById('picker-up');
  const pickerSelectBtn = elements.pickerSelectBtn || windowRef?.document?.getElementById('picker-select');
  let pickerTargetInput = null;

  function isValueInput(candidate) {
    return !!candidate && typeof candidate.value === 'string';
  }

  function getSchemaFieldInput(fieldId) {
    const getter = windowRef?.CodexAgent?.helpers?.getSchemaFieldInput;
    if (typeof getter !== 'function') return null;
    return getter(fieldId);
  }

  function getActiveCwdInput() {
    const schemaInput = getSchemaFieldInput('cwd');
    if (isValueInput(schemaInput)) return schemaInput;
    if (isValueInput(settingsCwdEl)) return settingsCwdEl;
    return null;
  }

  function getActiveCwdValue({ fallbackToSaved = true } = {}) {
    const input = getActiveCwdInput();
    const currentValue = isValueInput(input) ? input.value.trim() : '';
    if (currentValue) return currentValue;
    if (!fallbackToSaved) return '';
    const saved = getState().conversationSettings?.cwd;
    return typeof saved === 'string' ? saved.trim() : '';
  }

  function setPickerChrome(mode) {
    if (pickerTitleEl) {
      pickerTitleEl.textContent = mode === 'mention' ? 'Mentioning' : 'Pick CWD';
    }
    const isMention = mode === 'mention';
    if (pickerSelectBtn) pickerSelectBtn.style.display = isMention ? 'none' : '';
    if (pickerUpBtn) pickerUpBtn.style.display = isMention ? 'none' : '';
  }

  function applyPickedCwd(path) {
    const nextPath = typeof path === 'string' ? path.trim() : '';
    if (!nextPath) return;
    const targetInput = isValueInput(pickerTargetInput) ? pickerTargetInput : getActiveCwdInput();
    if (isValueInput(targetInput)) {
      targetInput.value = nextPath;
    }
    if (isValueInput(settingsCwdEl) && targetInput !== settingsCwdEl) {
      settingsCwdEl.value = nextPath;
    }
  }

  function parentPickerPath(path) {
    const raw = typeof path === 'string' ? path.trim() : '';
    if (!raw || raw === '~' || raw === '/') return raw || '~';
    if (raw.startsWith('~/')) {
      const rest = raw.slice(2).replace(/\/+$/, '');
      if (!rest) return '~';
      const slash = rest.lastIndexOf('/');
      return slash === -1 ? '~' : `~/${rest.slice(0, slash)}`;
    }
    const trimmed = raw.replace(/\/+$/, '');
    const slash = trimmed.lastIndexOf('/');
    return slash <= 0 ? '/' : trimmed.slice(0, slash);
  }

  function normalizeRuntimeOption(option) {
    if (!option || typeof option !== 'object') return null;
    const settingKey = typeof option.settingKey === 'string' ? option.settingKey.trim() : '';
    const options = Array.isArray(option.options)
      ? option.options
          .map((item) => {
            if (typeof item === 'string') {
              const text = item.trim();
              return text ? { value: text, label: text } : null;
            }
            if (!item || typeof item !== 'object') return null;
            const value = typeof item.value === 'string' ? item.value.trim() : '';
            if (!value) return null;
            const label = typeof item.label === 'string' && item.label.trim() ? item.label.trim() : value;
            return { value, label };
          })
          .filter(Boolean)
      : [];
    return {
      settingKey,
      options,
      current: typeof option.current === 'string' ? option.current : '',
      default: typeof option.default === 'string' ? option.default : '',
    };
  }

  function getSettingValueByKey(settings, key) {
    if (!settings || typeof settings !== 'object' || !key) return '';
    const value = settings[key];
    return typeof value === 'string' ? value : '';
  }

  function getActiveAgentOptions(state = getState()) {
    const extensions = Array.isArray(state?.extensionCatalog) ? state.extensionCatalog : [];
    return extensions
      .filter((ext) => ext?.active === true && ext?.id && ext.id !== 'codex')
      .map((ext) => ({
        value: ext.id,
        label: ext.name || ext.id,
      }));
  }

  function getDefaultAgentId(state = getState()) {
    const runtimeAgent = typeof state?.runtimeOptions?.agent === 'string' ? state.runtimeOptions.agent.trim() : '';
    if (runtimeAgent && runtimeAgent !== 'codex') return runtimeAgent;
    return getActiveAgentOptions(state)[0]?.value || '';
  }

  function resolveAgentId(candidate, state = getState()) {
    const agent = typeof candidate === 'string' ? candidate.trim() : '';
    if (agent && agent !== 'codex') return agent;
    const savedAgent = typeof state?.conversationSettings?.agent === 'string' ? state.conversationSettings.agent.trim() : '';
    if (savedAgent && savedAgent !== 'codex') return savedAgent;
    return getDefaultAgentId(state);
  }

  function applyRuntimeOptions(runtimeOptions) {
    const state = getState();
    const approval = normalizeRuntimeOption(runtimeOptions?.approval);
    const sandbox = normalizeRuntimeOption(runtimeOptions?.sandbox);
    if (approval) {
      updateDropdownOptions(settingsApprovalOptions, approval.options, settingsApprovalEl);
      if (settingsApprovalEl) {
        settingsApprovalEl.placeholder = approval.default || 'Use runtime default';
        settingsApprovalEl.value = getSettingValueByKey(state.conversationSettings, approval.settingKey)
          || approval.current
          || settingsApprovalEl.value
          || '';
      }
    } else {
      updateDropdownOptions(settingsApprovalOptions, [], settingsApprovalEl);
    }
    if (sandbox) {
      updateDropdownOptions(settingsSandboxOptions, sandbox.options, settingsSandboxEl);
      if (settingsSandboxEl) {
        settingsSandboxEl.placeholder = sandbox.default || 'Use runtime default';
        settingsSandboxEl.value = getSettingValueByKey(state.conversationSettings, sandbox.settingKey)
          || sandbox.current
          || settingsSandboxEl.value
          || '';
      }
    } else {
      updateDropdownOptions(settingsSandboxOptions, [], settingsSandboxEl);
    }
  }

  async function loadRuntimeOptions(agentId, conversationId) {
    const agent = typeof agentId === 'string' && agentId.trim() ? agentId.trim() : '';
    const conversation_id = typeof conversationId === 'string' && conversationId.trim() ? conversationId.trim() : '';
    try {
      const data = await sioCall('get_runtime_options', {
        conversation_id: conversation_id || null,
        agent: agent || null,
      });
      const next = (data && typeof data === 'object') ? data : {};
      setState({ runtimeOptions: next });
      applyRuntimeOptions(next);
      return next;
    } catch {
      const next = {};
      setState({ runtimeOptions: next });
      applyRuntimeOptions(next);
      return next;
    }
  }

  async function openSettingsModal() {
    if (!settingsModalEl) return;
    const state = getState();
    if (state.pendingNewConversation) {
      if (settingsCwdEl) {
        const projectPrefill = (state.hostUi?.ideMode && state.splashTab === 'project' && typeof state.hostUi?.projectRoot === 'string' && state.hostUi.projectRoot)
          ? state.hostUi.projectRoot
          : '';
        settingsCwdEl.value = projectPrefill;
      }
      if (settingsApprovalEl) settingsApprovalEl.value = '';
      if (settingsSandboxEl) settingsSandboxEl.value = '';
      if (settingsModelEl) settingsModelEl.value = '';
      if (settingsEffortEl) settingsEffortEl.value = '';
      if (settingsSummaryEl) settingsSummaryEl.value = '';
      if (settingsDeveloperInstructionsEl) settingsDeveloperInstructionsEl.value = '';
      if (settingsLabelEl) settingsLabelEl.value = '';
      if (settingsAliasEl) settingsAliasEl.value = '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = '20';
      if (settingsViewWrapEl) settingsViewWrapEl.checked = false;
      if (settingsMarkdownEl) settingsMarkdownEl.checked = true;
      if (settingsRolloutEl) settingsRolloutEl.value = state.pendingRollout?.id || '';
      if (settingsSemanticShellRibbonEl) settingsSemanticShellRibbonEl.checked = false;
      if (settingsTe2McpIntegrationEl) settingsTe2McpIntegrationEl.checked = false;
      if (settingsAgentEl) settingsAgentEl.value = resolveAgentId('', state);
    } else {
      if (settingsCwdEl) settingsCwdEl.value = state.conversationSettings?.cwd || '';
      if (settingsApprovalEl) settingsApprovalEl.value = getSettingValueByKey(state.conversationSettings, state.runtimeOptions?.approval?.settingKey) || state.conversationSettings?.approvalPolicy || '';
      if (settingsSandboxEl) settingsSandboxEl.value = getSettingValueByKey(state.conversationSettings, state.runtimeOptions?.sandbox?.settingKey) || state.conversationSettings?.sandboxPolicy || '';
      if (settingsModelEl) settingsModelEl.value = state.conversationSettings?.model || '';
      updateEffortOptionsForModel(state.conversationSettings?.model);
      if (settingsEffortEl) settingsEffortEl.value = state.conversationSettings?.effort || '';
      if (settingsSummaryEl) settingsSummaryEl.value = state.conversationSettings?.summary || '';
      if (settingsDeveloperInstructionsEl) settingsDeveloperInstructionsEl.value = state.conversationSettings?.developer_instructions || '';
      if (settingsLabelEl) settingsLabelEl.value = state.conversationSettings?.label || '';
      if (settingsAliasEl) settingsAliasEl.value = state.conversationSettings?.alias || '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = state.conversationSettings?.commandOutputLines || '20';
      if (settingsViewWrapEl) settingsViewWrapEl.checked = state.conversationSettings?.viewWrap === true;
      if (settingsMarkdownEl) settingsMarkdownEl.checked = state.conversationSettings?.markdown !== false;
      if (settingsDiffSyntaxEl) settingsDiffSyntaxEl.checked = state.conversationSettings?.diffSyntax === true;
      if (settingsSemanticShellRibbonEl) settingsSemanticShellRibbonEl.checked = state.conversationSettings?.semanticShellRibbon === true;
      if (settingsTe2McpIntegrationEl) settingsTe2McpIntegrationEl.checked = state.conversationSettings?.te2_mcp_integration === true;
      if (settingsRolloutEl) settingsRolloutEl.value = state.pendingRollout?.id || state.conversationSettings?.rolloutId || '';
      if (settingsAgentEl) settingsAgentEl.value = resolveAgentId(state.conversationSettings?.agent, state);
    }
    if (settingsAgentRowEl) {
      const hasSavedSettings = !state.pendingNewConversation && state.conversationMeta?.settings && Object.values(state.conversationMeta.settings).some((v) => v);
      settingsAgentRowEl.style.display = hasSavedSettings ? 'none' : 'block';
    }
    if (settingsRolloutRowEl) {
      const hasSavedSettings = !state.pendingNewConversation && state.conversationMeta?.settings && Object.values(state.conversationMeta.settings).some((v) => v);
      settingsRolloutRowEl.style.display = hasSavedSettings ? 'none' : 'block';
    }
    settingsModalEl.classList.remove('hidden');
    const currentAgent = resolveAgentId(settingsAgentEl?.value, state);
    if (settingsAgentEl) settingsAgentEl.value = currentAgent;
    if (currentAgent) {
      await onAgentSelectionChange(currentAgent);
    }
  }

  function closeSettingsModal() {
    if (!settingsModalEl) return;
    const state = getState();
    const agentType = resolveAgentId(settingsAgentEl?.value, state);
    if (!agentType) {
      setActivity('Agent required', true);
      return;
    }
    const cwdOk = Boolean(getActiveCwdValue({ fallbackToSaved: true }));
    if (!cwdOk) {
      setActivity('CWD required', true);
      return;
    }
    setState({ pendingNewConversation: false });
    settingsModalEl.classList.add('hidden');
  }

  function openPicker(startPath, mode = 'cwd', options = {}) {
    if (!pickerOverlayEl) return;
    const nextMode = mode || 'cwd';
    pickerTargetInput = nextMode === 'cwd' && isValueInput(options?.input)
      ? options.input
      : (nextMode === 'cwd' ? getActiveCwdInput() : null);
    const nextPath = startPath
      || (nextMode === 'cwd' ? getActiveCwdValue({ fallbackToSaved: true }) : '')
      || getState().pickerPath
      || '~';
    setState({ pickerMode: nextMode, pickerPath: nextPath });
    setPickerChrome(nextMode);
    pickerOverlayEl.classList.remove('hidden');
    fetchPicker(nextPath);
    if (pickerFilterEl) {
      pickerFilterEl.value = '';
      setTimeout(() => pickerFilterEl.focus(), 0);
    }
  }

  function closePicker() {
    if (!pickerOverlayEl) return;
    pickerOverlayEl.classList.add('hidden');
    pickerTargetInput = null;
    setPickerChrome('cwd');
    setState({ pickerMode: 'cwd' });
  }

  function bindPickerFilter() {
    if (!pickerFilterEl) return;
    pickerFilterEl.addEventListener('input', () => {
      const state = getState();
      if (state.filterTimer) clearTimeout(state.filterTimer);
      const timer = setTimeout(() => {
        applyPickerFilter();
      }, 150);
      setState({ filterTimer: timer });
    });
  }

  function openRolloutPicker() {
    if (!rolloutOverlayEl) return;
    const cwdOk = Boolean(getActiveCwdValue({ fallbackToSaved: false }));
    if (!cwdOk) {
      setActivity('select CWD first', true);
      return;
    }
    rolloutOverlayEl.classList.remove('hidden');
    fetchRollouts();
  }

  function closeRolloutPicker() {
    if (!rolloutOverlayEl) return;
    rolloutOverlayEl.classList.add('hidden');
  }

  function getRolloutPickerProvider() {
    const provider = getState().rolloutPickerProvider;
    if (provider && typeof provider === 'object') return provider;
    return null;
  }

  function renderRolloutList(items, emptyText = 'No rollouts found') {
    if (!rolloutListEl) return;
    rolloutListEl.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-item';
      empty.textContent = emptyText;
      rolloutListEl.appendChild(empty);
      return;
    }
    items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'picker-item rollout-item';
      row.dataset.rolloutId = item?.id || '';
      const idSpan = document.createElement('span');
      idSpan.className = 'rollout-id';
      idSpan.textContent = item?.short_id || item?.id || '';
      const previewSpan = document.createElement('span');
      previewSpan.className = 'rollout-preview';
      previewSpan.textContent = item?.preview || '';
      row.append(idSpan, previewSpan);
      rolloutListEl.appendChild(row);
    });
  }

  async function fetchRollouts() {
    const provider = getRolloutPickerProvider();
    if (!provider || typeof provider.list !== 'function') {
      renderRolloutList([], 'No rollout provider');
      setActivity('rollout picker unavailable', true);
      return;
    }
    try {
      const cwd = getActiveCwdValue({ fallbackToSaved: false });
      const data = await provider.list({ cwd, state: getState(), setState, setActivity, sioCall });
      let items = Array.isArray(data?.items) ? data.items : [];
      if (cwd) {
        items = items.filter((item) => item && item.cwd && String(item.cwd) === cwd);
      }
      renderRolloutList(items);
    } catch (err) {
      console.warn('rollout list failed', err);
      renderRolloutList([], 'Rollout list unavailable');
    }
  }

  async function loadRolloutPreview(rolloutId) {
    if (!rolloutId) return;
    const provider = getRolloutPickerProvider();
    if (!provider || typeof provider.preview !== 'function') {
      setActivity('rollout preview unavailable', true);
      return;
    }
    try {
      const cwd = getActiveCwdValue({ fallbackToSaved: false });
      const data = await provider.preview({
        rolloutId,
        cwd,
        state: getState(),
        setState,
        setActivity,
        sioCall,
      });
      const items = Array.isArray(data?.items) ? data.items : [];
      setState({
        pendingRollout: {
          id: rolloutId,
          items,
          token_total: data?.token_total ?? null,
        },
      });
      if (settingsRolloutEl) settingsRolloutEl.value = rolloutId;
      closeRolloutPicker();
    } catch (err) {
      console.warn('rollout preview failed', err);
      setActivity('rollout failed', true);
    }
  }

  function normalizeDropdownOption(option) {
    if (typeof option === 'string') {
      const text = option.trim();
      return text ? { value: text, label: text } : null;
    }
    if (!option || typeof option !== 'object') return null;
    const value = typeof option.value === 'string' ? option.value.trim() : '';
    if (!value) return null;
    const label = typeof option.label === 'string' && option.label.trim() ? option.label.trim() : value;
    return { value, label };
  }

  function buildDropdown(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    listEl.innerHTML = '';
    (options || [])
      .map(normalizeDropdownOption)
      .filter(Boolean)
      .forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.dataset.value = opt.value;
      btn.textContent = opt.label;
      btn.addEventListener('click', () => {
        if (inputEl) inputEl.value = opt.value;
        closeDropdownMenu(listEl);
        if (typeof onChange === 'function') onChange(opt.value);
      });
      listEl.appendChild(btn);
    });
  }

  function updateDropdownOptions(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    const seen = new Set();
    const values = [];
    (options || []).forEach((option) => {
      const normalized = normalizeDropdownOption(option);
      if (!normalized || seen.has(normalized.value)) return;
      seen.add(normalized.value);
      values.push(normalized);
    });
    buildDropdown(listEl, values, inputEl, onChange);
  }

  async function loadModelOptions(agentId = '') {
    try {
      const resolvedAgent = resolveAgentId(agentId, getState());
      if (!resolvedAgent) {
        setState({ modelList: [] });
        updateDropdownOptions(settingsModelOptions, [], settingsModelEl, updateEffortOptionsForModel);
        updateEffortOptionsForModel(settingsModelEl?.value);
        return;
      }
      const data = await sioCall('get_extension_models', { extension_id: resolvedAgent });
      const items = Array.isArray(data)
        ? data
        : data?.models || data?.data || data?.result?.models || data?.result?.data || [];
      if (Array.isArray(items)) {
        setState({ modelList: items.filter((m) => m && typeof m === 'object' && m.id) });
        const names = items.filter((m) => m && typeof m === 'object' && m.id).map((m) => m.id);
        if (names.length) {
          updateDropdownOptions(settingsModelOptions, names, settingsModelEl, updateEffortOptionsForModel);
        }
        updateEffortOptionsForModel(settingsModelEl?.value);
      }
    } catch {
      // ignore
    }
  }

  async function loadAgentOptions() {
    try {
      const data = await sioCall('get_extensions', {});
      const extensions = data?.extensions || [];
      const nextState = { ...getState(), extensionCatalog: Array.isArray(extensions) ? extensions : [] };
      setState({ extensionCatalog: nextState.extensionCatalog });
      const agents = getActiveAgentOptions(nextState);
      updateDropdownOptions(settingsAgentOptions, agents, settingsAgentEl, onAgentSelectionChange);
      const resolvedAgent = resolveAgentId(settingsAgentEl?.value, nextState);
      if (settingsAgentEl) settingsAgentEl.value = resolvedAgent;
    } catch {
      // ignore
    }
  }

  getWindow()?.addEventListener?.('codexagent:extensions-updated', () => {
    void loadAgentOptions();
  });

  async function onAgentSelectionChange(agentId) {
    const resolvedAgent = resolveAgentId(agentId, getState());
    if (settingsAgentEl) settingsAgentEl.value = resolvedAgent;
    const win = getWindow ? getWindow() : window;
    if (win.CodexAgent?.helpers?.onAgentChange) {
      await win.CodexAgent.helpers.onAgentChange(resolvedAgent);
    }
    await loadModelOptions(resolvedAgent);
    await loadRuntimeOptions(resolvedAgent, getState().conversationMeta?.conversation_id);
  }

  function normalizeModelEfforts(model) {
    const raw = model?.supportedReasoningEfforts ?? model?.supported_reasoning_efforts;
    if (!Array.isArray(raw)) return [];
    return raw
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          return item.reasoningEffort || item.reasoning_effort || item.value || '';
        }
        return '';
      })
      .filter(Boolean);
  }

  function updateEffortOptionsForModel(modelId) {
    const state = getState();
    if (!modelId || !state.modelList.length) return;
    const model = state.modelList.find((item) => item.id === modelId);
    if (!model) {
      updateDropdownOptions(settingsEffortOptions, ['low', 'medium', 'high'], settingsEffortEl);
      return;
    }
    const efforts = normalizeModelEfforts(model);
    if (!efforts.length) {
      updateDropdownOptions(settingsEffortOptions, ['low', 'medium', 'high'], settingsEffortEl);
      return;
    }
    updateDropdownOptions(settingsEffortOptions, efforts, settingsEffortEl);
    const currentEffort = settingsEffortEl?.value;
    const defaultEffort = model.defaultReasoningEffort || model.default_reasoning_effort || efforts[0] || '';
    if (currentEffort && !efforts.includes(currentEffort) && settingsEffortEl) {
      settingsEffortEl.value = defaultEffort;
    }
  }

  function openDropdownMenu(listEl) {
    if (!listEl) return;
    const state = getState();
    if (state.openDropdownEl && state.openDropdownEl !== listEl) {
      closeDropdownMenu(state.openDropdownEl);
    }
    listEl.classList.add('open');
    setState({ openDropdownEl: listEl });
  }

  function closeDropdownMenu(listEl) {
    if (!listEl) return;
    listEl.classList.remove('open');
    if (getState().openDropdownEl === listEl) {
      setState({ openDropdownEl: null });
    }
  }

  function toggleDropdownMenu(listEl) {
    if (!listEl) return;
    if (listEl.classList.contains('open')) {
      closeDropdownMenu(listEl);
    } else {
      openDropdownMenu(listEl);
    }
  }

  function setupDropdown(inputEl, toggleEl, listEl, options) {
    if (!listEl || !inputEl) return;
    buildDropdown(listEl, options, inputEl);
    toggleEl?.addEventListener('click', (evt) => {
      evt.preventDefault();
      toggleDropdownMenu(listEl);
    });
  }

  async function fetchPicker(path) {
    try {
      const data = await sioCall('fs_list', { path: path || '~' });
      if (!data || data.ok === false) return;
      setState({
        pickerPath: data?.path || path || '~',
        pickerItems: Array.isArray(data?.items) ? data.items : [],
      });
      if (pickerPathEl) pickerPathEl.textContent = data?.path || path || '~';
      applyPickerFilter();
    } catch {
      // ignore
    }
  }

  async function fetchPickerSearch(query) {
    try {
      const state = getState();
      const root = state.conversationSettings?.cwd || settingsCwdEl?.value || state.pickerPath || '~';
      const data = await sioCall('fs_search', { query, root });
      if (!data || data.ok === false) return [];
      return Array.isArray(data?.items) ? data.items : [];
    } catch {
      return [];
    }
  }

  function applyPickerFilter() {
    const state = getState();
    if (!pickerFilterEl) {
      renderPickerList(state.pickerItems || []);
      return;
    }
    const raw = pickerFilterEl.value || '';
    if (!raw.trim()) {
      renderPickerList(state.pickerItems || []);
      return;
    }
    if (state.pickerMode === 'mention') {
      fetchPickerSearch(raw).then(renderPickerList);
      return;
    }
    let regex = null;
    try {
      regex = new RegExp(raw, 'i');
    } catch {
      renderPickerList([]);
      return;
    }
    const items = (state.pickerItems || []).filter((item) => {
      const target = `${item?.name || ''} ${item?.path || ''}`;
      return regex.test(target);
    });
    renderPickerList(items);
  }

  function renderPickerList(items) {
    if (!pickerListEl) return;
    pickerListEl.innerHTML = '';
    const state = getState();
    items.forEach((item) => {
      if (!item) return;
      const row = document.createElement('div');
      row.className = 'picker-item';
      const icon = document.createElement('span');
      icon.textContent = item.type === 'directory' ? '📁' : '📄';
      const name = document.createElement('span');
      name.textContent = item.name || item.path;
      if (state.pickerMode === 'mention') {
        const textWrap = document.createElement('span');
        textWrap.className = 'picker-item-text';
        name.className = 'picker-item-name';
        const path = document.createElement('span');
        path.className = 'picker-item-path';
        const cwd = state.conversationSettings?.cwd || '';
        const relPath = getRelativePath(item.path || item.name || '', cwd) || (item.path || item.name || '');
        path.textContent = relPath;
        textWrap.append(name, path);
        row.append(icon, textWrap);
      } else {
        row.append(icon, name);
      }
      row.addEventListener('click', () => {
        if (item.type === 'directory') {
          fetchPicker(item.path);
          return;
        }
        if (getState().pickerMode === 'mention') {
          insertMention(item.path || item.name || '');
          closePicker();
        }
      });
      pickerListEl.appendChild(row);
    });
  }

  pickerCloseBtn?.addEventListener('click', closePicker);
  pickerUpBtn?.addEventListener('click', () => {
    if (getState().pickerMode === 'mention') return;
    const currentPath = getState().pickerPath || getActiveCwdValue({ fallbackToSaved: true }) || '~';
    fetchPicker(parentPickerPath(currentPath));
  });
  pickerSelectBtn?.addEventListener('click', () => {
    if (getState().pickerMode === 'mention') return;
    const currentPath = getState().pickerPath || pickerPathEl?.textContent || '';
    applyPickedCwd(currentPath);
    closePicker();
  });

  return {
    openSettingsModal,
    closeSettingsModal,
    openPicker,
    closePicker,
    bindPickerFilter,
    openRolloutPicker,
    closeRolloutPicker,
    fetchRollouts,
    loadRolloutPreview,
    buildDropdown,
    updateDropdownOptions,
    loadModelOptions,
    loadRuntimeOptions,
    loadAgentOptions,
    onAgentSelectionChange,
    updateEffortOptionsForModel,
    openDropdownMenu,
    closeDropdownMenu,
    toggleDropdownMenu,
    setupDropdown,
    fetchPicker,
    fetchPickerSearch,
    applyPickerFilter,
  };
}
