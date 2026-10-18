"""Materialize explicit CPython/dependency inputs without following symlinks."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

from agent_log_server_rs.mcp_bootstrap import python_abi
from agent_log_server_rs.private_runtime import digest


def _copy(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise RuntimeError(f"Private Python input contains a symlink: {source}")
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if item.name in {"__pycache__", "site-packages", "bin", ".lock"} or item.suffix in {".a", ".pyc", ".pyo"}:
                continue
            if item.suffix == ".pth" or item.name in {"sitecustomize.py", "usercustomize.py", "pyvenv.cfg"}:
                raise RuntimeError(f"Executable site customization in private input: {item}")
            _copy(item, target / item.name)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise RuntimeError(f"Private Python input is not regular: {source}")


def materialize(destination: Path, dependencies: Path, mcp: Path) -> None:
    if sys.version_info[:2] != (3, 14) or sys.platform != "linux" or sysconfig.get_config_var("Py_GIL_DISABLED"):
        raise RuntimeError("Bundled Linux ALS runtime requires ordinary CPython 3.14")
    prefix = Path(sys.base_prefix)
    python = destination / "python"
    _copy(prefix / "bin/python3.14", python / "bin/python3.14")
    _copy(prefix / "lib/libpython3.14.so.1.0", python / "lib/libpython3.14.so.1.0")
    _copy(prefix / "lib/python3.14", python / "lib/python3.14")
    _copy(dependencies, python / "lib/python3.14/site-packages")
    # python-build-standalone's PIE is already stripped/relocatable. Do not run
    # strip after patchelf: that combination corrupts its dynamic version data.
    executable = python / "bin/python3.14"
    rpath = subprocess.check_output(["patchelf", "--print-rpath", str(executable)], text=True).strip()
    if rpath != "$ORIGIN/../lib":
        subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/../lib", str(executable)], check=True)
    subprocess.run(["strip", "--strip-debug", str(mcp)], check=True)
    subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/../private_runtime/python/lib", str(mcp)], check=True)
    refresh(destination)


def refresh(root: Path) -> None:
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"schema": 1, "python": python_abi(), "files": {
        str(path.relative_to(root)): digest(path)
        for path in sorted(root.rglob("*")) if path.is_file() and path != manifest
    }}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
