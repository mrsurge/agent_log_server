from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

from extensions.codex_ext.transport import CodexAppServerTransport, MetaFns, ShellManager


def _discard_raw_log(_direction: str, _label: str, _payload: object) -> None:
    return None


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


class _BindingTransport(CodexAppServerTransport):
    bound_conversation_id: str | None = None

    async def _write_payload(self, payload: dict[str, object], *, conversation_id: str | None = None) -> None:
        params = payload.get("params")
        params_dict = cast(dict[str, object], params) if isinstance(params, dict) else {}
        self.bound_conversation_id = self._resolve_conversation_id(
            None,
            params_dict,
        )
        request_id = str(payload["id"])
        self._rpc_waiters[request_id].set_result({"id": payload["id"], "result": {}})

    async def _decode_rpc_response_result(
        self,
        method: str,
        response: dict[str, object],
        *,
        conversation_id: str | None,
    ) -> dict[str, object]:
        del method, response, conversation_id
        return {}

    def remember_response_bindings(self, conversation_id: str, result: dict[str, object]) -> None:
        self._remember_response_bindings(conversation_id=conversation_id, result=result)

    def find_conversation_by_thread_id(self, thread_id: str) -> str | None:
        return self._find_conversation_by_thread_id(thread_id)


class CodexTransportResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_binding_precedes_app_server_write(self) -> None:
        async def fws_getter() -> object:
            raise AssertionError("binding test should not touch framework shells")

        async def no_broadcast(_event: dict[str, object]) -> None:
            return None

        async def no_transcript(_conversation_id: str, _entry: dict[str, object]) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            transport = _BindingTransport(
                server_root=Path(tmp),
                fws_getter=cast(Callable[[], Awaitable[ShellManager]], fws_getter),
                broadcast_fn=no_broadcast,
                transcript_fn=no_transcript,
                meta_fns=None,
                raw_log_fn=_discard_raw_log,
            )
            await transport.rpc_request_unchecked(
                "turn/start",
                params={"threadId": "thread_123"},
                conversation_id="conv_123",
            )

        self.assertEqual(transport.bound_conversation_id, "conv_123")

    async def test_response_turn_binding_persists_through_meta_adapter(self) -> None:
        stored_meta: dict[str, object] = {"thread_id": "thread_123"}

        def load_meta(_conversation_id: str) -> dict[str, object]:
            return dict(stored_meta)

        def save_meta(_conversation_id: str, value: dict[str, object]) -> None:
            stored_meta.clear()
            stored_meta.update(value)

        async def fws_getter() -> object:
            raise AssertionError("response binding test should not touch framework shells")

        async def no_broadcast(_event: dict[str, object]) -> None:
            return None

        async def no_transcript(_conversation_id: str, _entry: dict[str, object]) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            transport = _BindingTransport(
                server_root=Path(tmp),
                fws_getter=cast(Callable[[], Awaitable[ShellManager]], fws_getter),
                broadcast_fn=no_broadcast,
                transcript_fn=no_transcript,
                meta_fns={"load": load_meta, "save": save_meta},
                raw_log_fn=_discard_raw_log,
            )
            transport.remember_response_bindings("conv_123", {"turn": {"id": "turn_155"}})

        self.assertEqual(stored_meta["turn_id"], "turn_155")

    async def test_conversation_fallback_uses_als_data_root(self) -> None:
        async def fws_getter() -> object:
            raise AssertionError("data root test should not touch framework shells")

        async def no_broadcast(_event: dict[str, object]) -> None:
            return None

        async def no_transcript(_conversation_id: str, _entry: dict[str, object]) -> None:
            return None

        with tempfile.TemporaryDirectory() as tmp:
            conversation_dir = Path(tmp) / "conversations" / "conv_123"
            conversation_dir.mkdir(parents=True)
            (conversation_dir / "meta.json").write_text(
                '{"thread_id":"thread_123"}',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ALS_RS_DATA_DIR": tmp}):
                transport = _BindingTransport(
                    server_root=Path(tmp),
                    fws_getter=cast(Callable[[], Awaitable[ShellManager]], fws_getter),
                    broadcast_fn=no_broadcast,
                    transcript_fn=no_transcript,
                    meta_fns=None,
                    raw_log_fn=_discard_raw_log,
                )
                resolved = transport.find_conversation_by_thread_id("thread_123")

        self.assertEqual(resolved, "conv_123")

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
