import asyncio
import contextlib
import queue
import threading
import time
from collections.abc import Awaitable
from typing import Optional, Protocol, TypeAlias, cast


class _PipeProcess(Protocol):
    stdin: object | None
    returncode: int | None


class _PipeState(Protocol):
    process: _PipeProcess


_SubscriptionChunk: TypeAlias = bytes | str | None
_OutputSubscription: TypeAlias = "asyncio.Queue[_SubscriptionChunk]"
_QueuedWrite: TypeAlias = bytes | None


class _PipeShellManager(Protocol):
    def get_pipe_state(self, shell_id: str) -> _PipeState | None: ...

    async def subscribe_output_bytes(self, shell_id: str) -> _OutputSubscription: ...

    async def unsubscribe_output_bytes(
        self,
        shell_id: str,
        subscription: _OutputSubscription,
    ) -> None: ...

    async def write_to_pipe(self, shell_id: str, text: str) -> None: ...

    async def terminate_shell(self, shell_id: str, force: bool) -> None: ...


class _SupportsWriteToShell(Protocol):
    async def write_to_shell(
        self,
        shell_id: str,
        text: str,
        append_newline: bool = True,
    ) -> object: ...


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
        self._owner.raise_if_write_failed()
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        with self._lock:
            self._buffer.extend(payload)
        return len(payload)

    def flush(self) -> None:
        self._owner.raise_if_write_failed()
        with self._lock:
            if not self._buffer:
                return
            payload = bytes(self._buffer)
            self._buffer.clear()
        self._owner.enqueue_write(payload)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True


class FrameworkShellPipeProcess:
    def __init__(self, mgr: _PipeShellManager, shell_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self._mgr = mgr
        self._shell_id = shell_id
        self._loop = loop
        self._write_queue: "asyncio.Queue[_QueuedWrite]" = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task[None]] = None
        self._subscription: _OutputSubscription | None = None
        self._pump_task: Optional[asyncio.Task[None]] = None
        self._closed = False
        self.returncode: Optional[int] = None
        self._write_error: BaseException | None = None
        self.stdin = _AsyncPipeWriter(self, loop)
        self.stdout = _BlockingBytesReader()
        self.stderr = None

    @classmethod
    async def create(
        cls,
        mgr: _PipeShellManager,
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
        self._writer_task = asyncio.create_task(
            self._drain_writes(),
            name=f"copilot-sdk-pipe-writer:{self._shell_id}",
        )
        self._pump_task = asyncio.create_task(
            self._pump_output(),
            name=f"copilot-sdk-pipe:{self._shell_id}",
        )

    def raise_if_write_failed(self) -> None:
        exc = self._write_error
        if exc is not None:
            raise RuntimeError("copilot sdk pipe write failed") from exc

    def _enqueue_write(self, payload: _QueuedWrite) -> None:
        self._write_queue.put_nowait(payload)

    def enqueue_write(self, payload: bytes) -> None:
        self.raise_if_write_failed()
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is self._loop:
                self._enqueue_write(payload)
                return
        self._loop.call_soon_threadsafe(self._enqueue_write, payload)

    async def _drain_writes(self) -> None:
        try:
            while True:
                payload = await self._write_queue.get()
                if payload is None:
                    return
                await self.write_bytes(payload)
        except Exception as exc:
            self._write_error = exc
            self.stdout.close()

    async def write_bytes(self, data: bytes) -> None:
        text = data.decode("utf-8")
        writer = cast(_SupportsWriteToShell | None, self._mgr if hasattr(self._mgr, "write_to_shell") else None)
        if writer is not None:
            try:
                await writer.write_to_shell(self._shell_id, text, append_newline=False)
                return
            except TypeError:
                await writer.write_to_shell(self._shell_id, text)
                return
        await self._mgr.write_to_pipe(self._shell_id, text)

    async def _pump_output(self) -> None:
        try:
            subscription = self._subscription
            if subscription is None:
                return
            while True:
                try:
                    chunk = await asyncio.wait_for(subscription.get(), timeout=1.0)
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

    async def _stop_writer(self) -> None:
        task = self._writer_task
        self._writer_task = None
        if task is None:
            return
        self._enqueue_write(None)
        await task

    async def close_streams(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_error: BaseException | None = None
        try:
            self.stdin.close()
        except Exception as exc:
            close_error = exc
        await self._stop_writer()
        task = self._pump_task
        self._pump_task = None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.stdout.close()
        if close_error is not None:
            raise RuntimeError("copilot sdk pipe close failed") from close_error

    async def aclose(self) -> None:
        await self.close_streams()
