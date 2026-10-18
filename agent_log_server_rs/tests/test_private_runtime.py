from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_log_server_rs import private_runtime as runtime
from release import private_python


class PrivateRuntimeTests(unittest.TestCase):
    def seed(self, package: Path) -> Path:
        root = package / runtime.DIRECTORY
        for name in ("python/bin/python3.14", "python/lib/libpython3.14.so.1.0", "python/lib/python3.14/encodings/__init__.py"):
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"test-runtime")
        private_python.refresh(root)
        return root

    def test_missing_is_source_mode_but_partial_is_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            self.assertIsNone(runtime.verify(package))
            (package / runtime.DIRECTORY).mkdir()
            with self.assertRaises(FileNotFoundError):
                runtime.verify(package)

    def test_checksums_and_traversal_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            root = self.seed(package)
            self.assertIsNotNone(runtime.verify(package))
            file = root / "python/bin/python3.14"
            file.write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                runtime.verify(package)
            private_python.refresh(root)
            data = json.loads((root / runtime.MANIFEST).read_text())
            data["files"]["../escape"] = "bad"
            (root / runtime.MANIFEST).write_text(json.dumps(data))
            with self.assertRaisesRegex(RuntimeError, "inventory path"):
                runtime.verify(package)
            private_python.refresh(root)
            file.unlink()
            file.symlink_to(root / "python/lib/libpython3.14.so.1.0")
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                runtime.verify(package)

    def test_exec_isolated_python_preserves_application_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            root = self.seed(package)
            with patch.dict(os.environ, {"PYTHONPATH": "/untrusted", "VIRTUAL_ENV": "/outer", "CONVERSATION_ID": "keep", "LD_PRELOAD": "/bad"}), patch.object(runtime.os, "execve") as execute:
                runtime.enter(package)
            executable, args, env = execute.call_args.args
            self.assertEqual(executable, str(root / "python/bin/python3.14"))
            self.assertEqual(args[1:4], ["-I", "-S", "-B"])
            self.assertEqual(env["CONVERSATION_ID"], "keep")
            self.assertNotIn("VIRTUAL_ENV", env)
            self.assertNotIn("LD_PRELOAD", env)
            self.assertNotIn("/untrusted", env["PYTHONPATH"])

    def test_builder_rejects_symlink_and_site_hook_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "input"
            source.mkdir()
            (source / "bad.pth").write_text("import bad")
            with self.assertRaisesRegex(RuntimeError, "site customization"):
                private_python._copy(source, root / "output")
            (source / "bad.pth").unlink()
            (source / "link").symlink_to("/missing")
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                private_python._copy(source, root / "output")


if __name__ == "__main__":
    unittest.main()
