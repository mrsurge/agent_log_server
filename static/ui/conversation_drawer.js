window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const conversationCreateBtn = document.getElementById('conversation-create');
  const conversationBackBtn = document.getElementById('conversation-back');
  const conversationSettingsBtn = document.getElementById('conversation-settings');

  conversationCreateBtn?.addEventListener('click', async () => {
    ctx.helpers.setPendingNewConversation(true);
    await ctx.helpers.setActiveView('splash');
    ctx.helpers.openSettingsModal();
  });

  conversationBackBtn?.addEventListener('click', async () => {
    await ctx.helpers.setActiveView('splash');
    ctx.helpers.setDrawerOpen(false);
  });

  conversationSettingsBtn?.addEventListener('click', () => {
    ctx.helpers.setPendingNewConversation(false);
    ctx.helpers.openSettingsModal();
  });
});
