export function bindDiffRendering(ctx) {
  const {
    getDiffRow,
    createRow,
    escapeHtml,
    toRelativePath,
    isDiffSyntaxEnabled,
    setLastEventType,
    maybeAutoScroll,
    timelineEl,
    postTe2OpenRequest,
  } = ctx;

  function addDiff(id, text, path, parentEl) {
    const entry = getDiffRow(id, path, parentEl);
    entry.pre.innerHTML = formatDiff(text || '', path);
    setLastEventType('diff');
    maybeAutoScroll();
  }

  function addDeclinedDiff(id, text, path) {
    const { row, body } = createRow('diff', 'diff-declined');
    row.classList.add('declined');
    if (path) {
      const pathLabel = document.createElement('div');
      pathLabel.className = 'declined-label';
      pathLabel.innerHTML = `<strong>DECLINED:</strong> ${escapeHtml(toRelativePath(path))}`;
      body.appendChild(pathLabel);
    }
    const pre = document.createElement('pre');
    pre.className = 'diff-block';
    pre.innerHTML = formatDiff(text || '', path);
    body.appendChild(pre);
    setLastEventType('diff');
    maybeAutoScroll();
  }

  function formatDiff(text, filePath) {
    if (!text) return '';
    const diffGitCount = (text.match(/^diff --git /gm) || []).length;
    const showFileHeaders = diffGitCount > 1 || !filePath;
    let oldLine = 0;
    let newLine = 0;
    let maxOldLen = 1;
    let maxNewLen = 1;

    text.split('\n').forEach((line) => {
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
        if (match) {
          oldLine = parseInt(match[1], 10);
          const oldCount = parseInt(match[2] || '1', 10);
          newLine = parseInt(match[3], 10);
          const newCount = parseInt(match[4] || '1', 10);
          maxOldLen = Math.max(maxOldLen, String(oldLine + oldCount).length);
          maxNewLen = Math.max(maxNewLen, String(newLine + newCount).length);
        }
      }
    });

    oldLine = 0;
    newLine = 0;
    let currentFilePath = filePath || null;
    const fileGutter = ''.padStart(maxOldLen, ' ') + '│' + ''.padStart(maxNewLen, ' ') + ' ';

    return text.split('\n').map((line) => {
      let cls = 'diff-context';
      let display = line;
      let changeMarker = ' ';
      let oldNo = '';
      let newNo = '';

      if (line.startsWith('diff --git ')) {
        const parts = line.split(/\s+/);
        if (parts.length >= 4) {
          let bpath = parts[3];
          if (bpath.startsWith('b/')) bpath = bpath.slice(2);
          currentFilePath = bpath || currentFilePath;
        }
        oldLine = 0;
        newLine = 0;
        if (!showFileHeaders) return '';
        const relLabel = currentFilePath ? (toRelativePath(currentFilePath) || currentFilePath) : 'file';
        const safePath = currentFilePath ? escapeHtml(String(currentFilePath)) : '';
        return `<span class="diff-line diff-file" data-path="${safePath}" data-old-line="" data-new-line=""><span class="diff-gutter">${escapeHtml(fileGutter)}</span><span class="diff-text"><strong>${escapeHtml(relLabel)}</strong></span></span>`;
      }

      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('index ') || line.startsWith('new file mode') || line.startsWith('deleted file mode') || line.startsWith('similarity index') || line.startsWith('rename from') || line.startsWith('rename to')) {
        return '';
      }

      const activePath = currentFilePath || filePath || '';
      const safePath = activePath ? escapeHtml(String(activePath)) : '';

      if (line.startsWith('@@')) {
        cls = 'diff-hunk';
        const match = line.match(/@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)/);
        if (match) {
          const oldStart = parseInt(match[1], 10);
          const oldCount = parseInt(match[2] || '1', 10);
          const newStart = parseInt(match[3], 10);
          const newCount = parseInt(match[4] || '1', 10);
          const oldEnd = Math.max(oldStart, oldStart + oldCount - 1);
          const newEnd = Math.max(newStart, newStart + newCount - 1);
          const oldRange = oldCount === 1 ? `${oldStart}` : `${oldStart}-${oldEnd}`;
          const newRange = newCount === 1 ? `${newStart}` : `${newStart}-${newEnd}`;
          const label = match[5] && match[5].trim() ? ` ${match[5].trim()}` : '';
          display = `Lines ${oldRange} → ${newRange}${label}`;
          oldLine = oldStart;
          newLine = newStart;
        }
        const hunkGutter = ''.padStart(maxOldLen, ' ') + '│' + ''.padStart(maxNewLen, ' ') + ' ';
        return `<span class="diff-line ${cls}" data-path="${safePath}" data-old-line="${escapeHtml(String(oldLine || ''))}" data-new-line="${escapeHtml(String(newLine || ''))}"><span class="diff-gutter">${escapeHtml(hunkGutter)}</span><span class="diff-text">${escapeHtml(display)}</span></span>`;
      } else if (line.startsWith('+') && !line.startsWith('+++')) {
        cls = 'diff-add';
        changeMarker = '+';
        newNo = String(newLine);
        newLine += 1;
        display = line.slice(1);
      } else if (line.startsWith('-') && !line.startsWith('---')) {
        cls = 'diff-del';
        changeMarker = '-';
        oldNo = String(oldLine);
        oldLine += 1;
        display = line.slice(1);
      } else if (line.startsWith(' ')) {
        changeMarker = ' ';
        oldNo = String(oldLine);
        newNo = String(newLine);
        oldLine += 1;
        newLine += 1;
        display = line.slice(1);
      } else {
        display = line;
      }

      const padOld = oldNo ? oldNo.padStart(maxOldLen, ' ') : ''.padStart(maxOldLen, ' ');
      const padNew = newNo ? newNo.padStart(maxNewLen, ' ') : ''.padStart(maxNewLen, ' ');
      const gutterText = `${padOld}│${padNew}${changeMarker} `;

      let codeHtml = escapeHtml(display);
      if (isDiffSyntaxEnabled() && typeof hljs !== 'undefined' && display.trim()) {
        try {
          const ext = activePath ? activePath.split('.').pop()?.toLowerCase() : '';
          const extToLang = {
            py: 'python', js: 'javascript', ts: 'typescript', tsx: 'typescript',
            jsx: 'javascript', rb: 'ruby', rs: 'rust', go: 'go', sh: 'bash',
            yml: 'yaml', md: 'markdown', htm: 'html',
          };
          const lang = extToLang[ext] || ext;
          if (lang && hljs.getLanguage(lang)) {
            codeHtml = hljs.highlight(display, { language: lang, ignoreIllegals: true }).value;
          } else if (display.length > 10) {
            const auto = hljs.highlightAuto(display);
            if (auto.relevance > 3) {
              codeHtml = auto.value;
            }
          }
        } catch (_) {}
      }

      const dataOld = escapeHtml(String(oldNo || ''));
      const dataNew = escapeHtml(String(newNo || ''));
      return `<span class="diff-line ${cls}" data-path="${safePath}" data-old-line="${dataOld}" data-new-line="${dataNew}"><span class="diff-gutter">${escapeHtml(gutterText)}</span><span class="diff-text">${codeHtml}</span></span>`;
    }).filter(line => line !== '').join('');
  }

  function bindDiffClickHandler() {
    timelineEl?.addEventListener('click', (evt) => {
      const target = evt.target;
      if (!(target instanceof HTMLElement)) return;
      const lineEl = target.closest('.diff-line');
      if (!(lineEl instanceof HTMLElement)) return;
      const path = lineEl.getAttribute('data-path') || '';
      const newLine = lineEl.getAttribute('data-new-line') || '';
      const oldLine = lineEl.getAttribute('data-old-line') || '';
      const line = parseInt(newLine || oldLine, 10);
      if (!path || !Number.isFinite(line)) return;
      if (line <= 0) return;
      try {
        lineEl.classList.add('tap-flash');
        setTimeout(() => lineEl.classList.remove('tap-flash'), 180);
      } catch {}
      postTe2OpenRequest({ path, line, column: 1 });
    });
  }

  return {
    addDiff,
    addDeclinedDiff,
    formatDiff,
    bindDiffClickHandler,
  };
}

