declare const Tribute: any;

type AnyRecord = Record<string, any>;

interface ComposerRuntimeState {
  conversationMeta?: AnyRecord;
  conversationSettings?: AnyRecord;
  draftSaveTimer?: ReturnType<typeof setTimeout> | null;
  lastDraftHash?: string | null;
  draftDirty?: boolean;
  applyingDraft?: boolean;
}

interface ComposerRuntimeContext {
  getState(): ComposerRuntimeState;
  setState(patch: Partial<ComposerRuntimeState>): void;
  promptEl: HTMLElement | null;
  mentionPillEl: HTMLElement | null;
  documentRef: Document;
  windowRef: Window;
  sioCall(event: string, data?: Record<string, unknown>): Promise<any>;
  escapeHtml(text: string): string;
}

const DRAFT_MENTION_ENVELOPE_START = '\x1eCODEX_MENTION ';
const DRAFT_MENTION_ENVELOPE_END = '\x1f';

function getDraftHash(text: string): string {
  return String(text || '').split('').reduce((acc, char) => ((acc << 5) - acc + char.charCodeAt(0)) | 0, 0).toString(16);
}

function isAbsolutePath(path: unknown): path is string {
  return typeof path === 'string' && path.startsWith('/');
}

function joinPath(basePath: string, childPath: string): string {
  if (!basePath) return childPath || '';
  if (!childPath) return basePath || '';
  if (basePath.endsWith('/')) return basePath + childPath;
  return `${basePath}/${childPath}`;
}

function getRelativePath(absolutePath: string, cwd: string): string {
  if (!absolutePath || !cwd) return absolutePath;
  const cwdNorm = cwd.endsWith('/') ? cwd : `${cwd}/`;
  if (absolutePath.startsWith(cwdNorm)) {
    return absolutePath.slice(cwdNorm.length);
  }
  return absolutePath;
}

