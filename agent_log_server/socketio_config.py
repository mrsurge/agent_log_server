from __future__ import annotations

import os
from typing import Any

import orjson

SOCKETIO_SERIALIZER_ENV = "AGENT_LOG_SOCKETIO_SERIALIZER"


class SocketIoOrjson:
    @staticmethod
    def dumps(obj: Any, *args: Any, **kwargs: Any) -> str:
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")

    @staticmethod
    def loads(data: Any, *args: Any, **kwargs: Any) -> Any:
        return orjson.loads(data)


def socketio_serializer_mode() -> str:
    raw = os.environ.get(SOCKETIO_SERIALIZER_ENV, "json")
    mode = str(raw or "json").strip().lower()
    if mode in {"msgpack", "messagepack"}:
        return "msgpack"
    return "json"


def socketio_server_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"transports": ["websocket"]}
    if socketio_serializer_mode() == "msgpack":
        try:
            import msgpack  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"{SOCKETIO_SERIALIZER_ENV}=msgpack requires the msgpack package"
            ) from exc
        kwargs["serializer"] = "msgpack"
        return kwargs
    kwargs["json"] = SocketIoOrjson
    return kwargs

