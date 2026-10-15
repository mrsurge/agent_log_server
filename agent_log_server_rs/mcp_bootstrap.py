"""Stdlib-only bootstrap; native rmcp owns stdio after exec, never an SDK relay."""
from __future__ import annotations

import hashlib
import glob
import json
import os
import platform
import signal
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import cast
from agent_log_server_rs import bootstrap as artifacts

BINARY = "als-mcp"
RECEIPT = "als-mcp.manifest.json"


def build_config() -> dict[str, object]:
    raw = cast(object, json.loads(Path(__file__).with_name("native_mcp_build.json").read_text()))
    if not isinstance(raw, dict):
        raise RuntimeError("Native MCP build config must be an object")
    config = cast(dict[str, object], raw)
    if config.get("schemaVersion") != 1 or config.get("binary") != BINARY or config.get("environmentKey") != "ALS_MCP_BIN" or config.get("pythonAbi") is not True or config.get("profile") != "release":
        raise RuntimeError("Unsupported native MCP build configuration")
    if not isinstance(config.get("features"), list) or any(not isinstance(v, str) for v in cast(list[object], config["features"])):
        raise RuntimeError("Native MCP build features must be strings")
    for field in ("manifest",):
        value = config.get(field)
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise RuntimeError(f"Invalid native MCP {field}")
    cache = config.get("cache")
    policy = config.get("fingerprint")
    if not isinstance(cache, dict) or not isinstance(policy, dict):
        raise RuntimeError("Native MCP cache/fingerprint configuration is missing")
    cache_map = cast(dict[str, object], cache)
    for field in ("root", "publishedDirectory", "targetDirectory"):
        value = cache_map.get(field)
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise RuntimeError(f"Invalid native MCP cache {field}")
    if cache_map.get("targetDirectoryEnvironment") != "ALS_MCP_CARGO_TARGET_DIR":
        raise RuntimeError("Unsupported native MCP target environment key")
    fingerprint_map = cast(dict[str, object], policy)
    inputs = fingerprint_map.get("inputs")
    if fingerprint_map.get("policy") != "als-mcp-source-v2" or not isinstance(inputs, list) or not inputs:
        raise RuntimeError("Unsupported native MCP fingerprint policy")
    for value in cast(list[object], inputs):
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise RuntimeError("Invalid native MCP fingerprint input")
    return config


def source_context(env: dict[str, str]) -> tuple[Path, Path, Path, str]:
    config = build_config()
    package = Path(__file__).absolute().parent
    relative = str(config["manifest"])
    manifest = package.parent / relative
    if not manifest.is_file():
        manifest = package / relative
    if not manifest.is_file():
        raise RuntimeError("No buildable native MCP source installed; run ALS bootstrap preparation")
    cache_config = cast(dict[str, object], config["cache"])
    cache = Path(env.get("ALS_RS_CACHE_DIR") or artifacts.default_cache_dir()).expanduser().absolute() / str(cache_config["root"])
    published = cache / str(cache_config["publishedDirectory"])
    digest = hashlib.sha256(b"als-mcp-selection-v1\0")
    digest.update(str(manifest.absolute()).encode())
    digest.update(json.dumps(python_abi(), sort_keys=True).encode())
    identity = digest.hexdigest()
    source = hashlib.sha256(json.dumps(config, sort_keys=True).encode())
    policy = cast(dict[str, object], config["fingerprint"])
    paths: set[Path] = set()
    for pattern in cast(list[str], policy["inputs"]):
        matches = [Path(path) for path in glob.glob(str(manifest.parent / pattern), recursive=True)]
        if not matches:
            raise RuntimeError(f"Native MCP fingerprint input missing: {pattern}")
        paths.update(path for path in matches if path.is_file())
    for path in sorted(paths):
        source.update(path.relative_to(manifest.parent).as_posix().encode())
        source.update(path.read_bytes())
    return manifest, cache / f"selection-{identity}.json", published, source.hexdigest()


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


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


