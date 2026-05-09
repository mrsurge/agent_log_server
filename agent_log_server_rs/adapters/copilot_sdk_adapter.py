from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO, TypeAlias, cast

from agent_log_server_rs.adapter_protocol import (
    AdapterCapabilities,
    AdapterEventMethod,
    AdapterMethod,
    AdapterModelInfo,
    ConversationAckResult,
    ExtensionInitializeResult,
    JsonMap,
)
from agent_log_server_rs.codec import (
    AdapterDecodeError,
    decode_json_line,
    encode_json_line,
)

RpcId: TypeAlias = str | int | None

JSONRPC_VERSION = "2.0"
COPILOT_EXTENSION_ID = "copilot-sdk"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class CopilotClientModule(Protocol):
    def init_copilot_manager(
        self,
        extensions_dir: Path,
        server_root: Path,
        fws_getter: object,
        broadcast_fn: Callable[[JsonMap], Awaitable[None]],
        transcript_fn: Callable[[str, JsonMap], Awaitable[None]],
        meta_fns: dict[str, Callable[..., object]] | None = None,
    ) -> None: ...

    async def list_models(self) -> list[JsonMap]: ...

    async def init_session(
        self,
        conversation_id: str,
        extension_id: str,
        cwd: str | None,
        settings: JsonMap | None = None,
    ) -> JsonMap: ...

    async def handle_message(
        self,
        conversation_id: str,
        text: str,
        agent_type: str,
        settings: JsonMap,
    ) -> JsonMap: ...

    async def stop_client(self) -> None: ...


class RpcAdapterError(Exception):
    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class AdapterState:
    extension_id: str = COPILOT_EXTENSION_ID
    cwd: Path = field(default_factory=Path.cwd)
    data_dir: Path = field(default_factory=Path.cwd)
    cache_dir: Path = field(default_factory=Path.cwd)
    config_dir: Path = field(default_factory=Path.cwd)
    settings: JsonMap = field(default_factory=dict)
    meta: dict[str, JsonMap] = field(default_factory=dict)


