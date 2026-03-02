/**
 * Settings Schema Module
 * 
 * Handles dynamic rendering of extension settings based on JSON schemas.
 * Each extension can define a settings_schema.json with field definitions.
 */
window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const settingsCodexFields = document.getElementById('settings-codex-fields');
  const settingsExtensionFields = document.getElementById('settings-extension-fields');
  const settingsAgentEl = document.getElementById('settings-agent');
  
  // Cache for loaded schemas
  const schemaCache = {};
  
  // Current schema field values (for save)
  let currentSchemaValues = {};
  
  // Session picker overlay elements (reuse HTML already in template)
  const sessionPickerOverlay = document.getElementById('session-picker');
  const sessionPickerCloseBtn = document.getElementById('session-picker-close');
  const sessionPickerListEl = document.getElementById('session-picker-list');
  
  // Track which input field the session picker is serving
  let _sessionPickerTarget = null;  // { input, field }
  
  function openSessionPicker(field, input) {
    if (!sessionPickerOverlay) return;
    _sessionPickerTarget = { input, field };
    sessionPickerOverlay.classList.remove('hidden');
    fetchAndRenderSessions(field.source || '');
  }
  
  function closeSessionPicker() {
    if (!sessionPickerOverlay) return;
    sessionPickerOverlay.classList.add('hidden');
    _sessionPickerTarget = null;
  }
  
  async function fetchAndRenderSessions(sourceUrl) {
    if (!sessionPickerListEl) return;
    sessionPickerListEl.innerHTML = '<div class="picker-item">Loading…</div>';
    try {
      const r = await fetch(sourceUrl, { cache: 'no-store' });
      if (!r.ok) throw new Error('failed');
      const data = await r.json();
      const items = Array.isArray(data?.sessions) ? data.sessions
        : Array.isArray(data) ? data : [];
      renderSessionList(items);
    } catch (err) {
      console.warn('session list failed', err);
      renderSessionList([]);
    }
  }
  
  function renderSessionList(items) {
    if (!sessionPickerListEl) return;
    sessionPickerListEl.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'picker-item';
      empty.textContent = 'No sessions found';
      sessionPickerListEl.appendChild(empty);
      return;
    }
    items.forEach(item => {
      const sid = item?.sessionId || item?.session_id || item?.id || '';
      const summary = item?.summary || '';
      const modified = item?.modifiedTime || item?.modified_time || '';
      
      const row = document.createElement('div');
      row.className = 'picker-item rollout-item';
      row.dataset.sessionId = sid;
      row.style.cursor = 'pointer';
      
      const idSpan = document.createElement('span');
      idSpan.className = 'rollout-id';
      idSpan.textContent = sid.length > 12 ? sid.slice(0, 8) + '…' : sid;
      
      const previewSpan = document.createElement('span');
      previewSpan.className = 'rollout-preview';
      previewSpan.textContent = summary || (modified ? `Modified: ${modified}` : '');
      
      row.append(idSpan, previewSpan);
      row.addEventListener('click', () => {
        if (_sessionPickerTarget) {
          _sessionPickerTarget.input.value = sid;
          _sessionPickerTarget.input.dataset.sessionId = sid;
        }
        closeSessionPicker();
      });
      sessionPickerListEl.appendChild(row);
    });
  }
  
  // Wire close button
  if (sessionPickerCloseBtn) {
    sessionPickerCloseBtn.addEventListener('click', closeSessionPicker);
  }
  
  /**
   * Load settings schema for an extension
   */
  async function loadSettingsSchema(extensionId) {
    if (schemaCache[extensionId]) {
      return schemaCache[extensionId];
    }
    
    try {
      const schema = ctx.helpers.sioCall
        ? await ctx.helpers.sioCall('get_extension_settings_schema', { extension_id: extensionId }, { fallbackUrl: `/api/extensions/${extensionId}/settings_schema`, fallbackMethod: 'GET' })
        : await fetch(`/api/extensions/${extensionId}/settings_schema`, { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
      schemaCache[extensionId] = schema;
      return schema;
    } catch {
      return null;
    }
  }
  
  /**
   * Render schema fields into the extension fields container
   */
  function renderSchemaFields(schema, values = {}) {
    if (!settingsExtensionFields) return;
    settingsExtensionFields.innerHTML = '';
    currentSchemaValues = {};
    
    if (!schema || !Array.isArray(schema.fields)) return;
    
    schema.fields.forEach(field => {
      const label = document.createElement('label');
      const span = document.createElement('span');
      span.textContent = field.label || field.id;
      label.appendChild(span);
      
      let input;
      const value = values[field.id] ?? field.default ?? '';
      
      switch (field.type) {
        case 'path':
          // Path field with optional browse button
          const pathDiv = document.createElement('div');
          pathDiv.className = 'settings-row';
          
          input = document.createElement('input');
          input.type = 'text';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = value;
          pathDiv.appendChild(input);
          
          if (field.browse) {
            const browseBtn = document.createElement('button');
            browseBtn.type = 'button';
            browseBtn.className = 'btn ghost';
            browseBtn.textContent = 'Browse';
            browseBtn.addEventListener('click', () => {
              // Use the existing picker if available
              if (ctx.helpers?.openPicker) {
                ctx.helpers.openPicker(input.value || '~');
              }
            });
            pathDiv.appendChild(browseBtn);
          }
          
          label.appendChild(pathDiv);
          break;

        case 'session_picker':
          // Session picker: only shown for NEW conversations (no thread_id yet).
          // Once a conversation is bound to a session, this field disappears.
          const hasThread = !window.CodexAgent?.state?.pendingNewConversation
            && window.CodexAgent?.state?.conversationMeta?.thread_id;
          if (hasThread) break; // Already bound — hide picker

          const sessionDiv = document.createElement('div');
          sessionDiv.className = 'settings-row';
          
          input = document.createElement('input');
          input.type = 'text';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '(new session)';
          input.readOnly = true;
          input.value = '';
          sessionDiv.appendChild(input);
          
          const resumeBtn = document.createElement('button');
          resumeBtn.type = 'button';
          resumeBtn.className = 'btn ghost';
          resumeBtn.textContent = 'Browse';
          resumeBtn.addEventListener('click', () => {
            openSessionPicker(field, input);
          });
          sessionDiv.appendChild(resumeBtn);
          
          label.appendChild(sessionDiv);
          break;
          
        case 'select':
          // Dropdown field
          const selectDiv = document.createElement('div');
          selectDiv.className = 'dropdown-field';
          
          input = document.createElement('input');
          input.type = 'text';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = value;
          input.readOnly = true;
          selectDiv.appendChild(input);
          
          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'btn ghost dropdown-toggle';
          toggleBtn.textContent = '▾';
          selectDiv.appendChild(toggleBtn);
          
          const listDiv = document.createElement('div');
          listDiv.className = 'dropdown-list';
          listDiv.id = `settings-ext-${field.id}-options`;
          
          // Build options (static or dynamic)
          const buildOptions = (options) => {
            listDiv.innerHTML = '';
            (options || []).forEach(opt => {
              const optBtn = document.createElement('button');
              optBtn.type = 'button';
              optBtn.className = 'dropdown-item';
              optBtn.textContent = typeof opt === 'object' ? opt.label : opt;
              optBtn.addEventListener('click', () => {
                input.value = typeof opt === 'object' ? opt.value : opt;
                listDiv.classList.remove('open');
              });
              listDiv.appendChild(optBtn);
            });
          };
          
          buildOptions(field.options);
          
          // Fetch dynamic options if configured
          if (field.dynamic_source) {
            const loadOpts = (data) => {
              if (!data) return;
              const items = data.models || data.options || [];
              const opts = items.map(m => typeof m === 'object'
                ? { value: m.id || m.value, label: m.name || m.label || m.id || m.value }
                : { value: m, label: m });
              if (opts.length) buildOptions(opts);
            };
            fetch(field.dynamic_source, { cache: 'no-store' })
              .then(r => r.ok ? r.json() : null)
              .then(loadOpts).catch(() => {});
          }
          
          toggleBtn.addEventListener('click', (e) => {
            e.preventDefault();
            listDiv.classList.toggle('open');
          });
          
          selectDiv.appendChild(listDiv);
          label.appendChild(selectDiv);
          break;
          
        case 'checkbox':
          input = document.createElement('input');
          input.type = 'checkbox';
          input.id = `settings-ext-${field.id}`;
          input.checked = value === true || value === 'true';
          label.appendChild(input);
          label.className = 'settings-checkbox-row';
          break;
          
        case 'number':
          input = document.createElement('input');
          input.type = 'number';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = value;
          if (field.min !== undefined) input.min = field.min;
          if (field.max !== undefined) input.max = field.max;
          label.appendChild(input);
          break;
          
        case 'text':
        default:
          input = document.createElement('input');
          input.type = 'text';
          input.id = `settings-ext-${field.id}`;
          input.placeholder = field.placeholder || '';
          input.value = value;
          label.appendChild(input);
          break;
      }
      
      // Track for save (only if input was created)
      if (input) {
        currentSchemaValues[field.id] = { input, type: field.type };
      }
      
      settingsExtensionFields.appendChild(label);
    });
  }
  
  /**
   * Get current values from schema fields
   */
  function getSchemaValues() {
    const values = {};
    Object.entries(currentSchemaValues).forEach(([id, { input, type }]) => {
      if (!input) return;
      if (type === 'session_picker') return; // one-time binding, not a persistent setting
      if (type === 'checkbox') {
        values[id] = input.checked;
      } else if (type === 'session_picker') {
        // Return full session ID from dataset, not truncated display value
        values[id] = input.dataset.sessionId || '';
      } else {
        values[id] = input.value;
      }
    });
    return values;
  }
  
  /**
   * Update settings modal based on selected agent
   */
  async function onAgentChange(agentId) {
    const isCodex = agentId === 'codex';
    
    // Show/hide Codex-specific fields
    if (settingsCodexFields) {
      settingsCodexFields.style.display = isCodex ? 'block' : 'none';
    }
    
    // Clear extension fields
    if (settingsExtensionFields) {
      settingsExtensionFields.innerHTML = '';
    }
    
    if (!isCodex) {
      // Load and render schema for this extension
      const schema = await loadSettingsSchema(agentId);
      if (schema && !schema.useBuiltin) {
        // For new conversations, use empty values; for existing, use saved settings
        const isPending = window.CodexAgent?.state?.pendingNewConversation;
        const settings = isPending ? {} : (window.CodexAgent?.state?.conversationSettings || {});
        renderSchemaFields(schema, settings);
      }
    }
  }
  
  // Export helpers - called after CodexAgent is created, so ctx === window.CodexAgent
  ctx.helpers = ctx.helpers || {};
  ctx.helpers.loadSettingsSchema = loadSettingsSchema;
  ctx.helpers.renderSchemaFields = renderSchemaFields;
  ctx.helpers.getSchemaValues = getSchemaValues;
  ctx.helpers.onAgentChange = onAgentChange;
});
