import * as smd from 'https://cdn.jsdelivr.net/npm/streaming-markdown/smd.min.js';

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

export function renderMarkdownBlock(text, extraClass = '') {
  const container = document.createElement('div');
  container.className = extraClass ? `markdown-body ${extraClass}` : 'markdown-body';
  renderMarkdownInto(container, text);
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
