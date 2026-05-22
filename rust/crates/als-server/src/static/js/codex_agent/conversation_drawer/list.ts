type Awaitable = Promise<unknown> | unknown;

interface ConversationSettings extends Record<string, unknown> {
  cwd?: string;
  label?: string;
  alias?: string;
  agent?: string;
}

interface ConversationMeta extends Record<string, unknown> {
  conversation_id?: string;
  settings?: ConversationSettings;
  cwd?: string;
  status?: string;
  pinned?: boolean;
  pending_approvals?: Record<string, unknown>;
  last_preview?: unknown;
}

export interface HostUiState {
  projectRoot?: string;
}

interface ConversationDisplay {
  conversationId: string;
  titleText: string;
  labelText: string;
  statusText: string;
  pendingCount: number;
  previewText: string;
  cwdText: string;
}

interface ExtensionCatalogEntry extends Record<string, unknown> {
  id?: string;
  active?: boolean;
  dependency_message?: string;
}

export interface DrawerListState {
  conversationList?: ConversationMeta[];
  clientConversationId?: string | null;
  conversationMeta?: ConversationMeta | null;
  extensionCatalog?: ExtensionCatalogEntry[];
  rpcTransportEnabled?: boolean;
}

interface ConversationDrawerListContext {
  conversationListEl: HTMLElement | null;
  conversationMiniListEl: HTMLElement | null;
  getState(): DrawerListState;
  getHostUi(): HostUiState | null | undefined;
  getSplashTab(): string;
  getConversationPreview(conversationId: string): unknown;
  selectConversation(conversationId: string): Awaitable;
  selectConversationWithView(conversationId: string, view: string): Awaitable;
  setConversationPins?(conversationIds: string[]): Awaitable;
  openSettingsModal(): void;
  openProjectModal(path?: string | null): void;
  deleteConversation(conversationId: string): Awaitable;
  documentRef?: Document;
  windowRef?: Window;
}

