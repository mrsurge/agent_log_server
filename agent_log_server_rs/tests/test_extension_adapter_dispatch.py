from __future__ import annotations

import asyncio
import io
import json
import unittest
from typing import cast

from agent_log_server_rs.adapter_protocol import AdapterMethod
from agent_log_server_rs.adapters.extension_adapter import ExtensionJsonRpcAdapter
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
            return [
                cast(dict[str, object], decode_json_line(line))
                for line in stdout.getvalue().splitlines()
                if line.strip()
            ]

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


if __name__ == "__main__":
    unittest.main()
