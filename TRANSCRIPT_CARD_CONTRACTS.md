# Transcript Card Contracts

This file defines the generic live/transcript shapes that routers may emit.

## Core rules

- Do not create platform-specific transcript card types.
- Routers target generic contracts.
- The frontend renders generic contracts.
- Routers own field normalization and population.
- Routers adapt upstream SDK/runtime payloads to the shared contract. The frontend must not branch per extension to recover missing contract fields.

## Live / replay mirror rule

- If a router emits a live event for a contract, it must record the same semantic data into the transcript when that contract is replayable.
- Replay must have enough data to rebuild the same UI state without guessing.
- Every `_emit()` must have a matching `_record()` with the same meaningful fields.

## Shared envelope

These fields are common across live/transcript payloads when applicable:

- `id`
- `turn_id`
- `conversation_id`
- `subagent_id`
- `timestamp`
- `path`
- `line`
- `column`
- `title`
- `tool`
- `server`
- `request`
- `response`
- `arguments`
- `result`
- `diff`
- `status`
- `is_error`
- `duration_ms`
- `source`
- `message`
- `error_type`
- `status_code`
- `provider_call_id`
- `details`
- `stack`
- `code`
- `internal`
- `visibility`
- `direction`
- `summary`
- `payload`
- `category`
- `debug_index`

Not every contract uses every field.

## Canonical tool payload fields

For generic tool cards, the canonical backend payload fields are:

- `request`
- `response`

Legacy compatibility fields still exist:

- `arguments`
- `result`

Frontend behavior:

- prefer canonical `request` / `response`
- fall back to legacy `arguments` / `result`
- do not branch on extension identity when canonical fields are present

Backend/router guidance:

- when backend/router code wants the shared tool-card payload normalizers, import them with the absolute package import:
  - `from extensions.tool_card_contracts import build_tool_card_request, build_tool_card_response`
- do not rely on repo-local relative imports for this helper when writing cross-root extensions; external roots are loaded under dynamic package names, not under the builtin `extensions.*` package tree

## Token / context contracts

These are shared runtime-state contracts, not visible tool cards.

### Live `token_count`

Use for live token/footer updates.

Expected fields:

- `type: "token_count"`
- `total`
- `context_window` when the backend knows the denominator
- `input_tokens` optional
- `output_tokens` optional
- `cached_input_tokens` optional
- `active_context` optional
- `source` optional

Contract rule:

- Routers/backends are responsible for supplying `context_window` when the extension/runtime can determine it.
- The frontend uses this event as-is and must not perform extension-specific model-capability recovery to complete the contract.

### Replay `token_usage`

Use for transcript replay of footer token/context state.

Expected fields:

- `role: "token_usage"`
- `total`
- `context_window` when the backend knew it at emission time
- `input_tokens` optional
- `output_tokens` optional
- `cached_input_tokens` optional
- `active_context` optional
- `event` optional upstream event label
- `source` optional normalized source label
- `turn_id`
- `timestamp`

Replay rule:

- replay should rebuild the same counter/footer state as live `token_count`
- `token_usage` is state-carrying transcript data, not a visible timeline card

### Live / replay `context_compacted`

Use when the backend/runtime compacted or truncated context.

Expected fields:

- live: `type: "context_compacted"`
- replay: `role: "context_compacted"`
- `source` optional
- `messages_removed` optional
- `tokens_removed` optional
- `turn_id`
- `timestamp` on transcript rows

Contract rule:

- when a compaction/truncation event also updates token totals, emit/record the matching `token_count` / `token_usage` state too

## Click target semantics

- Cards may have a header click listener even when they have no concrete target.
- A card only needs actionable navigation when the router provides enough target data.
- When a row has a real file target, the router must provide the path/line fields needed by the existing frontend click logic.

## Generic card types

### `command`

Use for real shell/command execution.

Expected fields:

- `role: "command"`
- `command`
- `output`
- `path` when the command has a concrete file target
- `source` when needed for prompt/terminal rendering

Notes:

- This is for actual command execution, not arbitrary tools flattened into commands.

### `view`

Use for file reads / file views.

Expected fields:

- `role: "view"`
- `path`
- `content`
- `lines` optional structured line rows: `[{ line_no, content }]`
- `view_range` optional
- `title` optional

