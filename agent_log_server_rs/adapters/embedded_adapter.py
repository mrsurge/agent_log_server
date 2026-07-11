from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import TypeAlias, cast

from agent_log_server_rs.adapter_protocol import JsonMap
from agent_log_server_rs.adapters.extension_adapter import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    ExtensionJsonRpcAdapter,
    close_framework_shell_peer,
)


class EmbeddedExtensionAdapter:
    """Thread-owned Python adapter facade for in-process Rust/PyO3 callers."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._notifications: queue.Queue[JsonMap] = queue.Queue()
        self._adapter: ExtensionJsonRpcAdapter | None = None
        self._thread = threading.Thread(
            target=self._run_loop,
            name="als-rs-embedded-extension-adapter",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("embedded extension adapter event loop did not start")

    def request_json(self, payload: str) -> str:
        if self._closed.is_set():
            return _json_dumps(
                _error_response(None, INTERNAL_ERROR, "embedded extension adapter is closed")
            )
        result_queue: queue.Queue[_RequestResult] = queue.Queue(maxsize=1)

        def schedule() -> None:
            self._loop.create_task(self._request_json_to_queue(payload, result_queue))

        self._loop.call_soon_threadsafe(schedule)
        ok, result = result_queue.get()
        if ok:
            return result
        raise RuntimeError(result)

    def drain_notifications_json(self) -> str:
        notifications: list[JsonMap] = []
        while True:
            try:
                notifications.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return _json_dumps(notifications)

    def shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        result_queue: queue.Queue[_RequestResult] = queue.Queue(maxsize=1)

        def schedule() -> None:
            self._loop.create_task(self._shutdown_to_queue(result_queue))

        self._loop.call_soon_threadsafe(schedule)
        ok, result = result_queue.get(timeout=10)
        if not ok:
            raise RuntimeError(result)
        self._thread.join(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._adapter = ExtensionJsonRpcAdapter(notification_callback=self._capture_notification)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    async def _request_json(self, payload: str) -> str:
        try:
            raw = cast(object, json.loads(payload))
        except json.JSONDecodeError as exc:
            return _json_dumps(_error_response(None, -32700, "Parse error", str(exc)))
        if not isinstance(raw, dict):
            return _json_dumps(_error_response(None, INVALID_REQUEST, "Invalid request"))
        adapter = self._require_adapter()
        response = await adapter.dispatch_jsonrpc(cast(JsonMap, raw))
        if response is None:
            response = {"jsonrpc": JSONRPC_VERSION, "result": None}
        return _json_dumps(response)

    async def _request_json_to_queue(
        self,
        payload: str,
        result_queue: queue.Queue[_RequestResult],
    ) -> None:
        try:
            result_queue.put((True, await self._request_json(payload)))
        except Exception as exc:
            result_queue.put((False, f"{type(exc).__name__}: {exc}"))

    async def _capture_notification(self, method: str, params: object) -> None:
        self._notifications.put(
            {
                "jsonrpc": JSONRPC_VERSION,
                "method": method,
                "params": params,
            }
        )

    async def _shutdown_to_queue(self, result_queue: queue.Queue[_RequestResult]) -> None:
        try:
            await self._shutdown()
            result_queue.put((True, ""))
        except Exception as exc:
            result_queue.put((False, f"{type(exc).__name__}: {exc}"))
        finally:
            self._loop.stop()

    async def _shutdown(self) -> None:
        if self._adapter is not None:
            await self._adapter.stop_supported_handlers()
        await close_framework_shell_peer()

    def _require_adapter(self) -> ExtensionJsonRpcAdapter:
        if self._adapter is None:
            raise RuntimeError("embedded extension adapter is not initialized")
        return self._adapter


_RequestResult: TypeAlias = tuple[bool, str]


def _error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    data: object | None = None,
) -> JsonMap:
    error: JsonMap = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
