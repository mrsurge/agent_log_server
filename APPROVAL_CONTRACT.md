# Approval contract

This document defines the ALS-RS approval contract shared by the frontend,
Rust server, Python extension adapter, and extension implementations.

## Live approval request

Extensions emit approval requests as live events with `type: "approval"`.
The event is both the frontend render payload and the source for the server
pending descriptor.

Required fields:

```json
{
  "type": "approval",
  "conversation_id": "conv_...",
  "id": "approval_or_tool_id",
  "request_id": "approval_or_tool_id",
  "kind": "command",
  "request_method": "provider.request.method",
  "request_params": {},
  "payload": {},
  "turn_id": "turn-or-empty",
  "created_at": "2026-..."
}
```

Optional fields such as `card_id`, `tool_call_id`, `subagent_id`, `path`, and
`diff` may be included by an extension when they are part of the rendered
approval card. Approval diffs are request-card previews: custom and fallback
approval renderers should use the shared `renderDiffBlock` helper when it is
available. They are not a substitute for standalone transcript `diff` rows when
the patch body should remain visible as transcript output after the approval
flow.

Provider-native user-input prompts use the same approval event lane. Examples:

- Copilot built-in user input: `request_method: "copilot/user_input/request"`.
- Codex tool user input: `request_method: "item/tool/requestUserInput"`.
- Generic MCP ask-user: `request_method: "agent-pty/ask-user"`.

The MCP ask-user request method is not special at the browser/server transport
boundary; it is one request method carried by the same generic approval DTO.

## Custom request-card assets

Extension manifests may declare custom approval renderers under
`ui.requestCards`. ALS-RS exposes those declarations through
`extension.requestCards.get` and serves module files from:

```text
/api/extensions/{extension_id}/assets/{asset_path}
```

Only extension-local `ui/...` and `static/...` assets are served. Request-card
modules such as `ui/request_cards/copilot_request_card.js` and
`ui/request_cards/codex_ext_request_card.js` are loaded through this route.

Each `ui.requestCards[]` descriptor is a manifest-owned rendering declaration:

```json
{
  "id": "provider-request-card",
  "module": "ui/request_cards/provider_request_card.js",
  "export": "renderRequestCard",
  "matches": [
    {"requestMethod": "provider/request/method", "kind": "optional-kind"}
  ]
}
```

Descriptor semantics:

- `module` is resolved inside the extension package and is exposed to the
  frontend as a server-owned `module_url`; request-card modules should be ES
  modules loaded through that URL, not hardcoded frontend bundle imports.
- `export` defaults to `renderRequestCard`; `default` may also be used by
  setting `"export": "default"`.
- `matches` entries may use `requestMethod` or `request_method`; matching is
  case-insensitive for the request method and exact for `kind` when supplied.
- a matching module receives `{ extensionId, event, card, config, schema,
  body, helpers }` and returns `true` only when it fully rendered the card;
  returning anything else lets the generic fallback renderer handle it.
- modules may optionally export `initializeRequestCardModule(config)` (or the
  legacy `initializeExtensionCardModule`) to receive `{ extensionId, cards,
  schemas }` after load.
- `helpers.renderDiffBlock(container, diff, path)` is the preferred approval
  diff renderer. `helpers.formatDiff(diff, path)` remains a fallback for older
  modules.

## Pending descriptor

The server persists each live approval request in `meta.json` under
`pending_approvals[request_id]` before broadcasting the live approval request, so
an immediate response can resolve against stored state. The frontend-visible
approval request remains the first live approval signal; the follow-up
`conversation.meta.updated` notification is restore/list state, not the primary
live render trigger. The descriptor mirrors the Python legacy shape:

```json
{
  "request_id": "approval_or_tool_id",
  "agent": "codex-ext",
  "kind": "command",
  "request_method": "provider.request.method",
  "request_params": {},
  "payload": {},
  "conversation_id": "conv_...",
  "thread_id": "provider-thread-or-session",
  "turn_id": "turn-or-empty",
  "transcript_anchor": {"turn_id": "turn-or-empty"},
  "source": "live",
  "created_at": "2026-...",
  "updated_at": "2026-...",
  "status": "pending",
  "render_event": {}
}
```

`render_event` must contain the original approval event fields used by the
frontend. On reload, the frontend restores pending approval cards from this
descriptor instead of asking the extension to replay history.

## Approval response

The frontend responds over `/rpc/conversations` method
`conversation.approval.respond`:

```json
{
  "conversation_id": "conv_...",
  "request_id": "approval_or_tool_id",
  "decision": "accept",
  "result": {"decision": "accept"}
}
```

ALS-RS resolves the pending descriptor, routes to the selected extension, and
calls adapter method `approval.respond` with the same `conversation_id`,
`request_id`, `extension_id`, and normalized `result`.

Extensions own provider-specific fulfillment. The boundary return is:

```json
{"ok": true, "resolved": true}
```

If the extension cannot resolve the provider-side pending request, ALS-RS treats
the approval as stale and removes the pending descriptor.

## Handoff transcript and live event

After a successful extension resolution, ALS-RS appends a transcript entry:

```json
{
  "role": "approval",
  "event": "approval_decision",
  "status": "accepted",
  "decision": "accept",
  "result": {"decision": "accept"},
  "request_method": "provider.request.method",
  "payload": {},
  "request_id": "approval_or_tool_id",
  "item_id": "card-or-request-id",
  "card_id": "card-id-if-present",
  "turn_id": "turn-or-empty"
}
```

The live notification is `conversation.approval.handoff` with
`type: "approval_handoff"` and the same approval identity/result fields. The
recorded transcript metadata (`order_id`, `card_id`, and `ask_user_msg_id` when
present) is merged into the live handoff before broadcasting.

## Enforcement points

- Extensions emit one generic `type: "approval"` request shape.
- ALS-RS persists `pending_approvals` from that live event; extensions do not
  write Rust `meta.json` directly.
- `conversation.approval.respond` is the only frontend response method.
- Adapter method `approval.respond` is the only extension fulfillment method.
- Successful responses append a `role: "approval"` transcript row before
  removing the pending descriptor.