Render expectations:

- normal collapsible behavior
- path line inside the collapsible content
- when `lines` is present, render the body as gutter/content rows instead of a flat blob
- line-number gutters should use the shared transcript line-number class so selection policy can be changed transcript-wide
- syntax highlighting inferred from the file path and applied to the final rendered code content
- not markdown rendering

### `search`

Use for search-style results such as ripgrep-style matches, glob/file listing results, or thin web-search style result sets that fit the generic search renderer better than a dedicated web result card.

Expected fields:

- `role: "search"`
- `mode`
- `path` optional root/target path
- `pattern` optional
- `arguments` optional normalized search parameters
- `content`
- `title` optional, but the frontend may standardize the visible ribbon text

Render expectations:

- normal collapsible behavior
- generic `search` header with a concise target summary when available
- collapsible body should show the normalized search parameters used for the search
- `rg`/`grep`-style results may render per-file matches with file-path-based highlighting
- when a file extension maps to a known language, use file-path-based highlighting; otherwise render as plain JetBrains Mono text
- `glob`-style results may render as plain monospace listings
- search result bodies are not markdown-rendered

### `diff`

Use for file patch/diff output.

Expected fields:

- `role: "diff"`
- `text`
- `path` optional

Render expectations:

- preserve existing diff card behavior
- clickable path when available
- this standalone diff card may exist alongside a related tool card that also carries embedded diff metadata

### `error`

Use for user-visible conversation/runtime errors.

Expected fields:

- live: `type: "error"`
- replay: `role: "error"`
- canonical `message`
- optional legacy transcript `text`
- `error_type` optional
- `status_code` optional
- `provider_call_id` optional
- `details` optional
- `stack` optional
- `code` optional
- `source` optional
- `turn_id` optional
- `timestamp` on transcript rows

Render expectations:

- use the shared generic error renderer
- always show the primary message
- render optional metadata generically and data-driven
- render optional detail/stack text when present
- `rpc_error` is not this contract; `rpc_error` remains reserved for actual app-server request/response failures

### `warning` (live-only)

Use for runtime warnings that should appear in the live timeline without being persisted as transcript history.

Expected fields:

- live event `type: "warning"`
- `message`
- optional `action`

Render expectations:

- use the generic warning renderer
- if `action` exists, keep it generic and data-driven:
  - `action.id`
  - `action.label`
- the frontend may map known generic action ids like `open_splash_settings` to existing shared helpers
- do not branch on extension identity

### Internal / hidden transcript data

Use this for transcript rows or live debug events that must persist for forensic/debug reasons but must stay out of the normal conversation UI.

Expected fields:

- `internal: true`
- `visibility: "internal"` recommended on transcript rows
- contract-specific payload fields
- `timestamp` on transcript rows when persisted

Render expectations:

- normal live timelines, replay, conversation previews, and transcript-range responses hide internal-tagged rows by default
- explicit debug tooling may opt in when inspecting raw/internal state
- internal rows still follow the live/replay mirror rule when they are intended to round-trip through both surfaces

### `debug_raw` (internal)

Use for durable raw backend/debug payload capture when a debug flag needs transcript-backed forensic evidence.

Expected fields:

- `role: "debug_raw"`
- live `type: "debug_raw"` optional when the live debug surface also consumes the row
- `internal: true`
- `visibility: "internal"` recommended
- `source`
- `direction`
- `summary`
- `payload`
- `category` optional
- `debug_index` optional monotonic index
- `turn_id` optional
- `timestamp`

Render expectations:

- no normal conversation card
- stored for explicit debug inspection only
- routers may pair these rows with other internal debug rows such as `debug_trace`, but none of them should leak into normal replay/UI surfaces

### Composer draft restore on failed send

Use this for failed initial sends where the backend should put the user's cleared composer text back into the draft.

Server-owned behavior:

- the server is the single owner of the transient `user_message_buffer` for in-flight sends
- the server restores composer text by writing `meta["draft"]` and broadcasting the existing live `draft_update` event
- the frontend does not invent a separate fallback route for this; it just consumes the normal draft channel

Extension result contract:

