# xterm Composer Implementation Plan

## Overview

Replace the current clunky per-card xterm mode with a single persistent xterm instance embedded in the composer. When the `>_` button is pressed, the composer transforms into a live terminal surface connected to the conversation's PTY.

## Current State

### Shell Modes (SSOT setting per conversation)
- **xterm mode**: Creates xterm instances in transcript for each command - REMOVING
- **plain text mode**: Same pattern but plain text output - KEEPING (with enhancements)

### New Modes

#### `xterm composer` (NEW - replacing xterm mode)
- `>_` button toggles composer ↔ terminal view
- Terminal shows current PTY state via WebSocket (live attach)
- Direct keyboard input to PTY
- Output summaries still post to transcript as plain text
- Outputs prepended to next message as metadata for agent

#### `plain text composer` (ENHANCED)
- Keeps text composer during command input
- Retains "direct input mode" for interactive commands
- Same metadata behavior as xterm composer

---

## Implementation Details

### 1. HTML Changes (server.py)

Add terminal container inside `.composer`:

```python
Div(
    Div(
        id="agent-prompt",
        contenteditable="true",
        cls="prompt-input",
        **{"data-placeholder": "@ to mention files"},
    ),
    Div(id="composer-terminal", cls="composer-terminal"),  # NEW
    Button("Send", id="agent-send", cls="btn primary"),
    cls="composer"
),
```

### 2. CSS Changes (codex_agent.css)

```css
/* Composer terminal mode */
.composer.terminal-active {
  height: 33vh;
  min-height: 200px;
  max-height: 50vh;
  padding: 0;
  flex-direction: column;
}
.composer.terminal-active .prompt-input,
.composer.terminal-active #agent-send {
  display: none;
}
.composer-terminal {
  display: none;
  width: 100%;
  height: 100%;
  background: #000;
}
.composer.terminal-active .composer-terminal {
  display: block;
}
```

### 3. JavaScript Changes (codex_agent.js)

#### New Variables
```javascript
const composerTerminalEl = document.getElementById('composer-terminal');
let composerTerm = null;           // xterm instance for composer
let composerFitAddon = null;       // FitAddon for auto-sizing
let composerResizeObserver = null; // ResizeObserver for container
```

#### Modified `setTerminalMode()`
```javascript
function setTerminalMode(enabled) {
  terminalMode = Boolean(enabled);
  document.body.classList.toggle('terminal-mode', terminalMode);
  footerEl?.classList.toggle('terminal-active', terminalMode);
  
  if (terminalMode) {
    initComposerTerminal();
  } else {
    promptEl?.focus();
  }
  
  if (footerTerminalToggleEl) {
    footerTerminalToggleEl.classList.toggle('active', terminalMode);
    footerTerminalToggleEl.textContent = terminalMode ? 'chat' : '>_';
  }
}
```

#### New `initComposerTerminal()`
```javascript
async function initComposerTerminal() {
  if (!composerTerminalEl) return;
  
  // Create xterm if not exists
  if (!composerTerm && typeof Terminal !== 'undefined') {
    composerTerm = new Terminal({
      convertEol: true,
      cursorBlink: true,
      scrollback: 5000,
      fontFamily: 'JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 12,
      theme: { background: '#000000', foreground: '#c9d1d9' },
    });
    composerTerm.open(composerTerminalEl);
    
    // Load FitAddon
    if (typeof FitAddon !== 'undefined') {
      composerFitAddon = new FitAddon.FitAddon();
      composerTerm.loadAddon(composerFitAddon);
    }
    
    // ResizeObserver for auto-fit
    if (typeof ResizeObserver !== 'undefined') {
      composerResizeObserver = new ResizeObserver(() => fitComposerTerminal());
      composerResizeObserver.observe(composerTerminalEl);
    }
    
    // Send input to PTY via WebSocket
    composerTerm.onData((data) => {
      if (ptyWebSocket && ptyWebSocket.readyState === WebSocket.OPEN) {
        ptyWebSocket.send(data);
      }
    });
    
    // Sync resize to backend
    composerTerm.onResize(({ cols, rows }) => {
      syncComposerTerminalSize(cols, rows);
    });
  }
  
  // Connect PTY WebSocket if not connected
  connectPtyWebSocket();
  
  // Fit and focus
  requestAnimationFrame(() => {
    fitComposerTerminal();
    composerTerm?.focus();
  });
}
```

#### Modified `handleUserPtyOutput()`
```javascript
function handleUserPtyOutput(chunk) {
  // Route to composer terminal when in terminal mode
  if (terminalMode && composerTerm) {
    composerTerm.write(chunk);
    return;
  }
  // ... existing fallback logic for transcript blocks
}
```

#### New Helper Functions
```javascript
function fitComposerTerminal() {
  if (composerFitAddon) {
    try { composerFitAddon.fit(); } catch (_) {}
  }
}

function syncComposerTerminalSize(cols, rows) {
  const convoId = conversationMeta?.conversation_id;
  if (!convoId) return;
  fetch('/api/mcp/agent-pty/resize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ conversation_id: convoId, cols, rows }),
  }).catch(() => {});
}
```

### 4. Script Dependencies

Ensure FitAddon is loaded in HTML head:
```html
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
```

---

## Sizing Strategy

- **Height**: 33vh (1/3 of viewport height)
  - Minimum: 200px
  - Maximum: 50vh
- **Width**: Automatic via FitAddon (fills container)
- **Resize sync**: `onResize` callback → POST to `/api/mcp/agent-pty/resize`

---

## Integration Points

### Existing Infrastructure (no changes needed)
- `/ws/pty/{conversation_id}` - Bidirectional WebSocket for PTY I/O
- `/api/mcp/agent-pty/resize` - Resize PTY endpoint
- `state.ensure_shell()` - Creates/attaches per-conversation shell
- `handleUserPtyOutput()` - Routes PTY chunks (just needs modification)

### Behavioral Notes
- Terminal persists across toggle (no dispose on hide)
- One terminal per conversation (uses conversation_id as shell namespace)
- Toggle button: `>_` when showing prompt, `chat` when showing terminal

---

## Files to Modify

1. `server.py` - Add `#composer-terminal` div, add FitAddon script
2. `static/codex_agent.css` - Add `.terminal-active` and `.composer-terminal` styles
3. `static/codex_agent.js` - Add composer terminal logic

---

## Future Enhancements (deferred)

- User-shell namespace isolation (`user_shell:<chat_id>:<n>`)
- Multiple terminal sessions per conversation
- Kill terminal button (distinct from ctrl+c)
- User-resizable terminal height
