import { createConversationsRpcClient } from './rpc/conversations/client.ts';
import type { JsonObject } from './rpc/conversations/contract.ts';

type RuntimeOptionKind = string;

interface RuntimeOptionChoice {
  value: string;
  label: string;
}

interface RuntimeOptionField extends JsonObject {
  current?: string;
}

interface RuntimeOptionDescriptor {
  settingKey: string;
  label: string;
  footerLabel: string;
  options: RuntimeOptionChoice[];
  current: string;
  default: string;
  accents: Record<string, string>;
}

interface RuntimeOptionsState extends JsonObject {
  quickControls?: unknown[];
  fields?: Record<string, RuntimeOptionField>;
}

interface RuntimeFooterState {
  runtimeOptions?: RuntimeOptionsState;
  openDropdownEl?: HTMLElement | null;
  conversationSettings?: JsonObject;
  activeRuntimeOptionValues?: Record<string, string>;
  conversationMeta?: JsonObject;
}

interface RuntimeFooterContext {
  getState: () => RuntimeFooterState;
  setState: (patch: Partial<RuntimeFooterState>) => void;
  footerRuntimeControlsEl: HTMLElement | null;
  closeDropdownMenu: (element: HTMLElement) => void;
  toggleDropdownMenu: (element: HTMLElement) => void;
  sioCall: (event: string, data?: Record<string, unknown>) => Promise<unknown>;
}

function asObject(value: unknown): JsonObject | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  return value as JsonObject;
}

function isRuntimeOptionChoice(value: RuntimeOptionChoice | null): value is RuntimeOptionChoice {
  return Boolean(value);
}

