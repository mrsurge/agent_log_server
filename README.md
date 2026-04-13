# Agent Log Server

A FastAPI server with a `fastcore.xml`-generated UI for AI agent orchestration and conversation management. The primary feature is the **Codex Agent UI** — a web-based interface for interacting with OpenAI's `codex-app-server` CLI.

## Device Litmus (Symlink-Resolved CWD)

- Date: `2026-02-26`
- Logical CWD (`pwd`): `/data/data/com.termux/files/home/mrselect6/worktrees/agent_log_server`
- Physical CWD (`pwd -P` / `readlink -f .`): `/data/data/com.termux/files/home/agent_log_server`

This repo is confirmed to run from a symlinked worktree path on this device. Any new IPC/transport features should treat path identity canonically (resolved path) when deriving socket paths, locks, and process-group keys.

## Codex Agent UI (`/`)

The main feature is a rich conversational interface mounted at `/`. It acts as a bridge and UI layer between the `codex-app-server` binary and a web frontend.

- Canonical app UI: `/`
- Legacy agent log page: `/agent-log`
- Compatibility redirect: `/codex-agent` → `/`

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Web Browser (Frontend)                      │
│  - Dumb renderer (displays what backend tells it)                   │
│  - WebSocket client for real-time updates                           │
│  - REST client for actions (send message, approve, etc.)            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ WebSocket (events) + REST (actions)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│             Python Server (`agent_log_server/server.py`)            │
│  - Translates codex events → frontend-friendly format               │
│  - Manages conversation state (SSOT sidecar)                        │
│  - Stores internal transcript (richer than rollout)                 │
│  - Handles approvals, settings, conversation switching              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ stdin/stdout (JSON-RPC)
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    codex-app-server (Rust binary)                   │
│  - Manages conversations with OpenAI API                            │
│  - Executes tools (shell commands, file edits)                      │
│  - Emits events via stdout (JSON-RPC notifications)                 │
│  - Writes rollout logs to ~/.codex/sessions/                        │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Features

- **Streaming responses** with markdown rendering
- **Approval workflow** for file changes and shell commands
- **Conversation management** with persistence
- **File mentions** via `@` autocomplete (Tribute.js)
- **Direct shell execution** with `!` prefix
- **Model/effort selection** with dynamic dropdowns
- **Context window tracking** with token usage display
- **Rollout loading** — resume conversations from `~/.codex/sessions/` rollout logs
- **PWA support** — installable as a standalone app

### Progressive Web App (PWA)

The Codex Agent UI is a full PWA with:

- **Web App Manifest** (`/manifest.json`) — enables "Add to Home Screen" on mobile and desktop
- **Service Worker** (`/sw.js`) — caches static assets for offline access
- **Standalone display** — runs in its own window without browser chrome when installed
- **Theme color** — dark theme (#0d0f13) for native app feel

To install: visit `/` in Chrome/Edge/Safari and use the browser's "Install" or "Add to Home Screen" option.

### Transcript Entry Types

The server maintains a richer transcript than raw rollout logs:

| Role | Description |
|------|-------------|
| `user` | User messages |
| `assistant` | Agent responses |
| `reasoning` | Agent thinking/reasoning |
| `diff` | File change diffs |
| `command` | Shell command execution with output |
| `approval` | Approval decisions (accepted/declined) |
| `plan` | Agent task plans with step status |
| `shell_input` / `shell_output` | Direct shell commands |
| `token_usage` | Context window statistics |
| `debug_trace` | Internal-only structured debug rows tagged with `internal: true` |

Normal transcript replay/range reads ignore `internal: true` rows so UI history stays clean; inspect the raw `transcript.jsonl` or use `include_internal=true` on the transcript range endpoint when you need them.

For detailed documentation, see [CODEX_APP_SERVER_EXTENSION.md](./CODEX_APP_SERVER_EXTENSION.md) and [AGENT_EXTENSION_INTEGRATION.md](./AGENT_EXTENSION_INTEGRATION.md).

---

## Agent Chat Log API

The server also provides a simple REST + HTML "chat log" for coordinating multiple agents.

- **Port:** 12356
- **Log format:** JSON Lines (one JSON object per line)
- **Write payload:** JSON with `who` and `message` fields
- **Read:** `GET /api/messages` returns stored entries (includes server-added `ts`)

### Curl Examples

Post a message:
```bash
curl -sS -X POST http://127.0.0.1:12356/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"who":"agent-alpha","message":"starting task 3"}'
```

Read all messages:
```bash
curl -sS http://127.0.0.1:12356/api/messages
```

Tail the last N:
```bash
curl -sS "http://127.0.0.1:12356/api/messages?limit=100"
```

---

## Install From Git

```bash
pip install "git+<repo-url>"
codex-agent --log agent_chat.log.jsonl --port 12356
```

## Run From Repo

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python -m agent_log_server --log agent_chat.log.jsonl --port 12356
```

Open the Codex Agent UI: `http://127.0.0.1:12356/`

## Framework Shells Integration

The server uses **Framework Shells** for process orchestration. You can also launch via the FWS CLI:

```bash
python -m framework_shells.cli.main up agent_log_server/shellspec/agent_log.yaml
```

## Roadmap (WIP)

- **IDE Chat Extension** — run Codex Agent UI as an embedded chat panel in your IDE
- **Sideband Terminal** — use the IDE terminal alongside Codex Agent for hybrid workflows

Stay tuned for updates.

## License

GPL-3.0 (see `LICENSE`).
