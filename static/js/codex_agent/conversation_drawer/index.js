import { createConversationDrawerList } from './list.js';
import { createConversationDrawerActions } from './actions.js';

export function bindConversationDrawer(ctx) {
  let actionsRef = null;

  const list = createConversationDrawerList({
    conversationListEl: ctx.conversationListEl,
    getHostUi: ctx.getHostUi,
    getSplashTab: ctx.getSplashTab,
    selectConversation: (...args) => actionsRef?.selectConversation?.(...args),
    selectConversationWithView: (...args) => actionsRef?.selectConversationWithView?.(...args),
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
    setDrawerOpen: ctx.setDrawerOpen,
    applyHostUi: ctx.applyHostUi,
    openSettingsModal: ctx.openSettingsModal,
    renderConversationList: list.renderConversationList,
    renderSplashTabs: list.renderSplashTabs,
    updateActiveConversationLabel: ctx.updateActiveConversationLabel,
    documentRef: ctx.documentRef,
  });

  actionsRef = actions;

  return {
    ...list,
    ...actions,
  };
}

