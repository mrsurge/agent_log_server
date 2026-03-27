import * as smd from '/static/vendor/streaming-markdown/smd.min.js';

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
  return smd.parser(renderer);
}

export function streamWrite(parser, text) {
  smd.parser_write(parser, text || '');
}

export function streamEnd(parser) {
  smd.parser_end(parser);
}
