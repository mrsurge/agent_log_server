import type { JsonObject } from './rpc/ui/contract.ts';
import { applyPathScrollLabel } from './path_label.ts';

interface UiRpcProjectClient {
  getProjectSummary(options?: { conversationId?: string | null; path?: string | null; maxDiffBytes?: number }): Promise<JsonObject & { transport: string }>;
  getTe2ProjectStatus(options?: { path?: string | null }): Promise<JsonObject & { transport: string }>;
  openTe2Project(options?: { path?: string | null }): Promise<JsonObject & { transport: string }>;
  createTe2Project(options?: { path?: string | null; adoptExisting?: boolean; open?: boolean }): Promise<JsonObject & { transport: string }>;
  openFile(payload: JsonObject): Promise<JsonObject & { transport: string }>;
}

interface ProjectModalContext {
  uiRpc: UiRpcProjectClient;
  getConversationId(): string | null | undefined;
  getConversationCwd(): string | null | undefined;
  getProjectRoot(): string | null | undefined;
  toRelativePath(path: string | null | undefined): string;
  renderDiffBlock(block: HTMLElement, text: string, filePath: string): void;
  makeCollapsible(row: HTMLElement | null, cardId: string, startExpanded: boolean, options?: Record<string, unknown>): void;
  confirmProjectAction(options: { title: string; body: string; confirmText: string }): Promise<boolean>;
  documentRef?: Document;
}

interface ProjectModalBinding {
  openProjectModal(path?: string | null): Promise<void>;
  closeProjectModal(): void;
}

interface ProjectFile {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  bytes: number | null;
  diffBytes: number | null;
  diffTruncated: boolean;
  diffText: string;
}

interface ProjectSummary {
  ok: boolean;
  root: string;
  branch: string | null;
  headShort: string | null;
  dirty: boolean;
  changedFiles: number;
  additions: number;
  deletions: number;
  maxDiffBytes: number;
  files: ProjectFile[];
  truncatedFiles: boolean;
  error: string;
}

type Te2ProjectAction = 'current' | 'switch' | 'create' | 'disabled';

