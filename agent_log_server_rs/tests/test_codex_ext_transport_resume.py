from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from extensions.codex_ext.transport import CodexAppServerTransport, MetaFns, ShellManager


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


class _BlockingRouteTransport(CodexAppServerTransport):
    def __init__(
        self,
        *,
        server_root: Path,
        fws_getter: Callable[[], Awaitable[ShellManager]],
        broadcast_fn: Callable[[dict[str, object]], Awaitable[None]],
        transcript_fn: Callable[[str, dict[str, object]], Awaitable[None]],
        meta_fns: MetaFns | None,
        raw_log_fn: Callable[[str, str, object], None],
    ) -> None:
        super().__init__(
            server_root=server_root,
            fws_getter=fws_getter,
            broadcast_fn=broadcast_fn,
            transcript_fn=transcript_fn,
            meta_fns=meta_fns,
            raw_log_fn=raw_log_fn,
        )
        self.route_started = asyncio.Event()
        self.release_route = asyncio.Event()

    async def _route_transport_event(
        self,
        label: str,
        payload: object,
        *,
        conversation_id: str | None,
        thread_id: str | None,
        turn_id: str | None,
        request_id: str | None,
    ) -> None:
        del label, payload, conversation_id, thread_id, turn_id, request_id
        self.route_started.set()
        await self.release_route.wait()

    async def process_line(self, raw_line: bytes, pending_label: str | None) -> str | None:
        return await self._process_incoming_line(raw_line, pending_label)

    def set_response_waiter(
        self,
        request_id: str,
        response_future: asyncio.Future[dict[str, object]],
    ) -> None:
        self._rpc_waiters[request_id] = response_future

    async def stop_event_router(self) -> None:
        await self._terminate_event_router()


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

    async def test_rpc_response_completes_while_previous_event_route_is_blocked(self) -> None:
        logs: list[tuple[str, str, object]] = []

        async def fws_getter() -> object:
            raise AssertionError("response dispatch test should not touch framework shells")

        async def broadcast(_event: dict[str, object]) -> None:
            return None

        async def transcript(_conversation_id: str, _entry: dict[str, object]) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            typed_fws_getter = cast(Callable[[], Awaitable[ShellManager]], fws_getter)
            transport = _BlockingRouteTransport(
                server_root=Path(tmp),
                fws_getter=typed_fws_getter,
                broadcast_fn=broadcast,
                transcript_fn=transcript,
                meta_fns=None,
                raw_log_fn=lambda direction, label, payload: logs.append((direction, label, payload)),
            )
            try:
                pending_label = await transport.process_line(
                    b'{"method":"thread/status/changed","params":{"threadId":"thread_123","status":{"type":"busy"}}}',
                    None,
                )
                await asyncio.wait_for(transport.route_started.wait(), timeout=1)

                response_future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
                transport.set_response_waiter("42", response_future)
                pending_label = await transport.process_line(
                    b'{"id":42,"result":{"data":[]}}',
                    pending_label,
                )
                response = await asyncio.wait_for(response_future, timeout=0.2)
            finally:
                transport.release_route.set()
                await transport.stop_event_router()

        self.assertIsNone(pending_label)
        self.assertEqual(response["id"], 42)
        self.assertTrue(any(label == "__codex_transport__" for _, label, _ in logs))


if __name__ == "__main__":
    unittest.main()
