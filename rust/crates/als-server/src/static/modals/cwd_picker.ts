type CodexAgentModuleApi = {
  helpers: Record<string, unknown>;
  state: Record<string, unknown>;
};

type TextValueElement = HTMLElement & { value: string };

declare global {
  interface Window {
    CodexAgentModules?: Array<(ctx: CodexAgentModuleApi | undefined) => void>;
  }
}

function callHelper(ctx: CodexAgentModuleApi | undefined, helperName: string, ...args: unknown[]): unknown {
  const helper = ctx?.helpers?.[helperName];
  if (typeof helper === 'function') {
    return helper(...args);
  }
  return undefined;
}

function stringHelper(ctx: CodexAgentModuleApi | undefined, helperName: string, fallback = ''): string {
  const value = callHelper(ctx, helperName);
  return typeof value === 'string' ? value : fallback;
}

window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx: CodexAgentModuleApi | undefined) => {
  const pickerCloseBtn = document.getElementById('picker-close');
  const pickerUpBtn = document.getElementById('picker-up');
  const pickerSelectBtn = document.getElementById('picker-select');
  const settingsCwdEl = document.getElementById('settings-cwd') as TextValueElement | null;

  pickerCloseBtn?.addEventListener('click', () => callHelper(ctx, 'closePicker'));
  pickerUpBtn?.addEventListener('click', () => {
    const pickerPath = stringHelper(ctx, 'getPickerPath');
    if (!pickerPath) return;
    const parent = pickerPath.split('/').slice(0, -1).join('/') || '/';
    callHelper(ctx, 'fetchPicker', parent);
  });
  pickerSelectBtn?.addEventListener('click', () => {
    const pickerPath = stringHelper(ctx, 'getPickerPath');
    const mode = stringHelper(ctx, 'getPickerMode', 'cwd');
    if (mode === 'mention') {
      if (pickerPath) callHelper(ctx, 'insertMention', pickerPath);
      callHelper(ctx, 'closePicker');
      return;
    }
    if (settingsCwdEl && pickerPath) settingsCwdEl.value = pickerPath;
    callHelper(ctx, 'closePicker');
  });
});

export {};
