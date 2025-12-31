# Codex App-Server Protocol: Practical Usage Guide

This is a compact, implementation-first guide to the JSON-RPC schema in `my-schema/`. It focuses on what you need to build UI and glue code without memorizing the entire schema.

---

## 0) Transport Model (stdio)

The app-server speaks JSON-RPC over stdio:

- **Client → Server:** JSON-RPC Request (with `id`).
- **Server → Client:** JSON-RPC Response (matching `id`) and Notifications.

Backend bridge responsibilities:
- Spawn app-server (framework-shells).
- Send requests, route responses.
- Forward notifications to UI.

---

## 1) Core Request Flow (Minimum Viable)

### 1.1 Initialize
**Purpose:** handshake, capabilities, config.

Request:
- `method: initialize`
- `params`: client info + optional config.

Response:
- server capabilities, default config.

UI:
- Hide; do on connect.

---

### 1.2 Start or Resume a Thread
**Thread == conversation container.**

- `thread/start` → create new thread
- `thread/resume` → resume prior thread
- `thread/list` → list prior threads

UI:
- “New Thread” button → `thread/start`
- “Resume” button → `thread/resume`
- “Recent Threads” panel → `thread/list`

---

### 1.3 Start a Turn
**Turn == one user prompt + model response.**

Request:
- `turn/start`
- Includes user input, project root, and policy overrides.

Notifications:
- `TurnStartedNotification`
- `ItemStartedNotification`
- `ItemCompletedNotification`
- `TurnCompletedNotification`
- `ErrorNotification`

UI:
- Timeline view, streaming output panel.

---

## 2) Approvals (User-in-the-loop)

App-server asks for permission for sensitive actions.

### 2.1 Command execution approval
Request (server → client):
- `item/commandExecution/requestApproval`

Response (client → server):
- `CommandExecutionRequestApprovalResponse`
- Decision: `approved` or `denied`

### 2.2 File change approval
Request (server → client):
- `item/fileChange/requestApproval`

Response (client → server):
- `FileChangeRequestApprovalResponse`

UI:
- Dedicated “Approvals” panel with diff/command summary and approve/deny.

Legacy:
- `applyPatchApproval` and `execCommandApproval` still exist in schema.

---

## 3) Diffs & Patch Flow

Where diffs appear:
- In `ItemCompletedNotification` or `TurnCompletedNotification`.
- May include unified diff text.

UI:
- Extract diff blocks and show in a “Diffs” pane.
- If `applyPatchApproval` flow is used, surface “Apply / Deny”.

---

## 4) Sandboxing & Policies

Policy inputs show up in request params for `thread/start` and `turn/start`:

- Sandbox mode (read-only, workspace-write, full).
- Approval policy (untrusted / on-failure / never).
- Runtime environment (env vars, cwd).

UI:
- Use a “Safety” section in the thread view.
- Store defaults in a profile per project.

---

## 5) Errors & Recovery

Errors can arrive in:
- JSON-RPC Error responses (request failed)
- `ErrorNotification`
- `TurnCompletedNotification` with error info

UI:
- Show errors in the timeline.
- Offer “resume thread” or “retry turn”.

---

## 6) Thread & Turn Lifecycle (Cheat Sheet)

```
initialize →
thread/start →
turn/start →
  TurnStarted →
  ItemStarted (tool calls, model streaming) →
  ItemCompleted →
TurnCompleted
```

Resume:
```
thread/resume →
turn/start
```

---

## 7) Minimal JSON-RPC Envelope (Example)

Client request:
```
{"jsonrpc":"2.0","id":1,"method":"thread/start","params":{...}}
```

Server response:
```
{"jsonrpc":"2.0","id":1,"result":{...}}
```

Server notification:
```
{"jsonrpc":"2.0","method":"TurnStartedNotification","params":{...}}
```

---

## 8) Practical “Don’t Overthink It” Defaults

- Use `thread/start` + `turn/start` for 90% of workflows.
- Treat approvals as a separate queue; they unblock turns.
- Log all notifications; map them to a timeline UI.
- Start with a minimal policy UI (read-only / safe / yolo).

---

## 9) How to Extend Later

- Add multi-agent indexing (from `AGENT_INDEX_MVP.md`).
- Add diff application preview (apply patch flow).
- Expose file search or tool calls as UI panels.

