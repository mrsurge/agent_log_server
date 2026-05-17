from __future__ import annotations

from typing import cast

from .te2_runtime import ObjectMap, coerce_object_map

DEVINS_CONTEXT_SETTINGS_KEY = "__als_devins_context__"


def _optional_map(value: object) -> ObjectMap:
    return coerce_object_map(cast(dict[object, object], value)) if isinstance(value, dict) else {}


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
