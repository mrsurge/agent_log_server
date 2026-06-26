from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_log_server_rs import bootstrap


def _args(manifest: Path) -> bootstrap.BootstrapArgs:
    return bootstrap.BootstrapArgs(
        host=bootstrap.DEFAULT_HOST,
        port=bootstrap.DEFAULT_PORT,
        data_dir=None,
        cache_dir=None,
        config_dir=None,
        static_dir=None,
        server_bin=None,
        cargo_manifest=str(manifest),
        framework_shells_base_dir=None,
        framework_shells_secret=None,
        framework_shells_repo_fingerprint=None,
        framework_shells_secret_fingerprint=None,
        framework_shells_fws_socketio_server_pid=None,
        framework_shells_run_id=None,
    )


class BootstrapCommandTests(unittest.TestCase):
    def test_server_command_isolates_bootstrap_cargo_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            manifest.parent.mkdir()
            manifest.write_text("[workspace]\n", encoding="utf-8")
            cache_dir = root / "cache"
            env: dict[str, str] = {"ALS_RS_CACHE_DIR": str(cache_dir)}

            command = bootstrap._server_command(_args(manifest), env)  # pyright: ignore[reportPrivateUsage]

            self.assertEqual(command[:4], ["cargo", "run", "--manifest-path", str(manifest)])
            self.assertEqual(command[4:6], ["-p", "als-server"])
            target_dir = Path(env["CARGO_TARGET_DIR"])
            self.assertEqual(target_dir.parent, cache_dir / "cargo-target")
            self.assertNotEqual(target_dir, manifest.parent / "target")

    def test_server_command_preserves_explicit_cargo_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            manifest.parent.mkdir()
            manifest.write_text("[workspace]\n", encoding="utf-8")
            explicit_target = root / "explicit-target"
            env = {
                "ALS_RS_CACHE_DIR": str(root / "cache"),
                "CARGO_TARGET_DIR": str(explicit_target),
            }

            bootstrap._server_command(_args(manifest), env)  # pyright: ignore[reportPrivateUsage]

            self.assertEqual(env["CARGO_TARGET_DIR"], str(explicit_target))


if __name__ == "__main__":
    unittest.main()