interface ConversationDrawerListBinding {
  renderConversationList(list: ConversationMeta[], activeConversationId: string | null): void;
  renderMiniConversationList(list: ConversationMeta[], activeConversationId: string | null): void;
  renderSplashTabs(): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

function getConversationSettings(meta: ConversationMeta | null | undefined): ConversationSettings {
  return isRecord(meta?.settings) ? (meta.settings as ConversationSettings) : {};
}

function conversationCwd(meta: ConversationMeta | null | undefined): string {
  const settings = getConversationSettings(meta);
  const settingsCwd = typeof settings.cwd === 'string' ? settings.cwd.trim() : '';
  if (settingsCwd) return settingsCwd;
  const metaCwd = typeof meta?.cwd === 'string' ? meta.cwd.trim() : '';
  return metaCwd;
}

function conversationMatchesProject(
  meta: ConversationMeta | null | undefined,
  hostUi: HostUiState | null | undefined,
  splashTab: string,
): boolean {
  if (splashTab !== 'project') return true;
  const projectRoot = hostUi?.projectRoot;
  if (!projectRoot || typeof projectRoot !== 'string') return false;
  const cwd = conversationCwd(meta);
  if (!cwd) return false;
  return cwd === projectRoot || cwd.startsWith(`${projectRoot}/`);
}

function normalizePreviewText(value: unknown): string {
  if (typeof value === 'string') {
    return value.trim().replace(/\s+/g, ' ').slice(0, 220);
  }
  if (isRecord(value) && typeof value.text === 'string') {
    return value.text.trim().replace(/\s+/g, ' ').slice(0, 220);
  }
  return '';
}

function buildConversationDisplay(
  meta: ConversationMeta | null | undefined,
  getConversationPreview: (conversationId: string) => unknown,
): ConversationDisplay {
  const conversationId = meta?.conversation_id || '';
  const settings = getConversationSettings(meta);
  const labelRaw = typeof settings.label === 'string' ? settings.label.trim() : '';
  const aliasRaw = typeof settings.alias === 'string' ? settings.alias.trim() : '';
  const alias = aliasRaw || conversationId;
  const agent = typeof settings.agent === 'string' && settings.agent.trim()
    ? settings.agent.trim()
    : '';
  const titleText = agent ? `${alias} · ${agent}` : alias;
  const status = typeof meta?.status === 'string' && meta.status.trim()
    ? meta.status.trim()
    : 'none';
  const pendingCount = meta?.pending_approvals && typeof meta.pending_approvals === 'object'
    ? Object.keys(meta.pending_approvals).length
    : 0;
  const previewText = normalizePreviewText(
    (typeof getConversationPreview === 'function' ? getConversationPreview(conversationId) : null) || meta?.last_preview,
  );
  const cwdText = conversationCwd(meta);
  return {
    conversationId,
    titleText,
    labelText: labelRaw,
    statusText: status,
    pendingCount,
    previewText,
    cwdText,
  };
}

function getActiveConversationIdFromState(state: DrawerListState | null | undefined): string | null {
  return state?.clientConversationId || state?.conversationMeta?.conversation_id || null;
}

export function createConversationDrawerList(
  ctx: ConversationDrawerListContext,
): ConversationDrawerListBinding {
  const {
    conversationListEl,
    conversationMiniListEl,
    getState,
    getHostUi,
    getSplashTab,
    getConversationPreview,
    selectConversation,
    selectConversationWithView,
    setConversationPins,
    openSettingsModal,
    openProjectModal,
    deleteConversation,
    documentRef,
    windowRef,
  } = ctx;

  let draggingConversationId: string | null = null;

  function getConversationListState(): ConversationMeta[] {
    const state = getState();
    return Array.isArray(state?.conversationList) ? state.conversationList : [];
  }

  function getPinnedConversationIds(): string[] {
    return getConversationListState()
      .filter((meta) => meta?.pinned === true && typeof meta?.conversation_id === 'string' && meta.conversation_id)
      .map((meta) => (typeof meta.conversation_id === 'string' ? meta.conversation_id : ''))
      .filter((conversationId): conversationId is string => Boolean(conversationId));
  }

  function clearPinnedDragMarkers(doc: Document | null | undefined): void {
    if (!doc) return;
    doc.querySelectorAll('.conversation-row.drag-over, .conversation-row.dragging, .conversation-mini-row.drag-over, .conversation-mini-row.dragging')
      .forEach((el) => {
        el.classList.remove('drag-over');
        el.classList.remove('dragging');
      });
  }

  function clearPinnedDragState(doc: Document | null | undefined): void {
    draggingConversationId = null;
    clearPinnedDragMarkers(doc);
  }

  async function persistPinnedConversationOrder(nextPinnedIds: string[]): Promise<void> {
    if (typeof setConversationPins !== 'function' || !Array.isArray(nextPinnedIds)) return;
    await setConversationPins(nextPinnedIds);
  }

  async function toggleConversationPinned(meta: ConversationMeta | null | undefined): Promise<void> {
    const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
    if (!conversationId) return;
    const pinnedIds = getPinnedConversationIds();
    let nextPinnedIds;
    if (meta?.pinned === true) {
      nextPinnedIds = pinnedIds.filter((id) => id !== conversationId);
    } else {
      nextPinnedIds = [...pinnedIds, conversationId];
    }
    await persistPinnedConversationOrder(nextPinnedIds);
  }

  function bindPinnedDragSource(
    handle: HTMLElement,
    row: HTMLDivElement,
    meta: ConversationMeta | null | undefined,
    doc: Document | null | undefined,
  ): void {
    const canPersistPins = typeof setConversationPins === 'function';
    const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
    const canDrag = Boolean(canPersistPins && meta?.pinned === true && conversationId);
    handle.draggable = canDrag;
    if (!canDrag) return;
    handle.addEventListener('dragstart', (evt) => {
      if (!evt.dataTransfer) {
        evt.preventDefault();
        return;
      }
      draggingConversationId = conversationId;
      evt.dataTransfer.effectAllowed = 'move';
      evt.dataTransfer.setData('text/plain', conversationId);
      row.classList.add('dragging');
    });
    handle.addEventListener('dragend', () => {
      clearPinnedDragState(doc);
    });
  }

  function reorderPinnedConversationIds(targetConversationId: string): string[] | null {
    const targetId = typeof targetConversationId === 'string' ? targetConversationId : '';
    const draggedId = typeof draggingConversationId === 'string' ? draggingConversationId : '';
    if (!draggedId || !targetId || draggedId === targetId) return null;
    const pinnedIds = getPinnedConversationIds();
    const fromIndex = pinnedIds.indexOf(draggedId);
    const toIndex = pinnedIds.indexOf(targetId);
    if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return null;
    const nextPinnedIds = [...pinnedIds];
    nextPinnedIds.splice(fromIndex, 1);
    nextPinnedIds.splice(toIndex, 0, draggedId);
    return nextPinnedIds;
  }

  function bindPinnedDropTarget(
    row: HTMLDivElement,
    meta: ConversationMeta | null | undefined,
    doc: Document | null | undefined,
  ): void {
    const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
    if (!conversationId) return;
    row.addEventListener('dragover', (evt) => {
      if (!draggingConversationId || meta?.pinned !== true || draggingConversationId === conversationId) return;
      if (!evt.dataTransfer) return;
      evt.preventDefault();
      evt.dataTransfer.dropEffect = 'move';
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', (evt) => {
      const nextTarget = evt.relatedTarget;
      if (nextTarget instanceof Element && row.contains(nextTarget)) return;
      row.classList.remove('drag-over');
    });
    row.addEventListener('drop', async (evt) => {
      if (!draggingConversationId || meta?.pinned !== true || draggingConversationId === conversationId) return;
      evt.preventDefault();
      const nextPinnedIds = reorderPinnedConversationIds(conversationId);
      clearPinnedDragState(doc);
      if (!nextPinnedIds) return;
      await persistPinnedConversationOrder(nextPinnedIds);
    });
  }

  function buildConversationInfo(
    doc: Document,
    meta: ConversationMeta | null | undefined,
    { compact = false }: { compact?: boolean } = {},
  ): HTMLDivElement {
    const display = buildConversationDisplay(meta, getConversationPreview);
    const info = doc.createElement('div');
    info.className = 'conversation-meta';

    const labelRow = doc.createElement('div');
    labelRow.className = 'conversation-label-line';
    labelRow.textContent = display.labelText || ' ';
    info.appendChild(labelRow);

    const title = doc.createElement('div');
    title.className = 'conversation-name';
    title.textContent = display.titleText;
    info.appendChild(title);

    const previewText = compact ? (display.previewText || display.cwdText) : display.previewText;
    const previewRow = doc.createElement('div');
    previewRow.className = `conversation-preview-line${compact ? ' compact' : ''}`;
    previewRow.textContent = previewText || '';
    if (previewText) previewRow.title = previewText;

    if (compact) {
      info.appendChild(previewRow);
      return info;
    }

    const statusRow = doc.createElement('div');
    statusRow.className = 'conversation-status';
    statusRow.textContent = `status:${display.statusText}`;
    info.appendChild(statusRow);

    if (!compact) {
      if (display.cwdText) {
        const cwd = doc.createElement('div');
        cwd.className = 'conversation-aux-line';
        cwd.textContent = display.cwdText;
        cwd.title = display.cwdText;
        info.appendChild(cwd);
      }

      if (display.pendingCount) {
        const pending = doc.createElement('div');
        pending.className = 'conversation-aux-line';
        pending.textContent = `${display.pendingCount} pending approval${display.pendingCount === 1 ? '' : 's'}`;
        info.appendChild(pending);
      }
    }

    info.appendChild(previewRow);
    return info;
  }

  function buildConversationCardControls(
    doc: Document,
    meta: ConversationMeta | null | undefined,
    row: HTMLDivElement,
    { compact = false, includeDragHandle = true }: { compact?: boolean; includeDragHandle?: boolean } = {},
  ): HTMLDivElement {
    const controls = doc.createElement('div');
    controls.className = compact ? 'conversation-card-controls conversation-mini-controls' : 'conversation-card-controls';

    const canPersistPins = typeof setConversationPins === 'function';

    const pinBtn = doc.createElement('button');
    pinBtn.type = 'button';
    pinBtn.className = `btn tiny conversation-pin-btn${meta?.pinned === true ? ' active' : ''}`;
    pinBtn.textContent = meta?.pinned === true ? '📌' : 'Pin';
    pinBtn.title = meta?.pinned === true ? 'Unpin conversation' : 'Pin conversation';
    pinBtn.disabled = !canPersistPins;
    pinBtn.addEventListener('click', async (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      await toggleConversationPinned(meta);
    });
    controls.appendChild(pinBtn);

    if (includeDragHandle) {
      const dragHandle = doc.createElement('button');
      dragHandle.type = 'button';
      dragHandle.className = `btn tiny conversation-drag-handle${meta?.pinned === true ? '' : ' disabled'}`;
      dragHandle.textContent = '↕';
      dragHandle.title = meta?.pinned === true
        ? 'Drag to reorder pinned conversations'
        : 'Pin this conversation to enable drag reordering';
      dragHandle.disabled = !canPersistPins || meta?.pinned !== true;
      dragHandle.addEventListener('click', (evt) => {
        evt.preventDefault();
        evt.stopPropagation();
      });
      bindPinnedDragSource(dragHandle, row, meta, doc);
      controls.appendChild(dragHandle);
    }

    bindPinnedDropTarget(row, meta, doc);
    return controls;
  }

  function buildConversationRowHandle(
    doc: Document,
    meta: ConversationMeta | null | undefined,
    row: HTMLDivElement,
  ): HTMLDivElement {
    const handle = doc.createElement('div');
    const canDrag = typeof setConversationPins === 'function' && meta?.pinned === true && meta?.conversation_id;
    handle.className = `conversation-row-handle${canDrag ? '' : ' disabled'}`;
    handle.textContent = '≡';
    handle.title = canDrag
      ? 'Drag to reorder pinned conversations'
      : 'Pin this conversation to enable drag reordering';
    bindPinnedDragSource(handle, row, meta, doc);
    return handle;
  }

  function hasActiveTextSelection(): boolean {
    const selection = windowRef?.getSelection?.();
    return Boolean(selection && !selection.isCollapsed && String(selection).trim());
  }

  function shouldIgnoreRowActivation(evt: Event): boolean {
    if (hasActiveTextSelection()) return true;
    const target = evt.target;
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest('button, a, input, label, select, textarea, summary'));
  }

