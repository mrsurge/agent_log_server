window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const rolloutCloseBtn = document.getElementById('rollout-close');
  const rolloutListEl = document.getElementById('rollout-list');

  rolloutCloseBtn?.addEventListener('click', () => ctx.helpers.closeRolloutPicker());
  rolloutListEl?.addEventListener('click', async (evt) => {
    const target = evt.target;
    if (!(target instanceof HTMLElement)) return;
    const row = target.closest('.rollout-item');
    if (!row) return;
    const rolloutId = row.dataset.rolloutId;
    if (rolloutId) {
      await ctx.helpers.loadRolloutPreview(rolloutId);
    }
  });
});
