# TE2 Services Package — Appserver Transport Relay

This directory contains the Socket.IO transport relay and HTTP reverse proxy
for integrating the Agent Log Server (appserver) into the TE2 app framework.

## Contents

| File | Purpose |
|------|---------|
| `appserver_transport.py` | TE2 service module — SIO relay + HTTP reverse proxy |
| `manifest.example.json` | Example manifest with `services` key added |
| `SIO_ENVELOPE_SCHEMA.md` | Complete Socket.IO envelope schema reference |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  TE2 Host (NiceGUI / Starlette)                     │
│                                                     │
│  ┌─────────────────┐   ┌────────────────────────┐   │
│  │ codex_agent app  │   │ appserver_transport.py │   │
│  │ (iframe wrapper) │   │ (services module)      │   │
│  └────────┬────────┘   └────────┬───────────────┘   │
│           │                     │                    │
│           │  iframe src=        │  /appserver ns     │
│           │  /codex-agent-proxy │  (SIO relay)       │
│           │                     │                    │
│           │  /codex-agent-proxy │  /codex-agent-proxy│
│           │  (HTTP reverse prx) │  (HTTP rev proxy)  │
│           │                     │                    │
└───────────┼─────────────────────┼────────────────────┘
            │                     │
            ▼                     ▼
┌─────────────────────────────────────────────────────┐
│  Agent Log Server (localhost:12359)                  │
│                                                     │
│  ┌─────────────────┐   ┌────────────────────────┐   │
│  │ Starlette app   │   │ socketio /appserver ns  │   │
│  │ (HTTP routes)   │   │ (all SIO handlers)      │   │
│  └─────────────────┘   └────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

## How It Works

### SIO Relay
The `_AppserverRelay` class creates a Socket.IO namespace `/appserver` on the
TE2 host server. For each connected TE2 client, it maintains a backend SIO
client connection to `localhost:12359`'s `/appserver` namespace.

- **Client→Server**: TE2 client emits event → relay forwards to appserver →
  appserver ack → relay returns ack to TE2 client
- **Server→Client**: Appserver broadcasts `appserver_event` → relay's backend
  client receives it → relay emits to the specific TE2 client

### HTTP Reverse Proxy
Mounted at `/codex-agent-proxy/`, this forwards HTTP requests to the appserver.
The iframe `src` should point to `/codex-agent-proxy/codex-agent` instead of
`localhost:12359/codex-agent` when running inside TE2.

## Installation

1. Copy `appserver_transport.py` to your codex_agent app's `services/` directory:
   ```
   cp appserver_transport.py /path/to/mrselect6/app/apps/codex_agent/services/
   ```

2. Update `manifest.json` to include the services key (see `manifest.example.json`):
   ```json
   "services": {
     "path": "services",
     "modules": ["appserver_transport"]
   }
   ```

3. Update `main.js` to use the proxy URL when inside TE2:
   ```javascript
   // Before:
   const TARGET_URL = 'http://localhost:12359/codex-agent';
   // After:
   const TARGET_URL = '/codex-agent-proxy/codex-agent';
   ```

4. Update `connectWS()` in `codex_agent.js` to detect iframe context:
   ```javascript
   // In connectWS(), detect if running inside TE2 iframe
   const sioPath = window.parent !== window
     ? undefined  // use TE2 host's SIO (same origin, default path)
     : undefined; // use appserver's SIO (same origin when standalone)
   _socket = io('/appserver', { transports: ['websocket'] });
   ```
   No change needed when the reverse proxy handles same-origin routing.

## Dependencies

- `python-socketio[asyncio]` (already in appserver requirements)
- `httpx` (for HTTP reverse proxy)

## Future: Direct Mount (Alternative)

Instead of relay, if the appserver runs in the same process as TE2, the SIO
server instance can be shared directly:

```python
# In register(app):
from server import socketio_server
sio = app.state.sio
# Transfer all namespace handlers from socketio_server to sio
```

This eliminates relay latency but couples the lifecycles.