  function closeConversationMenus(doc: Document): void {
    doc.querySelectorAll<HTMLElement>('.app-menu-panel').forEach((panel) => {
      panel.classList.add('hidden');
    });
    doc.querySelectorAll<HTMLElement>('.app-menu [aria-expanded="true"]').forEach((toggle) => {
      toggle.setAttribute('aria-expanded', 'false');
    });
  }

  function buildConversationRowMenu(
    doc: Document,
    meta: ConversationMeta | null | undefined,
    conversationId: string,
  ): HTMLDivElement {
    const menu = doc.createElement('div');
    menu.className = 'app-menu conversation-row-menu';

    const toggle = doc.createElement('button');
    toggle.type = 'button';
    toggle.className = 'btn tiny icon-btn conversation-row-menu-toggle';
    toggle.setAttribute('aria-haspopup', 'menu');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.title = 'Conversation menu';
    toggle.textContent = '☰';

    const panel = doc.createElement('div');
    panel.className = 'app-menu-panel conversation-row-menu-panel hidden';
    panel.setAttribute('role', 'menu');

    const settingsItem = doc.createElement('button');
    settingsItem.type = 'button';
    settingsItem.className = 'app-menu-item';
    settingsItem.setAttribute('role', 'menuitem');
    settingsItem.textContent = 'Settings';
    settingsItem.addEventListener('click', async (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      closeConversationMenus(doc);
      if (!conversationId) return;
      await selectConversationWithView(conversationId, 'splash');
      openSettingsModal();
    });

    const projectItem = doc.createElement('button');
    projectItem.type = 'button';
    projectItem.className = 'app-menu-item';
    projectItem.setAttribute('role', 'menuitem');
    projectItem.textContent = 'Project';
    projectItem.addEventListener('click', (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      closeConversationMenus(doc);
      const cwd = conversationCwd(meta) || null;
      openProjectModal(cwd);
    });

    toggle.addEventListener('click', (evt) => {
      evt.preventDefault();
      evt.stopPropagation();
      const willOpen = panel.classList.contains('hidden');
      closeConversationMenus(doc);
      panel.classList.toggle('hidden', !willOpen);
      toggle.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });

    panel.append(settingsItem, projectItem);
    menu.append(toggle, panel);
    return menu;
  }

