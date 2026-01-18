# Session Setup

> Creating and loading sessions

Sessions represent a specific conversation or thread between the Client and Agent. Each session maintains its own context, conversation history, and state.

Before creating a session, Clients **MUST** first complete the initialization phase.

## Creating a Session

Clients create a new session by calling `session/new`:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "session/new",
  "params": {
    "cwd": "/home/user/project",
    "mcpServers": []
  }
}
```

The Agent **MUST** respond with a unique Session ID:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "sessionId": "sess_abc123def456"
  }
}
```

## Session ID

The session ID returned by `session/new` is a unique identifier for the conversation context.

Clients use this ID to:
- Send prompt requests via `session/prompt`
- Cancel ongoing operations via `session/cancel`
- Load previous sessions via `session/load` (if supported)

## Working Directory

The `cwd` parameter establishes the file system context for the session:
- **MUST** be an absolute path
- **SHOULD** serve as a boundary for tool operations
