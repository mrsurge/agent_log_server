declare const hljs: any;

type DiffBlockState = {
  text: string;
  path: string;
};

type DiffBlockElement = HTMLElement & {
  __diffRender?: DiffBlockState;
};

export function bindDiffRendering(ctx) {
  const {
    getDiffRow,
    createRow,
    escapeHtml,
    toRelativePath,
    isDiffSyntaxEnabled,
    detectLangFromPath,
    resolveHljsLanguage,
    setLastEventType,
    maybeAutoScroll,
    timelineEl,
    postTe2OpenRequest,
  } = ctx;
  const JSDIFF_CDN_URL = 'https://cdn.jsdelivr.net/npm/diff@8.0.2/+esm';
  const DIFF_RENDER_MODE_STORAGE_KEY = 'codex_diff_render_mode';
  const DIFF_RENDER_MODE_AUTO = 'auto';
  const DIFF_RENDER_MODE_HEURISTIC_ONLY = 'heuristic-only';
  let jsDiffApi = null;
  let jsDiffLoadPromise = null;
  let diffRenderMode = readStoredDiffRenderMode();

  function normalizeDiffRenderMode(value) {
    const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
    if (
      normalized === DIFF_RENDER_MODE_HEURISTIC_ONLY
      || normalized === 'heuristic'
      || normalized === 'fallback'
      || normalized === 'off'
      || normalized === 'disable-jsdiff'
    ) {
      return DIFF_RENDER_MODE_HEURISTIC_ONLY;
    }
    return DIFF_RENDER_MODE_AUTO;
  }

  function readStoredDiffRenderMode() {
    if (typeof window === 'undefined' || !window.localStorage) return DIFF_RENDER_MODE_AUTO;
    try {
      return normalizeDiffRenderMode(window.localStorage.getItem(DIFF_RENDER_MODE_STORAGE_KEY));
    } catch {
      return DIFF_RENDER_MODE_AUTO;
    }
  }

  function persistDiffRenderMode(mode) {
    if (typeof window === 'undefined' || !window.localStorage) return;
    try {
      window.localStorage.setItem(DIFF_RENDER_MODE_STORAGE_KEY, mode);
    } catch {}
  }

  function isJsDiffEnabled() {
    return diffRenderMode !== DIFF_RENDER_MODE_HEURISTIC_ONLY;
  }

  function hasJsDiffApi(value) {
    return Boolean(
      value
      && typeof value.diffWords === 'function'
      && typeof value.diffChars === 'function',
    );
  }

  function getJsDiffApi() {
    if (!isJsDiffEnabled()) return null;
    return hasJsDiffApi(jsDiffApi) ? jsDiffApi : null;
  }

  function getDiffRenderState() {
    return {
      mode: diffRenderMode,
      jsDiffEnabled: isJsDiffEnabled(),
      jsDiffLoaded: hasJsDiffApi(jsDiffApi),
      jsDiffLoading: Boolean(jsDiffLoadPromise),
      jsDiffActive: Boolean(getJsDiffApi()),
      jsDiffCdnUrl: JSDIFF_CDN_URL,
    };
  }

  function renderDiffBlock(block, text, path) {
    if (!(block instanceof HTMLElement)) return;
    const diffBlock = block as DiffBlockElement;
    diffBlock.__diffRender = {
      text: typeof text === 'string' ? text : '',
      path: typeof path === 'string' ? path : '',
    };
    diffBlock.innerHTML = formatDiff(text || '', path);
  }

  function rerenderKnownDiffBlocks() {
    if (typeof document === 'undefined') return;
    document.querySelectorAll('.diff-block').forEach((node) => {
      if (!(node instanceof HTMLElement)) return;
      const state = (node as DiffBlockElement).__diffRender;
      if (!state || typeof state !== 'object') return;
      node.innerHTML = formatDiff(state.text || '', state.path || '');
    });
  }

  function preloadJsDiff() {
    if (getJsDiffApi() || jsDiffLoadPromise) return jsDiffLoadPromise;
    if (typeof document === 'undefined' || typeof window === 'undefined') return null;
    if (!isJsDiffEnabled()) return null;
    jsDiffLoadPromise = import(JSDIFF_CDN_URL)
      .then((mod) => {
        if (!hasJsDiffApi(mod)) return null;
        jsDiffApi = mod;
        rerenderKnownDiffBlocks();
        return jsDiffApi;
      })
      .catch((error) => {
        jsDiffLoadPromise = null;
        console.warn('[diff] Failed to load jsdiff CDN module; using fallback intraline logic.', error);
        return null;
      });
    return jsDiffLoadPromise;
  }

  function setDiffRenderMode(nextMode) {
    diffRenderMode = normalizeDiffRenderMode(nextMode);
    persistDiffRenderMode(diffRenderMode);
    rerenderKnownDiffBlocks();
    if (isJsDiffEnabled()) {
      preloadJsDiff();
    }
    return getDiffRenderState();
  }

  function addDiff(id, text, path, parentEl) {
    const entry = getDiffRow(id, path, parentEl);
    renderDiffBlock(entry.block, text || '', path);
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
    const block = document.createElement('div');
    block.className = 'diff-block';
    renderDiffBlock(block, text || '', path);
    body.appendChild(block);
    setLastEventType('diff');
    maybeAutoScroll();
  }

  function tokenizeDiffWords(text) {
    if (typeof text !== 'string' || !text) return [];
    return text.match(/(\s+|[A-Za-z0-9_]+|[^A-Za-z0-9_\s]+)/g) || Array.from(text);
  }

  function tokenizeDiffChars(text) {
    return Array.from(text || '');
  }

  function tokenizeLineAlignmentTokens(text) {
    return tokenizeDiffWords(text).filter((token) => token && !/^\s+$/.test(token));
  }

  function buildLcsTable(leftTokens, rightTokens) {
    const table = Array.from({ length: leftTokens.length + 1 }, () => Array(rightTokens.length + 1).fill(0));
    for (let leftIdx = leftTokens.length - 1; leftIdx >= 0; leftIdx -= 1) {
      for (let rightIdx = rightTokens.length - 1; rightIdx >= 0; rightIdx -= 1) {
        if (leftTokens[leftIdx] === rightTokens[rightIdx]) {
          table[leftIdx][rightIdx] = table[leftIdx + 1][rightIdx + 1] + 1;
        } else {
          table[leftIdx][rightIdx] = Math.max(table[leftIdx + 1][rightIdx], table[leftIdx][rightIdx + 1]);
        }
      }
    }
    return table;
  }

  function buildWeightedLcsTable(leftTokens, rightTokens) {
    const table = Array.from({ length: leftTokens.length + 1 }, () => Array(rightTokens.length + 1).fill(0));
    for (let leftIdx = leftTokens.length - 1; leftIdx >= 0; leftIdx -= 1) {
      for (let rightIdx = rightTokens.length - 1; rightIdx >= 0; rightIdx -= 1) {
        if (leftTokens[leftIdx] === rightTokens[rightIdx]) {
          table[leftIdx][rightIdx] = leftTokens[leftIdx].length + table[leftIdx + 1][rightIdx + 1];
        } else {
          table[leftIdx][rightIdx] = Math.max(table[leftIdx + 1][rightIdx], table[leftIdx][rightIdx + 1]);
        }
      }
    }
    return table;
  }

  function diffTokenSequences(leftTokens, rightTokens) {
    const table = buildLcsTable(leftTokens, rightTokens);
    const ops = [];
    let leftIdx = 0;
    let rightIdx = 0;
    while (leftIdx < leftTokens.length && rightIdx < rightTokens.length) {
      if (leftTokens[leftIdx] === rightTokens[rightIdx]) {
        ops.push({ type: 'common', value: leftTokens[leftIdx] });
        leftIdx += 1;
        rightIdx += 1;
        continue;
      }
      if (table[leftIdx + 1][rightIdx] >= table[leftIdx][rightIdx + 1]) {
        ops.push({ type: 'del', value: leftTokens[leftIdx] });
        leftIdx += 1;
      } else {
        ops.push({ type: 'add', value: rightTokens[rightIdx] });
        rightIdx += 1;
      }
    }
    while (leftIdx < leftTokens.length) {
      ops.push({ type: 'del', value: leftTokens[leftIdx] });
      leftIdx += 1;
    }
    while (rightIdx < rightTokens.length) {
      ops.push({ type: 'add', value: rightTokens[rightIdx] });
      rightIdx += 1;
    }
    return ops;
  }

  function sumTokenWeights(tokens) {
    return tokens.reduce((total, token) => total + token.length, 0);
  }

  function measureCharSimilarity(leftText, rightText) {
    const leftChars = tokenizeDiffChars(leftText);
    const rightChars = tokenizeDiffChars(rightText);
    if (!leftChars.length || !rightChars.length) return 0;
    const sharedChars = buildLcsTable(leftChars, rightChars)[0][0];
    return sharedChars / Math.max(leftChars.length, rightChars.length, 1);
  }

  function shouldRefineCharLevel(leftText, rightText) {
    if (!leftText || !rightText) return false;
    if (leftText.length > 160 || rightText.length > 160) return false;
    if (!/\S/.test(leftText) || !/\S/.test(rightText)) return false;
    return measureCharSimilarity(leftText, rightText) >= 0.5;
  }

  function measureLinePairSimilarity(leftText, rightText) {
    const leftTokens = tokenizeLineAlignmentTokens(leftText);
    const rightTokens = tokenizeLineAlignmentTokens(rightText);
    if (!leftTokens.length || !rightTokens.length) return 0;
    const sharedWeight = buildWeightedLcsTable(leftTokens, rightTokens)[0][0];
    const totalWeight = Math.max(sumTokenWeights(leftTokens), sumTokenWeights(rightTokens), 1);
    return sharedWeight / totalWeight;
  }

  function alignChangedLineRuns(deletions, additions, threshold = 0.45) {
    if (!deletions.length || !additions.length) return [];
    const scores = deletions.map((deletion) => additions.map((addition) => measureLinePairSimilarity(deletion.display || '', addition.display || '')));
    const rowCount = deletions.length;
    const colCount = additions.length;
    const dp = Array.from({ length: rowCount + 1 }, () => Array(colCount + 1).fill(0));

    for (let rowIdx = rowCount - 1; rowIdx >= 0; rowIdx -= 1) {
      for (let colIdx = colCount - 1; colIdx >= 0; colIdx -= 1) {
        const benefit = scores[rowIdx][colIdx] - threshold;
        const skipDeletion = dp[rowIdx + 1][colIdx];
        const skipAddition = dp[rowIdx][colIdx + 1];
        const match = benefit > 0 ? benefit + dp[rowIdx + 1][colIdx + 1] : Number.NEGATIVE_INFINITY;
        dp[rowIdx][colIdx] = Math.max(skipDeletion, skipAddition, match);
      }
    }

    const pairs = [];
    let rowIdx = 0;
    let colIdx = 0;
    while (rowIdx < rowCount && colIdx < colCount) {
      const benefit = scores[rowIdx][colIdx] - threshold;
      const skipDeletion = dp[rowIdx + 1][colIdx];
      const skipAddition = dp[rowIdx][colIdx + 1];
      const match = benefit > 0 ? benefit + dp[rowIdx + 1][colIdx + 1] : Number.NEGATIVE_INFINITY;
      if (match > Number.NEGATIVE_INFINITY && match >= skipDeletion && match >= skipAddition) {
        pairs.push({
          deletionIndex: rowIdx,
          additionIndex: colIdx,
          similarity: scores[rowIdx][colIdx],
        });
        rowIdx += 1;
        colIdx += 1;
        continue;
      }
      if (skipDeletion >= skipAddition) {
        rowIdx += 1;
      } else {
        colIdx += 1;
      }
    }
    return pairs;
  }

  function appendDiffPart(parts, text, changed) {
    if (!text) return;
    const last = parts[parts.length - 1];
    if (last && last.changed === changed) {
      last.text += text;
      return;
    }
    parts.push({ text, changed });
  }

  function mergeDiffParts(targetParts, sourceParts) {
    sourceParts.forEach((part) => appendDiffPart(targetParts, part.text, part.changed));
  }

  function buildIntralineParts(leftText, rightText, allowCharRefine = true) {
    const leftParts = [];
    const rightParts = [];
    const ops = diffTokenSequences(tokenizeDiffWords(leftText), tokenizeDiffWords(rightText));
    const pendingLeft = [];
    const pendingRight = [];

    function flushPending() {
      const leftPendingText = pendingLeft.join('');
      const rightPendingText = pendingRight.join('');
      pendingLeft.length = 0;
      pendingRight.length = 0;
      if (!leftPendingText && !rightPendingText) return;
      const shouldRefineChars = allowCharRefine && shouldRefineCharLevel(leftPendingText, rightPendingText);
      if (shouldRefineChars) {
        const refined = buildIntralineParts(leftPendingText, rightPendingText, false);
        if (refined) {
          mergeDiffParts(leftParts, refined.leftParts);
          mergeDiffParts(rightParts, refined.rightParts);
          return;
        }
      }
      appendDiffPart(leftParts, leftPendingText, true);
      appendDiffPart(rightParts, rightPendingText, true);
    }

    const tokenOps = allowCharRefine ? ops : diffTokenSequences(tokenizeDiffChars(leftText), tokenizeDiffChars(rightText));
    tokenOps.forEach((op) => {
      if (op.type === 'common') {
        flushPending();
        appendDiffPart(leftParts, op.value, false);
        appendDiffPart(rightParts, op.value, false);
        return;
      }
      if (op.type === 'del') {
        pendingLeft.push(op.value);
        return;
      }
      pendingRight.push(op.value);
    });
    flushPending();

    const unchangedChars = leftParts.reduce((total, part) => total + (part.changed ? 0 : part.text.length), 0);
    const totalChars = Math.max(leftText.length, rightText.length, 1);
    const hasChangedSegments = leftParts.some((part) => part.changed) || rightParts.some((part) => part.changed);
    if (!hasChangedSegments || unchangedChars < 2 || unchangedChars / totalChars < 0.2) {
      return null;
    }
    return { leftParts, rightParts };
  }

  function buildJsDiffCharParts(leftText, rightText) {
    const api = getJsDiffApi();
    if (!api) return null;
    const leftParts = [];
    const rightParts = [];
    let hasChangedSegments = false;
    api.diffChars(leftText, rightText).forEach((change) => {
      const value = typeof change?.value === 'string' ? change.value : '';
      if (!value) return;
      if (change.added) {
        appendDiffPart(rightParts, value, true);
        hasChangedSegments = true;
        return;
      }
      if (change.removed) {
        appendDiffPart(leftParts, value, true);
        hasChangedSegments = true;
        return;
      }
      appendDiffPart(leftParts, value, false);
      appendDiffPart(rightParts, value, false);
    });
    return hasChangedSegments ? { leftParts, rightParts } : null;
  }

  function buildJsDiffIntralineParts(leftText, rightText) {
    const api = getJsDiffApi();
    if (!api) return null;
    const leftParts = [];
    const rightParts = [];
    let pendingLeft = '';
    let pendingRight = '';

    function flushPending() {
      const leftPendingText = pendingLeft;
      const rightPendingText = pendingRight;
      pendingLeft = '';
      pendingRight = '';
      if (!leftPendingText && !rightPendingText) return;
      const shouldRefineChars = shouldRefineCharLevel(leftPendingText, rightPendingText);
      if (shouldRefineChars) {
        const refined = buildJsDiffCharParts(leftPendingText, rightPendingText);
        if (refined) {
          mergeDiffParts(leftParts, refined.leftParts);
          mergeDiffParts(rightParts, refined.rightParts);
          return;
        }
      }
      appendDiffPart(leftParts, leftPendingText, true);
      appendDiffPart(rightParts, rightPendingText, true);
    }

    api.diffWords(leftText, rightText).forEach((change) => {
      const value = typeof change?.value === 'string' ? change.value : '';
      if (!value) return;
      if (change.added) {
        pendingRight += value;
        return;
      }
      if (change.removed) {
        pendingLeft += value;
        return;
      }
      flushPending();
      appendDiffPart(leftParts, value, false);
      appendDiffPart(rightParts, value, false);
    });
    flushPending();

    const unchangedChars = leftParts.reduce((total, part) => total + (part.changed ? 0 : part.text.length), 0);
    const totalChars = Math.max(leftText.length, rightText.length, 1);
    const hasChangedSegments = leftParts.some((part) => part.changed) || rightParts.some((part) => part.changed);
    if (!hasChangedSegments || unchangedChars < 2 || unchangedChars / totalChars < 0.2) {
      return null;
    }
    return { leftParts, rightParts };
  }

  function renderDiffPartsHtml(parts, emphasisClass) {
    return parts.map((part) => {
      const safeText = escapeHtml(part.text);
      return part.changed ? `<span class="${emphasisClass}">${safeText}</span>` : safeText;
    }).join('');
  }

  function hasMeaningfulChangedContent(parts) {
    return parts.some((part) => part.changed && /\S/.test(part.text));
  }

  function renderDiffCodeHtml(display, activePath) {
    let codeHtml = escapeHtml(display);
    if (isDiffSyntaxEnabled() && typeof hljs !== 'undefined' && display.trim()) {
      try {
        const langHint = detectLangFromPath?.(activePath) || null;
        const lang = resolveHljsLanguage?.(langHint) || null;
        if (lang) {
          codeHtml = hljs.highlight(display, { language: lang, ignoreIllegals: true }).value;
        } else if (display.length > 10) {
          const auto = hljs.highlightAuto(display);
          if (auto.relevance > 3) {
            codeHtml = auto.value;
          }
        }
      } catch (_) {}
    }
    return codeHtml;
  }

  function applyIntralineHighlights(rows) {
    let idx = 0;
    while (idx < rows.length) {
      const row = rows[idx];
      if (!row || row.kind !== 'code' || row.cls !== 'diff-del') {
        idx += 1;
        continue;
      }

      const deletions = [];
      while (idx < rows.length && rows[idx]?.kind === 'code' && rows[idx].cls === 'diff-del') {
        deletions.push(rows[idx]);
        idx += 1;
      }

      const additions = [];
      let addIdx = idx;
      while (addIdx < rows.length && rows[addIdx]?.kind === 'code' && rows[addIdx].cls === 'diff-add') {
        additions.push(rows[addIdx]);
        addIdx += 1;
      }

      if (!additions.length) {
        continue;
      }

      const alignedPairs = alignChangedLineRuns(deletions, additions);
      alignedPairs.forEach(({ deletionIndex, additionIndex }) => {
        const deletion = deletions[deletionIndex];
        const addition = additions[additionIndex];
        const pair = buildJsDiffIntralineParts(deletion.display, addition.display)
          || buildIntralineParts(deletion.display, addition.display);
        if (!pair) return;
        if (!hasMeaningfulChangedContent(pair.leftParts) && !hasMeaningfulChangedContent(pair.rightParts)) return;
        deletion.codeHtml = renderDiffPartsHtml(pair.leftParts, 'diff-intraline-change diff-intraline-del');
        addition.codeHtml = renderDiffPartsHtml(pair.rightParts, 'diff-intraline-change diff-intraline-add');
      });

      idx = addIdx;
    }
  }

  function renderDiffRowHtml(row) {
    const codeHtml = row.codeHtml || renderDiffCodeHtml(row.display || '', row.activePath || '');
    const safePath = row.path ? escapeHtml(String(row.path)) : '';
    const safeOldLine = escapeHtml(String(row.oldLine || ''));
    const safeNewLine = escapeHtml(String(row.newLine || ''));
    return `<tr class="diff-line ${row.cls}" data-path="${safePath}" data-old-line="${safeOldLine}" data-new-line="${safeNewLine}"><td class="diff-gutter transcript-line-no">${escapeHtml(row.gutterText || '')}</td><td class="diff-text">${codeHtml}</td></tr>`;
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
    const rows = [];
    text.split('\n').forEach((line) => {
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
        if (!showFileHeaders) return;
        const relLabel = currentFilePath ? (toRelativePath(currentFilePath) || currentFilePath) : 'file';
        rows.push({
          kind: 'file',
          cls: 'diff-file',
          path: currentFilePath || '',
          oldLine: '',
          newLine: '',
          gutterText: fileGutter,
          display: '',
          codeHtml: `<strong>${escapeHtml(relLabel)}</strong>`,
        });
        return;
      }

      if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('index ') || line.startsWith('new file mode') || line.startsWith('deleted file mode') || line.startsWith('similarity index') || line.startsWith('rename from') || line.startsWith('rename to')) {
        return;
      }

      const activePath = currentFilePath || filePath || '';

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
        rows.push({
          kind: 'meta',
          cls,
          path: activePath,
          oldLine: String(oldLine || ''),
          newLine: String(newLine || ''),
          gutterText: hunkGutter,
          display,
          codeHtml: escapeHtml(display),
          activePath,
        });
        return;
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
      rows.push({
        kind: 'code',
        cls,
        path: activePath,
        oldLine: String(oldNo || ''),
        newLine: String(newNo || ''),
        gutterText,
        display,
        activePath,
        codeHtml: '',
      });
    });
    if (!rows.length) return '';
    applyIntralineHighlights(rows);
    return `<table class="diff-table" role="presentation"><tbody>${rows.map((row) => renderDiffRowHtml(row)).join('')}</tbody></table>`;
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

  preloadJsDiff();

  return {
    addDiff,
    addDeclinedDiff,
    formatDiff,
    renderDiffBlock,
    getDiffRenderState,
    setDiffRenderMode,
    bindDiffClickHandler,
  };
}
