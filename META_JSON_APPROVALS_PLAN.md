# Meta JSON Approvals Plan

## Purpose

This document defines the ownership model for pending approvals in conversation `meta.json`.

The goal is to make approvals survive view changes, remain actionable when the user returns to a conversation, and invalidate cleanly when they no longer belong to a live runtime instance.

This plan is specifically about pending approval persistence and replay behavior. It does not try to redesign all approval UI or all transport logic at once.

## Implemented Status

The core ownership model in this document is now partially implemented.

Implemented now:

- `meta.json` has a first-class `pending_approvals` field
- pending approvals persist by `request_id`
- the exact live approval card payload is stored as `render_event`
- conversation re-entry rehydrates approvals from `meta.json`, not from transcript guessing
- backend validation removes stale approvals before replay
- approval accept or decline removes the pending item from `meta.json`

Still roadmap or follow-up work:

- full live validation coverage across every backend/runtime combination
- continued migration of Codex-specific backend ownership out of `server.py`
- any additional transcript/debug surfacing for invalidation events
- Copilot SDK non-`auto-approve` policies still need alignment with the newer permission-request contract
- the Termux managed-wrapper return remains a separate follow-up after approval flow is stable

## Current Problems

The original system mixed several different concepts:

- live approval events sent to the frontend
- transcript records for approval decisions
- in-memory backend approval resolvers
- partial conversation metadata expectations

This creates several failure modes:

- a pending approval may render live but not survive leaving and re-entering the conversation
- a replayed approval may visually appear but be dead on acceptance because it no longer maps to a live backend request
- stale approvals from an earlier runtime instance may still appear actionable
- different agent backends do not share a single persistence contract

That is why pending approval ownership was moved into an explicit top-level `meta.json.pending_approvals` field.

## Ownership Model

Pending approvals belong to the conversation.

That means the serialized approval descriptor belongs in `meta.json`, because `meta.json` is already the durable home for conversation-scoped state such as settings, thread identity, labels, and preview information.

What belongs in `meta.json`:

- serialized pending approval descriptors
- runtime identity used to validate whether the approval is still actionable
- enough transcript anchoring information to restore the approval in the correct conversation context
- lightweight replay state

What does not belong in `meta.json`:

- in-memory futures
- live SDK request objects
- callback handles
- transport-specific resolver instances

`meta.json` should store the durable description of a pending approval. The backend runtime keeps the live resolver object separately.

## Data Shape

Add a top-level field:

```json
{
  "pending_approvals": {
    "<request_id>": {
      "request_id": "<request_id>",
      "agent": "codex|copilot-sdk|...",
      "kind": "command|diff|tool|unknown",
      "status": "pending",
      "payload": {},
      "conversation_id": "<conversation_id>",
      "thread_id": "<thread_id or null>",
      "turn_id": "<turn_id or null>",
      "runtime_signature": "<backend runtime signature>",
      "runtime_instance_id": "<optional stronger runtime instance token>",
      "transcript_anchor": {
        "seq": 123,
        "turn_id": "<turn_id or null>"
      },
      "created_at": "<utc iso timestamp>",
      "source": "live|background|replayed",
      "render_event": {
        "type": "approval",
        "request_id": "<request_id>",
        "kind": "command|diff|tool|unknown",
        "payload": {},
        "conversation_id": "<conversation_id>",
        "turn_id": "<turn_id or null>",
        "created_at": "<utc iso timestamp>"
      }
    }
  }
}
```

Notes:

- `request_id` is the canonical key for resolving and invalidating approvals
- `payload` is the frontend-facing serialized approval content
- `render_event` is the exact card-render payload used for replay
- `runtime_signature` is the minimum required staleness check
- `runtime_instance_id` is preferred if the backend can expose a stronger per-runtime token
- `transcript_anchor` is for correct replay placement, not for backend resolution

## Lifecycle

## Approval Request Arrival

When a backend requests approval:

- construct a normalized approval descriptor
- persist it into `meta.json.pending_approvals[request_id]`
- broadcast the live approval event to the frontend
- keep any in-memory resolver object in the backend runtime

The write to `meta.json` must happen before or alongside the live broadcast, not as a later best-effort step.

## Approval Acceptance Or Decline

When the user responds:

- resolve the live backend request using `request_id`
- if resolution succeeds, remove the pending approval from `meta.json`
- append the approval decision record to the transcript as historical state

If backend resolution fails because the request is stale or missing:

- mark the approval invalid
- remove it from `meta.json`
- optionally append an invalidation record for debugging visibility

The pending approval record is operational state. The transcript approval decision is historical state. They are not the same thing.

