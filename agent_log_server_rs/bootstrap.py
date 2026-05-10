from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Sequence, cast

APP_ID = "als-rs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "12459"

SignalHandler = int | signal.Handlers | Callable[[int, FrameType | None], object]


@dataclass(frozen=True)
class BootstrapArgs:
    host: str
    port: str
    data_dir: str | None
    cache_dir: str | None
    config_dir: str | None
    static_dir: str | None
    server_bin: str | None
    cargo_manifest: str | None
    framework_shells_base_dir: str | None
    framework_shells_secret: str | None
    framework_shells_repo_fingerprint: str | None
    framework_shells_secret_fingerprint: str | None
    framework_shells_fws_socketio_server_pid: str | None
    framework_shells_run_id: str | None


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    env = _build_env(args)
    command = _server_command(args, env)
    return _run_child(command, env)


def _parse_args(argv: Sequence[str] | None) -> BootstrapArgs:
    parser = argparse.ArgumentParser(
        prog="als-rs",
        description="Launch the ALS-RS Rust server with isolated runtime roots.",
    )
    parser.add_argument("--host", default=os.environ.get("ALS_RS_HOST", DEFAULT_HOST))
    parser.add_argument("--port", default=os.environ.get("ALS_RS_PORT", DEFAULT_PORT))
    parser.add_argument("--data-dir", default=os.environ.get("ALS_RS_DATA_DIR"))
    parser.add_argument("--cache-dir", default=os.environ.get("ALS_RS_CACHE_DIR"))
    parser.add_argument("--config-dir", default=os.environ.get("ALS_RS_CONFIG_DIR"))
    parser.add_argument("--static-dir", default=os.environ.get("ALS_RS_STATIC_DIR"))
    parser.add_argument(
        "--server-bin",
        default=os.environ.get("ALS_RS_SERVER_BIN"),
        help="Path to an installed als-server binary. Defaults to cargo run in source checkouts.",
    )
    parser.add_argument(
        "--cargo-manifest",
        default=os.environ.get("ALS_RS_CARGO_MANIFEST"),
        help="Path to rust/Cargo.toml for development launches.",
    )
    parser.add_argument(
        "--framework-shells-base-dir",
        default=os.environ.get("FRAMEWORK_SHELLS_BASE_DIR"),
    )
    parser.add_argument(
        "--framework-shells-secret",
        default=os.environ.get("FRAMEWORK_SHELLS_SECRET"),
    )
    parser.add_argument(
        "--framework-shells-repo-fingerprint",
        default=os.environ.get("FRAMEWORK_SHELLS_REPO_FINGERPRINT"),
    )
    parser.add_argument(
        "--framework-shells-secret-fingerprint",
        default=os.environ.get("FRAMEWORK_SHELLS_SECRET_FINGERPRINT"),
    )
    parser.add_argument(
        "--framework-shells-fws-socketio-server-pid",
        default=os.environ.get("FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID"),
    )
    parser.add_argument(
        "--framework-shells-run-id",
        default=os.environ.get("FRAMEWORK_SHELLS_RUN_ID"),
    )
    raw = parser.parse_args(argv)
    return BootstrapArgs(
        host=cast(str, raw.host),
        port=cast(str, raw.port),
        data_dir=cast(str | None, raw.data_dir),
        cache_dir=cast(str | None, raw.cache_dir),
        config_dir=cast(str | None, raw.config_dir),
        static_dir=cast(str | None, raw.static_dir),
        server_bin=cast(str | None, raw.server_bin),
        cargo_manifest=cast(str | None, raw.cargo_manifest),
        framework_shells_base_dir=cast(str | None, raw.framework_shells_base_dir),
        framework_shells_secret=cast(str | None, raw.framework_shells_secret),
        framework_shells_repo_fingerprint=cast(str | None, raw.framework_shells_repo_fingerprint),
        framework_shells_secret_fingerprint=cast(str | None, raw.framework_shells_secret_fingerprint),
        framework_shells_fws_socketio_server_pid=cast(
            str | None,
            raw.framework_shells_fws_socketio_server_pid,
        ),
        framework_shells_run_id=cast(str | None, raw.framework_shells_run_id),
    )


