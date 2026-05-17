/**
 * Settings Schema Module
 * 
 * Handles dynamic rendering of extension settings based on JSON schemas.
 * Each extension can define a settings_schema.json with field definitions.
 */
type JsonRecord = Record<string, unknown>;
type JsonValue = unknown;
type SchemaInput = HTMLInputElement | HTMLTextAreaElement;

type CodexAgentModuleApi = {
  helpers: Record<string, unknown>;
  state: Record<string, unknown>;
};

type SettingsRpcClient = {
  getExtensionSettingsSchema: (extensionId: string) => Promise<unknown>;
  listExtensionSessions: (options: { extensionId: string; cwd: string | null; extraParams?: JsonRecord | null }) => Promise<unknown>;
  listExtensionModels: (options: { extensionId: string }) => Promise<unknown>;
  getRuntimeOptions: (options: { conversationId: string | null; agent: string | null }) => Promise<unknown>;
  getExtensionSessionState?: (options: {
    extensionId: string;
    conversationId: string;
    providerSessionId?: string | null;
  }) => Promise<unknown>;
  unloadExtensionSession?: (options: {
    extensionId: string;
    conversationId: string;
    providerSessionId?: string | null;
  }) => Promise<unknown>;
};

type SchemaField = {
  id: string;
  type?: string;
  label?: string;
  description?: string;
  detail?: string;
  text?: string;
  tone?: string;
  placeholder?: string;
  default?: JsonValue;
  options?: unknown[];
  dynamic_source?: string;
  dynamic_options_key?: string;
  dynamic_options_from?: JsonRecord;
  value_keys?: unknown[];
  model_gate?: JsonRecord;
  source?: string;
  picker_sort?: JsonRecord;
  browse?: boolean;
  min?: number;
  max?: number;
  rows?: number;
  json_kind?: string;
};

type SettingsSchema = {
  fields?: SchemaField[];
  cache?: string;
  useBuiltin?: boolean;
};

type SessionPickerTarget = {
  input: HTMLInputElement;
  field: SchemaField;
};

type SchemaValueEntry = {
  input: SchemaInput;
  type?: string;
  field: SchemaField;
};

type SelectOption = {
  value: string;
  label: string;
};

type ModelVersion = {
  family: string;
  major: number;
  minor: number;
};

type DynamicSelectOptions = {
  items: JsonRecord[];
  options: SelectOption[];
  current: string;
  defaultValue: string;
};

type SelectControl = {
  field: SchemaField;
  input: HTMLInputElement;
  listDiv: HTMLDivElement;
  initialValue: string;
  initialValueApplied: boolean;
  dynamicItems: JsonRecord[];
};

type DynamicSourceOptions = {
  cwd?: string | null;
  conversationId?: string | null;
  agent?: string | null;
  extraParams?: JsonRecord | null;
};

type ConversationMeta = {
  conversation_id?: unknown;
  extension_id?: unknown;
  agent_type?: unknown;
  thread_id?: unknown;
  provider_session_id?: unknown;
  status?: unknown;
};

type HostUiState = {
  ideMode?: unknown;
  projectRoot?: unknown;
};

type CodexAgentState = {
  pendingNewConversation?: unknown;
  conversationMeta?: ConversationMeta | null;
  conversationSettings?: JsonRecord | null;
  hostUi?: HostUiState | null;
  homePrefix?: unknown;
  splashTab?: unknown;
};

