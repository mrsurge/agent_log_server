from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Protocol, cast

import socketio
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .typing_helpers import AsyncObjectCallable, ObjectMap, coerce_object_map


def _extract_line_from_diff(diff_text: str) -> int:
    match = re.search(r"^@@\s*[+-]\d+(?:,\d+)?\s+\+(\d+)", diff_text, re.MULTILINE)
    return int(match.group(1)) if match else 1


def _safe_getattr(value: object, name: str) -> object | None:
    try:
        return cast(object, object.__getattribute__(value, name))
    except AttributeError:
        return None


def _coerce_headers(value: object) -> dict[str, str]:
    items_obj = _safe_getattr(value, "items")
    if not callable(items_obj):
        return {}
    raw_items = cast(object, items_obj())
    if not isinstance(raw_items, Iterable):
        return {}
    headers: dict[str, str] = {}
    for entry in raw_items:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        key_obj, value_obj = entry
        headers[str(key_obj)] = str(value_obj)
    return headers


def _read_response_body(value: object) -> bytes:
    read_obj = _safe_getattr(value, "read")
    if not callable(read_obj):
        return b""
    body = cast(object, read_obj())
    return body if isinstance(body, bytes) else b""


def _response_status(value: object, *, default: int, attr_name: str) -> int:
    status_obj = _safe_getattr(value, attr_name)
    return status_obj if isinstance(status_obj, int) else default


