from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import TypeAlias, cast

import msgspec

JsonMap: TypeAlias = dict[str, object]

_JSON_ENCODER = msgspec.json.Encoder()
_JSON_DECODER = msgspec.json.Decoder()


class AdapterCodecError(ValueError):
    """Raised when an adapter JSON-RPC payload cannot be encoded or decoded."""


class AdapterDecodeError(AdapterCodecError):
    pass


class AdapterEncodeError(AdapterCodecError):
    pass


def _runtime_type_name(value: object) -> str:
    return type(value).__name__


def decode_json_line(line: bytes | bytearray | memoryview | str) -> object:
    if isinstance(line, str):
        raw = line.encode("utf-8")
    elif isinstance(line, bytes):
        raw = line
    else:
        raw = bytes(line)
    try:
        return cast(object, _JSON_DECODER.decode(raw))
    except msgspec.DecodeError as exc:
        raise AdapterDecodeError(str(exc)) from exc


def encode_json_line(payload: object) -> bytes:
    try:
        encoded = _JSON_ENCODER.encode(to_json_compatible(payload))
    except (TypeError, msgspec.EncodeError) as exc:
        raise AdapterEncodeError(str(exc)) from exc
    return encoded + b"\n"


def to_json_compatible(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return cast(object, value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json_compatible(to_json())

    if is_dataclass(value) and not isinstance(value, type):
        return to_json_compatible(asdict(value))

    if isinstance(value, Mapping):
        value_map = cast(Mapping[object, object], value)
        return {
            str(key): to_json_compatible(item)
            for key, item in value_map.items()
        }

    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        value_sequence = cast(Sequence[object], value)
        return [to_json_compatible(item) for item in value_sequence]

    raise AdapterEncodeError(
        f"Object of type {_runtime_type_name(cast(object, value))} is not JSON serializable"
    )
