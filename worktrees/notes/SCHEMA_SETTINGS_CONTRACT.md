# Schema settings contract

## Purpose

ALS-RS settings UI must be schema-driven and provider-neutral. The shared
frontend and Rust server may route generic requests, persist generic
conversation metadata, and render declared controls, but they must not infer
provider semantics from Codex, Copilot, Gemini, or any other extension-specific
field names.

The extension-owned settings schema is the authority for what a setting means,
how it is rendered, what backend source populates it, and whether it opts into a
shared ALS-RS surface such as conversation binding or footer/runtime controls.

The schema itself is file-owned, not Python-hook-owned. ALS-RS reads
`settings_schema.json` from the extension's registered source root/path as the
authoritative UI contract. Extension Python may still implement provider
actions, dynamic source methods, runtime option values, and provider-native
request translation, but it should not be the primary owner of the UI schema
shape.

## Core rule

Every schema field is opaque extension-owned data unless the schema explicitly
declares a shared semantic role.

ALS-RS may understand generic field mechanics:

- type: text, path, select, checkbox, JSON, picker, info
- label, description, placeholder, default, current value
- dependencies between fields
- backend source/method to call for options or validation
- response paths for items, labels, values, defaults, and identities
- optional semantic role flags for shared ALS-RS behavior

ALS-RS must not understand provider-specific meanings:

- `sandboxPolicy` is not generically a sandbox control unless declared as one
- `approval_policy` is not generically an approval control unless declared as one
- `session` is not persisted provider binding state; it is a UI field value
- `reasoning_effort` is not normalized by hardcoded provider model shape
- provider history/resume behavior belongs to the extension/adapter

## Three questions every dynamic field answers

1. **What field should be rendered?**
   - The schema declares field type, label, current/default value, validation,
     dependencies, picker/select behavior, and optional shared semantic role.
2. **What backend source should populate or validate it?**
   - The schema declares the extension-facing source/method. ALS-RS may route the
     call through `/rpc/settings` and the generic adapter, but must not invent a
     provider-specific method.
3. **What response shape should the renderer read?**
   - The schema declares item, option, label, value, default, current, and
     identity paths. The renderer follows those paths instead of hardcoding
     provider payload normalization.

## Field identity vs shared semantic role

`id` is the persisted extension setting key. It is not enough to opt into shared
ALS-RS behavior.

Shared behavior requires an explicit semantic declaration. The exact schema
property names can evolve, but the contract should express these concepts:

```json
{
  "id": "approval_policy",
  "type": "select",
  "label": "Approval Policy",
  "semantic": {
    "role": "approval_policy",
    "runtime_key": "approval"
  }
}
```

The important split:

- `id` says where the extension wants this value persisted in
  `meta.settings`.
- `semantic.role` says what provider-neutral concept the field represents.
- `semantic.runtime_key` is the generic alias used by shared frontend controls.
- Placement is derived from the semantic role. For example, the shared UI knows
  an extension's approval policy and mode are footer controls for that
  conversation; the schema does not need a separate "put this in the footer"
  flag.

Without a semantic role, the shared UI renders the field only in the schema
settings modal and persists it only as ordinary extension settings data.

## Dynamic source contract

Dynamic fields should declare source and response mapping in the schema. A
renderer should be able to populate options without knowing provider payload
shapes.

Example concept:

```json
{
  "id": "reasoning_effort",
  "type": "select",
  "label": "Reasoning Effort",
  "dynamic_options_from": {
    "source_field": "model",
    "source_method": "extension.models.list",
    "items_path": "models",
    "match": {
      "source_value_path": "id",
      "selected_value_path": "$source"
    },
    "options_path": "capabilities.supports.reasoning_effort",
    "option_value_path": "$item",
    "option_label_path": "$item",
    "default_path": "default_reasoning_effort",
    "missing_source_placeholder": "Select model first",
    "empty_placeholder": "No reasoning options"
  }
}
```

The same model applies to any dynamic select or picker:

- source field dependencies
- method/source to call
- extra params to pass
- item path
- value/label paths
- current/default paths
- identity path for binding-like selections

## Conversation binding role

Provider session/thread/rollout pickers are a shared conversation binding
concept, but provider naming is extension-owned.

Schema fields that select an existing provider conversation should explicitly
declare a conversation-binding role. Example concept:

```json
{
  "id": "session",
  "type": "session_picker",
  "label": "Session",
  "source_method": "extension.sessions.list",
  "binding": {
    "role": "provider_session",
    "id_path": "session_id",
    "label_path": "summary",
    "created_at_path": "created_at",
    "updated_at_path": "updated_at",
    "persist": false
  }
}
```

