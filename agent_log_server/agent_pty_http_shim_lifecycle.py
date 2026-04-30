from __future__ import annotations

import asyncio
import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence, cast
from urllib.parse import urlsplit, urlunsplit

import httpx


_SHIM_URL_ENV = "AGENT_PTY_HTTP_SHIM_URL"
_SHIM_DEFAULT_URL = "http://127.0.0.1:8765/mcp"
_SHIM_SHELL_NAME = "agent_pty_http_shim"
_SHIM_LABEL = "agent-pty-blocks:http-shim"
_SHIM_DEFAULT_TIMEOUT_SEC = 10.0


class ShellRecordProtocol(Protocol):
    id: str
    status: str
    label: str | None
    spec_id: str | None


class FrameworkShellManagerProtocol(Protocol):
    async def list_shells(self) -> Sequence[ShellRecordProtocol]: ...


class ShellStarterRecordProtocol(Protocol):
    id: str


class OrchestratorProtocol(Protocol):
    async def start_from_ref(
        self,
        ref: str,
        *,
        base_dir: Path,
        ctx: dict[str, str],
        label: str,
        wait_ready: bool,
    ) -> ShellStarterRecordProtocol: ...


class OrchestratorFactoryProtocol(Protocol):
    def __call__(self, mgr: FrameworkShellManagerProtocol) -> OrchestratorProtocol: ...


@dataclass(frozen=True)
class ShimTarget:
    url: str
    health_url: str
    host: str
    port: int
    path: str


async def ensure_agent_pty_http_shim_ready(
    fws_getter: Callable[[], Awaitable[object]],
    *,
    cwd: str,
    timeout_sec: float = _SHIM_DEFAULT_TIMEOUT_SEC,
) -> None:
    target = _shim_target()
    if await _health_ok(target.health_url, timeout_sec=min(timeout_sec, 1.0)):
        return
    if target.path != "/mcp":
        raise RuntimeError(
            "agent-pty-blocks HTTP shim warm-up only supports the /mcp path; "
            f"{_SHIM_URL_ENV} is set to {target.url!r}"
        )
    if target.host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError(
            "agent-pty-blocks HTTP shim warm-up only starts localhost shims; "
            f"{_SHIM_URL_ENV} is set to {target.url!r}"
        )

    mgr = cast(FrameworkShellManagerProtocol, await fws_getter())
    adopted = await _adopt_running_shell(mgr)
    if adopted and await _wait_health(target.health_url, timeout_sec=timeout_sec):
        return

    await _start_shim_shell(mgr, cwd=cwd, target=target)
    if not await _wait_health(target.health_url, timeout_sec=timeout_sec):
        raise TimeoutError(f"agent-pty-blocks HTTP shim did not become healthy at {target.health_url}")


def _shim_target() -> ShimTarget:
    configured = os.environ.get(_SHIM_URL_ENV)
    url = configured.strip() if isinstance(configured, str) and configured.strip() else _SHIM_DEFAULT_URL
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"invalid {_SHIM_URL_ENV or 'agent-pty-blocks shim URL'}: {url!r}")
    if parsed.scheme == "http":
        default_port = 80
    else:
        default_port = 443
    port = parsed.port or default_port
    path = parsed.path or "/mcp"
    health_url = urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
    return ShimTarget(url=url, health_url=health_url, host=parsed.hostname, port=port, path=path)


async def _adopt_running_shell(mgr: FrameworkShellManagerProtocol) -> str | None:
    try:
        records = await mgr.list_shells()
    except Exception:
        return None
    for rec in records:
        if rec.status != "running":
            continue
        if (rec.label or "") != _SHIM_LABEL:
            continue
        if getattr(rec, "spec_id", "") != _SHIM_SHELL_NAME:
            continue
        return rec.id
    return None


async def _start_shim_shell(
    mgr: FrameworkShellManagerProtocol,
    *,
    cwd: str,
    target: ShimTarget,
) -> str:
    spec_path = Path(__file__).parent / "shellspec" / "mcp_agent_pty.yaml"
    orch = _load_orchestrator_factory()(mgr)
    bind_host = "127.0.0.1" if target.host == "localhost" else target.host
    shell = await orch.start_from_ref(
        f"{spec_path}#{_SHIM_SHELL_NAME}",
        base_dir=spec_path.parent,
        ctx={
            "CWD": cwd,
            "SHIM_HOST": bind_host,
            "SHIM_PORT": str(target.port),
        },
        label=_SHIM_LABEL,
        wait_ready=False,
    )
    return shell.id


def _load_orchestrator_factory() -> OrchestratorFactoryProtocol:
    module = importlib.import_module("framework_shells.orchestrator")
    orchestrator = getattr(module, "Orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("framework_shells.orchestrator.Orchestrator unavailable")
    return cast(OrchestratorFactoryProtocol, orchestrator)


async def _wait_health(health_url: str, *, timeout_sec: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while True:
        if await _health_ok(health_url, timeout_sec=min(timeout_sec, 1.0)):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


async def _health_ok(health_url: str, *, timeout_sec: float) -> bool:
    timeout = httpx.Timeout(timeout_sec)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(health_url)
    except httpx.HTTPError:
        return False
    return response.status_code == 200
