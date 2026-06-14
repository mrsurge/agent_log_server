# Extension Planning Contract

Status: draft

This contract defines the provider-neutral planning and todo shape that agent
extensions expose to ALS-RS and the shared frontend. Codex and Copilot use
different provider-native storage and events, but both normalize into this same
surface.

## Scope

This contract defines:

- extension capability flags for planning and todo support
- the `extension.plan.get` read DTO
- live planning/todo notification shapes
- final transcript plan-card shape
- frontend expectations for the plan overlay and plan modal

This contract does not define:

- provider-native plan storage
- provider-native todo database schemas
- a write/edit API for plan documents
- provider-specific plan lifecycle semantics

## Ownership

- Extensions own provider-native plan/todo discovery, parsing, watching, and
  normalization.
- ALS-RS owns transport, conversation scoping, default fields, and fanout.
- The frontend owns only generic rendering of the normalized DTO.

Rust and the frontend must not parse provider-native storage such as Codex plan
events, Copilot plan files, or Copilot todo databases. That logic belongs inside
the extension package.

## Capability Flags

Extensions advertise planning availability through runtime options:

```json
{
  "has_plan": true,
  "has_todo": true
}
```

`has_plan` means the extension can expose a full plan document for the modal.
`has_todo` means the extension can expose a todo/checklist stream for the header
overlay.

The flags are capabilities, not current state. A provider can set
`has_plan: true` while `plan_exists: false` for a conversation with no current
plan document.

## Authoritative Read DTO

The frontend asks `/rpc/settings` method `extension.plan.get` for authoritative
state. Rust forwards adapter method `extension.get_plan`, and the Python adapter
delegates to the extension loader `read_plan(extension_id, conversation_id)`
hook.

The normalized response shape is:

```json
{
  "extension_id": "copilot-sdk",
  "conversation_id": "conv_123",
  "has_plan": true,
  "plan_exists": true,
  "plan_content": "# Plan\n\n- [ ] First step",
  "plan_path": "/optional/provider/native/path/plan.md",
  "plan_source": "session_file",
  "has_todo": true,
  "plan_steps": [
    { "step": "First step", "status": "pending" }
  ],
  "transport": "rpc"
}
```

Required normalized fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `extension_id` | string | Extension that owns the state. Rust/adapter may fill this default. |
| `conversation_id` | string | ALS-RS conversation id. Rust/adapter may fill this default. |
| `has_plan` | boolean | This extension supports a plan document surface. |
| `plan_exists` | boolean | A plan document exists for this conversation. |
| `plan_content` | string | Markdown plan document content. Authoritative only when `plan_exists` is true. |
| `has_todo` | boolean | This extension supports todo/checklist state. |
| `plan_steps` | array | Normalized todo/checklist steps. |

Optional normalized fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `plan_path` | string or null | Provider-native source path for diagnostics or future links. |
| `plan_source` | string or null | Provider-defined source tag, such as `session_file`, `sdk`, `active_meta`, or `missing_session`. |
| `todo_source` | string or null | Provider-defined todo source tag, such as `session_db` or `missing_session`. |
| `plan_operation` | string | Live operation hint, usually `create`, `update`, or `delete`. |

Rust fills safe defaults when an extension returns an object without every
field: `has_plan=false`, `plan_exists=false`, `plan_content=""`,
`plan_path=null`, `plan_source=null`, `has_todo=false`, and `plan_steps=[]`.

## Plan Step DTO

Each todo/checklist item should normalize to:

```json
{
  "step": "Short human-readable task",
  "status": "pending"
}
```

Required fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `step` | string | User-visible task text. Empty strings are ignored. |
| `status` | string | One of `pending`, `in_progress`, or `completed`. |

Status normalization is extension-owned. Extensions should collapse provider
variants into the three generic values:

- `pending`
- `in_progress`
- `completed`

Unknown or missing statuses should normalize to `pending`.

Extensions may include additional provider metadata per step, but the current
frontend ignores it. Do not rely on extra fields for rendering or transcript
replay unless this contract is extended.

## Live Notifications

Live plan/todo state uses the generic conversation event lane.

### `plan_state`

`plan_state` is the authoritative live snapshot event. It may include the full
read DTO shape:

