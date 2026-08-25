from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Generator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Sequence, cast

APP_ID = "als-rs"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "12459"
SERVER_PACKAGE = "als-server"
PACKAGED_SERVER_MANIFEST_SCHEMA = 1
PACKAGED_SERVER_MANIFEST_NAME = "als-server.manifest.json"

_INCLUDE_LITERAL_PATTERN = re.compile(
    rb'include_(?:str|bytes)!\(\s*"([^"\\]+)"\s*\)',
)

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
    debug: bool
    framework_shells_base_dir: str | None
    framework_shells_secret: str | None
    framework_shells_repo_fingerprint: str | None
    framework_shells_secret_fingerprint: str | None
    framework_shells_fws_socketio_server_pid: str | None
    framework_shells_run_id: str | None


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "extension":
        from agent_log_server_rs.extension_cli import run_extension_cli

        return run_extension_cli(argv[1:])
    args = _parse_args(argv)
    env = _build_env(args)
    command = _server_command(args, env)
    return _run_child(command, env)


def _parse_args(argv: Sequence[str] | None) -> BootstrapArgs:
    parser = argparse.ArgumentParser(
        prog="als-rs",
        usage=(
            "als-rs [options]\n"
            "       als-rs extension {validate,install,update,remove,reload,list} ..."
        ),
        description=(
            "Launch the ALS-RS Rust server with isolated runtime roots.\n\n"
            "Subcommands:\n"
            "  extension   Manage extension packages. Run `als-rs extension -h` for details."
        ),
        epilog=(
            "Examples:\n"
            "  als-rs\n"
            "  als-rs --debug\n"
            "  als-rs --port 12459\n"
            "  als-rs extension list\n"
            "  als-rs extension install --path /path/to/extension --install-dependencies\n"
            "  als-rs extension validate --git https://example.invalid/repo.git --ref main"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        help=(
            "Path to an installed als-server binary. Defaults to the fingerprinted Cargo "
            "release cache; use --debug for the debug profile."
        ),
    )
    parser.add_argument(
        "--cargo-manifest",
        default=os.environ.get("ALS_RS_CARGO_MANIFEST"),
        help="Path to rust/Cargo.toml for development launches.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build and run the Rust server with Cargo's debug profile (release is the default).",
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
        debug=cast(bool, raw.debug),
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
    static_dir = Path(args.static_dir) if args.static_dir else _default_static_dir()

    for root in (data_dir, cache_dir, config_dir):
        root.mkdir(parents=True, exist_ok=True)

    env["ALS_RS_HOST"] = str(args.host)
    env["ALS_RS_PORT"] = str(args.port)
    env["ALS_RS_DATA_DIR"] = str(data_dir)
    env["ALS_RS_CACHE_DIR"] = str(cache_dir)
    env["ALS_RS_CONFIG_DIR"] = str(config_dir)
    env["ALS_RS_STATIC_DIR"] = str(static_dir)
    env.setdefault("ALS_RS_EXTENSIONS_DIR", str(_default_extensions_dir()))
    template_path = _default_developer_message_template_path()
    if template_path.is_file():
        env.setdefault("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH", str(template_path))
    env["ALS_RS_PYTHON_BIN"] = sys.executable
    _ensure_framework_shells_env(env, args, data_dir)
    return env


def build_env(args: BootstrapArgs) -> dict[str, str]:
    return _build_env(args)


def _server_command(args: BootstrapArgs, env: MutableMapping[str, str] | None = None) -> list[str]:
    runtime_env = env if env is not None else os.environ
    framework_shell_args = _framework_shell_args(args)
    if args.server_bin:
        return [str(Path(args.server_bin)), *framework_shell_args]

    if not args.cargo_manifest:
        packaged_binary = _packaged_server_binary()
        if packaged_binary is not None:
            if args.debug:
                raise RuntimeError(
                    "--debug requires an ALS-RS source checkout; the installed binary-release "
                    "wheel contains only the verified release server"
                )
            return [str(packaged_binary), *framework_shell_args]

    manifest = Path(args.cargo_manifest) if args.cargo_manifest else _default_rust_manifest()
    target_dir = _bootstrap_cargo_target_dir(manifest, runtime_env)
    cargo_profile = "debug" if args.debug else "release"
    fingerprint = _rust_source_fingerprint(manifest, profile=cargo_profile)
    cache_dir = Path(runtime_env.get("ALS_RS_CACHE_DIR") or args.cache_dir or _default_cache_dir())
    binary_name = _server_binary_name()
    bin_root = cache_dir / "bin"
    cached_binary = bin_root / fingerprint / cargo_profile / binary_name

    with _exclusive_build_cache_lock(cache_dir):
        if _cached_binary_is_usable(cached_binary):
            _prune_final_binary_cache(bin_root, cached_binary)
            return [str(cached_binary), *framework_shell_args]

        command = [
            "cargo",
            "build",
            "--manifest-path",
            str(manifest),
            "-p",
            SERVER_PACKAGE,
        ]
        if not args.debug:
            command.append("--release")
        build_env = dict(runtime_env)
        build_env["CARGO_TARGET_DIR"] = str(target_dir)
        result = subprocess.run(command, env=build_env, check=False)
        if result.returncode != 0:
            raise SystemExit(result.returncode)

        built_binary = target_dir / cargo_profile / binary_name
        if not _cached_binary_is_usable(built_binary):
            raise SystemExit(
                f"Rust server build finished but binary is missing or unusable: {built_binary}"
            )
        _publish_cached_binary(built_binary, cached_binary)
        _prune_final_binary_cache(bin_root, cached_binary)

    return [str(cached_binary), *framework_shell_args]


def _packaged_server_binary() -> Path | None:
    binary_dir = _package_root() / "bin"
    manifest_path = binary_dir / PACKAGED_SERVER_MANIFEST_NAME
    binary_path = binary_dir / _server_binary_name()
    if not manifest_path.is_file():
        if binary_path.exists():
            raise RuntimeError(
                f"ALS-RS binary-release payload is incomplete: missing {manifest_path}"
            )
        return None

    try:
        loaded = cast(object, json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ALS-RS binary-release manifest is unreadable: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"ALS-RS binary-release manifest must be an object: {manifest_path}")
    raw = cast(dict[str, object], loaded)

    schema = raw.get("schema")
    package_version = raw.get("package_version")
    binary_name = raw.get("binary")
    target = raw.get("target")
    platform_tag = raw.get("platform_tag")
    source_commit = raw.get("source_commit")
    source_dirty = raw.get("source_dirty")
    expected_digest = raw.get("sha256")
    if schema != PACKAGED_SERVER_MANIFEST_SCHEMA:
        raise RuntimeError(f"Unsupported ALS-RS binary-release manifest schema: {schema!r}")
    if not isinstance(package_version, str) or not package_version:
        raise RuntimeError("ALS-RS binary-release manifest has no package_version")
    if binary_name != _server_binary_name():
        raise RuntimeError(f"ALS-RS binary-release manifest has invalid binary: {binary_name!r}")
    if not isinstance(target, str) or not target:
        raise RuntimeError("ALS-RS binary-release manifest has no target")
    if not isinstance(platform_tag, str) or not platform_tag:
        raise RuntimeError("ALS-RS binary-release manifest has no platform_tag")
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", source_commit):
        raise RuntimeError("ALS-RS binary-release manifest has invalid source_commit")
    if not isinstance(source_dirty, bool):
        raise RuntimeError("ALS-RS binary-release manifest has invalid source_dirty")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RuntimeError("ALS-RS binary-release manifest has invalid sha256")

    installed_version = _installed_package_version()
    if installed_version is not None and package_version != installed_version:
        raise RuntimeError(
            "ALS-RS binary-release package version mismatch: "
            f"manifest={package_version}, installed={installed_version}"
        )
    _validate_packaged_target(target, platform_tag)
    if not _cached_binary_is_usable(binary_path):
        raise RuntimeError(f"ALS-RS packaged server is missing or unusable: {binary_path}")
    actual_digest = _sha256_file(binary_path)
    if actual_digest != expected_digest:
        raise RuntimeError(
            "ALS-RS packaged server digest mismatch: "
            f"expected={expected_digest}, actual={actual_digest}"
        )
    return binary_path


def _installed_package_version() -> str | None:
    try:
        return importlib.metadata.version("agent-log-server")
    except importlib.metadata.PackageNotFoundError:
        return None


def _validate_packaged_target(target: str, platform_tag: str) -> None:
    machine = platform.machine().strip().lower().replace("amd64", "x86_64")
    machine = machine.replace("arm64", "aarch64")
    sys_platform = sys.platform.lower()
    sysconfig_platform = sysconfig.get_platform().lower()
    multiarch = str(sysconfig.get_config_var("MULTIARCH") or "").lower()
    is_android = (
        "android" in sysconfig_platform
        or "android" in multiarch
        or bool(os.environ.get("ANDROID_ROOT"))
    )

    if target == "x86_64-unknown-linux-gnu":
        compatible = sys_platform.startswith("linux") and machine == "x86_64" and not is_android
    elif target == "aarch64-linux-android":
        compatible = sys_platform.startswith("linux") and machine == "aarch64" and is_android
    else:
        raise RuntimeError(f"Unsupported ALS-RS packaged server target: {target}")
    if not compatible:
        raise RuntimeError(
            "ALS-RS packaged server is incompatible with this runtime: "
            f"target={target}, platform_tag={platform_tag}, "
            f"runtime={sys_platform}/{machine}, android={is_android}"
        )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@contextmanager
def _exclusive_build_cache_lock(cache_dir: Path) -> Generator[None, None, None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / ".build.lock"
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _cached_binary_is_usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0 and os.access(path, os.X_OK)
    except OSError:
        return False


def _publish_cached_binary(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        temporary.chmod(temporary.stat().st_mode | 0o755)
        with temporary.open("rb") as file_handle:
            os.fsync(file_handle.fileno())
        if not _cached_binary_is_usable(temporary):
            raise RuntimeError(f"published Rust server binary is unusable: {temporary}")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _prune_final_binary_cache(bin_root: Path, selected_binary: Path) -> None:
    if not _cached_binary_is_usable(selected_binary):
        raise RuntimeError(
            f"refusing to prune before the selected binary validates: {selected_binary}"
        )

    selected_profile_dir = selected_binary.parent
    selected_fingerprint_dir = selected_profile_dir.parent
    if selected_fingerprint_dir.parent != bin_root:
        raise RuntimeError(
            f"selected binary is outside the final binary cache: {selected_binary}"
        )

    for fingerprint_entry in tuple(bin_root.iterdir()):
        if fingerprint_entry != selected_fingerprint_dir:
            _remove_cache_entry(fingerprint_entry)
    for profile_entry in tuple(selected_fingerprint_dir.iterdir()):
        if profile_entry != selected_profile_dir:
            _remove_cache_entry(profile_entry)
    for binary_entry in tuple(selected_profile_dir.iterdir()):
        if binary_entry != selected_binary:
            _remove_cache_entry(binary_entry)
    _fsync_directory(bin_root)


def _remove_cache_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _server_binary_name() -> str:
    suffix = ".exe" if sys.platform == "win32" else ""
    return f"{SERVER_PACKAGE}{suffix}"


def _rust_source_fingerprint(manifest: Path, *, profile: str) -> str:
    workspace = manifest.parent
    hasher = hashlib.sha256()
    hasher.update(b"als-rs-server-build-cache-v1\0")
    hasher.update(str(workspace).encode("utf-8", "surrogateescape"))
    hasher.update(b"\0")
    hasher.update(profile.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(sys.platform.encode("utf-8"))
    hasher.update(b"\0")
    uname_result = os.uname() if hasattr(os, "uname") else None
    hasher.update(uname_result.machine.encode("utf-8") if uname_result is not None else b"")
    for path in _rust_fingerprint_paths(workspace):
        relative = path.relative_to(workspace).as_posix()
        hasher.update(b"\0path:")
        hasher.update(relative.encode("utf-8", "surrogateescape"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
    return hasher.hexdigest()[:24]


def _rust_fingerprint_paths(workspace: Path) -> list[Path]:
    candidates: set[Path] = set()
    for relative in ("Cargo.toml", "Cargo.lock"):
        path = workspace / relative
        if path.is_file():
            candidates.add(path)

    crates_root = workspace / "crates"
    if not crates_root.is_dir():
        return sorted(candidates)

    rust_sources: list[Path] = []
    for path in crates_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in {"Cargo.toml", "build.rs"} or path.suffix == ".rs":
            candidates.add(path)
        if path.suffix == ".rs":
            rust_sources.append(path)

    for source in rust_sources:
        source_bytes = source.read_bytes()
        for match in _INCLUDE_LITERAL_PATTERN.finditer(source_bytes):
            included = source.parent / os.fsdecode(match.group(1))
            if included.is_file():
                candidates.add(included)
    return sorted(candidates)


def _bootstrap_cargo_target_dir(manifest: Path, runtime_env: MutableMapping[str, str]) -> Path:
    configured_target_dir = runtime_env.get("CARGO_TARGET_DIR")
    if configured_target_dir:
        return Path(configured_target_dir)

    cache_dir = Path(runtime_env.get("ALS_RS_CACHE_DIR") or _default_cache_dir())
    runtime_env.setdefault("ALS_RS_CACHE_DIR", str(cache_dir))
    manifest_key = hashlib.sha256(str(manifest.parent).encode("utf-8")).hexdigest()[:16]
    target_dir = cache_dir / "cargo-target" / manifest_key
    runtime_env["CARGO_TARGET_DIR"] = str(target_dir)
    return target_dir


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


def _set_if_present(env: dict[str, str], key: str, value: str | None) -> None:
    if value:
        env[key] = value


def _ensure_framework_shells_env(
    env: dict[str, str],
    args: BootstrapArgs,
    data_dir: Path,
) -> None:
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
    env.setdefault("FRAMEWORK_SHELLS_RUN_ID", "app-server")
    env.setdefault("FRAMEWORK_SHELLS_SIGWINCH_ON_RESIZE", "1")

    if env.get("FRAMEWORK_SHELLS_SECRET"):
        _prime_framework_shells_import(env)
        return

    fingerprint = env.get("FRAMEWORK_SHELLS_REPO_FINGERPRINT")
    if not fingerprint:
        fingerprint = _framework_shells_fingerprint(data_dir)
        env["FRAMEWORK_SHELLS_REPO_FINGERPRINT"] = fingerprint
    env.setdefault("FRAMEWORK_SHELLS_SECRET_FINGERPRINT", fingerprint)

    base_dir = Path(
        env.get("FRAMEWORK_SHELLS_BASE_DIR") or _default_framework_shells_base_dir()
    )
    secret_dir = base_dir / "runtimes" / fingerprint
    secret_file = secret_dir / "secret"
    if secret_file.is_file():
        secret = secret_file.read_text(encoding="utf-8").strip()
    else:
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        secret_file.write_text(secret, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass

    env["FRAMEWORK_SHELLS_BASE_DIR"] = str(base_dir)
    env["FRAMEWORK_SHELLS_SECRET"] = secret
    _prime_framework_shells_import(env)


def _framework_shells_fingerprint(data_dir: Path) -> str:
    root = _cwd_for_fingerprint() or data_dir
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _cwd_for_fingerprint() -> Path | None:
    pwd = os.environ.get("PWD")
    if pwd:
        path = Path(pwd)
        if path.is_dir():
            return path
    try:
        return Path.cwd()
    except OSError:
        return None


def _default_framework_shells_base_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "framework_shells"


def _prime_framework_shells_import(env: dict[str, str]) -> None:
    try:
        framework_shells = importlib.import_module("framework_shells")
    except ImportError:
        return
    get_secret = getattr(framework_shells, "get_secret", None)
    if not callable(get_secret):
        return

    framework_env = {
        key: value for key, value in env.items() if key.startswith("FRAMEWORK_SHELLS_")
    }
    previous = {key: os.environ.get(key) for key in framework_env}
    try:
        os.environ.update(framework_env)
        get_secret()
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


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


def _default_extensions_dir() -> Path:
    extensions_dir = _source_root() / "extensions"
    if (extensions_dir / "extensions.json").is_file():
        return extensions_dir
    return extensions_dir


def _default_static_dir() -> Path:
    source_static = _source_root() / "rust" / "crates" / "als-server" / "src" / "static"
    if source_static.is_dir():
        return source_static
    packaged_static = _package_root() / "rust" / "crates" / "als-server" / "src" / "static"
    return packaged_static


def _default_developer_message_template_path() -> Path:
    packaged_template = _package_root() / "DEVELOPER_MESSAGE_TEMPLATE.md"
    if packaged_template.is_file():
        return packaged_template
    source_template = _source_root() / "DEVELOPER_MESSAGE_TEMPLATE.md"
    if source_template.is_file():
        return source_template
    return packaged_template


def _source_root() -> Path:
    return Path(__file__).parent.parent


def source_root() -> Path:
    return _source_root()


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
