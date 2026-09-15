from __future__ import annotations

import unittest
from typing import Callable, cast
from unittest.mock import AsyncMock, Mock, patch

from extensions.codex_ext import client


class CodexClientResumeTests(unittest.TestCase):
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