export function bindComposerRuntime(ctx: ComposerRuntimeContext) {
  const {
    getState,
    setState,
    promptEl,
    mentionPillEl,
    documentRef,
    windowRef,
    sioCall,
    escapeHtml,
  } = ctx;

  let tributeInstance: any = null;
  let explicitMentionSaveTimer: ReturnType<typeof setTimeout> | null = null;
  let lastComposerSelectionRange: Range | null = null;
  let lastComposerSelectionConversationId: string | null = null;

  function appendTextWithBreaks(parent: HTMLElement, text: unknown) {
    if (!parent || text === null || text === undefined) return;
    const parts = String(text).split('\n');
    parts.forEach((part, idx) => {
      if (part) parent.appendChild(documentRef.createTextNode(part));
      if (idx < parts.length - 1) parent.appendChild(documentRef.createElement('br'));
    });
  }

  function buildDraftMentionPayload(rawPath: unknown, opts: AnyRecord = {}) {
    const resolvedOpts = opts || {};
    let pathOnly = String(rawPath || '').trim();
    let line = resolvedOpts.line ?? resolvedOpts.lineNo ?? null;
    let endLine = resolvedOpts.endLine ?? resolvedOpts.endLineNo ?? null;
    const col = resolvedOpts.col ?? null;
    const endCol = resolvedOpts.endCol ?? null;
    const content = typeof resolvedOpts.content === 'string' ? resolvedOpts.content : '';
    const lineMatch = pathOnly.match(/^(.+):(\d+)(?:-(\d+))?$/);
    if (lineMatch) {
      pathOnly = lineMatch[1];
      if (line == null) line = lineMatch[2];
      if (endLine == null) endLine = lineMatch[3] || null;
    }
    if (!pathOnly) return null;
    const payload: AnyRecord = { path: pathOnly };
    if (line != null && String(line).trim()) payload.line = String(line);
    if (endLine != null && String(endLine).trim()) payload.endLine = String(endLine);
    if (col != null && String(col).trim()) payload.col = String(col);
    if (endCol != null && String(endCol).trim()) payload.endCol = String(endCol);
    if (content) payload.content = content;
    return payload;
  }

  function encodeDraftMentionToken(rawPath: unknown, opts: AnyRecord = {}) {
    const payload = buildDraftMentionPayload(rawPath, opts);
    if (!payload) return '';
    return DRAFT_MENTION_ENVELOPE_START + JSON.stringify(payload) + DRAFT_MENTION_ENVELOPE_END;
  }

  function decodeDraftMentionPayload(payloadText: string) {
    try {
      const parsed: AnyRecord = JSON.parse(payloadText);
      if (!parsed || typeof parsed !== 'object') return null;
      const path = typeof parsed.path === 'string' ? parsed.path.trim() : '';
      if (!path) return null;
      const payload: AnyRecord = { path };
      if (parsed.line != null && String(parsed.line).trim()) payload.line = String(parsed.line);
      if (parsed.endLine != null && String(parsed.endLine).trim()) payload.endLine = String(parsed.endLine);
      if (parsed.col != null && String(parsed.col).trim()) payload.col = String(parsed.col);
      if (parsed.endCol != null && String(parsed.endCol).trim()) payload.endCol = String(parsed.endCol);
      if (typeof parsed.content === 'string' && parsed.content) payload.content = parsed.content;
      return payload;
    } catch {
      return null;
    }
  }

  function clearStoredComposerSelection() {
    lastComposerSelectionRange = null;
    lastComposerSelectionConversationId = null;
  }

  function isPromptRangeNode(node: Node | null): boolean {
    if (!promptEl || !node) return false;
    try {
      return node === promptEl || promptEl.contains(node);
    } catch {
      return false;
    }
  }

  function rememberComposerSelection(): boolean {
    if (!promptEl || getState().applyingDraft) return false;
    const selection = windowRef.getSelection?.();
    if (!selection || selection.rangeCount < 1) return false;
    const range = selection.getRangeAt(0);
    if (!range) return false;
    if (!isPromptRangeNode(range.commonAncestorContainer) || !isPromptRangeNode(range.startContainer) || !isPromptRangeNode(range.endContainer)) {
      return false;
    }
    try {
      lastComposerSelectionRange = range.cloneRange();
      lastComposerSelectionConversationId = getState().conversationMeta?.conversation_id || null;
      return true;
    } catch {
      return false;
    }
  }

  function getStoredComposerSelectionRange(): Range | null {
    if (!lastComposerSelectionRange) return null;
    if ((getState().conversationMeta?.conversation_id || null) !== lastComposerSelectionConversationId) {
      clearStoredComposerSelection();
      return null;
    }
    try {
      if (!isPromptRangeNode(lastComposerSelectionRange.startContainer) || !isPromptRangeNode(lastComposerSelectionRange.endContainer)) {
        clearStoredComposerSelection();
        return null;
      }
      return lastComposerSelectionRange.cloneRange();
    } catch {
      clearStoredComposerSelection();
      return null;
    }
  }

  function resolveComposerInsertTarget() {
    const selection = windowRef.getSelection?.();
    if (selection && selection.rangeCount > 0) {
      const liveRange = selection.getRangeAt(0);
      if (liveRange && isPromptRangeNode(liveRange.commonAncestorContainer) && isPromptRangeNode(liveRange.startContainer) && isPromptRangeNode(liveRange.endContainer)) {
        return { selection, range: liveRange };
      }
    }
    const storedRange = getStoredComposerSelectionRange();
    if (!storedRange) return null;
    promptEl?.focus();
    const restoredSelection = windowRef.getSelection?.();
    if (!restoredSelection) return null;
    try {
      restoredSelection.removeAllRanges();
      restoredSelection.addRange(storedRange);
      return { selection: restoredSelection, range: restoredSelection.getRangeAt(0) };
    } catch {
      clearStoredComposerSelection();
      return null;
    }
  }

  function toMentionAbsAndBestPath(rawPath: unknown) {
    const state = getState();
    const cwd = state.conversationSettings?.cwd || state.conversationMeta?.cwd || '';
    const stringPath = String(rawPath || '');
    const absPath = isAbsolutePath(stringPath) ? stringPath : (cwd ? joinPath(cwd, stringPath) : stringPath);
    const bestPath = (cwd && isAbsolutePath(absPath)) ? getRelativePath(absPath, cwd) : absPath;
    return { absPath, bestPath };
  }

  function createMentionToken(rawPath: unknown, opts: AnyRecord = {}) {
    let pathOnly = String(rawPath || '');
    let parsedLine: string | null = null;
    let parsedEndLine: string | null = null;
    const lineMatch = pathOnly.match(/^(.+):(\d+)(?:-(\d+))?$/);
    if (lineMatch) {
      pathOnly = lineMatch[1];
      parsedLine = lineMatch[2];
      parsedEndLine = lineMatch[3] || null;
    }

    const { absPath, bestPath } = toMentionAbsAndBestPath(pathOnly);
    const span = documentRef.createElement('span');
    span.className = 'mention-token';
    span.dataset.abs = absPath || '';
    span.dataset.path = bestPath || '';
    span.setAttribute('contenteditable', 'false');
    span.title = absPath || bestPath || '';

    const line = opts.line || parsedLine;
    const endLine = opts.endLine || parsedEndLine;
    if (line) span.dataset.line = String(line);
    if (endLine) span.dataset.endLine = String(endLine);
    if (opts.col) span.dataset.col = String(opts.col);
    if (opts.endCol) span.dataset.endCol = String(opts.endCol);

    const display = String(bestPath || '').split('/').filter(Boolean).pop() || bestPath || absPath;
    let displayText = display;
    if (line) {
      displayText += `:${line}`;
      if (endLine && endLine !== line) displayText += `-${endLine}`;
    }

    const content = opts.content || '';
    if (content) {
      span.dataset.content = content;
      span.appendChild(documentRef.createTextNode(displayText));
      const codeEl = documentRef.createElement('code');
      codeEl.className = 'mention-content-preview';
      codeEl.textContent = content;
      span.appendChild(codeEl);
    } else {
      span.textContent = displayText;
    }

    const removeBtn = documentRef.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'mention-token-remove';
    removeBtn.textContent = '(x)';
    removeBtn.setAttribute('aria-label', 'Remove mention');
    removeBtn.title = 'Remove mention';
    removeBtn.setAttribute('contenteditable', 'false');
    removeBtn.tabIndex = -1;
    span.appendChild(removeBtn);

    return span;
  }

  function renderPromptFromText(text: unknown) {
    if (!promptEl) return;
    setState({ applyingDraft: true });
    try {
      clearStoredComposerSelection();
      promptEl.innerHTML = '';
      const resolvedText = String(text || '');
      let cursor = 0;

      while (cursor < resolvedText.length) {
        const startIdx = resolvedText.indexOf(DRAFT_MENTION_ENVELOPE_START, cursor);
        if (startIdx === -1) {
          const remaining = resolvedText.slice(cursor);
          if (remaining) appendTextWithBreaks(promptEl, remaining);
          break;
        }

        const before = resolvedText.slice(cursor, startIdx);
        if (before) appendTextWithBreaks(promptEl, before);

        const payloadStart = startIdx + DRAFT_MENTION_ENVELOPE_START.length;
        const endIdx = resolvedText.indexOf(DRAFT_MENTION_ENVELOPE_END, payloadStart);
        if (endIdx === -1) {
          appendTextWithBreaks(promptEl, resolvedText.slice(startIdx));
          break;
        }

        const mentionPayload = decodeDraftMentionPayload(resolvedText.slice(payloadStart, endIdx));
        if (mentionPayload) {
          promptEl.appendChild(createMentionToken(mentionPayload.path, {
            line: mentionPayload.line,
            endLine: mentionPayload.endLine,
            col: mentionPayload.col,
            endCol: mentionPayload.endCol,
            content: mentionPayload.content,
          }));
        } else {
          appendTextWithBreaks(promptEl, resolvedText.slice(startIdx, endIdx + DRAFT_MENTION_ENVELOPE_END.length));
        }
        cursor = endIdx + DRAFT_MENTION_ENVELOPE_END.length;
      }
    } finally {
      setState({ applyingDraft: false });
    }
  }

  function serializePromptNode(node: Node | null): string {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node as HTMLElement;
    if (el.classList.contains('mention-token')) {
      const state = getState();
      const absPath = el.dataset.abs || '';
      const fallback = el.dataset.path || el.textContent || '';
      const cwd = state.conversationSettings?.cwd || state.conversationMeta?.cwd || '';
      let pathStr = '';
      if (absPath && cwd) {
        const best = getRelativePath(absPath, cwd);
        pathStr = best !== absPath ? best : absPath;
      } else {
        pathStr = fallback;
      }
      if (!pathStr) return '';
      const line = el.dataset.line;
      const endLine = el.dataset.endLine;
      if (line) {
        pathStr += `:${line}`;
        if (endLine && endLine !== line) pathStr += `-${endLine}`;
      }
      const content = el.dataset.content || '';
      if (content) {
        return '`' + pathStr + '`\n```\n' + content + '\n```';
      }
      return '`' + pathStr + '`';
    }
    if (el.tagName === 'BR') return '\n';
    let out = '';
    el.childNodes.forEach((child) => {
      out += serializePromptNode(child);
    });
    if (el.tagName === 'DIV' || el.tagName === 'P') out += '\n';
    return out;
  }

  function serializePromptNodeForDraft(node: Node | null): string {
    if (!node) return '';
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node as HTMLElement;
    if (el.classList.contains('mention-token')) {
      return encodeDraftMentionToken(el.dataset.abs || el.dataset.path || '', {
        line: el.dataset.line,
        endLine: el.dataset.endLine,
        col: el.dataset.col,
        endCol: el.dataset.endCol,
        content: el.dataset.content || '',
      });
    }
    if (el.tagName === 'BR') return '\n';
    let out = '';
    el.childNodes.forEach((child) => {
      out += serializePromptNodeForDraft(child);
    });
    if (el.tagName === 'DIV' || el.tagName === 'P') out += '\n';
    return out;
  }

  function getPromptText(): string {
    if (!promptEl) return '';
    let text = '';
    promptEl.childNodes.forEach((child) => {
      text += serializePromptNode(child);
    });
    return text;
  }

  function getPromptDraftText(): string {
    if (!promptEl) return '';
    let text = '';
    promptEl.childNodes.forEach((child) => {
      text += serializePromptNodeForDraft(child);
    });
    return text;
  }

  function clearPrompt() {
    if (!promptEl) return;
    promptEl.innerHTML = '';
    clearStoredComposerSelection();
  }

  function moveCaretToEnd() {
    if (!promptEl) return;
    promptEl.focus();
    const range = documentRef.createRange();
    range.selectNodeContents(promptEl);
    range.collapse(false);
    const selection = windowRef.getSelection?.();
    selection?.removeAllRanges();
    selection?.addRange(range);
    rememberComposerSelection();
  }

  function persistDraftNow() {
    const state = getState();
    const convoId = state.conversationMeta?.conversation_id;
    if (!convoId || !promptEl) return Promise.resolve();
    if (state.draftSaveTimer) {
      clearTimeout(state.draftSaveTimer);
      setState({ draftSaveTimer: null });
    }
    const text = getPromptDraftText();
    const hash = getDraftHash(text);
    if (hash === state.lastDraftHash) {
      setState({ draftDirty: false });
      return Promise.resolve();
    }
    setState({ lastDraftHash: hash });
    return sioCall('conversation_draft', {
      conversation_id: convoId,
      draft: text,
    }).then(() => {
      const latestState = getState();
      if (latestState.conversationMeta && latestState.conversationMeta.conversation_id === convoId) {
        latestState.conversationMeta.draft = text;
      }
      setState({ draftDirty: false });
    }).catch((err) => {
      setState({ draftDirty: true });
      console.warn('Immediate draft save failed:', err);
    });
  }

  function queueExplicitMentionDraftSave() {
    if (getState().applyingDraft) return;
    setState({ draftDirty: true });
    if (explicitMentionSaveTimer) clearTimeout(explicitMentionSaveTimer);
    explicitMentionSaveTimer = setTimeout(() => {
      explicitMentionSaveTimer = null;
      void persistDraftNow();
    }, 0);
  }

  function saveDraftDebounced() {
    const state = getState();
    if (state.draftSaveTimer) clearTimeout(state.draftSaveTimer);
    const convoId = state.conversationMeta?.conversation_id;
    if (!convoId) return;

    const nextTimer = setTimeout(async () => {
      const currentState = getState();
      const text = getPromptDraftText();
      const hash = getDraftHash(text);
      if (hash === currentState.lastDraftHash) return;
      setState({ lastDraftHash: hash });
      try {
        await sioCall('conversation_draft', {
          conversation_id: convoId,
          draft: text,
        });
        const latestState = getState();
        if (latestState.conversationMeta && latestState.conversationMeta.conversation_id === convoId) {
          latestState.conversationMeta.draft = text;
        }
        setState({ draftDirty: false });
      } catch (err) {
        console.warn('Draft save failed:', err);
      }
    }, 500);

    setState({ draftSaveTimer: nextTimer });
  }

  function bindComposerSelectionTracking() {
    if (!promptEl) return;
    const remember = () => {
      rememberComposerSelection();
    };
    documentRef.addEventListener('selectionchange', remember);
    promptEl.addEventListener('input', remember);
    promptEl.addEventListener('keyup', remember);
    promptEl.addEventListener('mouseup', remember);
    promptEl.addEventListener('focus', remember);
    mentionPillEl?.addEventListener('pointerdown', remember);
  }

  function restoreDraft() {
    if (!promptEl) return;
    const draft = getState().conversationMeta?.draft;
    if (typeof draft === 'string' && draft.trim()) {
      renderPromptFromText(draft);
      setState({
        draftDirty: false,
        lastDraftHash: getDraftHash(draft),
      });
      return;
    }
    clearPrompt();
    setState({
      draftDirty: false,
      lastDraftHash: null,
    });
  }

  function clearDraft() {
    setState({
      lastDraftHash: null,
      draftDirty: false,
    });
    const convoId = getState().conversationMeta?.conversation_id;
    if (convoId) {
      sioCall('conversation_draft', {
        conversation_id: convoId,
        draft: '',
      }).catch(() => {});
    }
  }

  async function syncDraftFromServer(convoId: string | null | undefined) {
    if (!convoId || !promptEl) return;
    if (getState().draftDirty) return;
    try {
      const meta = await sioCall('conversation_get', { conversation_id: convoId });
      if (!meta || meta.ok === false || meta.conversation_id !== convoId) return;
      const serverDraft = meta.draft;
      if (typeof serverDraft !== 'string') return;
      const localText = getPromptDraftText();
      if (serverDraft === localText) return;
      renderPromptFromText(serverDraft);
      const state = getState();
      if (state.conversationMeta) state.conversationMeta.draft = serverDraft;
      setState({
        draftDirty: false,
        lastDraftHash: getDraftHash(serverDraft),
      });
    } catch {
      // ignore
    }
  }

  function initTribute() {
    if (!promptEl || typeof Tribute === 'undefined') return;
    if (tributeInstance) {
      tributeInstance.detach(promptEl);
    }

    tributeInstance = new Tribute({
      trigger: '@',
      allowSpaces: false,
      menuShowMinLength: 1,
      noMatchTemplate: '<li class="tribute-no-match">No files found</li>',
      selectTemplate(item: any) {
        if (!item) return '';
        const absPath = item.original.path || '';
        queueExplicitMentionDraftSave();
        return createMentionToken(absPath).outerHTML;
      },
      menuItemTemplate(item: any) {
        const icon = item.original.type === 'directory' ? '📁' : '📄';
        const typeClass = item.original.type === 'directory' ? 'tribute-dir' : 'tribute-file';
        const cwd = getState().conversationSettings?.cwd || '';
        const relPath = getRelativePath(item.original.path, cwd) || item.original.path || '';
        const safeName = escapeHtml(item.original.name || '');
        const safePath = escapeHtml(relPath);
        return '<div class="' + typeClass + '">' +
          '<div class="tribute-item-name">' + icon + ' ' + safeName + '</div>' +
          '<div class="tribute-item-path">' + safePath + '</div>' +
          '</div>';
      },
      values: async (text: string, cb: (items: any[]) => void) => {
        if (!text || !text.trim()) {
          cb([]);
          return;
        }
        try {
          const cwd = getState().conversationSettings?.cwd || '~';
          const data = await sioCall('fs_search', { query: text, root: cwd, limit: 30 });
          if (!data || data.ok === false) {
            cb([]);
            return;
          }
          cb(data.items || []);
        } catch (err) {
          console.warn('Tribute fetch error:', err);
          cb([]);
        }
      },
      lookup: 'name',
      fillAttr: 'path',
    });

    promptEl.addEventListener('tribute-active-true', () => {
      setTimeout(() => {
        const menu = documentRef.querySelector('.tribute-container ul');
        if (!(menu instanceof HTMLElement)) return;
        const items = menu.querySelectorAll('li');
        let lastWasDir = false;
        let firstFile: Element | null = null;
        items.forEach((item) => {
          const isDir = item.querySelector('.tribute-dir');
          if (lastWasDir && !isDir && !firstFile) {
            firstFile = item;
          }
          lastWasDir = Boolean(isDir);
        });
        if (firstFile && !(firstFile.previousElementSibling instanceof HTMLElement && firstFile.previousElementSibling.classList.contains('tribute-separator'))) {
          const separator = documentRef.createElement('li');
          separator.className = 'tribute-separator';
          separator.innerHTML = '<hr>';
          firstFile.parentNode?.insertBefore(separator, firstFile);
        }
      }, 10);
    });

    tributeInstance.attach(promptEl);

    promptEl.addEventListener('paste', (evt: ClipboardEvent) => {
      evt.preventDefault();
      const clipboardData = evt.clipboardData || (windowRef as any).clipboardData;
      const text = clipboardData?.getData?.('text/plain');
      if (!text) return;
      const selection = windowRef.getSelection?.();
      if (!selection || !selection.rangeCount) return;
      const range = selection.getRangeAt(0);
      range.deleteContents();
      range.insertNode(documentRef.createTextNode(text));
      range.collapse(false);
    });
  }

  function insertMention(path: string, opts: AnyRecord = {}) {
    if (!promptEl || !path) return;
    const token = createMentionToken(path, {
      line: opts.lineNo,
      endLine: opts.endLineNo,
      col: opts.col,
      endCol: opts.endCol,
      content: opts.content,
    });

    const insertTarget = resolveComposerInsertTarget();
    if (insertTarget) {
      const { selection, range } = insertTarget;
      range.deleteContents();
      range.insertNode(token);
      const space = documentRef.createTextNode(' ');
      range.setStartAfter(token);
      range.insertNode(space);
      range.setStartAfter(space);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
      rememberComposerSelection();
    } else {
      promptEl.appendChild(token);
      promptEl.appendChild(documentRef.createTextNode(' '));
      moveCaretToEnd();
    }
    promptEl.focus();
    queueExplicitMentionDraftSave();
  }

  return {
    saveDraftDebounced,
    renderPromptFromText,
    getPromptText,
    getPromptDraftText,
    clearPrompt,
    bindComposerSelectionTracking,
    restoreDraft,
    clearDraft,
    syncDraftFromServer,
    initTribute,
    insertMention,
  };
}