## Conversation Re Entry

When the user returns to a conversation:

- load `meta.json`
- inspect `pending_approvals`
- validate each pending approval against the currently live backend runtime
- replay only approvals that are still actionable
- invalidate the rest

This is the key distinction:

- transcript replay restores historical entries
- `meta.json.pending_approvals` restores actionable pending state

## Runtime Validation

Pending approvals must not remain actionable across unrelated runtime instances.

Validation rules:

- if the approval runtime signature does not match the current runtime signature, invalidate it
- if the backend can expose a stronger runtime instance ID and it does not match, invalidate it
- if the associated thread or session is gone and cannot be resumed into a compatible state, invalidate it
- if the backend has no live resolver for the request and cannot reconstruct one, invalidate it

Invalidation must remove the approval from `meta.json`.

The system should prefer removing stale approvals over leaving dead approvals visible.

## Backend Responsibilities

Each backend must provide two things:

- a normalized approval descriptor for persistence
- a resolver path keyed by `request_id`

The persistence contract should be shared even if the live resolver implementation differs by backend.

### Codex

The Codex path currently needs a real approval resolver keyed by `request_id`.

The system should not rely on extension-only approval hooks for Codex if Codex remains partly hard-coded in `server.py`.

### Copilot SDK

The Copilot SDK path already has an in-memory resolver model keyed by `request_id`, but it must persist the normalized descriptor into `meta.json` and must validate replayed approvals against current session/runtime identity.

The Copilot event router can continue to own tool-context assembly, but pending approval persistence should be driven from the approval request path, not from arbitrary event replay.

Current state:

- `auto-approve` is working again under the vendored SDK
- non-`auto-approve` policies are still behaving like the older v2 binary accept/decline flow
- `meta.json` is still the correct durable store for pending actionable approvals
- the current gap is the live permission contract, not the choice of `meta.json`

What changed in the vendored/newer SDK contract:

- permission requests arrive as a typed `PermissionRequest` dataclass, not an unstructured dict
- permission results must be returned as `PermissionRequestResult`
- the SDK wire result supports richer fields than the old binary flow:
  - `kind`
  - `rules`
  - `feedback`
  - `message`
  - `path`

That means the Copilot approval path now has three distinct responsibilities:

- normalize the SDK request into a JSON-safe descriptor for `meta.json`
- replay and validate that descriptor through the existing `pending_approvals` model
- translate the user decision back into a proper `PermissionRequestResult`

The earlier `auto-approve` fix only solved the early-return branch. It did not fully update the persisted-request and user-response path for non-`auto-approve` policies.

### Current Copilot SDK Approval Gap

The currently observed Copilot problem is:

- `meta.json` settings still correctly drive conversation-level intent
- `auto-approve` succeeds
- `suggest` and `always-ask` still pass through a mostly v2-style approval model
- the UI and backend currently assume a binary `accept` / `decline` response, while the SDK now supports a richer permission result object

The practical consequences are:

- the live request must be serialized more carefully before persisting to `meta.json`
- the request payload must preserve newer SDK fields such as:
  - `can_offer_session_approval`
  - `possible_paths`
  - `possible_urls`
- enum-like SDK fields such as permission `kind` must be normalized to plain JSON-safe values before persistence or broadcast
- the non-`auto-approve` resolver path must construct a proper `PermissionRequestResult`, not just rely on an older accept/decline assumption

This is a protocol-alignment problem in the Copilot extension. It is not evidence that `meta.json.pending_approvals` is the wrong storage model.

## Codex Ownership Roadmap

The current Codex implementation is acceptable as a proof of concept, but it is still structurally split across two places:

- `server.py` owns app-server shell startup, shell readiness, the reader loop, and direct RPC transport
- `extensions/codex/client.py` owns part of the runtime protocol and part of the extension-facing session logic

That split is workable for now, but it is not the target architecture.

### Current Acceptable State

For the current phase, it is acceptable to keep the authoritative `ensure shell` and app-server process ownership in `server.py`.

That includes:

- framework-shell ownership of the Codex app-server process
- the current app-server reader loop
- the current server-owned RPC waiter map
- the generic Socket.IO and HTTP entry points that already exist

This keeps the existing system stable while the extension surface is still being hardened.

### Near Term Direction

The Codex extension should grow a mirrored backend-ownership path inside `extensions/codex/client.py`.

That means adding extension-owned logic for:

- `ensure shell` or `ensure backend` behavior in the Codex client
- direct app-server RPC write/read ownership inside the extension layer
- extension-owned runtime validation and approval resolution paths
- extension-owned session ensure/resume/start behavior

