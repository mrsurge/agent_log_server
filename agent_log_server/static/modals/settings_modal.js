window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const settingsModalEl = document.getElementById('settings-modal');
  const settingsCloseBtn = document.getElementById('settings-close');
  const settingsCancelBtn = document.getElementById('settings-cancel');
  const settingsSaveBtn = document.getElementById('settings-save');
  const settingsCwdEl = document.getElementById('settings-cwd');
  const settingsCwdBrowseBtn = document.getElementById('settings-cwd-browse');
  const settingsRolloutBrowseBtn = document.getElementById('settings-rollout-browse');

  settingsCloseBtn?.addEventListener('click', () => ctx.helpers.closeSettingsModal());
  settingsCancelBtn?.addEventListener('click', () => {
    if (settingsModalEl) settingsModalEl.classList.add('hidden');
    ctx.helpers.setPendingNewConversation(false);
    ctx.helpers.setPendingRollout(null);
  });
  settingsSaveBtn?.addEventListener('click', async () => {
    await ctx.helpers.saveSettings();
  });
  settingsCwdBrowseBtn?.addEventListener('click', () => {
    ctx.helpers.openPicker(settingsCwdEl?.value || '~');
  });
  settingsRolloutBrowseBtn?.addEventListener('click', () => {
    ctx.helpers.openRolloutPicker();
  });

  const footerApprovalValue = document.getElementById('footer-approval-value');
  const footerApprovalOptions = document.getElementById('footer-approval-options');
  const toggleFooterApproval = (evt) => {
    evt?.preventDefault();
    footerApprovalOptions?.classList.toggle('open');
  };
  footerApprovalValue?.addEventListener('click', toggleFooterApproval);
  if (footerApprovalOptions && footerApprovalOptions.childElementCount === 0) {
    ['never', 'on-failure', 'untrusted'].forEach((opt) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dropdown-item';
      btn.textContent = opt;
      footerApprovalOptions.appendChild(btn);
    });
  }
  footerApprovalOptions?.addEventListener('click', (evt) => {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    if (!target.classList.contains('dropdown-item')) return;
    const value = target.textContent?.trim();
    if (!value) return;
    if (footerApprovalValue) footerApprovalValue.textContent = value;
    footerApprovalOptions.classList.remove('open');
    ctx.helpers.saveApprovalQuick(value);
  });
});
