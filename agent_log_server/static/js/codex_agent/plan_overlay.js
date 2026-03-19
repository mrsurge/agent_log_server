export function bindPlanOverlay(ctx) {
  const {
    timelineEl,
    getState,
    setState,
    persistCollapsedState,
    openPlanModal,
  } = ctx;

  function scrollPlanOverlayToBottom() {
    const { planOverlayEl } = getState();
    if (!planOverlayEl) return;
    requestAnimationFrame(() => {
      planOverlayEl.scrollTop = planOverlayEl.scrollHeight;
    });
  }

  function syncPlanOverlayUi() {
    const state = getState();
    if (!state.planOverlayEl || !state.planListEl) return;
    const toggleBtn = state.planOverlayEl.querySelector('.plan-toggle');
    const openBtn = state.planOverlayEl.querySelector('.plan-modal-open');
    const itemCount = state.planItems?.size || 0;
    const hasTodoCapability = Boolean(state.planState?.has_todo);
    const hasPlanDoc = Boolean(state.planState?.has_plan && state.planState?.plan_exists);
    const showOverlay = (hasTodoCapability && itemCount > 0) || hasPlanDoc;
    const collapsed = Boolean(state.planCollapsed);
    if (toggleBtn) toggleBtn.textContent = collapsed ? '[+]' : '[-]';
    if (toggleBtn) toggleBtn.style.display = hasTodoCapability && itemCount > 0 ? 'inline-flex' : 'none';
    if (openBtn) openBtn.style.display = hasPlanDoc ? 'inline-flex' : 'none';
    state.planListEl.style.display = hasTodoCapability && itemCount > 0 && !collapsed ? 'flex' : 'none';
    state.planOverlayEl.style.display = showOverlay ? 'block' : 'none';
  }

  function ensurePlanOverlay() {
    const state = getState();
    if (state.planOverlayEl) {
      syncPlanOverlayUi();
      return;
    }
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
      syncPlanOverlayUi();
      if (!nextCollapsed) scrollPlanOverlayToBottom();
      const persisted = persistCollapsedState?.(nextCollapsed);
      if (persisted && typeof persisted.catch === 'function') {
        persisted.catch((err) => console.warn('todo overlay collapse persistence failed', err));
      }
    });

    const title = document.createElement('span');
    title.className = 'plan-title';
    title.textContent = 'To Do';

    const actions = document.createElement('div');
    actions.className = 'plan-header-actions';

    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'btn ghost tiny plan-modal-open';
    openBtn.textContent = 'View Plan';
    openBtn.addEventListener('click', () => {
      const maybePromise = openPlanModal?.();
      if (maybePromise && typeof maybePromise.catch === 'function') {
        maybePromise.catch((err) => console.warn('plan modal open failed', err));
      }
    });

    actions.append(openBtn);
    header.append(toggleBtn, title, actions);

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
    syncPlanOverlayUi();
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

    syncPlanOverlayUi();
  }

  function restorePlanOverlay(snapshot) {
    clearPlanOverlay();
    const steps = Array.isArray(snapshot?.steps) ? snapshot.steps : [];
    if (!steps.length) return;
    for (const item of steps) {
      if (!item || typeof item.step !== 'string') continue;
      updatePlanItem(item.step, item.status || 'pending');
    }
    syncPlanOverlayUi();
    if (!getState().planCollapsed) scrollPlanOverlayToBottom();
  }

  function clearPlanOverlay() {
    const state = getState();
    state.planItems.clear();
    if (state.planListEl) state.planListEl.innerHTML = '';
    syncPlanOverlayUi();
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
    restorePlanOverlay,
    syncPlanOverlayUi,
    clearPlanOverlay,
    finalizePlanToTranscript,
  };
}
