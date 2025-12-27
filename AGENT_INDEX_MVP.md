# Agent Index + Profiles (MVP Spec)

This document captures the proposed on-disk layout, `manifest.json` shape, message JSON extension, and an MVP sprint order for adding an agent index + dtach-backed agent CLIs on top of this repo’s agent chat server + UI.

## Goals

- Add an `agent_index/` concept where each agent has its own directory + `manifest.json`.
- Support multiple per-agent **profiles** (at minimum: `interactive`, `headless`), both backed by **dtach** via Framework Shells.
- Extend the existing message JSON shape (currently `{who,message}`) with optional tag/condition/request metadata, with **server-side filtering/enforcement**.
- Make all of this configurable in the UI frontend, with the manifest as SSOT for per-agent configuration.

## Storage Layout (SSOT in user cache)

Store the SSOT under the user cache root:

- Use `$XDG_CACHE_HOME` if set; otherwise use `~/.cache`.

Recommended parent directory:

```
${AGENT_CACHE_HOME}/agent_messaging/
  agent_index/
    <agent_id>/
      manifest.json
      profiles/
        interactive/
          shellspec.yaml
        headless/
          shellspec.yaml
      assets/              # optional
```

Notes:

- Repo-local `agent_index/` (at project root) can exist as “templates”, but the SSOT lives in cache.
- Each profile points at a shellspec ref (e.g. `profiles/headless/shellspec.yaml#agent_cli`).

## Agent Manifest (`manifest.json`) — MVP schema

File: `${AGENT_CACHE_HOME}/agent_messaging/agent_index/<agent_id>/manifest.json`

```json
{
  "schema_version": 1,

  "agent_id": "agent_alpha",
  "display_name": "Agent Alpha",
  "pseudonym": "agent-alpha",
  "enabled": true,

  "profiles": {
    "interactive": {
      "enabled": true,
      "backend": "dtach",
      "shellspec_ref": "profiles/interactive/shellspec.yaml#agent_cli",
      "mode": "interactive",
      "execution": {
        "attach_hint": "Attach via framework_shells attach (or dtach attach).",
        "startup_timeout_ms": 15000
      },
      "env": {
        "AGENT_MODE": "interactive",
        "AGENT_LOG_URL": "http://127.0.0.1:12356",
        "AGENT_PSEUDONYM": "agent-alpha"
      }
    },
    "headless": {
      "enabled": true,
      "backend": "dtach",
      "shellspec_ref": "profiles/headless/shellspec.yaml#agent_cli",
      "mode": "headless",
      "execution": {
        "startup_timeout_ms": 15000,
        "input": {
          "send_method": "dtach",
          "line_ending": "\n"
        }
      },
      "env": {
        "AGENT_MODE": "headless",
        "AGENT_LOG_URL": "http://127.0.0.1:12356",
        "AGENT_PSEUDONYM": "agent-alpha"
      }
    }
  },

  "tags": [
    {
      "tag": "@agent-alpha",
      "pseudonym": "agent-alpha",
      "default_condition": "acknowledge",
      "allowed_conditions": ["reply", "act", "acknowledge", "standby"]
    }
  ],

  "permissions": {
    "allow_outbound_tags": true,
    "allow_outbound_conditions": ["reply", "acknowledge"],
    "allow_outbound_requests": false,
    "max_message_chars": 8000
  },

  "ui": {
    "color": "#1d467e",
    "groups": ["default"]
  }
}
```

### Semantics (MVP)

- `agent_id` is filesystem-safe and stable.
- `pseudonym` is the default `who` value when the agent posts to the log.
- `profiles.*.shellspec_ref` is the SSOT linkage to the runnable agent CLI.
- `permissions` are enforced server-side by filtering/normalizing inbound messages.
- `tags` define the “hook” surface and allowed condition set.

## Message JSON extension (server-enforced)

Current minimum message shape:

```json
{ "who": "agent-alpha", "message": "hello" }
```

Extended message shape (MVP):

```json
{
  "who": "agent-alpha",
  "message": "…",
  "tags": ["@agent-beta"],
  "condition": "reply",
  "request": {
    "to": "agent_beta",
    "profile": "headless",
    "kind": "prompt",
    "payload": { "text": "do X" }
  }
}
```

Enforcement/normalization (MVP):

- Validate `who`/`message` as today.
- Optional keys (`tags`, `condition`, `request`) are accepted but **filtered** using the sender’s `permissions` and the receiver’s tag rules.
- Store the **normalized** record into JSONL and broadcast it (WebSocket/UI).

## Agent Log Usage (the non-negotiable interface)

On initial interaction, the agent must be informed (best-effort) that this is the chat server contract, and for headless mode it is the only supported “outside world” channel:

**Agent Log CLI Usage**

The server is running on `http://127.0.0.1:12356`. You can interact with it using `curl`.

**Post a Message**
To send a message, use a `POST` request with a JSON body containing `who` (your pseudonym) and `message`.

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"who": "your-name", "message": "your message here"}' \
     http://127.0.0.1:12356/api/messages
```

**Read Messages**
To fetch the log of messages:

```bash
# Get all messages
curl http://127.0.0.1:12356/api/messages

# Get only the last n messages
curl "http://127.0.0.1:12356/api/messages?limit=n"
```

## MVP Sprint Order

### Sprint 1 — Agent index + manifest loader

- Define cache parent dir and agent index directory.
- Implement manifest load + validation (schema versioning, required keys, profile refs exist).

### Sprint 2 — Orchestration primitives (Framework Shells + dtach)

- Start/stop/status an agent profile via `shellspec_ref` (dtach backend).
- Persist mapping `{agent_id, profile} -> shell_id` in cache.

### Sprint 3 — Minimal agent control API

- `GET /api/agents` list + status.
- `POST /api/agents/{agent_id}/start?profile=…`
- `POST /api/agents/{agent_id}/stop?profile=…`
- `POST /api/agents/{agent_id}/attach?profile=…` (returns attach instructions/command)

### Sprint 4 — UI MVP (“Agent Dashboard” panel)

- List agents + current status.
- Enable toggle, profile selector, Start/Stop, Attach button.

### Sprint 5 — Message JSON extension + permission filtering

- Extend `POST /api/messages` to accept `tags/condition/request`.
- Enforce manifest permissions/tag rules; store normalized record in JSONL.

### Sprint 6 — Headless input (best-effort)

- `POST /api/agents/{agent_id}/send_input` to write to the dtach session (no reliance on terminal UI logs).
- UI textbox to send “input”/commands to headless profile.

