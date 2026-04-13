# Socket.IO Envelope Schema — `/appserver` Namespace

All client↔server communication uses a single Socket.IO namespace: `/appserver`.

## Compatibility status

This document describes the current `/appserver` compatibility transport.

- `/appserver` is the live compatibility shim that the current frontend and TE2 relay already speak
- future JSON-RPC namespaces are expected to be added beside `/appserver`, not by silently rewriting this contract in place
- do not treat `/api/appserver/rpc` as an established historical contract; the real compatibility surface is the flat `/appserver` event model documented here
- once the frontend and TE2 relay learn the new JSON-RPC namespaces, `/appserver` should remain an adapter layer until the migration is complete

## Transport Summary

| Direction | Mechanism | Notes |
|-----------|-----------|-------|
| Client→Server | `socket.emit("event_name", data, ackCallback)` | Request/response via SIO ack |
| Server→Client | `socketio_server.emit("appserver_event", event)` | Broadcast, no ack |
| Fallback | `sioCall()` degrades to HTTP POST/GET | Automatic when socket is disconnected |

---

## Request Envelope (Client → Server)

Each SIO event carries a flat data object. The event name IS the action — no nested `event` field needed.

```json
{
  "conversation_id": "uuid-or-null",
  "text": "user message",
  "...": "event-specific fields"
}
```

The server's SIO ack callback returns the response directly (no wrapping envelope in practice). On error, the server returns `{"__error": "message"}`.

---

## Ack Envelope (Server → Client)

### Success
The ack value is the response payload directly (dict/list), e.g.:
```json
{ "conversation_id": "abc", "settings": {...}, "thread_id": "..." }
```

### Error
```json
{ "__error": "Conversation not found" }
```

The frontend `sioCall()` normalizes this: if `ack.__error` exists, it returns `{ ok: false, error: ack.__error }`.

### Future-compatible envelope (planned)
```json
{
  "ok": true,
  "request_id": "client-generated-uuid",
  "data": { ... }
}
```
```json
{
  "ok": false,
  "request_id": "client-generated-uuid",
  "error": { "code": "NOT_FOUND", "message": "Conversation not found" }
}
```

#### Error Codes
| Code | Type | Meaning |
|------|------|---------|
| `NOT_FOUND` | business | Resource doesn't exist |
| `INVALID_REQUEST` | business | Missing/malformed fields |
| `AGENT_ERROR` | business | Extension handler failed |
| `UNAUTHORIZED` | business | Not allowed |
| `INTERNAL_ERROR` | transport | Unexpected server failure |
| `TIMEOUT` | transport | Handler didn't respond in time |

---

## Registered Event Names (Client→Server)

### Real-time / Agent Control
| Event | Payload | Returns | Notes |
|-------|---------|---------|-------|
| `send_message` | `{conversation_id, text}` | `{ok: true}` | User message to agent |
| `shell_exec` | `{conversation_id, command, terminal_mode?}` | `{callId, ...}` | Direct shell command |
| `rpc` | `{method, params?, id?}` | `{...rpc_result}` | Codex JSON-RPC passthrough |
| `interrupt` | `{conversation_id?}` | `{ok: true}` | Stop current agent turn |
| `approval_response` | `{conversation_id, id, decision}` | `{ok: true}` | Accept/decline tool approval |
| `approval_record` | `{status, diff?, path?, item_id?}` | `{ok: true}` | Record approval to transcript |
| `compact` | `{conversation_id?}` | `{ok: true}` | Compact conversation context |

### Conversation CRUD
| Event | Payload | Returns | Notes |
|-------|---------|---------|-------|
| `conversation_create` | `{settings?}` | `{conversation_id, ...meta}` | Create new conversation |
| `conversation_get` | `{conversation_id?}` | `{...meta}` | Get conversation meta (null = active) |
| `conversation_meta` | `{conversation_id}` | `{...meta}` | Get specific conversation meta |
| `conversations_list` | `{}` | `{items: [...], active_conversation_id}` | List all conversations |
| `conversation_select` | `{conversation_id, view?}` | `{ok: true}` | Switch active conversation |
| `conversation_delete` | `{conversation_id}` | `{ok: true}` | Delete conversation |
| `conversation_update` | `{conversation_id, settings}` | `{ok: true}` | Update settings/meta |
| `conversation_draft` | `{conversation_id, draft}` | `{ok: true}` | Save draft text |
| `conversation_bind_rollout` | `{rollout_id}` | `{ok: true}` | Bind codex rollout |

### Data / Settings
| Event | Payload | Returns | Notes |
|-------|---------|---------|-------|
| `set_view` | `{view}` | `{ok: true}` | Switch frontend view (splash/conversation) |
| `get_models` | `{}` | `{result: {data: [...]}}` | List available models |
| `get_extensions` | `{}` | `{extensions: [...]}` | List loaded extensions |
| `get_extension_models` | `{extension_id}` | `{models: [...]}` | Extension-specific model list |
| `get_sessions` | `{extension_id}` | `{sessions: [...]}` | List extension sessions |
| `session_resume` | `{extension_id, session_id}` | `{ok: true}` | Resume extension session |
| `get_rollouts` | `{}` | `{items: [...]}` | List codex rollouts |
| `get_rollout_preview` | `{rollout_id}` | `{items: [...], token_total?}` | Preview rollout entries |
| `get_status` | `{}` | `{running, shell_id}` | Server/agent status |
| `get_transcript` | `{conversation_id?}` | `{entries: [...]}` | Full transcript |
| `get_transcript_range` | `{conversation_id?, offset, limit}` | `{entries: [...], total}` | Paginated transcript |
| `get_extension_settings_schema` | `{extension_id}` | `{schema: [...]}` | Extension settings schema |

### App Lifecycle
| Event | Payload | Returns | Notes |
|-------|---------|---------|-------|
| `app_start` | `{}` | `{ok: true, shell_id}` | Start codex app-server |
| `app_stop` | `{}` | `{ok: true}` | Stop codex app-server |

---

## Server→Client Events (Broadcast)

All broadcast events use a single SIO event name: `appserver_event`

```json
{
  "type": "assistant_delta",
  "conversation_id": "uuid",
  "text": "partial response...",
  "...": "type-specific fields"
}
```

### Event Types
| Type | Key Fields | Notes |
|------|-----------|-------|
| `assistant_delta` | `text` | Streaming assistant text |
| `assistant_end` | — | Turn complete |
| `reasoning_delta` | `text` | Streaming reasoning/thinking |
| `reasoning_end` | — | Reasoning block complete |
| `shell_begin` | `callId, command` | Shell command started |
| `shell_delta` | `callId, text` | Shell output chunk |
| `shell_end` | `callId, exitCode, stdout, stderr` | Shell command finished |
| `diff` | `text, path` | File change diff (post-execution) |
| `diff_declined` | `text, path` | Declined approval diff |
| `approval` | `id, payload` | Tool approval request |
| `activity` | `text, spinner?` | Status activity indicator |
| `token_count` | `input, output, total` | Token usage |
| `context_compacted` | `summary?` | Context window compacted |
| `error` | `text` | Error message |
| `tool_begin` | `callId, command` | Tool execution started |
| `tool_delta` | `callId, text` | Tool output chunk |
| `tool_end` | `callId, text?` | Tool execution finished |
| `command_result` | `exitCode, stdout, stderr` | Legacy shell result |
| `host_ui` | `...host_fields` | Host UI state update |
| `meta_envelope_injected` | `conversation_id` | Meta envelope was updated |
