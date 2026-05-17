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
- `kind`
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
- `variant`
- `action`
- `assistant_id`
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

## Message / reasoning contracts

These contracts define the shared user/assistant/reasoning lane that routers should emit, independent of provider/runtime.

### User send / user message

Success-side rule:

- the transport ack from `send_message` confirms backend acceptance only
- the visible user row remains backend-owned
- the frontend must not invent an optimistic transcript row of its own

Live shape:

- `type: "message"`
- `role: "user"`
- `id`
- `text`
- `turn_id`
- `conversation_id` optional at router level; transport may inject it
- `subagent_id` optional

Replay shape:

- `role: "user"`
- `id` when known
- `item_id` optional legacy/source identifier
- `text`
- `turn_id`
- `timestamp`
- `subagent_id` optional

Failure-side rule:

- failed initial sends still use the shared composer-draft restore contract documented below
- user-send failure does not invent a separate user-row contract

### Assistant message

Live streaming shape:

- zero or more `assistant_delta` events
- one `assistant_finalize` event with the full final text

Expected live fields:

- `type: "assistant_delta"` or `type: "assistant_finalize"`
- `id`
- `delta` for `assistant_delta`
- `text` for `assistant_finalize`
- `turn_id`
- `conversation_id` optional at router level; transport may inject it
- `subagent_id` optional

Replay shape:

- `role: "assistant"`
- `id` when known
- `item_id` optional legacy/source identifier
- `text`
- `turn_id`
- `timestamp`
- `subagent_id` optional

Contract rule:

- live deltas are transient rendering state
- replay uses the finalized transcript row
- if an upstream runtime only emits a one-shot complete assistant message, routers should still normalize it onto `assistant_finalize` plus replay `role: "assistant"`

### Reasoning

The reasoning lane is split into:

- live-only `thought`
- visible reasoning body (`reasoning_delta` / `reasoning_finalize`)

#### Live-only `thought`

Use for short live reasoning labels/headings/ribbon text that should not become transcript rows.

Expected fields:

- `type: "thought"`
- `text`
- `turn_id` optional
- `conversation_id` optional at router level; transport may inject it
- `subagent_id` optional

Rule:

- `thought` is live-only and has no replay row

#### Live reasoning body

Expected live fields:

- `type: "reasoning_delta"` or `type: "reasoning_finalize"`
- `id`
- `delta` for `reasoning_delta`
- `text` for `reasoning_finalize`
- `turn_id`
- `conversation_id` optional at router level; transport may inject it
- `subagent_id` optional

Replay shape:

- `role: "reasoning"`
- `id` when known
- `item_id` optional legacy/source identifier
- `text`
- `turn_id`
- `timestamp`
- `subagent_id` optional

Reasoning rule:

- only visible reasoning body becomes replay `role: "reasoning"`
- title-only / label-only reasoning belongs in live `thought`, not transcript `reasoning`
- routers may scrub title wrappers from live/provider payloads before persisting transcript reasoning text

### Ordering and identity rules

- user rows must appear before assistant/reasoning/tool output for the same turn
- when visible reasoning precedes assistant finalization in live play, replay must preserve that order
- when assistant prose finalizes before a later tool/result card in live play, replay must preserve that order
- `assistant_delta` and `assistant_finalize` for one logical row must share the same stable `id`
- `reasoning_delta` and `reasoning_finalize` for one logical row must share the same stable `id`
- when known, replay rows should preserve the same semantic `id`
- if a live message/assistant/reasoning event is nested under `subagent_id`, the replay row must carry the same `subagent_id`

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

- live `type: "diff"`
- `role: "diff"`
- `text`
- `path` optional
- `id` / `item_id` optional but recommended for replay-safe identity
- `turn_id`, `event`, and `subagent_id` when available

Render expectations:

- preserve existing diff card behavior
- clickable path when available
- if `path` is absent, the frontend may derive it from a `diff --git a/... b/...` header
- standalone diff cards own user-visible patch rendering for file-change output
- this standalone diff card may exist alongside a related tool card that also carries embedded diff metadata

Router expectations:

- normalize structured upstream file-change payloads into unified diff text before emitting `diff`
- when upstream per-file changes provide hunks without file headers, add `diff --git`, `---`, and `+++` headers so path extraction and highlighting work during live render and replay
- split multi-file unified diffs into per-file diff cards when the upstream shape provides independent file changes or when doing so preserves path-specific rendering
- avoid duplicate standalone diff cards when a provider emits the same patch through both file-change items and later aggregate turn-diff notifications

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

### `toast` (live-only)

Use for ephemeral UI notifications that should not appear as transcript rows or normal timeline cards.

Expected fields:

- live event `type: "toast"`
- `id`
- `kind`
- `conversation_id`
- `message`
- `turn_id` optional
- `title` optional
- `variant` optional
- `assistant_id` optional
- `duration_ms` optional
- `reply_enabled` optional
- `reply_label` optional
- `expanded_max_lines` optional
- optional `action`
  - `action.id`
  - `action.label`

Typed transport ownership:

- the canonical live transport for conversation toasts is the conversations RPC notification `conversation.toast`
- the typed schema owners for this live surface are:
  - `agent_log_server/conversations_rpc_contract.py`
  - `agent_log_server/static/js/codex_agent/rpc/conversations/contract.ts`
- toast-specific backend normalization/validation should live in a generic module under `agent_log_server/`, not in `server.py`
- frontend consumers should receive the canonical normalized live event `type: "toast"` after RPC notification normalization
- the legacy `/appserver` `appserver_event` lane may mirror the same payload while compatibility remains, but it is not the source-of-truth contract