ALS-RS behavior for this role:

- The picker value is transient UI input.
- The selected provider id is stored as `provider_session_id`.
- `thread_id` may mirror `provider_session_id` as a compatibility field while
  Codex-era paths still exist.
- The picker field itself must not leak into `meta.settings` unless the schema
  explicitly says it is also an ordinary extension setting.
- Resume, load, and hydrate semantics remain adapter/extension-owned.
- ALS-RS stores normalized binding ids and transcript records; it does not parse
  provider rollout/session history itself.

Provider naming examples:

| Provider | Provider-native name | ALS-RS metadata |
| --- | --- | --- |
| Codex app-server | `thread_id` | `provider_session_id`, mirrored to `thread_id` |
| Copilot SDK | `session_id` | `provider_session_id`, mirrored to `thread_id` |
| Gemini app-server | provider session id | `provider_session_id`, mirrored to `thread_id` |

## Runtime roles for approval and mode

Approval policy and mode are shared runtime concepts. An extension opts into
those concepts through schema semantic roles or equivalent extension-owned
runtime descriptors. The shared server/UI can then place those controls in the
conversation footer because it understands the generic concepts, not because it
recognized a provider-specific field id.

Example concept:

```json
{
  "id": "approval_policy",
  "type": "select",
  "label": "Approval Policy",
  "options": [
    { "value": "ask", "label": "Ask" },
    { "value": "never", "label": "Never" }
  ],
  "semantic": {
    "role": "approval_policy",
    "runtime_key": "approval"
  }
}
```

```json
{
  "id": "mode",
  "type": "select",
  "label": "Mode",
  "options": [
    { "value": "default", "label": "Default" },
    { "value": "planning", "label": "Planning" }
  ],
  "semantic": {
    "role": "mode",
    "runtime_key": "mode"
  }
}
```

ALS-RS behavior for this role:

- Shared footer controls render from declared `approval_policy` and `mode`
  semantics.
- Missing semantics mean no footer control and no persisted fallback key.
- ALS-RS must not default unsupported extensions to Codex keys such as
  `approvalPolicy`, `sandboxPolicy`, or `sandbox_policy`.
- Current values are read from the declared persisted `id`, not from a guessed
  provider key.
- A field can have a provider-native persisted id such as `approval_policy`,
  `approvalPolicy`, or another extension-owned name; the semantic role is what
  makes it the shared approval control.

## Persistence rules

Conversation metadata is generic and stable:

- `provider_session_id` is the semantic provider bind id.
- `thread_id` is a compatibility mirror while legacy paths still use it.
- `settings` stores extension settings by schema-declared field `id`.
- transient picker fields are stripped unless explicitly declared persistent.
- null and empty-string settings remove prior values.
- new conversations must not preserve arbitrary settings from the previously
  active conversation.

## Routing rules

ALS-RS owns generic routing only:

- `/rpc/settings` can expose `extension.settingsSchema.get`,
  `extension.models.list`, `extension.sessions.list`,
  `extension.runtimeOptions.get`, and other provider-neutral adapter methods.
- `extension.settingsSchema.get` reads the registered extension's
  `settings_schema.json` before consulting any compatibility adapter hook.
- The adapter/extension owns provider-native request shapes and response
  normalization.
- Shared frontend code reads schema-declared paths and semantic roles.
- No HTTP fallback paths should be added for runtime UI/backend contracts.

## Current implementation checkpoints

Existing pieces that align with this contract:

- schema rendering is driven by extension `settings_schema.json`
- `dynamic_options_from` already handles dependent select options
- generic runtime option descriptors exist in `extensions/__init__.py`
- ALS-RS persists both `provider_session_id` and compatibility `thread_id`
- the settings save flow strips `settings.session` before persistence

Known follow-up work:

- Make `settings_schema.json` mandatory for all installed extensions.
- Replace Codex's Python-built settings schema with a real schema file and keep
  Python limited to runtime/provider translation and dynamic data sources.
- Replace hardcoded `session_picker` behavior with a generic binding role.
- Make dynamic source declarations cover session/model/runtime sources uniformly.
- Normalize approval and mode controls around schema-declared semantic roles
  instead of legacy Codex key assumptions.
- Ensure all new-conversation saves start from fresh settings and only preserve
  existing settings for existing conversations.
- Add schema examples for Codex, Copilot, and Gemini app-server.
- Add focused frontend tests or smoke checks for:
  - no stale settings leak across provider switches
  - transient picker fields do not persist
  - footer controls only appear for declared approval/mode semantics
  - `conversation.meta.updated` updates active binding display
