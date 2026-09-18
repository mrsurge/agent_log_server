from __future__ import annotations

from typing import Dict, cast

ObjectDict = Dict[str, object]

DEVINS_CONTEXT_SETTINGS_KEY = "__als_devins_context__"


def _optional_map(value: object) -> ObjectDict:
    return cast(ObjectDict, value).copy() if isinstance(value, dict) else {}


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def effective_developer_instructions(settings: object) -> str | None:
    merged = _optional_map(settings)
    context = _optional_map(merged.get(DEVINS_CONTEXT_SETTINGS_KEY))
    instructions = _optional_string(context.get("effective")) or _optional_string(merged.get("developer_instructions"))
    config = _optional_map(merged.get("config"))
    if config.get("include_collaboration_mode_instructions") is True:
        return instructions

    mode = _optional_string(merged.get("mode")) or "default"
    if mode.lower() == "plan":
        mode_instructions = (
            "Current collaboration mode: Plan. Investigate and develop a decision-complete "
            "plan using non-mutating inspection. Do not implement changes, even when the "
            "user asks to proceed, until the configured mode changes to Default. "
            "Present the completed plan in a <proposed_plan> block."
        )
    else:
        mode_instructions = (
            "Current collaboration mode: Default. Carry out the user's task, honoring "
            "repository approval requirements and explicit scope limits."
        )
    input_instructions = (
        "Use the native request_user_input tool when available for clarification, "
        "required information, design choices, and workflow approval gates. Otherwise "
        "use MCP ask_user when available, then plain text. Await the actual response "
        "before dependent work; missing, cancelled, or rejected answers are not approval. "
        "Do not parallelize a question with work dependent on its answer. These workflow "
        "approvals do not replace execution-permission or sandbox enforcement."
    )
    policy = f"## ALS collaboration and user input\n\n{mode_instructions}\n\n{input_instructions}"
    return f"{instructions}\n\n{policy}" if instructions else policy
