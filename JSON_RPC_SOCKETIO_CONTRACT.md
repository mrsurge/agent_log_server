# JSON-RPC 2.0 Socket.IO Contract

Status: proposed public runtime contract for the harness frontend/backend transport.

This document defines the intended replacement for the current overloaded `/appserver`
flat-event Socket.IO contract. The goal is not to change the transcript model or
session-lifecycle contract. The goal is to give the public runtime transport a typed,
namespaced, JSON-RPC 2.0 shape before the frontend grows more notification-heavy
features such as final-message toasts and toast quick reply.

## Goals

- replace flat Socket.IO event-name RPC with a real JSON-RPC 2.0 transport
- split public traffic by concern instead of multiplexing everything through `/appserver`
- keep runtime traffic Socket.IO-only; no HTTP fallback for the live contract
- preserve transcript-first replay and existing extension lifecycle invariants
- give conversation summaries, live toasts, and toast quick reply a first-class shared lane
- keep extension-specific semantics out of the public transport surface
- make TE2 relay requirements explicit instead of assuming new namespaces will proxy automatically

## Non-goals

- changing the transcript source of truth away from local `transcript.jsonl`
- changing the existing harness reload contract for already-started conversations
- changing extension-internal provider protocols such as Codex app-server JSON-RPC or ACP
- replacing `/ipc` or `/sidebar_ipc` with this contract in the first pass

## Namespace topology

Public runtime traffic is split across three Socket.IO namespaces:

| Namespace | Purpose | Traffic profile |
|-----------|---------|-----------------|
| `/rpc/conversations` | conversation lifecycle, send/interrupt/compact, live turn events, replay, summaries, toasts, quick reply context | hot, ordered, high volume |
| `/rpc/settings` | config, models, extension schemas, runtime options, sessions, extension/package CRUD | colder, request/response heavy |
| `/rpc/ui` | host UI state, view state, file/open actions, filesystem helpers, PTY/runtime presentation helpers | medium, shell/host oriented |

Existing non-public namespaces remain separate:

| Namespace | Status | Notes |
|-----------|--------|-------|
| `/ipc` | internal-only | private machine-to-machine control plane; not part of the public frontend contract |
| `/sidebar_ipc` | TE2/sidebar-only | host integration namespace; stays outside the public JSON-RPC split |
| `/appserver` | compatibility shim | existing flat namespace kept for TE2 relay and legacy frontend modules while JSON-RPC is added beside it |

## Compatibility shim policy

`/appserver` remains the current compatibility transport during the JSON-RPC migration.

Rules:

- add the new JSON-RPC namespaces beside `/appserver`, not in place of it
- keep TE2 relay and legacy frontend modules working through `/appserver` until they explicitly learn the new RPC transport
- do not treat `/api/appserver/rpc` as a historical source-of-truth contract; the real compatibility surface is the flat `/appserver` event model
- treat `/appserver` as an adapter/shim layer once the JSON-RPC transport becomes canonical

## Why not dynamic per-conversation namespaces

Dynamic per-conversation namespaces were considered and rejected for this contract.

Reasons:

- final-message summaries must update inactive conversation cards, not just the active one
- toast notifications may target conversations other than the currently visible thread
- toast quick reply still routes through the canonical conversation send path
- conversation list, pinned ordering, previews, unread state, and meta updates are all cross-conversation concerns
- TE2 relay passthrough already requires explicit namespace wiring; multiplying namespaces by conversation would make relay complexity worse
- browser socket count and lifecycle churn would grow with conversation count for little gain

Contract rule:

- `conversation_id` stays in the JSON-RPC params/payload
- public namespaces are stable by concern, not per conversation

## Transport shape

Each public RPC namespace uses a single request event and a single notification event:

- client -> server request or JSON-RPC notification: Socket.IO event `rpc`
- server -> client JSON-RPC notification: Socket.IO event `rpc.notify`

Responses to client requests travel through the Socket.IO ack for the originating `rpc`
emit. The server does not send JSON-RPC responses as unsolicited broadcast events.

