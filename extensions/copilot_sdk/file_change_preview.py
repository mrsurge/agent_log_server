"""Shared preview helpers for file-changing Copilot tools."""

from typing import Any, Dict
import difflib


def _normalized_text(value: Any) -> str:
    text = str(value)
    if not text.endswith("\n"):
        text += "\n"
    return text


def build_file_change_preview(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return preview metadata for file-changing tool arguments.

    Supports edit-style replacements (`old_str`/`new_str`) and create/write-style
    full-file writes (`file_text`/`content`).
    """
    if not isinstance(args, dict):
        return {}

    preview: Dict[str, Any] = {}
    file_path = args.get("path") or args.get("file_path") or args.get("file") or ""

    old_str = args.get("old_str")
    if old_str is None:
        old_str = args.get("oldString") or args.get("old_text") or args.get("oldText")

    new_str = args.get("new_str")
    if new_str is None:
        new_str = args.get("newString") or args.get("new_text") or args.get("newText")

    file_text = args.get("file_text") or args.get("content") or args.get("new_content") or args.get("fileText")
    command = args.get("command") or args.get("cmd")

    if old_str is not None and new_str is not None:
        old_lines = _normalized_text(old_str).splitlines(keepends=True)
        new_lines = _normalized_text(new_str).splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=file_path or "a",
            tofile=file_path or "b",
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
            tofile=file_path,
        )
        preview["diff"] = "".join(diff)
        preview["path"] = file_path
        return preview

    if command and file_path:
        preview["path"] = file_path

    return preview
