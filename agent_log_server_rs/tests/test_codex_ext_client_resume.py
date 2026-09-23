from __future__ import annotations

import unittest
from typing import Callable, cast
from unittest.mock import AsyncMock, Mock, patch

from extensions.codex_ext import client


class CodexClientResumeTests(unittest.TestCase):
    def test_existing_thread_send_does_not_clobber_routed_turn_id(self) -> None:
        import asyncio

        stored_meta: dict[str, object] = {
            "thread_id": "thread_123",
            "status": "active",
            "settings": {"model": "old-model"},
        }

        def load_meta(_conversation_id: str) -> dict[str, object]:
            return dict(stored_meta)

        def save_meta(_conversation_id: str, value: dict[str, object]) -> None:
            stored_meta.clear()
            stored_meta.update(value)

        async def rpc_request(method: str, **_kwargs: object) -> dict[str, object]:
            self.assertEqual(method, "turn/start")
            self.assertEqual(stored_meta["settings"], {"model": "new-model"})
            stored_meta["turn_id"] = "turn_155"
            return {"turn": {"id": "turn_155"}}

        ready_threads: list[str] = []

        def mark_thread_ready(thread_id: str) -> None:
            ready_threads.append(thread_id)

        transport = Mock(
            rpc_request=AsyncMock(side_effect=rpc_request),
            mark_thread_ready=mark_thread_ready,
        )
        with patch.object(client, "_meta_fns", {"load": load_meta, "save": save_meta}), \
             patch.object(client, "_ensure_transport_ready", new=AsyncMock(return_value=transport)), \
             patch.object(client, "get_runtime_protocol", new=AsyncMock(return_value=object())), \
             patch.object(client, "build_request_params", return_value={"threadId": "thread_123"}), \
             patch.object(client, "_merge_runtime_settings", return_value={"model": "new-model"}), \
             patch.object(client, "_thread_runtime_signature", return_value="signature"), \
             patch.object(client, "_add_to_raw_buffer"):
            result = asyncio.run(client.handle_message(
                "local-1",
                "hello",
                "codex-ext",
                {"model": "new-model"},
            ))

        self.assertTrue(result["ok"])
        self.assertEqual(stored_meta["turn_id"], "turn_155")
        self.assertEqual(ready_threads, ["thread_123"])

    def test_abort_without_persisted_turn_uses_startup_interrupt(self) -> None:
        import asyncio

        rpc_calls: list[tuple[str, dict[str, object]]] = []
        build_turn_ids: list[str | None] = []

        async def rpc_request(method: str, **kwargs: object) -> dict[str, object]:
            rpc_calls.append((method, kwargs))
            return {}

        def load_meta(_conversation_id: str) -> dict[str, object]:
            return {"thread_id": "thread_123"}

        def build_params(
            _protocol: object,
            _method: str,
            _settings: dict[str, object],
            **kwargs: object,
        ) -> dict[str, object]:
            turn_id = kwargs.get("turn_id")
            build_turn_ids.append(turn_id if isinstance(turn_id, str) else None)
            return {"threadId": "thread_123", "turnId": turn_id or ""}

        transport = Mock(rpc_request=rpc_request)
        with patch.object(client, "_meta_fns", {
            "load": load_meta,
        }), patch.object(
            client,
            "_ensure_transport_ready",
            new=AsyncMock(return_value=transport),
        ), patch.object(
            client,
            "get_runtime_protocol",
            new=AsyncMock(return_value=object()),
        ), patch.object(
            client,
            "build_request_params",
            build_params,
        ), patch.object(client, "_add_to_raw_buffer"):
            result = asyncio.run(client.abort_session("local-1"))

        self.assertTrue(result)
        self.assertEqual(build_turn_ids, [""])
        self.assertEqual(rpc_calls, [(
            "turn/interrupt",
            {
                "params": {"threadId": "thread_123", "turnId": ""},
                "conversation_id": "local-1",
                "timeout": 10.0,
            },
        )])

    def test_compact_resumes_only_on_cold_miss_without_history(self) -> None:
        import asyncio

        async def scenario(cold: bool) -> None:
            rpc = AsyncMock(side_effect=[RuntimeError("thread not found: thread_123"), {}] if cold else [{}])
            transport = Mock(rpc_request=rpc)
            meta: dict[str, object] = {"thread_id": "thread_123", "settings": {"model": "test"}}
            def load_meta(_conversation_id: str) -> dict[str, object]:
                return meta
            resume = AsyncMock()
            with patch.object(client, "_meta_fns", {"load": load_meta}), \
                 patch.object(client, "_ensure_transport_ready", new=AsyncMock(return_value=transport)), \
                 patch.object(client, "get_runtime_protocol", new=AsyncMock(return_value=object())), \
                 patch.object(client, "build_request_params", return_value={"threadId": "thread_123"}), \
                 patch.object(client, "_merge_runtime_settings", return_value={"model": "test"}), \
                 patch.object(client, "_resume_thread_for_rpc_server", new=resume), \
                 patch.object(client, "_save_meta"), patch.object(client, "_add_to_raw_buffer"):
                result = await client.compact_session("local-1")
                self.assertTrue(result["ok"])
                self.assertEqual(rpc.await_count, 2 if cold else 1)
                self.assertTrue(all(call.args[0] == "thread/compact/start" for call in rpc.await_args_list))
                if cold:
                    resume.assert_awaited_once()
                    assert resume.await_args is not None
                    self.assertTrue(resume.await_args.kwargs["exclude_turns"])
                else:
                    resume.assert_not_awaited()

        asyncio.run(scenario(False))
        asyncio.run(scenario(True))

    def test_thread_not_found_error_must_match_bound_thread(self) -> None:
        looks_like_thread_not_loaded = cast(
            Callable[[object, str | None], bool],
            getattr(client, "_looks_like_thread_not_loaded_error"),
        )

        self.assertTrue(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: thread not found: thread_123",
                "thread_123",
            )
        )
        self.assertFalse(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: thread not found: thread_abc",
                "thread_123",
            )
        )
        self.assertFalse(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: conversation not found",
                "thread_123",
            )
        )


if __name__ == "__main__":
    unittest.main()
