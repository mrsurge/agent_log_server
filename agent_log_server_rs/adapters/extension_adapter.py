from __future__ import annotations

import asyncio
import importlib
import json
import os
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

SUPPORTED_RUNTIME_TYPES = {"copilot_sdk", "codex_ext", "codex_ext_exp"}
SUPPORTED_MODEL_TYPES = SUPPORTED_RUNTIME_TYPES


async def _get_framework_shell_manager() -> object:
    module = importlib.import_module("framework_shells")
    get_manager = getattr(module, "get_manager", None)
    if not callable(get_manager):
        raise RuntimeError("framework_shells.get_manager is unavailable")
    result = get_manager(run_id=os.environ.get("FRAMEWORK_SHELLS_RUN_ID", "app-server"))
    if hasattr(result, "__await__"):
        return await cast(Awaitable[object], result)
    return result


async def _close_framework_shell_peer() -> None:
    try:
        module = cast(object, importlib.import_module("framework_shells.fws_socketio_peer"))
        relay = cast(object | None, getattr(module, "_peer_relay", None))
        if relay is None:
            return
        tasks = [
            task
            for task in (
                getattr(relay, "_connect_task", None),
                getattr(relay, "_relay_task", None),
            )
            if isinstance(task, asyncio.Task)
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client = cast(object | None, getattr(relay, "client", None))
        disconnect = getattr(client, "disconnect", None)
        if callable(disconnect):
            result = disconnect()
            if hasattr(result, "__await__"):
                await cast(Awaitable[object], result)
    except Exception as exc:
        print(f"[ExtensionAdapter] framework-shell peer cleanup failed: {exc}", file=sys.stderr)


def _framework_shell_env_probe() -> JsonMap:
    keys = [
        "FRAMEWORK_SHELLS_BASE_DIR",
        "FRAMEWORK_SHELLS_SECRET",
        "FRAMEWORK_SHELLS_REPO_FINGERPRINT",
        "FRAMEWORK_SHELLS_SECRET_FINGERPRINT",
        "FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID",
        "FRAMEWORK_SHELLS_RUN_ID",
    ]
    snapshot: JsonMap = {}
    for key in keys:
        value = os.environ.get(key)
        if key == "FRAMEWORK_SHELLS_SECRET":
            snapshot[key] = {"present": bool(value), "length": len(value or "")}
        else:
            snapshot[key] = {"present": bool(value), "value": value or None}
    return snapshot


async def _framework_shell_manager_probe(ensure_manager: bool) -> JsonMap:
    probe: JsonMap = {
        "ensure_manager": ensure_manager,
        "module_importable": False,
        "get_manager_callable": False,
        "singleton_created": False,
    }
    try:
        module = importlib.import_module("framework_shells")
    except ImportError as exc:
        probe["error"] = f"framework_shells import failed: {exc}"
        return probe
    probe["module_importable"] = True
    get_manager = getattr(module, "get_manager", None)
    probe["get_manager_callable"] = callable(get_manager)

    shared_manager = importlib.import_module("framework_shells.shared_manager")
    probe["singleton_created"] = getattr(shared_manager, "_manager_instance", None) is not None
    if not ensure_manager:
        return probe

    if not callable(get_manager):
        probe["error"] = "framework_shells.get_manager is unavailable"
        return probe

    try:
        manager = await _get_framework_shell_manager()
        probe["singleton_created"] = True
        probe["manager_class"] = type(manager).__name__
        _put_store_probe(probe, manager)
        await _put_shells_probe(probe, manager)
    except Exception as exc:
        probe["error"] = f"{type(exc).__name__}: {exc}"
    return probe


def _put_store_probe(probe: JsonMap, manager: object) -> None:
    store: object | None = getattr(manager, "store", None)
    if store is None:
        probe["store_present"] = False
        return
    probe["store_present"] = True
    probe["store"] = {
        "runtime_id": str(getattr(store, "runtime_id", "")),
        "root": str(getattr(store, "root", "")),
        "metadata_dir": str(getattr(store, "metadata_dir", "")),
        "logs_dir": str(getattr(store, "logs_dir", "")),
    }


async def _put_shells_probe(probe: JsonMap, manager: object) -> None:
    list_shells = getattr(manager, "list_shells", None)
    if not callable(list_shells):
        probe["list_shells_callable"] = False
        return
    probe["list_shells_callable"] = True
    result = list_shells()
    if hasattr(result, "__await__"):
        result = await cast(Awaitable[object], result)
    shells = result if isinstance(result, list) else []
    probe["shell_count"] = len(shells)
    probe["shells"] = [_shell_probe(shell) for shell in shells[-12:]]


def _shell_probe(shell: object) -> JsonMap:
    return {
        "id": str(getattr(shell, "id", "")),
        "spec_id": str(getattr(shell, "spec_id", "")),
        "label": str(getattr(shell, "label", "")),
        "status": str(getattr(shell, "status", "")),
        "backend": str(getattr(shell, "backend", "")),
        "pid": getattr(shell, "pid", None),
        "app_id": str(getattr(shell, "app_id", "")),
    }


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

    def set_extension_enabled(self, extension_id: str, enabled: bool) -> bool: ...

    def supports_dependency_check(self, extension_id: str) -> bool: ...

    def supports_dependency_install(self, extension_id: str) -> bool: ...

    async def check_extension_dependencies(self, extension_id: str) -> JsonMap: ...

    async def install_extension_dependencies(self, extension_id: str) -> JsonMap: ...

    def set_extension_dependency_result(
        self,
        extension_id: str,
        result: JsonMap | None,
    ) -> bool: ...

    async def wait_extension_ready(self, extension_id: str, timeout: float = 60.0) -> bool: ...

    async def warm_up_extensions(self, timeout: float = 60.0) -> dict[str, bool]: ...

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
            return await self._reload(params)
        if method == AdapterMethod.EXTENSION_INSTALL_DEPENDENCIES:
            return await self._install_dependencies(params)
        if method == AdapterMethod.EXTENSION_DEBUG_PROBE:
            return await self._debug_probe(params)
        if method == AdapterMethod.EXTENSION_WARM_UP:
            return await self._warm_up(params)
        if method == AdapterMethod.EXTENSION_GET_SETTINGS_SCHEMA:
            return await self._settings_schema(params)
        if method == AdapterMethod.EXTENSION_GET_SPLASH_SCHEMA:
            return await self._splash_schema(params)
        if method == AdapterMethod.EXTENSION_LIST_MODELS:
            return await self._list_models(params)
        if method == AdapterMethod.CONVERSATION_START:
            return await self._conversation_start(params)
        if method == AdapterMethod.CONVERSATION_RESUME:
            return await self._conversation_resume(params)
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

    async def _reload(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        force = params.get("force") is True
        changed_extension_ids = _string_list(params.get("changed_extension_ids"))
        self._loader.reload_extensions(changed_extension_ids, force=force)
        self._apply_enabled_overrides(params.get("enabled_overrides"))
        extensions = await self._refresh_dependency_state()
        wait_ready_extension_id = _optional_string(params.get("wait_ready_extension_id"))
        wait_ready = False
        if wait_ready_extension_id:
            wait_ready = await self._wait_ready_if_active(wait_ready_extension_id)
            extensions = list(self._loader.list_extensions())
        return {"ok": True, "extensions": extensions, "wait_ready": wait_ready}

    async def _warm_up(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        self._apply_enabled_overrides(params.get("enabled_overrides"))
        extensions = await self._refresh_dependency_state()
        timeout = _positive_float(params.get("timeout"), 60.0)
        results = await self._loader.warm_up_extensions(timeout=timeout)
        if not isinstance(results, dict):
            results = {}
        return {
            "ok": True,
            "supported": True,
            "results": {str(key): bool(value) for key, value in results.items()},
            "extensions": list(self._loader.list_extensions()),
        }

    async def _install_dependencies(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        extension_id = self._extension_id_param(params)
        if not self._loader.supports_dependency_install(extension_id):
            raise RpcAdapterError(INVALID_PARAMS, f"{extension_id} does not support dependency install")
        result = await self._loader.install_extension_dependencies(extension_id)
        if result.get("ok"):
            if self._loader.supports_dependency_check(extension_id):
                dependency_result = await self._loader.check_extension_dependencies(extension_id)
            else:
                dependency_result = {
                    "ok": True,
                    "status": "met",
                    "message": "No dependency check required",
                }
            self._loader.set_extension_dependency_result(extension_id, dependency_result)
        else:
            failed_result: JsonMap = {
                "ok": False,
                "status": "error",
                "message": str(result.get("message") or "Dependency install failed"),
            }
            self._loader.set_extension_dependency_result(extension_id, failed_result)
        return {
            "ok": bool(result.get("ok")),
            "result": result,
            "extension": self._loader.get_extension_info(extension_id) or {},
            "extensions": list(self._loader.list_extensions()),
        }

    async def _debug_probe(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        ensure_manager = params.get("ensure_manager") is True
        info: JsonMap | None = None
        handler: object | None = None
        handler_class: str | None = None
        if self._loader_initialized:
            raw_info = self._loader.get_extension_info(extension_id)
            if isinstance(raw_info, dict):
                info = cast(JsonMap, raw_info)
            handler = self._loader.get_handler(extension_id)
            if handler is not None:
                handler_class = type(handler).__name__
        manager_probe = await _framework_shell_manager_probe(ensure_manager)
        return {
            "ok": True,
            "adapter": {
                "pid": os.getpid(),
                "cwd": str(Path.cwd()),
                "state_cwd": str(self._state.cwd),
                "extensions_dir": str(self._state.extensions_dir),
                "loader_initialized": self._loader_initialized,
                "selected_extension_id": self._state.extension_id,
            },
            "extension": {
                "requested_id": extension_id,
                "known": info is not None,
                "type": _optional_string(info.get("type")) if info else None,
                "active": bool(info.get("active")) if info else False,
                "enabled": bool(info.get("enabled")) if info else False,
                "dependency_ok": bool(info.get("dependency_ok")) if info else False,
                "handler_present": handler is not None,
                "handler_class": handler_class,
            },
            "env": _framework_shell_env_probe(),
            "framework_shells": manager_probe,
        }

    def _apply_enabled_overrides(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        for extension_id, enabled in value.items():
            if isinstance(extension_id, str) and isinstance(enabled, bool):
                self._loader.set_extension_enabled(extension_id, enabled)

    async def _refresh_dependency_state(self) -> list[JsonMap]:
        for extension in list(self._loader.list_extensions()):
            if not isinstance(extension, dict):
                continue
            extension_id = _optional_string(extension.get("id"))
            if not extension_id:
                continue
            if self._loader.supports_dependency_check(extension_id):
                result = await self._loader.check_extension_dependencies(extension_id)
            else:
                result = {
                    "ok": True,
                    "status": "met",
                    "message": "No dependency check required",
                }
            self._loader.set_extension_dependency_result(extension_id, result)
        return list(self._loader.list_extensions())

    async def _wait_ready_if_active(self, extension_id: str) -> bool:
        info = self._loader.get_extension_info(extension_id)
        if not isinstance(info, dict) or info.get("active") is not True:
            return False
        return bool(await self._loader.wait_extension_ready(extension_id, timeout=60.0))

    async def _list_models(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        info = self._extension_info(extension_id)
        ext_type = _optional_string(info.get("type")) or ""
        if ext_type not in SUPPORTED_MODEL_TYPES:
            return {
                "models": [],
                "supported": False,
                "reason": f"Extension {extension_id} has type {ext_type or '<unknown>'}; ALS-RS model listing is not wired yet",
            }
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
        settings.setdefault("agent", extension_id)
        result = init_session(conversation_id, extension_id, cwd, settings=settings)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid session start result")
        return _ack_from_result(conversation_id, cast(JsonMap, result)).to_json()

    async def _conversation_resume(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        resume_session_with_history = getattr(handler, "resume_session_with_history", None)
        if not callable(resume_session_with_history):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support session resume")

        conversation_id = _required_string(params, "conversation_id")
        provider_session_id = _provider_session_id_param(params)
        if not provider_session_id:
            raise RpcAdapterError(INVALID_PARAMS, "provider_session_id or thread_id is required")

        cwd = _optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = _merged_settings(self._state.settings, _optional_map(params.get("settings")))
        settings["cwd"] = cwd
        settings.setdefault("agent", extension_id)
        self._seed_conversation_meta(
            conversation_id,
            extension_id=extension_id,
            settings=settings,
            cwd=cwd,
            provider_session_id=provider_session_id,
        )

        result = resume_session_with_history(
            extension_id=extension_id,
            session_id=provider_session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=_optional_string(settings.get("model")),
            settings=settings,
        )
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid session resume result")

        ack = _ack_from_result(conversation_id, cast(JsonMap, result)).to_json()
        if result.get("ok") is not True:
            return ack

        hydrated_count = 0
        hydrate_transcript = getattr(handler, "hydrate_transcript", None)
        if callable(hydrate_transcript):
            try:
                items = hydrate_transcript(
                    session_id=provider_session_id,
                    conversation_id=conversation_id,
                    cwd=cwd,
                    model=_optional_string(settings.get("model")),
                    settings=settings,
                )
                if hasattr(items, "__await__"):
                    items = await cast(Awaitable[object], items)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            await self._transcript(conversation_id, cast(JsonMap, item))
                            hydrated_count += 1
            except Exception as exc:
                print(
                    f"[ExtensionAdapter] {extension_id} hydrate_transcript failed for "
                    f"{conversation_id[:8]}: {exc}",
                    file=sys.stderr,
                )
                ack["hydration_error"] = f"{type(exc).__name__}: {exc}"
        ack["hydrated_count"] = hydrated_count
        return ack

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
        settings.setdefault("agent", extension_id)
        self._seed_conversation_meta(
            conversation_id,
            extension_id=extension_id,
            settings=settings,
            cwd=cwd,
            provider_session_id=_provider_session_id_param(params),
        )
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
            _get_framework_shell_manager,
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
                "settings": {
                    **dict(self._state.settings),
                    "agent": self._state.extension_id,
                },
            },
        )

    def _save_meta(self, conversation_id: str, meta: JsonMap) -> None:
        self._state.meta[conversation_id] = dict(meta)

    def _seed_conversation_meta(
        self,
        conversation_id: str,
        *,
        extension_id: str,
        settings: JsonMap,
        cwd: str,
        provider_session_id: str | None = None,
    ) -> None:
        meta = dict(self._load_meta(conversation_id))
        existing_settings = meta.get("settings")
        merged_settings = dict(existing_settings) if isinstance(existing_settings, dict) else {}
        merged_settings.update(settings)
        if cwd:
            merged_settings["cwd"] = cwd
        merged_settings["agent"] = extension_id
        meta["settings"] = merged_settings
        meta["agent_type"] = extension_id
        meta["extension_id"] = extension_id
        if provider_session_id:
            meta["thread_id"] = provider_session_id
            meta["provider_session_id"] = provider_session_id
            meta["status"] = "active"
        self._save_meta(conversation_id, meta)

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


def _positive_float(value: object, default: float) -> float:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def _provider_session_id_param(params: JsonMap) -> str | None:
    for key in ("provider_session_id", "thread_id", "session_id", "threadId", "sessionId"):
        value = _optional_string(params.get(key))
        if value:
            return value
    return None


async def amain() -> int:
    adapter = ExtensionJsonRpcAdapter()
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        return await adapter.run_stdio(sys.stdin, protocol_stdout)
    finally:
        await adapter._stop_supported_handlers()
        await _close_framework_shell_peer()


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("extension adapter does not accept CLI arguments yet")
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