In this phase, `server.py` can still remain the live owner of the shellspec and framework-shell process, while the Codex extension gains the same shape of backend management that the Copilot SDK extension already demonstrates.

The point of this phase is not to move everything at once. The point is to make the Codex extension capable of taking over cleanly.

### Target End State

The long-term target is:

- `extensions/codex/client.py` talks to the Codex backend directly
- `extensions/codex/client.py` owns Codex runtime ensure/start/resume/read/write logic
- `server.py` no longer contains Codex-specific app-server transport logic

At that end state, `server.py` should be left with only:

- generic HTTP and Socket.IO hook surfaces
- conversation and `meta.json` ownership
- extension orchestration through `extensions/__init__.py`
- generic persistence and replay helpers

The frontend should then only speak Socket.IO or HTTP application actions to `server.py`. It should not need to know about Codex JSON-RPC transport details.

### Why This Matters

This separation is what makes the extension system real instead of nominal.

If Codex remains partly hard-coded in `server.py`, then the extension system is only fully true for non-Codex agents. That is not the intended architecture.

## How To Build An Extension

This section documents the extension surface that exists today, including the hooks that are currently known to work in this repo.

### Package Shape

The expected extension layout is:

- `extensions/<folder>/manifest.json`
- `extensions/<folder>/client.py`
- `extensions/<folder>/router.py` when event translation is needed
- `extensions/<folder>/settings_schema.json` when schema-driven settings are needed

The loader discovers extensions from `extensions/extensions.json` first, then falls back to scanning subfolders.

### Loader Initialization Contract

The loader initializes an extension client module by calling an init function with this shape:

- `init_<type>_manager(...)`
- fallback to `init_<folder>_manager(...)`
- fallback to any `init_*_manager(...)`
- fallback to `init_manager(...)`

The callback arguments currently passed by `server.py` are:

- `extensions_dir`
- `server_root`
- `fws_getter`
- `broadcast_fn`
- `transcript_fn`
- `meta_fns`

### Known Shared Callback Inputs

The currently known callback responsibilities are:

- `fws_getter`
  - access to framework-shells when the extension wants to inspect or manage a process
- `broadcast_fn`
  - emits live frontend events
- `transcript_fn`
  - appends transcript entries for replay
- `meta_fns`
  - currently includes:
    - `load`
    - `save`
    - `upsert_pending_approval`
    - `remove_pending_approval`

Extensions should prefer these shared callbacks instead of importing `server.py` internals directly when a callback already exists for the job.

### Known Extension Handler Hooks

These are the handler hooks currently supported by `extensions/__init__.py`.

- `warm_up_all_extensions(timeout)`
  - optional
  - warms up one handler type and returns readiness by extension ID
- `is_extension_ready(extension_id)`
  - optional
  - reports current readiness state
- `wait_extension_ready(extension_id, timeout)`
  - optional
  - waits until ready
- `list_models()`
  - optional
  - returns extension model list
- `get_settings_schema(extension_id)`
  - optional
  - returns dynamic schema for the settings modal
- `get_runtime_options(extension_id, conversation_id=None, settings=None)`
  - optional
  - returns generic approval/sandbox option descriptors for the shared frontend
- `route_event(extension_id, label, payload, conversation_id, thread_id, turn_id, request_id)`
  - optional
  - translates backend-native live events into internal UI events
- `list_sessions(cwd=None)`
  - optional
  - returns resumable sessions
- `resume_session_with_history(session_id, conversation_id, cwd=None, model=None, settings=None)`
  - optional
  - binds a conversation to an existing backend session
- `hydrate_transcript(session_id, conversation_id, cwd=None, model=None, settings=None)`
  - optional
  - ports historical transcript into the internal transcript format
- `resolve_approval(request_id, decision)`
  - optional
  - resolves a live pending approval
- `validate_pending_approval(conversation_id, request_id, descriptor)`
  - optional
  - determines whether a persisted pending approval is still actionable
- `abort_session(conversation_id)`
  - optional
  - interrupts an active turn or session
- `shutdown_client()`
  - optional
  - performs cleanup on shutdown
- `get_raw_buffer(limit=50)`
  - optional
  - returns extension debug buffer entries

### Known Direct Client Hooks Used By Server

In addition to the generic loader hooks, `server.py` currently calls some client handlers directly when dispatching actions.

The ones we have actively worked with are:

- `handle_message(conversation_id, text, agent_type, settings)`
  - used to send a user turn into an extension backend
- `resume_session_with_history(...)`
  - used by session picker and bind flow
