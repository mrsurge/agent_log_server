from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

REPO_MEMORY_FILENAME = ".repo_memory.md"
_DEFAULT_TEMPLATE_PATH = Path(os.path.abspath(__file__)).parent.parent / "DEVELOPER_MESSAGE_TEMPLATE.md"


class RepoMemorySnapshot(TypedDict):
    cwd: str | None
    repo_root: str | None
    path: str | None
    exists: bool
    truncated: bool
    content: str


def _template_path(template_path: str | None = None) -> Path:
    raw = template_path
    if not raw:
        raw = os.environ.get("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH")
    if isinstance(raw, str) and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return _DEFAULT_TEMPLATE_PATH


@lru_cache(maxsize=8)
def load_te2_developer_message_template(template_path: str | None = None) -> str:
    path = _template_path(template_path)
    if not path.exists():
        raise FileNotFoundError(f"TE2 developer message template not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"TE2 developer message template is empty: {path}")
    return text


def _logical_cwd_path(cwd: object) -> Path | None:
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    path = Path(os.path.abspath(os.path.expanduser(cwd.strip())))
    if path.is_file():
        return path.parent
    return path


def _detect_repo_memory_root(start: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        root = result.stdout.strip()
        if root:
            return Path(root)
    except Exception:
        pass

    cur = start
    while True:
        if (cur / ".agent-pty.toml").exists() or (cur / REPO_MEMORY_FILENAME).exists():
            return cur
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return start


def load_repo_memory_snapshot(
    cwd: object,
    *,
    max_chars: int = 0,
) -> RepoMemorySnapshot:
    logical_cwd = _logical_cwd_path(cwd)
    snapshot: RepoMemorySnapshot = {
        "cwd": str(logical_cwd) if isinstance(logical_cwd, Path) else None,
        "repo_root": None,
        "path": None,
        "exists": False,
        "truncated": False,
        "content": "",
    }
    if logical_cwd is None:
        return snapshot

    repo_root = _detect_repo_memory_root(logical_cwd)
    memory_path = repo_root / REPO_MEMORY_FILENAME
    snapshot["repo_root"] = str(repo_root)
    snapshot["path"] = str(memory_path)
    if not memory_path.exists() or not memory_path.is_file():
        return snapshot

    text = memory_path.read_text(encoding="utf-8").strip()
    snapshot["exists"] = True
    if not text:
        return snapshot
    if max_chars > 0 and len(text) > max_chars:
        snapshot["content"] = text[:max_chars].rstrip()
        snapshot["truncated"] = True
        return snapshot
    snapshot["content"] = text
    return snapshot


def build_effective_developer_instructions(
    user_instructions: object,
    *,
    te2_enabled: bool,
    template_path: str | None = None,
) -> str | None:
    user_text = user_instructions.strip() if isinstance(user_instructions, str) else ""
    if not te2_enabled:
        return user_text or None
    template = load_te2_developer_message_template(template_path)
    if not user_text:
        return template
    if template in user_text:
        return user_text
    return f"{template}\n\n{user_text}"


def build_effective_prompt_context(
    user_instructions: object,
    *,
    te2_enabled: bool,
    cwd: object = None,
    template_path: str | None = None,
) -> str | None:
    developer_text = build_effective_developer_instructions(
        user_instructions,
        te2_enabled=te2_enabled,
        template_path=template_path,
    )
    snapshot = load_repo_memory_snapshot(cwd)
    repo_memory_text = snapshot["content"].strip()
    if not repo_memory_text:
        return developer_text
    if not developer_text:
        return repo_memory_text
    if repo_memory_text in developer_text:
        return developer_text
    return f"{developer_text}\n\n{repo_memory_text}"


__all__ = [
    "REPO_MEMORY_FILENAME",
    "build_effective_developer_instructions",
    "build_effective_prompt_context",
    "load_repo_memory_snapshot",
    "load_te2_developer_message_template",
]