declare global {
  interface Window {
    CodexAgentModules?: Array<(ctx: CodexAgentModuleApi | undefined) => void>;
    CodexAgent?: CodexAgentModuleApi;
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function asRecord(value: unknown): JsonRecord {
  return isRecord(value) ? value : {};
}

function asCodexAgentState(value: unknown): CodexAgentState {
  return isRecord(value) ? value as CodexAgentState : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function trimString(value: unknown): string {
  return stringValue(value).trim();
}

function normalizeSelectOption(value: unknown): SelectOption | null {
  if (typeof value === 'string') {
    const normalized = value.trim();
    return normalized ? { value: normalized, label: normalized } : null;
  }
  const item = asRecord(value);
  const optionValue = trimString(item.value || item.id);
  if (!optionValue) return null;
  return {
    value: optionValue,
    label: trimString(item.label || item.name || optionValue) || optionValue,
  };
}

function boolValue(value: unknown): boolean {
  return value === true;
}

function schemaFieldId(field: SchemaField): string {
  return trimString(field.id);
}

function normalizeSchemaField(value: unknown): SchemaField | null {
  if (!isRecord(value)) return null;
  const id = trimString(value.id);
  if (!id) return null;
  return {
    ...value,
    id,
    type: trimString(value.type) || undefined,
    label: typeof value.label === 'string' ? value.label : undefined,
    description: typeof value.description === 'string' ? value.description : undefined,
    detail: typeof value.detail === 'string' ? value.detail : undefined,
    text: typeof value.text === 'string' ? value.text : undefined,
    tone: typeof value.tone === 'string' ? value.tone : undefined,
    placeholder: typeof value.placeholder === 'string' ? value.placeholder : undefined,
    dynamic_source: typeof value.dynamic_source === 'string' ? value.dynamic_source : undefined,
    dynamic_options_key: typeof value.dynamic_options_key === 'string' ? value.dynamic_options_key : undefined,
    dynamic_options_from: isRecord(value.dynamic_options_from) ? value.dynamic_options_from : undefined,
    source: typeof value.source === 'string' ? value.source : undefined,
    picker_sort: isRecord(value.picker_sort) ? value.picker_sort : undefined,
    browse: value.browse === true,
    min: typeof value.min === 'number' ? value.min : undefined,
    max: typeof value.max === 'number' ? value.max : undefined,
    rows: typeof value.rows === 'number' ? value.rows : undefined,
    json_kind: typeof value.json_kind === 'string' ? value.json_kind : undefined,
  };
}

function normalizeSchema(value: unknown): SettingsSchema | null {
  if (!isRecord(value)) return null;
  const fields = Array.isArray(value.fields)
    ? value.fields.map(normalizeSchemaField).filter((field): field is SchemaField => Boolean(field))
    : [];
  return {
    ...value,
    fields,
    cache: typeof value.cache === 'string' ? value.cache : undefined,
    useBuiltin: value.useBuiltin === true,
  };
}

function getHelper(ctx: CodexAgentModuleApi | undefined, helperName: string): unknown {
  return ctx?.helpers?.[helperName];
}

function getCodexAgentState(): CodexAgentState {
  return asCodexAgentState(window.CodexAgent?.state);
}

function callCtxHelper(ctx: CodexAgentModuleApi | undefined, helperName: string, ...args: unknown[]): unknown {
  const helper = getHelper(ctx, helperName);
  if (typeof helper === 'function') {
    return helper(...args);
  }
  return undefined;
}

function formatJsonSetting(ctx: CodexAgentModuleApi | undefined, value: unknown): string {
  const result = callCtxHelper(ctx, 'formatJsonSetting', value);
  return typeof result === 'string' ? result : JSON.stringify(value, null, 2);
}

function parseJsonSetting(ctx: CodexAgentModuleApi | undefined, value: string, label: string): unknown {
  const helper = getHelper(ctx, 'parseJsonSetting');
  if (typeof helper === 'function') {
    return helper(value, label);
  }
  return JSON.parse(value || 'null');
}

function normalizeStaticOptions(options: unknown[] | undefined): SelectOption[] {
  if (!Array.isArray(options)) return [];
  return options.map((item: unknown): SelectOption | null => {
    if (typeof item === 'string') return { value: item, label: item };
    const itemMap = asRecord(item);
    const value = trimString(itemMap.value || itemMap.id);
    if (!value) return null;
    return { value, label: trimString(itemMap.label || itemMap.name || itemMap.value || itemMap.id) || value };
  }).filter((option): option is SelectOption => Boolean(option));
}

window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx: CodexAgentModuleApi | undefined) => {
  if (!ctx) return;
  const settingsCodexFields = document.getElementById('settings-codex-fields');
  const settingsExtensionFields = document.getElementById('settings-extension-fields');
  const settingsAgentEl = document.getElementById('settings-agent') as HTMLInputElement | null;
   
  // Cache for loaded schemas
  const schemaCache: Record<string, SettingsSchema> = {};
   
  // Current schema field values (for save)
  let currentSchemaValues: Record<string, SchemaValueEntry> = {};
   
  // Session picker overlay elements (reuse HTML already in template)
  const sessionPickerOverlay = document.getElementById('session-picker');
  const sessionPickerCloseBtn = document.getElementById('session-picker-close');
  const sessionPickerSortEl = document.getElementById('session-picker-sort');
  const sessionPickerListEl = document.getElementById('session-picker-list');
   
  // Track which input field the session picker is serving
  let _sessionPickerTarget: SessionPickerTarget | null = null;
  let _sessionPickerSortValue = '';
  let _sessionPickerFilterEnabled = false;
  let _sessionPickerItems: JsonRecord[] = [];

  function requireSettingsRpc(): SettingsRpcClient {
    const client = getHelper(ctx, 'settingsRpc');
    if (!client || typeof client !== 'object') {
      throw new Error('Settings RPC helper unavailable');
    }
    return client as SettingsRpcClient;
  }

  function dynamicSourceErrorMessage(err: unknown): string {
    if (typeof err === 'string' && err) return err;
    if (isRecord(err)) {
      if (typeof err.message === 'string' && err.message) return err.message;
      if (typeof err.error === 'string' && err.error) return err.error;
      const nestedError = err.error;
      if (isRecord(nestedError) && typeof nestedError.message === 'string' && nestedError.message) {
        return nestedError.message;
      }
    }
    return 'Dynamic source request failed';
  }

  function unwrapDynamicSourceResult(result: unknown): unknown {
    if (!isRecord(result)) return result;
    if (result.ok === false) {
      throw new Error(dynamicSourceErrorMessage(result));
    }
    if (result.ok === true && Object.prototype.hasOwnProperty.call(result, 'data')) {
      return result.data;
    }
    return result;
  }

  function sourcePathname(sourceUrl: unknown): string {
    if (typeof sourceUrl !== 'string') return '';
    const raw = sourceUrl.trim();
    if (!raw) return '';
    try {
      return new URL(raw, window.location.origin).pathname || '';
    } catch {
      return raw.split(/[?#]/, 1)[0] || '';
    }
  }

  function isRuntimeOptionsSource(sourceUrl: unknown): boolean {
    const pathname = sourcePathname(sourceUrl).replace(/\/+$/, '');
    return /(?:^|\/)api\/appserver\/runtime_options$/.test(pathname);
  }

  function extensionIdFromApiPath(sourceUrl: unknown, suffix: string): string {
    const pathname = sourcePathname(sourceUrl).replace(/\/+$/, '');
    const escapedSuffix = String(suffix || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = pathname
      ? pathname.match(new RegExp(`(?:^|/)api/extensions/([^/]+)/${escapedSuffix}$`))
      : null;
    return match?.[1] ? decodeURIComponent(match[1]) : '';
  }

  function sessionPickerSortConfig(field: SchemaField | null | undefined): JsonRecord {
    return asRecord(field?.picker_sort);
  }

  function sessionPickerSortOptions(field: SchemaField | null | undefined): SelectOption[] {
    const rawOptions = sessionPickerSortConfig(field).options;
    if (!Array.isArray(rawOptions)) return [];
    return rawOptions
      .map((item) => normalizeSelectOption(item))
      .filter((item): item is SelectOption => Boolean(item));
  }

  function sessionPickerSortDefault(field: SchemaField | null | undefined): string {
    const configured = trimString(sessionPickerSortConfig(field).default);
    if (configured) return configured;
    return sessionPickerSortOptions(field)[0]?.value || '';
  }

  function sessionPickerSortParam(field: SchemaField | null | undefined): string {
    return trimString(sessionPickerSortConfig(field).param) || 'sort';
  }

  function getSessionPickerActiveCwd(): string {
    const schemaCwd = currentSchemaValues.cwd?.input?.value;
    if (typeof schemaCwd === 'string' && schemaCwd.trim()) return schemaCwd.trim();
    const stateCwd = getCodexAgentState().conversationSettings?.cwd;
    return trimString(stateCwd);
  }

  function normalizePickerPath(value: string): string {
    const normalized = value.replace(/\/+/g, '/').replace(/\/+$/, '');
    return normalized || '/';
  }

  function sessionPickerHomePrefix(): string {
    return trimString(getCodexAgentState().homePrefix);
  }

  function sessionPickerRelativeHomePath(pathValue: unknown): string {
    const rawPath = trimString(pathValue);
    if (!rawPath) return '—';
    const homePrefix = sessionPickerHomePrefix();
    if (!homePrefix) return rawPath;
    const normalizedPath = normalizePickerPath(rawPath);
    const normalizedHome = normalizePickerPath(homePrefix);
    if (normalizedPath === normalizedHome) return '~';
    if (normalizedPath.startsWith(`${normalizedHome}/`)) {
      return `~/${normalizedPath.slice(normalizedHome.length + 1)}`;
    }
    return rawPath;
  }

  function sessionPickerMatchesCwdScope(pathValue: unknown, cwdValue: unknown): boolean {
    const sessionPath = trimString(pathValue);
    const selectedCwd = trimString(cwdValue);
    if (!selectedCwd) return true;
    if (!sessionPath) return false;
    const normalizedSession = normalizePickerPath(sessionPath);
    const normalizedCwd = normalizePickerPath(selectedCwd);
    return normalizedSession === normalizedCwd
      || normalizedSession.startsWith(`${normalizedCwd}/`)
      || normalizedCwd.startsWith(`${normalizedSession}/`);
  }

  function sessionPickerFormatTimestamp(value: unknown): string {
    const raw = trimString(value);
    if (!raw) return '—';
    const parsed = new Date(raw);
    if (!Number.isNaN(parsed.getTime())) {
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: 'short',
        timeStyle: 'short',
      }).format(parsed);
    }
    return raw;
  }

  function sessionPickerShortId(value: unknown): string {
    const sid = trimString(value);
    if (!sid) return '—';
    return sid.length > 12 ? `${sid.slice(0, 8)}…` : sid;
  }

  function sessionPickerSummary(itemMap: JsonRecord, metadata: JsonRecord): string {
    return trimString(
      itemMap.summary
        || itemMap.preview
        || metadata.summary
        || metadata.preview
        || itemMap.label
        || metadata.label
        || itemMap.title
        || itemMap.name,
    );
  }

  function sessionPickerCwd(itemMap: JsonRecord, metadata: JsonRecord): string {
    const context = asRecord(itemMap.context);
    const metadataContext = asRecord(metadata.context);
    return trimString(
      itemMap.cwd
        || metadata.cwd
        || context.cwd
        || metadataContext.cwd,
    );
  }

  function sessionPickerUpdatedAt(itemMap: JsonRecord, metadata: JsonRecord): string {
    return trimString(
      itemMap.updated_at
        || itemMap.updatedAt
        || itemMap.modifiedTime
        || itemMap.modified_time
        || metadata.updated_at
        || metadata.updatedAt
        || metadata.modifiedTime
        || metadata.modified_time,
    );
  }

  function sessionPickerCreatedAt(itemMap: JsonRecord, metadata: JsonRecord): string {
    return trimString(
      itemMap.created_at
        || itemMap.createdAt
        || itemMap.start_time
        || itemMap.startTime
        || metadata.created_at
        || metadata.createdAt
        || metadata.start_time
        || metadata.startTime,
    );
  }

  function renderSessionPickerSort(field: SchemaField | null | undefined): void {
    if (!sessionPickerSortEl) return;
    sessionPickerSortEl.innerHTML = '';
    const options = sessionPickerSortOptions(field);
    const activeCwd = getSessionPickerActiveCwd();
    const sortGroup = document.createElement('div');
    sortGroup.className = 'picker-sort-group';
    if (options.length) {
      const label = document.createElement('span');
      label.className = 'picker-sort-label';
      label.textContent = 'Order';
      sortGroup.appendChild(label);

      options.forEach((option) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost picker-sort-btn';
        button.dataset.selected = option.value === _sessionPickerSortValue ? 'true' : 'false';
        button.textContent = option.label;
        button.addEventListener('click', () => {
          if (!_sessionPickerTarget || option.value === _sessionPickerSortValue) return;
          _sessionPickerSortValue = option.value;
          renderSessionPickerSort(_sessionPickerTarget.field);
          void fetchAndRenderSessions(_sessionPickerTarget.field.source || '', _sessionPickerTarget.field);
        });
        sortGroup.appendChild(button);
      });
    }

    const filterGroup = document.createElement('div');
    filterGroup.className = 'picker-filter-group';
    if (activeCwd) {
      const filterButton = document.createElement('button');
      filterButton.type = 'button';
      filterButton.className = 'btn ghost picker-sort-btn picker-filter-btn';
      filterButton.dataset.selected = _sessionPickerFilterEnabled ? 'true' : 'false';
      filterButton.textContent = sessionPickerRelativeHomePath(activeCwd);
      filterButton.title = activeCwd;
      filterButton.addEventListener('click', () => {
        _sessionPickerFilterEnabled = !_sessionPickerFilterEnabled;
        renderSessionPickerSort(_sessionPickerTarget?.field);
        renderSessionList(_sessionPickerItems);
      });
      filterGroup.appendChild(filterButton);
    }

    sessionPickerSortEl.hidden = options.length === 0 && !activeCwd;
    if (sessionPickerSortEl.hidden) return;
    if (options.length) sessionPickerSortEl.appendChild(sortGroup);
    if (activeCwd) sessionPickerSortEl.appendChild(filterGroup);
  }

  async function fetchDynamicSource(sourceUrl: unknown, options: DynamicSourceOptions = {}): Promise<unknown> {
    const settingsRpc = requireSettingsRpc();
    const request = async (): Promise<unknown> => {
      const extensionIdForSessions = extensionIdFromApiPath(sourceUrl, 'sessions');
      if (extensionIdForSessions) {
        return unwrapDynamicSourceResult(await settingsRpc.listExtensionSessions({
          extensionId: extensionIdForSessions,
          cwd: options.cwd || null,
          extraParams: options.extraParams || null,
        }));
      }
      const extensionIdForModels = extensionIdFromApiPath(sourceUrl, 'models');
      if (extensionIdForModels) {
        return unwrapDynamicSourceResult(await settingsRpc.listExtensionModels({ extensionId: extensionIdForModels }));
      }
      if (isRuntimeOptionsSource(sourceUrl)) {
        return unwrapDynamicSourceResult(await settingsRpc.getRuntimeOptions({
          conversationId: options.conversationId || null,
          agent: options.agent || null,
        }));
      }
      throw new Error(`Unsupported Socket.IO schema source: ${sourceUrl || '(empty)'}`);
    };

    return await request();
  }
  
  function openSessionPicker(field: SchemaField, input: HTMLInputElement): void {
    if (!sessionPickerOverlay) return;
    _sessionPickerTarget = { input, field };
    _sessionPickerSortValue = sessionPickerSortDefault(field);
    _sessionPickerFilterEnabled = Boolean(getSessionPickerActiveCwd());
    _sessionPickerItems = [];
    renderSessionPickerSort(field);
    sessionPickerOverlay.classList.remove('hidden');
    fetchAndRenderSessions(field.source || '', field);
  }
  
  function closeSessionPicker(): void {
    if (!sessionPickerOverlay) return;
    sessionPickerOverlay.classList.add('hidden');
    _sessionPickerTarget = null;
    _sessionPickerSortValue = '';
    _sessionPickerFilterEnabled = false;
    _sessionPickerItems = [];
    if (sessionPickerSortEl) {
      sessionPickerSortEl.hidden = true;
      sessionPickerSortEl.innerHTML = '';
    }
  }
  
  async function fetchAndRenderSessions(sourceUrl: string, field: SchemaField | null = null): Promise<void> {
    if (!sessionPickerListEl) return;
    sessionPickerListEl.innerHTML = '<div class="picker-item">Loading…</div>';
    try {
      const extraParams: JsonRecord = {};
      const sortParam = sessionPickerSortParam(field);
      if (sortParam && _sessionPickerSortValue) {
        extraParams[sortParam] = _sessionPickerSortValue;
      }
      const data = await fetchDynamicSource(sourceUrl, {
        cwd: getSessionPickerActiveCwd() || null,
        extraParams: Object.keys(extraParams).length ? extraParams : null,
      });
      const dataMap = asRecord(data);
      const items = Array.isArray(dataMap.sessions) ? dataMap.sessions
        : Array.isArray(data) ? data : [];
      _sessionPickerItems = items.map((item) => asRecord(item)).filter((item) => Object.keys(item).length > 0);
      renderSessionPickerSort(field);
      renderSessionList(_sessionPickerItems);
    } catch (err) {
      console.warn('[schema] session list failed', err);
      _sessionPickerItems = [];
      renderSessionList([], dynamicSourceErrorMessage(err));
    }
  }
  
  function renderSessionList(items: unknown[], errorMessage = ''): void {
    if (!sessionPickerListEl) return;
    sessionPickerListEl.innerHTML = '';
    const selectedCwd = getSessionPickerActiveCwd();
    const renderedItems = items
      .map((item) => asRecord(item))
      .filter((item) => {
        if (!_sessionPickerFilterEnabled || !selectedCwd) return true;
        return sessionPickerMatchesCwdScope(sessionPickerCwd(item, asRecord(item.metadata)), selectedCwd);
      });
    if (!renderedItems.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-item';
      empty.textContent = errorMessage
        ? 'Session list unavailable'
        : (_sessionPickerFilterEnabled && selectedCwd ? 'No sessions match selected CWD' : 'No sessions found');
      if (errorMessage) empty.title = errorMessage;
      sessionPickerListEl.appendChild(empty);
      return;
    }
    renderedItems.forEach((item: unknown) => {
      const itemMap = asRecord(item);
      const metadata = asRecord(itemMap.metadata);
      const sid = trimString(itemMap.sessionId || itemMap.session_id || itemMap.id);
      const summary = sessionPickerSummary(itemMap, metadata);
      const modified = sessionPickerUpdatedAt(itemMap, metadata);
      const created = sessionPickerCreatedAt(itemMap, metadata);
      const cwd = sessionPickerCwd(itemMap, metadata);

      const row = document.createElement('div');
      row.className = 'picker-item session-item';
      row.dataset.sessionId = sid;
      row.style.cursor = 'pointer';
      if (_sessionPickerTarget) {
        const selectedId = trimString(_sessionPickerTarget.input.dataset.sessionId || _sessionPickerTarget.input.value);
        if (selectedId && selectedId === sid) {
          row.classList.add('selected');
        }
      }
      if (boolValue(itemMap.active) || boolValue(metadata.active)) {
        row.classList.add('active');
      }

      const header = document.createElement('div');
      header.className = 'session-header';

      const idSpan = document.createElement('span');
      idSpan.className = 'session-cell';
      idSpan.textContent = sessionPickerShortId(sid);

      const usedSpan = document.createElement('span');
      usedSpan.className = 'session-cell';
      usedSpan.textContent = sessionPickerFormatTimestamp(modified);

      const createdSpan = document.createElement('span');
      createdSpan.className = 'session-cell';
      createdSpan.textContent = sessionPickerFormatTimestamp(created);

      header.append(idSpan, usedSpan, createdSpan);
      row.appendChild(header);

      if (summary) {
        const previewSpan = document.createElement('div');
        previewSpan.className = 'session-summary';
        previewSpan.textContent = summary;
        row.appendChild(previewSpan);
      }

      const footer = document.createElement('div');
      footer.className = 'session-footer';
      const cwdSpan = document.createElement('span');
      cwdSpan.className = 'session-cell session-meta';
      cwdSpan.textContent = sessionPickerRelativeHomePath(cwd);
      cwdSpan.title = cwd || '';
      footer.appendChild(cwdSpan);
      row.appendChild(footer);

      row.addEventListener('click', () => {
        if (_sessionPickerTarget) {
          _sessionPickerTarget.input.value = sid;
          _sessionPickerTarget.input.dataset.sessionId = sid;
        }
        closeSessionPicker();
      });
      sessionPickerListEl.appendChild(row);
    });
  }
  
  // Wire close button
  if (sessionPickerCloseBtn) {
    sessionPickerCloseBtn.addEventListener('click', closeSessionPicker);
  }
  
  /**
   * Load settings schema for an extension
   */
  async function loadSettingsSchema(extensionId: string): Promise<SettingsSchema | null> {
    const cachedSchema = schemaCache[extensionId];
    if (cachedSchema && cachedSchema.cache !== 'none') {
      console.log(`[schema] cache hit for ${extensionId}`);
      return cachedSchema;
    }
    
    try {
      console.log(`[schema] loading schema for ${extensionId} sioCall=${typeof getHelper(ctx, 'sioCall') === 'function'}`);
      const schema = normalizeSchema(await requireSettingsRpc().getExtensionSettingsSchema(extensionId));
      console.log(`[schema] loaded schema for ${extensionId}`, schema ? Object.keys(schema) : null);
      if (schema && schema.cache !== 'none') {
        schemaCache[extensionId] = schema;
      } else {
        delete schemaCache[extensionId];
      }
      return schema;
    } catch {
      return null;
    }
  }

  function liveSessionBinding(): {
    conversationId: string;
    extensionId: string;
    providerSessionId: string;
  } | null {
    const state = getCodexAgentState();
    const meta = state.conversationMeta;
    if (state.pendingNewConversation || !meta || typeof meta !== 'object') return null;
    const conversationId = trimString(meta.conversation_id);
    const threadId = trimString(meta.thread_id);
    const providerSessionId = trimString(meta.provider_session_id);
    const bindingId = threadId || providerSessionId;
    const extensionId = (
      settingsAgentEl?.value?.trim()
      || trimString(state.conversationSettings?.agent)
      || trimString(meta.extension_id)
      || trimString(meta.agent_type)
    );
    if (!conversationId || !extensionId || !bindingId) return null;
    return {
      conversationId,
      extensionId,
      providerSessionId: bindingId,
    };
  }

  function boolFromPayload(payload: JsonRecord, key: string): boolean {
    return payload[key] === true;
  }

  function sessionStateText(payload: JsonRecord): string {
    const state = trimString(payload.state);
    if (state === 'loaded' || boolFromPayload(payload, 'loaded')) return 'Loaded in memory';
    if (state === 'cold') return 'Not loaded';
    if (state === 'busy') return 'Busy';
    if (state === 'unsupported') return 'Live state unsupported';
    if (state === 'unbound') return 'No provider binding';
    return 'Status unavailable';
  }

  function sessionStateTone(payload: JsonRecord): string {
    const state = trimString(payload.state);
    if (state === 'loaded' || boolFromPayload(payload, 'loaded')) return 'loaded';
    if (state === 'cold') return 'cold';
    if (state === 'busy') return 'busy';
    return 'unknown';
  }

  function renderLiveSessionInfo(field: SchemaField): HTMLDivElement {
    const binding = liveSessionBinding();
    const info = document.createElement('div');
    info.className = 'settings-schema-info settings-live-session';
    info.dataset.state = 'unknown';

    const header = document.createElement('div');
    header.className = 'settings-live-session-header';

    const infoLabel = document.createElement('div');
    infoLabel.className = 'settings-schema-info-label';
    infoLabel.textContent = field.label || field.id || 'Provider Session';
    header.appendChild(infoLabel);

    const status = document.createElement('span');
    status.className = 'settings-live-session-status';
    status.textContent = binding ? 'Checking...' : 'Unavailable';
    header.appendChild(status);
    info.appendChild(header);

    const row = document.createElement('div');
    row.className = 'settings-live-session-row';

    const infoText = document.createElement('div');
    infoText.className = 'settings-schema-info-text settings-live-session-id';
    infoText.textContent = typeof field.text === 'string' && field.text.trim()
      ? field.text.trim()
      : 'Information unavailable.';
    row.appendChild(infoText);

    const unloadButton = document.createElement('button');
    unloadButton.type = 'button';
    unloadButton.className = 'settings-live-session-unload';
    unloadButton.textContent = '×';
    unloadButton.title = 'Unload provider session from memory';
    unloadButton.disabled = true;
    row.appendChild(unloadButton);
    info.appendChild(row);

    const detail = document.createElement('div');
    detail.className = 'settings-schema-info-detail';
    detail.textContent = field.detail || 'Bound provider session or thread identifier.';
    info.appendChild(detail);

    if (!binding) {
      status.textContent = 'Unavailable';
      unloadButton.style.display = 'none';
      return info;
    }

    const setPayload = (payload: JsonRecord): void => {
      const tone = sessionStateTone(payload);
      info.dataset.state = tone;
      status.textContent = sessionStateText(payload);
      const supported = payload.supported === true;
      const unloadSupported = payload.unload_supported === true || payload.unloadSupported === true;
      const busy = payload.busy === true || tone === 'busy';
      unloadButton.style.display = supported && unloadSupported ? '' : 'none';
      unloadButton.disabled = !supported || !unloadSupported || busy;
      const error = trimString(payload.error);
      detail.textContent = error || field.detail || 'Bound provider session or thread identifier.';
    };

    const setFailure = (error: unknown): void => {
      info.dataset.state = 'unknown';
      status.textContent = 'Status unavailable';
      unloadButton.disabled = true;
      detail.textContent = dynamicSourceErrorMessage(error);
    };

    const refresh = async (): Promise<void> => {
      const settingsRpc = requireSettingsRpc();
      if (typeof settingsRpc.getExtensionSessionState !== 'function') {
        setPayload({
          ok: true,
          supported: false,
          state: 'unsupported',
          loaded: false,
          unload_supported: false,
        });
        return;
      }
      const result = await settingsRpc.getExtensionSessionState({
        extensionId: binding.extensionId,
        conversationId: binding.conversationId,
        providerSessionId: binding.providerSessionId,
      });
      setPayload(asRecord(result));
    };

    unloadButton.addEventListener('click', () => {
      void (async () => {
        const settingsRpc = requireSettingsRpc();
        if (typeof settingsRpc.unloadExtensionSession !== 'function') return;
        unloadButton.disabled = true;
        status.textContent = 'Unloading...';
        try {
          const result = await settingsRpc.unloadExtensionSession({
            extensionId: binding.extensionId,
            conversationId: binding.conversationId,
            providerSessionId: binding.providerSessionId,
          });
          const payload = asRecord(result);
          setPayload(payload);
          if (payload.ok !== false) {
            await refresh();
          }
        } catch (error) {
          setFailure(error);
        }
      })();
    });

    void refresh().catch(setFailure);
    return info;
  }

  /**
   * Render schema fields into the extension fields container
   */
  function renderSchemaFields(schema: SettingsSchema | null, values: JsonRecord = {}): void {
    if (!settingsExtensionFields) return;
    settingsExtensionFields.innerHTML = '';
    currentSchemaValues = {};
    
    if (!schema || !Array.isArray(schema.fields)) return;
    const getConversationInfoFields = (): SchemaField[] => {
      const state = getCodexAgentState();
      if (state?.pendingNewConversation) return [];

      const meta = state.conversationMeta;
      if (!meta || typeof meta !== 'object') return [];

      const conversationId = typeof meta.conversation_id === 'string' ? meta.conversation_id.trim() : '';
      const threadId = typeof meta.thread_id === 'string' ? meta.thread_id.trim() : '';
      const providerSessionId = typeof meta.provider_session_id === 'string'
        ? meta.provider_session_id.trim()
        : '';
      const bindingId = threadId || providerSessionId;
      if (!conversationId || !bindingId) return [];

      return [
        {
          id: '__conversation_info_section',
          type: 'section',
          label: 'Conversation Info',
          description: 'Current harness conversation binding for this active provider-backed conversation.',
        },
        {
          id: '__conversation_info_conversation_id',
          type: 'info',
          label: 'Conversation ID',
          text: conversationId,
          detail: 'Harness conversation identifier.',
        },
        {
          id: '__conversation_info_provider_thread_id',
          type: 'live_session_info',
          label: 'Provider Session / Thread ID',
          text: bindingId,
          detail: 'Bound provider session or thread identifier.',
        },
      ];
    };

    const moveFieldAfterIndex = (fields: SchemaField[], fieldIndex: number, afterIndex: number): SchemaField[] => {
      if (!Array.isArray(fields) || fieldIndex < 0 || afterIndex < 0 || fieldIndex === afterIndex + 1) {
        return fields;
      }

      const reordered = fields.slice();
      const [field] = reordered.splice(fieldIndex, 1);
      if (!field) return fields;

      const normalizedAfterIndex = fieldIndex < afterIndex ? afterIndex - 1 : afterIndex;
      reordered.splice(normalizedAfterIndex + 1, 0, field);
      return reordered;
    };

    const getRenderFields = (): SchemaField[] => {
      const fields = Array.isArray(schema.fields) ? schema.fields.slice() : [];
      const conversationInfoFields = getConversationInfoFields();
      if (!conversationInfoFields.length) return fields;

      const sessionPickerIndex = fields.findIndex((field) => field && field.type === 'session_picker');
      if (sessionPickerIndex >= 0) {
        let renderFields = [
          ...fields.slice(0, sessionPickerIndex + 1),
          ...conversationInfoFields,
          ...fields.slice(sessionPickerIndex + 1),
        ];

        const cwdIndex = fields.findIndex((field) => field && field.id === 'cwd');
        if (cwdIndex >= 0 && cwdIndex < sessionPickerIndex) {
          const renderedCwdIndex = renderFields.findIndex((field) => field && field.id === 'cwd');
          const conversationInfoEndIndex = sessionPickerIndex + conversationInfoFields.length;
          renderFields = moveFieldAfterIndex(renderFields, renderedCwdIndex, conversationInfoEndIndex);
        }

        return renderFields;
      }

      let insertAt = 0;
      while (insertAt < fields.length) {
        const type = typeof fields[insertAt]?.type === 'string' ? fields[insertAt].type : '';
        if (type === 'section' || type === 'info') {
          insertAt += 1;
          continue;
        }
        break;
      }

      return [
        ...fields.slice(0, insertAt),
        ...conversationInfoFields,
        ...fields.slice(insertAt),
      ];
    };

    const renderFields = getRenderFields();
    if (!renderFields.length) return;
    const selectControls: Record<string, SelectControl> = {};
    let modelItems: JsonRecord[] = [];

    const fieldValueKeys = (field: SchemaField): string[] => {
      const extraKeys = Array.isArray(field?.value_keys)
        ? field.value_keys
            .map((key) => (typeof key === 'string' ? key.trim() : ''))
            .filter(Boolean)
        : [];
      return [field.id, ...extraKeys].filter((key, index, items): key is string => Boolean(key) && items.indexOf(key) === index);
    };

    const getFieldValue = (field: SchemaField, sourceValues: JsonRecord): JsonValue => {
      const resolvedValues = asRecord(sourceValues);
      for (const key of fieldValueKeys(field)) {
        if (Object.prototype.hasOwnProperty.call(resolvedValues, key)) {
          return resolvedValues[key];
        }
      }
      return field.default ?? '';
    };

    const readPath = (source: unknown, path: unknown): unknown => {
      if (typeof path !== 'string' || !path.trim()) return undefined;
      return path
        .trim()
        .split('.')
        .filter(Boolean)
        .reduce<unknown>((current, part) => {
          if (!isRecord(current)) return undefined;
          return current[part];
        }, source);
    };

    const firstPathValue = (source: unknown, paths: unknown): unknown => {
      const candidates = Array.isArray(paths) ? paths : [paths];
      for (const path of candidates) {
        const value = readPath(source, path);
        if (value !== undefined && value !== null && value !== '') return value;
      }
      return undefined;
    };

    const optionFromDynamicItem = (
      item: unknown,
      valuePath: unknown,
      labelPath: unknown,
    ): SelectOption | null => {
      if (typeof item === 'string') {
        return item ? { value: item, label: item } : null;
      }
      const value = trimString(firstPathValue(item, valuePath) ?? (isRecord(item) ? item.value : ''));
      if (!value) return null;
      const label = trimString(firstPathValue(item, labelPath) ?? value) || value;
      return { value, label };
    };

    const selectedDependencyValue = (field: SchemaField): string => {
      const dynamicOptions = asRecord(field.dynamic_options_from);
      const sourceField = trimString(dynamicOptions.source_field);
      if (!sourceField) return '';
      return selectControls[sourceField]?.input?.value || '';
    };

    const findDependentSourceItem = (field: SchemaField): JsonRecord | null => {
      const dynamicOptions = asRecord(field.dynamic_options_from);
      const sourceValue = selectedDependencyValue(field);
      if (!sourceValue) return null;
      const matchPath = trimString(dynamicOptions.match_path) || 'id';
      const sourceControl = selectControls[trimString(dynamicOptions.source_field)];
      const ownControl = selectControls[field.id];
      const items = sourceControl?.dynamicItems?.length
        ? sourceControl.dynamicItems
        : (ownControl?.dynamicItems?.length ? ownControl.dynamicItems : modelItems);
      return items.find((item) => trimString(firstPathValue(item, matchPath)) === sourceValue) || null;
    };

    const optionsFromDependentSource = (field: SchemaField): DynamicSelectOptions => {
      const dynamicOptions = asRecord(field.dynamic_options_from);
      const sourceItem = findDependentSourceItem(field);
      if (!sourceItem) return { items: [], options: [], current: '', defaultValue: '' };
      const rawOptions = firstPathValue(sourceItem, dynamicOptions.options_path);
      const optionItems = Array.isArray(rawOptions) ? rawOptions : [];
      const valuePath = dynamicOptions.option_value_path || 'value';
      const labelPath = dynamicOptions.option_label_path || dynamicOptions.option_value_path || 'label';
      const options = optionItems
        .map((item) => optionFromDynamicItem(item, valuePath, labelPath))
        .filter((option): option is SelectOption => Boolean(option));
      return {
        items: optionItems.filter(isRecord),
        options,
        current: '',
        defaultValue: trimString(firstPathValue(sourceItem, dynamicOptions.default_path)),
      };
    };

    const parseModelVersion = (modelId: unknown): ModelVersion | null => {
      if (typeof modelId !== 'string') return null;
      const match = modelId.trim().toLowerCase().match(/^([a-z][a-z0-9]*)-(\d+)(?:\.(\d+))?(?:$|[-_])/);
      if (!match) return null;
      return {
        family: match[1],
        major: Number.parseInt(match[2], 10),
        minor: match[3] ? Number.parseInt(match[3], 10) : 0,
      };
    };

    const modelMatchesGate = (modelId: unknown, gate: unknown): boolean => {
      if (!isRecord(gate)) return true;
      const version = parseModelVersion(modelId);
      if (!version) return false;
      const family = typeof gate.family === 'string' ? gate.family.trim().toLowerCase() : '';
      if (family && version.family !== family) return false;
      const minMajor = Number.isFinite(gate.min_major) ? Number(gate.min_major) : gate.minMajor;
      const minMinor = Number.isFinite(gate.min_minor) ? Number(gate.min_minor) : gate.minMinor;
      const requiredMajor = Number.isFinite(minMajor) ? Number(minMajor) : 0;
      const requiredMinor = Number.isFinite(minMinor) ? Number(minMinor) : 0;
      if (version.major > requiredMajor) return true;
      if (version.major < requiredMajor) return false;
      return version.minor >= requiredMinor;
    };

    const normalizeDynamicSelectOptions = (field: SchemaField, data: unknown): DynamicSelectOptions => {
      if (!data) return { items: [], options: [], current: '', defaultValue: '' };
      const dataMap = asRecord(data);
      if (field.dynamic_options_key && isRecord(data)) {
        const descriptor = asRecord(dataMap[field.dynamic_options_key]);
        const items = Array.isArray(descriptor.options) ? descriptor.options : [];
        const options = items.map((item: unknown): SelectOption | null => {
          if (typeof item === 'string') return { value: item, label: item };
          const itemMap = asRecord(item);
          const value = trimString(itemMap.value);
          if (!value) return null;
          return {
            value,
            label: trimString(itemMap.label) || value,
          };
        }).filter((option): option is SelectOption => Boolean(option));
        return {
          items: items.filter(isRecord),
          options,
          current: trimString(descriptor.current),
          defaultValue: trimString(descriptor.default),
        };
      }
      const rawItems = Array.isArray(data) ? data : dataMap.models || dataMap.options || [];
      const items = Array.isArray(rawItems) ? rawItems : [];
      const options = items.map((item: unknown): SelectOption => {
        if (isRecord(item)) {
          const value = trimString(item.id || item.value);
          return { value, label: trimString(item.name || item.label || item.id || item.value) || value };
        }
        const value = String(item ?? '');
        return { value, label: value };
      }).filter((option) => option.value);
      return { items: items.filter(isRecord), options, current: '', defaultValue: '' };
    };

    const setSelectOptions = (control: SelectControl | undefined, options: SelectOption[] | string[] | undefined): void => {
      if (!control?.listDiv || !control?.input) return;
      control.listDiv.innerHTML = '';
      (options || []).forEach((opt: SelectOption | string) => {
        const optValue = typeof opt === 'object' ? opt.value : opt;
        const optLabel = typeof opt === 'object' ? (opt.label || opt.value) : opt;
        if (!optValue) return;
        const optBtn = document.createElement('button');
        optBtn.type = 'button';
        optBtn.className = 'dropdown-item';
        optBtn.textContent = optLabel;
        optBtn.addEventListener('click', () => {
          control.input.value = optValue;
          const closeDropdownMenu = getHelper(ctx, 'closeDropdownMenu');
          if (typeof closeDropdownMenu === 'function') {
            closeDropdownMenu(control.listDiv);
          } else {
            control.listDiv.classList.remove('open');
          }
          if (control.field?.id === 'model') syncModelDependentFields();
        });
        control.listDiv.appendChild(optBtn);
      });
    };

    const setSelectMessage = (control: SelectControl, message: string): void => {
      if (!control?.listDiv) return;
      control.listDiv.innerHTML = '';
      const messageRow = document.createElement('div');
      messageRow.className = 'picker-item';
      messageRow.textContent = message;
      control.listDiv.appendChild(messageRow);
    };

    const resetModelGatedInput = (input: SchemaInput, type: string | undefined): void => {
      if (type === 'checkbox') {
        if (input instanceof HTMLInputElement) input.checked = false;
        return;
      }
      input.value = '';
    };

    const syncModelGatedFields = (): void => {
      const modelControl = selectControls.model;
      if (!modelControl?.input) return;
      const selectedModelId = modelControl.input.value || '';
      Object.values(currentSchemaValues).forEach((entry) => {
        const modelGate = entry?.field?.model_gate;
        if (!isRecord(modelGate) || !entry?.input) return;
        const input = entry.input;
        const label = input.closest('label');
        const enabled = modelMatchesGate(selectedModelId, modelGate);
        const gateLabel = typeof modelGate.label === 'string' && modelGate.label.trim()
          ? modelGate.label.trim()
          : 'a supported model';
        const hint = enabled ? '' : `Available only when Model is ${gateLabel}`;
        input.disabled = !enabled;
        if (!enabled) {
          resetModelGatedInput(input, entry.type);
        }
        if (label) {
          label.classList.toggle('is-disabled', !enabled);
          if (hint) {
            label.title = hint;
          } else {
            label.removeAttribute('title');
          }
        }
        if (hint) {
          input.title = hint;
        } else {
          input.removeAttribute('title');
        }
      });
    };

    const syncReasoningEffortOptions = (): void => {
      const effortControl = selectControls.reasoning_effort;
      if (!effortControl) return;
      const dynamicOptions = asRecord(effortControl.field.dynamic_options_from);
      const sourceField = trimString(dynamicOptions.source_field);
      if (!sourceField) return;
      if (!selectedDependencyValue(effortControl.field)) {
        setSelectOptions(effortControl, []);
        effortControl.input.value = '';
        effortControl.input.placeholder = trimString(dynamicOptions.missing_source_placeholder)
          || 'Select source first';
        return;
      }
      const { options, defaultValue } = optionsFromDependentSource(effortControl.field);
      if (!options.length) {
        setSelectOptions(effortControl, []);
        effortControl.input.value = '';
        effortControl.input.placeholder = trimString(dynamicOptions.empty_placeholder)
          || 'No options available';
        return;
      }
      const currentValue = effortControl.input.value;
      const initialValue = effortControl.initialValue || '';
      const initialSourceValue = selectControls[sourceField]?.initialValue || '';
      const selectedSourceValue = selectedDependencyValue(effortControl.field);
      const optionValues = options.map((option) => option.value);
      const defaultEffort = defaultValue || optionValues[0] || '';
      setSelectOptions(effortControl, options);
      effortControl.input.placeholder = effortControl.field?.placeholder || '';
      let nextValue = defaultEffort;
      if (
        !effortControl.initialValueApplied
        && selectedSourceValue === initialSourceValue
        && initialValue
        && optionValues.includes(initialValue)
      ) {
        nextValue = initialValue;
        effortControl.initialValueApplied = true;
      } else if (currentValue && optionValues.includes(currentValue)) {
        nextValue = currentValue;
      }
      effortControl.input.value = nextValue;
    };

    const syncModelDependentFields = (): void => {
      syncReasoningEffortOptions();
      syncModelGatedFields();
    };
    
    renderFields.forEach((field: SchemaField) => {
      if (field.type === 'section') {
        const section = document.createElement('div');
        section.className = 'settings-schema-section';

        const title = document.createElement('div');
        title.className = 'settings-schema-section-title';
        title.textContent = field.label || field.id || 'Section';
        section.appendChild(title);

        if (typeof field.description === 'string' && field.description.trim()) {
          const description = document.createElement('div');
          description.className = 'settings-schema-section-description';
          description.textContent = field.description.trim();
          section.appendChild(description);
        }

        settingsExtensionFields.appendChild(section);
        return;
      }

      if (field.type === 'info') {
        const info = document.createElement('div');
        info.className = 'settings-schema-info';
        if (typeof field.tone === 'string' && field.tone.trim()) {
          info.dataset.tone = field.tone.trim();
        }

        const infoLabel = document.createElement('div');
        infoLabel.className = 'settings-schema-info-label';
        infoLabel.textContent = field.label || field.id || 'Information';
        info.appendChild(infoLabel);

        const infoText = document.createElement('div');
        infoText.className = 'settings-schema-info-text';
        infoText.textContent = typeof field.text === 'string' && field.text.trim()
          ? field.text.trim()
          : 'Information unavailable.';
        info.appendChild(infoText);

        if (typeof field.detail === 'string' && field.detail.trim()) {
          const infoDetail = document.createElement('div');
          infoDetail.className = 'settings-schema-info-detail';
          infoDetail.textContent = field.detail.trim();
          info.appendChild(infoDetail);
        }

        settingsExtensionFields.appendChild(info);
        return;
      }

      if (field.type === 'live_session_info') {
        const info = renderLiveSessionInfo(field);
        settingsExtensionFields.appendChild(info);
        return;
      }

      const label = document.createElement('label');
      const span = document.createElement('span');
      span.textContent = field.label || field.id;
      label.appendChild(span);
      
      let input: SchemaInput | null = null;
      const value = getFieldValue(field, values);
      
      switch (field.type) {
        case 'path':
          // Path field with optional browse button
          const pathDiv = document.createElement('div');
          pathDiv.className = 'settings-row';
          
          input = document.createElement('input');
          const pathInput = input;
          pathInput.type = 'text';
          pathInput.id = `settings-ext-${field.id}`;
          pathInput.placeholder = field.placeholder || '';
          pathInput.value = String(value ?? '');
          pathDiv.appendChild(pathInput);
          
          if (field.browse) {
            const browseBtn = document.createElement('button');
            browseBtn.type = 'button';
            browseBtn.className = 'btn ghost';
            browseBtn.textContent = 'Browse';
            browseBtn.addEventListener('click', () => {
              // Use the existing picker if available
              const openPicker = getHelper(ctx, 'openPicker');
              if (typeof openPicker === 'function') {
                openPicker(pathInput.value || '~', 'cwd', { input: pathInput });
              }
            });
            pathDiv.appendChild(browseBtn);
          }
          
          label.appendChild(pathDiv);
          break;

        case 'session_picker':
          // Session picker: only shown for NEW conversations (no thread_id yet).
          // Once a conversation is bound to a session, this field disappears.
          const state = getCodexAgentState();
          const hasThread = !state.pendingNewConversation && Boolean(state.conversationMeta?.thread_id);
          if (hasThread) break; // Already bound — hide picker

          const sessionDiv = document.createElement('div');
          sessionDiv.className = 'settings-row';
          
          input = document.createElement('input');
          const sessionInput = input;
          sessionInput.type = 'text';
          sessionInput.id = `settings-ext-${field.id}`;
          sessionInput.placeholder = field.placeholder || '(new session)';
          sessionInput.value = String(value || '');
          sessionInput.dataset.sessionId = String(value || '');
          sessionInput.addEventListener('input', () => {
            sessionInput.dataset.sessionId = sessionInput.value || '';
          });
          sessionDiv.appendChild(sessionInput);
          
          const resumeBtn = document.createElement('button');
          resumeBtn.type = 'button';
          resumeBtn.className = 'btn ghost';
          resumeBtn.textContent = 'Browse';
          resumeBtn.addEventListener('click', () => {
            openSessionPicker(field, sessionInput);
          });
          sessionDiv.appendChild(resumeBtn);
          
          label.appendChild(sessionDiv);
          break;
          
        case 'select':
          // Dropdown field
          const selectDiv = document.createElement('div');
          selectDiv.className = 'dropdown-field';
          
          input = document.createElement('input');
          const selectInput = input;
          selectInput.type = 'text';
          selectInput.id = `settings-ext-${field.id}`;
          selectInput.placeholder = field.placeholder || '';
          selectInput.value = String(value ?? '');
          selectInput.readOnly = true;
          selectDiv.appendChild(selectInput);
          
          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'btn ghost dropdown-toggle';
          toggleBtn.textContent = '▾';
          selectDiv.appendChild(toggleBtn);
          
          const listDiv = document.createElement('div');
          listDiv.className = 'dropdown-list';
          listDiv.id = `settings-ext-${field.id}-options`;
          const selectControl: SelectControl = {
            field,
            input: selectInput,
            listDiv,
            initialValue: selectInput.value,
            initialValueApplied: false,
            dynamicItems: [],
          };
          selectControls[field.id] = selectControl;
          
          // Build options (static or dynamic)
          const buildOptions = (options: SelectOption[] | string[] | undefined): void => {
            setSelectOptions(selectControl, options);
          };
          
          if (field.dynamic_options_from) {
            buildOptions([]);
            const dynamicOptions = asRecord(field.dynamic_options_from);
            selectInput.placeholder = trimString(dynamicOptions.missing_source_placeholder)
              || field.placeholder
              || 'Select source first';
          } else {
            buildOptions(normalizeStaticOptions(field.options));
          }
          
          // Fetch dynamic options if configured
          if (field.dynamic_source) {
            const loadOpts = (data: unknown): void => {
              if (!data) return;
              const { items, options: opts, current, defaultValue } = normalizeDynamicSelectOptions(field, data);
              selectControl.dynamicItems = items;
              selectInput.removeAttribute('title');
              if (field.id === 'model') {
                modelItems = items;
                if (!selectInput.value) {
                  selectInput.placeholder = field.placeholder || 'Use server default';
                }
              } else if (field.dynamic_options_from) {
                const dependent = optionsFromDependentSource(field);
                buildOptions(dependent.options);
                if (!selectInput.value) {
                  selectInput.placeholder = dependent.defaultValue || field.placeholder || '';
                  if (dependent.defaultValue) selectInput.value = dependent.defaultValue;
                }
              } else if (!selectInput.value) {
                selectInput.placeholder = defaultValue || field.placeholder || '';
                if (current) selectInput.value = current;
              }
              if (!field.dynamic_options_from && opts.length) buildOptions(opts);
              if (field.id === 'model') syncModelDependentFields();
            };
            const selectedAgent = settingsAgentEl?.value?.trim() || '';
            const conversationId = stringValue(getCodexAgentState().conversationMeta?.conversation_id);
            const dynamicSource = typeof field.dynamic_source === 'string' ? field.dynamic_source : '';
            const runtimeOptionsSource = isRuntimeOptionsSource(dynamicSource);
            const extensionModelsSource = Boolean(extensionIdFromApiPath(dynamicSource, 'models'));
            if (runtimeOptionsSource || extensionModelsSource) {
              fetchDynamicSource(dynamicSource, {
                conversationId,
                agent: selectedAgent,
              }).then(loadOpts).catch((e) => {
                console.error('[schema] dynamic Socket.IO load failed', e);
                const errorMessage = dynamicSourceErrorMessage(e);
                selectInput.title = errorMessage;
                if (!selectInput.value) {
                  selectInput.placeholder = 'Unable to load options';
                }
                setSelectMessage(selectControl, 'Unable to load options');
              });
            } else {
              const errorMessage = `Unsupported dynamic source: ${field.dynamic_source}`;
              input.title = errorMessage;
              if (!input.value) {
                input.placeholder = 'Unable to load options';
              }
              setSelectMessage(selectControl, 'Unable to load options');
              console.error(`[schema] ${errorMessage}`);
            }
          }
          
          toggleBtn.addEventListener('click', (e: MouseEvent) => {
            e.preventDefault();
            const toggleDropdownMenu = getHelper(ctx, 'toggleDropdownMenu');
            if (typeof toggleDropdownMenu === 'function') {
              toggleDropdownMenu(listDiv);
            } else {
              listDiv.classList.toggle('open');
            }
          });
          
          selectDiv.appendChild(listDiv);
          label.appendChild(selectDiv);
          break;
          
        case 'checkbox':
          input = document.createElement('input');
          input.type = 'checkbox';
          input.id = `settings-ext-${field.id}`;
          input.checked = value === true || value === 'true';
          label.appendChild(input);
          label.className = 'settings-checkbox-row';
          break;
          
        case 'number':
          input = document.createElement('input');
          input.type = 'number';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = String(value ?? '');
          if (field.min !== undefined) input.min = String(field.min);
          if (field.max !== undefined) input.max = String(field.max);
          label.appendChild(input);
          break;

        case 'textarea':
          input = document.createElement('textarea');
          input.id = `settings-ext-${field.id}`;
          input.className = 'settings-textarea';
          input.placeholder = field.placeholder || '';
          input.rows = field.rows || 6;
          input.value = value == null ? '' : String(value);
          label.appendChild(input);
          break;

        case 'json':
          input = document.createElement('textarea');
          input.id = `settings-ext-${field.id}`;
          input.className = 'settings-textarea settings-json-input';
          input.placeholder = field.placeholder || '';
          input.rows = field.rows || 8;
          if (typeof getHelper(ctx, 'formatJsonSetting') === 'function') {
            input.value = formatJsonSetting(ctx, value);
          } else if (typeof value === 'string') {
            input.value = value;
          } else if (value == null || value === '') {
            input.value = '';
          } else {
            input.value = JSON.stringify(value, null, 2);
          }
          label.appendChild(input);
          break;
          
        case 'text':
        default:
          input = document.createElement('input');
          input.type = 'text';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = String(value ?? '');
          label.appendChild(input);
          break;
      }
      
      // Track for save (only if input was created)
      if (input) {
        currentSchemaValues[field.id] = { input, type: field.type, field };
      }
      
      settingsExtensionFields.appendChild(label);
    });

    syncModelDependentFields();
  }
  
  /**
   * Get current values from schema fields
   */
  function collectSchemaValues(parseStructured = false): JsonRecord {
    const values: JsonRecord = {};
    Object.entries(currentSchemaValues).forEach(([id, { input, type, field }]) => {
      if (!input) return;
      if (type === 'session_picker') {
        values[id] = input.value || input.dataset.sessionId || '';
        return;
      }
      if (type === 'checkbox') {
        values[id] = input instanceof HTMLInputElement ? input.checked : false;
      } else if (parseStructured && type === 'json') {
        const parsed = parseJsonSetting(ctx, input.value, field?.label || field?.id || id);
        if (field?.json_kind === 'object' && parsed != null && (Array.isArray(parsed) || typeof parsed !== 'object')) {
          throw new Error(`${field.label || field.id || id} must be a JSON object`);
        }
        values[id] = parsed;
      } else {
        values[id] = input.value;
      }
    });
    return values;
  }

  function getSchemaRawValues(): JsonRecord {
    return collectSchemaValues(false);
  }

  function getSchemaParsedValues(): JsonRecord {
    return collectSchemaValues(true);
  }

  function getSchemaValues(): JsonRecord {
    return getSchemaRawValues();
  }

  function getSchemaFieldInput(fieldId: string): SchemaInput | null {
    return currentSchemaValues[fieldId]?.input || null;
  }
  
  /**
   * Update settings modal based on selected agent
   */
  async function onAgentChange(agentId: string): Promise<void> {
    const isCodex = agentId === 'codex';
    
    // Show/hide Codex-specific fields
    if (settingsCodexFields) {
      settingsCodexFields.style.display = isCodex ? 'block' : 'none';
    }
    
    // Clear extension fields
    if (settingsExtensionFields) {
      settingsExtensionFields.innerHTML = '';
    }
    currentSchemaValues = {};
    
    if (!isCodex) {
      // Load and render schema for this extension
      const schema = await loadSettingsSchema(agentId);
      if (schema && !schema.useBuiltin) {
        // For new conversations, use empty values; for existing, use saved settings
        const state = getCodexAgentState();
        const isPending = Boolean(state.pendingNewConversation);
        let settings: JsonRecord = isPending ? {} : asRecord(state.conversationSettings);
        // Prefill CWD from project root when starting from the project tab
        if (isPending) {
          const st = getCodexAgentState();
          const hu = st.hostUi;
          if (hu?.ideMode && st.splashTab === 'project' && typeof hu.projectRoot === 'string' && hu.projectRoot) {
            settings = { cwd: hu.projectRoot };
          }
        }
        renderSchemaFields(schema, settings);
      }
    }
  }
  
  // Export helpers - called after CodexAgent is created, so ctx === window.CodexAgent
  ctx.helpers = ctx.helpers || {};
  ctx.helpers.loadSettingsSchema = loadSettingsSchema;
  ctx.helpers.renderSchemaFields = renderSchemaFields;
  ctx.helpers.getSchemaRawValues = getSchemaRawValues;
  ctx.helpers.getSchemaParsedValues = getSchemaParsedValues;
  ctx.helpers.getSchemaValues = getSchemaValues;
  ctx.helpers.getSchemaFieldInput = getSchemaFieldInput;
  ctx.helpers.onAgentChange = onAgentChange;
});

export {};