- failed `handle_message(...)` results may include `restore_draft: true`
- failed results may also include `surface_error: true`
- failed results may include generic error metadata fields already used by the shared `error` contract:
  - `error`
  - `error_type`
  - `status_code`
  - `provider_call_id`
  - `details`
  - `stack`
  - `code`
  - `error_source`
  - `turn_id`
- the server only restores the draft when the extension explicitly opts in; this avoids duplicate user-text behavior for extensions that may already have emitted/persisted a user turn before their low-level send fails

Render expectations:

- visible failure still uses the shared generic `error` contract
- no dedicated composer-specific frontend event or card type is introduced

### `web_search`

Use only for genuinely structured web-search result cards whose payload is richer than the generic `search` contract.

Expected fields:

- `role: "web_search"`
- `query`
- `results`

Render expectations:

- use the generic web-search renderer
- do not overload `mcp_tool` for web search
- if upstream only provides thin search/query payloads, prefer the generic `search` contract instead

### `tool`

Use for structured tools that do not map more cleanly onto `command`, `view`, `search`, `diff`, or `web_search`.

Expected fields:

- `role: "tool"`
- `tool`
- canonical `request`
- canonical `response` optional
- legacy `arguments` optional
- legacy `result` optional
- `path` optional
- `diff` optional
- `new_file` optional
- `status` optional
- `is_error` optional
- `duration_ms` optional
- `server` optional

Render expectations:

- use the shared generic tool-card renderer
- normal collapsible behavior
- render canonical `request` / `response` first, with legacy fallback support
- if `tool == "apply_patch"`, use the shared patch-style tool-card behavior:
  - patch-style ribbon label
  - when `new_file: true`, keep the same patch-style card but label it as a new-file operation instead of a generic patch
  - path-aware header behavior when path is available
  - success/failure outcome treatment from generic tool fields such as `status`, `is_error`, and `response`
  - embedded diff preview only when a real `diff` payload is present
- routers may normalize semantically equivalent upstream edit tools onto this shared contract; for example an old-string/new-string replacement may be emitted as `tool: "apply_patch"` for success/failure/path semantics even when its actual diff arrives separately as a standalone `diff` card
- emitting a tool card with embedded `diff` does not forbid also emitting a standalone `diff` card when preserving existing diff-row behavior matters

### `mcp_tool`

Fallback for tools that do not map cleanly onto a more specific generic card.

Expected fields:

- `role: "mcp_tool"`
- `server`
- `tool`
- canonical `request`
- canonical `response` optional
- legacy `arguments` optional
- legacy `result` optional
- `path` optional
- `diff` optional
- `status` optional
- `is_error` optional
- `duration_ms` optional

Render expectations:

- this uses the same shared generic tool-card renderer as `tool`
- frontend prefers canonical `request` / `response` and falls back to legacy fields
- this is a fallback, not the preferred target when a more specific generic card exists
- if an `mcp_tool` payload is normalized onto `tool: "apply_patch"`, it should still render with the shared patch-style tool-card behavior; embedded diff preview remains conditional on an actual `diff` payload

## Router responsibilities

- Choose the most specific generic contract available.
- Do not flatten structured tools into `command` unless they are actually shell commands.
- Normalize semantically equivalent upstream tool names onto shared generic contracts when that preserves the real card semantics better than surfacing the raw tool name.
- Do not invent extension-specific transcript card types.
- Emit and record the same semantic data for live and replay.
- Satisfy generic token/context contracts in the backend/router layer rather than relying on frontend extension-specific recovery logic.
- Persist internal debug rows with `internal: true` / `visibility: "internal"` when durable forensics are needed, and keep them out of normal visible contracts.
- Do not emit or record empty visible message cards for upstream control envelopes that carry no user-visible content.

## Frontend responsibilities

- Render generic contracts consistently across extensions.
- Keep card behavior generic and data-driven.
- Prefer canonical tool `request` / `response` fields and fall back to legacy fields for compatibility.
- Do not branch on extension identity when a generic contract exists.
- Hide internal-tagged live/transcript rows by default; only explicit debug tooling should surface them.

## Priority order for routers

When routing a tool/event into a transcript card:

1. use an existing generic contract if one fits
2. for footer/runtime state, emit the shared `token_count` / `token_usage` / `context_compacted` contracts
3. only use `mcp_tool` when no specific generic card fits
4. do not create a platform-specific transcript card type
