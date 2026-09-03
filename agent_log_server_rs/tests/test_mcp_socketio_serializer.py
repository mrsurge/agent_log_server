from __future__ import annotations

import tempfile
import unittest
from typing import cast

from extensions.codex_ext.mcp_contract import apply_mcp_context as apply_codex_mcp_context
from extensions.copilot_sdk.mcp_contract import apply_mcp_context as apply_copilot_mcp_context


class McpSocketIoSerializerTests(unittest.TestCase):
    def _settings(self, cwd: str) -> dict[str, object]:
        return {
            "cwd": cwd,
            "mcp_context": {
                "conversation_id": "conv-serializer",
                "cwd": cwd,
                "requested_servers": {},
                "defaults": {
                    "agent-pty-blocks": {
                        "enabled_by_default": True,
                        "cwd": cwd,
                        "conversation_id": "conv-serializer",
                        "appserver_origin": "http://127.0.0.1:12459",
                        "socketio_serializer": "msgpack",
                    }
                },
            },
        }

    def test_codex_mcp_child_inherits_socketio_serializer(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            config = apply_codex_mcp_context({}, self._settings(cwd))

        self.assertIsNotNone(config)
        assert config is not None
        mcp_servers = cast(dict[str, object], config["mcp_servers"])
        server = cast(dict[str, object], mcp_servers["agent-pty-blocks"])
        env = cast(dict[str, str], server["env"])
        self.assertEqual(env["AGENT_LOG_SOCKETIO_SERIALIZER"], "msgpack")

    def test_codex_high_context_uses_450k_window_and_400k_compaction(self) -> None:
        config = apply_codex_mcp_context(
            {},
            {},
            enable_high_context_400k=True,
        )

        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config["model_context_window"], 450000)
        self.assertEqual(config["model_auto_compact_token_limit"], 400000)

    def test_copilot_mcp_child_inherits_socketio_serializer(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            settings = apply_copilot_mcp_context(self._settings(cwd))

        mcp_servers = cast(dict[str, object], settings["mcp_servers"])
        server = cast(dict[str, object], mcp_servers["agent-pty-blocks"])
        env = cast(dict[str, str], server["env"])
        self.assertEqual(env["AGENT_LOG_SOCKETIO_SERIALIZER"], "msgpack")


if __name__ == "__main__":
    unittest.main()