  function bindConversationRowActivation(
    row: HTMLDivElement,
    conversationId: string,
  ): void {
    if (!conversationId) return;
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.addEventListener('click', (evt) => {
      if (shouldIgnoreRowActivation(evt)) return;
      void selectConversation(conversationId);
    });
    row.addEventListener('keydown', (evt) => {
      if (shouldIgnoreRowActivation(evt)) return;
      if (evt.key !== 'Enter' && evt.key !== ' ') return;
      evt.preventDefault();
      void selectConversation(conversationId);
    });
  }

  function renderConversationList(list: ConversationMeta[], activeConversationId: string | null): void {
    if (!conversationListEl) return;
    const doc = documentRef || document;
    const hostUi = getHostUi();
    const splashTab = getSplashTab();
    conversationListEl.innerHTML = '';
    const items = Array.isArray(list) ? list.filter((meta) => conversationMatchesProject(meta, hostUi, splashTab)) : [];
    if (!items.length) {
      const empty = doc.createElement('div');
      empty.className = 'muted';
      empty.textContent = splashTab === 'project' ? 'No project conversations yet.' : 'No conversations yet.';
      conversationListEl.appendChild(empty);
      return;
    }

    items.forEach((meta) => {
      const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
      const row = doc.createElement('div');
      row.className = 'conversation-row';
      if (conversationId && conversationId === activeConversationId) row.classList.add('active');
      if (meta?.pinned === true) row.classList.add('pinned');
      bindConversationRowActivation(row, conversationId);

      row.appendChild(buildConversationRowHandle(doc, meta, row));

      const info = buildConversationInfo(doc, meta);
      row.appendChild(info);

      const actions = doc.createElement('div');
      actions.className = 'conversation-actions';
      actions.appendChild(buildConversationCardControls(doc, meta, row, { includeDragHandle: false }));

      actions.appendChild(buildConversationRowMenu(doc, meta, conversationId));

      const deleteBtn = doc.createElement('button');
      deleteBtn.className = 'btn tiny decline';
      deleteBtn.textContent = 'Delete';
      deleteBtn.addEventListener('click', async () => {
        if (!conversationId) return;
        await deleteConversation(conversationId);
      });
      actions.appendChild(deleteBtn);

      row.appendChild(actions);
      conversationListEl.appendChild(row);
    });
  }

