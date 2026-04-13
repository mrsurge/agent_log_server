import json
import re
from typing import Optional


def normalize_plan_status(status: object) -> str:
    if not isinstance(status, str):
        return "pending"
    normalized = re.sub(r"[^a-z0-9]+", "", status.strip().lower())
    if normalized == "completed":
        return "completed"
    if normalized == "inprogress":
        return "in_progress"
    return "pending"


def normalize_plan_steps(plan: object) -> list[dict[str, str]]:
    if not isinstance(plan, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        if not isinstance(step, str) or not step.strip():
            continue
        normalized.append(
            {
                "step": step.strip(),
                "status": normalize_plan_status(item.get("status")),
            }
        )
    return normalized


def plan_signature(steps: list[dict[str, str]], explanation: Optional[str]) -> str:
    payload = {
        "steps": steps,
        "explanation": explanation or "",
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def render_plan_markdown(steps: list[dict[str, str]], explanation: Optional[str] = None) -> str:
    if not steps and not explanation:
        return ""

    lines: list[str] = ["# Plan", ""]
    if isinstance(explanation, str) and explanation.strip():
        lines.append(explanation.strip())
        lines.append("")

    for item in steps:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        if not isinstance(step, str) or not step.strip():
            continue
        status = normalize_plan_status(item.get("status"))
        if status == "completed":
            prefix = "- [x]"
            suffix = ""
        elif status == "in_progress":
            prefix = "- [ ]"
            suffix = " _(in progress)_"
        else:
            prefix = "- [ ]"
            suffix = ""
        lines.append(f"{prefix} {step.strip()}{suffix}")

    return "\n".join(lines).strip()
