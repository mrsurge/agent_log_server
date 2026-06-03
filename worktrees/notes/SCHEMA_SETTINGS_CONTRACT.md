# Schema settings contract

<!-- ALS inline review test edit: 2026-05-31. -->
<!-- ALS inline review second canary: no functional content. -->
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

Dynamic selects can also declare multi-field write-back from the selected option.
The renderer must preserve the selected raw option metadata, not just the
normalized `{ value, label }`, so schema paths can copy extension-owned metadata
into ordinary settings fields:

```json
{
  "id": "model",
  "type": "select",
  "source_method": "extension.models.list",
  "write_back": {
    "on_select": [
      { "field": "model", "path": "id" },
      { "field": "provider", "path": "capabilities.provider", "fallback_path": "family" }
    ]
  }
}
```

This is provider-neutral. ALS-RS does not know what `provider` means; it only
copies schema-declared paths from the selected option into schema-declared
fields. Target fields may be visible inputs or hidden ordinary fields. Values
written to hidden ordinary fields are included in the final settings patch.

When a select depends on another field through `dynamic_options_from`, the
renderer updates its local option set when the source value changes. If the
current selected value is no longer present in the refreshed option set, the
renderer clears it or applies the schema-declared default value.

## Dynamic submenu and fragment contract

Schemas can split complex settings into generic submenu/group fields without
teaching ALS-RS provider-specific layout rules. A submenu can declare inline
child fields or a lazily loaded schema fragment.

Inline submenu example:

```json
{
  "id": "advanced",
  "type": "submenu",
  "label": "Advanced",
  "fields": [
    {
      "id": "reasoning_summary",
      "type": "select",
      "label": "Reasoning Summary",
      "options": [
        { "value": "auto", "label": "Auto" },
        { "value": "none", "label": "None" }
      ]
    }
  ]
}
```

Fragment submenu example:

```json
{
  "id": "model_extras",
  "type": "submenu",
  "label": "Model Extras",
  "visible_if": {
    "field": "model",
    "op": "matches",
    "value": "^gpt-5"
  },
  "schema_ref": {
    "target": "settings/model_extras.json"
  }
}
```

`schema_ref.target` is a relative file path under the extension's registered
source root/path. ALS-RS reads it through `/rpc/settings` method
`extension.settingsSchema.fragment.get`; it rejects absolute paths, parent/root
components, path prefixes, and symlink traversal. A fragment should normally be
a JSON object with a `fields` array, though a raw array can be normalized as
`fields`.

Submenu child fields follow the same contract as top-level fields:

- nested fields persist by their own `id`
- nested fields can use dynamic sources, dependent options, semantic roles, and
  provider info roles
- the shared renderer treats inline and fragment-loaded children the same after
  loading
- missing or failed fragments are shown as generic schema errors, not
  provider-specific fallback UI

## Conditional visibility and enabled state

Schema fields can declare provider-neutral conditions. Conditions read other
schema field values by `id` and do not infer provider meaning from field names.

Examples:

```json
{
  "id": "model_extras",
  "type": "submenu",
  "label": "Model Extras",
  "visible_if": { "field": "model", "op": "not_empty" }
}
```

```json
{
  "id": "deep_reasoning",
  "type": "checkbox",
  "label": "Deep Reasoning",
  "enabled_if": {
    "all": [
      { "field": "model", "op": "matches", "value": "^gpt-5" },
      { "field": "reasoning_effort", "op": "in", "values": ["high", "xhigh"] }
    ]
  },
  "clear_when_hidden": true
}
```

Supported condition concepts:

- single field predicate: `field`, `op`, and optional `value` / `values`
- compounds: `all`, `any`, and `not`
- operators: `eq`, `neq`, `in`, `not_in`, `truthy`, `falsy`, `empty`,
  `not_empty`, and `matches`

`visible_if` controls whether the field/submenu is displayed. `enabled_if`
controls whether the input is interactive. `clear_when_hidden` explicitly opts a
hidden input into clearing its transient value; otherwise hidden values are
preserved so existing settings are not destroyed by a temporary condition.

## Schema interaction fields

Provider lookups, external API searches, and other request/response settings
flows use a schema-declared `interaction` field. ALS-RS owns the generic UI and
RPC routing; the extension owns the provider/API call and returned DTO.

Example concept:

```json
{
  "id": "provider_lookup",
  "type": "interaction",
  "label": "Provider Lookup",
  "inputs": [
    {
      "id": "query",
      "type": "text",
      "placeholder": "Search provider..."
    }
  ],
  "trigger": {
    "label": "Search",
    "mode": "submit",
    "min_length": 2
  },
  "source": {
    "method": "extension.schemaInteraction.run",
    "action": "provider.lookup",
    "params": {
      "query": "$input.query",
      "model": "$field.model",
      "cwd": "$context.cwd"
    }
  },
  "output": {
    "kind": "list",
    "items_path": "items",
    "id_path": "id",
    "label_path": "name",
    "detail_path": "description",
    "empty_text": "No results"
  },
  "write_back": {
    "on_select": [
      { "field": "provider_resource_id", "path": "id" },
      { "field": "provider_resource_label", "path": "name" }
    ]
  }
}
```

ALS-RS sends `/rpc/settings` method `extension.schemaInteraction.run` and
forwards to adapter method `extension.schema_interaction.run`. The Python
adapter calls extension hook `run_schema_interaction(...)`.