def _build_env(args: BootstrapArgs) -> dict[str, str]:
    env = os.environ.copy()
    data_dir = Path(args.data_dir) if args.data_dir else _default_data_dir()
    cache_dir = Path(args.cache_dir) if args.cache_dir else _default_cache_dir()
    config_dir = Path(args.config_dir) if args.config_dir else data_dir
    static_dir = Path(args.static_dir) if args.static_dir else _source_root() / "agent_log_server" / "static"

    for root in (data_dir, cache_dir, config_dir):
        root.mkdir(parents=True, exist_ok=True)

    env["ALS_RS_HOST"] = str(args.host)
    env["ALS_RS_PORT"] = str(args.port)
    env["ALS_RS_DATA_DIR"] = str(data_dir)
    env["ALS_RS_CACHE_DIR"] = str(cache_dir)
    env["ALS_RS_CONFIG_DIR"] = str(config_dir)
    env["ALS_RS_STATIC_DIR"] = str(static_dir)
    env["ALS_RS_PYTHON_BIN"] = sys.executable
    if _ferrous_framework_enabled(args):
        env.pop("PYO3_CONFIG_FILE", None)
        env["PYO3_PYTHON"] = sys.executable
    _set_if_present(env, "FRAMEWORK_SHELLS_BASE_DIR", args.framework_shells_base_dir)
    _set_if_present(env, "FRAMEWORK_SHELLS_SECRET", args.framework_shells_secret)
    _set_if_present(
        env,
        "FRAMEWORK_SHELLS_REPO_FINGERPRINT",
        args.framework_shells_repo_fingerprint,
    )
    _set_if_present(
        env,
        "FRAMEWORK_SHELLS_SECRET_FINGERPRINT",
        args.framework_shells_secret_fingerprint or args.framework_shells_repo_fingerprint,
    )
    _set_if_present(
        env,
        "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
        args.framework_shells_fws_socketio_server_pid,
    )
    _set_if_present(env, "FRAMEWORK_SHELLS_RUN_ID", args.framework_shells_run_id)
    return env


def _server_command(args: BootstrapArgs, env: dict[str, str] | None = None) -> list[str]:
    env = env if env is not None else os.environ
    framework_shell_args = _framework_shell_args(args)
    if args.server_bin:
        return [str(Path(args.server_bin)), *framework_shell_args]

    manifest = Path(args.cargo_manifest) if args.cargo_manifest else _default_rust_manifest()
    packaged_manifest = _packaged_rust_manifest()
    target_dir = None
    if (
        not args.cargo_manifest
        and packaged_manifest is not None
        and manifest == packaged_manifest
    ):
        cache_dir = Path(env["ALS_RS_CACHE_DIR"])
        target_dir = Path(env.setdefault("CARGO_TARGET_DIR", str(cache_dir / "cargo-target")))

    debug_binary = (target_dir or manifest.parent / "target") / "debug" / "als-server"
    use_ferrous_framework = _ferrous_framework_enabled(args)
    if debug_binary.exists() and not use_ferrous_framework:
        return [str(debug_binary), *framework_shell_args]

    command = [
        "cargo",
        "run",
        "--manifest-path",
        str(manifest),
        "-p",
        "als-server",
    ]
    if use_ferrous_framework:
        command.extend(["--features", "ferrous-framework-pyo3"])
    command.extend(["--", *framework_shell_args])
    return command


def _framework_shell_args(args: BootstrapArgs) -> list[str]:
    values = [
        ("--framework-shells-base-dir", args.framework_shells_base_dir),
        ("--framework-shells-secret", args.framework_shells_secret),
        ("--framework-shells-repo-fingerprint", args.framework_shells_repo_fingerprint),
        (
            "--framework-shells-secret-fingerprint",
            args.framework_shells_secret_fingerprint or args.framework_shells_repo_fingerprint,
        ),
        (
            "--framework-shells-fws-socketio-server-pid",
            args.framework_shells_fws_socketio_server_pid,
        ),
        ("--framework-shells-run-id", args.framework_shells_run_id),
    ]
    rendered: list[str] = []
    for flag, value in values:
        if value:
            rendered.extend((flag, value))
    return rendered


def _ferrous_framework_enabled(args: BootstrapArgs) -> bool:
    disabled = os.environ.get("ALS_RS_DISABLE_FERROUS_FRAMEWORK", "").lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    return any(
        (
            args.framework_shells_base_dir,
            args.framework_shells_secret,
            args.framework_shells_repo_fingerprint,
            args.framework_shells_secret_fingerprint,
            args.framework_shells_fws_socketio_server_pid,
            args.framework_shells_run_id,
        )
    )


def _set_if_present(env: dict[str, str], key: str, value: str | None) -> None:
    if value:
        env[key] = value


def _run_child(command: Sequence[str], env: dict[str, str]) -> int:
    child = subprocess.Popen(command, env=env)
    previous_handlers: dict[signal.Signals, SignalHandler] = {}

    def forward_signal(signum: int, _frame: FrameType | None) -> object:
        if child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = cast(SignalHandler, signal.getsignal(signum))
        signal.signal(signum, forward_signal)

    try:
        return child.wait()
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _default_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / APP_ID


def _default_cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / APP_ID


def _source_root() -> Path:
    return Path(__file__).parent.parent


def _package_root() -> Path:
    return Path(__file__).parent


def _packaged_rust_manifest() -> Path | None:
    manifest = _package_root() / "rust" / "Cargo.toml"
    return manifest if manifest.is_file() else None


def _default_rust_manifest() -> Path:
    source_manifest = _source_root() / "rust" / "Cargo.toml"
    if source_manifest.is_file():
        return source_manifest
    packaged_manifest = _packaged_rust_manifest()
    if packaged_manifest is not None:
        return packaged_manifest
    return source_manifest


if __name__ == "__main__":
    sys.exit(main())