Turn-summary policy:

- for end-of-turn assistant-message toasts, use `kind: "turn_summary"`
- `message` should be a normalized plain-text summary derived from the last top-level assistant message that ended the turn
- the toast event supplements, but does not replace, the underlying assistant/turn-complete live events
- when reply UX is enabled for this kind, use `reply_enabled: true`
- `reply_label` is optional; if omitted, the frontend may use a generic pencil/edit affordance such as `✏️`
- `expanded_max_lines` should be treated as a bounded display hint; for the current planned turn-summary reply UX, the default target is 20 lines

Reply-expansion policy:

- a reply-capable toast begins as ephemeral, compact, and truncated
- activating the reply affordance expands it into a more persistent toast/card surface for that same toast `id`
- the expanded surface is still a live-only UI object, not a transcript row
- the expanded surface owns a reply text field plus generic controls for:
  - send reply
  - go to conversation
  - dismiss
- reply submission targets the toast's `conversation_id`; extensions do not invent a second routing key for this UX
- the go-to-conversation control should reuse the shared conversation/view selection flow rather than a toast-specific route

Reply submit / response contract:

- reply submission should reuse the official conversations RPC send method `conversation.send`
- the strict request shape for reply-capable toast submits is:
  - top-level `conversation.send` params:
    - `conversation_id`
    - `text`
    - `toast_context` optional
  - `toast_context` strict fields:
    - `toast_id`
    - `kind`
    - `turn_id` optional
    - `assistant_id` optional
- the reply-submit result is the normal `conversation.send` result / normalized send result
- do not define a second reply-specific ack envelope unless the shared send contract proves insufficient
- if a dedicated reply-specific RPC is ever introduced later, it must remain generic and preserve the same canonical `toast_context` field names

Reply response / ack expectations:

- reply-capable toast sends should use the same success/failure contract as ordinary `conversation.send`
- transport/UI state comes from that existing send result plus the normal live conversation events
- do not invent a toast-only transcript row or a second reply-only success record
- the actual persisted conversation result still comes from the normal generic send-message flow

Current typed rollout note:

- the TypeScript conversations RPC client already accepts `toastContext` and serializes it as `toast_context`
- the Python conversations RPC send-param contract must accept and preserve the same `toast_context` keys before reply-capable toasts can ship end-to-end

Render expectations:

- use a shared toast runtime, not a transcript-row or timeline-card renderer
- do not persist toast events as transcript history
- do not update conversation previews from `toast`
- generic backend toast handling must not allocate transcript card metadata or transcript order reservations for `toast`
- `server.py` should remain orchestration/fan-out only; any toast-specific policy, normalization, or validation logic belongs in a separate generic module
- dedupe or replace by stable `id`
- keep actions generic and data-driven
- do not branch on extension identity
- when `reply_enabled` is true, the frontend should render reply/expand behavior generically from the shared contract instead of per-extension logic
- reuse the existing shared frontend mobile/user-agent detection value; do not introduce a second mobile check just for toasts
- on mobile, send reply only via the explicit send button
- on non-mobile/desktop, Enter submits and Shift+Enter inserts a newline

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
  - summary/outcome/path rendering only; standalone `diff` cards own patch body rendering for apply-patch/file-change output
- routers may normalize semantically equivalent upstream edit tools onto this shared contract; for example an old-string/new-string replacement may be emitted as `tool: "apply_patch"` for success/failure/path semantics even when its actual diff arrives separately as a standalone `diff` card
- routers may still include `diff` metadata on the tool event/transcript entry for semantic parity, but they should not rely on the apply-patch tool card to render that patch body
- emit a standalone `diff` card whenever the patch body should be visible in the transcript

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
- if an `mcp_tool` payload is normalized onto `tool: "apply_patch"`, it should still render with the shared patch-style summary/outcome/path behavior; emit a standalone `diff` card for visible patch bodies

## Router responsibilities

- Choose the most specific generic contract available.
- Do not flatten structured tools into `command` unless they are actually shell commands.
- Normalize semantically equivalent upstream tool names onto shared generic contracts when that preserves the real card semantics better than surfacing the raw tool name.
- Do not invent extension-specific transcript card types.
- Emit and record the same semantic data for live and replay.
- Satisfy generic token/context contracts in the backend/router layer rather than relying on frontend extension-specific recovery logic.
- Persist internal debug rows with `internal: true` / `visibility: "internal"` when durable forensics are needed, and keep them out of normal visible contracts.
- Do not emit or record empty visible message cards for upstream control envelopes that carry no user-visible content.
- When emitting a live-only `toast`, continue to emit the underlying live/transcript semantic events that actually describe the turn or message outcome.

## Frontend responsibilities

- Render generic contracts consistently across extensions.
- Keep card behavior generic and data-driven.
- Prefer canonical tool `request` / `response` fields and fall back to legacy fields for compatibility.
- Do not branch on extension identity when a generic contract exists.
- Hide internal-tagged live/transcript rows by default; only explicit debug tooling should surface them.
- Treat `toast` as live-only UI state; do not synthesize transcript rows or conversation-preview updates from it.

## Priority order for routers

When routing a tool/event into a transcript card:

1. use an existing generic contract if one fits
2. for footer/runtime state, emit the shared `token_count` / `token_usage` / `context_compacted` contracts
3. only use `mcp_tool` when no specific generic card fits
4. do not create a platform-specific transcript card type