class CopilotSdkJsonRpcAdapter:
    def __init__(self, client: CopilotClientModule | None = None) -> None:
        self._client = client or cast(
            CopilotClientModule,
            importlib.import_module("extensions.copilot_sdk.client"),
        )
        self._state = AdapterState()
        self._manager_initialized = False
        self._stdout: TextIO | None = None
        self._write_lock = asyncio.Lock()

    async def run_stdio(
        self,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
    ) -> int:
        self._stdout = stdout
        while True:
            line = await asyncio.to_thread(stdin.readline)
            if line == "":
                return 0
            line = line.strip()
            if not line:
                continue
            await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        try:
            raw = decode_json_line(line)
        except AdapterDecodeError as exc:
            await self._send_error(None, PARSE_ERROR, "Parse error", str(exc))
            return

        if not isinstance(raw, dict):
            await self._send_error(None, INVALID_REQUEST, "Invalid request")
            return

        message = cast(JsonMap, raw)
        request_id = _rpc_id(message.get("id"))
        try:
            result = await self._dispatch(message)
        except RpcAdapterError as exc:
            if "id" in message:
                await self._send_error(request_id, exc.code, exc.message, exc.data)
            return
        except Exception as exc:
            if "id" in message:
                await self._send_error(request_id, INTERNAL_ERROR, str(exc))
            return

        if "id" in message:
            await self._send_success(request_id, result)

    async def _dispatch(self, message: JsonMap) -> object:
        if message.get("jsonrpc") != JSONRPC_VERSION:
            raise RpcAdapterError(INVALID_REQUEST, "Invalid JSON-RPC version")
        method = _string(message.get("method"))
        params = _params_map(message.get("params"))

        if method == AdapterMethod.EXTENSION_INITIALIZE:
            return await self._initialize(params)
        if method == AdapterMethod.EXTENSION_SHUTDOWN:
            await self._client.stop_client()
            return {"ok": True}
        if method == AdapterMethod.EXTENSION_LIST_MODELS:
            return await self._list_models()
        if method == AdapterMethod.CONVERSATION_START:
            return await self._conversation_start(params)
        if method == AdapterMethod.CONVERSATION_SEND:
            return await self._conversation_send(params)

        raise RpcAdapterError(METHOD_NOT_FOUND, f"Unsupported method: {method}")

    async def _initialize(self, params: JsonMap) -> JsonMap:
        extension_id = _optional_string(params.get("extension_id")) or COPILOT_EXTENSION_ID
        cwd = _optional_path(params.get("cwd")) or Path.cwd()
        data_dir = _optional_path(params.get("data_dir")) or cwd
        cache_dir = _optional_path(params.get("cache_dir")) or data_dir
        config_dir = _optional_path(params.get("config_dir")) or data_dir
        settings = _optional_map(params.get("settings")) or {}

        self._state = AdapterState(
            extension_id=extension_id,
            cwd=cwd,
            data_dir=data_dir,
            cache_dir=cache_dir,
            config_dir=config_dir,
            settings=settings,
            meta=self._state.meta,
        )
        self._ensure_manager_initialized()
        return ExtensionInitializeResult(
            extension_id=extension_id,
            provider="copilot-sdk",
            capabilities=AdapterCapabilities(
                conversations=True,
                models=True,
                live_events=True,
                transcript_records=True,
            ),
        ).to_json()

    async def _list_models(self) -> JsonMap:
        self._ensure_manager_initialized()
        models = await self._client.list_models()
        return {
            "models": [
                _adapter_model(model).to_json()
                for model in models
                if isinstance(model.get("id"), str)
            ]
        }

    async def _conversation_start(self, params: JsonMap) -> JsonMap:
        self._ensure_manager_initialized()
        conversation_id = _required_string(params, "conversation_id")
        cwd = _optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = _merged_settings(self._state.settings, _optional_map(params.get("settings")))
        settings["cwd"] = cwd

        result = await self._client.init_session(
            conversation_id,
            self._state.extension_id,
            cwd,
            settings=settings,
        )
        return _ack_from_result(conversation_id, result).to_json()

    async def _conversation_send(self, params: JsonMap) -> JsonMap:
        self._ensure_manager_initialized()
        conversation_id = _required_string(params, "conversation_id")
        text = _required_string(params, "text")
        cwd = _optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = _merged_settings(self._state.settings, _optional_map(params.get("settings")))
        settings["cwd"] = cwd

        result = await self._client.handle_message(
            conversation_id,
            text,
            self._state.extension_id,
            settings,
        )
        return _ack_from_result(conversation_id, result).to_json()

    def _ensure_manager_initialized(self) -> None:
        if self._manager_initialized:
            return
        self._client.init_copilot_manager(
            extensions_dir=self._state.data_dir / "extensions",
            server_root=self._state.data_dir,
            fws_getter=None,
            broadcast_fn=self._broadcast,
            transcript_fn=self._transcript,
            meta_fns={"load": self._load_meta, "save": self._save_meta},
        )
        self._manager_initialized = True

    async def _broadcast(self, payload: JsonMap) -> None:
        await self._send_notification(AdapterEventMethod.LIVE_EVENT, payload)

    async def _transcript(self, conversation_id: str, entry: JsonMap) -> None:
        payload = dict(entry)
        payload.setdefault("conversation_id", conversation_id)
        await self._send_notification(AdapterEventMethod.TRANSCRIPT_RECORD, payload)

    def _load_meta(self, conversation_id: str) -> JsonMap:
        return self._state.meta.setdefault(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "agent_type": self._state.extension_id,
                "settings": dict(self._state.settings),
            },
        )

    def _save_meta(self, conversation_id: str, meta: JsonMap) -> None:
        self._state.meta[conversation_id] = dict(meta)

    async def _send_notification(self, method: str, params: object) -> None:
        await self._write_json({"jsonrpc": JSONRPC_VERSION, "method": method, "params": params})

    async def _send_success(self, request_id: RpcId, result: object) -> None:
        await self._write_json(
            {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
        )

    async def _send_error(
        self,
        request_id: RpcId,
        code: int,
        message: str,
        data: object | None = None,
    ) -> None:
        error: JsonMap = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        await self._write_json({"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error})

    async def _write_json(self, payload: JsonMap) -> None:
        if self._stdout is None:
            return
        encoded = encode_json_line(payload)
        async with self._write_lock:
            self._stdout.write(encoded)
            self._stdout.flush()


def _adapter_model(model: JsonMap) -> AdapterModelInfo:
    model_id = _required_string(model, "id")
    name = _optional_string(model.get("name"))
    capabilities = _optional_map(model.get("capabilities")) or {}
    efforts = _string_list(model.get("supported_reasoning_efforts"))
    return AdapterModelInfo(
        id=model_id,
        name=name,
        context_window=_context_window(model, capabilities),
        supported_reasoning_efforts=efforts,
        capabilities=capabilities,
        raw=dict(model),
    )


def _context_window(model: JsonMap, capabilities: JsonMap) -> int | None:
    limits = _optional_map(capabilities.get("limits")) or {}
    for candidate in (
        limits.get("max_context_window_tokens"),
        limits.get("maxContextWindowTokens"),
        model.get("context_window"),
        model.get("max_context_window_tokens"),
        model.get("maxContextWindowTokens"),
    ):
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    return None


def _ack_from_result(conversation_id: str, result: JsonMap) -> ConversationAckResult:
    ok = result.get("ok") is True or result.get("accepted") is True
    provider_session_id = (
        _optional_string(result.get("provider_session_id"))
        or _optional_string(result.get("thread_id"))
        or _optional_string(result.get("session_id"))
    )
    if provider_session_id == conversation_id:
        provider_session_id = None
    if not ok:
        provider_session_id = None
    return ConversationAckResult(
        conversation_id=conversation_id,
        accepted=ok,
        provider_session_id=provider_session_id,
        provider_call_id=_optional_string(result.get("provider_call_id")),
        turn_id=_optional_string(result.get("turn_id")),
        restore_draft=result.get("restore_draft") is True,
        metadata=dict(result),
    )


def _merged_settings(base: JsonMap, override: JsonMap | None) -> JsonMap:
    merged = dict(base)
    if override:
        merged.update(override)
    return merged


def _params_map(params: object) -> JsonMap:
    if params is None:
        return {}
    mapping = _optional_map(params)
    if mapping is None:
        raise RpcAdapterError(INVALID_PARAMS, "Params must be an object")
    return mapping


def _optional_map(value: object) -> JsonMap | None:
    if isinstance(value, dict):
        return cast(JsonMap, value)
    return None


def _required_string(mapping: JsonMap, key: str) -> str:
    value = _optional_string(mapping.get(key))
    if value is None:
        raise RpcAdapterError(INVALID_PARAMS, f"Missing string param: {key}")
    return value


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _string(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    raise RpcAdapterError(INVALID_REQUEST, "Missing method")


def _optional_path(value: object) -> Path | None:
    text = _optional_string(value)
    return Path(text) if text else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _rpc_id(value: object) -> RpcId:
    if value is None or isinstance(value, (str, int)):
        return value
    raise RpcAdapterError(INVALID_REQUEST, "Invalid request id")


async def amain() -> int:
    adapter = CopilotSdkJsonRpcAdapter()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        return await adapter.run_stdio(sys.stdin, protocol_stdout)
    finally:
        await adapter._client.stop_client()


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("copilot-sdk adapter does not accept CLI arguments yet")
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
