"""Private native-MCP tool runtime; one ordinary asyncio loop, no MCP SDK."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Literal
from agent_log_server_rs.mcp_registry import ObjectMap, RequestContext, Registry


class Runtime:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.registry: Registry | None = None
        self.catalog: ObjectMap = {}
        self.error: BaseException | None = None
        self.tasks: set[asyncio.Task[object]] = set()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="als-mcp-tools")
        self.thread.start()
        if not self.ready.wait(timeout=15):
            raise RuntimeError("Python MCP runtime startup timed out")
        if self.error is not None:
            raise RuntimeError("Python MCP runtime startup failed") from self.error

    def _run(self) -> None:
        try:
            from agent_log_server_rs.mcp_tools import mcp
            self.registry = mcp
            self.catalog = mcp.catalog
            with asyncio.Runner() as runner:
                self.loop = runner.get_loop()
                self.ready.set()
                self.loop.run_forever()
        except BaseException as exc:
            self.error = exc
            self.ready.set()

    async def _call(self, kind: Literal["tool", "resource"], name: str, arguments: ObjectMap, request_id: str) -> ObjectMap:
        assert self.registry is not None
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            if kind == "tool":
                return await self.registry.call(name, arguments, RequestContext(request_id))
            return await self.registry.read(name)
        finally:
            self.tasks.discard(task)

    def submit(self, kind: Literal["tool", "resource"], name: str, arguments: ObjectMap, request_id: str, complete: Callable[[concurrent.futures.Future[ObjectMap]], None]) -> concurrent.futures.Future[ObjectMap]:
        if self.loop is None or self.loop.is_closed() or not self.thread.is_alive():
            raise RuntimeError("Python MCP runtime is not available")
        future = asyncio.run_coroutine_threadsafe(self._call(kind, name, arguments, request_id), self.loop)
        future.add_done_callback(complete)
        return future

    def close(self) -> None:
        if self.loop is not None and not self.loop.is_closed():
            # Let tool finally/consent cleanup finish before stopping the loop.
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), self.loop).result(timeout=4)
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Python MCP runtime failed to stop")

    async def _shutdown(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
