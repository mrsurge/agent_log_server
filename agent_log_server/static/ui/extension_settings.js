window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((agent) => {
  const splashSettingsBtn = document.getElementById('splash-settings');
  const splashSettingsModal = document.getElementById('splash-settings-modal');
  const container = document.getElementById('splash-settings-extensions');
  let refreshTimer = null;

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

  async function fetchSplashSchema(extensionId) {
    const result = await getSioCall()('get_extension_splash_schema', { extension_id: extensionId });
    if (!result || result.ok === false) return null;
    return result;
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

  function clearRefreshTimer() {
    if (!refreshTimer) return;
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  function scheduleRefresh(intervalMs, attempts = 1) {
    clearRefreshTimer();
    const delay = Number(intervalMs);
    const remaining = Number(attempts);
    if (!Number.isFinite(delay) || delay <= 0 || !Number.isFinite(remaining) || remaining <= 0) return;
    refreshTimer = window.setTimeout(async () => {
      refreshTimer = null;
      if (splashSettingsModal?.classList?.contains('hidden')) return;
      await refresh();
      emitUpdated();
      scheduleRefresh(delay, remaining - 1);
    }, delay);
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

  function buildStatusField(field) {
    const wrapper = document.createElement('div');
    wrapper.className = 'extension-settings-field extension-settings-field-status';
    if (typeof field?.tone === 'string' && field.tone.trim()) {
      wrapper.dataset.tone = field.tone.trim();
    }

    const label = document.createElement('div');
    label.className = 'extension-settings-field-label';
    label.textContent = typeof field?.label === 'string' && field.label.trim() ? field.label.trim() : 'Status';

    const text = document.createElement('div');
    text.className = 'extension-settings-field-text';
    text.textContent = typeof field?.text === 'string' && field.text.trim() ? field.text.trim() : 'Unavailable';

    wrapper.append(label, text);

    if (typeof field?.detail === 'string' && field.detail.trim()) {
      const detail = document.createElement('div');
      detail.className = 'extension-settings-field-detail';
      detail.textContent = field.detail.trim();
      wrapper.appendChild(detail);
    }

    return wrapper;
  }

  async function runSplashAction(ext, field) {
    const actionId = typeof field?.action_id === 'string' ? field.action_id.trim() : '';
    if (!actionId) return;
    const hostOpen = field?.open_strategy === 'host';
    let popup = null;
    if (field?.opens_window === true && !hostOpen) {
      try {
        popup = window.open('', '_blank');
      } catch {
        popup = null;
      }
    }
    try {
      const response = await getSioCall()('run_extension_splash_action', {
        extension_id: ext.id,
        action_id: actionId,
        payload: {},
      });
      if (!response || response.ok === false) {
        const errorText = response?.result?.error || response?.error || 'Extension action failed';
        throw new Error(errorText);
      }
      const result = response.result && typeof response.result === 'object' ? response.result : {};
      const openUrl = typeof result.open_url === 'string' ? result.open_url.trim() : '';
      const openedExternally = result.opened_externally === true;
      if (openUrl && !openedExternally) {
        if (popup && !popup.closed) {
          popup.location.replace(openUrl);
        } else {
          window.open(openUrl, '_blank', 'noopener,noreferrer');
        }
      } else if (popup && !popup.closed) {
        popup.close();
      }
      emitUpdated();
      await refresh();
      scheduleRefresh(result.poll_interval_ms, result.poll_attempts);
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      showError(error instanceof Error ? error.message : 'Extension action failed');
    }
  }

  function buildActionField(ext, field) {
    const wrapper = document.createElement('div');
    wrapper.className = 'extension-settings-field extension-settings-field-action';

    const label = document.createElement('div');
    label.className = 'extension-settings-field-label';
    label.textContent = typeof field?.label === 'string' && field.label.trim() ? field.label.trim() : 'Action';
    wrapper.appendChild(label);

    const actions = document.createElement('div');
    actions.className = 'extension-settings-inline-actions';

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn tiny';
    button.textContent = typeof field?.button_label === 'string' && field.button_label.trim()
      ? field.button_label.trim()
      : (typeof field?.label === 'string' && field.label.trim() ? field.label.trim() : 'Run');
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await runSplashAction(ext, field);
      } finally {
        button.disabled = false;
      }
    });

    actions.appendChild(button);
    wrapper.appendChild(actions);

    if (typeof field?.description === 'string' && field.description.trim()) {
      const detail = document.createElement('div');
      detail.className = 'extension-settings-field-detail';
      detail.textContent = field.description.trim();
      wrapper.appendChild(detail);
    }

    return wrapper;
  }

  function buildSplashSchema(ext, schema) {
    const fields = Array.isArray(schema?.fields) ? schema.fields : [];
    if (!fields.length) return null;
    const root = document.createElement('div');
    root.className = 'extension-settings-splash';
    fields.forEach((field) => {
      if (!field || typeof field !== 'object') return;
      if (field.type === 'status') {
        root.appendChild(buildStatusField(field));
        return;
      }
      if (field.type === 'action') {
        root.appendChild(buildActionField(ext, field));
      }
    });
    return root.childNodes.length ? root : null;
  }

  function buildRow(ext, splashSchema) {
    const row = document.createElement('div');
    row.className = 'extension-settings-row';
    row.dataset.extensionId = ext?.id || '';

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

    const splash = buildSplashSchema(ext, splashSchema);
    if (splash) {
      splash.style.gridColumn = '1 / -1';
      row.appendChild(splash);
    }

    return row;
  }

  async function refresh() {
    container.textContent = 'Loading extensions…';
    try {
      const extensions = await fetchExtensions();
      const splashSchemas = new Map(
        await Promise.all(
          extensions.map(async (ext) => {
            try {
              return [ext.id, await fetchSplashSchema(ext.id)];
            } catch {
              return [ext.id, null];
            }
          }),
        ),
      );
      container.innerHTML = '';
      if (!extensions.length) {
        container.textContent = 'No extensions discovered.';
        return;
      }
      extensions.forEach((ext) => {
        container.appendChild(buildRow(ext, splashSchemas.get(ext.id)));
      });
    } catch (error) {
      container.textContent = error instanceof Error ? error.message : 'Failed to load extensions';
    }
  }

  splashSettingsBtn?.addEventListener('click', () => {
    clearRefreshTimer();
    setTimeout(() => { void refresh(); }, 0);
  });

  window.addEventListener('codexagent:extensions-updated', () => {
    void refresh();
  });
});
