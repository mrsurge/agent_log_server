# Shell Features Roadmap

## Current State (v0.1)

Basic `!command` escape system implemented:
- User types `!ls` in message input
- Backend routes to codex-app-server's `command/exec` RPC
- Output rendered in command-result card styling
- Status dot shows success/error
- Persisted to transcript SSOT for replay

## Phase 1: Enhanced Shell Integration

### 1.1 Streaming Output
- Replace batch `command/exec` with streaming execution
- Use `CommandExecutionOutputDeltaNotification` for live output
- Pin output card to bottom during execution (Warp-style)

### 1.2 Shell Session Management
- Leverage codex-app-server's internal PTY management
- Background shell support (multiple concurrent sessions)
- Session query/list capability

## Phase 2: Warp-like Interface

### 2.1 UI Refinements
- Status ribbon (implemented ✓)
- Toggle between chat mode and shell mode
- Shell input with autocomplete suggestions

### 2.2 Command Blocks
- Collapsible command output blocks
- Command history navigation
- Re-run previous commands

## Phase 3: Advanced Shell Options

### Option A: Python Click Sidecar
```
┌─────────────────────────────────────┐
│  agent_log_server                   │
│  ├── server.py (FastAPI)            │
│  └── shell_cli.py (Click)           │
│       └── subprocess management     │
└─────────────────────────────────────┘
```
- Use Python `click` for CLI argument parsing
- `subprocess.Popen` with PTY for local shell
- Advantages: Full control, no codex dependency
- Disadvantages: Duplicate shell management

### Option B: Framework-Shells Sidecar
```
┌─────────────────────────────────────┐
│  agent_log_server                   │
│  ├── server.py (FastAPI)            │
│  └── framework-shells/              │
│       ├── shell_manager.py          │
│       └── pty_handler.py            │
└─────────────────────────────────────┘
```
- Leverage existing framework-shells library
- Battle-tested PTY management
- Multiplexing built-in
- Advantages: Proven, feature-rich
- Disadvantages: Additional dependency

### Option C: Pure codex-app-server (Current)
```
┌─────────────────────────────────────┐
│  agent_log_server ──RPC──► codex    │
│                            └── PTY  │
└─────────────────────────────────────┘
```
- Use codex-app-server's internal shell system
- `command/exec` for one-shot commands
- Agent-initiated commands stream via existing events
- Advantages: No extra deps, unified with agent
- Disadvantages: Less control, tied to codex lifecycle

## Recommended Path

**Short-term**: Continue with Option C (codex-app-server)
- Already working for basic commands
- Leverage streaming when available

**Medium-term**: Evaluate Option B (framework-shells)
- If codex-app-server shell limitations emerge
- For standalone shell features independent of agent

## Related Files
- `server.py`: `/api/appserver/shell/exec` endpoint
- `static/js/main.js`: `sendShellCommand()`, `renderShellOutput()`
- `new-binary-schema/ts/`: TypeScript bindings for shell RPCs

## References
- Warp terminal: https://www.warp.dev/
- framework-shells: (internal library)
- codex-app-server shell RPCs: See `CommandExecParams`, `CommandExecResponse` in TS bindings
