"""
appserver_transport.py — TE2 Services shim for the Agent Log Server SIO namespace.

Mounts the /appserver Socket.IO namespace onto the TE2 main-process ASGI app
so the codex_agent iframe can communicate over the host's transport instead of
connecting directly to localhost:12359.

Usage in manifest.json:
    "services": {
        "path": "services",
        "modules": ["appserver_transport"]
    }

The loader calls register(app) at startup.
"""

import asyncio
import logging
from typing import Optional

import httpx
import socketio
from socketio.exceptions import ConnectionRefusedError as SocketIOConnectionRefusedError
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from agent_log_server.conversations_rpc_contract import CONVERSATIONS_RPC_NAMESPACE
from agent_log_server.settings_ui_rpc_contract import SETTINGS_RPC_NAMESPACE, UI_RPC_NAMESPACE

logger = logging.getLogger(__name__)

# Target appserver — the standalone server.py instance
APPSERVER_ORIGIN = "http://127.0.0.1:12359"
APPSERVER_SIO_PATH = "/socket.io"  # default SIO path on the target

# --- Option A: Reverse-proxy relay (two SIO servers) ---
#
# This creates a local SIO namespace on the TE2 server that relays every
# client event to the remote appserver and forwards broadcasts back.
# More decoupled — appserver lifecycle is independent.


class _AppserverRelay(socketio.AsyncNamespace):
    """
    SIO namespace that acts as a relay between TE2 clients and the
    standalone appserver's /appserver namespace.

    For each connected TE2 client, we maintain a backend SIO client
    that mirrors the connection to the real appserver.
    """

    def __init__(self):
        super().__init__("/appserver")
        # sid -> backend AsyncClient
        self._backends: dict[str, socketio.AsyncClient] = {}

    async def _get_backend(self, sid: str) -> Optional[socketio.AsyncClient]:
        if sid in self._backends:
            return self._backends[sid]

        client = socketio.AsyncClient(reconnection=True, reconnection_attempts=5)

        # Forward broadcast events from appserver back to this specific TE2 client
        async def _on_event(data):
            try:
                await self.emit("appserver_event", data, room=sid)
            except Exception:
                pass

        client.on("appserver_event", _on_event, namespace="/appserver")

        try:
            await client.connect(
                APPSERVER_ORIGIN,
                namespaces=["/appserver"],
                socketio_path=APPSERVER_SIO_PATH,
                wait_timeout=10,
            )
            self._backends[sid] = client
            return client
        except Exception as e:
            logger.error("Failed to connect relay backend for sid=%s: %s", sid, e)
            return None

    async def on_connect(self, sid, environ):
        logger.info("TE2 client connected to /appserver relay: %s", sid)
        backend = await self._get_backend(sid)
        if not backend:
            raise SocketIOConnectionRefusedError("Backend unavailable")

    async def on_disconnect(self, sid):
        logger.info("TE2 client disconnected from /appserver relay: %s", sid)
        backend = self._backends.pop(sid, None)
        if backend:
            try:
                await backend.disconnect()
            except Exception:
                pass

    # --- Generic catch-all relay ---
    # SIO doesn't have a true wildcard, so we register the known events.

    async def _relay(self, event: str, sid: str, data):
        """Relay a client event to the backend and return the ack."""
        backend = await self._get_backend(sid)
        if not backend or not backend.connected:
            return {"__error": "Backend not connected"}
        try:
            result = await backend.call(event, data, namespace="/appserver", timeout=30)
            return result
        except SocketIOTimeoutError:
            return {"__error": f"Timeout relaying {event}"}
        except Exception as e:
            return {"__error": str(e)}

    # Real-time / Agent Control
    async def on_send_message(self, sid, data):
        return await self._relay("send_message", sid, data)

    async def on_shell_exec(self, sid, data):
        return await self._relay("shell_exec", sid, data)

    async def on_rpc(self, sid, data):
        return await self._relay("rpc", sid, data)

    async def on_interrupt(self, sid, data):
        return await self._relay("interrupt", sid, data)

    async def on_approval_response(self, sid, data):
        return await self._relay("approval_response", sid, data)

    async def on_approval_record(self, sid, data):
        return await self._relay("approval_record", sid, data)

    async def on_compact(self, sid, data):
        return await self._relay("compact", sid, data)

    # Conversation CRUD
    async def on_conversation_create(self, sid, data):
        return await self._relay("conversation_create", sid, data)

    async def on_conversation_get(self, sid, data):
        return await self._relay("conversation_get", sid, data)

    async def on_conversation_meta(self, sid, data):
        return await self._relay("conversation_meta", sid, data)

    async def on_conversations_list(self, sid, data):
        return await self._relay("conversations_list", sid, data)

    async def on_conversation_select(self, sid, data):
        return await self._relay("conversation_select", sid, data)

    async def on_conversation_delete(self, sid, data):
        return await self._relay("conversation_delete", sid, data)

    async def on_conversation_update(self, sid, data):
        return await self._relay("conversation_update", sid, data)

    async def on_conversation_draft(self, sid, data):
        return await self._relay("conversation_draft", sid, data)

    async def on_conversation_bind_rollout(self, sid, data):
        return await self._relay("conversation_bind_rollout", sid, data)

    # Data / Settings
    async def on_set_view(self, sid, data):
        return await self._relay("set_view", sid, data)

    async def on_get_models(self, sid, data):
        return await self._relay("get_models", sid, data)

    async def on_get_extensions(self, sid, data):
        return await self._relay("get_extensions", sid, data)

    async def on_get_extension_models(self, sid, data):
        return await self._relay("get_extension_models", sid, data)

    async def on_get_extension_settings_schema(self, sid, data):
        return await self._relay("get_extension_settings_schema", sid, data)

    async def on_get_sessions(self, sid, data):
        return await self._relay("get_sessions", sid, data)

    async def on_session_resume(self, sid, data):
        return await self._relay("session_resume", sid, data)

    async def on_get_rollouts(self, sid, data):
        return await self._relay("get_rollouts", sid, data)

    async def on_get_rollout_preview(self, sid, data):
        return await self._relay("get_rollout_preview", sid, data)

    async def on_get_status(self, sid, data):
        return await self._relay("get_status", sid, data)

    async def on_get_transcript(self, sid, data):
        return await self._relay("get_transcript", sid, data)

    async def on_get_transcript_range(self, sid, data):
        return await self._relay("get_transcript_range", sid, data)

    # App Lifecycle
    async def on_app_start(self, sid, data):
        return await self._relay("app_start", sid, data)

    async def on_app_stop(self, sid, data):
        return await self._relay("app_stop", sid, data)


