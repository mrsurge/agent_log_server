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

## Durable projection units

The transcript pipeline uses three different units. They must not be treated as
interchangeable:

- a **raw transcript record** is one append-only JSONL object in
  `transcript.jsonl`; state records and lifecycle updates are records
- a **transcript card** is one user-visible semantic timeline item; one card may
  consume several raw records, while runtime state may consume no card
- a **layout row** is a mounted card root or a virtual placeholder measured by
  the browser; it is geometry, not persistence or cursor state

Rust is the authority for durable transcript-card identity and position. The
card index classifies records as card creation, card update, runtime state, or
hidden/internal data. It groups lifecycle records such as one agent PTY block
and one subagent into one card recipe, reduces repeated snapshots with the same
family/source identity to the latest record, preserves and updates explicit
`card_id` values, and generates stable IDs for records that do not supply one.

Projection requests use the same action contract through either the default
binary transcript stream or the explicit Socket.IO RPC debug transport:

- `projection.action`: `tail`, `older`, `newer`, or `current`
- `projection.window_cards`
- `projection.shift_cards`

Every projection response carries:

- `projection.unit: "transcript_card"`
- `start_card`, `end_card`, `total_cards`, `window_cards`, `shift_cards`
- `at_start`, `at_tail`, and the server cursor `revision`
- ordered cards where every entry has `card_id`, `card_index`, and `version`
- complete snapshots carry every selected recipe; stream deltas carry removed
  card IDs and only new or version-changed recipes
- every recipe has `card_id`, `card_index`, `version`, `family`, optional
  `parent_card_id`, and ordered `events[]`
- `runtime_state[]` for latest state-only records such as `mode`, `status`, and
  `token_usage`

Every durable recipe event carries `projection_card_id`,
`projection_card_index`, `projection_card_version`, `projection_card_op`, and
`projection_card_scope`; nested recipes also carry
`projection_parent_card_id`. Process-local active recipe events use the same
identity/operation/scope envelope without a durable card version. The frontend
must produce exactly one card root for each durable recipe. Durable recipes use
scope `durable`; process-local turn recipes use scope `active`. Active and
unscoped live-only rows participate in layout but never in durable cursor
arithmetic. A renderer mismatch produces one visible projection-error root at
the authoritative durable card index instead of aborting and leaving a partially
replaced window. The frontend must not derive or mutate the backend cursor from
DOM node counts, JSONL offsets, or provider order IDs.

The default `/ws/transcript` transport is binary-only, versioned tagged
MessagePack. It sends a full snapshot for initial hydration or forced resync and
uses ordered/remove/upsert deltas afterward. Large server frames may be gzip
compressed only after client negotiation. Reconnect requests carry the logical
client ID, current bounds, known card IDs/versions, and stream sequence. The
server routes selected-conversation transcript events only to clients subscribed
to that conversation. `ALS_RS_TRANSCRIPT_TRANSPORT=rpc` is an explicit debugging
mode; implementations must not silently fall back or deliver both modes.

The default durable window is 75 cards with 20-card shifts. While pinned, live
events append normally and the frontend may prune completed card roots to that
display budget without changing the server cursor. Unresolved approvals and
other active rows remain mounted. Active-turn replay recipes use the same card
metadata envelope but remain process-local until matching finalized transcript
records retire them.

Detached history shifts combine the first and last visible authoritative
durable card indices with prefix-summed measured geometry. The frontend uses a
six-card trigger and a 12-card re-arm boundary, plus measured edge and live-tail
runways of two viewports with a 640px floor. A four-viewport/1280px physical
distance may also re-arm a direction. Hidden descendants of collapsed cards,
active rows, and unscoped live-only rows do not count as visible durable cards.
Mounted-node count, JSONL offsets, provider order IDs, and synthetic omitted-row
estimates never drive the backend cursor. A shift remains loading/programmatic
until rendering, card-ID anchor restoration, and virtualizer layout settle;
scroll deltas produced by that transition cannot trigger an immediate reverse
shift.

The structural top spacer is zero-height. A parked placeholder still represents
one logical layout record, so the mounted DOM subset may be smaller than the
75-card recipe window without changing card count. Expanded state may survive
one overlapping in-memory window replacement, but is never persisted across a
conversation reset or reload.

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
- when upstream add/new-file changes provide raw file content in a field named `diff`, synthesize a real new-file unified diff (`--- /dev/null`, `+++ <path>`, hunk header, and `+`-prefixed body lines) before emitting `diff` or embedding diff metadata on the related tool card
- do not create fake diff cards by prepending `diff --git`, `---`, and `+++` to arbitrary raw file content; raw Markdown bullets and ordinary text must never be interpreted as deletion/context lines because of a header-only wrapper
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
