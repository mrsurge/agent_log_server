"""Stdlib-only bootstrap; native rmcp owns stdio after exec, never an SDK relay."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import cast
from agent_log_server_rs import bootstrap as artifacts

BINARY = "als-mcp"
RECEIPT = "als-mcp.manifest.json"


def python_abi() -> dict[str, object]:
    return {"version": list(sys.version_info[:2]), "soabi": sysconfig.get_config_var("SOABI"),
            "implementation": sys.implementation.name, "free_threaded": bool(cast(object, sysconfig.get_config_var("Py_GIL_DISABLED"))),
            "platform": sys.platform, "machine": platform.machine()}


def runtime_env() -> dict[str, str]:
    env = dict(os.environ)
    root = Path(__file__).absolute().parent.parent
    env["ALS_MCP_PYTHON_ROOT"] = str(root)
    env["PYTHONHOME"] = sys.base_prefix
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([str(root), *[str(Path(p).absolute()) for p in sys.path if p]]))
    return env


def validate(binary: Path, receipt: Path, *, fingerprint: str | None = None) -> None:
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"Native MCP executable is missing/unusable: {binary}")
    raw = cast(object, json.loads(receipt.read_text()))
    if not isinstance(raw, dict):
        raise RuntimeError("Native MCP receipt must be an object")
    data = cast(dict[str, object], raw)
    if data.get("schema") != 1 or data.get("python") != python_abi():
        raise RuntimeError("Native MCP artifact is incompatible with the invoking Python ABI/platform")
    if data.get("sha256") != artifacts.sha256_file(binary):
        raise RuntimeError("Native MCP artifact digest mismatch")
    if fingerprint is not None and data.get("fingerprint") != fingerprint:
        raise RuntimeError("Native MCP source fingerprint mismatch")


def fingerprint(manifest: Path) -> str:
    digest = hashlib.sha256(b"als-mcp-source-v1\0")
    digest.update(json.dumps(python_abi(), sort_keys=True).encode())
    digest.update(str(manifest.absolute()).encode())
    digest.update(subprocess.check_output(["rustc", "-vV"]))
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CC", "CFLAGS", "LDFLAGS"):
        digest.update(f"{key}={os.environ.get(key, '')}\0".encode())
    for path in [manifest, manifest.with_name("Cargo.lock"), *sorted((manifest.parent / "src").glob("*.rs"))]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    library = Path(str(cast(object, sysconfig.get_config_var("LIBDIR")))) / str(cast(object, sysconfig.get_config_var("LDLIBRARY")))
    if not library.is_file():
        raise RuntimeError(f"Invoking Python has no discoverable shared library: {library}")
    digest.update(artifacts.sha256_file(library).encode())
    return digest.hexdigest()


def select(env: dict[str, str]) -> Path:
    package = Path(__file__).absolute().parent
    supplied = env.get("ALS_MCP_BIN")
    if supplied:
        binary = Path(supplied).expanduser().absolute()
        validate(binary, binary.with_name(RECEIPT))
        return binary
    packaged = package / "bin" / BINARY
    receipt = packaged.with_name(RECEIPT)
    if packaged.exists() or receipt.exists():
        validate(packaged, receipt)
        return packaged
    if (package / "bin" / "als-server.manifest.json").exists():
        raise RuntimeError("Packaged ALS payload is missing native MCP; refusing source/SDK fallback")
    manifest = package.parent / "rust/mcp_embedding_probe/Cargo.toml"
    if not manifest.is_file():
        manifest = package / "rust/mcp_embedding_probe/Cargo.toml"
    if not manifest.is_file():
        raise RuntimeError("No native MCP artifact or buildable source is installed")
    key = fingerprint(manifest)
    cache = Path(env.get("ALS_RS_CACHE_DIR") or artifacts.default_cache_dir()) / "mcp"
    binary = cache / "bin" / key / BINARY
    with artifacts.exclusive_build_cache_lock(cache):
        if binary.exists() or binary.with_name(RECEIPT).exists():
            validate(binary, binary.with_name(RECEIPT), fingerprint=key)
            return binary
        build_env = dict(env)
        for name in list(build_env):
            if name.startswith("PYO3_"):
                del build_env[name]
        build_env["PYO3_PYTHON"] = sys.executable
        target = artifacts.bootstrap_cargo_target_dir(manifest, build_env)
        build_env["CARGO_TARGET_DIR"] = str(target)
        print("[als-mcp] Preparing native MCP executable", file=sys.stderr, flush=True)
        command = ["cargo", "build", "--locked", "--manifest-path", str(manifest), "--bin", BINARY, "--release"]
        child = subprocess.Popen(command, env=build_env, stdout=sys.stderr, start_new_session=True)
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum: int, frame: object) -> None:
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)
        try:
            status = child.wait()
            if status:
                raise subprocess.CalledProcessError(status, command)
        finally:
            signal.signal(signal.SIGTERM, previous)
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
        built = target / "release" / BINARY
        artifacts.publish_cached_binary(built, binary)
        binary.with_name(RECEIPT).write_text(json.dumps({"schema": 1, "python": python_abi(), "fingerprint": key,
                                                     "sha256": artifacts.sha256_file(binary)}, indent=2) + "\n")
    return binary


def main() -> None:
    if sys.argv[1:]:
        raise RuntimeError("Native MCP stdio bootstrap accepts no command-line arguments")
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in {"", "stdio"}:
        raise RuntimeError("agent-pty-blocks supports stdio only; no Python HTTP/SSE fallback")
    env = runtime_env()
    binary = select(env)
    info = subprocess.run([str(binary), "--runtime-info"], env=env, capture_output=True, text=True, timeout=10, check=True)
    if json.loads(info.stdout) != python_abi():
        raise RuntimeError("Native MCP loaded a different interpreter ABI; refusing launch")
    os.execve(str(binary), [str(binary)], env)