### Client request

The payload on `rpc` is a JSON-RPC 2.0 request object:

```json
{
  "jsonrpc": "2.0",
  "id": "req_01JABC...",
  "method": "conversation.send",
  "params": {
    "conversation_id": "conv_123",
    "text": "hello"
  }
}
```

### Client notification

If the client intentionally sends a JSON-RPC notification, it omits `id`:

```json
{
  "jsonrpc": "2.0",
  "method": "conversation.toast.dismiss",
  "params": {
    "conversation_id": "conv_123",
    "toast_id": "toast_456"
  }
}
```

### Server success response

The Socket.IO ack for a request carries a JSON-RPC 2.0 response object:

```json
{
  "jsonrpc": "2.0",
  "id": "req_01JABC...",
  "result": {
    "conversation_id": "conv_123",
    "accepted": true
  }
}
```

### Server error response

```json
{
  "jsonrpc": "2.0",
  "id": "req_01JABC...",
  "error": {
    "code": -32602,
    "message": "Invalid params",
    "data": {
      "reason": "conversation_id is required"
    }
  }
}
```

### Server notification

Server-initiated notifications are emitted on `rpc.notify`:

```json
{
  "jsonrpc": "2.0",
  "method": "conversation.message.delta",
  "params": {
    "conversation_id": "conv_123",
    "turn_id": "turn_7",
    "id": "msg_7_1",
    "text": "partial text"
  }
}
```

## Envelope rules

- `jsonrpc` must always be `"2.0"`
- public frontend request ids should be strings; the backend must echo them byte-for-byte
- there is no `{"__error": ...}` compatibility shape on the new namespaces
- there is no event-name-per-method transport on the new namespaces; the method lives inside JSON-RPC
- batch support is reserved for later; initial rollout only requires single-request objects
- transport adapters may expose promise helpers, but the wire format stays JSON-RPC 2.0

## Error model

Use standard JSON-RPC numeric error codes plus structured `error.data`.

Recommended numeric codes:

| Code | Meaning |
|------|---------|
| `-32600` | invalid request |
| `-32601` | method not found |
| `-32602` | invalid params |
| `-32603` | internal error |
| `-32000` to `-32099` | server-defined runtime failures |

Recommended `error.data.code` values:

- `NOT_FOUND`
- `CONFLICT`
- `UNAVAILABLE`
- `DEPENDENCY_UNMET`
- `AUTH_REQUIRED`
- `TIMEOUT`
- `RESTORE_DRAFT`

If a failed send should restore the composer draft, the response error must carry that in
`error.data`, not in a parallel transport envelope.

Example:

```json
{
  "jsonrpc": "2.0",
  "id": "req_send_1",
  "error": {
    "code": -32010,
    "message": "Send failed",
    "data": {
      "code": "RESTORE_DRAFT",
      "conversation_id": "conv_123",
      "restore_draft": true
    }
  }
}
```

## Method naming rules

- use dotted method names
- keep resource names stable and generic
- do not encode extension ids into public method names
- conversation-affecting methods and notifications must carry `conversation_id` explicitly
- prefer semantic notification names over a single generic `event` method
- reuse existing shared live/transcript field names where contracts already exist

Examples:

- `conversation.send`
- `conversation.message.delta`
- `extension.settingsSchema.get`
- `hostUi.get`

## `/rpc/conversations`

This namespace is the hot path. It owns conversation lifecycle, live turn traffic,
transcript replay, preview/summary updates, and live-only toast notifications.

### Request methods

| Method | Purpose |
|--------|---------|
| `conversation.create` | create a new conversation |
| `conversation.get` | fetch one conversation meta/state snapshot |
| `conversation.list` | fetch ordered conversation list |
| `conversation.select` | mark the active conversation/view target |
| `conversation.update` | update settings/alias/title/meta-owned fields |
| `conversation.delete` | delete local conversation state |
| `conversation.pins.set` | persist canonical pinned ordering |
| `conversation.draft.set` | persist draft text |
| `conversation.rollout.bind` | bind/import rollout-style source ids when applicable |
| `conversation.send` | send a user message through the active extension |
| `conversation.interrupt` | interrupt the active turn |
| `conversation.compact` | request compaction |
| `conversation.replay.getChunk` | fetch a chunk-framed replay payload from transcript history |

