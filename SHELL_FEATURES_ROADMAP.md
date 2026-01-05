# Shell Features Roadmap

## Current State (v0.1)

Basic `!command` escape system implemented:
- User types `!ls` in message input
- Backend routes to codex-app-server's `exec/oneOff` RPC
- Output rendered in command-result card styling
- Status dot shows success/error
- Persisted to transcript SSOT for replay

## Phase 1: Dual-Mode Interface

### 1.1 Mode Toggle
- UI button swaps compose area between **Chat Mode** and **Shell Mode**
- **Shell Mode**: hides Send button, Enter executes command directly
- **Chat Mode**: current behavior (Send button, @mentions, markdown)
- Optional keyboard shortcut (e.g., `Ctrl+\``)
- Mode state persisted per conversation in SSOT

### 1.2 Shell Mode Input UX
- contenteditable styled as terminal prompt (monospace, `$` prefix)
- Fish/Warp-style inline autocomplete suggestions (greyed ghost text)
- Tab to accept suggestion
- Up/Down arrow keys to navigate command history
- No auto-detect magic - explicit mode switch only

### 1.3 Agent Access from Shell Mode
- Escape character to invoke agent (e.g., `@` or `/ask`)
- Example: `/ask how do I find large files` sends to agent
- Keeps shell mode active, agent response appears in transcript

## Phase 2: Click-Powered Autocomplete

### 2.1 Backend Suggestion Engine
- Python Click CLI defines command tree structure
- `/api/shell/complete` endpoint returns suggestions for partial input
- Introspects:
  - Shell builtins (`cd`, `export`, `alias`, etc.)
  - PATH executables
  - File/directory paths (relative to cwd)
  - Command history (per conversation)
  - Git commands and branches (context-aware)

### 2.2 Frontend Autocomplete UI
- Fetch suggestions on keystroke (debounced) or Tab press
- Dropdown menu below input showing matches
- Ghost text for top suggestion (inline, greyed)
- Arrow keys to navigate dropdown, Tab/Enter to accept

### 2.3 Click Integration Architecture
```
┌─────────────────────────────────────────────────────────┐
│  Frontend (contenteditable)                             │
│    └── onKeyUp → POST /api/shell/complete {input, cwd}  │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  server.py                                              │
│    └── /api/shell/complete                              │
│          └── calls shell_completer.get_suggestions()    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│  shell_completer.py (Click-based)                       │
│    ├── introspect command tree                          │
│    ├── fuzzy match against input tokens                 │
│    ├── file path completion (respects .gitignore)       │
│    └── return ranked suggestions                        │
└─────────────────────────────────────────────────────────┘
```

## Phase 3: Shell Environment State

### 3.1 SSOT Environment Tracking
- Track shell state in `conversation_meta.json`:
  ```json
  {
    "shell_env": {
      "pwd": "/home/user/project",
      "exports": {"NODE_ENV": "development"},
      "aliases": {"ll": "ls -la"},
      "history": ["git status", "npm test", "..."]
    }
  }
  ```
- Updated after each command execution
- `pwd` changes tracked via `cd` command parsing

### 3.2 Environment Escape Commands
- `\env` or `/env` - list current tracked environment
- `\pwd` - show current working directory
- `\history` - show command history for conversation
- `\clear` - clear transcript (not env)

### 3.3 Environment Injection
- Pass `shell_env.pwd` as `cwd` to each `exec/oneOff` call
- Export variables prepended to command execution
- Enables stateful-feeling shell over stateless RPC

## Phase 4: Streaming Output

### 4.1 Live Output Rendering
- Use `ExecCommandOutputDeltaEvent` for streaming chunks
- Replace batch response with incremental updates
- Pin output block to bottom during execution (Warp-style)

### 4.2 Stream Handling
- `stdout` and `stderr` rendered distinctly (white vs red)
- Chunked output appended in real-time
- Exit code shown on completion
- Interrupt button to cancel long-running commands

### 4.3 codex-app-server Integration
- Commands sent via `exec/oneOff` RPC with `source: "user_shell"`
- Output events routed through existing `_appserver_reader` task
- Rollout captures user shell commands for agent context

## Architecture Decision: codex-app-server as Shell Backend

**Chosen**: Route all shell commands through codex-app-server's PTY system

**Rationale**:
- Commands appear in rollout → agent has full context of user actions
- Unified PTY management (no duplicate process handling)
- `ExecCommandSource: "user_shell"` already supported
- Streaming via `ExecCommandOutputDeltaEvent` already implemented

**Trade-offs accepted**:
- Shell tied to codex-app-server lifecycle
- Less control than raw subprocess
- Users wanting full terminal can use actual terminal

**Click's role**: Autocomplete suggestions only, not command execution

## Related Files
- `server.py`: `/api/appserver/shell/exec` endpoint (to be updated)
- `static/js/main.js`: `sendShellCommand()`, `renderShellOutput()`
- `new-binary-schema/ts/ExecOneOffCommandParams.ts`: RPC params
- `new-binary-schema/ts/ExecCommandOutputDeltaEvent.ts`: Streaming events
- `new-binary-schema/ts/ExecCommandSource.ts`: Includes `"user_shell"`

## References
- Warp terminal: https://www.warp.dev/
- Fig Autocomplete: https://github.com/withfig/autocomplete
- Inshellisense (Microsoft): https://github.com/microsoft/inshellisense
- Click shell completion: https://click.palletsprojects.com/en/stable/shell-completion/
- Click autocomplete blog: https://amjith.com/blog/2025/autocompletion-click-commands/
- codex-app-server TypeScript bindings: `new-binary-schema/ts/`
