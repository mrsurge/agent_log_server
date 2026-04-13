import { createConversationDrawerList } from './list.ts';
import { createConversationDrawerActions } from './actions.ts';

interface ConversationDrawerListBinding {
  renderConversationList: (...args: unknown[]) => void;
  renderMiniConversationList: (...args: unknown[]) => void;
  renderSplashTabs: (...args: unknown[]) => void;
}

interface ConversationDrawerActionsBinding {
  bindHeaderHandlers?(): void;
  selectConversation?(...args: unknown[]): unknown;
  selectConversationWithView?(...args: unknown[]): unknown;
  setConversationPins?(...args: unknown[]): unknown;
  deleteConversation?(...args: unknown[]): unknown;
}

interface ConversationDrawerContext {
  conversationListEl: HTMLElement | null;
  conversationMiniListEl: HTMLElement | null;
  getState(): unknown;
  getHostUi(): unknown;
  getSplashTab(): string;
  getConversationPreview(conversationId: string): unknown;
  openSettingsModal(): unknown;
  sioCall(event: string, payload: Record<string, unknown>): Promise<unknown>;
  setState(nextState: Record<string, unknown>): void;
  resetTimeline(): void;
  fetchConversation(conversationId?: string | null): Promise<unknown>;
  replayTranscript(): Promise<unknown>;
  refreshPlanSurface?(): Promise<unknown>;
  restorePendingApprovals(): void;
  setDrawerOpen(open: boolean): void;
  applyHostUi(): void;
  updateActiveConversationLabel(): void;
  conversationTitleEl: HTMLElement | null;
  conversationCreateBtn: HTMLElement | null;
  conversationBackBtn: HTMLElement | null;
  conversationSettingsBtn: HTMLElement | null;
  conversationBodyEl: HTMLElement | null;
  conversationMiniDrawerEl: HTMLElement | null;
  conversationMiniCloseBtn: HTMLElement | null;
  documentRef?: Document;
  windowRef?: Window;
}

type ConversationDrawerBinding = ConversationDrawerListBinding & ConversationDrawerActionsBinding;

export function bindConversationDrawer(ctx: ConversationDrawerContext): ConversationDrawerBinding {
  let actionsRef: ConversationDrawerActionsBinding | null = null;

  const list = createConversationDrawerList({
    conversationListEl: ctx.conversationListEl,
    conversationMiniListEl: ctx.conversationMiniListEl,
    getState: ctx.getState,
    getHostUi: ctx.getHostUi,
    getSplashTab: ctx.getSplashTab,
    getConversationPreview: ctx.getConversationPreview,
    selectConversation: (...args) => actionsRef?.selectConversation?.(...args),
    selectConversationWithView: (...args) => actionsRef?.selectConversationWithView?.(...args),
    setConversationPins: (...args) => actionsRef?.setConversationPins?.(...args),
    openSettingsModal: ctx.openSettingsModal,
    deleteConversation: (...args) => actionsRef?.deleteConversation?.(...args),
    documentRef: ctx.documentRef,
    windowRef: ctx.windowRef,
  }) as ConversationDrawerListBinding;

  const actions = createConversationDrawerActions({
    sioCall: ctx.sioCall,
    getState: ctx.getState,
    setState: ctx.setState,
    resetTimeline: ctx.resetTimeline,
    fetchConversation: ctx.fetchConversation,
    replayTranscript: ctx.replayTranscript,
    refreshPlanSurface: ctx.refreshPlanSurface,
    restorePendingApprovals: ctx.restorePendingApprovals,
    setDrawerOpen: ctx.setDrawerOpen,
    applyHostUi: ctx.applyHostUi,
    openSettingsModal: ctx.openSettingsModal,
    renderConversationList: list.renderConversationList,
    renderMiniConversationList: list.renderMiniConversationList,
    renderSplashTabs: list.renderSplashTabs,
    updateActiveConversationLabel: ctx.updateActiveConversationLabel,
    conversationTitleEl: ctx.conversationTitleEl,
    conversationCreateBtn: ctx.conversationCreateBtn,
    conversationBackBtn: ctx.conversationBackBtn,
    conversationSettingsBtn: ctx.conversationSettingsBtn,
    conversationBodyEl: ctx.conversationBodyEl,
    conversationMiniDrawerEl: ctx.conversationMiniDrawerEl,
    conversationMiniCloseBtn: ctx.conversationMiniCloseBtn,
    documentRef: ctx.documentRef,
    windowRef: ctx.windowRef,
  }) as ConversationDrawerActionsBinding;

  actionsRef = actions;
  actions.bindHeaderHandlers?.();

  return {
    ...list,
    ...actions,
  };
}
