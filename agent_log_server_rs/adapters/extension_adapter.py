from __future__ import annotations

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO, TypeAlias, cast

from agent_log_server_rs.adapter_protocol import (
    AdapterCapabilities,
    AdapterEventMethod,
    AdapterMethod,
    ExtensionInitializeResult,
    JsonMap,
)
from agent_log_server_rs.adapters.copilot_sdk_adapter import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcAdapterError,
    _ack_from_result,
    _adapter_model,
    _merged_settings,
    _optional_map,
    _optional_path,
    _optional_string,
    _params_map,
    _required_string,
    _rpc_id,
    _string_list,
    _string,
)

RpcId: TypeAlias = str | int | None

SUPPORTED_RUNTIME_TYPES = {"copilot_sdk"}


class ExtensionLoaderModule(Protocol):
    def load_extensions(
        self,
        extensions_dir: Path | list[Path],
        server_root: Path,
        fws_getter: Callable[..., object] | None,
        broadcast_fn: Callable[[JsonMap], Awaitable[None]],
        transcript_fn: Callable[[str, JsonMap], Awaitable[None]],
        meta_fns: dict[str, Callable[..., object]] | None = None,
    ) -> None: ...

    def get_extension_info(self, extension_id: str) -> JsonMap | None: ...

    def get_handler(self, extension_id: str) -> object | None: ...

    def list_extensions(self) -> list[JsonMap]: ...

    def reload_extensions(
        self,
        changed_extension_ids: list[str] | None = None,
        *,
        force: bool = False,
    ) -> list[JsonMap]: ...

    def get_static_settings_schema(self, extension_id: str) -> JsonMap | None: ...

    async def list_models(self, extension_id: str) -> object: ...

    async def get_settings_schema(self, extension_id: str) -> JsonMap | None: ...

    async def get_splash_schema(self, extension_id: str) -> JsonMap | None: ...


@dataclass
class AdapterState:
    extension_id: str = "copilot-sdk"
    cwd: Path = field(default_factory=Path.cwd)
    data_dir: Path = field(default_factory=Path.cwd)
    cache_dir: Path = field(default_factory=Path.cwd)
    config_dir: Path = field(default_factory=Path.cwd)
    extensions_dir: Path = field(default_factory=lambda: Path.cwd() / "extensions")
    settings: JsonMap = field(default_factory=dict)
    meta: dict[str, JsonMap] = field(default_factory=dict)