class _ResponseContextManager(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object: ...


@dataclass
class HostRoutesState:
    show_close: bool = False
    parent_origin: str | None = None
    ide_mode: bool = False
    project_root: str | None = None
    sidebar_sio: socketio.AsyncClient | None = None
    sidebar_sio_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> ObjectMap:
        return {
            "show_close": self.show_close,
            "parent_origin": self.parent_origin,
            "ide_mode": self.ide_mode,
            "project_root": self.project_root,
        }

    def te2_base_url(self) -> str:
        if isinstance(self.parent_origin, str) and self.parent_origin.startswith(("http://", "https://")):
            return self.parent_origin.rstrip("/")
        return "http://127.0.0.1:8089"


@dataclass(frozen=True)
class HostRoutesDeps:
    config_lock: asyncio.Lock
    load_appserver_config: Callable[[], ObjectMap]
    broadcast_appserver_ui: AsyncObjectCallable
    process_mention: AsyncObjectCallable
    load_conversation_meta: Callable[[str], ObjectMap]
    meta_settings: Callable[[ObjectMap], ObjectMap]


class HostRoutes:
    def __init__(self, deps: HostRoutesDeps, state: HostRoutesState) -> None:
        self._deps = deps
        self._state = state

    def te2_base_url(self) -> str:
        return self._state.te2_base_url()

    async def _http_post_json(
        self,
        url: str,
        payload: ObjectMap,
        timeout_s: float = 6.0,
    ) -> tuple[int, bytes, dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        def _do() -> tuple[int, bytes, dict[str, str]]:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                response_handle = cast(_ResponseContextManager, urllib.request.urlopen(req, timeout=timeout_s))
                with response_handle as response:
                    resp_body = _read_response_body(response)
                    resp_headers = _coerce_headers(_safe_getattr(response, "headers"))
                    return _response_status(response, default=200, attr_name="status"), resp_body, resp_headers
            except urllib.error.HTTPError as exc:
                err_body = _read_response_body(exc)
                resp_headers = _coerce_headers(_safe_getattr(exc, "headers"))
                return _response_status(exc, default=502, attr_name="code"), err_body, resp_headers

        return await asyncio.to_thread(_do)

    @staticmethod
    def cors_headers_for_origin(origin: str | None) -> dict[str, str]:
        if not origin:
            return {"Access-Control-Allow-Origin": "*"}
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
        }

    async def set_host_ui_state(
        self,
        *,
        show_close: bool | None = None,
        parent_origin: str | None = None,
        ide_mode: bool | None = None,
        project_root: str | None = None,
    ) -> ObjectMap:
        if show_close is not None:
            self._state.show_close = bool(show_close)
        if parent_origin is not None:
            self._state.parent_origin = parent_origin or None
        if ide_mode is not None:
            self._state.ide_mode = bool(ide_mode)
        if project_root is not None:
            self._state.project_root = project_root or None

        event = {
            "type": "host_ui",
            **self._state.snapshot(),
        }
        await self._deps.broadcast_appserver_ui(event)
        return event

    async def _disconnect_sidebar_client(self) -> None:
        client = self._state.sidebar_sio
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:
            pass
        self._state.sidebar_sio = None

    async def get_sidebar_sio(self) -> socketio.AsyncClient | None:
        async with self._state.sidebar_sio_lock:
            current = self._state.sidebar_sio
            if current and current.connected:
                return current

            if current is not None:
                print("[Sidebar] Cleaning up stale SIO client")
                await self._disconnect_sidebar_client()

            te2_base = self.te2_base_url()
            if not te2_base:
                print("[Sidebar] No TE2 base URL available")
                return None

            sio_path = "/ui_ipc_ws/socket.io/"
            print(f"[Sidebar] Connecting to {te2_base} path={sio_path} ns=/sidebar_ipc")
            try:
                client = socketio.AsyncClient()
                self._state.sidebar_sio = client

                async def _on_sidebar_cwd_set(data: object) -> None:
                    data_map = coerce_object_map(data)
                    cwd = data_map.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        print(f"[Sidebar] CWD push from TE2: {cwd}")
                        await self.set_host_ui_state(project_root=cwd, ide_mode=True)

                async def _on_sidebar_mention(data: object) -> None:
                    data_map = coerce_object_map(data)
                    path = data_map.get("path")
                    if not isinstance(path, str) or not path.strip():
                        print("[Sidebar] mention ignored: missing path")
                        return
                    print(f"[Sidebar] mention from TE2: path={path}")
                    try:
                        await self._deps.process_mention(data_map)
                    except Exception as exc:
                        print(f"[Sidebar] mention processing failed: {exc}")

                client.on("sidebar:cwd_set", handler=_on_sidebar_cwd_set, namespace="/sidebar_ipc")
                client.on("sidebar:mention", handler=_on_sidebar_mention, namespace="/sidebar_ipc")

                await client.connect(
                    f"{te2_base}?app_id=file_editor_cm6&source=appserver",
                    socketio_path=sio_path,
                    namespaces=["/sidebar_ipc"],
                    transports=["websocket"],
                )
                print(f"[Sidebar] Connected OK (connected={client.connected})")

                try:
                    resp_obj = await client.call(
                        "sidebar:cwd_get",
                        {"source": "codex_agent"},
                        namespace="/sidebar_ipc",
                        timeout=5,
                    )
                    resp = coerce_object_map(resp_obj)
                    data = coerce_object_map(resp.get("data"))
                    cwd = data.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        print(f"[Sidebar] Initial CWD from TE2: {cwd}")
                        await self.set_host_ui_state(project_root=cwd, ide_mode=True)
                except Exception as exc:
                    print(f"[Sidebar] CWD query failed (non-fatal): {exc}")

                return client
            except Exception as exc:
                print(f"[Sidebar] Failed to connect to TE2 sidebar_ipc: {exc}")
                self._state.sidebar_sio = None
                return None

    async def sidebar_recheck_status(self) -> ObjectMap:
        sio = await self.get_sidebar_sio()
        return {
            "ok": True,
            "connected": bool(sio and getattr(sio, "connected", False)),
        }

    async def emit_sidebar_agent_edit(self, payload: ObjectMap) -> None:
        try:
            sio = await self.get_sidebar_sio()
            if sio:
                print(f"[Sidebar] Emitting sidebar:agent_edit payload={payload}")
                await sio.emit("sidebar:agent_edit", payload, namespace="/sidebar_ipc")
            else:
                print("[Sidebar] No SIO client for agent_edit")
        except Exception as exc:
            print(f"[Sidebar] Failed to emit agent_edit: {exc}")
            await self._disconnect_sidebar_client()

    async def emit_sidebar_agent_open(self, payload: ObjectMap) -> None:
        try:
            sio = await self.get_sidebar_sio()
            if sio:
                print(f"[Sidebar] Emitting sidebar:agent_open payload={payload}")
                await sio.emit("sidebar:agent_open", payload, namespace="/sidebar_ipc")
            else:
                print("[Sidebar] No SIO client for agent_open")
        except Exception as exc:
            print(f"[Sidebar] Failed to emit agent_open: {exc}")
            await self._disconnect_sidebar_client()

    async def maybe_emit_sidebar_edit(self, event: ObjectMap) -> None:
        if event.get("type") != "diff" or not self._state.ide_mode:
            return
        conversation_id = event.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return
        meta = self._deps.load_conversation_meta(conversation_id)
        settings = self._deps.meta_settings(meta)
        if settings.get("trackEdits") is not True:
            return
        path = event.get("path")
        if not isinstance(path, str) or not path:
            return
        diff_text = event.get("text")
        line = _extract_line_from_diff(diff_text if isinstance(diff_text, str) else "")
        await self.emit_sidebar_agent_edit(
            {
                "path": path,
                "line": line,
                "column": 1,
                "source": "appserver_diff",
                "conversation_id": conversation_id,
            }
        )

    async def api_te2_agent_open(
        self,
        request: Request,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> Response:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        te2_url = f"{self.te2_base_url()}/api/app/file_editor_cm6/agent/open"
        status, body, _ = await self._http_post_json(te2_url, payload, timeout_s=6.0)
        origin = request.headers.get("origin")
        headers = self.cors_headers_for_origin(origin)
        headers["Content-Type"] = "application/json"
        return Response(content=body, status_code=status, headers=headers)

    async def api_te2_agent_open_options(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        headers = {
            **self.cors_headers_for_origin(origin),
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
        return Response(status_code=204, headers=headers)

    async def api_host_ui_get(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            return {
                "ok": True,
                "host_ui": self._state.snapshot(),
                "active_view": cfg.get("active_view", "splash"),
                "conversation_id": cfg.get("conversation_id"),
            }

    async def api_host_ui_set(self, payload: Annotated[ObjectMap, Body(...)]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        show_close = payload.get("show_close")
        parent_origin = payload.get("parent_origin")
        ide_mode = payload.get("ide_mode")
        project_root = payload.get("project_root")
        if show_close is not None and not isinstance(show_close, bool):
            raise HTTPException(status_code=400, detail="Invalid 'show_close'")
        if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
            raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
        if ide_mode is not None and not isinstance(ide_mode, bool):
            raise HTTPException(status_code=400, detail="Invalid 'ide_mode'")
        if project_root is not None and project_root != "" and not isinstance(project_root, str):
            raise HTTPException(status_code=400, detail="Invalid 'project_root'")
        parent_origin_text = parent_origin if isinstance(parent_origin, str) and parent_origin != "" else None
        project_root_text = project_root if isinstance(project_root, str) and project_root != "" else None
        event = await self.set_host_ui_state(
            show_close=bool(show_close) if show_close is not None else None,
            parent_origin=parent_origin_text,
            ide_mode=bool(ide_mode) if ide_mode is not None else None,
            project_root=project_root_text,
        )
        return JSONResponse({"ok": True, **event}, headers={"Access-Control-Allow-Origin": "*"})

    async def api_host_drawer_open(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> JSONResponse:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        parent_origin = payload.get("parent_origin")
        if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
            raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
        project_root = payload.get("project_root")
        if project_root is not None and project_root != "" and not isinstance(project_root, str):
            raise HTTPException(status_code=400, detail="Invalid 'project_root'")
        parent_origin_text = parent_origin if isinstance(parent_origin, str) and parent_origin != "" else None
        project_root_text = project_root if isinstance(project_root, str) and project_root != "" else None
        event = await self.set_host_ui_state(
            show_close=True,
            ide_mode=True,
            parent_origin=parent_origin_text,
            project_root=project_root_text,
        )
        return JSONResponse({"ok": True, **event}, headers={"Access-Control-Allow-Origin": "*"})

    async def api_host_drawer_open_options(self) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    async def api_host_drawer_close(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> JSONResponse:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        parent_origin = payload.get("parent_origin")
        if parent_origin is not None and parent_origin != "" and not isinstance(parent_origin, str):
            raise HTTPException(status_code=400, detail="Invalid 'parent_origin'")
        parent_origin_text = parent_origin if isinstance(parent_origin, str) and parent_origin != "" else None
        event = await self.set_host_ui_state(
            show_close=False,
            ide_mode=False,
            parent_origin=parent_origin_text,
        )
        return JSONResponse({"ok": True, **event}, headers={"Access-Control-Allow-Origin": "*"})

    async def api_host_drawer_close_options(self) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    async def api_host_project_cwd(self, payload: Annotated[ObjectMap, Body(...)]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            data = payload.get("data")
            if isinstance(data, dict):
                cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise HTTPException(status_code=400, detail="Missing or invalid 'cwd'")
        event = await self.set_host_ui_state(project_root=cwd)
        return JSONResponse({"ok": True, **event}, headers={"Access-Control-Allow-Origin": "*"})

    async def api_host_project_cwd_options(self) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    async def api_host_resolve_iframe(self, request: Request) -> JSONResponse:
        scheme = request.url.scheme or "http"
        host = request.headers.get("host") or request.url.netloc
        url = f"{scheme}://{host}/"
        origin = request.headers.get("origin")
        return JSONResponse(
            {"ok": True, "url": url, "data": {"url": url}},
            headers=self.cors_headers_for_origin(origin),
        )

    async def api_host_resolve_iframe_options(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        headers = {
            **self.cors_headers_for_origin(origin),
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "86400",
        }
        return Response(status_code=204, headers=headers)

    async def api_host_ui_options(self) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    def register_routes(self, app: FastAPI) -> None:
        app.add_api_route("/api/te2/agent/open", self.api_te2_agent_open, methods=["POST"])
        app.add_api_route("/api/te2/agent/open", self.api_te2_agent_open_options, methods=["OPTIONS"])
        app.add_api_route("/api/host/ui", self.api_host_ui_get, methods=["GET"])
        app.add_api_route("/api/host/ui", self.api_host_ui_set, methods=["POST", "PUT"])
        app.add_api_route("/api/host/ui", self.api_host_ui_options, methods=["OPTIONS"])
        app.add_api_route("/api/host/drawer/open", self.api_host_drawer_open, methods=["POST", "PUT"])
        app.add_api_route("/api/host/drawer/open", self.api_host_drawer_open_options, methods=["OPTIONS"])
        app.add_api_route("/api/host/drawer/close", self.api_host_drawer_close, methods=["POST", "PUT"])
        app.add_api_route("/api/host/drawer/close", self.api_host_drawer_close_options, methods=["OPTIONS"])
        app.add_api_route("/api/host/project/cwd", self.api_host_project_cwd, methods=["POST", "PUT"])
        app.add_api_route("/api/host/project/cwd", self.api_host_project_cwd_options, methods=["OPTIONS"])
        app.add_api_route("/api/host/resolve_iframe", self.api_host_resolve_iframe, methods=["GET"])
        app.add_api_route("/api/host/resolve_iframe", self.api_host_resolve_iframe_options, methods=["OPTIONS"])
