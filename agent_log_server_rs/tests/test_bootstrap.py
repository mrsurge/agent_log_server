from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from agent_log_server_rs import bootstrap


def _args(
    manifest: Path,
    *,
    debug: bool = False,
    server_bin: str | None = None,
    framework_shells_run_id: str | None = None,
) -> bootstrap.BootstrapArgs:
    return bootstrap.BootstrapArgs(
        host=bootstrap.DEFAULT_HOST,
        port=bootstrap.DEFAULT_PORT,
        data_dir=None,
        cache_dir=None,
        config_dir=None,
        static_dir=None,
        server_bin=server_bin,
        cargo_manifest=str(manifest),
        debug=debug,
        framework_shells_base_dir=None,
        framework_shells_secret=None,
        framework_shells_repo_fingerprint=None,
        framework_shells_secret_fingerprint=None,
        framework_shells_fws_socketio_server_pid=None,
        framework_shells_run_id=framework_shells_run_id,
    )


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(path.stat().st_mode | 0o755)


class BootstrapCommandTests(unittest.TestCase):
    def test_release_build_publishes_and_prunes_stale_final_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            _write(manifest, "[workspace]\n")
            cache_dir = root / "cache"
            stale = cache_dir / "bin" / "stale" / "debug" / "als-server"
            _write(stale, "stale", executable=True)
            incremental = cache_dir / "cargo-target" / "incremental" / "keep"
            _write(incremental, "cargo-cache")
            env: dict[str, str] = {"ALS_RS_CACHE_DIR": str(cache_dir)}
            built_commands: list[list[str]] = []

            def fake_build(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                built_commands.append(command)
                build_env = cast(dict[str, str], kwargs["env"])
                target_dir = Path(build_env["CARGO_TARGET_DIR"])
                _write(target_dir / "release" / "als-server", "release", executable=True)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(
                    bootstrap,
                    "_rust_source_fingerprint",
                    return_value="selected",
                ),
                mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_build),
            ):
                command = bootstrap._server_command(  # pyright: ignore[reportPrivateUsage]
                    _args(manifest),
                    env,
                )

            selected = cache_dir / "bin" / "selected" / "release" / "als-server"
            self.assertEqual(command, [str(selected)])
            self.assertEqual(selected.read_text(encoding="utf-8"), "release")
            self.assertTrue(bootstrap._cached_binary_is_usable(selected))  # pyright: ignore[reportPrivateUsage]
            self.assertFalse(stale.exists())
            self.assertEqual(incremental.read_text(encoding="utf-8"), "cargo-cache")
            cargo_command = built_commands[0]
            self.assertEqual(cargo_command[:2], ["cargo", "build"])
            self.assertIn("--release", cargo_command)
            target_dir = Path(env["CARGO_TARGET_DIR"])
            self.assertEqual(target_dir.parent, cache_dir / "cargo-target")
            self.assertNotEqual(target_dir, manifest.parent / "target")

    def test_valid_cache_hit_skips_build_and_prunes_other_final_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            _write(manifest, "[workspace]\n")
            cache_dir = root / "cache"
            selected = cache_dir / "bin" / "selected" / "release" / "als-server"
            stale = cache_dir / "bin" / "stale" / "release" / "als-server"
            _write(selected, "selected", executable=True)
            _write(stale, "stale", executable=True)

            with (
                mock.patch.object(
                    bootstrap,
                    "_rust_source_fingerprint",
                    return_value="selected",
                ),
                mock.patch.object(bootstrap.subprocess, "run") as run,
            ):
                command = bootstrap._server_command(  # pyright: ignore[reportPrivateUsage]
                    _args(manifest),
                    {"ALS_RS_CACHE_DIR": str(cache_dir)},
                )

            self.assertEqual(command, [str(selected)])
            run.assert_not_called()
            self.assertTrue(selected.exists())
            self.assertFalse(stale.exists())

    def test_debug_build_uses_debug_profile_without_release_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            _write(manifest, "[workspace]\n")
            cache_dir = root / "cache"
            env: dict[str, str] = {"ALS_RS_CACHE_DIR": str(cache_dir)}
            built_commands: list[list[str]] = []

            def fake_build(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                built_commands.append(command)
                build_env = cast(dict[str, str], kwargs["env"])
                target_dir = Path(build_env["CARGO_TARGET_DIR"])
                _write(target_dir / "debug" / "als-server", "debug", executable=True)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(
                    bootstrap,
                    "_rust_source_fingerprint",
                    return_value="debug-selected",
                ),
                mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_build),
            ):
                command = bootstrap._server_command(  # pyright: ignore[reportPrivateUsage]
                    _args(manifest, debug=True),
                    env,
                )

            selected = cache_dir / "bin" / "debug-selected" / "debug" / "als-server"
            self.assertEqual(command, [str(selected)])
            self.assertNotIn("--release", built_commands[0])

    def test_failed_build_preserves_previous_final_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            _write(manifest, "[workspace]\n")
            cache_dir = root / "cache"
            stale = cache_dir / "bin" / "previous" / "release" / "als-server"
            _write(stale, "previous", executable=True)

            with (
                mock.patch.object(
                    bootstrap,
                    "_rust_source_fingerprint",
                    return_value="selected",
                ),
                mock.patch.object(
                    bootstrap.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 9),
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                bootstrap._server_command(  # pyright: ignore[reportPrivateUsage]
                    _args(manifest),
                    {"ALS_RS_CACHE_DIR": str(cache_dir)},
                )

            self.assertEqual(raised.exception.code, 9)
            self.assertEqual(stale.read_text(encoding="utf-8"), "previous")
            self.assertFalse((cache_dir / "bin" / "selected").exists())

    def test_explicit_server_binary_bypasses_cache_and_forwards_framework_args(self) -> None:
        manifest = Path("rust/Cargo.toml")
        args = _args(
            manifest,
            server_bin="/example/als-server",
            framework_shells_run_id="test-run",
        )

        with mock.patch.object(bootstrap.subprocess, "run") as run:
            command = bootstrap._server_command(args, {})  # pyright: ignore[reportPrivateUsage]

        self.assertEqual(
            command,
            ["/example/als-server", "--framework-shells-run-id", "test-run"],
        )
        run.assert_not_called()

    def test_server_command_preserves_explicit_cargo_target_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            manifest = root / "rust" / "Cargo.toml"
            _write(manifest, "[workspace]\n")
            explicit_target = root / "explicit-target"
            cache_dir = root / "cache"
            env = {
                "ALS_RS_CACHE_DIR": str(cache_dir),
                "CARGO_TARGET_DIR": str(explicit_target),
            }

            def fake_build(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
                _write(explicit_target / "release" / "als-server", "release", executable=True)
                return subprocess.CompletedProcess(command, 0)

            with (
                mock.patch.object(
                    bootstrap,
                    "_rust_source_fingerprint",
                    return_value="selected",
                ),
                mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_build),
            ):
                bootstrap._server_command(  # pyright: ignore[reportPrivateUsage]
                    _args(manifest),
                    env,
                )

            self.assertEqual(env["CARGO_TARGET_DIR"], str(explicit_target))

    def test_source_fingerprint_tracks_profile_rust_and_embedded_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            workspace = Path(temp_root) / "rust"
            manifest = workspace / "Cargo.toml"
            source = workspace / "crates" / "server" / "src" / "main.rs"
            embedded = source.parent / "index.html"
            _write(manifest, "[workspace]\n")
            _write(workspace / "Cargo.lock", "# lock\n")
            _write(
                workspace / "crates" / "server" / "Cargo.toml",
                '[package]\nname = "server"\n',
            )
            _write(source, 'const INDEX: &str = include_str!("index.html");\n')
            _write(embedded, "first")

            release = bootstrap._rust_source_fingerprint(  # pyright: ignore[reportPrivateUsage]
                manifest,
                profile="release",
            )
            debug = bootstrap._rust_source_fingerprint(  # pyright: ignore[reportPrivateUsage]
                manifest,
                profile="debug",
            )
            _write(source, 'const INDEX: &str = include_str!("index.html");\nfn main() {}\n')
            rust_changed = bootstrap._rust_source_fingerprint(  # pyright: ignore[reportPrivateUsage]
                manifest,
                profile="release",
            )
            _write(embedded, "second")
            embedded_changed = bootstrap._rust_source_fingerprint(  # pyright: ignore[reportPrivateUsage]
                manifest,
                profile="release",
            )

            self.assertNotEqual(release, debug)
            self.assertNotEqual(release, rust_changed)
            self.assertNotEqual(rust_changed, embedded_changed)

    def test_parse_args_exposes_debug_profile_flag(self) -> None:
        default_args = bootstrap._parse_args([])  # pyright: ignore[reportPrivateUsage]
        debug_args = bootstrap._parse_args(["--debug"])  # pyright: ignore[reportPrivateUsage]

        self.assertFalse(default_args.debug)
        self.assertTrue(debug_args.debug)


if __name__ == "__main__":
    unittest.main()
