// Assistant streaming row helpers extracted from static/codex_agent.js

export function bindAssistantStream(ctx) {
  const {
    assistantRows,
    buildRow,
    insertRow,
    isMarkdownEnabled,
    createStreamingParser,
    streamWrite,
    streamEnd,
    highlightCode,
    incrementMessages,
    stripCitations,
    maybeAutoScroll,
  } = ctx;

  function getAssistantRow(id, parentEl) {
    const key = id || 'assistant';
    let entry = assistantRows.get(key);
    if (!entry) {
      const { row, body } = buildRow('message', 'assistant');
      // If parentEl provided (subagent body), insert there instead of main timeline
      if (parentEl) {
        parentEl.appendChild(row);
      } else {
        insertRow(row);
      }
      const container = document.createElement('div');
      container.className = 'markdown-body';
      body.append(container);
      if (isMarkdownEnabled()) {
        // Create streaming markdown parser with default renderer
        const parser = createStreamingParser(container);
        entry = { container, parser, useMarkdown: true, counted: false };
      } else {
        // Plain text mode - use pre element
        const pre = document.createElement('pre');
        container.append(pre);
        entry = { container, pre, useMarkdown: false, counted: false };
      }
      assistantRows.set(key, entry);
    }
    return entry;
  }

  function appendAssistantDelta(id, delta, parentEl) {
    if (!delta) return;
    const entry = getAssistantRow(id, parentEl);
    const cleanDelta = stripCitations(delta);
    if (entry.useMarkdown && entry.parser) {
      streamWrite(entry.parser, cleanDelta);
    } else if (entry.pre) {
      entry.pre.textContent += cleanDelta;
    }
    maybeAutoScroll();
  }

  function finalizeAssistant(id, text, parentEl) {
    const key = id || 'assistant';
    let entry = assistantRows.get(key);
    if (!entry) {
      // No prior delta created this row (e.g. SDK sends ASSISTANT_MESSAGE complete, not deltas)
      // Create the row now and feed the full text through the streaming parser
      entry = getAssistantRow(id, parentEl);
      if (text) appendAssistantDelta(id, text, parentEl);
    }
    if (entry.useMarkdown && entry.parser) {
      // End the streaming parser
      streamEnd(entry.parser);
      highlightCode(entry.container);
    }
    if (!entry.counted) {
      incrementMessages();
      entry.counted = true;
    }
  }

  return { getAssistantRow, appendAssistantDelta, finalizeAssistant };
}
