import { bindPlanOverlay } from '../plan_overlay.ts';
import { bindPlanModal } from '../plan_modal.ts';

type AnyRecord = Record<string, any>;

interface PlanRuntimeState {
  conversationMeta?: AnyRecord;
  conversationSettings?: AnyRecord;
  runtimeOptions?: AnyRecord;
  planOverlayEl?: HTMLDivElement | null;
  planListEl?: HTMLDivElement | null;
  planCollapsed?: boolean;
  planDocState?: AnyRecord;
  todoState?: AnyRecord;
  planDocDirty?: boolean;
  planFetchSerial?: number;
  topSpacerEl?: HTMLElement | null;
}

interface PlanRuntimeContext {
  getState(): PlanRuntimeState;
  setState(patch: Partial<PlanRuntimeState>): void;
  timelineEl: HTMLElement | null;
  planItems: Map<any, any>;
  sioCall(event: string, data?: Record<string, unknown>): Promise<any>;
  currentExtensionId(): string;
  planModalEl: HTMLElement | null;
  planCloseBtn: HTMLElement | null;
  planDismissBtn: HTMLElement | null;
  planBodyEl: HTMLElement | null;
  renderMarkdownInto: (...args: any[]) => any;
  highlightCode: (...args: any[]) => any;
}

export function bindPlanRuntime(ctx: PlanRuntimeContext) {
  const {
    getState,
    setState,
    timelineEl,
    planItems,
    sioCall,
    currentExtensionId,
    planModalEl,
    planCloseBtn,
    planDismissBtn,
    planBodyEl,
    renderMarkdownInto,
    highlightCode,
  } = ctx;

  function createEmptyPlanDocumentState(hasPlan = Boolean(getState().runtimeOptions?.has_plan)) {
    return {
      has_plan: Boolean(hasPlan),
      plan_exists: false,
      plan_content: '',
      plan_path: null,
      plan_source: null,
    };
  }

  function createEmptyTodoState(hasTodo = Boolean(getState().runtimeOptions?.has_todo)) {
    return {
      has_todo: Boolean(hasTodo),
      plan_steps: [],
    };
  }

  function createEmptyPlanState(
    hasPlan = Boolean(getState().runtimeOptions?.has_plan),
    hasTodo = Boolean(getState().runtimeOptions?.has_todo),
  ) {
    return {
      ...createEmptyPlanDocumentState(hasPlan),
      ...createEmptyTodoState(hasTodo),
    };
  }

  function currentPlanState() {
    const { planDocState = {}, todoState = {}, runtimeOptions = {} } = getState();
    return {
      has_plan: Boolean(planDocState?.has_plan ?? runtimeOptions?.has_plan),
      has_todo: Boolean(todoState?.has_todo ?? runtimeOptions?.has_todo),
      plan_exists: Boolean(planDocState?.plan_exists),
      plan_content: typeof planDocState?.plan_content === 'string' ? planDocState.plan_content : '',
      plan_steps: Array.isArray(todoState?.plan_steps) ? todoState.plan_steps : [],
      plan_path: typeof planDocState?.plan_path === 'string' ? planDocState.plan_path : null,
      plan_source: typeof planDocState?.plan_source === 'string' ? planDocState.plan_source : null,
    };
  }

  function normalizePlanDocumentState(nextState: AnyRecord = {}) {
    const { planDocState = {}, runtimeOptions = {} } = getState();
    const hasPlan = nextState.has_plan ?? planDocState.has_plan ?? Boolean(runtimeOptions?.has_plan);
    const planContent = typeof nextState.plan_content === 'string'
      ? nextState.plan_content
      : (nextState.plan_exists === false ? '' : (planDocState.plan_content || ''));
    const planExists = nextState.plan_exists ?? (Boolean(hasPlan) && Boolean(planContent.trim()));
    return {
      ...planDocState,
      has_plan: Boolean(hasPlan),
      plan_exists: Boolean(planExists),
      plan_content: Boolean(planExists) ? planContent : '',
      plan_path: Boolean(planExists)
        ? (typeof nextState.plan_path === 'string' ? nextState.plan_path : (planDocState.plan_path || null))
        : null,
      plan_source: typeof nextState.plan_source === 'string' ? nextState.plan_source : (planDocState.plan_source || null),
    };
  }

  function normalizeTodoState(nextState: AnyRecord = {}) {
    const { todoState = {}, runtimeOptions = {} } = getState();
    const hasTodo = nextState.has_todo ?? todoState.has_todo ?? Boolean(runtimeOptions?.has_todo);
    const rawSteps = Array.isArray(nextState.plan_steps)
      ? nextState.plan_steps
      : (Array.isArray(nextState.steps) ? nextState.steps : (todoState.plan_steps || []));
    const steps = rawSteps
      .map((item) => {
        if (!item || typeof item !== 'object') return null;
        const step = typeof item.step === 'string' ? item.step : '';
        if (!step) return null;
        return {
          step,
          status: typeof item.status === 'string' ? item.status : 'pending',
        };
      })
      .filter(Boolean);
    return {
      has_todo: Boolean(hasTodo),
      plan_steps: steps,
    };
  }

  const planModal = bindPlanModal({
    elements: {
      planModalEl,
      planCloseBtn,
      planDismissBtn,
      planBodyEl,
    },
    getState: () => ({ planState: currentPlanState() }),
    renderMarkdownInto,
    highlightCode,
  });

  async function persistPlanCollapsedState(collapsed: boolean) {
    const { conversationMeta = {}, conversationSettings = {} } = getState();
    const nextPlanCollapsed = Boolean(collapsed);
    const nextConversationSettings = {
      ...(conversationSettings || {}),
      planOverlayCollapsed: nextPlanCollapsed,
    };
    const nextConversationMeta = conversationMeta && typeof conversationMeta === 'object'
      ? {
          ...conversationMeta,
          settings: nextConversationSettings,
        }
      : conversationMeta;
    setState({
      planCollapsed: nextPlanCollapsed,
      conversationSettings: nextConversationSettings,
      conversationMeta: nextConversationMeta,
    });
    const convoId = conversationMeta?.conversation_id || null;
    if (!convoId) return;
    try {
      await sioCall('conversation_update', {
        conversation_id: convoId,
        settings: {
          planOverlayCollapsed: nextPlanCollapsed,
        },
      });
    } catch (err) {
      console.warn('failed to persist plan overlay collapse state', err);
    }
  }

  async function openPlanModal() {
    if (getState().planDocDirty) {
      try {
        await refreshPlanSurface(true);
      } catch (err) {
        console.warn('failed to refresh stale plan state before opening modal', err);
      }
    }
    return planModal.openPlanModal();
  }

  function closePlanModal() {
    return planModal.closePlanModal();
  }

  function renderPlanModal() {
    return planModal.renderPlanModal();
  }

  const planOverlay = bindPlanOverlay({
    timelineEl,
    getState: () => ({
      planOverlayEl: getState().planOverlayEl,
      planListEl: getState().planListEl,
      planCollapsed: getState().planCollapsed,
      planItems,
      planState: currentPlanState(),
      topSpacerEl: getState().topSpacerEl,
    }),
    setState: (patch) => {
      const nextPatch: Partial<PlanRuntimeState> = {};
      if (patch.planOverlayEl !== undefined) nextPatch.planOverlayEl = patch.planOverlayEl;
      if (patch.planListEl !== undefined) nextPatch.planListEl = patch.planListEl;
      if (patch.planCollapsed !== undefined) nextPatch.planCollapsed = patch.planCollapsed;
      setState(nextPatch);
    },
    persistCollapsedState: (collapsed) => persistPlanCollapsedState(collapsed),
    openPlanModal: () => openPlanModal(),
  });

  function ensurePlanOverlay() {
    return planOverlay.ensurePlanOverlay();
  }

  function updatePlanItem(step: string, status: string) {
    return planOverlay.updatePlanItem(step, status);
  }

  function clearPlanOverlay() {
    return planOverlay.clearPlanOverlay();
  }

  function finalizePlanToTranscript() {
    return planOverlay.finalizePlanToTranscript();
  }

  function syncPlanOverlayUi() {
    return planOverlay.syncPlanOverlayUi();
  }

  function restorePlanOverlay(snapshot: AnyRecord) {
    return planOverlay.restorePlanOverlay(snapshot);
  }

  function syncPlanSurface({ renderModal = false } = {}) {
    const mergedState = currentPlanState();
    if (mergedState.plan_exists || (mergedState.has_todo && mergedState.plan_steps.length > 0)) {
      ensurePlanOverlay();
    }
    if (mergedState.has_todo && mergedState.plan_steps.length > 0) {
      restorePlanOverlay({ steps: mergedState.plan_steps });
    } else {
      clearPlanOverlay();
    }
    syncPlanOverlayUi();
    if (renderModal && planModal.isPlanModalOpen()) {
      renderPlanModal();
    }
    return mergedState;
  }

  function applyAuthoritativePlanState(nextState: AnyRecord) {
    setState({
      planDocState: normalizePlanDocumentState(nextState),
      todoState: normalizeTodoState(nextState),
      planDocDirty: false,
    });
    return syncPlanSurface({ renderModal: true });
  }

  function applyTodoState(nextState: AnyRecord) {
    setState({ todoState: normalizeTodoState(nextState) });
    return syncPlanSurface();
  }

  function updateTodoStateStep(step: string, status: string) {
    const normalizedStep = typeof step === 'string' ? step : '';
    if (!normalizedStep) return currentPlanState();
    const normalizedStatus = typeof status === 'string' && status ? status : 'pending';
    const { todoState = {} } = getState();
    const steps = Array.isArray(todoState.plan_steps) ? [...todoState.plan_steps] : [];
    const existingIndex = steps.findIndex((item) => item && item.step === normalizedStep);
    const nextItem = { step: normalizedStep, status: normalizedStatus };
    if (existingIndex >= 0) {
      steps[existingIndex] = nextItem;
    } else {
      steps.push(nextItem);
    }
    setState({
      todoState: {
        has_todo: true,
        plan_steps: steps,
      },
    });
    return syncPlanSurface();
  }

  function handleLiveTodoUpdate(nextState: AnyRecord = {}) {
    if (Array.isArray(nextState.plan_steps) || Array.isArray(nextState.steps)) {
      return applyTodoState(nextState);
    }
    if (typeof nextState.step === 'string' && nextState.step) {
      return updateTodoStateStep(nextState.step, nextState.status);
    }
    return currentPlanState();
  }

  function handleLivePlanState(nextState: AnyRecord = {}) {
    handleLiveTodoUpdate(nextState);
    const operation = typeof nextState.plan_operation === 'string' ? nextState.plan_operation.trim().toLowerCase() : '';
    const modalOpen = planModal.isPlanModalOpen();
    const { planDocState = {}, runtimeOptions = {} } = getState();
    const hasPlanCapability = Boolean(nextState.has_plan ?? planDocState.has_plan ?? runtimeOptions?.has_plan);
    if (operation === 'update' && !modalOpen && hasPlanCapability) {
      setState({ planDocDirty: true });
      return currentPlanState();
    }
    if (operation === 'create' || operation === 'delete' || (operation === 'update' && modalOpen)) {
      const refreshPromise = refreshPlanSurface(true);
      if (refreshPromise && typeof (refreshPromise as Promise<unknown>).catch === 'function') {
        (refreshPromise as Promise<unknown>).catch((err) => console.warn('failed to refresh authoritative plan state', err));
      }
      return refreshPromise;
    }
    return currentPlanState();
  }

  async function fetchPlanState(force = false) {
    const { conversationMeta = {}, runtimeOptions = {} } = getState();
    const convoId = conversationMeta?.conversation_id || null;
    const extensionId = currentExtensionId();
    const hasPlanCapability = Boolean(runtimeOptions?.has_plan);
    const hasTodoCapability = Boolean(runtimeOptions?.has_todo);
    const hasStateCapability = hasPlanCapability || hasTodoCapability;
    if (!convoId || !extensionId || !hasStateCapability) {
      return applyAuthoritativePlanState(createEmptyPlanState(hasPlanCapability, hasTodoCapability));
    }

    const requestSerial = Number(getState().planFetchSerial || 0) + 1;
    setState({ planFetchSerial: requestSerial });

    try {
      const data = await sioCall('get_extension_plan', {
        extension_id: extensionId,
        conversation_id: convoId,
        force,
      });
      if (requestSerial !== getState().planFetchSerial) return currentPlanState();
      if (!data || data.ok === false) {
        console.warn('failed to fetch plan state', data?.error || 'unknown error');
        return currentPlanState();
      }
      return applyAuthoritativePlanState(data);
    } catch (err) {
      if (requestSerial !== getState().planFetchSerial) return currentPlanState();
      console.warn('failed to refresh plan state', err);
      return currentPlanState();
    }
  }

  async function refreshPlanSurface(force = false) {
    return fetchPlanState(force);
  }

  return {
    ensurePlanOverlay,
    updatePlanItem,
    clearPlanOverlay,
    finalizePlanToTranscript,
    syncPlanOverlayUi,
    restorePlanOverlay,
    createEmptyPlanDocumentState,
    createEmptyTodoState,
    createEmptyPlanState,
    currentPlanState,
    applyAuthoritativePlanState,
    applyTodoState,
    updateTodoStateStep,
    handleLiveTodoUpdate,
    handleLivePlanState,
    fetchPlanState,
    refreshPlanSurface,
    openPlanModal,
    closePlanModal,
    renderPlanModal,
    persistPlanCollapsedState,
  };
}
