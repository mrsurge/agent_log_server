import asyncio
import contextlib
import queue
import threading
import time
from typing import Any, Optional


class _BlockingBytesReader:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._buffer = bytearray()
        self._closed = False

    def feed(self, data: bytes) -> None:
        if self._closed:
            return
        self._queue.put(bytes(data))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)

    def _fill(self) -> bool:
        if self._closed:
            return False
        chunk = self._queue.get()
        if chunk is None:
            self._closed = True
            return False
        self._buffer.extend(chunk)
        return True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            while self._fill():
                pass
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size and self._fill():
            pass
        if not self._buffer and self._closed:
            return b""
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def readline(self, size: int = -1) -> bytes:
        while True:
            newline = self._buffer.find(b"\n")
            if newline != -1:
                end = newline + 1
                if size is not None and size >= 0:
                    end = min(end, size)
                data = bytes(self._buffer[:end])
                del self._buffer[:end]
                return data
            if size is not None and size >= 0 and len(self._buffer) >= size:
                data = bytes(self._buffer[:size])
                del self._buffer[:size]
                return data
            if not self._fill():
                data = bytes(self._buffer)
                self._buffer.clear()
                return data


class _AsyncPipeWriter:
    def __init__(self, owner: "FrameworkShellPipeProcess", loop: asyncio.AbstractEventLoop) -> None:
        self._owner = owner
        self._loop = loop
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False

    def write(self, data: bytes | str) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed pipe writer")
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            self._buffer.extend(payload)
        return len(payload)

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            payload = bytes(self._buffer)
            self._buffer.clear()
        future = asyncio.run_coroutine_threadsafe(self._owner.write_bytes(payload), self._loop)
        future.result()

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True


class FrameworkShellPipeProcess:
    def __init__(self, mgr: Any, shell_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self._mgr = mgr
        self._shell_id = shell_id
        self._loop = loop
        self._subscription: Any = None
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self.returncode: Optional[int] = None
        self.stdin = _AsyncPipeWriter(self, loop)
        self.stdout = _BlockingBytesReader()
        self.stderr = None

    @classmethod
    async def create(
        cls,
        mgr: Any,
        shell_id: str,
        loop: asyncio.AbstractEventLoop,
    ) -> "FrameworkShellPipeProcess":
        process = cls(mgr, shell_id, loop)
        await process._start()
        return process

    async def _start(self) -> None:
        state = self._mgr.get_pipe_state(self._shell_id)
        if not state or not state.process.stdin:
            raise RuntimeError("copilot sdk pipe not available")
        self._subscription = await self._mgr.subscribe_output_bytes(self._shell_id)
        self._pump_task = asyncio.create_task(
            self._pump_output(),
            name=f"copilot-sdk-pipe:{self._shell_id}",
        )

    async def write_bytes(self, data: bytes) -> None:
        text = data.decode("utf-8")
        writer = getattr(self._mgr, "write_to_shell", None)
        if callable(writer):
            try:
                await writer(self._shell_id, text, append_newline=False)
                return
            except TypeError:
                await writer(self._shell_id, text)
                return
        await self._mgr.write_to_pipe(self._shell_id, text)

    async def _pump_output(self) -> None:
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self._subscription.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    state = self._mgr.get_pipe_state(self._shell_id)
                    if not state or state.process.returncode is not None:
                        self.returncode = None if not state else state.process.returncode
                        break
                    continue
                if not chunk:
                    state = self._mgr.get_pipe_state(self._shell_id)
                    if not state or state.process.returncode is not None:
                        self.returncode = None if not state else state.process.returncode
                        break
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self.stdout.feed(bytes(chunk))
        finally:
            self.stdout.close()
            if self._subscription is not None:
                with contextlib.suppress(Exception):
                    await self._mgr.unsubscribe_output_bytes(self._shell_id, self._subscription)
                self._subscription = None

    def poll(self) -> Optional[int]:
        state = self._mgr.get_pipe_state(self._shell_id)
        if not state:
            return self.returncode
        self.returncode = state.process.returncode
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        deadline = None if timeout is None else time.time() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError("framework shell wait timed out")
            time.sleep(0.05)

    def terminate(self) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._mgr.terminate_shell(self._shell_id, force=False),
            self._loop,
        )
        future.result()

    def kill(self) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._mgr.terminate_shell(self._shell_id, force=True),
            self._loop,
        )
        future.result()

    async def close_streams(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stdin.close()
        task = self._pump_task
        self._pump_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.stdout.close()

    async def aclose(self) -> None:
        await self.close_streams()
