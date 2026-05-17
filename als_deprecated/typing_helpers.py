from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

ObjectMap: TypeAlias = dict[str, object]
ObjectList: TypeAlias = list[ObjectMap]
AsyncObjectCallable: TypeAlias = Callable[..., Awaitable[object]]
ObjectEntriesWriter: TypeAlias = Callable[[str, list[ObjectMap]], Awaitable[object]]
RequestId: TypeAlias = str | int | None


def coerce_object_map(value: object) -> ObjectMap:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def coerce_object_list(value: object) -> ObjectList:
    if not isinstance(value, list):
        return []
    items: ObjectList = []
    for entry in value:
        if isinstance(entry, dict):
            items.append(coerce_object_map(entry))
    return items
