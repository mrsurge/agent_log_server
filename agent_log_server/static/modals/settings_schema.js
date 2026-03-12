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
    console.log(`[schema] fetchSessions url=${sourceUrl} sioCall=${!!ctx.helpers?.sioCall}`);
    try {
      let data;
      // Route through SIO when available (proxy-safe)
      const srcMatch = sourceUrl.match(/\/api\/extensions\/([^/]+)\/sessions/);
      if (srcMatch && ctx.helpers?.sioCall) {
        data = await ctx.helpers.sioCall('get_sessions', { extension_id: srcMatch[1] }, {
          fallbackUrl: sourceUrl, fallbackMethod: 'GET',
        });
      } else {
        const r = await fetch(sourceUrl, { cache: 'no-store' });
        if (!r.ok) throw new Error('failed');
        data = await r.json();
      }
      console.log(`[schema] sessions response`, data);
      const items = Array.isArray(data?.sessions) ? data.sessions
        : Array.isArray(data) ? data : [];
      renderSessionList(items);
    } catch (err) {
      console.warn('[schema] session list failed', err);
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
      console.log(`[schema] cache hit for ${extensionId}`);
      return schemaCache[extensionId];
    }
    
    try {
      console.log(`[schema] loading schema for ${extensionId} sioCall=${!!ctx.helpers?.sioCall}`);
      const schema = ctx.helpers.sioCall
        ? await ctx.helpers.sioCall('get_extension_settings_schema', { extension_id: extensionId }, { fallbackUrl: `/api/extensions/${extensionId}/settings_schema`, fallbackMethod: 'GET' })
        : await fetch(`/api/extensions/${extensionId}/settings_schema`, { cache: 'no-store' }).then(r => r.ok ? r.json() : null);
      console.log(`[schema] loaded schema for ${extensionId}`, schema ? Object.keys(schema) : null);
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
    const selectControls = {};
    let modelItems = [];

    const normalizeEffortList = (model) => {
      const raw = model?.supported_reasoning_efforts ?? model?.supportedReasoningEfforts;
      if (!Array.isArray(raw)) return [];
      return raw
        .map((item) => {
          if (typeof item === 'string') return item;
          if (item && typeof item === 'object') {
            return item.reasoning_effort || item.reasoningEffort || item.value || '';
          }
          return '';
        })
        .filter(Boolean);
    };

    const setSelectOptions = (control, options) => {
      if (!control?.listDiv || !control?.input) return;
      control.listDiv.innerHTML = '';
      (options || []).forEach((opt) => {
        const optValue = typeof opt === 'object' ? opt.value : opt;
        const optLabel = typeof opt === 'object' ? (opt.label || opt.value) : opt;
        if (!optValue) return;
        const optBtn = document.createElement('button');
        optBtn.type = 'button';
        optBtn.className = 'dropdown-item';
        optBtn.textContent = optLabel;
        optBtn.addEventListener('click', () => {
          control.input.value = optValue;
          if (ctx.helpers?.closeDropdownMenu) {
            ctx.helpers.closeDropdownMenu(control.listDiv);
          } else {
            control.listDiv.classList.remove('open');
          }
          if (control.field?.id === 'model') syncReasoningEffortOptions();
        });
        control.listDiv.appendChild(optBtn);
      });
    };

    const syncReasoningEffortOptions = () => {
      const modelControl = selectControls.model;
      const effortControl = selectControls.reasoning_effort;
      if (!modelControl || !effortControl) return;
      const selectedModelId = modelControl.input?.value || '';
      if (!selectedModelId) {
        setSelectOptions(effortControl, []);
        effortControl.input.value = '';
        effortControl.input.placeholder = 'Select model first';
        return;
      }
      const model = modelItems.find((item) => {
        if (!item || typeof item !== 'object') return false;
        return (item.id || item.value) === selectedModelId;
      });
      if (!model) {
        if (!modelItems.length) return;
        setSelectOptions(effortControl, []);
        effortControl.input.value = '';
        effortControl.input.placeholder = 'Model capabilities unavailable';
        return;
      }
      const modelEfforts = normalizeEffortList(model);
      const supportsReasoningEffort = modelEfforts.length > 0;
      const options = supportsReasoningEffort ? modelEfforts : [];
      const currentValue = effortControl.input.value;
      const initialValue = effortControl.initialValue || '';
      const initialModelId = modelControl.initialValue || '';
      const defaultEffort = model.default_reasoning_effort || model.defaultReasoningEffort || options[0] || '';
      setSelectOptions(effortControl, options.map((v) => ({ value: v, label: v })));
      if (!supportsReasoningEffort) {
        effortControl.input.value = '';
        effortControl.input.placeholder = 'Not supported by selected model';
        return;
      }
      effortControl.input.placeholder = effortControl.field?.placeholder || '';
      let nextValue = defaultEffort;
      if (!effortControl.initialValueApplied && selectedModelId === initialModelId && initialValue && options.includes(initialValue)) {
        nextValue = initialValue;
        effortControl.initialValueApplied = true;
      } else if (currentValue && options.includes(currentValue)) {
        nextValue = currentValue;
      }
      effortControl.input.value = nextValue;
    };
    
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
          input.value = value || '';
          input.dataset.sessionId = value || '';
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
          const selectControl = { field, input, listDiv, initialValue: value, initialValueApplied: false };
          selectControls[field.id] = selectControl;
          
          // Build options (static or dynamic)
          const buildOptions = (options) => {
            setSelectOptions(selectControl, options);
          };
          
          if (field.id === 'reasoning_effort') {
            buildOptions([]);
            input.placeholder = 'Select model first';
          } else {
            buildOptions(field.options);
          }
          
          // Fetch dynamic options if configured
          if (field.dynamic_source) {
            const loadOpts = (data) => {
              console.log(`[schema] loadOpts field=${field.id}`, data);
              if (!data) return;
              const items = Array.isArray(data) ? data
                : data.models || data.options || [];
              const opts = items.map(m => typeof m === 'object'
                ? { value: m.id || m.value, label: m.name || m.label || m.id || m.value }
                : { value: m, label: m });
              console.log(`[schema] built ${opts.length} options for ${field.id}`);
              if (field.id === 'model') {
                modelItems = items.filter((item) => item && typeof item === 'object');
                if (!input.value) {
                  input.placeholder = field.placeholder || 'Use server default';
                }
              }
              if (opts.length) buildOptions(opts);
              if (field.id === 'model') syncReasoningEffortOptions();
            };
            // Extract extension_id from dynamic_source URL pattern /api/extensions/{id}/models
            const srcMatch = field.dynamic_source.match(/\/api\/extensions\/([^/]+)\/models/);
            console.log(`[schema] dynamic_source=${field.dynamic_source} srcMatch=${srcMatch?.[1]||'null'} sioCall=${!!ctx.helpers?.sioCall}`);
            if (srcMatch && ctx.helpers?.sioCall) {
              ctx.helpers.sioCall('get_extension_models', { extension_id: srcMatch[1] }, {
                fallbackUrl: field.dynamic_source, fallbackMethod: 'GET',
              }).then(loadOpts).catch(e => console.error(`[schema] sioCall models failed`, e));
            } else {
              fetch(field.dynamic_source, { cache: 'no-store' })
                .then(r => { console.log(`[schema] fetch ${field.dynamic_source} status=${r.status}`); return r.ok ? r.json() : null; })
                .then(loadOpts).catch(e => console.error(`[schema] fetch models failed`, e));
            }
          }
          
          toggleBtn.addEventListener('click', (e) => {
            e.preventDefault();
            if (ctx.helpers?.toggleDropdownMenu) {
              ctx.helpers.toggleDropdownMenu(listDiv);
            } else {
              listDiv.classList.toggle('open');
            }
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

        case 'textarea':
          input = document.createElement('textarea');
          input.id = `settings-ext-${field.id}`;
          input.className = 'settings-textarea';
          input.placeholder = field.placeholder || '';
          input.rows = field.rows || 6;
          input.value = value == null ? '' : String(value);
          label.appendChild(input);
          break;

        case 'json':
          input = document.createElement('textarea');
          input.id = `settings-ext-${field.id}`;
          input.className = 'settings-textarea settings-json-input';
          input.placeholder = field.placeholder || '';
          input.rows = field.rows || 8;
          if (ctx.helpers?.formatJsonSetting) {
            input.value = ctx.helpers.formatJsonSetting(value);
          } else if (typeof value === 'string') {
            input.value = value;
          } else if (value == null || value === '') {
            input.value = '';
          } else {
            input.value = JSON.stringify(value, null, 2);
          }
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
        currentSchemaValues[field.id] = { input, type: field.type, field };
      }
      
      settingsExtensionFields.appendChild(label);
    });

    syncReasoningEffortOptions();
  }
  
  /**
   * Get current values from schema fields
   */
  function collectSchemaValues(parseStructured = false) {
    const values = {};
    Object.entries(currentSchemaValues).forEach(([id, { input, type, field }]) => {
      if (!input) return;
      if (type === 'session_picker') {
        values[id] = input.dataset.sessionId || input.value || '';
        return;
      }
      if (type === 'checkbox') {
        values[id] = input.checked;
      } else if (parseStructured && type === 'json') {
        const parsed = ctx.helpers?.parseJsonSetting
          ? ctx.helpers.parseJsonSetting(input.value, field?.label || field?.id || id)
          : JSON.parse(input.value || 'null');
        if (field?.json_kind === 'object' && parsed != null && (Array.isArray(parsed) || typeof parsed !== 'object')) {
          throw new Error(`${field.label || field.id || id} must be a JSON object`);
        }
        values[id] = parsed;
      } else {
        values[id] = input.value;
      }
    });
    return values;
  }

  function getSchemaRawValues() {
    return collectSchemaValues(false);
  }

  function getSchemaParsedValues() {
    return collectSchemaValues(true);
  }

  function getSchemaValues() {
    return getSchemaRawValues();
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
        let settings = isPending ? {} : (window.CodexAgent?.state?.conversationSettings || {});
        // Prefill CWD from project root when starting from the project tab
        if (isPending) {
          const st = window.CodexAgent?.state;
          const hu = st?.hostUi;
          if (hu?.ideMode && st?.splashTab === 'project' && typeof hu?.projectRoot === 'string' && hu.projectRoot) {
            settings = { cwd: hu.projectRoot };
          }
        }
        renderSchemaFields(schema, settings);
      }
    }
  }
  
  // Export helpers - called after CodexAgent is created, so ctx === window.CodexAgent
  ctx.helpers = ctx.helpers || {};
  ctx.helpers.loadSettingsSchema = loadSettingsSchema;
  ctx.helpers.renderSchemaFields = renderSchemaFields;
  ctx.helpers.getSchemaRawValues = getSchemaRawValues;
  ctx.helpers.getSchemaParsedValues = getSchemaParsedValues;
  ctx.helpers.getSchemaValues = getSchemaValues;
  ctx.helpers.onAgentChange = onAgentChange;
});
