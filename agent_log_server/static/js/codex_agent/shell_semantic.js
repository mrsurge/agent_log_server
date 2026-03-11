export function bindShellSemantic(ctx) {
  const {
    getEnabled,
    setEnabled,
    getCheckboxEl,
    escapeHtml,
  } = ctx;

  let tsRibbonReady = false;
  let tsRibbonInitPromise = null;
  let tsRibbonParser = null;
  let tsRibbonLang = null;
  let tsRibbonQuery = null;
  const tsRibbonCache = new Map();
  const TS_RIBBON_CACHE_MAX = 500;

  function isSemanticShellRibbonEnabled() {
    return getEnabled() === true;
  }

  function setSemanticShellRibbonEnabled(enabled) {
    const next = enabled === true;
    setEnabled(next);
    const el = getCheckboxEl?.();
    if (el) el.checked = next;
  }

  function normalizeCaptureName(name) {
    const raw = String(name || '').replace(/^@/, '');
    return raw.replace(/[^\w.-]+/g, '-').replace(/\./g, '-');
  }

  function maybeCachePut(key, value) {
    if (!key) return;
    if (tsRibbonCache.has(key)) tsRibbonCache.delete(key);
    tsRibbonCache.set(key, value);
    while (tsRibbonCache.size > TS_RIBBON_CACHE_MAX) {
      const first = tsRibbonCache.keys().next().value;
      tsRibbonCache.delete(first);
    }
  }

  function utf8Len(cp) {
    if (cp <= 0x7F) return 1;
    if (cp <= 0x7FF) return 2;
    if (cp <= 0xFFFF) return 3;
    return 4;
  }

  function buildJsIndexToUtf8ByteOffsets(text) {
    const s = String(text || '');
    const offsets = new Array(s.length + 1);
    let byte = 0;
    for (let i = 0; i < s.length; ) {
      offsets[i] = byte;
      const cp = s.codePointAt(i);
      byte += utf8Len(cp);
      i += cp > 0xFFFF ? 2 : 1;
    }
    offsets[s.length] = byte;
    for (let i = 1; i < offsets.length; i++) {
      if (offsets[i] == null) offsets[i] = offsets[i - 1];
    }
    return offsets;
  }

  function utf8ByteToJsIndex(offsets, byteIndex) {
    const arr = offsets;
    let lo = 0;
    let hi = arr.length - 1;
    const target = Math.max(0, Math.min(Number(byteIndex) || 0, arr[hi] || 0));
    while (lo < hi) {
      const mid = Math.floor((lo + hi + 1) / 2);
      if (arr[mid] <= target) lo = mid;
      else hi = mid - 1;
    }
    return lo;
  }

  async function ensureTreeSitterRibbonReady() {
    if (!isSemanticShellRibbonEnabled()) return false;
    if (tsRibbonReady) return true;
    if (tsRibbonInitPromise) return tsRibbonInitPromise;
    tsRibbonInitPromise = (async () => {
      const mod = await import('/static/vendor/web-tree-sitter/web-tree-sitter.js');
      const Parser = mod?.Parser;
      const Language = mod?.Language;
      const Query = mod?.Query;
      if (!Parser || !Language || !Query) {
        throw new Error('web-tree-sitter module did not export Parser/Language/Query');
      }
      await Parser.init({
        locateFile: (file) => `/static/vendor/web-tree-sitter/${file}`,
      });
      tsRibbonLang = await Language.load('/static/vendor/tree-sitter-bash/tree-sitter-bash.wasm');
      tsRibbonParser = new Parser();
      tsRibbonParser.setLanguage(tsRibbonLang);
      const response = await fetch('/static/vendor/tree-sitter-bash/highlights.scm', { cache: 'no-store' });
      if (!response.ok) throw new Error('failed to load highlights.scm');
      const scm = await response.text();
      tsRibbonQuery = new Query(tsRibbonLang, scm);
      tsRibbonReady = true;
      return true;
    })().catch((err) => {
      console.warn('Tree-sitter ribbon init failed:', err);
      tsRibbonReady = false;
      tsRibbonInitPromise = null;
      return false;
    });
    return tsRibbonInitPromise;
  }

  function splitQuotedSegments(text) {
    const s = String(text || '');
    const segs = [];
    let buf = '';
    let i = 0;

    function pushText() {
      if (buf) {
        segs.push({ type: 'text', text: buf });
        buf = '';
      }
    }

    while (i < s.length) {
      const ch = s[i];
      if (ch !== '\'' && ch !== '"' && ch !== '`') {
        buf += ch;
        i += 1;
        continue;
      }

      const quote = ch;
      pushText();
      i += 1;
      let inner = '';

      while (i < s.length) {
        const c = s[i];
        if (quote === '\'') {
          if (c === '\'') break;
          inner += c;
          i += 1;
          continue;
        }
        if (c === '\\' && i + 1 < s.length) {
          inner += c + s[i + 1];
          i += 2;
          continue;
        }
        if (c === quote) break;
        inner += c;
        i += 1;
      }

      if (i >= s.length || s[i] !== quote) {
        buf += quote + inner;
        break;
      }

      i += 1;
      segs.push({ type: 'quote', quote, text: inner });
    }

    pushText();
    return segs;
  }

  function treeSitterHighlightHtml(text) {
    if (!tsRibbonReady || !tsRibbonParser || !tsRibbonQuery) {
      return escapeHtml(text || '');
    }
    const input = String(text || '');
    if (!input.trim()) return escapeHtml(input);
    const cached = tsRibbonCache.get(input);
    if (cached) return cached;

    let tree;
    try {
      tree = tsRibbonParser.parse(input);
    } catch (_) {
      const escaped = escapeHtml(input);
      maybeCachePut(input, escaped);
      return escaped;
    }

    let captures = [];
    try {
      captures = tsRibbonQuery.captures(tree.rootNode) || [];
    } catch (_) {
      captures = [];
    }

    const offsets = buildJsIndexToUtf8ByteOffsets(input);
    const spans = [];
    for (const cap of captures) {
      const name = cap && (cap.name || cap.capture || cap[0]);
      const node = cap && (cap.node || cap[1]);
      const startB = node?.startIndex;
      const endB = node?.endIndex;
      if (startB == null || endB == null) continue;
      const start = utf8ByteToJsIndex(offsets, startB);
      const end = utf8ByteToJsIndex(offsets, endB);
      if (end <= start) continue;
      spans.push({
        start,
        end,
        cls: `ts-${normalizeCaptureName(name)}`,
        len: end - start,
      });
    }

    spans.sort((a, b) => (a.start - b.start) || (b.len - a.len));
    const picked = [];
    let lastEnd = 0;
    for (const span of spans) {
      if (span.start < lastEnd) continue;
      picked.push(span);
      lastEnd = span.end;
    }

    let out = '';
    let idx = 0;
    for (const span of picked) {
      if (span.start > idx) out += escapeHtml(input.slice(idx, span.start));
      out += `<span class="${span.cls}">${escapeHtml(input.slice(span.start, span.end))}</span>`;
      idx = span.end;
    }
    if (idx < input.length) out += escapeHtml(input.slice(idx));
    maybeCachePut(input, out);
    return out;
  }

  function renderShellCmdRibbon(el, cmd) {
    if (!el) return;
    const command = String(cmd || '');

    const savedTwisty = el.querySelector('.twisty');
    const savedToggle = el.querySelector('.ribbon-toggle-zone');

    if (isSemanticShellRibbonEnabled()) {
      if (!tsRibbonReady && !tsRibbonInitPromise) {
        ensureTreeSitterRibbonReady();
      }
      if (tsRibbonReady) {
        try {
          const segs = splitQuotedSegments(command);
          let html = '';
          for (const seg of segs) {
            if (seg.type === 'text') {
              html += treeSitterHighlightHtml(seg.text);
            } else if (seg.type === 'quote') {
              const q = escapeHtml(seg.quote);
              html += `<span class="ts-quote">${q}</span>`;
              html += `<span class="ts-quoted-inner">${treeSitterHighlightHtml(seg.text)}</span>`;
              html += `<span class="ts-quote">${q}</span>`;
            }
          }
          el.innerHTML = `<span class="shell-prompt">$ </span><code class="tsribbon">${html}</code>`;
          if (savedTwisty) el.appendChild(savedTwisty);
          if (savedToggle) el.appendChild(savedToggle);
          return;
        } catch (_) {
          // Fall through to hljs rendering.
        }
      }
    }

    if (typeof hljs === 'undefined' || !command.trim()) {
      el.textContent = `$ ${command}`;
      if (savedTwisty) el.appendChild(savedTwisty);
      if (savedToggle) el.appendChild(savedToggle);
      return;
    }

    try {
      el.innerHTML = '';
      const prefix = document.createElement('span');
      prefix.className = 'shell-prompt';
      prefix.textContent = '$ ';
      const codeEl = document.createElement('code');
      codeEl.className = 'language-bash';
      codeEl.textContent = command;
      el.append(prefix, codeEl);
      hljs.highlightElement(codeEl);
    } catch (_) {
      el.textContent = `$ ${command}`;
    }

    if (savedTwisty) el.appendChild(savedTwisty);
    if (savedToggle) el.appendChild(savedToggle);
  }

  return {
    isSemanticShellRibbonEnabled,
    setSemanticShellRibbonEnabled,
    ensureTreeSitterRibbonReady,
    renderShellCmdRibbon,
  };
}
