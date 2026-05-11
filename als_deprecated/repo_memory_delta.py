from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from .markdown_sections import SectionNode, parse_markdown


class SectionEntry(TypedDict):
    label: str
    title: str
    body: str
    line_start: int


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _extract_line_range(lines: list[str], start: int, end: int) -> str:
    start_idx = max(start - 1, 0)
    end_idx = max(end, 0)
    return "\n".join(lines[start_idx:end_idx])


def _section_label(node: SectionNode) -> str:
    if node.id_disambiguated != node.id:
        return node.id_disambiguated
    return node.id


def _build_section_map(text: str) -> dict[str, SectionEntry]:
    lines = (text or "").splitlines()
    sections: dict[str, SectionEntry] = {}
    for node in parse_markdown(text):
        body = _extract_line_range(lines, node.body_start, node.body_end).strip()
        if not body:
            continue
        label = _section_label(node)
        sections[label] = {
            "label": label,
            "title": node.title,
            "body": body,
            "line_start": node.line_start,
        }
    return sections


def _append_text_block(out: list[str], label: str, text: str) -> None:
    out.append(f"  - {label}:")
    stripped = (text or "").strip()
    if not stripped:
        out.append("    (empty)")
        return
    for line in stripped.splitlines():
        out.append(f"    {line}")


def build_repo_memory_delta(
    previous_text: str,
    current_text: str,
    *,
    source_path: str | None = None,
    ts: float | None = None,
) -> str | None:
    old_sections = _build_section_map(previous_text)
    new_sections = _build_section_map(current_text)
    if not old_sections and not new_sections:
        return None

    added_keys = sorted(set(new_sections) - set(old_sections))
    removed_keys = sorted(set(old_sections) - set(new_sections))
    replaced_keys = sorted(
        key
        for key in (set(old_sections) & set(new_sections))
        if old_sections[key]["body"] != new_sections[key]["body"]
    )
    if not added_keys and not removed_keys and not replaced_keys:
        return None

    old_hash = _content_hash(previous_text)
    new_hash = _content_hash(current_text)
    timestamp = datetime.fromtimestamp(ts, timezone.utc).isoformat() if isinstance(ts, (int, float)) else None
    rendered_path = Path(source_path).name if isinstance(source_path, str) and source_path.strip() else ".repo_memory.md"

    out = [
        f"## Repo Memory Update v{new_hash}",
        "",
        "Apply only the changes below. Newer repo-memory updates supersede older repo-memory entries with the same heading. Entries listed under Removed are no longer valid.",
        "",
        f"- Source: `{rendered_path}`",
        f"- Hash: `{old_hash} -> {new_hash}`",
    ]
    if timestamp:
        out.append(f"- Timestamp: `{timestamp}`")

    if added_keys:
        out.extend(["", "### Added"])
        for key in added_keys:
            entry = new_sections[key]
            out.append(f"- `{entry['label']}`")
            _append_text_block(out, "New content", entry["body"])

    if replaced_keys:
        out.extend(["", "### Replaced"])
        for key in replaced_keys:
            old_entry = old_sections[key]
            new_entry = new_sections[key]
            out.append(f"- `{new_entry['label']}`")
            _append_text_block(out, "Previous content", old_entry["body"])
            _append_text_block(out, "New content", new_entry["body"])

    if removed_keys:
        out.extend(["", "### Removed"])
        for key in removed_keys:
            entry = old_sections[key]
            out.append(f"- `{entry['label']}`")
            _append_text_block(out, "Previous content", entry["body"])
            out.append("  - Status: Was removed from memory")

    return "\n".join(out).strip()


__all__ = ["build_repo_memory_delta"]
