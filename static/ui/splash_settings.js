window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const splashSettingsBtn = document.getElementById('splash-settings');
  const splashSettingsCloseBtn = document.getElementById('splash-settings-close');
  const splashSettingsCancelBtn = document.getElementById('splash-settings-cancel');
  const splashSettingsSaveBtn = document.getElementById('splash-settings-save');

  splashSettingsBtn?.addEventListener('click', () => {
    ctx.helpers.openSplashSettingsModal?.();
  });
  splashSettingsCloseBtn?.addEventListener('click', () => {
    ctx.helpers.closeSplashSettingsModal?.();
  });
  splashSettingsCancelBtn?.addEventListener('click', () => {
    ctx.helpers.closeSplashSettingsModal?.();
  });
  splashSettingsSaveBtn?.addEventListener('click', async () => {
    await ctx.helpers.saveSplashSettings?.();
  });
});
