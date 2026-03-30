from __future__ import annotations

import os
import secrets
from pathlib import Path


_CACHE_DIR = Path(os.path.expanduser("~/.cache/app_server"))
_IPC_SECRET_PATH = _CACHE_DIR / "ipc_secret"


def app_server_cache_dir() -> Path:
    return _CACHE_DIR


def ipc_secret_path() -> Path:
    return _IPC_SECRET_PATH


def load_or_create_ipc_secret() -> str:
    path = ipc_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    secret = secrets.token_hex(32)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return secret


__all__ = ["app_server_cache_dir", "ipc_secret_path", "load_or_create_ipc_secret"]
