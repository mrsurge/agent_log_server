type AnyRecord = Record<string, any>;

interface SubagentsCollapsibleContext {
  clearPlaceholder(): void;
  insertRow(row: HTMLElement, beforeEl?: ChildNode | null): void;
  maybeAutoScroll(force?: boolean): void;
  documentRef: Document;
  storage?: Storage | null;
}

export function bindSubagentsCollapsible(ctx: SubagentsCollapsibleContext) {
  const {
    clearPlaceholder,
    insertRow,
    maybeAutoScroll,
    documentRef,
    storage,
  } = ctx;

  const subagentContainers = new Map<string, AnyRecord>();

  function loadExpandedCards() {
    if (!storage) return new Set<string>();
    try {
      const raw = storage.getItem('expandedCards');
      const parsed = raw ? JSON.parse(raw) : [];
      return new Set(Array.isArray(parsed) ? parsed.map((item) => String(item)) : []);
    } catch {
      return new Set<string>();
    }
  }

  const expandedCards = loadExpandedCards();

  function saveExpandedCards() {
    if (!storage) return;
    try {
      storage.setItem('expandedCards', JSON.stringify([...expandedCards]));
    } catch {
      // ignore storage failures
    }
  }

  function makeCollapsible(row: HTMLElement | null, cardId: string, startExpanded: boolean, options: AnyRecord = {}) {
    if (!(row instanceof HTMLElement)) return;
    const {
      headerEl = row.querySelector('.command-ribbon') || row.querySelector('.diff-path-label'),
      persist = true,
      fullHeaderToggle = false,
      toggleZone = !fullHeaderToggle,
      onToggle = null,
    } = options;
    if (!(headerEl instanceof HTMLElement)) return;

    row.classList.add('collapsible');
    const isExpanded = Boolean(startExpanded || (persist && cardId && expandedCards.has(cardId)));
    row.classList.toggle('expanded', isExpanded);

    let twistyEl = headerEl.querySelector(':scope > .twisty') as HTMLElement | null;
    if (!(twistyEl instanceof HTMLElement)) {
      twistyEl = headerEl.querySelector('.twisty') as HTMLElement | null;
    }
    if (!(twistyEl instanceof HTMLElement)) {
      twistyEl = documentRef.createElement('span');
      twistyEl.className = 'twisty';
      twistyEl.textContent = '▶';
      headerEl.appendChild(twistyEl);
    }

    function syncExpandedState(expanded: boolean) {
      headerEl.dataset.expanded = expanded ? 'true' : 'false';
    }

    function persistExpandedState(expanded: boolean) {
      if (!persist || !cardId) return;
      if (expanded) expandedCards.add(cardId);
      else expandedCards.delete(cardId);
      saveExpandedCards();
    }

    function toggleCollapse(forceExpanded?: boolean) {
      const expanded = typeof forceExpanded === 'boolean'
        ? forceExpanded
        : !row.classList.contains('expanded');
      row.classList.toggle('expanded', expanded);
      persistExpandedState(expanded);
      syncExpandedState(expanded);
      if (typeof onToggle === 'function') onToggle(expanded);
      maybeAutoScroll();
      return expanded;
    }

    (row as AnyRecord)._toggleCollapse = toggleCollapse;
    syncExpandedState(isExpanded);

    twistyEl.style.pointerEvents = 'auto';
    twistyEl.style.cursor = 'pointer';
    twistyEl.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleCollapse();
    });

    if (toggleZone) {
      let toggleZoneEl = headerEl.querySelector(':scope > .ribbon-toggle-zone') || headerEl.querySelector('.ribbon-toggle-zone');
      if (!(toggleZoneEl instanceof HTMLElement)) {
        toggleZoneEl = documentRef.createElement('span');
        toggleZoneEl.className = 'ribbon-toggle-zone';
        headerEl.appendChild(toggleZoneEl);
      }
      toggleZoneEl.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleCollapse();
      });
    }

    if (fullHeaderToggle) {
      headerEl.addEventListener('click', (event) => {
        const target = event.target;
        if (target instanceof Element && (target.closest('.twisty') || target.closest('.ribbon-toggle-zone'))) return;
        toggleCollapse();
      });
    }
  }

  function getSubagentContainer(id: string, name: string, intent: string) {
    let subagent = subagentContainers.get(id);
    if (!subagent) {
      clearPlaceholder();
      const row = documentRef.createElement('div');
      row.className = 'timeline-row subagent-card';
      row.dataset.subagentId = id;

      const header = documentRef.createElement('div');
      header.className = 'subagent-header command-ribbon';
      const label = documentRef.createElement('span');
      label.textContent = `${name || 'subagent'}: ${intent || 'working'}`;
      const statusEl = documentRef.createElement('span');
      statusEl.className = 'subagent-status';
      statusEl.textContent = '⏳ running';
      header.append(label, statusEl);
      row.appendChild(header);

      const body = documentRef.createElement('div');
      body.className = 'subagent-body';
      row.appendChild(body);

      insertRow(row);
      makeCollapsible(row, `subagent:${id}`, false, {
        headerEl: header,
        fullHeaderToggle: true,
      });
      subagent = { row, body, header, statusEl, label, items: [] };
      subagentContainers.set(id, subagent);
    }
    return subagent;
  }

  function getLiveEventParent(evt: AnyRecord | null | undefined) {
    if (!evt || !evt.subagent_id) return null;
    return getSubagentContainer(evt.subagent_id, '', '').body;
  }

  function finalizeSubagent(id: string, summary: string, success: boolean) {
    const subagent = subagentContainers.get(id);
    if (!subagent) return;
    subagent.statusEl.textContent = success !== false ? '✓ done' : '✗ failed';
    if (summary) {
      const summaryEl = documentRef.createElement('div');
      summaryEl.className = 'subagent-summary';
      summaryEl.style.cssText = 'padding: 4px 14px; font-size: 0.85em; opacity: 0.7; font-style: italic;';
      summaryEl.textContent = summary;
      subagent.body.appendChild(summaryEl);
    }
  }

  return {
    getSubagentContainer,
    getLiveEventParent,
    finalizeSubagent,
    makeCollapsible,
  };
}
