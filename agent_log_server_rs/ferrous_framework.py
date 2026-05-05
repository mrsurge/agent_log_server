from __future__ import annotations

import asyncio
import os
import queue
import shlex
import threading
import time
from pathlib import Path
from collections.abc import Sequence
from typing import Any


class FerrousFrameworkPipe:
    def __init__(
        self,
        command: Sequence[str],
        cwd: str | None,
        env: dict[str, str],
        label: str,
        spec_id: str,
        subgroups: Sequence[str],
        shellspec_path: str | None = None,
    ) -> None:
        self._command = list(command)
        self._cwd = cwd
        self._env = dict(env)
        self._label = label
        self._spec_id = spec_id
        self._subgroups = list(subgroups)
        self._shellspec_path = shellspec_path
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._ready = threading.Event()
        self._ready_error: BaseException | None = None
        self._closed = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mgr: Any | None = None
        self._subscription: Any | None = None
        self._shell_id = ""
        self._thread = threading.Thread(
            target=self._thread_main,
            name="ferrous-framework-pipe",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise TimeoutError("timed out starting ferrous_framework pipe")
        if self._ready_error is not None:
            raise RuntimeError("failed to start ferrous_framework pipe") from self._ready_error

    def shell_id(self) -> str:
        return self._shell_id

    def write_line(self, line: str) -> None:
        loop = self._require_loop()
        future = asyncio.run_coroutine_threadsafe(self._write_line(line), loop)
        future.result(timeout=10.0)

    def read_line(self, timeout: float | None = None) -> str | None:
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._terminate(), loop)
        try:
            future.result(timeout=5.0)
        except Exception as exc:
            raise RuntimeError("failed to terminate ferrous_framework pipe") from exc

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run())
        except BaseException as exc:
            self._ready_error = exc
            self._ready.set()
            self._lines.put(None)
        finally:
            self._lines.put(None)
            loop.close()

    async def _run(self) -> None:
        import framework_shells

        os.environ.update(
            {
                key: value
                for key, value in self._env.items()
                if key.startswith("FRAMEWORK_SHELLS_")
            }
        )
        self._mgr = await framework_shells.get_manager()
        command, cwd, env, spec_id, subgroups, pipe_config = self._render_shellspec()
        record = await self._mgr.spawn_shell_pipe(
            command,
            cwd=cwd,
            env=env,
            label=self._label,
            spec_id=spec_id,
            subgroups=subgroups,
            pipe_config=pipe_config,
            autostart=True,
        )
        self._shell_id = str(record.id)
        await self._wait_for_pipe_state()
        self._subscription = await self._mgr.subscribe_output_bytes(self._shell_id)
        self._ready.set()
        await self._pump_output()

    async def _wait_for_pipe_state(self) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            state = self._mgr.get_pipe_state(self._shell_id)
            process = getattr(state, "process", None) if state is not None else None
            if getattr(process, "stdin", None) is not None:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"native pipe stdin never became ready for {self._shell_id}")
            await asyncio.sleep(0.05)

    def _render_shellspec(
        self,
    ) -> tuple[list[str], str | None, dict[str, str], str, list[str], dict[str, object]]:
        if not self._shellspec_path:
            return (
                self._command,
                self._cwd,
                self._env,
                self._spec_id,
                self._subgroups,
                {"mode": "native_pipe_testing"},
            )
        from framework_shells.shellspec import load_shellspec, render_shellspec

        specs = load_shellspec(Path(self._shellspec_path))
        spec = specs.get("extension_adapter") or next(iter(specs.values()))
        rendered = render_shellspec(
            spec,
            ctx={
                "PYTHON": self._command[0],
                "CWD": self._cwd or os.getcwd(),
            },
            env=self._env,
        )
        command = rendered.normalized_command()
        env = {**self._env, **rendered.env}
        return (
            command,
            rendered.cwd or self._cwd,
            env,
            rendered.id or self._spec_id,
            rendered.subgroups or self._subgroups,
            rendered.pipe or {"mode": "native_pipe_testing"},
        )

    async def _pump_output(self) -> None:
        buffer = bytearray()
        try:
            while not self._closed.is_set():
                chunk = await self._subscription.get()
                if not chunk:
                    if self._process_exited():
                        return
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                buffer.extend(bytes(chunk))
                while True:
                    newline = buffer.find(b"\n")
                    if newline == -1:
                        break
                    raw = bytes(buffer[:newline])
                    del buffer[: newline + 1]
                    self._lines.put(raw.decode("utf-8", errors="replace"))
        finally:
            if buffer:
                self._lines.put(bytes(buffer).decode("utf-8", errors="replace"))
            self._lines.put(None)
            await self._unsubscribe()

    def _process_exited(self) -> bool:
        state = self._mgr.get_pipe_state(self._shell_id)
        process = getattr(state, "process", None) if state is not None else None
        return process is None or getattr(process, "returncode", None) is not None

    async def _write_line(self, line: str) -> None:
        if hasattr(self._mgr, "write_to_shell"):
            await self._mgr.write_to_shell(self._shell_id, line, append_newline=True)
            return
        await self._mgr.write_to_pipe(self._shell_id, f"{line}\n")

    async def _terminate(self) -> None:
        if self._mgr is not None and self._shell_id:
            await self._mgr.terminate_shell(self._shell_id, force=True)
        await self._unsubscribe()

    async def _unsubscribe(self) -> None:
        if self._mgr is None or self._subscription is None:
            return
        subscription = self._subscription
        self._subscription = None
        await self._mgr.unsubscribe_output_bytes(self._shell_id, subscription)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("ferrous_framework pipe loop is not running")
        return self._loop
