# UI Proposal: App-Server Schema ⇄ Front-End

This proposal maps the Codex app-server JSON-RPC schema to a front-end experience that feels like a “mission control” for agents, while keeping the backend launch/orchestration handled by framework-shells.

Scope: UI-first with explicit schema mappings; backend integration details are deferred but the UI assumes endpoints exist.

---

## 1) Product Goals (UI-first)

- Launch, resume, and monitor agent sessions (threads/turns) from the browser.
- Show live turn output, tool activity, diffs, and approvals.
- Keep state approachable: the UI abstracts JSON-RPC complexity.
- Allow “project-centric” workflows (choose root, instructions, sandbox, agent config).

---

## 2) Primary Screens & Panels

### A) Dashboard (Default)
**Purpose:** Quick launch + recent activity.

UI blocks:
- **Project Picker**: root path, env preset, runtime profile.
- **New Thread**: model, instructions, sandbox, MCP servers, metadata.
- **Recent Threads**: resume/start new, status, last activity.

Schema mapping:
- `thread/list` → populate Recent Threads.
- `thread/start` → create a new thread.
- `thread/resume` → resume selected thread.

---

### B) Thread View (Main Workbench)
**Purpose:** Focused interaction with one active thread.

Left column (Context):
- Project root, instructions, tool policy, sandbox.
- Thread metadata (id, origin, created time, tags).

Center (Conversation & Output):
- User turn composer.
- Streaming assistant output.
- Turn timeline (Started → Items → Completed).

Right column (Ops):
- Approvals queue (commands, file changes).
- Diffs & patches.
- Tool call log.

Schema mapping:
- `turn/start` → submit a user prompt.
- `turn/continue` (if present) → follow-ups (use schema if available).
- `item/commandExecution/requestApproval` → approvals queue.
- `item/fileChange/requestApproval` → approvals queue.
- `applyPatchApproval` (legacy) → approvals queue.
- `execCommandApproval` (legacy) → approvals queue.
- Notifications:
  - `ThreadStartedNotification`
  - `TurnStartedNotification`
  - `ItemStartedNotification`
  - `ItemCompletedNotification`
  - `TurnCompletedNotification`
  - `ErrorNotification`

---

### C) Agent Ops (Processes)
**Purpose:** Control the runtime via framework-shells.

UI blocks:
- Active shells (app-server, agents).
- Start/Stop/Attach controls.
- Resource stats (optional).

Schema mapping:
- UI does not call app-server here; it uses framework-shells REST/CLI.
- Status is separate from app-server threads (but linked by metadata).

---

### D) Settings / Profiles
**Purpose:** Save & reuse settings.

UI blocks:
- Saved project profiles.
- Default sandbox/approval policies.
- Default instructions / system prompt.

Schema mapping:
- `config/read` / `config/write` (if present in schema).
- Otherwise local UI persistence.

---

## 3) Critical UI Flows → Schema

### Flow: New Thread
1. User selects project root, model, instructions.
2. UI sends `thread/start` with:
   - `input` (initial user message or empty)
   - `config` (model, sandbox, mcp servers, etc.)
3. UI subscribes to notifications and renders output.

### Flow: Resume Thread
1. User picks from list (`thread/list`).
2. UI sends `thread/resume` with thread id.
3. UI starts a new turn or continues with `turn/start`.

### Flow: Submit Turn
1. User enters prompt + optional mode (read-only, safe, yolo).
2. UI sends `turn/start` with content and policy overrides.
3. UI listens for `Item*` and `Turn*` notifications.

### Flow: Approvals
1. Server sends `item/commandExecution/requestApproval` or `item/fileChange/requestApproval`.
2. UI surfaces a prompt with details.
3. User approves/denies:
   - Approve → send `CommandExecutionRequestApprovalResponse` or `FileChangeRequestApprovalResponse`.
   - Deny → same response with `decision=denied`.

### Flow: Diffs
1. Server emits a diff in `ItemCompleted` or `TurnCompleted`.
2. UI shows unified diff + file list.
3. Optional “apply patch” (legacy): `applyPatchApproval`.

---

## 4) UI Model (State Store)

Minimal client state:
- `threads`: list with id, title, status, lastActivity.
- `activeThreadId`
- `turns`: timeline for active thread
- `items`: per-turn tool calls and outputs
- `approvals`: pending approvals queue
- `projectProfile`: root, instructions, policy, mcp
- `processStatus`: framework-shells state

---

## 5) Where Framework-Shells Fits

- UI button “Start App Server” → backend endpoint which calls framework-shells to spawn `codex-app-server` in cwd.
- UI button “Stop App Server” → terminate shell.
- UI retains a “transport status” (connected/disconnected) for the app-server stdio bridge.

The app-server is just another managed shell; the UI should treat it like a dependency.

---

## 6) MVP UI Scope (fast to build)

1. One-page dashboard (3-column layout).
2. Thread list + thread view + approvals pane.
3. Live stream area with minimal timeline.
4. Project root/instructions controls.

Defer:
- Multi-agent orchestration
- Complex theme editor
- Large analytics panels

---

## 7) Endpoints the UI Assumes (Backend Later)

These are placeholders to wire later:

- `POST /rpc` → JSON-RPC to app-server (stdio bridge)
- `GET /threads` → cached thread list
- `POST /app-server/start` → start app-server via framework-shells
- `POST /app-server/stop`
- `GET /app-server/status`

---

## 8) Success Criteria

- A user can launch app-server, start a thread, send a turn, see outputs.
- Approvals show clearly and round-trip.
- Diffs appear as readable blocks.

