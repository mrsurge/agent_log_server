"""Linux packaged MCP runtime; editable/Android installations keep their ABI owner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from typing import cast

DIRECTORY = "private_runtime"
MANIFEST = "manifest.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def metadata(package: Path) -> dict[str, object] | None:
    root = package / DIRECTORY
    if not root.exists():
        return None
    if root.is_symlink():
        raise RuntimeError("Private ALS runtime must not be a symlink")
    manifest = root / MANIFEST
    if manifest.is_symlink():
        raise RuntimeError("Private ALS manifest must not be a symlink")
    raw = cast(object, json.loads(manifest.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise RuntimeError("Invalid private ALS runtime manifest")
    result = cast(dict[str, object], raw)
    if result.get("schema") != 1:
        raise RuntimeError("Invalid private ALS runtime manifest")
    if not isinstance(result.get("python"), dict) or not isinstance(result.get("files"), dict):
        raise RuntimeError("Private ALS runtime inventory/ABI is missing")
    return result


def verify(package: Path) -> dict[str, object] | None:
    data = metadata(package)
    if data is None:
        return None
    files = cast(dict[str, object], data["files"])
    required = {"python/bin/python3.14", "python/lib/libpython3.14.so.1.0", "python/lib/python3.14/encodings/__init__.py"}
    if not required.issubset(files):
        raise RuntimeError("Private ALS runtime inventory is incomplete")
    root = package / DIRECTORY
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise RuntimeError("Invalid private ALS runtime inventory path")
        file = root / relative
        if any(parent.is_symlink() for parent in (file, *file.parents) if parent != package.parent):
            raise RuntimeError("Private ALS runtime symlinks are not supported")
        if not isinstance(expected, str) or digest(file) != expected:
            raise RuntimeError(f"Private ALS runtime checksum mismatch: {name}")
    return data


def expected_abi(binary: Path) -> dict[str, object] | None:
    package = Path(__file__).absolute().parent
    if binary.absolute() != package / "bin/als-mcp":
        return None
    data = metadata(package)
    return cast(dict[str, object], data["python"]) if data is not None else None


def private_environment(package: Path) -> dict[str, str]:
    home = package / DIRECTORY / "python"
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON") and key not in {"VIRTUAL_ENV", "LD_PRELOAD", "LD_LIBRARY_PATH"}}
    env["PYTHONHOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join((str(home / "lib/python3.14/site-packages"), str(package.parent)))
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def enter(package: Path) -> None:
    """Exec private Python once, retaining stdio and application capability env."""
    data = verify(package)
    if data is None:
        return
    home = package / DIRECTORY / "python"
    if sys.prefix == str(home) and sys.version_info[:2] == (3, 14):
        return
    executable = home / "bin/python3.14"
    # -I/-S avoid outer user/site hooks; explicitly expose only private deps and
    # this installed package tree. The native process receives the same paths.
    code = (
        "import sys;sys.path[:0]=sys.argv[1:3];sys.argv=['mcp_agent_pty_server.py'];"
        "from agent_log_server_rs.mcp_bootstrap import main;main()"
    )
    os.execve(str(executable), [str(executable), "-I", "-S", "-B", "-c", code,
        str(home / "lib/python3.14/site-packages"), str(package.parent)], private_environment(package))