### `conversation.send`

Minimal params:

```json
{
  "conversation_id": "conv_123",
  "text": "reply text"
}
```

Toast-aware quick reply uses the same method, not a second extension-specific send RPC.

Optional toast context:

```json
{
  "conversation_id": "conv_123",
  "text": "follow-up from toast",
  "toast_context": {
    "toast_id": "toast_456",
    "kind": "turn_summary",
    "turn_id": "turn_7",
    "assistant_id": "msg_7_1"
  }
}
```

Result shape:

```json
{
  "conversation_id": "conv_123",
  "accepted": true
}
```

Notes:

- the ack only means the send request was accepted by the runtime transport
- actual user/assistant turn results still come through the normal live notification stream
- the quick-reply path reuses the canonical send transport and therefore preserves the normal transcript/user-message flow

### `conversation.replay.getChunk`

Replay stays transcript-first. This method does not ask the extension/provider for history.
It reads the harness-owned local transcript and returns it in chunk-framed JSONL form.

Recommended params:

```json
{
  "conversation_id": "conv_123",
  "cursor": {
    "offset": 0
  },
  "max_entries": 500,
  "max_bytes": 524288,
  "format": "jsonl"
}
```

Recommended result:

```json
{
  "conversation_id": "conv_123",
  "replay_id": "replay_01JABC...",
  "frame": {
    "format": "jsonl",
    "offset": 0,
    "item_count": 173,
    "total_count": 173,
    "chunk_index": 0,
    "complete": true,
    "next_cursor": null,
    "jsonl": "{\"role\":\"user\",...}\n{\"role\":\"assistant\",...}\n"
  }
}
```

Rules:

- JSONL remains the replay payload format
- JSON-RPC is the frame around that payload
- current deployments usually return one chunk because the frontend treadmill is effectively `500` items, but the frame shape must support multiple chunks later
- replay chunking is driven by transcript entries and byte size, not by live event names
- this method does not change the existing contract that already-started conversations reload from local transcript only

### Notifications

Use typed semantic methods, not a single generic `conversation.event`.

Core live notification families:

| Method | Notes |
|--------|-------|
| `conversation.message.delta` | shared live assistant delta contract |
| `conversation.message.final` | shared live assistant finalize contract |
| `conversation.reasoning.delta` | shared live reasoning delta contract |
| `conversation.reasoning.final` | shared live reasoning finalize contract |
| `conversation.user.message` | normalized user-message echo when appropriate |
| `conversation.activity` | shared spinner/activity updates |
| `conversation.tool.begin` | shared tool begin contract |
| `conversation.tool.delta` | shared tool delta contract |
| `conversation.tool.end` | shared tool end contract |
| `conversation.command.begin` | shared shell/command begin contract |
| `conversation.command.delta` | shared shell/command delta contract |
| `conversation.command.end` | shared shell/command end contract |
| `conversation.command.result` | compatibility lane for mirrored command-result cards |
| `conversation.diff` | shared diff card contract |
| `conversation.diff.declined` | shared declined-diff contract |
| `conversation.approval.request` | shared approval request contract |
| `conversation.approval.handoff` | resolved approval handoff/update |
| `conversation.token.updated` | shared token/context usage contract |
| `conversation.context.compacted` | shared compacted-context contract |
| `conversation.error` | shared visible error contract |
| `conversation.warning` | shared warning contract |
| `conversation.status` | shared turn/runtime status updates |
| `conversation.thought` | live-only thought/ribbon contract |
| `conversation.subagent.start` | shared subagent container start |
| `conversation.subagent.end` | shared subagent container end |
| `conversation.plan` | shared plan snapshot contract |
| `conversation.plan.update` | shared incremental/full plan update contract |
| `conversation.plan.state` | shared plan-doc state contract |
| `conversation.mode.changed` | shared runtime mode-change contract |

