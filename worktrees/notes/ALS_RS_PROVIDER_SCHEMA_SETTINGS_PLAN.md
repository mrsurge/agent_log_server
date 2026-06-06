# ALS-RS Provider Schema Settings Plan

## Purpose

ALS-RS needs a provider/model settings primitive that works for provider-heavy
extensions such as the new OpenCode app-server extension without teaching the
shared frontend or Rust server OpenCode, Codex, Copilot, OpenRouter, or any
other provider-specific semantics.

The schema should let an extension expose flat provider and model lists while
the extension/app-server owns provider configuration, auth state, catalog
lookup, and final runtime request shaping.

## Current Direction

Open Gemini's config-generator work proved useful schema mechanics:

- hidden/derived write-back targets
- transient and secret fields
- schema interactions
- result actions such as option upsert/select, option refresh, collapse, dirty
  marking, and external URL open

The next system should keep those mechanics but stop treating "config" as the
semantic runtime object. The new model is:

1. provider auth/configuration is an interaction workflow
2. provider selection is an optional first-class schema field
3. model selection can opt into depending on provider selection
4. ALS-RS forwards schema-declared params and applies schema-declared write-back
5. extensions remain the only layer that understands provider-native payloads

## Contract Sketch

Provider select:

```json
{
  "id": "provider",
  "type": "select",
  "label": "Provider",
  "semantic": {
    "role": "provider",
    "runtime_key": "provider"
  },
  "dynamic_source": "/api/extensions/opencode-app-server/providers",
  "source_method": "extension.schemaInteraction.run",
  "source": {
    "action": "provider.list",
    "params": {
      "cwd": "$context.cwd"
    }
  },
  "options_path": "items",
  "option_value_path": "id",
  "option_label_path": "label"
}
```

Provider-dependent model select:

```json
{
  "id": "model",
  "type": "select",
  "label": "Model",
  "semantic": {
    "role": "model",
    "runtime_key": "model"
  },
  "dynamic_source": "/api/extensions/opencode-app-server/models",
  "source_method": "extension.models.list",
  "source_params": {
    "provider": "$field.provider",
    "cwd": "$context.cwd"
  },
  "refresh_on": ["provider"],
  "placeholder": "Select a provider first"
}
```

Provider auth modal action:

```json
{
  "id": "provider_auth",
  "type": "provider_auth",
  "label": "Connect Provider",
  "source": {
    "method": "extension.schemaInteraction.run",
    "actions": {
      "list": "provider.auth.methods",
      "start": "provider.auth.start",
      "status": "provider.auth.status",
      "complete": "provider.auth.complete"
    },
    "params": {
      "cwd": "$context.cwd"
    }
  },
  "targets": {
    "provider": "provider",
    "model": "model"
  }
}
```

Auth DTO minimum shape:

```json
{
  "items": [
    {
      "id": "openrouter",
      "label": "OpenRouter",
      "connected": false,
      "auth_methods": [
        {
          "id": "api-key",
          "type": "api_key",
          "label": "API Key",
          "prompts": [
            {
              "id": "api_key",
              "type": "secret",
              "label": "API Key",
              "required": true,
              "persist": false
            }
          ]
        },
        {
          "id": "oauth",
          "type": "oauth",
          "label": "OAuth"
        }
      ]
    }
  ]
}
```

Auth submit response can reuse existing schema actions:

```json
{
  "ok": true,
  "provider": {
    "id": "openrouter",
    "label": "OpenRouter",
    "connected": true
  },
  "actions": [
    { "type": "refresh_options", "field": "provider" },
    { "type": "upsert_option", "field": "provider", "item_path": "provider" },
    { "type": "select_option", "field": "provider", "value_path": "provider.id" },
    { "type": "refresh_options", "field": "model" },
    { "type": "mark_dirty" }
  ]
}
```

OAuth start response can reuse `open_url`:

```json
{
  "ok": true,
  "mode": "oauth",
  "auth_flow_id": "flow_123",
  "url": "https://example.test/oauth/start",
  "actions": [
    { "type": "open_url", "url_path": "url" }
  ]
}
```

## Large Select Picker

When a select has more than ten options, the renderer should stop rendering a
large inline dropdown. Instead it should show:

- a read-only value field
- a button that opens a settings option picker
- a regex filter input
- an option list that selects through the same code path as dropdown selection

The option picker is not a filesystem picker and has no parent-directory `Up`
control. It only filters the loaded select options by label, value, and detail.

## Hierarchy Hotfix

Provider/model hierarchy depends on preserving upstream dependency fields while
the user selects downstream options. Model selection can write back provider and
model identifiers from the selected raw option, but unresolved write-back paths
must not clear existing fields unless the schema explicitly opts into clearing.
Same-value write-back should also avoid firing dependency refreshes.

Dependent select option hydration must stay field-id neutral. OpenCode can use
`reasoning_effort` as a temporary persisted key while treating it semantically as
an app-server model variant, and future extensions can use `variant` or another
field id through the same `dynamic_options_from` shape.

## Implementation Checklist

- [x] Extend the schema contract documentation for provider/model hierarchy,
      dynamic source params, provider auth, and large select picker behavior.
- [x] Add frontend schema support for `source_params`, `source.params`, and
      dependency refresh declarations.
- [x] Pass generic model-list params through the settings RPC client.
- [x] Forward generic model-list params through Rust settings RPC.
- [x] Forward supported model-list params through the Python adapter/loader
      without breaking no-argument extension handlers.
- [x] Add provider auth modal UI as a provider-neutral schema field.
- [x] Add the large select option picker UI.
- [x] Make schema write-back non-destructive by default and dependent selects
      field-id neutral for provider/model hierarchy.
- [x] Update durable repo memory after the verified contract lands.
- [x] Validate with frontend typecheck/build and backend tests relevant to the
      touched Rust/Python code.
- [ ] Bump `pyproject.toml` version, commit, push, and post an agent-log
      handoff.

## Non-Goals

- Do not add OpenCode-specific frontend branches.
- Do not persist API keys, OAuth codes, tokens, auth flow IDs, or provider
  secrets in conversation `meta.settings`.
- Do not revive HTTP fallback lanes for settings/runtime contracts.
- Do not require existing Codex/Copilot model-list handlers to accept provider
  params.