Request DTO:

```json
{
  "extension_id": "some-extension",
  "interaction_id": "provider_lookup",
  "action": "provider.lookup",
  "inputs": { "query": "abc" },
  "values": { "model": "gpt-5.1" },
  "params": { "query": "abc", "model": "gpt-5.1", "cwd": "/repo" },
  "conversation_id": "optional"
}
```

The response DTO is extension-owned and schema-mapped. Render kinds are `list`,
`info`, and `json`. `write_back.on_select` can copy values from a selected result
into ordinary schema fields by field id. Interaction inputs are transient unless
the schema explicitly writes returned values into normal settings fields.

Interaction responses can also return provider-neutral success actions. ALS-RS
does not interpret provider semantics; it only follows action `type`, schema
field ids, and JSON paths into the returned DTO.

Example:

```json
{
  "ok": true,
  "config": {
    "id": "openrouter-custom-abc123",
    "label": "OpenRouter / Gemma",
    "provider": "openrouter-custom-abc123",
    "model": "google/gemma-4-26b-a4b-it"
  },
  "actions": [
    { "type": "refresh_options", "field": "config" },
    { "type": "upsert_option", "field": "config", "item_path": "config" },
    {
      "type": "select_option",
      "field": "config",
      "value_path": "config.id",
      "apply_write_back": true
    },
    { "type": "collapse", "field": "config_generator" },
    { "type": "mark_dirty" }
  ]
}
```

Supported generic action types:

- `refresh_options`: refetch or locally refresh a select field's option set.
- `upsert_option`: insert or replace one select option from `item_path`.
- `select_option`: select an option by `value` / `value_path`; by default this
  runs the target select field's `write_back.on_select` rules.
- `write_back` / `apply_write_back`: apply a field's write-back rules, or an
  action-local `rules` / `write_back.on_select` block, to the result payload.
- `collapse` / `open`: close or open a submenu/group field by field id.
- `mark_dirty`: marks the modal as having schema-driven pending changes.
- `open_url`: open an `http` / `https` URL through the generic `url.open` UI RPC.

`open_url` may also be returned as a top-level `open_url` / `openUrl` value for
simple cases. The UI RPC handler launches the URL through `xdg-open`; extensions
should still keep OAuth/API credential state transient and should not persist raw
secrets into conversation metadata.

Interaction fields may declare either one `input` or multiple `inputs`. Supported
input kinds are `text`, `password`, `secret`, `number`, `checkbox`, and
`textarea`; `secret: true`, `sensitive: true`, or `type: "secret"` are rendered
as password-style inputs and are only sent to the extension call.

Ordinary schema fields can also opt out of persistence:

```json
{
  "id": "openrouter_api_key",
  "type": "text",
  "label": "API key",
  "secret": true,
  "persist": false
}
```

Fields with `persist: false`, `transient: true`, `secret: true`, or
`sensitive: true` are excluded from `meta.settings` and are sent as a null
settings patch on save so stale previously persisted values are removed. They
can still be referenced by interaction param tokens such as
`"$field.openrouter_api_key"` while the modal is open.

Supported param tokens:

- `$input.<id>` reads the interaction input value
- `$field.<id>` reads another schema field's current value
- `$context.cwd` reads the current schema/conversation cwd
- `$context.conversation_id` reads the active harness conversation id
- `$context.provider_session_id` reads the bound provider session/thread id
- `$values` sends the current schema values map

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

## Provider status and usage roles

Provider status and usage are shared read-only schema concepts. The settings
schema declares them as `info` fields with semantic roles, and ALS-RS reads one
provider DTO from `/rpc/settings` method `extension.providerInfo.get`.

Example concept:

```json
{
  "id": "__provider_status",
  "type": "info",
  "label": "Provider Status",
  "semantic": {
    "role": "provider_status",
    "runtime_key": "status"
  }
}
```

```json
{
  "id": "__provider_usage",
  "type": "info",
  "label": "Provider Usage",
  "semantic": {
    "role": "provider_usage",
    "runtime_key": "usage"
  }
}
```

ALS-RS behavior for this role:

- The modal calls `extension.providerInfo.get` once for the selected extension.
- The Rust settings RPC forwards that request through adapter method
  `extension.get_provider_info`.
- Extension Python returns a provider-neutral DTO with `status` and `usage`
  members; provider-native account, quota, and auth calls stay inside the
  extension package.
- Unsupported usage is represented as `usage.supported = false` in the same DTO,
  not as a separate route or extension-specific UI branch.
- The renderer reads the member named by `semantic.runtime_key` and displays its
  `text`, `detail`, and `tone` fields without knowing Codex, Copilot, or Gemini
  payload shapes.

## Persistence rules

Conversation metadata is generic and stable:

- `provider_session_id` is the semantic provider bind id.
- `thread_id` is a compatibility mirror while legacy paths still use it.
- `settings` stores extension settings by schema-declared field `id`.
- fields marked `persist: false`, `transient: true`, `secret: true`, or
  `sensitive: true` are stripped from persisted `meta.settings`.
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
- dynamic select options preserve selected raw metadata for schema-declared
  `write_back.on_select` rules
- schema interaction submit results can run generic action blocks for option
  refresh/upsert/select, submit-result write-back, submenu collapse/open,
  dirty marking, and `url.open`
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