These methods should preserve the existing semantic field names documented in
`TRANSCRIPT_CARD_CONTRACTS.md` rather than inventing namespace-specific payload keys.

### Preview and summary notifications

Conversation-list/meta updates need their own typed lane instead of being inferred from
toast state or transcript deltas.

Use:

- `conversation.preview.updated`
- `conversation.meta.updated`
- `conversation.draft.updated`
- `conversation.mention.inserted`

Recommended `conversation.preview.updated` params:

```json
{
  "conversation_id": "conv_123",
  "preview_text": "Normalized final assistant summary...",
  "preview_source": "assistant_final",
  "turn_id": "turn_7",
  "assistant_id": "msg_7_1",
  "updated_at": "2026-01-07T13:41:44Z"
}
```

Rules:

- preview/meta updates are cross-conversation signals; they are one of the main reasons per-conversation namespaces were rejected
- preview updates are not toast replacements
- preview updates are durable UI state, unlike live-only toasts

### Toast notifications

Use `conversation.toast` for live-only toast state.

The payload shape for the toast body follows the shared `toast` contract in
`TRANSCRIPT_CARD_CONTRACTS.md`.

Key rules from that contract:

- `kind: "turn_summary"` is the canonical end-of-turn assistant summary toast
- toast is live-only UI state and is not persisted as transcript history
- toast must not update conversation previews by itself
- reply-capable toast UX stays generic and data-driven
- when reply is enabled, the reply path targets the toast's `conversation_id`

Required payload fields:

- `conversation_id`
- `id`
- `kind`
- `message`

Useful optional fields:

- `turn_id`
- `title`
- `variant`
- `assistant_id`
- `duration_ms`
- `reply_enabled`
- `reply_label`
- `expanded_max_lines`
- `action`

Important rule:

- when emitting `conversation.toast`, continue emitting the underlying assistant/turn semantic events too; toast supplements live UX and does not replace the real conversation events

### Reserved toast request methods

Reserved, optional, and not required for the first implementation:

- `conversation.toast.dismiss`
- `conversation.toast.action`

These exist only for shared toast runtime state if dismissal/action syncing later needs a
real round trip. They do not change the rule that quick reply itself reuses
`conversation.send`.

## `/rpc/settings`

This namespace is the colder CRUD/config surface. It owns extension discovery, model
lists, settings schemas, runtime options, extension sessions, and package-management style
operations.

### Request methods

| Method | Purpose |
|--------|---------|
| `config.get` | fetch app-server config snapshot |
| `config.update` | persist app-server config changes |
| `models.list` | fetch generic top-level model list when applicable |
| `extensions.list` | fetch extension registry state |
| `extensions.reload` | reload extension registry/handlers |
| `extension.settingsSchema.get` | fetch a schema-driven settings surface |
| `extension.runtimeOptions.get` | fetch runtime options/quick controls |
| `extension.models.list` | fetch extension-specific model list |
| `extension.sessions.list` | fetch extension session picker entries |
| `extension.session.bind` | bind/resume a selected extension session to a harness conversation |
| `extension.requestCards.get` | fetch request-card config/schema |
| `extension.uiFeatures.get` | fetch manifest-driven frontend feature flags |
| `extension.plan.get` | fetch extension-provided plan/todo state |
| `extension.package.install` | install/update an extension package |
| `extension.package.remove` | remove an installed extension package |
| `rollouts.list` | list rollout/import candidates |
| `rollouts.preview` | preview rollout/import contents |

### Notifications

Recommended notifications:

- `extensions.updated`
- `extension.status.updated`
- `config.updated`
- `extension.runtimeOptions.updated`
- `extension.plan.updated`

Rules:

- this namespace is request/response heavy; notification volume should stay comparatively low
- extension-specific data still stays behind generic method names plus `extension_id` params
- schema-driven settings remain the source of truth for extension settings surfaces

## `/rpc/ui`