interface Te2ProjectState {
  ok: boolean;
  connected: boolean;
  targetPath: string;
  currentCwd: string;
  matchesCurrent: boolean;
  known: boolean;
  reason: string;
  action: Te2ProjectAction;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function numberValue(value: unknown): number {
  return Number.isFinite(Number(value)) ? Number(value) : 0;
}

function boolValue(value: unknown): boolean {
  return value === true;
}

function te2ActionValue(value: unknown): Te2ProjectAction {
  if (value === 'current' || value === 'switch' || value === 'create' || value === 'disabled') return value;
  return 'disabled';
}

function normalizeProjectFile(value: unknown): ProjectFile | null {
  if (!isRecord(value)) return null;
  const path = stringValue(value.path);
  if (!path) return null;
  return {
    path,
    status: stringValue(value.status) || 'modified',
    additions: numberValue(value.additions),
    deletions: numberValue(value.deletions),
    bytes: Number.isFinite(Number(value.bytes)) ? Number(value.bytes) : null,
    diffBytes: Number.isFinite(Number(value.diff_bytes)) ? Number(value.diff_bytes) : null,
    diffTruncated: value.diff_truncated === true,
    diffText: stringValue(value.diff_text),
  };
}

function normalizeProjectSummary(value: unknown): ProjectSummary {
  const payload = isRecord(value) ? value : {};
  const rawFiles = Array.isArray(payload.files) ? payload.files : [];
  return {
    ok: payload.ok !== false,
    root: stringValue(payload.root),
    branch: stringValue(payload.branch) || null,
    headShort: stringValue(payload.head_short) || null,
    dirty: payload.dirty === true,
    changedFiles: numberValue(payload.changed_files),
    additions: numberValue(payload.additions),
    deletions: numberValue(payload.deletions),
    maxDiffBytes: numberValue(payload.max_diff_bytes) || 15 * 1024,
    files: rawFiles.map(normalizeProjectFile).filter((file): file is ProjectFile => Boolean(file)),
    truncatedFiles: payload.truncated_files === true,
    error: stringValue(payload.error),
  };
}

function normalizeTe2ProjectState(value: unknown): Te2ProjectState {
  const payload = isRecord(value) ? value : {};
  return {
    ok: payload.ok !== false,
    connected: boolValue(payload.connected),
    targetPath: stringValue(payload.target_path),
    currentCwd: stringValue(payload.current_cwd),
    matchesCurrent: boolValue(payload.matches_current),
    known: boolValue(payload.known),
    reason: stringValue(payload.reason),
    action: te2ActionValue(payload.action),
  };
}

function formatBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return '';
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function absoluteProjectPath(root: string, path: string): string {
  if (!path || path.startsWith('/')) return path;
  return `${root.replace(/\/+$/, '')}/${path.replace(/^\.?\//, '')}`;
}

function setMenuOpen(toggle: HTMLElement | null, panel: HTMLElement | null, open: boolean): void {
  if (!toggle || !panel) return;
  panel.classList.toggle('hidden', !open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

export function bindProjectModal(ctx: ProjectModalContext): ProjectModalBinding {
  const doc = ctx.documentRef || document;
  const projectModalEl = doc.getElementById('project-modal');
  const projectCloseBtn = doc.getElementById('project-close');
  const projectDismissBtn = doc.getElementById('project-dismiss');
  const projectRefreshBtn = doc.getElementById('project-refresh');
  const projectTe2Control = doc.getElementById('project-te2-control') as HTMLButtonElement | null;
  const projectBodyEl = doc.getElementById('project-body');
  const conversationMenuToggle = doc.getElementById('conversation-menu-toggle');
  const conversationMenu = doc.getElementById('conversation-menu');
  const conversationProjectBtn = doc.getElementById('conversation-project');
  const conversationSettingsBtn = doc.getElementById('conversation-settings');
  let selectedProjectPath: string | null = null;
  let currentSummary: ProjectSummary | null = null;
  let currentTe2State: Te2ProjectState | null = null;

  function closeMenus(): void {
    setMenuOpen(conversationMenuToggle, conversationMenu, false);
    doc.querySelectorAll<HTMLElement>('.app-menu-panel').forEach((panel) => {
      panel.classList.add('hidden');
    });
    doc.querySelectorAll<HTMLElement>('.app-menu [aria-expanded="true"]').forEach((toggle) => {
      toggle.setAttribute('aria-expanded', 'false');
    });
  }

  function toggleMenu(toggle: HTMLElement | null, panel: HTMLElement | null): void {
    if (!toggle || !panel) return;
    const willOpen = panel.classList.contains('hidden');
    closeMenus();
    setMenuOpen(toggle, panel, willOpen);
  }

  function renderMessage(message: string): void {
    if (!projectBodyEl) return;
    projectBodyEl.innerHTML = '';
    const empty = doc.createElement('div');
    empty.className = 'muted project-empty';
    empty.textContent = message;
    projectBodyEl.appendChild(empty);
  }

  function appendStat(parent: HTMLElement, label: string, value: string): void {
    const stat = doc.createElement('div');
    stat.className = 'project-stat';
    const labelEl = doc.createElement('span');
    labelEl.className = 'project-stat-label';
    labelEl.textContent = label;
    const valueEl = doc.createElement('strong');
    valueEl.textContent = value;
    stat.append(labelEl, valueEl);
    parent.appendChild(stat);
  }

  function appendProjectRoot(parent: HTMLElement, value: string): void {
    const row = doc.createElement('div');
    row.className = 'project-root-row';
    const labelEl = doc.createElement('span');
    labelEl.className = 'project-stat-label';
    labelEl.textContent = 'Project Root';
    const valueEl = doc.createElement('strong');
    valueEl.textContent = value;
    row.append(labelEl, valueEl);
    parent.appendChild(row);
  }

  function appendProjectFilePill(parent: HTMLElement, className: string, value: string): void {
    if (!value) return;
    const pill = doc.createElement('span');
    pill.className = className;
    pill.textContent = value;
    parent.appendChild(pill);
  }

  function setTe2ControlState(state: Te2ProjectState | null, loading = false): void {
    if (!projectTe2Control) return;
    projectTe2Control.classList.remove('success', 'warning');
    if (loading) {
      projectTe2Control.disabled = true;
      projectTe2Control.textContent = 'Checking TE2...';
      projectTe2Control.title = '';
      return;
    }
    if (!state || !state.connected || state.action === 'disabled') {
      projectTe2Control.disabled = true;
      projectTe2Control.textContent = 'TE2 unavailable';
      projectTe2Control.title = state?.reason || 'TE2 sidebar IPC is unavailable.';
      return;
    }
    if (state.action === 'current') {
      projectTe2Control.disabled = true;
      projectTe2Control.classList.add('success');
      projectTe2Control.textContent = 'TE2 current';
      projectTe2Control.title = state.currentCwd
        ? `TE2 is open on ${state.currentCwd}`
        : 'TE2 is open on this project.';
      return;
    }
    if (state.action === 'switch') {
      projectTe2Control.disabled = false;
      projectTe2Control.classList.add('warning');
      projectTe2Control.textContent = 'Switch TE2';
      projectTe2Control.title = state.currentCwd
        ? `Switch TE2 from ${state.currentCwd} to ${state.targetPath}`
        : `Switch TE2 to ${state.targetPath}`;
      return;
    }
    projectTe2Control.disabled = false;
    projectTe2Control.classList.add('warning');
    projectTe2Control.textContent = 'Create TE2 Project';
    projectTe2Control.title = `Create or adopt ${state.targetPath} in TE2`;
  }

  async function refreshTe2ProjectState(projectRoot: string | null | undefined): Promise<Te2ProjectState | null> {
    const root = typeof projectRoot === 'string' && projectRoot.trim() ? projectRoot.trim() : '';
    if (!root) {
      currentTe2State = null;
      setTe2ControlState(null);
      return null;
    }
    setTe2ControlState(currentTe2State, true);
    try {
      currentTe2State = normalizeTe2ProjectState(await ctx.uiRpc.getTe2ProjectStatus({ path: root }));
    } catch {
      currentTe2State = {
        ok: true,
        connected: false,
        targetPath: root,
        currentCwd: '',
        matchesCurrent: false,
        known: false,
        reason: 'request_failed',
        action: 'disabled',
      };
    }
    setTe2ControlState(currentTe2State);
    return currentTe2State;
  }

  async function ensureTe2ProjectReady(projectRoot: string | null | undefined): Promise<boolean> {
    const root = typeof projectRoot === 'string' && projectRoot.trim() ? projectRoot.trim() : '';
    if (!root) return false;
    let state = currentTe2State && currentTe2State.targetPath === root
      ? currentTe2State
      : await refreshTe2ProjectState(root);
    if (!state || !state.connected || state.action === 'disabled') {
      setTe2ControlState(state);
      return false;
    }
    if (state.action === 'current') {
      return true;
    }
    if (state.action === 'switch') {
      const confirmed = await ctx.confirmProjectAction({
        title: 'Switch TE2 project?',
        body: state.currentCwd
          ? `TE2 is currently open on ${state.currentCwd}. Switch it to ${root}?`
          : `Switch TE2 to ${root}?`,
        confirmText: 'Switch',
      });
      if (!confirmed) return false;
      const result = await ctx.uiRpc.openTe2Project({ path: root });
      if (result.ok === false) return false;
      state = await refreshTe2ProjectState(root);
      return state?.action === 'current';
    }
    const confirmed = await ctx.confirmProjectAction({
      title: 'Create TE2 project?',
      body: `TE2 does not have ${root} in its project database. Create or adopt it and open it now?`,
      confirmText: 'Create',
    });
    if (!confirmed) return false;
    const result = await ctx.uiRpc.createTe2Project({ path: root, adoptExisting: true, open: true });
    if (result.ok === false) return false;
    state = await refreshTe2ProjectState(root);
    return state?.action === 'current';
  }

  function renderProjectFileCard(summary: ProjectSummary, file: ProjectFile): HTMLElement {
    const absolutePath = absoluteProjectPath(summary.root, file.path);
    const card = doc.createElement('div');
    card.className = `timeline-row command-result terminal-card diff project-file-card${file.diffTruncated ? ' disabled' : ''}`;
    const body = doc.createElement('div');
    body.className = 'body';

    const header = doc.createElement('div');
    header.className = 'diff-path-label command-ribbon project-file-header';
    const pathLabel = doc.createElement('span');
    pathLabel.className = 'project-file-path';
    applyPathScrollLabel(pathLabel, ctx.toRelativePath(absolutePath) || file.path, { title: absolutePath });

    const meta = doc.createElement('span');
    meta.className = 'project-file-meta';
    appendProjectFilePill(meta, 'project-status-pill', file.status);
    appendProjectFilePill(meta, 'project-file-stats', `+${file.additions} -${file.deletions}`);
    if (file.diffTruncated) {
      appendProjectFilePill(meta, 'project-file-truncated', `>${formatBytes(summary.maxDiffBytes)}`);
    } else if (file.diffBytes !== null) {
      appendProjectFilePill(meta, 'project-file-bytes', formatBytes(file.diffBytes));
    }

    header.append(pathLabel, meta);
    body.appendChild(header);

    if (file.diffTruncated) {
      const note = doc.createElement('div');
      note.className = 'muted project-file-disabled-note';
      note.textContent = `Diff exceeds ${formatBytes(summary.maxDiffBytes)}; rendering and links disabled.`;
      body.appendChild(note);
    } else if (file.diffText) {
      const block = doc.createElement('div');
      block.className = 'diff-block';
      body.appendChild(block);
      ctx.renderDiffBlock(block, file.diffText, absolutePath);
      ctx.makeCollapsible(card, `project-diff:${summary.root}:${file.path}`, false, {
        headerEl: header,
        persist: false,
        fullHeaderToggle: true,
      });
    } else {
      const note = doc.createElement('div');
      note.className = 'muted project-file-disabled-note project-file-open-placeholder';
      note.setAttribute('role', 'button');
      note.tabIndex = 0;
      note.dataset.path = absolutePath;
      note.title = absolutePath;
      note.textContent = 'No text diff available.';
      body.appendChild(note);
    }

    card.appendChild(body);
    return card;
  }

  function renderProjectSummary(summary: ProjectSummary): void {
    if (!projectBodyEl) return;
    currentSummary = summary;
    projectBodyEl.innerHTML = '';
    if (!summary.ok) {
      renderMessage(summary.error || 'Project summary unavailable.');
      return;
    }

    appendProjectRoot(projectBodyEl, summary.root || '-');

    const header = doc.createElement('div');
    header.className = 'project-summary-grid';
    appendStat(header, 'Branch', summary.branch || 'detached');
    appendStat(header, 'Commit', summary.headShort || '-');
    appendStat(header, 'Files', String(summary.changedFiles));
    appendStat(header, 'Added', `+${summary.additions}`);
    appendStat(header, 'Deleted', `-${summary.deletions}`);
    projectBodyEl.appendChild(header);

    if (!summary.files.length) {
      const empty = doc.createElement('div');
      empty.className = 'muted project-empty';
      empty.textContent = 'Working tree clean.';
      projectBodyEl.appendChild(empty);
      return;
    }

    const list = doc.createElement('div');
    list.className = 'project-file-list';
    summary.files.forEach((file) => {
      list.appendChild(renderProjectFileCard(summary, file));
    });
    projectBodyEl.appendChild(list);

    if (summary.truncatedFiles) {
      const note = doc.createElement('div');
      note.className = 'muted project-truncated-note';
      note.textContent = 'Changed file list truncated.';
      projectBodyEl.appendChild(note);
    }
  }

  function bindProjectDiffClickHandler(): void {
    async function openProjectFile(target: HTMLElement, evt: Event): Promise<void> {
      if (target.closest('.project-file-card.disabled')) return;
      const path = target.getAttribute('data-path') || '';
      if (!path) return;
      evt.preventDefault();
      evt.stopPropagation();
      const ready = await ensureTe2ProjectReady(currentSummary?.root);
      if (!ready) return;
      void ctx.uiRpc.openFile({ path, line: 1, column: 1 });
    }

    projectBodyEl?.addEventListener('click', (evt) => {
      const target = evt.target;
      if (!(target instanceof HTMLElement)) return;
      const fileOpenEl = target.closest('.project-file-open-placeholder');
      if (fileOpenEl instanceof HTMLElement) {
        void openProjectFile(fileOpenEl, evt);
        return;
      }
      const lineEl = target.closest('.diff-line');
      if (!(lineEl instanceof HTMLElement)) return;
      if (lineEl.closest('.project-file-card.disabled')) return;
      const path = lineEl.getAttribute('data-path') || '';
      const newLine = lineEl.getAttribute('data-new-line') || '';
      const oldLine = lineEl.getAttribute('data-old-line') || '';
      const line = parseInt(newLine || oldLine, 10);
      if (!path || !Number.isFinite(line) || line <= 0) return;
      evt.preventDefault();
      evt.stopPropagation();
      try {
        lineEl.classList.add('tap-flash');
        setTimeout(() => lineEl.classList.remove('tap-flash'), 180);
      } catch {}
      void (async () => {
        const ready = await ensureTe2ProjectReady(currentSummary?.root);
        if (!ready) return;
        await ctx.uiRpc.openFile({ path, line, column: 1 });
      })();
    });
    projectBodyEl?.addEventListener('keydown', (evt) => {
      if (evt.key !== 'Enter' && evt.key !== ' ') return;
      const target = evt.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('project-file-open-placeholder')) return;
      void openProjectFile(target, evt);
    });
  }

  async function refreshProjectSummary(): Promise<void> {
    renderMessage('Loading project summary...');
    try {
      const result = await ctx.uiRpc.getProjectSummary({
        conversationId: ctx.getConversationId() || undefined,
        path: selectedProjectPath || ctx.getConversationCwd() || ctx.getProjectRoot() || undefined,
        maxDiffBytes: 15 * 1024,
      });
      const summary = normalizeProjectSummary(result);
      renderProjectSummary(summary);
      if (summary.ok) {
        void refreshTe2ProjectState(summary.root);
      } else {
        setTe2ControlState(null);
      }
    } catch (error) {
      currentSummary = null;
      setTe2ControlState(null);
      renderMessage(error instanceof Error ? error.message : 'Project summary unavailable.');
    }
  }

  async function openProjectModal(path?: string | null): Promise<void> {
    closeMenus();
    selectedProjectPath = typeof path === 'string' && path.trim() ? path.trim() : null;
    if (!projectModalEl) return;
    projectModalEl.classList.remove('hidden');
    await refreshProjectSummary();
  }

  function closeProjectModal(): void {
    projectModalEl?.classList.add('hidden');
  }

  conversationMenuToggle?.addEventListener('click', (evt) => {
    evt.stopPropagation();
    toggleMenu(conversationMenuToggle, conversationMenu);
  });
  conversationProjectBtn?.addEventListener('click', () => {
    void openProjectModal();
  });
  conversationSettingsBtn?.addEventListener('click', closeMenus);
  projectCloseBtn?.addEventListener('click', closeProjectModal);
  projectDismissBtn?.addEventListener('click', closeProjectModal);
  projectRefreshBtn?.addEventListener('click', () => {
    void refreshProjectSummary();
  });
  projectTe2Control?.addEventListener('click', () => {
    void ensureTe2ProjectReady(currentSummary?.root);
  });
  bindProjectDiffClickHandler();
  projectModalEl?.addEventListener('click', (evt) => {
    if (evt.target === projectModalEl) closeProjectModal();
  });
  doc.addEventListener('click', (evt) => {
    const target = evt.target;
    if (!(target instanceof Element)) return;
    if (target.closest('.app-menu')) return;
    closeMenus();
  });
  doc.addEventListener('keydown', (evt) => {
    if (evt.key === 'Escape') {
      closeMenus();
      closeProjectModal();
    }
  });

  return {
    openProjectModal,
    closeProjectModal,
  };
}
