# Agent Log Server (Flask)

A tiny REST + HTML "chat log" server for coordinating multiple agents in one repo.

- **Port:** 12356 (HTML + API on the same port)
- **Log format:** JSON Lines (one JSON object per line)
- **Write payload:** JSON with exactly two fields: `who`, `message`
- **Read:** `GET /api/messages` returns stored entries (includes server-added `ts`)

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

python server.py --log agent_chat.log.jsonl --port 12356
```

Open: `http://127.0.0.1:12356/`

## Curl (agent-friendly)

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

## Framework Shells (shellspec YAML)

This repo includes a shellspec file you can launch via Framework Shells' CLI:

```bash
python -m framework_shells.cli.main up shellspec/agent_log.yaml
```

The Framework Shells docs describe the shellspec YAML format and the `cli.main up <spec>` pattern.  fileciteturn1file0L11-L20 fileciteturn1file3L28-L40

> Note: if your Framework Shells runtime requires auth, set `FRAMEWORK_SHELLS_SECRET` per your normal workflow.
