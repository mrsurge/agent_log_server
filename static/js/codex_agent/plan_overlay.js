export function bindPlanOverlay(ctx) {
  const {
    timelineEl,
    getState,
    setState,
  } = ctx;

  function ensurePlanOverlay() {
    const state = getState();
    if (state.planOverlayEl) return;
    if (!timelineEl) return;

    const planOverlayEl = document.createElement('div');
    planOverlayEl.className = 'plan-overlay';
    planOverlayEl.style.display = 'none';

    const header = document.createElement('div');
    header.className = 'plan-header';

    const toggleBtn = document.createElement('span');
    toggleBtn.className = 'plan-toggle';
    toggleBtn.textContent = '[-]';
    toggleBtn.addEventListener('click', () => {
      const nextCollapsed = !getState().planCollapsed;
      setState({ planCollapsed: nextCollapsed });
      toggleBtn.textContent = nextCollapsed ? '[+]' : '[-]';
      const nextState = getState();
      if (nextState.planListEl) {
        nextState.planListEl.style.display = nextCollapsed ? 'none' : 'block';
      }
    });

    const title = document.createElement('span');
    title.className = 'plan-title';
    title.textContent = 'Plan';

    header.append(toggleBtn, title);

    const planListEl = document.createElement('div');
    planListEl.className = 'plan-list';

    planOverlayEl.append(header, planListEl);
    setState({ planOverlayEl, planListEl });

    const nextState = getState();
    if (nextState.topSpacerEl && nextState.topSpacerEl.parentElement === timelineEl) {
      timelineEl.insertBefore(planOverlayEl, nextState.topSpacerEl.nextSibling);
    } else {
      timelineEl.prepend(planOverlayEl);
    }
  }

  function updatePlanItem(step, status) {
    ensurePlanOverlay();
    const state = getState();
    if (!state.planListEl) return;

    let itemEl = state.planItems.get(step);
    if (!itemEl) {
      itemEl = document.createElement('div');
      itemEl.className = 'plan-item';

      const checkbox = document.createElement('span');
      checkbox.className = 'plan-checkbox';

      const text = document.createElement('span');
      text.className = 'plan-text';
      text.textContent = step;

      itemEl.append(checkbox, text);
      itemEl._checkbox = checkbox;
      state.planListEl.appendChild(itemEl);
      state.planItems.set(step, itemEl);
    }

    itemEl.classList.remove('pending', 'in_progress', 'completed');
    itemEl.classList.add(status || 'pending');

    const checkbox = itemEl._checkbox;
    if (checkbox) {
      if (status === 'completed') {
        checkbox.textContent = '☑';
      } else if (status === 'in_progress') {
        checkbox.textContent = '◐';
      } else {
        checkbox.textContent = '☐';
      }
    }

    if (state.planOverlayEl) state.planOverlayEl.style.display = 'block';
  }

  function clearPlanOverlay() {
    const state = getState();
    state.planItems.clear();
    if (state.planListEl) state.planListEl.innerHTML = '';
    if (state.planOverlayEl) state.planOverlayEl.style.display = 'none';
  }

  function finalizePlanToTranscript() {
    const state = getState();
    if (state.planItems.size === 0) return;
    const items = [];
    state.planItems.forEach((el, step) => {
      const status = el.classList.contains('completed')
        ? 'completed'
        : el.classList.contains('in_progress')
          ? 'in_progress'
          : 'pending';
      items.push({ step, status });
    });
    clearPlanOverlay();
  }

  return {
    ensurePlanOverlay,
    updatePlanItem,
    clearPlanOverlay,
    finalizePlanToTranscript,
  };
}
