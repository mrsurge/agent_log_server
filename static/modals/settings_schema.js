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
  
  /**
   * Load settings schema for an extension
   */
  async function loadSettingsSchema(extensionId) {
    if (schemaCache[extensionId]) {
      return schemaCache[extensionId];
    }
    
    try {
      const r = await fetch(`/api/extensions/${extensionId}/settings_schema`, { cache: 'no-store' });
      if (!r.ok) return null;
      const schema = await r.json();
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
          
          // Build options
          (field.options || []).forEach(opt => {
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
      
      // Track for save
      currentSchemaValues[field.id] = { input, type: field.type };
      
      settingsExtensionFields.appendChild(label);
    });
  }
  
  /**
   * Get current values from schema fields
   */
  function getSchemaValues() {
    const values = {};
    Object.entries(currentSchemaValues).forEach(([id, { input, type }]) => {
      if (type === 'checkbox') {
        values[id] = input.checked;
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
