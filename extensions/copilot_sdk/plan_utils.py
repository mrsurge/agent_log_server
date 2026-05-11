import re
from typing import Dict, List


_CHECKBOX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\[(?P<mark>[ xX~>\-])\]\s+(?P<text>.+?)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<text>.+?)\s*$")


def _status_from_checkbox(mark: str, text: str) -> str:
    normalized_mark = (mark or "").strip().lower()
    if normalized_mark == "x":
        return "completed"
    if normalized_mark in {"~", ">", "-"}:
        return "in_progress"
    lowered = text.strip().lower()
    if "in progress" in lowered or "working" in lowered:
        return "in_progress"
    return "pending"


def parse_plan_steps(plan_content: str) -> List[Dict[str, str]]:
    if not plan_content.strip():
        return []

    steps: List[Dict[str, str]] = []
    in_code_block = False

    for raw_line in plan_content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue

        checkbox_match = _CHECKBOX_RE.match(line)
        if checkbox_match:
            text = checkbox_match.group("text").strip()
            if text:
                steps.append({
                    "step": text,
                    "status": _status_from_checkbox(checkbox_match.group("mark"), text),
                })
            continue

        list_match = _LIST_ITEM_RE.match(line)
        if list_match:
            text = list_match.group("text").strip()
            if text:
                steps.append({
                    "step": text,
                    "status": "pending",
                })

    return steps