export function bindRuntimeFooter(ctx: RuntimeFooterContext) {
  const {
    getState,
    setState,
    footerRuntimeControlsEl,
    closeDropdownMenu,
    toggleDropdownMenu,
    sioCall,
  } = ctx;
  const conversationsRpcClient = createConversationsRpcClient({
    windowRef: typeof window !== 'undefined' ? window : null,
  });

  function normalizeApprovalValue(value: string | undefined): string | undefined {
    if (!value) return value;
    if (value === 'unlessTrusted') return 'untrusted';
    return value;
  }

  function normalizeRuntimeOptionDescriptor(kind: RuntimeOptionKind): RuntimeOptionDescriptor | null {
    const { runtimeOptions } = getState();
    const raw = runtimeOptions?.[kind];
    const rawDescriptor = asObject(raw);
    if (!rawDescriptor) return null;
    const settingKey = typeof rawDescriptor.settingKey === 'string' ? rawDescriptor.settingKey.trim() : '';
    const options = Array.isArray(rawDescriptor.options)
      ? rawDescriptor.options
          .map((item: unknown) => {
            if (typeof item === 'string') {
              const text = item.trim();
              return text ? { value: text, label: text } : null;
            }
            const choice = asObject(item);
            if (!choice) return null;
            const value = typeof choice.value === 'string' ? choice.value.trim() : '';
            if (!value) return null;
            const label = typeof choice.label === 'string' && choice.label.trim() ? choice.label.trim() : value;
            return { value, label };
          })
          .filter(isRuntimeOptionChoice)
      : [];
    const rawAccents = asObject(rawDescriptor.accents);
    const accents = rawAccents
      ? Object.fromEntries(
          Object.entries(rawAccents)
            .filter(([, accent]) => typeof accent === 'string'),
        ) as Record<string, string>
      : {};
    return {
      settingKey,
      label: typeof rawDescriptor.label === 'string' ? rawDescriptor.label.trim() : '',
      footerLabel: typeof rawDescriptor.footerLabel === 'string' ? rawDescriptor.footerLabel.trim() : '',
      options,
      current: typeof rawDescriptor.current === 'string' ? rawDescriptor.current.trim() : '',
      default: typeof rawDescriptor.default === 'string' ? rawDescriptor.default.trim() : '',
      accents,
    };
  }

  function getQuickControlKinds(): string[] {
    const { runtimeOptions } = getState();
    const configured = Array.isArray(runtimeOptions?.quickControls)
      ? runtimeOptions.quickControls
          .map((item: unknown) => (typeof item === 'string' ? item.trim() : ''))
          .filter(Boolean)
      : [];
    if (configured.length) return configured;
    return normalizeRuntimeOptionDescriptor('approval') ? ['approval'] : [];
  }

  function getFooterSlotKinds(): string[] {
    const configured = new Set(getQuickControlKinds());
    const kinds: string[] = [];
    const approvalDescriptor = normalizeRuntimeOptionDescriptor('approval');
    if (configured.has('approval') || approvalDescriptor?.options?.length) {
      kinds.push('approval');
    }
    kinds.push('mode');
    return kinds;
  }

  function getFooterRuntimeLabel(kind: RuntimeOptionKind, descriptor: RuntimeOptionDescriptor | null): string {
    if (kind === 'mode') {
      return descriptor?.label || descriptor?.footerLabel || 'Mode';
    }
    return descriptor?.footerLabel || descriptor?.label || kind;
  }

  function getRuntimeSettingKey(kind: RuntimeOptionKind, fallbackKey: string): string {
    return normalizeRuntimeOptionDescriptor(kind)?.settingKey || fallbackKey;
  }

  function getConversationSettingByRuntimeKey(kind: RuntimeOptionKind, fallbackKey: string): string {
    const { conversationSettings } = getState();
    const key = getRuntimeSettingKey(kind, fallbackKey);
    if (!key || !conversationSettings || typeof conversationSettings !== 'object') return '';
    const value = conversationSettings[key];
    return typeof value === 'string' ? value : '';
  }

  function getRuntimeOptionLabel(kind: RuntimeOptionKind, value: string): string {
    if (!value) return '';
    const descriptor = normalizeRuntimeOptionDescriptor(kind);
      const match = descriptor?.options?.find((option: RuntimeOptionChoice) => option.value === value);
    return match?.label || value;
  }

  function getRuntimeQuickValue(kind: RuntimeOptionKind, fallbackKey: string): string {
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

  function renderFooterRuntimeControls(): void {
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
    kinds.forEach((kind: string) => {
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

  function updateRuntimeOptionsCurrent(
    runtimeOptions: RuntimeOptionsState | undefined,
    kind: RuntimeOptionKind,
    settingKey: string,
    nextValue: string,
  ): RuntimeOptionsState | undefined {
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

  async function saveRuntimeOptionQuick(
    kind: RuntimeOptionKind,
    value: string | undefined,
    fallbackKey: string,
  ): Promise<void> {
    const state = getState();
    let nextValue = value?.trim();
    if (kind === 'approval') {
      nextValue = normalizeApprovalValue(nextValue);
    }
    if (!nextValue) return;
    const settingKey = getRuntimeSettingKey(kind, fallbackKey || kind);
    const conversationId = state.conversationMeta?.conversation_id;
    const metaSettings = asObject(state.conversationMeta?.settings);
    const agentId = typeof state.runtimeOptions?.agent === 'string' && state.runtimeOptions.agent.trim()
      ? state.runtimeOptions.agent.trim()
      : (typeof state.conversationSettings?.agent === 'string' && state.conversationSettings.agent.trim()
        ? state.conversationSettings.agent.trim()
        : (typeof metaSettings?.agent === 'string' && metaSettings.agent.trim()
          ? metaSettings.agent.trim()
          : ''));
    await conversationsRpcClient.updateConversation({
      conversationId: typeof conversationId === 'string' ? conversationId : null,
      settings: { [settingKey]: nextValue },
    });
    let nextRuntimeOptions = updateRuntimeOptionsCurrent(state.runtimeOptions, kind, settingKey, nextValue);
    let persistedSettingValue = nextValue;
    try {
      const refreshed = await sioCall('get_runtime_options', {
        conversation_id: conversationId,
        agent: agentId || null,
      });
      const refreshedPayload = asObject(refreshed);
      if (refreshedPayload) {
        nextRuntimeOptions = refreshedPayload as RuntimeOptionsState;
        const refreshedDescriptor = asObject(refreshedPayload[kind]);
        if (refreshedDescriptor && typeof refreshedDescriptor.current === 'string') {
          persistedSettingValue = refreshedDescriptor.current.trim() || persistedSettingValue;
        }
      }
    } catch {
      // Keep the optimistic local state if the authoritative refresh fails.
    }
    setState({
      conversationSettings: {
        ...(state.conversationSettings || {}),
        [settingKey]: persistedSettingValue,
      },
      runtimeOptions: nextRuntimeOptions,
    });
    renderFooterRuntimeControls();
  }

  async function saveApprovalQuick(value: string | undefined): Promise<void> {
    await saveRuntimeOptionQuick('approval', value, 'approvalPolicy');
  }

  function applyRuntimeMode(kind: string): void {
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

  function resetRuntimeFooterState(): void {
    const { openDropdownEl } = getState();
    if (openDropdownEl && footerRuntimeControlsEl?.contains(openDropdownEl)) {
      closeDropdownMenu(openDropdownEl);
    }
    setState({
      runtimeOptions: {},
      activeRuntimeOptionValues: {},
      openDropdownEl: null,
    });
    renderFooterRuntimeControls();
  }

  return {
    normalizeApprovalValue,
    renderFooterRuntimeControls,
    saveApprovalQuick,
    applyRuntimeMode,
    resetRuntimeFooterState,
  };
}