- `hydrate_transcript(...)`
  - used when porting an existing backend session into transcript replay

If a new extension cannot implement all optional hooks up front, `handle_message(...)` plus enough session/meta integration to run a conversation is the minimal practical starting point.

### Router Rule

If an extension has a `router.py`, every `_emit()` that sends live data must have a matching `_record()` with the same fields.

Live and replay must stay structurally identical.

### Codex Specific Caveat

The current Codex extension is not yet a pure example of the intended architecture.

It still reaches back into `server.py` for:

- app-server shell ensuring
- app-server reader setup
- app-server initialization
- RPC request transport

That is acceptable for the current proof-of-concept phase, but it should be treated as transitional technical debt, not as the permanent extension pattern to copy.

## Frontend Responsibilities

The frontend should treat pending approvals from `meta.json` as actionable only after backend validation.

The frontend should not assume that replayed approval cards are valid just because they exist in transcript or cached UI state.

The frontend conversation contract should stay narrow:

- the normal turn path is `conversation_id` plus generic `send_message`
- backend runtime code owns thread/session start, resume, and retry behavior
- approval and sandbox dropdown values should come from backend `get_runtime_options(...)`
- shared frontend files should not hardcode backend-specific policy enums

The approval response contract must use a single canonical field:

- `request_id`

The frontend should not send ambiguous identifiers such as `id` when the backend expects `request_id`.

## Transcript Relationship

Transcript and `meta.json` serve different roles:

- transcript is the historical event log
- `meta.json.pending_approvals` is the actionable pending-state cache

Approval request events may also be written to transcript for historical replay, but transcript presence alone must not decide whether an approval is still actionable.

The backend runtime validation step is authoritative.

## Invalidation Rules

Pending approvals should be removed when:

- they are accepted
- they are declined
- the runtime signature no longer matches
- the runtime instance is gone
- the backend resolver is gone and cannot be reconstructed
- the conversation is rebound to a different live session that makes the request obsolete

Invalidation should be idempotent and safe to perform repeatedly.

## Migration Strategy

Implement in phases:

- fix the live approval response contract first
- add explicit `pending_approvals` storage in `meta.json`
- wire backend validation on conversation load
- remove any older implicit approval replay assumptions once the explicit model is working

This keeps the first fix small while moving toward a correct durable ownership model.

## Copilot SDK Patch Plan

The next Copilot approval pass should be implemented in this order:

1. Normalize the permission request before persistence
- convert SDK-only values such as enum `kind` into plain JSON-safe values
- preserve richer request fields needed by the newer protocol
- ensure the `render_event` and persisted descriptor carry the same normalized fields

2. Keep `meta.json` as the actionable approval cache
- continue storing pending approvals in `meta.json.pending_approvals`
- continue validating them against live runtime/session identity on conversation return
- do not move live SDK objects or callbacks into `meta.json`

3. Fix the non-`auto-approve` resolver path
- keep `request_id` as the canonical durable key
- map user decisions back into explicit `PermissionRequestResult` objects
- stop assuming every non-auto request is only a binary v2-style accept/decline

4. Preserve backward-compatible UI behavior while aligning the backend
- keep the current accept/decline UI working first
- make the Copilot handler explicitly translate those current UI decisions into the richer SDK result object
- only expand the frontend approval UI after the backend contract is stable

5. Expand the descriptor shape where the newer SDK requires it
- include newer permission-request fields that materially affect replay or user understanding
- keep `_emit()` and persisted approval data structurally aligned so replay mirrors live behavior

## Adjacent Follow Up: Termux Wrapper

This is not part of the approval patch, but it is the next adjacent runtime follow-up now that the core Copilot SDK issue is understood.

Current intended direction:

- return `copilot_runtime_manager` to a managed-prefix model under `~/.local/share/copilot_runtime_manager/runtime`
- keep `copilot-runtime-manager` as the admin CLI
- make the host `copilot` path resolve to the managed runtime via symlink takeover
- install `@github/copilot` and `node-pty` in the managed prefix
- copy `pty.node` and `rg` into the managed Copilot tree exactly as the known-good manual workflow does
- keep `agent_log_server` itself unaware of the managed-prefix details and continue resolving `copilot` from `PATH`

That work should proceed separately from the approval-flow patch so the two problem spaces stay isolated.

## Open Questions

- whether `runtime_signature` is sufficient or whether a stronger `runtime_instance_id` is required for all backends
- whether approval request events should also be written into transcript as first-class historical records
- whether invalidation should create a visible transcript/debug entry or remain silent
- whether some approval kinds should deliberately never persist
