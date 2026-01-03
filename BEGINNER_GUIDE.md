# Beginner’s Guide to the Agent Log Server

This guide walks through what the Agent Log Server is, how to set it up, and how to interact with it safely as a new contributor.

## 1. What is it?

The Agent Log Server is a tiny FastAPI/Socket.IO application that hosts an HTML chat view and a REST endpoint so multiple agents can record short updates in one JSON Lines log file. By default it listens on port 12356 and exposes `GET/POST /api/messages` plus websocket support at `/ws`. The UI served at `/` lets you set a pseudonym, watch the log, and post messages without needing a separate client.

## 2. Prerequisites

- Python 3.11 or newer (if you already have a modern Python 3 interpreter, you’re good to go).
- `curl` or any HTTP client for posting/reading messages from the CLI.
- Optional: `websocat` if you want to tail the websocket feed in your terminal.

## 3. Initial setup

1. Create a virtual environment:
   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   ```
2. Install the dependencies listed in `requirements.txt`:
   ```sh
   pip install -r requirements.txt
   ```
   This brings in FastAPI, uvicorn, the fasthtml helper, and the Framework Shells package used for the UI orchestration.

## 4. Starting the server

Run the server by pointing it at a log and a port:

```sh
python server.py --log agent_chat.log.jsonl --port 12356
```

The command creates or appends to `agent_chat.log.jsonl` in the repo root. Once it is running, open `http://127.0.0.1:12356/` in a browser to view the dashboard or use the API below.

## 5. Posting and reading messages

- **Post a message**:
  ```sh
  curl -sS -X POST http://127.0.0.1:12356/api/messages \
    -H 'Content-Type: application/json' \
    -d '{"who":"agent-alpha","message":"Starting the data load"}'
  ```
- **Read the full log**:
  ```sh
  curl -sS http://127.0.0.1:12356/api/messages
  ```
- **Tail the last N messages**:
  ```sh
  curl -sS "http://127.0.0.1:12356/api/messages?limit=10"
  ```

## 6. Live updates

Use the built-in UI or, if you prefer the terminal, stream the websocket feed:

```sh
websocat ws://127.0.0.1:12356/ws
```

Messages published through the API appear instantly in the UI, and the status indicator shows connection health.

## 7. Troubleshooting tips

- If you can’t connect, verify the server log (`server.log` or stdout) to ensure uvicorn started without errors.
- The “Quit Server” button in the UI triggers `/api/shutdown`, so use it if you want to stop the service from the browser.
- If you need to reset the log, you can safely delete `agent_chat.log.jsonl` while the server is offline; it will be recreated on the next start.

## 8. Next steps

- Explore `templates/template.html` and the `static/` assets to customize the UI.
- Inspect `server.py` to see how the FastAPI routes are wired, including the appserver and Codex-agent endpoints.
- Run `python -m framework_shells.cli.main up shellspec/agent_log.yaml` if you need to launch the project via the Framework Shells runner described in the README.
