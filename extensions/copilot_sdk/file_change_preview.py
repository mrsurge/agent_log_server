"""Shared preview helpers for file-changing Copilot tools."""

from __future__ import annotations

import difflib
from typing import TypeAlias

PreviewMap: TypeAlias = dict[str, object]


def _coerce_preview_map(value: object) -> PreviewMap:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _normalized_text(value: object) -> str:
    text = str(value)
    if not text.endswith("\n"):
        text += "\n"
    return text


def build_file_change_preview(args: object) -> PreviewMap:
    """
    Return preview metadata for file-changing tool arguments.

    Supports edit-style replacements (`old_str`/`new_str`) and create/write-style
    full-file writes (`file_text`/`content`).
    """
    args_map = _coerce_preview_map(args)
    if not args_map:
        return {}

    preview: PreviewMap = {}
    file_path = args_map.get("path") or args_map.get("file_path") or args_map.get("file") or ""
    file_label = str(file_path) if file_path else ""

    old_str = args_map.get("old_str")
    if old_str is None:
        old_str = args_map.get("oldString") or args_map.get("old_text") or args_map.get("oldText")

    new_str = args_map.get("new_str")
    if new_str is None:
        new_str = args_map.get("newString") or args_map.get("new_text") or args_map.get("newText")

    file_text = args_map.get("file_text") or args_map.get("content") or args_map.get("new_content") or args_map.get("fileText")
    command = args_map.get("command") or args_map.get("cmd")

    if old_str is not None and new_str is not None:
        old_lines = _normalized_text(old_str).splitlines(keepends=True)
        new_lines = _normalized_text(new_str).splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=file_label or "a",
            tofile=file_label or "b",
        )
        preview["diff"] = "".join(diff)
        if file_path:
            preview["path"] = file_path
        return preview

    if file_text is not None and file_path:
        new_lines = _normalized_text(file_text).splitlines(keepends=True)
        diff = difflib.unified_diff(
            [],
            new_lines,
            fromfile="/dev/null",
            tofile=file_label,
        )
        preview["diff"] = "".join(diff)
        preview["path"] = file_path
        return preview

    if command and file_path:
        preview["path"] = file_path

    return preview
