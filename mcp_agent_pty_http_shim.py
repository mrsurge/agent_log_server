#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SHIM_PATH = "/mcp"
DEFAULT_CHILD_PATH = "/mcp"
DEFAULT_READY_TIMEOUT_SEC = 10.0

_CONVERSATION_ID_HEADERS = (
    "conversation-id",
    "x-agent-conversation-id",
    "x-conversation-id",
)
_CWD_HEADERS = (
    "x-agent-cwd",
    "cwd",
    "x-cwd",
)
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


@dataclass
class ChildSession:
    conversation_id: str
    cwd: str
    port: int
    process: asyncio.subprocess.Process
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    base_url: str = field(init=False)

    def __post_init__(self) -> None:
        self.base_url = f"http://{DEFAULT_HOST}:{self.port}{DEFAULT_CHILD_PATH}"

    def running(self) -> bool:
        return self.process.returncode is None


class ChildRegistry:
    def __init__(self) -> None:
        self._children: dict[str, ChildSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_start(self, *, conversation_id: str, cwd: str) -> ChildSession:
        async with self._lock:
            existing = self._children.get(conversation_id)
            if existing and existing.running():
                if existing.cwd != cwd:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "conversation already has an agent-pty-blocks MCP child "
                            f"bound to cwd {existing.cwd!r}"
                        ),
                    )
                return existing
            if existing:
                await self._stop_child(existing)
                self._children.pop(conversation_id, None)

            child = await self._start_child(conversation_id=conversation_id, cwd=cwd)
            self._children[conversation_id] = child
            return child

    async def snapshot(self) -> list[dict[str, object]]:
        async with self._lock:
            return [
                {
                    "conversation_id": child.conversation_id,
                    "cwd": child.cwd,
                    "pid": child.process.pid,
                    "port": child.port,
                    "running": child.running(),
                }
                for child in self._children.values()
            ]

    async def stop_all(self) -> None:
        async with self._lock:
            children = list(self._children.values())
            self._children.clear()
        await asyncio.gather(*(self._stop_child(child) for child in children), return_exceptions=True)

    async def _start_child(self, *, conversation_id: str, cwd: str) -> ChildSession:
        port = _allocate_local_port()
        env = os.environ.copy()
        env.update(
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HTTP_HOST": DEFAULT_HOST,
                "MCP_HTTP_PORT": str(port),
                "MCP_STREAMABLE_HTTP_PATH": DEFAULT_CHILD_PATH,
                "CONVERSATION_ID": conversation_id,
                "PWD": cwd,
                "FASTMCP_HOST": DEFAULT_HOST,
                "FASTMCP_PORT": str(port),
                "FASTMCP_STREAMABLE_HTTP_PATH": DEFAULT_CHILD_PATH,
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_mcp_server_script_path()),
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        child = ChildSession(conversation_id=conversation_id, cwd=cwd, port=port, process=process)
        child.stdout_task = asyncio.create_task(
            _drain_prefixed(process.stdout, f"[agent-pty-child {conversation_id[:8]} stdout]"),
            name=f"agent-pty-http-shim-stdout-{conversation_id[:8]}",
        )
        child.stderr_task = asyncio.create_task(
            _drain_prefixed(process.stderr, f"[agent-pty-child {conversation_id[:8]} stderr]"),
            name=f"agent-pty-http-shim-stderr-{conversation_id[:8]}",
        )
        try:
            await _wait_for_tcp(DEFAULT_HOST, port, timeout_sec=DEFAULT_READY_TIMEOUT_SEC)
        except Exception:
            await self._stop_child(child)
            raise
        return child

    async def _stop_child(self, child: ChildSession) -> None:
        if child.running():
            child.process.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(child.process.wait(), timeout=2.0)
        if child.running():
            with suppress(ProcessLookupError):
                child.process.send_signal(signal.SIGKILL)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(child.process.wait(), timeout=2.0)
        for task in (child.stdout_task, child.stderr_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


_registry = ChildRegistry()
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=None)
    try:
        yield
    finally:
        await _registry.stop_all()
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


app = FastAPI(lifespan=_lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    return {"ok": True, "sessions": await _registry.snapshot()}


@app.api_route(DEFAULT_SHIM_PATH, methods=["GET", "POST", "DELETE", "OPTIONS"])
@app.api_route(f"{DEFAULT_SHIM_PATH}/{{tail:path}}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def proxy_mcp(request: Request, tail: str = "") -> StreamingResponse:
    conversation_id = _header_value(request, _CONVERSATION_ID_HEADERS)
    if not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="conversation-id header is required for agent-pty-blocks HTTP shim",
        )
    cwd = _normalize_cwd(_header_value(request, _CWD_HEADERS))
    child = await _registry.get_or_start(conversation_id=conversation_id, cwd=cwd)
    client = _require_http_client()
    target_url = child.base_url if not tail else f"{child.base_url.rstrip('/')}/{tail}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"
    outgoing = client.build_request(
        request.method,
        target_url,
        headers=_forward_request_headers(request),
        content=await request.body(),
    )
    try:
        response = await client.send(outgoing, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"child MCP request failed: {exc}") from exc
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=_forward_response_headers(response),
        background=BackgroundTask(response.aclose),
    )


def _mcp_server_script_path() -> Path:
    return Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "mcp_agent_pty_server.py")))


def _header_value(request: Request, names: tuple[str, ...]) -> str:
    for name in names:
        value = request.headers.get(name)
        if value and value.strip():
            return value.strip()
    return ""


def _normalize_cwd(raw_cwd: str) -> str:
    cwd = raw_cwd.strip() if raw_cwd else os.getcwd()
    if not os.path.isabs(cwd):
        raise HTTPException(status_code=400, detail=f"cwd must be absolute: {cwd!r}")
    if not os.path.isdir(cwd):
        raise HTTPException(status_code=400, detail=f"cwd does not exist: {cwd!r}")
    return os.path.abspath(cwd)


def _require_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        raise HTTPException(status_code=503, detail="HTTP shim client is not initialized")
    return _http_client


def _forward_request_headers(request: Request) -> dict[str, str]:
    return {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


def _forward_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((DEFAULT_HOST, 0))
        address: object = sock.getsockname()  # pyright: ignore[reportAny]
        if not isinstance(address, tuple) or len(address) < 2 or not isinstance(address[1], int):
            raise RuntimeError(f"unexpected socket address from getsockname(): {address!r}")
        return address[1]


async def _wait_for_tcp(host: str, port: int, *, timeout_sec: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    last_exc: BaseException | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_exc = exc
            await asyncio.sleep(0.05)
    if last_exc:
        raise TimeoutError(f"child MCP server did not become ready on {host}:{port}") from last_exc
    raise TimeoutError(f"child MCP server did not become ready on {host}:{port}")


async def _drain_prefixed(
    stream: asyncio.StreamReader | None,
    prefix: str,
) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        print(f"{prefix} {line.decode('utf-8', errors='replace').rstrip()}", file=sys.stderr)


def main() -> None:
    host = os.environ.get("AGENT_PTY_HTTP_SHIM_HOST", DEFAULT_HOST)
    raw_port = os.environ.get("AGENT_PTY_HTTP_SHIM_PORT", str(DEFAULT_PORT))
    port = int(raw_port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
