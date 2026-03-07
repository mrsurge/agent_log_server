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
    settingsLabelEl,
    settingsAliasEl,
    settingsCommandLinesEl,
    settingsMarkdownEl,
    settingsXtermEl,
    settingsDiffSyntaxEl,
    settingsSemanticShellRibbonEl,
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
      if (settingsLabelEl) settingsLabelEl.value = '';
      if (settingsAliasEl) settingsAliasEl.value = '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = '20';
      if (settingsMarkdownEl) settingsMarkdownEl.checked = true;
      if (settingsRolloutEl) settingsRolloutEl.value = state.pendingRollout?.id || '';
      if (settingsSemanticShellRibbonEl) settingsSemanticShellRibbonEl.checked = false;
    } else {
      if (settingsCwdEl) settingsCwdEl.value = state.conversationSettings?.cwd || '';
      if (settingsApprovalEl) settingsApprovalEl.value = state.conversationSettings?.approvalPolicy || '';
      if (settingsSandboxEl) settingsSandboxEl.value = state.conversationSettings?.sandboxPolicy || '';
      if (settingsModelEl) settingsModelEl.value = state.conversationSettings?.model || '';
      updateEffortOptionsForModel(state.conversationSettings?.model);
      if (settingsEffortEl) settingsEffortEl.value = state.conversationSettings?.effort || '';
      if (settingsSummaryEl) settingsSummaryEl.value = state.conversationSettings?.summary || '';
      if (settingsLabelEl) settingsLabelEl.value = state.conversationSettings?.label || '';
      if (settingsAliasEl) settingsAliasEl.value = state.conversationSettings?.alias || '';
      if (settingsCommandLinesEl) settingsCommandLinesEl.value = state.conversationSettings?.commandOutputLines || '20';
      if (settingsMarkdownEl) settingsMarkdownEl.checked = state.conversationSettings?.markdown !== false;
      if (settingsXtermEl) settingsXtermEl.checked = state.conversationSettings?.useXterm !== false;
      if (settingsDiffSyntaxEl) settingsDiffSyntaxEl.checked = state.conversationSettings?.diffSyntax === true;
      if (settingsSemanticShellRibbonEl) settingsSemanticShellRibbonEl.checked = state.conversationSettings?.semanticShellRibbon === true;
      if (settingsRolloutEl) settingsRolloutEl.value = state.pendingRollout?.id || state.conversationSettings?.rolloutId || '';
      if (settingsAgentEl) settingsAgentEl.value = state.conversationSettings?.agent || 'codex';
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
    const currentAgent = settingsAgentEl?.value || 'codex';
    await onAgentSelectionChange(currentAgent);
  }

  function closeSettingsModal() {
    if (!settingsModalEl) return;
    const state = getState();
    const agentType = settingsAgentEl?.value?.trim() || 'codex';
    let cwdOk;
    if (agentType === 'codex') {
      cwdOk = Boolean(settingsCwdEl?.value?.trim());
    } else {
      const schemaVals = getWindow()?.CodexAgent?.helpers?.getSchemaValues?.() || {};
      cwdOk = Boolean(schemaVals.cwd?.trim());
    }
    if (!cwdOk) cwdOk = Boolean(state.conversationSettings?.cwd?.trim());
    if (!cwdOk) {
      setActivity('CWD required', true);
      return;
    }
    setState({ pendingNewConversation: false });
    settingsModalEl.classList.add('hidden');
  }

  function openPicker(startPath, mode = 'cwd') {
    if (!pickerOverlayEl) return;
    const nextMode = mode || 'cwd';
    const nextPath = startPath || settingsCwdEl?.value || '~';
    setState({ pickerMode: nextMode, pickerPath: nextPath });
    if (pickerTitleEl) {
      pickerTitleEl.textContent = nextMode === 'mention' ? 'Mentioning' : 'Pick CWD';
    }
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
    const cwdOk = Boolean(settingsCwdEl?.value?.trim());
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

  function renderRolloutList(items) {
    if (!rolloutListEl) return;
    rolloutListEl.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-item';
      empty.textContent = 'No rollouts found';
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
    try {
      const data = await sioCall('get_rollouts', {}, {
        fallbackUrl: '/api/appserver/rollouts',
        fallbackMethod: 'GET',
      });
      let items = Array.isArray(data?.items) ? data.items : [];
      const cwd = settingsCwdEl?.value?.trim();
      if (cwd) {
        items = items.filter((item) => item && item.cwd && String(item.cwd) === cwd);
      }
      renderRolloutList(items);
    } catch (err) {
      console.warn('rollout list failed', err);
      renderRolloutList([]);
    }
  }

  async function loadRolloutPreview(rolloutId) {
    if (!rolloutId) return;
    try {
      const data = await sioCall('get_rollout_preview', { rollout_id: rolloutId }, {
        fallbackUrl: `/api/appserver/rollouts/${encodeURIComponent(rolloutId)}/preview`,
        fallbackMethod: 'GET',
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

  function buildDropdown(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    listEl.innerHTML = '';
    options.forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.textContent = opt;
      btn.addEventListener('click', () => {
        if (inputEl) inputEl.value = opt;
        closeDropdownMenu(listEl);
        if (typeof onChange === 'function') onChange(opt);
      });
      listEl.appendChild(btn);
    });
  }

  function updateDropdownOptions(listEl, options, inputEl, onChange) {
    if (!listEl) return;
    listEl.innerHTML = '';
    const values = Array.from(new Set((options || []).filter(Boolean)));
    buildDropdown(listEl, values, inputEl, onChange);
  }

  async function loadModelOptions() {
    try {
      const data = await sioCall('get_models', {}, {
        fallbackUrl: '/api/appserver/models',
        fallbackMethod: 'GET',
      });
      const items = data?.result?.data || data?.result?.models || data?.data || data?.result || [];
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
      const data = await sioCall('get_extensions', {}, {
        fallbackUrl: '/api/extensions',
        fallbackMethod: 'GET',
      });
      const extensions = data?.extensions || [];
      const agents = ['codex'];
      extensions.forEach((ext) => {
        if (ext?.id && ext?.name) agents.push(ext.id);
      });
      if (agents.length > 1) {
        updateDropdownOptions(settingsAgentOptions, agents, settingsAgentEl, onAgentSelectionChange);
      }
    } catch {
      // ignore
    }
  }

  async function onAgentSelectionChange(agentId) {
    const win = getWindow ? getWindow() : window;
    if (win.CodexAgent?.helpers?.onAgentChange) {
      await win.CodexAgent.helpers.onAgentChange(agentId);
    }
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
      const url = `/api/fs/list?path=${encodeURIComponent(path || '~')}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
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
      const url = `/api/fs/search?query=${encodeURIComponent(query)}&root=${encodeURIComponent(root)}`;
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return [];
      const data = await r.json();
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
