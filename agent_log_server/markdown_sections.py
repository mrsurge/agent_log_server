from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, TypedDict


@dataclass
class SectionNode:
    id: str
    id_disambiguated: str
    depth: int
    title: str
    line_start: int
    body_start: int
    body_end: int
    subtree_end: int


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MD_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_UNICODE_NORMALIZE_MAP = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
)


class _HeadingEntry(TypedDict):
    id: str
    depth: int
    title: str
    normalized_title: str
    line_start: int


def normalize_heading(text: str) -> str:
    normalized = (text or "").translate(_UNICODE_NORMALIZE_MAP)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def parse_markdown(text: str) -> list[SectionNode]:
    lines = (text or "").splitlines()
    headings: list[_HeadingEntry] = []
    stack: list[_HeadingEntry] = []
    in_fence = False

    for idx, line in enumerate(lines, start=1):
        fence_match = _MD_FENCE_RE.match(line)
        if fence_match:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _MD_HEADING_RE.match(line)
        if not match:
            continue
        depth = len(match.group(1))
        title = match.group(2)
        normalized_title = normalize_heading(title)

        while stack and stack[-1]["depth"] >= depth:
            stack.pop()
        path_parts = [node["normalized_title"] for node in stack] + [normalized_title]
        section_id = " > ".join(path_parts)

        heading_entry: _HeadingEntry = {
            "id": section_id,
            "depth": depth,
            "title": title,
            "normalized_title": normalized_title,
            "line_start": idx,
        }
        headings.append(heading_entry)
        stack.append(heading_entry)

    if not headings:
        return []

    total_lines = len(lines)
    nodes: list[SectionNode] = []
    for i, heading in enumerate(headings):
        depth = heading["depth"]
        line_start = heading["line_start"]
        body_start = min(line_start + 1, total_lines + 1)

        subtree_end = total_lines
        for j in range(i + 1, len(headings)):
            nxt = headings[j]
            if nxt["depth"] <= depth:
                subtree_end = nxt["line_start"] - 1
                break

        first_child_start: Optional[int] = None
        for j in range(i + 1, len(headings)):
            nxt = headings[j]
            nxt_depth = nxt["depth"]
            nxt_start = nxt["line_start"]
            if nxt_depth <= depth:
                break
            if nxt_depth == depth + 1:
                first_child_start = nxt_start
                break

        if first_child_start is not None:
            body_end = first_child_start - 1
        else:
            body_end = subtree_end

        nodes.append(
            SectionNode(
                id=heading["id"],
                id_disambiguated=heading["id"],
                depth=depth,
                title=heading["title"],
                line_start=line_start,
                body_start=body_start,
                body_end=body_end,
                subtree_end=subtree_end,
            )
        )

    counts: dict[str, int] = {}
    for section_node in nodes:
        counts[section_node.id] = counts.get(section_node.id, 0) + 1
    for section_node in nodes:
        if counts.get(section_node.id, 0) > 1:
            section_node.id_disambiguated = f"{section_node.id}@L{section_node.line_start}"

    return nodes


__all__ = ["SectionNode", "normalize_heading", "parse_markdown"]
