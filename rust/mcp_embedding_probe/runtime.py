"""Synthetic embedding proof only: no ALS imports, credentials or mutations."""
import asyncio
import concurrent.futures
import threading
from typing import Literal


class Runtime:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.active = 0
        self.cancelled = 0
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="mcp-proof-python")
        self.thread.start()
        self.ready.wait()

    def _run(self) -> None:
        with asyncio.Runner() as runner:
            self.loop = runner.get_loop()
            self.ready.set()
            self.loop.run_forever()

    async def _call(self, method: Literal["echo", "state", "wait"], value: str | int) -> str:
        if method == "echo":
            if not isinstance(value, str):
                raise ValueError("echo requires string")
            return value
        if method == "state":
            return f"active={self.active};cancelled={self.cancelled}"
        if method != "wait":
            raise ValueError("unknown synthetic tool")
        if not isinstance(value, int):
            raise ValueError("wait requires integer")
        self.active += 1
        try:
            await asyncio.sleep(value / 1000)
            return "completed"
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1

    def submit(self, method: Literal["echo", "state", "wait"], value: str | int) -> concurrent.futures.Future[str]:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(self._call(method, value), self.loop)

    def close(self) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("embedding proof loop failed to stop")