This namespace owns host-shell and presentation commands that are not part of the hot
conversation event stream.

### Request methods

| Method | Purpose |
|--------|---------|
| `view.get` | fetch current frontend view state |
| `view.set` | switch splash/conversation/other top-level view state |
| `hostUi.get` | fetch TE2/host UI state |
| `hostUi.recheck` | trigger lazy host/sidebar recheck |
| `file.open` | open a file/path in the host editor |
| `url.open` | open an external URL through the backend opener |
| `filesystem.list` | browse files/dirs for picker flows |
| `filesystem.search` | search files for picker flows |
| `pty.stdin` | write to the active PTY/runtime shell |
| `pty.resize` | resize the active PTY/runtime shell |
| `runtime.status.get` | fetch runtime status for host surfaces |
| `runtime.start` | start runtime shell/app surface when user-invoked |
| `runtime.stop` | stop runtime shell/app surface when user-invoked |

### Notifications

Recommended notifications:

- `view.changed`
- `hostUi.updated`
- `runtime.status.updated`
- `pty.output`

Rules:

- this namespace is for shell/host presentation and control, not for extension conversation semantics
- open-file/open-url methods should remain generic and host-owned
- host UI notifications should not be tunneled through conversation event methods

## Frontend module layout

New frontend transport modules for this migration should be written in TypeScript.

Planned TypeScript module layout under `agent_log_server/static/js/codex_agent/`:

- `rpc/transport.ts`
- `rpc/namespaces.ts`
- `rpc/registry.ts`
- `rpc/conversations/client.ts`
- `rpc/settings/client.ts`
- `rpc/ui/client.ts`

Migration guidance:

- existing JS modules may import these TypeScript modules as non-behavioral placeholders first so parallel work can proceed without ownership collisions
- `events/socket.js` is the natural legacy anchor for transport + namespace concerns
- `orchestrator/rpc_flow.js` is the natural legacy anchor for the method/notification registry boundary
- `orchestrator/session_flow.js` and `transcript_loader.js` are the natural legacy anchors for conversation RPC client work
- `settings/ui_flow.js` is the natural legacy anchor for the settings RPC client
- `markdown.js` is a natural legacy anchor for the UI RPC client because it already routes file/open-url behavior

## Transcript and replay invariants

This transport contract does not change the existing harness lifecycle rules:

- existing harness conversations still reload from local `transcript.jsonl`
- bound remote/provider sessions stay cold until the first new send
- first new send may fail against a cold backend and trigger the extension's lazy resume/retry path
- replay/load noise must still be suppressed according to the existing extension lifecycle contracts

In other words:

- live notifications are a runtime transport concern
- replay payloads are a transcript transport concern
- the transcript remains the sole replay source of truth

## Alignment with transcript card contracts

This contract intentionally reuses the existing shared semantic contracts.

Rules:

- typed JSON-RPC conversation notifications should carry the same semantic fields the frontend already expects from shared contracts
- routers still must choose the most specific generic contract available
- `toast` stays live-only and does not become a transcript row
- `error` stays the shared visible conversation error contract
- token/footer/context updates still use the backend-owned shared contract

`TRANSCRIPT_CARD_CONTRACTS.md` remains the card-shape contract. This document defines the
transport framing and namespace split around those semantic payloads.

## TE2 relay requirements

The TE2 relay does not wildcard-forward namespaces. Every public namespace introduced by
this contract must be explicitly mounted and forwarded by the relay layer.

That means:

- `/rpc/conversations` needs its own relay namespace
- `/rpc/settings` needs its own relay namespace
- `/rpc/ui` needs its own relay namespace
- each relay namespace must forward both `rpc` and `rpc.notify`

Adding the contract doc alone does not make proxied TE2 paths work. Relay passthrough must
be implemented deliberately for each namespace.

## Migration mapping from the current `/appserver` contract

