export function bindRuntimeFooter(ctx) {
  const {
    getState,
    setState,
    footerRuntimeControlsEl,
    closeDropdownMenu,
    toggleDropdownMenu,
    sioCall,
  } = ctx;

  function normalizeApprovalValue(value) {
    if (!value) return value;
    if (value === 'unlessTrusted') return 'untrusted';
    return value;
  }

  function normalizeRuntimeOptionDescriptor(kind) {
    const { runtimeOptions } = getState();
    const raw = runtimeOptions?.[kind];
    if (!raw || typeof raw !== 'object') return null;
    const settingKey = typeof raw.settingKey === 'string' ? raw.settingKey.trim() : '';
    const options = Array.isArray(raw.options)
      ? raw.options
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
      label: typeof raw.label === 'string' ? raw.label.trim() : '',
      footerLabel: typeof raw.footerLabel === 'string' ? raw.footerLabel.trim() : '',
      options,
      current: typeof raw.current === 'string' ? raw.current.trim() : '',
      default: typeof raw.default === 'string' ? raw.default.trim() : '',
      accents: raw.accents && typeof raw.accents === 'object' ? { ...raw.accents } : {},
    };
  }

  function getQuickControlKinds() {
    const { runtimeOptions } = getState();
    const configured = Array.isArray(runtimeOptions?.quickControls)
      ? runtimeOptions.quickControls
          .map((item) => (typeof item === 'string' ? item.trim() : ''))
          .filter(Boolean)
      : [];
    if (configured.length) return configured;
    return normalizeRuntimeOptionDescriptor('approval') ? ['approval'] : [];
  }

  function getFooterSlotKinds() {
    const configured = new Set(getQuickControlKinds());
    const kinds = [];
    const approvalDescriptor = normalizeRuntimeOptionDescriptor('approval');
    if (configured.has('approval') || approvalDescriptor?.options?.length) {
      kinds.push('approval');
    }
    kinds.push('mode');
    return kinds;
  }

  function getFooterRuntimeLabel(kind, descriptor) {
    if (kind === 'mode') {
      return descriptor?.label || descriptor?.footerLabel || 'Mode';
    }
    return descriptor?.footerLabel || descriptor?.label || kind;
  }

  function getRuntimeSettingKey(kind, fallbackKey) {
    return normalizeRuntimeOptionDescriptor(kind)?.settingKey || fallbackKey;
  }

  function getConversationSettingByRuntimeKey(kind, fallbackKey) {
    const { conversationSettings } = getState();
    const key = getRuntimeSettingKey(kind, fallbackKey);
    if (!key || !conversationSettings || typeof conversationSettings !== 'object') return '';
    const value = conversationSettings[key];
    return typeof value === 'string' ? value : '';
  }

  function getRuntimeOptionLabel(kind, value) {
    if (!value) return '';
    const descriptor = normalizeRuntimeOptionDescriptor(kind);
    const match = descriptor?.options?.find((option) => option.value === value);
    return match?.label || value;
  }

  function getRuntimeQuickValue(kind, fallbackKey) {
    const { activeRuntimeOptionValues } = getState();
    const activeValue = activeRuntimeOptionValues?.[kind];
    if (typeof activeValue === 'string' && activeValue.trim()) {
      return activeValue.trim();
    }
    const descriptor = normalizeRuntimeOptionDescriptor(kind);
    return getConversationSettingByRuntimeKey(kind, fallbackKey)
      || descriptor?.current
      || descriptor?.default
      || '';
  }

  function renderFooterRuntimeControls() {
    if (!footerRuntimeControlsEl) return;
    const { runtimeOptions, openDropdownEl } = getState();
    if (openDropdownEl && footerRuntimeControlsEl.contains(openDropdownEl)) {
      closeDropdownMenu(openDropdownEl);
    }
    footerRuntimeControlsEl.innerHTML = '';
    const hasRuntimeOptions = runtimeOptions && Object.keys(runtimeOptions).length > 0;
    if (!hasRuntimeOptions) {
      footerRuntimeControlsEl.style.display = 'none';
      return;
    }
    const kinds = getFooterSlotKinds();
    footerRuntimeControlsEl.style.display = kinds.length ? '' : 'none';
    kinds.forEach((kind) => {
      const descriptor = normalizeRuntimeOptionDescriptor(kind);
      if (!descriptor || !descriptor.options.length) {
        if (kind === 'mode') {
          const placeholder = document.createElement('div');
          placeholder.className = 'status-pill footer-cell footer-runtime-cell footer-runtime-empty';
          placeholder.dataset.runtimeKind = kind;
          placeholder.setAttribute('aria-hidden', 'true');
          footerRuntimeControlsEl.appendChild(placeholder);
        }
        return;
      }
      const fallbackKey = descriptor.settingKey || kind;
      const currentValue = getRuntimeQuickValue(kind, fallbackKey);
      const cell = document.createElement('div');
      cell.className = 'status-pill footer-cell footer-runtime-cell';
      cell.dataset.runtimeKind = kind;

      const labelEl = document.createElement('span');
      labelEl.textContent = getFooterRuntimeLabel(kind, descriptor);
      cell.appendChild(labelEl);

      const dropdownEl = document.createElement('div');
      dropdownEl.className = 'footer-dropdown';

      const valueBtn = document.createElement('button');
      valueBtn.type = 'button';
      valueBtn.className = 'pill dropdown-toggle footer-runtime-toggle';
      valueBtn.dataset.runtimeKind = kind;
      const accentClass = typeof descriptor.accents?.[currentValue] === 'string'
        ? descriptor.accents[currentValue]
        : '';
      valueBtn.classList.toggle('ok', accentClass === 'ok');
      valueBtn.classList.toggle('warn', accentClass === 'warn');
      valueBtn.classList.toggle('err', accentClass === 'err');
      valueBtn.textContent = getRuntimeOptionLabel(kind, currentValue) || currentValue || 'default';
      valueBtn.addEventListener('click', (evt) => {
        evt.preventDefault();
        toggleDropdownMenu(optionsEl);
      });
      dropdownEl.appendChild(valueBtn);

      const optionsEl = document.createElement('div');
      optionsEl.className = 'dropdown-list';
      descriptor.options.forEach((option) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'dropdown-item';
        btn.dataset.value = option.value;
        btn.textContent = option.label;
        btn.addEventListener('click', async () => {
          closeDropdownMenu(optionsEl);
          await saveRuntimeOptionQuick(kind, option.value, fallbackKey);
        });
        optionsEl.appendChild(btn);
      });
      dropdownEl.appendChild(optionsEl);
      cell.appendChild(dropdownEl);
      footerRuntimeControlsEl.appendChild(cell);
    });
  }

  function updateRuntimeOptionsCurrent(runtimeOptions, kind, settingKey, nextValue) {
    let nextRuntimeOptions = runtimeOptions;
    if (nextRuntimeOptions?.[kind] && typeof nextRuntimeOptions[kind] === 'object') {
      nextRuntimeOptions = {
        ...nextRuntimeOptions,
        [kind]: {
          ...nextRuntimeOptions[kind],
          current: nextValue,
        },
      };
    }
    if (nextRuntimeOptions?.fields && typeof nextRuntimeOptions.fields === 'object' && nextRuntimeOptions.fields[settingKey]) {
      nextRuntimeOptions = {
        ...nextRuntimeOptions,
        fields: {
          ...nextRuntimeOptions.fields,
          [settingKey]: {
            ...nextRuntimeOptions.fields[settingKey],
            current: nextValue,
          },
        },
      };
    }
    return nextRuntimeOptions;
  }

  async function saveRuntimeOptionQuick(kind, value, fallbackKey) {
    const state = getState();
    let nextValue = value?.trim();
    if (kind === 'approval') {
      nextValue = normalizeApprovalValue(nextValue);
    }
    if (!nextValue) return;
    const settingKey = getRuntimeSettingKey(kind, fallbackKey || kind);
    await sioCall('conversation_update', {
      conversation_id: state.conversationMeta?.conversation_id,
      settings: { [settingKey]: nextValue },
    });
    setState({
      conversationSettings: {
        ...(state.conversationSettings || {}),
        [settingKey]: nextValue,
      },
      runtimeOptions: updateRuntimeOptionsCurrent(state.runtimeOptions, kind, settingKey, nextValue),
    });
    renderFooterRuntimeControls();
  }

  async function saveApprovalQuick(value) {
    await saveRuntimeOptionQuick('approval', value, 'approvalPolicy');
  }

  function applyRuntimeMode(kind) {
    const { activeRuntimeOptionValues } = getState();
    const normalizedKind = typeof kind === 'string' ? kind.trim() : '';
    if (normalizedKind) {
      setState({
        activeRuntimeOptionValues: {
          ...(activeRuntimeOptionValues || {}),
          mode: normalizedKind,
        },
      });
    } else if (activeRuntimeOptionValues?.mode) {
      const next = { ...(activeRuntimeOptionValues || {}) };
      delete next.mode;
      setState({ activeRuntimeOptionValues: next });
    }
    renderFooterRuntimeControls();
  }

  return {
    normalizeApprovalValue,
    renderFooterRuntimeControls,
    saveApprovalQuick,
    applyRuntimeMode,
  };
}
