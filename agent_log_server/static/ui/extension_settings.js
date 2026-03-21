window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((agent) => {
  const splashSettingsBtn = document.getElementById('splash-settings');
  const container = document.getElementById('splash-settings-extensions');

  if (!container) return;

  function getSioCall() {
    const sioCall = agent?.helpers?.sioCall || window.CodexAgent?.helpers?.sioCall;
    if (typeof sioCall !== 'function') {
      throw new Error('Socket.IO helper unavailable');
    }
    return sioCall;
  }

  async function fetchExtensions() {
    const data = await getSioCall()('get_extensions', {});
    return Array.isArray(data?.extensions) ? data.extensions : [];
  }

  function statusText(ext) {
    if (!ext || typeof ext !== 'object') return 'Unknown';
    if (ext.enabled !== true) return 'Disabled';
    if (ext.dependency_status === 'met') return 'Ready';
    if (typeof ext.dependency_message === 'string' && ext.dependency_message.trim()) {
      return ext.dependency_message.trim();
    }
    if (ext.dependency_status === 'unmet') return 'Dependencies unmet';
    if (ext.dependency_status === 'error') return 'Dependency check failed';
    return 'Status unknown';
  }

  function emitUpdated() {
    window.dispatchEvent(new CustomEvent('codexagent:extensions-updated'));
  }

  function showError(message) {
    const text = typeof message === 'string' && message.trim() ? message.trim() : 'Extension action failed';
    const errorEl = document.createElement('div');
    errorEl.className = 'muted extension-settings-status';
    errorEl.textContent = text;
    container.prepend(errorEl);
  }

  async function setEnabled(extensionId, enabled) {
    const result = await getSioCall()('extension_set_enabled', { extension_id: extensionId, enabled });
    if (!result || result.ok === false) {
      throw new Error(result?.error || 'Failed to update extension state');
    }
    emitUpdated();
    await refresh();
  }

  async function install(extensionId) {
    const result = await getSioCall()('extension_install', { extension_id: extensionId });
    if (!result || result.ok === false) {
      throw new Error(result?.error || 'Install failed');
    }
    emitUpdated();
    await refresh();
  }

  function buildRow(ext) {
    const row = document.createElement('div');
    row.className = 'extension-settings-row';

    const meta = document.createElement('div');
    meta.className = 'extension-settings-meta';

    const title = document.createElement('div');
    title.className = 'extension-settings-title';
    title.textContent = typeof ext?.name === 'string' && ext.name.trim() ? ext.name.trim() : (ext?.id || 'extension');

    const status = document.createElement('div');
    status.className = 'extension-settings-status';
    status.textContent = statusText(ext);

    meta.append(title, status);

    const toggleLabel = document.createElement('label');
    toggleLabel.className = 'toggle-label';
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.checked = ext?.enabled === true;
    toggle.addEventListener('change', async () => {
      toggle.disabled = true;
      try {
        await setEnabled(ext.id, toggle.checked);
      } catch (error) {
        toggle.checked = !toggle.checked;
        showError(error instanceof Error ? error.message : 'Failed to update extension state');
      } finally {
        toggle.disabled = false;
      }
    });
    toggleLabel.append(toggle, document.createTextNode(' Enabled'));

    const actions = document.createElement('div');
    actions.className = 'extension-settings-actions';

    if (ext?.has_dependency_install === true && ext?.dependency_status !== 'met') {
      const installBtn = document.createElement('button');
      installBtn.className = 'btn tiny';
      installBtn.textContent = 'Install';
      installBtn.disabled = ext?.enabled !== true;
      installBtn.addEventListener('click', async () => {
        installBtn.disabled = true;
        try {
          await install(ext.id);
        } catch (error) {
          showError(error instanceof Error ? error.message : 'Install failed');
        } finally {
          installBtn.disabled = false;
        }
      });
      actions.appendChild(installBtn);
    }

    row.append(meta, toggleLabel, actions);
    return row;
  }

  async function refresh() {
    container.textContent = 'Loading extensions…';
    try {
      const extensions = await fetchExtensions();
      container.innerHTML = '';
      if (!extensions.length) {
        container.textContent = 'No extensions discovered.';
        return;
      }
      extensions.forEach((ext) => {
        container.appendChild(buildRow(ext));
      });
    } catch (error) {
      container.textContent = error instanceof Error ? error.message : 'Failed to load extensions';
    }
  }

  splashSettingsBtn?.addEventListener('click', () => {
    setTimeout(() => { void refresh(); }, 0);
  });

  window.addEventListener('codexagent:extensions-updated', () => {
    void refresh();
  });
});
