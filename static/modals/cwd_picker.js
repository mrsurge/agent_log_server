window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const pickerCloseBtn = document.getElementById('picker-close');
  const pickerUpBtn = document.getElementById('picker-up');
  const pickerSelectBtn = document.getElementById('picker-select');
  const settingsCwdEl = document.getElementById('settings-cwd');

  pickerCloseBtn?.addEventListener('click', () => ctx.helpers.closePicker());
  pickerUpBtn?.addEventListener('click', () => {
    const pickerPath = ctx.helpers.getPickerPath();
    if (!pickerPath) return;
    const parent = pickerPath.split('/').slice(0, -1).join('/') || '/';
    ctx.helpers.fetchPicker(parent);
  });
  pickerSelectBtn?.addEventListener('click', () => {
    const pickerPath = ctx.helpers.getPickerPath();
    if (settingsCwdEl && pickerPath) settingsCwdEl.value = pickerPath;
    ctx.helpers.closePicker();
  });
});
