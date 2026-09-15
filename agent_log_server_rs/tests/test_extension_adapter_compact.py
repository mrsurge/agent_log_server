from __future__ import annotations

# Unit tests exercise the adapter's internal dispatch boundary without a live harness.
# pyright: reportPrivateUsage=false

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from typing import cast

from agent_log_server_rs.adapters.extension_adapter import ExtensionJsonRpcAdapter, RpcAdapterError


class CompactAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_seeds_cold_metadata_and_preserves_results(self) -> None:
        adapter = ExtensionJsonRpcAdapter()
        handler = SimpleNamespace(compact_session=AsyncMock())
        params: dict[str, object] = {"extension_id": "test-provider", "conversation_id": "local-1",
                  "provider_session_id": "provider-9", "cwd": "/project",
                  "settings": {"model": "test-model", "reasoning_effort": "xhigh"}}
        for result in ({"ok": True, "thread_id": "provider-9"},
                       {"ok": False, "error": "Provider refused"}):
            with patch.object(adapter, "_supported_handler", return_value=handler), \
                 patch.object(adapter._loader, "compact_session", new=AsyncMock(return_value=result)) as compact:
                response = await adapter._dispatch({"jsonrpc": "2.0", "method": "conversation.compact", "params": params})
                assert isinstance(response, dict)
                response = cast(dict[str, object], response)
                self.assertEqual(response.get("ok"), result["ok"])
                self.assertEqual(response.get("error"), result.get("error"))
                compact.assert_awaited_once_with("test-provider", "local-1")
                meta = adapter._load_meta("local-1")
                self.assertEqual(meta["thread_id"], "provider-9")
                self.assertEqual(meta["provider_session_id"], "provider-9")
                settings = meta["settings"]
                assert isinstance(settings, dict)
                settings = cast(dict[str, object], settings)
                self.assertEqual(settings["reasoning_effort"], "xhigh")

    async def test_unsupported_and_invalid_results_are_explicit(self) -> None:
        adapter = ExtensionJsonRpcAdapter()
        params: dict[str, object] = {"extension_id": "test-provider", "conversation_id": "local-1", "provider_session_id": "provider-9"}
        with patch.object(adapter, "_supported_handler", return_value=object()):
            with self.assertRaisesRegex(RpcAdapterError, "does not support compaction"):
                await adapter._conversation_compact(params)
        with patch.object(adapter, "_supported_handler", return_value=SimpleNamespace(compact_session=AsyncMock())), \
             patch.object(adapter._loader, "compact_session", new=AsyncMock(return_value={} )):
            with self.assertRaisesRegex(RpcAdapterError, "invalid compact result"):
                await adapter._conversation_compact(params)

    async def test_timeout_reports_unknown_completion(self) -> None:
        adapter = ExtensionJsonRpcAdapter()
        params: dict[str, object] = {"extension_id": "test-provider", "conversation_id": "local-1", "provider_session_id": "provider-9"}
        with patch.object(adapter, "_supported_handler", return_value=SimpleNamespace(compact_session=AsyncMock())), \
             patch.object(adapter._loader, "compact_session", new=AsyncMock(side_effect=TimeoutError)):
            result = await adapter._conversation_compact(params)
        self.assertFalse(result["ok"])
        self.assertIn("completion is unknown", str(result["error"]))

    async def test_capability_follows_handler_support(self) -> None:
        adapter = ExtensionJsonRpcAdapter()
        for handler, expected in ((object(), False), (SimpleNamespace(compact_session=AsyncMock()), True)):
            with patch.object(adapter, "_ensure_loader_initialized"), \
                 patch.object(adapter, "_extension_info", return_value={"active": True}), \
                 patch.object(adapter._loader, "get_handler", return_value=handler):
                result = await adapter._initialize({"extension_id": "test-provider"})
                capabilities = result["capabilities"]
                assert isinstance(capabilities, dict)
                capabilities = cast(dict[str, object], capabilities)
                self.assertEqual(capabilities["compaction"], expected)


if __name__ == "__main__":
    unittest.main()
