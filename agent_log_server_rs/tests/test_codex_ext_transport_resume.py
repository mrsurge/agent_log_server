from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from extensions.codex_ext.transport import CodexAppServerTransport, ShellManager


class _ResumeTransport(CodexAppServerTransport):
    async def _write_payload(self, payload: dict[str, object], *, conversation_id: str | None = None) -> None:
        request_id = str(payload["id"])
        await asyncio.sleep(0)
        self._rpc_waiters[request_id].set_result(
            {
                "id": payload["id"],
                "result": {
                    "thread": {
                        "id": "thread_123",
                    },
                },
            }
        )

    async def _decode_rpc_response_result(
        self,
        method: str,
        response: dict[str, object],
        *,
        conversation_id: str | None,
    ) -> dict[str, object]:
        del method, conversation_id
        result = response.get("result")
        return cast(dict[str, object], result) if isinstance(result, dict) else {}

    async def resume_unchecked(self) -> dict[str, object]:
        return await self._resume_thread_until_idle_unchecked(
            params={"threadId": "thread_123"},
            conversation_id="conv_123",
            thread_id="thread_123",
        )

    def note_idle(self) -> None:
        self._note_resume_idle_event(
            conversation_id="conv_123",
            thread_id="thread_123",
            label="thread/status/changed",
            payload={
                "threadId": "thread_123",
                "status": {
                    "type": "idle",
                },
            },
        )


class CodexTransportResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_waits_for_matching_thread_idle_after_rpc_response(self) -> None:
        logs: list[tuple[str, str, object]] = []

        async def fws_getter() -> object:
            raise AssertionError("resume test should not touch framework shells")

        async def broadcast(_event: dict[str, object]) -> None:
            return None

        async def transcript(_conversation_id: str, _entry: dict[str, object]) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            typed_fws_getter = cast(Callable[[], Awaitable[ShellManager]], fws_getter)
            transport = _ResumeTransport(
                server_root=Path(tmp),
                fws_getter=typed_fws_getter,
                broadcast_fn=broadcast,
                transcript_fn=transcript,
                meta_fns=None,
                raw_log_fn=lambda direction, label, payload: logs.append((direction, label, payload)),
            )
            task = asyncio.create_task(transport.resume_unchecked())

            await asyncio.sleep(0.05)
            self.assertFalse(task.done())

            transport.note_idle()
            result = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(result["thread"], {"id": "thread_123"})
        self.assertTrue(any("resume_idle_wait_release" in str(payload) for _, _, payload in logs))


if __name__ == "__main__":
    unittest.main()
