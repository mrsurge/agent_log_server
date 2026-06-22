from __future__ import annotations

import asyncio
import io
import json
import os
import unittest
from collections.abc import Iterable
from typing import cast
from unittest.mock import patch

from agent_log_server_rs.adapter_protocol import AdapterMethod
from agent_log_server_rs.adapters.extension_adapter import (
    ExtensionJsonRpcAdapter,
    write_all_fd,
)
from agent_log_server_rs.codec import decode_json_line


class _ConcurrentAdapter(ExtensionJsonRpcAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()
        self.fast_seen = asyncio.Event()
        self.shutdown_seen = asyncio.Event()

    async def _dispatch(self, message: dict[str, object]) -> object:
        method = message.get("method")
        if method == "test.slow":
            self.slow_started.set()
            await self.release_slow.wait()
            return {"name": "slow"}
        if method == "test.fast":
            self.fast_seen.set()
            return {"name": "fast"}
        if method == AdapterMethod.EXTENSION_SHUTDOWN:
            self.shutdown_seen.set()
            self._shutdown_requested = True
            return {"ok": True}
        return await super()._dispatch(message)


def _rpc_line(request_id: int, method: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method}) + "\n"


def _decode_responses(output: str) -> list[dict[str, object]]:
    return [
        cast(dict[str, object], decode_json_line(line))
        for line in output.splitlines()
        if line.strip()
    ]


class ExtensionAdapterDispatchTests(unittest.TestCase):
    def test_stdio_dispatches_independent_requests_concurrently(self) -> None:
        async def scenario() -> list[dict[str, object]]:
            adapter = _ConcurrentAdapter()
            stdin = io.StringIO(
                _rpc_line(1, "test.slow")
                + _rpc_line(2, "test.fast")
            )
            stdout = io.StringIO()
            task = asyncio.create_task(adapter.run_stdio(stdin, stdout))
            await asyncio.wait_for(adapter.slow_started.wait(), timeout=1.0)
            await asyncio.wait_for(adapter.fast_seen.wait(), timeout=1.0)
            adapter.release_slow.set()
            await asyncio.wait_for(task, timeout=1.0)
            return _decode_responses(stdout.getvalue())

        responses = asyncio.run(scenario())
        by_id = {response["id"]: response["result"] for response in responses}
        self.assertEqual(by_id[1], {"name": "slow"})
        self.assertEqual(by_id[2], {"name": "fast"})

    def test_stdio_drains_before_ordered_control_methods(self) -> None:
        async def scenario() -> bool:
            adapter = _ConcurrentAdapter()
            stdin = io.StringIO(
                _rpc_line(1, "test.slow")
                + _rpc_line(2, AdapterMethod.EXTENSION_SHUTDOWN)
            )
            stdout = io.StringIO()
            task = asyncio.create_task(adapter.run_stdio(stdin, stdout))
            await asyncio.wait_for(adapter.slow_started.wait(), timeout=1.0)
            await asyncio.sleep(0)
            shutdown_before_release = adapter.shutdown_seen.is_set()
            adapter.release_slow.set()
            await asyncio.wait_for(task, timeout=1.0)
            return shutdown_before_release

        self.assertFalse(asyncio.run(scenario()))

    def test_event_reader_handles_multiple_frames_in_one_chunk(self) -> None:
        responses = asyncio.run(
            self._run_pipe_scenario([
                (_rpc_line(1, "test.fast") + _rpc_line(2, "test.fast")).encode("utf-8")
            ])
        )

        by_id = {response["id"]: response["result"] for response in responses}
        self.assertEqual(by_id[1], {"name": "fast"})
        self.assertEqual(by_id[2], {"name": "fast"})

    def test_event_reader_handles_frame_split_across_chunks(self) -> None:
        line = _rpc_line(1, "test.fast").encode("utf-8")
        responses = asyncio.run(self._run_pipe_scenario([line[:10], line[10:]]))

        self.assertEqual(responses[0]["result"], {"name": "fast"})

    def test_event_reader_handles_eof_after_partial_frame(self) -> None:
        line = _rpc_line(1, "test.fast").rstrip("\n").encode("utf-8")
        responses = asyncio.run(self._run_pipe_scenario([line]))

        self.assertEqual(responses[0]["result"], {"name": "fast"})

    def test_event_reader_ignores_empty_lines(self) -> None:
        responses = asyncio.run(
            self._run_pipe_scenario([b"\n   \n", _rpc_line(1, "test.fast").encode("utf-8")])
        )

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["result"], {"name": "fast"})

    def test_write_all_fd_retries_partial_writes(self) -> None:
        chunks: list[bytes] = []

        def fake_write(_fd: int, data: bytes | bytearray | memoryview) -> int:
            chunk = bytes(data)
            written = min(3, len(chunk))
            chunks.append(chunk[:written])
            return written

        with patch("agent_log_server_rs.adapters.extension_adapter.os.write", fake_write):
            write_all_fd(7, b"abcdefghi")

        self.assertEqual(b"".join(chunks), b"abcdefghi")

    async def _run_pipe_scenario(self, chunks: Iterable[bytes]) -> list[dict[str, object]]:
        adapter = _ConcurrentAdapter()
        stdin_read_fd, stdin_write_fd = os.pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()
        stdin = os.fdopen(stdin_read_fd, "r", encoding="utf-8")
        stdout = os.fdopen(stdout_write_fd, "w", encoding="utf-8")
        try:
            task = asyncio.create_task(adapter.run_stdio(stdin, stdout))
            for chunk in chunks:
                os.write(stdin_write_fd, chunk)
                await asyncio.sleep(0)
            os.close(stdin_write_fd)
            stdin_write_fd = -1
            await asyncio.wait_for(task, timeout=1.0)
            stdout.close()
            stdout_write_fd = -1
            with os.fdopen(stdout_read_fd, "r", encoding="utf-8") as reader:
                stdout_read_fd = -1
                return _decode_responses(reader.read())
        finally:
            stdin.close()
            if stdin_write_fd >= 0:
                os.close(stdin_write_fd)
            if stdout_write_fd >= 0:
                os.close(stdout_write_fd)
            if stdout_read_fd >= 0:
                os.close(stdout_read_fd)


if __name__ == "__main__":
    unittest.main()