| Current shape | New namespace | New method |
|---------------|---------------|------------|
| `send_message` | `/rpc/conversations` | `conversation.send` |
| `interrupt` | `/rpc/conversations` | `conversation.interrupt` |
| `compact` | `/rpc/conversations` | `conversation.compact` |
| `conversation_create` | `/rpc/conversations` | `conversation.create` |
| `conversation_get` / `conversation_meta` | `/rpc/conversations` | `conversation.get` |
| `conversations_list` | `/rpc/conversations` | `conversation.list` |
| `conversation_select` | `/rpc/conversations` | `conversation.select` |
| `conversation_update` | `/rpc/conversations` | `conversation.update` |
| `conversation_delete` | `/rpc/conversations` | `conversation.delete` |
| `conversation_draft` | `/rpc/conversations` | `conversation.draft.set` |
| `conversation_bind_rollout` | `/rpc/conversations` | `conversation.rollout.bind` |
| `get_transcript_range` | `/rpc/conversations` | `conversation.replay.getChunk` |
| `get_extensions` | `/rpc/settings` | `extensions.list` |
| `get_extension_models` | `/rpc/settings` | `extension.models.list` |
| `get_sessions` | `/rpc/settings` | `extension.sessions.list` |
| `session_resume` | `/rpc/settings` | `extension.session.bind` |
| `get_extension_settings_schema` | `/rpc/settings` | `extension.settingsSchema.get` |
| `get_host_ui` | `/rpc/ui` | `hostUi.get` |
| `sidebar_recheck` | `/rpc/ui` | `hostUi.recheck` |
| `set_view` | `/rpc/ui` | `view.set` |
| `te2_agent_open` | `/rpc/ui` | `file.open` |
| `open_external_url` | `/rpc/ui` | `url.open` |

Legacy broadcast `appserver_event.type` mappings:

| Current `type` | New namespace | New notification |
|----------------|---------------|------------------|
| `assistant_delta` | `/rpc/conversations` | `conversation.message.delta` |
| `assistant_finalize` / `assistant_end` | `/rpc/conversations` | `conversation.message.final` |
| `reasoning_delta` | `/rpc/conversations` | `conversation.reasoning.delta` |
| `reasoning_finalize` / `reasoning_end` | `/rpc/conversations` | `conversation.reasoning.final` |
| `tool_begin` | `/rpc/conversations` | `conversation.tool.begin` |
| `tool_delta` | `/rpc/conversations` | `conversation.tool.delta` |
| `tool_end` | `/rpc/conversations` | `conversation.tool.end` |
| `token_count` | `/rpc/conversations` | `conversation.token.updated` |
| `context_compacted` | `/rpc/conversations` | `conversation.context.compacted` |
| `error` | `/rpc/conversations` | `conversation.error` |
| `toast` | `/rpc/conversations` | `conversation.toast` |
| `host_ui` | `/rpc/ui` | `hostUi.updated` |

## Implementation order

Recommended rollout:

1. create the TypeScript frontend placeholder modules and reserve their ownership boundaries from the current JS modules
2. add the three new namespaces beside compatibility `/appserver`
3. add a thin adapter that maps current server handlers/events into JSON-RPC method form
4. migrate the frontend transport helpers to namespace-aware JSON-RPC calls
5. migrate replay onto `conversation.replay.getChunk`
6. migrate summary/preview notifications and toast runtime
7. add toast quick reply on top of `conversation.send` with `toast_context`
8. remove the old flat `/appserver` request contract once TE2 relay and frontend consumers are fully migrated

## Summary

The intended public runtime shape is:

- three stable public Socket.IO namespaces:
  - `/rpc/conversations`
  - `/rpc/settings`
  - `/rpc/ui`
- JSON-RPC 2.0 envelopes over Socket.IO
- typed semantic conversation notifications instead of flat `appserver_event.type`
- transcript replay as chunk-framed JSONL over RPC
- final-message previews and live-only toasts as separate first-class signals
- toast quick reply implemented by reusing `conversation.send`, not by inventing a second send protocol

This gives the harness a coherent transport split before notification-heavy UX grows any
further, while keeping the existing transcript-first and extension-lifecycle contracts intact.
