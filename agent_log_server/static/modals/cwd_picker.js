window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const pickerCloseBtn = document.getElementById('picker-close');
  const pickerUpBtn = document.getElementById('picker-up');
  const pickerSelectBtn = document.getElementById('picker-select');
  const settingsCwdEl = document.getElementById('settings-cwd');
  const settingsAgentEl = document.getElementById('settings-agent');

  pickerCloseBtn?.addEventListener('click', () => ctx.helpers.closePicker());
  pickerUpBtn?.addEventListener('click', () => {
    const pickerPath = ctx.helpers.getPickerPath();
    if (!pickerPath) return;
    const parent = pickerPath.split('/').slice(0, -1).join('/') || '/';
    ctx.helpers.fetchPicker(parent);
  });
  pickerSelectBtn?.addEventListener('click', () => {
    const pickerPath = ctx.helpers.getPickerPath();
    const mode = ctx.helpers.getPickerMode ? ctx.helpers.getPickerMode() : 'cwd';
    if (mode === 'mention') {
      if (pickerPath) ctx.helpers.insertMention(pickerPath);
      ctx.helpers.closePicker();
      return;
    }
    // Update CWD field based on agent type
    const agentType = settingsAgentEl?.value?.trim() || 'codex';
    if (agentType === 'codex') {
      if (settingsCwdEl && pickerPath) settingsCwdEl.value = pickerPath;
    } else {
      // Update schema field for non-codex agents
      const schemaField = document.getElementById('settings-ext-cwd');
      if (schemaField && pickerPath) schemaField.value = pickerPath;
    }
    ctx.helpers.closePicker();
  });
});
