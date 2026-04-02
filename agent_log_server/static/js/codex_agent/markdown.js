import * as smd from '/static/vendor/streaming-markdown/smd.min.js';

let markdownLinkHandlers = {
  openFilePath: null,
  openExternalUrl: null,
};

function parseLineColumnHash(hash) {
  const text = typeof hash === 'string' ? hash.trim() : '';
  const match = text.match(/^#L(\d+)(?:C(\d+))?$/i);
  if (!match) return null;
  const line = Number.parseInt(match[1], 10);
  const column = match[2] ? Number.parseInt(match[2], 10) : null;
  if (!Number.isFinite(line) || line < 1) return null;
  return {
    line,
    column: Number.isFinite(column) && column >= 1 ? column : null,
  };
}

function parseLineColumnSuffix(pathText) {
  const text = typeof pathText === 'string' ? pathText.trim() : '';
  const match = text.match(/^(.*?):(\d+)(?::(\d+))?$/);
  if (!match) return null;
  const line = Number.parseInt(match[2], 10);
  const column = match[3] ? Number.parseInt(match[3], 10) : null;
  if (!Number.isFinite(line) || line < 1) return null;
  return {
    path: match[1] || '',
    line,
    column: Number.isFinite(column) && column >= 1 ? column : null,
  };
}

function normalizeMarkdownHref(rawHref) {
  return typeof rawHref === 'string' ? rawHref.trim() : '';
}

function isExternalHttpUrl(rawHref) {
  return /^https?:\/\//i.test(rawHref);
}

function isFileUrl(rawHref) {
  return /^file:\/\//i.test(rawHref);
}

function decodeFileUrlPath(rawHref) {
  try {
    const url = new URL(rawHref);
    return decodeURIComponent(url.pathname || '');
  } catch {
    return '';
  }
}

function decodePathLikeHref(rawHref) {
  try {
    return decodeURIComponent(rawHref);
  } catch {
    return rawHref;
  }
}

function normalizeParsedFileTarget(path, line = null, column = null) {
  const normalizedPath = typeof path === 'string' ? path.trim() : '';
  if (!normalizedPath) return null;
  return {
    path: normalizedPath,
    line: Number.isFinite(line) && line >= 1 ? line : null,
    column: Number.isFinite(column) && column >= 1 ? column : null,
  };
}

function parseFileTargetFromAnchor(anchor, rawHref) {
  if (!(anchor instanceof HTMLAnchorElement)) return null;
  const decodedHref = decodePathLikeHref(rawHref);
  if (isFileUrl(decodedHref)) {
    const hashParts = parseLineColumnHash(anchor.hash);
    return normalizeParsedFileTarget(
      decodeFileUrlPath(decodedHref),
      hashParts?.line ?? null,
      hashParts?.column ?? null,
    );
  }

  const hashParts = parseLineColumnHash(anchor.hash);
  const basePath = decodePathLikeHref(anchor.pathname || decodedHref.split('#', 1)[0] || '');
  if (hashParts) {
    return normalizeParsedFileTarget(basePath, hashParts.line, hashParts.column);
  }

  const suffixParts = parseLineColumnSuffix(decodedHref);
  if (suffixParts) {
    return normalizeParsedFileTarget(suffixParts.path, suffixParts.line, suffixParts.column);
  }

  return normalizeParsedFileTarget(basePath || decodedHref);
}

function isLikelyFilePath(rawHref) {
  if (!rawHref) return false;
  if (rawHref.startsWith('#')) return false;
  if (isExternalHttpUrl(rawHref)) return false;
  if (isFileUrl(rawHref)) return true;
  if (/^[a-z][a-z0-9+.-]*:/i.test(rawHref)) return false;
  return true;
}

function bindMarkdownLinkRouting(container) {
  if (!container || container.dataset.markdownLinkRoutingBound === 'true') return;
  container.dataset.markdownLinkRoutingBound = 'true';
  container.addEventListener('click', (evt) => {
    const target = evt.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest('a[href]');
    if (!(anchor instanceof HTMLAnchorElement) || !container.contains(anchor)) return;
    const rawHref = normalizeMarkdownHref(anchor.getAttribute('href'));
    if (!rawHref) return;

    if (isExternalHttpUrl(rawHref) && typeof markdownLinkHandlers.openExternalUrl === 'function') {
      evt.preventDefault();
      evt.stopPropagation();
      markdownLinkHandlers.openExternalUrl(rawHref, anchor);
      return;
    }

    if (isLikelyFilePath(rawHref) && typeof markdownLinkHandlers.openFilePath === 'function') {
      const fileTarget = parseFileTargetFromAnchor(anchor, rawHref);
      if (!fileTarget?.path) return;
      evt.preventDefault();
      evt.stopPropagation();
      markdownLinkHandlers.openFilePath(fileTarget, anchor);
    }
  });
}

export function setMarkdownLinkHandlers(handlers = {}) {
  markdownLinkHandlers = {
    openFilePath: typeof handlers.openFilePath === 'function' ? handlers.openFilePath : null,
    openExternalUrl: typeof handlers.openExternalUrl === 'function' ? handlers.openExternalUrl : null,
  };
}

export function highlightCode(container) {
  if (!container) return;
  container.querySelectorAll('pre code').forEach((block) => {
    if (typeof hljs !== 'undefined') {
      hljs.highlightElement(block);
    }
  });
}

export function renderMarkdownInto(container, text) {
  const renderer = smd.default_renderer(container);
  const parser = smd.parser(renderer);
  bindMarkdownLinkRouting(container);
  smd.parser_write(parser, text || '');
  smd.parser_end(parser);
}

export function renderMarkdownSourceInto(container, text) {
  if (!container) return;
  container.textContent = '';
  const pre = document.createElement('pre');
  const code = document.createElement('code');
  code.className = 'language-markdown';
  code.textContent = text || '';
  pre.appendChild(code);
  container.appendChild(pre);
  highlightCode(container);
}

function escapeHtmlText(text) {
  const value = document.createElement('div');
  value.textContent = text || '';
  return value.innerHTML;
}

function renderHighlightedFenceHtml(source, lang) {
  const rawLang = typeof lang === 'string' ? lang.trim() : '';
  const requestedLangRaw = rawLang ? rawLang.split(/\s+/, 1)[0].toLowerCase() : '';
  const languageAliasMap = {
    sh: 'bash',
    shell: 'bash',
    zsh: 'bash',
  };
  const requestedLang = languageAliasMap[requestedLangRaw] || requestedLangRaw;
  if (typeof hljs === 'undefined') {
    const languageClass = requestedLang ? ` language-${requestedLang}` : '';
    return `<pre><code class="hljs${languageClass}">${escapeHtmlText(source)}</code></pre>`;
  }

  try {
    if (requestedLang && hljs.getLanguage(requestedLang)) {
      const highlighted = hljs.highlight(source, { language: requestedLang, ignoreIllegals: true });
      const languageClass = highlighted.language ? ` language-${highlighted.language}` : ` language-${requestedLang}`;
      return `<pre><code class="hljs${languageClass}">${highlighted.value}</code></pre>`;
    }

    const auto = hljs.highlightAuto(source);
    const languageClass = auto.language ? ` language-${auto.language}` : (requestedLang ? ` language-${requestedLang}` : '');
    return `<pre><code class="hljs${languageClass}">${auto.value}</code></pre>`;
  } catch (_) {
    const languageClass = requestedLang ? ` language-${requestedLang}` : '';
    return `<pre><code class="hljs${languageClass}">${escapeHtmlText(source)}</code></pre>`;
  }
}

function emphasisTokenizeAsteriskOnly(state, silent) {
  const start = state.pos;
  const marker = state.src.charCodeAt(start);

  if (silent) return false;
  if (marker !== 0x2A) return false;

  const scanned = state.scanDelims(state.pos, true);
  for (let idx = 0; idx < scanned.length; idx += 1) {
    const token = state.push('text', '', 0);
    token.content = '*';
    state.delimiters.push({
      marker,
      length: scanned.length,
      token: state.tokens.length - 1,
      end: -1,
      open: scanned.can_open,
      close: scanned.can_close,
    });
  }

  state.pos += scanned.length;
  return true;
}

function postProcessAsteriskOnly(state, delimiters) {
  const max = delimiters.length;
  for (let idx = max - 1; idx >= 0; idx -= 1) {
    const startDelim = delimiters[idx];
    if (startDelim.marker !== 0x2A || startDelim.end === -1) {
      continue;
    }

    const endDelim = delimiters[startDelim.end];
    const isStrong = idx > 0
      && delimiters[idx - 1].end === startDelim.end + 1
      && delimiters[idx - 1].marker === startDelim.marker
      && delimiters[idx - 1].token === startDelim.token - 1
      && delimiters[startDelim.end + 1].token === endDelim.token + 1;

    const tokenOpen = state.tokens[startDelim.token];
    tokenOpen.type = isStrong ? 'strong_open' : 'em_open';
    tokenOpen.tag = isStrong ? 'strong' : 'em';
    tokenOpen.nesting = 1;
    tokenOpen.markup = isStrong ? '**' : '*';
    tokenOpen.content = '';

    const tokenClose = state.tokens[endDelim.token];
    tokenClose.type = isStrong ? 'strong_close' : 'em_close';
    tokenClose.tag = isStrong ? 'strong' : 'em';
    tokenClose.nesting = -1;
    tokenClose.markup = isStrong ? '**' : '*';
    tokenClose.content = '';

    if (isStrong) {
      state.tokens[delimiters[idx - 1].token].content = '';
      state.tokens[delimiters[startDelim.end + 1].token].content = '';
      idx -= 1;
    }
  }
}

function emphasisPostProcessAsteriskOnly(state) {
  const tokensMeta = state.tokens_meta || [];
  postProcessAsteriskOnly(state, state.delimiters || []);
  for (let idx = 0; idx < tokensMeta.length; idx += 1) {
    if (tokensMeta[idx] && tokensMeta[idx].delimiters) {
      postProcessAsteriskOnly(state, tokensMeta[idx].delimiters);
    }
  }
}

let cachedEventMarkdownRenderer;

function getEventMarkdownRenderer() {
  if (cachedEventMarkdownRenderer !== undefined) {
    return cachedEventMarkdownRenderer;
  }

  const MarkdownIt = globalThis?.markdownit;
  if (typeof MarkdownIt !== 'function') {
    cachedEventMarkdownRenderer = null;
    return cachedEventMarkdownRenderer;
  }

  let renderer = null;
  renderer = MarkdownIt({
    html: false,
    linkify: false,
    typographer: false,
    breaks: true,
    highlight(str, lang) {
      return renderHighlightedFenceHtml(str, lang);
    },
  });
  renderer.inline.ruler.at('emphasis', emphasisTokenizeAsteriskOnly);
  renderer.inline.ruler2.at('emphasis', emphasisPostProcessAsteriskOnly);
  cachedEventMarkdownRenderer = renderer;
  return cachedEventMarkdownRenderer;
}

export function renderEventMarkdownInto(container, text) {
  if (!container) return;
  const renderer = getEventMarkdownRenderer();
  if (!renderer) {
    renderMarkdownSourceInto(container, text);
    return;
  }
  container.innerHTML = renderer.render(text || '');
  bindMarkdownLinkRouting(container);
}

export function renderMarkdownBlock(text, extraClass = '') {
  const container = document.createElement('div');
  container.className = extraClass ? `markdown-body ${extraClass}` : 'markdown-body';
  renderMarkdownInto(container, text);
  highlightCode(container);
  return container;
}

export function renderMarkdownItBlock(text, extraClass = '') {
  const container = document.createElement('div');
  container.className = extraClass ? `markdown-body ${extraClass}` : 'markdown-body';
  renderEventMarkdownInto(container, text);
  highlightCode(container);
  return container;
}

export function createStreamingParser(container) {
  const renderer = smd.default_renderer(container);
  bindMarkdownLinkRouting(container);
  return smd.parser(renderer);
}

export function streamWrite(parser, text) {
  smd.parser_write(parser, text || '');
}

export function streamEnd(parser) {
  smd.parser_end(parser);
}