```json
{
  "type": "plan_state",
  "conversation_id": "conv_123",
  "has_plan": true,
  "plan_exists": true,
  "plan_content": "# Plan\n\n...",
  "plan_path": "/optional/path/plan.md",
  "plan_source": "sdk",
  "has_todo": true,
  "plan_steps": [
    { "step": "First step", "status": "in_progress" }
  ],
  "plan_operation": "update"
}
```

Frontend behavior:

- updates todo overlay from `plan_steps`
- updates plan document state from `plan_exists` / `plan_content`
- treats `plan_operation: "update"` as a dirty signal when the modal is closed
- refreshes authoritative state on `create`, `delete`, or an `update` while the
  plan modal is open

### `plan_update`

`plan_update` is a lightweight todo update event. It is for checklist state, not
for a full plan document.

Accepted shapes:

```json
{
  "type": "plan_update",
  "steps": [
    { "step": "First step", "status": "completed" }
  ]
}
```

or:

```json
{
  "type": "plan_update",
  "step": "First step",
  "status": "completed"
}
```

The frontend normalizes `steps` or `plan_steps` arrays. Single `step` updates
upsert by matching the `step` text.

## Final Transcript Plan Card

Final, replayable plan cards use event type `plan` live and transcript role
`plan`.

Live event:

```json
{
  "type": "plan",
  "steps": [
    { "step": "First step", "status": "completed" }
  ],
  "explanation": "Optional provider explanation"
}
```

Transcript row:

```json
{
  "role": "plan",
  "steps": [
    { "step": "First step", "status": "completed" }
  ],
  "explanation": "Optional provider explanation",
  "turn_id": "provider-turn-id"
}
```

Routers must preserve the live/replay mirror rule: if a field is required to
render the live plan card, the same field belongs in the transcript row.

## Provider Patterns

Codex currently behaves as a todo-only provider:

- `has_plan=false`
- `has_todo=true`
- live provider plan updates normalize into `plan_state` and `plan_update`
- the active checklist is stored in conversation metadata as extension-owned
  state
- the final turn can emit `type: "plan"` and transcript `role: "plan"`

Copilot currently behaves as a plan-document plus todo provider:

- `has_plan=true`
- `has_todo=true`
- plan document content comes from provider plan events or provider session files
- todo steps come from provider session todo storage
- live plan-document changes emit `plan_state` with `plan_operation`
- `read_plan(...)` returns the current document plus current todo snapshot

These are examples, not special frontend branches. New extensions should choose
the provider-native source they need and normalize to the same DTO.

## Extension Hook Requirements

An extension that advertises `has_plan` or `has_todo` should implement:

```python
async def read_plan(extension_id: str, conversation_id: str) -> dict[str, object]:
    ...
```

The hook should be cold-safe. If no provider session is bound yet, return a
supported empty state rather than raising:

```json
{
  "has_plan": true,
  "has_todo": true,
  "plan_exists": false,
  "plan_content": "",
  "plan_steps": [],
  "plan_source": "missing_session",
  "todo_source": "missing_session"
}
```

Provider read/watch failures should return empty state with a source/error tag
when possible. They should not crash the settings RPC path for a normal
conversation without plan state.

## Frontend Rules

- Do not hardcode provider ids.
- Use runtime options `has_plan` / `has_todo` to decide whether to fetch plan
  state.
- Use `extension.plan.get` for authoritative state.
- Use `plan_state` and `plan_update` only as generic live events.
- Show the todo overlay when `has_todo` is true and `plan_steps` is non-empty.
- Show the plan modal action when `has_plan` and `plan_exists` are true.
- Render `plan_content` as Markdown only when `plan_exists` is true.

## Validation Checklist

For a new or changed extension:

1. Runtime options expose correct `has_plan` / `has_todo` capability flags.
2. `extension.plan.get` returns the normalized DTO with safe empty values before
   a provider session exists.
3. Live todo updates use `plan_update` or `plan_state` and update the overlay.
4. Live plan-document updates use `plan_state` and refresh/dirty the modal
   correctly.
5. Final replayable plan cards emit live `type: "plan"` and record transcript
   `role: "plan"` with matching fields.
6. Provider-native paths, databases, and event names remain inside the extension.