def fingerprint(manifest: Path, source_digest: str) -> str:
    digest = hashlib.sha256(b"als-mcp-source-v2\0")
    digest.update(json.dumps(build_config(), sort_keys=True).encode())
    digest.update(source_digest.encode())
    digest.update(json.dumps(python_abi(), sort_keys=True).encode())
    digest.update(str(manifest.absolute()).encode())
    digest.update(subprocess.check_output(["rustc", "-vV"]))
    for key in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CC", "CFLAGS", "LDFLAGS"):
        digest.update(f"{key}={os.environ.get(key, '')}\0".encode())
    library = Path(str(cast(object, sysconfig.get_config_var("LIBDIR")))) / str(cast(object, sysconfig.get_config_var("LDLIBRARY")))
    if not library.is_file():
        raise RuntimeError(f"Invoking Python has no discoverable shared library: {library}")
    digest.update(artifacts.sha256_file(library).encode())
    return digest.hexdigest()


def explicit_artifact(env: dict[str, str]) -> Path | None:
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
    return None


def select(env: dict[str, str]) -> Path:
    explicit = explicit_artifact(env)
    if explicit is not None:
        return explicit
    _, selector, published, source_digest = source_context(env)
    if not selector.is_file():
        raise RuntimeError("Native MCP is not prepared; run als-rs --build-only before starting MCP")
    raw = cast(object, json.loads(selector.read_text()))
    if not isinstance(raw, dict):
        raise RuntimeError("Native MCP selector must be an object")
    selected = cast(dict[str, object], raw)
    if selected.get("schema") != 1 or selected.get("python") != python_abi() or selected.get("source") != source_digest:
        raise RuntimeError("Native MCP selection is stale/incompatible; run als-rs --build-only")
    raw_binary = selected.get("binary")
    key = selected.get("fingerprint")
    if not isinstance(raw_binary, str) or not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise RuntimeError("Invalid native MCP selector artifact")
    binary = Path(raw_binary)
    if binary != published / key / BINARY:
        raise RuntimeError("Native MCP selector points outside its managed artifact root")
    validate(binary, binary.with_name(RECEIPT), fingerprint=key)
    return binary


def prepare(env: dict[str, str]) -> Path:
    explicit = explicit_artifact(env)
    if explicit is not None:
        return explicit
    manifest, selector, published, source_digest = source_context(env)
    key = fingerprint(manifest, source_digest)
    cache = selector.parent
    binary = published / key / BINARY
    with artifacts.exclusive_build_cache_lock(cache):
        if binary.exists() or binary.with_name(RECEIPT).exists():
            validate(binary, binary.with_name(RECEIPT), fingerprint=key)
            atomic_json(selector, {"schema": 1, "python": python_abi(), "source": source_digest,
                                   "fingerprint": key, "binary": str(binary)})
            return binary
        build_env = dict(env)
        for name in list(build_env):
            if name.startswith("PYO3_"):
                del build_env[name]
        build_env["PYO3_PYTHON"] = sys.executable
        cache_config = cast(dict[str, object], build_config()["cache"])
        override = build_env.get(str(cache_config["targetDirectoryEnvironment"]))
        inherited_target = build_env.get("CARGO_TARGET_DIR")
        target = Path(override) if override else artifacts.bootstrap_cargo_target_dir(manifest, build_env)
        if not override and not inherited_target:
            target = target.parent.parent / str(cache_config["targetDirectory"]) / target.name
        build_env["CARGO_TARGET_DIR"] = str(target)
        print("[als-mcp] Preparing native MCP executable", file=sys.stderr, flush=True)
        command = ["cargo", "build", "--locked", "--manifest-path", str(manifest), "--bin", BINARY, "--release"]
        features = cast(list[str], build_config()["features"])
        if features:
            command.extend(["--features", ",".join(features)])
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
        if source_context(env)[3] != source_digest:
            raise RuntimeError("Native MCP source/config changed during preparation; rerun ALS bootstrap")
        built = target / "release" / BINARY
        artifacts.publish_cached_binary(built, binary)
        atomic_json(binary.with_name(RECEIPT), {"schema": 1, "python": python_abi(), "fingerprint": key,
                                               "sha256": artifacts.sha256_file(binary)})
        atomic_json(selector, {"schema": 1, "python": python_abi(), "source": source_digest,
                               "fingerprint": key, "binary": str(binary)})
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
