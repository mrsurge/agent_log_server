import { createConversationDrawerList } from './list.js';
import { createConversationDrawerActions } from './actions.js';

export function bindConversationDrawer(ctx) {
  let actionsRef = null;

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
  });

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
  });

  actionsRef = actions;
  actions.bindHeaderHandlers?.();

  return {
    ...list,
    ...actions,
  };
}
