# TE2 <-> App Worker UDS Backchannel Plan

## Goal

Create a same-device backend-to-backend control plane between TE2 host backend and app worker backend using a Unix domain socket (UDS), driven by shellspec-injected runtime flags, to replace browser-mediated mention/MCP routing.

## Scope (Phase 1)

1. Add a UDS transport channel for host <-> app worker JSON-RPC.
2. Drive socket discovery via shellspec/env.
3. Implement one end-to-end method first: `mention.resolve`.
4. Keep existing HTTP/web path intact during rollout, but make UDS the preferred path.

## Constraints

1. Same-device only.
2. No fallback-as-primary behavior.
3. Canonical path handling is required because runtime may start from a symlinked worktree path.
4. Socket permissions must be private (`0600`) and socket lifecycle must be deterministic.

## Canonical Path Invariant

Use resolved absolute paths for all identity-sensitive derivations:

1. `PROJECT_ROOT_CANON = realpath(PROJECT_ROOT)`
2. Socket directory and name must be derived from canonical root + app id + run id.
3. Any lock file / pid file / process-group key derived for this channel must also use canonical root.

This avoids split-brain behavior when logical path and physical path differ (for example, worktree symlink paths).

## Shellspec Contract

Shellspec injects the runtime contract into both backends:

1. `TE_BACKCHANNEL_ENABLED=1`
2. `TE_BACKCHANNEL_TRANSPORT=unix`
3. `TE_BACKCHANNEL_SOCKET=<absolute uds path>`
4. `TE_BACKCHANNEL_APP_ID=<app id>`
5. `TE_BACKCHANNEL_RUN_ID=<run id>`
6. `TE_BACKCHANNEL_PROTOCOL_VERSION=1`

Optional:

1. `TE_BACKCHANNEL_TIMEOUT_MS=2500`
2. `TE_BACKCHANNEL_MAX_INFLIGHT=64`

## RPC Wire Contract

Protocol: JSON-RPC 2.0 over UDS stream.

Request:

```json
{
  "jsonrpc": "2.0",
  "id": "uuid-or-int",
  "method": "mention.resolve",
  "params": {
    "query": "main.py",
    "cwd": "/abs/path"
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": "same-id",
  "result": {
    "items": []
  }
}
```

Error:

```json
{
  "jsonrpc": "2.0",
  "id": "same-id-or-null",
  "error": {
    "code": -32001,
    "message": "unauthorized method",
    "data": {
      "method": "mcp.proxy"
    }
  }
}
```

## Handshake

On connect, both sides perform a strict handshake before serving methods.

Method: `session.hello`

Params:

1. `app_id`
2. `run_id`
3. `protocol_version`
4. `pid`
5. `capabilities` (array)

Rules:

1. Reject mismatched `app_id`, `run_id`, or incompatible protocol version.
2. Reject unknown peers.
3. Store peer capabilities for method gating.

## Allowed Methods (Phase 1)

1. `mention.resolve`
2. `mention.insert`
3. `agent.open`

Deferred:

1. `mcp.proxy` (after allowlist and audit trail are in place)

## Security Model

1. Socket file mode `0600`.
2. Socket parent directory mode `0700`.
3. Remove stale socket before bind after ownership/path validation.
4. Method allowlist required.
5. Structured audit log for request id, method, latency, and error code.

## Failure Behavior

1. Hard timeout per request.
2. Connection reset handling with bounded retry.
3. Distinguish transport errors from method errors.
4. Emit explicit health state for UI/diagnostics.

## Rollout Plan

1. Land transport scaffolding + handshake (no business methods).
2. Implement `mention.resolve` end-to-end.
3. Validate symlinked-root behavior using canonical path checks.
4. Add `mention.insert` and `agent.open`.
5. Add selective `mcp.proxy` only after method policy is locked.

## Test Plan

1. Start host and worker from symlinked worktree path; verify both derive the same canonical socket path.
2. Verify socket creation permissions (`0700` dir, `0600` socket).
3. Verify handshake rejects bad app id / run id / protocol.
4. Verify `mention.resolve` success and timeout paths.
5. Verify stale socket cleanup behavior.
6. Verify group kill still works with app subgrouping (`codex_agent`).

## Open Decisions

1. One socket per app-worker instance vs one socket per run-id namespace.
2. Framing: newline-delimited JSON vs length-prefixed frames.
3. Whether to require peer credential checks beyond filesystem permissions.
