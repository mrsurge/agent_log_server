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
    return _optional_string(context.get("effective")) or _optional_string(merged.get("developer_instructions"))