class ExtensionJsonRpcAdapter:
    def __init__(self, loader: ExtensionLoaderModule | None = None) -> None:
        self._loader = loader or cast(
            ExtensionLoaderModule,
            importlib.import_module("extensions"),
        )
        self._state = AdapterState()
        self._loader_initialized = False
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
            raw = cast(object, json.loads(line))
        except json.JSONDecodeError as exc:
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
            await self._stop_supported_handlers()
            return {"ok": True}
        if method == AdapterMethod.EXTENSION_RELOAD:
            return self._reload(params)
        if method == AdapterMethod.EXTENSION_GET_SETTINGS_SCHEMA:
            return await self._settings_schema(params)
        if method == AdapterMethod.EXTENSION_GET_SPLASH_SCHEMA:
            return await self._splash_schema(params)
        if method == AdapterMethod.EXTENSION_LIST_MODELS:
            return await self._list_models(params)
        if method == AdapterMethod.CONVERSATION_START:
            return await self._conversation_start(params)
        if method == AdapterMethod.CONVERSATION_SEND:
            return await self._conversation_send(params)

        raise RpcAdapterError(METHOD_NOT_FOUND, f"Unsupported method: {method}")

    async def _initialize(self, params: JsonMap) -> JsonMap:
        extension_id = _optional_string(params.get("extension_id")) or self._state.extension_id
        cwd = _optional_path(params.get("cwd")) or Path.cwd()
        data_dir = _optional_path(params.get("data_dir")) or cwd
        cache_dir = _optional_path(params.get("cache_dir")) or data_dir
        config_dir = _optional_path(params.get("config_dir")) or data_dir
        extensions_dir = _optional_path(params.get("extensions_dir")) or self._state.extensions_dir
        settings = _optional_map(params.get("settings")) or {}

        self._state = AdapterState(
            extension_id=extension_id,
            cwd=cwd,
            data_dir=data_dir,
            cache_dir=cache_dir,
            config_dir=config_dir,
            extensions_dir=extensions_dir,
            settings=settings,
            meta=self._state.meta,
        )
        self._ensure_loader_initialized()
        info = self._extension_info(extension_id)
        ext_type = _optional_string(info.get("type")) or ""
        supported = ext_type in SUPPORTED_RUNTIME_TYPES
        return ExtensionInitializeResult(
            extension_id=extension_id,
            provider=ext_type or extension_id,
            capabilities=AdapterCapabilities(
                conversations=supported,
                models=supported,
                live_events=supported,
                transcript_records=supported,
                extra={
                    "supported": supported,
                    "type": ext_type,
                    "manifest": info.get("manifest", {}),
                },
            ),
        ).to_json()

    def _reload(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        force = params.get("force") is True
        changed_extension_ids = _string_list(params.get("changed_extension_ids"))
        extensions = self._loader.reload_extensions(changed_extension_ids, force=force)
        return {"ok": True, "extensions": list(extensions)}

    async def _list_models(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        result = await self._loader.list_models(extension_id)
        if isinstance(result, dict):
            raw_models = result.get("models")
            models = raw_models if isinstance(raw_models, list) else []
        else:
            models = result if isinstance(result, list) else []
        return {
            "models": [
                _adapter_model(cast(JsonMap, model)).to_json()
                for model in models
                if isinstance(model, dict) and isinstance(model.get("id"), str)
            ]
        }

    async def _settings_schema(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        schema = await self._loader.get_settings_schema(extension_id)
        if not isinstance(schema, dict) or not schema:
            schema = self._loader.get_static_settings_schema(extension_id)
        if isinstance(schema, dict) and schema:
            return dict(schema)
        return {"version": "1", "fields": []}

    async def _splash_schema(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        schema = await self._loader.get_splash_schema(extension_id)
        if isinstance(schema, dict) and schema:
            result = dict(schema)
            result.setdefault("extension_id", extension_id)
            return result
        return {"version": "1", "extension_id": extension_id, "fields": []}

    async def _conversation_start(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        init_session = getattr(handler, "init_session", None)
        if not callable(init_session):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support session start")

        conversation_id = _required_string(params, "conversation_id")
        cwd = _optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = _merged_settings(self._state.settings, _optional_map(params.get("settings")))
        settings["cwd"] = cwd
        result = init_session(conversation_id, extension_id, cwd, settings=settings)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid session start result")
        return _ack_from_result(conversation_id, cast(JsonMap, result)).to_json()

    async def _conversation_send(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        handle_message = getattr(handler, "handle_message", None)
        if not callable(handle_message):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support conversation send")

        conversation_id = _required_string(params, "conversation_id")
        text = _required_string(params, "text")
        cwd = _optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = _merged_settings(self._state.settings, _optional_map(params.get("settings")))
        settings["cwd"] = cwd
        result = handle_message(conversation_id, text, extension_id, settings)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid send result")
        return _ack_from_result(conversation_id, cast(JsonMap, result)).to_json()

    def _ensure_loader_initialized(self) -> None:
        if self._loader_initialized:
            return
        self._loader.load_extensions(
            self._state.extensions_dir,
            self._state.extensions_dir.parent,
            None,
            self._broadcast,
            self._transcript,
            meta_fns={"load": self._load_meta, "save": self._save_meta},
        )
        self._loader_initialized = True

    def _extension_id_param(self, params: JsonMap) -> str:
        return _optional_string(params.get("extension_id")) or self._state.extension_id

    def _extension_info(self, extension_id: str) -> JsonMap:
        self._ensure_loader_initialized()
        info = self._loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            raise RpcAdapterError(INVALID_PARAMS, f"Unknown extension: {extension_id}")
        return cast(JsonMap, info)

    def _supported_handler(self, extension_id: str) -> object:
        info = self._extension_info(extension_id)
        ext_type = _optional_string(info.get("type")) or ""
        if ext_type not in SUPPORTED_RUNTIME_TYPES:
            raise RpcAdapterError(
                METHOD_NOT_FOUND,
                f"Extension {extension_id} has type {ext_type or '<unknown>'}; ALS-RS adapter support is not wired yet",
            )
        if info.get("active") is not True:
            raise RpcAdapterError(INVALID_PARAMS, f"Extension is not active: {extension_id}")
        handler = self._loader.get_handler(extension_id)
        if handler is None:
            raise RpcAdapterError(INTERNAL_ERROR, f"No handler loaded for extension: {extension_id}")
        return handler

    async def _stop_supported_handlers(self) -> None:
        for extension in self._loader.list_extensions():
            if not isinstance(extension, dict):
                continue
            extension_id = _optional_string(extension.get("id"))
            ext_type = _optional_string(extension.get("type"))
            if not extension_id or ext_type not in SUPPORTED_RUNTIME_TYPES:
                continue
            handler = self._loader.get_handler(extension_id)
            stop_client = getattr(handler, "stop_client", None) if handler is not None else None
            if callable(stop_client):
                result = stop_client()
                if hasattr(result, "__await__"):
                    await cast(Awaitable[object], result)

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
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        async with self._write_lock:
            self._stdout.write(encoded + "\n")
            self._stdout.flush()


async def amain() -> int:
    adapter = ExtensionJsonRpcAdapter()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        return await adapter.run_stdio(sys.stdin, protocol_stdout)
    finally:
        await adapter._stop_supported_handlers()


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("extension adapter does not accept CLI arguments yet")
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
