from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_log_server_rs import mcp_bootstrap as boot
from agent_log_server_rs import bootstrap as als


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

    def test_missing_or_stale_selector_never_builds_or_probes_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selector = Path(tmp) / "selection.json"
            published = Path(tmp) / "bin"
            context = (Path(tmp) / "Cargo.toml", selector, published, "source")
            with patch.object(boot, "explicit_artifact", return_value=None), patch.object(boot, "source_context", return_value=context), patch.object(boot.subprocess, "check_output", side_effect=AssertionError("compiler probe")), patch.object(boot.subprocess, "Popen", side_effect=AssertionError("Cargo build")):
                with self.assertRaisesRegex(RuntimeError, "not prepared"):
                    boot.select({})
                boot.atomic_json(selector, {"schema": 1, "python": boot.python_abi(), "source": "old"})
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    boot.select({})

    def test_valid_selector_launches_without_source_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selector = Path(tmp) / "selection.json"
            published = Path(tmp) / "bin"
            key = "a" * 64
            binary = published / key / boot.BINARY
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"native")
            binary.chmod(0o755)
            boot.atomic_json(binary.with_name(boot.RECEIPT), {"schema": 1, "python": boot.python_abi(), "fingerprint": key, "sha256": boot.artifacts.sha256_file(binary)})
            boot.atomic_json(selector, {"schema": 1, "python": boot.python_abi(), "source": "source", "fingerprint": key, "binary": str(binary)})
            with patch.object(boot, "explicit_artifact", return_value=None), patch.object(boot, "source_context", return_value=(Path(tmp) / "Cargo.toml", selector, published, "source")), patch.object(boot.subprocess, "check_output", side_effect=AssertionError("compiler probe")), patch.object(boot.subprocess, "Popen", side_effect=AssertionError("Cargo build")):
                self.assertEqual(boot.select({}), binary)

    def test_als_build_only_prepares_before_launch_and_preserves_server_cache(self) -> None:
        events: list[str] = []
        def server(args: object, env: dict[str, str]) -> list[str]:
            events.append("server")
            env["CARGO_TARGET_DIR"] = "/existing/server/cache"
            return ["/server"]
        def mcp(env: dict[str, str]) -> Path:
            events.append("mcp")
            self.assertNotIn("CARGO_TARGET_DIR", env)
            return Path("/mcp")
        with patch.object(als, "_build_env", return_value={}), patch.object(als, "_server_command", side_effect=server), patch.object(boot, "prepare", side_effect=mcp), patch.object(als, "_run_child") as run:
            self.assertEqual(als.main(["--build-only"]), 0)
            run.assert_not_called()
            self.assertEqual(events, ["server", "mcp"])

    def test_config_declares_existing_target_and_cache_policy(self) -> None:
        config = boot.build_config()
        self.assertEqual(config["binary"], boot.BINARY)
        self.assertIs(config["pythonAbi"], True)

    def test_preparation_reuses_valid_binary_and_atomically_publishes_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            selector = Path(tmp) / "selection.json"
            published = Path(tmp) / "bin"
            key = "b" * 64
            binary = published / key / boot.BINARY
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"native")
            binary.chmod(0o755)
            boot.atomic_json(binary.with_name(boot.RECEIPT), {"schema": 1, "python": boot.python_abi(), "fingerprint": key, "sha256": boot.artifacts.sha256_file(binary)})
            with patch.object(boot, "explicit_artifact", return_value=None), patch.object(boot, "source_context", return_value=(Path(tmp) / "Cargo.toml", selector, published, "source")), patch.object(boot, "fingerprint", return_value=key), patch.object(boot.subprocess, "Popen", side_effect=AssertionError("unexpected rebuild")):
                self.assertEqual(boot.prepare({}), binary)
            selected = json.loads(selector.read_text())
            self.assertEqual(selected["source"], "source")
            self.assertEqual(selected["binary"], str(binary))
            self.assertFalse(list(Path(tmp).glob(".selection.json.*")))
