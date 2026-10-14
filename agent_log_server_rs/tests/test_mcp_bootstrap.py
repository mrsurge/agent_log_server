from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_log_server_rs import mcp_bootstrap as boot


class BootstrapTests(unittest.TestCase):
    def test_receipt_rejects_wrong_abi_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / boot.BINARY
            binary.write_bytes(b"native-test")
            binary.chmod(0o755)
            receipt = binary.with_name(boot.RECEIPT)
            data = {"schema": 1, "python": boot.python_abi(), "sha256": boot.artifacts._sha256_file(binary)}
            receipt.write_text(json.dumps(data))
            boot.validate(binary, receipt)
            data["python"] = {"version": [0, 0]}
            receipt.write_text(json.dumps(data))
            with self.assertRaisesRegex(RuntimeError, "incompatible"):
                boot.validate(binary, receipt)
            data["python"] = boot.python_abi()
            receipt.write_text(json.dumps(data))
            binary.write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "digest"):
                boot.validate(binary, receipt)

    def test_exec_preserves_environment_and_does_not_relay_stdio(self) -> None:
        env = boot.runtime_env()
        self.assertIn("ALS_MCP_PYTHON_ROOT", env)
        with patch.dict(os.environ, {"CONVERSATION_ID": "test-owner", "MCP_TRANSPORT": "stdio"}), patch.object(boot.sys, "argv", ["mcp_agent_pty_server.py"]), patch.object(boot, "select", return_value=Path("/selected/native")), patch.object(boot.subprocess, "run") as probe, patch.object(boot.os, "execve") as execute:
            probe.return_value.stdout = json.dumps(boot.python_abi())
            boot.main()
            args = execute.call_args.args
            self.assertEqual(args[0], "/selected/native")
            self.assertEqual(args[1], ["/selected/native"])
            self.assertEqual(args[2]["CONVERSATION_ID"], "test-owner")
            self.assertEqual(probe.call_args.args[0], ["/selected/native", "--runtime-info"])

    def test_no_sdk_or_http_fallback(self) -> None:
        with patch.dict(os.environ, {"MCP_TRANSPORT": "sse"}), patch.object(boot.sys, "argv", ["mcp_agent_pty_server.py"]):
            with self.assertRaisesRegex(RuntimeError, "stdio only"):
                boot.main()
