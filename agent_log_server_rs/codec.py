from __future__ import annotations

import importlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, TypeAlias, cast

import msgspec

JsonMap: TypeAlias = dict[str, object]

_JSON_ENCODER = msgspec.json.Encoder()
_JSON_DECODER = msgspec.json.Decoder()
_MSGPACK_ENCODER = msgspec.msgpack.Encoder()
_MSGPACK_DECODER = msgspec.msgpack.Decoder()
MAX_FRAME_BYTES = 32 * 1024 * 1024
ADAPTER_CODEC_ENV = "ALS_RS_ADAPTER_CODEC"


class AdapterCodecError(ValueError):
    """Raised when an adapter envelope cannot be encoded or decoded."""


class AdapterDecodeError(AdapterCodecError):
    pass


class AdapterEncodeError(AdapterCodecError):
    pass


class _Unpacker(Protocol):
    def feed(self, data: bytes) -> None: ...
    def skip(self) -> None: ...
    def tell(self) -> int: ...


_msgpack = importlib.import_module("msgpack")
_make_unpacker = cast(Callable[..., _Unpacker], _msgpack.Unpacker)
_out_of_data = cast(type[Exception], _msgpack.OutOfData)


def adapter_codec() -> str:
    codec = os.environ.get(ADAPTER_CODEC_ENV, "messagepack")
    if codec not in ("messagepack", "json"):
        raise AdapterCodecError(f"Invalid {ADAPTER_CODEC_ENV}: {codec}")
    return codec


def encode_frame(payload: object, codec: str) -> bytes:
    if codec == "json":
        encoded = encode_json_line(payload)
        size = len(encoded) - 1
    else:
        encoded = _MSGPACK_ENCODER.encode(to_json_compatible(payload))
        size = len(encoded)
    if size > MAX_FRAME_BYTES:
        raise AdapterEncodeError("adapter frame exceeds byte limit")
    return encoded


class FrameDecoder:
    """Frame concatenated objects without treating binary newlines as delimiters."""

    def __init__(self, codec: str) -> None:
        self.codec = codec
        self.pending = bytearray()
        self._unpacker = _make_unpacker(max_buffer_size=MAX_FRAME_BYTES + 65536)
        self._consumed = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for start in range(0, len(chunk), 65536):
            part = chunk[start:start + 65536]
            self.pending.extend(part)
            if self.codec == "json":
                while (end := self.pending.find(b"\n")) >= 0:
                    if end > MAX_FRAME_BYTES:
                        raise AdapterDecodeError("adapter frame exceeds byte limit")
                    frame = bytes(self.pending[:end])
                    del self.pending[:end + 1]
                    if frame.strip():
                        frames.append(frame)
            else:
                self._unpacker.feed(part)
                while self.pending:
                    if not (0x80 <= self.pending[0] <= 0x8f or self.pending[0] in (0xde, 0xdf)):
                        raise AdapterDecodeError("expected adapter envelope map")
                    try:
                        self._unpacker.skip()
                    except _out_of_data:
                        break
                    # tell() may advance on partial input; only commit a boundary
                    # after skip() succeeds, then decode the exact original bytes.
                    size = self._unpacker.tell() - self._consumed
                    if size > MAX_FRAME_BYTES:
                        raise AdapterDecodeError("adapter frame exceeds byte limit")
                    frames.append(bytes(self.pending[:size]))
                    del self.pending[:size]
                    self._consumed += size
            if len(self.pending) > MAX_FRAME_BYTES:
                raise AdapterDecodeError("adapter frame exceeds byte limit")
        return frames

    def finish(self) -> None:
        if self.pending:
            raise AdapterDecodeError("truncated adapter frame at EOF")


def decode_frame(frame: bytes | str, codec: str) -> object:
    if codec == "json":
        return decode_json_line(frame)
    if isinstance(frame, str):
        raise AdapterDecodeError("MessagePack requires binary input")
    try:
        return cast(object, _MSGPACK_DECODER.decode(frame))
    except msgspec.DecodeError as exc:
        raise AdapterDecodeError(str(exc)) from exc


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