class _RpcNamespaceRelay(socketio.AsyncNamespace):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self._namespace = namespace
        self._backends: dict[str, socketio.AsyncClient] = {}

    async def _get_backend(self, sid: str) -> Optional[socketio.AsyncClient]:
        if sid in self._backends:
            return self._backends[sid]

        client = socketio.AsyncClient(reconnection=True, reconnection_attempts=5)

        async def _on_rpc_notify(data):
            try:
                await self.emit("rpc.notify", data, room=sid)
            except Exception:
                pass

        client.on("rpc.notify", _on_rpc_notify, namespace=self._namespace)

        try:
            await client.connect(
                APPSERVER_ORIGIN,
                namespaces=[self._namespace],
                socketio_path=APPSERVER_SIO_PATH,
                wait_timeout=10,
            )
            self._backends[sid] = client
            return client
        except Exception as e:
            logger.error("Failed to connect %s relay for sid=%s: %s", self._namespace, sid, e)
            return None

    async def on_connect(self, sid, environ):
        logger.info("TE2 client connected to %s relay: %s", self._namespace, sid)
        backend = await self._get_backend(sid)
        if not backend:
            raise SocketIOConnectionRefusedError("Backend unavailable")

    async def on_disconnect(self, sid):
        logger.info("TE2 client disconnected from %s relay: %s", self._namespace, sid)
        backend = self._backends.pop(sid, None)
        if backend:
            try:
                await backend.disconnect()
            except Exception:
                pass

    async def on_rpc(self, sid, data):
        backend = await self._get_backend(sid)
        if not backend or not backend.connected:
            return {"__error": "Backend not connected"}
        try:
            return await backend.call("rpc", data, namespace=self._namespace, timeout=30)
        except SocketIOTimeoutError:
            return {"__error": "Timeout relaying rpc"}
        except Exception as e:
            return {"__error": str(e)}


# --- HTTP reverse proxy for iframe content ---

_http_client: Optional[httpx.AsyncClient] = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(base_url=APPSERVER_ORIGIN, timeout=30.0)
    return _http_client


async def _proxy_handler(request):
    """
    Reverse-proxy HTTP requests to the appserver.
    Mounted at /codex-agent-proxy/ — proxies to APPSERVER_ORIGIN.
    """
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    client = await _get_http_client()
    # Build target URL: strip the proxy prefix
    path = request.url.path
    prefix = "/codex-agent-proxy"
    if path.startswith(prefix):
        path = path[len(prefix):] or "/"
    query = str(request.url.query) if request.url.query else ""
    target_url = path + ("?" + query if query else "")

    # Forward the request
    body = await request.body()
    headers = dict(request.headers)
    # Remove hop-by-hop headers
    for h in ("host", "connection", "transfer-encoding"):
        headers.pop(h, None)

    resp = await client.request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body,
    )

    # Stream response back
    response_headers = dict(resp.headers)
    for h in ("content-encoding", "content-length", "transfer-encoding"):
        response_headers.pop(h, None)

    return StreamingResponse(
        content=iter([resp.content]),
        status_code=resp.status_code,
        headers=response_headers,
    )


def register(app) -> None:
    """
    TE2 services loader entry point.
    Called by the app framework when this module is listed in manifest.json services.
    """
    # 1. Mount SIO relay namespace
    # The TE2 app framework should have a socketio.AsyncServer on the app.
    # We look for it at app.state.sio or create one if needed.
    sio = getattr(getattr(app, "state", None), "sio", None)
    if sio is None:
        logger.warning(
            "No SIO server found on app.state.sio — "
            "creating standalone SIO server for /appserver relay"
        )
        sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
        sio_app = socketio.ASGIApp(sio, other_asgi_app=app)
        # This is a fallback — ideally the TE2 framework provides the SIO server
        app.mount("/appserver_sio", sio_app)

    relay = _AppserverRelay()
    sio.register_namespace(relay)
    logger.info("Registered /appserver SIO relay namespace")
    for namespace in (CONVERSATIONS_RPC_NAMESPACE, SETTINGS_RPC_NAMESPACE, UI_RPC_NAMESPACE):
        sio.register_namespace(_RpcNamespaceRelay(namespace))
        logger.info("Registered %s SIO relay namespace", namespace)

    # 2. Mount HTTP reverse proxy for iframe content
    try:
        from starlette.routing import Mount, Route
        proxy_routes = [
            Route("/{path:path}", _proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
            Route("/", _proxy_handler, methods=["GET"]),
        ]
        app.mount("/codex-agent-proxy", Mount(path="", routes=proxy_routes))
        logger.info("Mounted HTTP reverse proxy at /codex-agent-proxy/")
    except Exception as e:
        logger.warning("Could not mount HTTP reverse proxy: %s", e)