  function renderMiniConversationList(list: ConversationMeta[], activeConversationId: string | null): void {
    if (!conversationMiniListEl) return;
    const doc = documentRef || document;
    conversationMiniListEl.innerHTML = '';
    const items = Array.isArray(list) ? list : [];
    if (!items.length) {
      const empty = doc.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'No conversations yet.';
      conversationMiniListEl.appendChild(empty);
      return;
    }

    items.forEach((meta) => {
      const conversationId = typeof meta?.conversation_id === 'string' ? meta.conversation_id : '';
      const row = doc.createElement('div');
      row.className = 'conversation-mini-row';
      if (conversationId && conversationId === activeConversationId) row.classList.add('active');
      if (meta?.pinned === true) row.classList.add('pinned');

      const state = getState();
      const extensionCatalog = Array.isArray(state?.extensionCatalog) ? state.extensionCatalog : [];
      const agent = (meta?.settings || {}).agent || '';
      const extInfo = extensionCatalog.find((ext) => ext?.id === agent) || null;
      const unavailableDetail = extInfo && extInfo.active === false
        ? (extInfo.dependency_message || 'This extension is unavailable.')
        : '';

      const mainButton = doc.createElement('button');
      mainButton.type = 'button';
      mainButton.className = 'conversation-mini-main';
      if (unavailableDetail) {
        mainButton.disabled = true;
        mainButton.title = unavailableDetail;
      } else {
        mainButton.addEventListener('click', () => {
          if (!conversationId) return;
          void selectConversation(conversationId);
        });
      }

      const info = buildConversationInfo(doc, meta, { compact: true });
      mainButton.appendChild(info);
      row.appendChild(mainButton);
      row.appendChild(buildConversationCardControls(doc, meta, row, { compact: true }));
      conversationMiniListEl.appendChild(row);
    });
  }

  function renderSplashTabs(): void {
    const doc = documentRef || document;
    const state = getState();
    const splashTabAllBtn = doc.getElementById('splash-tab-all');
    const splashTabProjectBtn = doc.getElementById('splash-tab-project');
    const splashGoConversationBtn = doc.getElementById('splash-go-conversation');
    const activeTab = getSplashTab();
    const activeConversationId = getActiveConversationIdFromState(state);
    splashTabAllBtn?.classList.toggle('active', activeTab === 'all');
    splashTabProjectBtn?.classList.toggle('active', activeTab === 'project');
    if (splashGoConversationBtn instanceof HTMLButtonElement) {
      splashGoConversationBtn.disabled = !activeConversationId;
      splashGoConversationBtn.title = activeConversationId
        ? 'Go to active conversation'
        : 'No active conversation selected';
    }
  }

  return {
    renderConversationList,
    renderMiniConversationList,
    renderSplashTabs,
  };
}
