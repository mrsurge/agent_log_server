# Screen Model Golden Test (Gemini --screen-reader)

**Purpose:** Validate that the pyte screen model + MCP screen tools return a clean, rendered view of a TUI (no escape noise, no spinner duplication).

## Setup
- MCP server running with screen model enabled (Sprint 1–3).
- `gemini` installed and available in PATH.
- Uses the new tools: `pty_exec_interactive`, `pty_wait_for`, `pty_send`, `pty_enter`, `pty_read_screen`.

## Test Steps
1. Start an interactive session:
   - `pty_exec_interactive` with command: `gemini --screen-reader`
2. Wait for the prompt:
   - `pty_wait_for` match: `Type your message`
3. Send a short message (no exclamation mark):
   - `pty_send` with a short sentence
   - `pty_enter`
4. Wait for response to complete:
   - `pty_wait_for` match: `Type your message`
5. Read the rendered screen:
   - `pty_read_screen`

## Expected Outcome
- `pty_read_screen.rows` contains clean, user-visible text only.
- No raw escape sequences, no repeated spinner lines.
- The model response appears as stable, readable lines.

## Sample Result (trimmed)
```
User:  Hello Gemini, this is vectorArc testing the new pyte screen model. Can
confirm you received this message.
Responding with gemini-3-flash-preview
Model:  I have received your message, vectorArc.
```

## Notes
- Loading Gemini can take ~60 seconds; allow extra time for the initial prompt to appear.
- This test is the current “golden” reference for validating screen rendering fidelity.
