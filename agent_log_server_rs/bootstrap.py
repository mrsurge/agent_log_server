from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import FrameType
from typing import Sequence

APP_ID = "als-rs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "12459"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    env = _build_env(args)
    command = _server_command(args)
    return _run_child(command, env)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="als-rs",
        description="Launch the ALS-RS Rust server with isolated runtime roots.",
    )
    parser.add_argument("--host", default=os.environ.get("ALS_RS_HOST", DEFAULT_HOST))
    parser.add_argument("--port", default=os.environ.get("ALS_RS_PORT", DEFAULT_PORT))
    parser.add_argument("--data-dir", default=os.environ.get("ALS_RS_DATA_DIR"))
    parser.add_argument("--cache-dir", default=os.environ.get("ALS_RS_CACHE_DIR"))
    parser.add_argument("--config-dir", default=os.environ.get("ALS_RS_CONFIG_DIR"))
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
    return parser.parse_args(argv)


def _build_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    data_dir = Path(args.data_dir) if args.data_dir else _default_data_dir()
    cache_dir = Path(args.cache_dir) if args.cache_dir else _default_cache_dir()
    config_dir = Path(args.config_dir) if args.config_dir else data_dir

    for root in (data_dir, cache_dir, config_dir):
        root.mkdir(parents=True, exist_ok=True)

    env["ALS_RS_HOST"] = str(args.host)
    env["ALS_RS_PORT"] = str(args.port)
    env["ALS_RS_DATA_DIR"] = str(data_dir)
    env["ALS_RS_CACHE_DIR"] = str(cache_dir)
    env["ALS_RS_CONFIG_DIR"] = str(config_dir)
    return env


def _server_command(args: argparse.Namespace) -> list[str]:
    if args.server_bin:
        return [str(Path(args.server_bin))]

    manifest = (
        Path(args.cargo_manifest)
        if args.cargo_manifest
        else _source_root() / "rust" / "Cargo.toml"
    )
    debug_binary = manifest.parent / "target" / "debug" / "als-server"
    if debug_binary.exists():
        return [str(debug_binary)]

    return [
        "cargo",
        "run",
        "--manifest-path",
        str(manifest),
        "-p",
        "als-server",
        "--",
    ]


def _run_child(command: Sequence[str], env: dict[str, str]) -> int:
    child = subprocess.Popen(command, env=env)
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def forward_signal(signum: int, _frame: FrameType | None) -> None:
        if child.poll() is None:
            child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
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


if __name__ == "__main__":
    sys.exit(main())
