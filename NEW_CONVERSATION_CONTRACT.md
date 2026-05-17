# New Conversation Binding and Hydration Contract

This document defines the provider-neutral boundary between ALS-RS and agent extensions for fresh conversations and imported provider sessions.

The server owns conversation identity, persistence, active-view state, transcript ordering, and generic RPC fan-out. Each extension owns provider-specific session creation, resume, history import, and event translation. Data crossing the ALS-RS adapter boundary must use the normalized DTO shapes below, regardless of how a provider fulfills them internally.

## Conversation creation paths

There are two supported creation paths.

### Fresh draft conversation

1. ALS-RS creates a local conversation id and stores user-selected settings.
2. The conversation remains a draft until the first provider session id is returned.
3. On first send, ALS-RS calls the extension through `conversation.send`.
4. The extension starts or attaches to the provider session using its own runtime method.
5. The extension returns a normalized ACK with the provider session id.
6. ALS-RS persists that id as both `thread_id` and `provider_session_id`, marks the conversation active, and appends provider transcript events through the shared transcript DTO path.

### Provider session import / port-in

1. The settings schema session picker calls the extension session-list source and renders normalized session rows.
2. The selected provider session id is removed from persisted settings and sent to ALS-RS as a binding value.
3. ALS-RS calls the extension through `conversation.resume`.
4. The extension resumes or binds the provider session using its own runtime method.
5. The extension returns the same normalized ACK shape used by fresh sends.
6. The extension emits or returns normalized transcript records for hydration. ALS-RS persists those records without parsing provider-specific history formats.

## Picker session DTO

Session list responses must normalize provider-specific rows before crossing into ALS-RS:

```json
{
  "id": "provider-session-id",
  "label": "optional display label",
  "cwd": "/absolute/project/path",
  "created_at": "2026-05-06T12:00:00Z",
  "updated_at": "2026-05-06T12:30:00Z",
  "metadata": {}
}
```

Rules:

- `id` is the provider session/thread id that can later be passed to `conversation.resume`.
- `cwd` stays absolute at the DTO boundary. The frontend may render it relative to `$HOME`.
- `created_at` and `updated_at` are ISO-8601 strings at the DTO boundary. Extensions may derive them from provider-native fields or schema registry metadata before returning the DTO.
- `metadata` may carry provider-specific fields, but shared UI behavior must not depend on provider-specific keys unless declared by schema.

## Resume request DTO

ALS-RS calls the adapter with:

```json
{
  "extension_id": "copilot-sdk",
  "conversation_id": "local-harness-conversation-id",
  "provider_session_id": "provider-session-id",
  "cwd": "/absolute/project/path",
  "settings": {}
}
```

Compatibility aliases accepted at the boundary are `thread_id`, `session_id`, `threadId`, and `sessionId`, but `provider_session_id` is canonical.

## Send request DTO

ALS-RS calls the adapter with:

```json
{
  "extension_id": "copilot-sdk",
  "conversation_id": "local-harness-conversation-id",
  "text": "user message",
  "provider_session_id": "optional-existing-provider-session-id",
  "cwd": "/absolute/project/path",
  "settings": {},
  "attachments": [],
  "toast_context": null
}
```

For a draft conversation, `provider_session_id` is absent. The extension must create or attach a provider session and return it in the ACK.

## ACK DTO

Every successful `conversation.send`, `conversation.start`, and `conversation.resume` response must use this normalized ACK shape:

```json
{
  "ok": true,
  "accepted": true,
  "conversation_id": "local-harness-conversation-id",
  "provider_session_id": "provider-session-id",
  "provider_call_id": "optional-provider-call-id",
  "turn_id": "optional-provider-turn-id",
  "restore_draft": false,
  "metadata": {}
}
```

Rules:

- `provider_session_id` is canonical. Extensions may include legacy `session_id` or `thread_id` for compatibility, but ALS-RS persists only the normalized provider id.
- ALS-RS must only bind a provider id from an ACK when `ok` or `accepted` is `true`.
- `conversation_id` is always the local ALS-RS conversation id. It must never be returned as `provider_session_id`.
- `restore_draft` is only meaningful for failed sends.
- Failed ACKs must not include a bindable provider id.

## Hydration transcript DTO

Hydration rows must already be translated by the extension into the generic transcript/event record shape before ALS-RS persists them:

```json
{
  "conversation_id": "local-harness-conversation-id",
  "role": "assistant",
  "type": "message",
  "text": "normalized transcript text",
  "ts": "2026-05-06T12:30:00Z",
  "metadata": {}
}
```

Rules:

- Extensions own provider-specific transcript parsing and hydration-ignore behavior.
- ALS-RS owns transcript ordering and persistence.
- ALS-RS must not parse Codex rollout JSONL, Copilot event streams, or any future provider-native history format directly.

## Enforcement points

- The Python extension adapter normalizes extension results into the ACK DTO.
- ALS-RS only accepts provider binding ids from successful ACKs.
- Extensions should return canonical `provider_session_id` on every successful fresh start, resume, and send.
- Compatibility aliases exist only to bridge older extension return shapes while the normalized contract is being enforced.
