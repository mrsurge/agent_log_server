from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO, TypeAlias, cast

from agent_log_server_rs.adapter_protocol import (
    AdapterCapabilities,
    AdapterEventMethod,
    AdapterSessionInfo,
    AdapterMethod,
    ConversationControlResult,
    ExtensionInitializeResult,
    JsonMap,
)
from agent_log_server_rs.codec import (
    AdapterDecodeError,
    decode_json_line,
    encode_json_line,
)
from agent_log_server_rs.adapters.rpc_common import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JSONRPC_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RpcAdapterError,
    ack_from_result,
    adapter_model,
    merged_settings,
    optional_map,
    optional_path,
    optional_string,
    params_map,
    required_string,
    rpc_id,
    string_list,
    string,
)

RpcId: TypeAlias = str | int | None

IMPORT_TRANSCRIPT_BATCH_SIZE = 1000

CONVERSATION_METHODS = ("init_session", "handle_message", "resume_session_with_history")
CONVERSATION_FORK_METHODS = ("fork_conversation", "fork_session")
SESSION_METHODS = ("list_sessions", "resume_session_with_history", "hydrate_transcript")
LIVE_SESSION_STATE_METHOD = "get_live_session_state"
LIVE_SESSION_UNLOAD_METHOD = "unload_live_session"
DEVINS_CONTEXT_SETTINGS_KEY = "__als_devins_context__"


class _FrameworkShellRecord(Protocol):
    id: str
    status: str
    spec_id: str | None
    app_id: str | None
    subgroups: Sequence[str] | None


class _FrameworkShellManager(Protocol):
    async def list_shells(self) -> list[_FrameworkShellRecord]: ...

    async def terminate_shell(self, shell_id: str, force: bool = False) -> object: ...


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
        tasks: list[asyncio.Task[object]] = []
        for task_candidate in (
            getattr(relay, "_connect_task", None),
            getattr(relay, "_relay_task", None),
        ):
            if isinstance(task_candidate, asyncio.Task):
                tasks.append(cast(asyncio.Task[object], task_candidate))
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


def _shellspec_id_from_extension_info(info: JsonMap) -> str | None:
    manifest = optional_map(info.get("manifest")) or {}
    agent = optional_map(manifest.get("agent")) or {}
    shellspec = optional_string(agent.get("shellspec"))
    if not shellspec:
        return None
    _, marker, spec_id = shellspec.rpartition("#")
    if marker != "#":
        return None
    normalized = spec_id.strip()
    return normalized or None


def _shell_record_subgroups(record: _FrameworkShellRecord) -> set[str]:
    raw_subgroups = getattr(record, "subgroups", None)
    if not isinstance(raw_subgroups, (list, tuple, set)):
        return set()
    return {
        value.strip()
        for value in cast(Sequence[object], raw_subgroups)
        if isinstance(value, str) and value.strip()
    }


def _shell_record_matches_current_app(record: _FrameworkShellRecord) -> bool:
    expected_app_id = optional_string(os.environ.get("TE_APP_ID"))
    if not expected_app_id:
        return True
    record_app_id = optional_string(getattr(record, "app_id", None))
    return record_app_id == expected_app_id or expected_app_id in _shell_record_subgroups(record)


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


async def _invoke_handler_with_supported_kwargs(handler_fn: Callable[..., object], **kwargs: object) -> object:
    signature = inspect.signature(handler_fn)
    allows_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    call_kwargs = {
        key: value
        for key, value in kwargs.items()
        if allows_kwargs or key in signature.parameters
    }
    result = handler_fn(**call_kwargs)
    if hasattr(result, "__await__"):
        return await cast(Awaitable[object], result)
    return result


def _dict_result_with_defaults(
    result: JsonMap,
    extension_id: str,
    conversation_id: str,
    params: JsonMap,
) -> JsonMap:
    payload = dict(result)
    payload.setdefault("extension_id", extension_id)
    payload.setdefault("conversation_id", conversation_id)
    provider_session_id = _provider_session_id_param(params)
    if provider_session_id:
        payload.setdefault("provider_session_id", provider_session_id)
    payload.setdefault("supported", True)
    return payload


def optional_path_list(value: object) -> list[Path]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    paths: list[Path] = []
    for item in cast(Sequence[object], value):
        path = optional_path(item)
        if path is not None:
            paths.append(path)
    return paths


def _json_map_list(value: object) -> list[JsonMap]:
    if not isinstance(value, list):
        return []
    items = cast(list[object], value)
    return [cast(JsonMap, item) for item in items if isinstance(item, dict)]


def _env_path(key: str) -> Path | None:
    value = os.environ.get(key)
    return Path(value) if value else None


def _fallback_root() -> Path:
    for key in ("ALS_RS_DATA_DIR", "ALS_RS_CACHE_DIR", "ALS_RS_CONFIG_DIR", "HOME"):
        path = _env_path(key)
        if path is not None:
            return path
    return Path(".")


def _safe_cwd() -> Path:
    try:
        return Path.cwd()
    except OSError:
        return _fallback_root()


def _has_callable_attr(obj: object | None, name: str) -> bool:
    return callable(getattr(obj, name, None)) if obj is not None else False


def _has_any_callable_attr(obj: object | None, names: Sequence[str]) -> bool:
    return any(_has_callable_attr(obj, name) for name in names)


def _default_data_dir() -> Path:
    return _env_path("ALS_RS_DATA_DIR") or _safe_cwd()


def _default_cache_dir() -> Path:
    return _env_path("ALS_RS_CACHE_DIR") or _default_data_dir()


def _default_config_dir() -> Path:
    return _env_path("ALS_RS_CONFIG_DIR") or _default_data_dir()


def _default_extensions_dir() -> Path:
    return _env_path("ALS_RS_EXTENSIONS_DIR") or (_safe_cwd() / "extensions")


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
    shells = cast(list[object], result) if isinstance(result, list) else []
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


def _apply_mcp_context(settings: JsonMap, params: JsonMap) -> None:
    mcp_context = optional_map(params.get("mcp_context"))
    if mcp_context:
        settings["mcp_context"] = mcp_context


def _apply_devins_context(settings: JsonMap, params: JsonMap) -> None:
    devins_context = optional_map(params.get("devins_context"))
    if devins_context:
        settings[DEVINS_CONTEXT_SETTINGS_KEY] = devins_context


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

    def validate_extension_source(
        self,
        *,
        source_type: str,
        source_path: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
        extension_id: str | None = None,
    ) -> JsonMap: ...

    def install_extension_source(
        self,
        *,
        source_type: str,
        source_path: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
        extension_id: str | None = None,
        allow_override: bool = False,
    ) -> JsonMap: ...

    def update_extension_source(
        self,
        extension_id: str,
        *,
        source_type: str | None = None,
        source_path: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
    ) -> JsonMap: ...

    def remove_user_extension(self, extension_id: str) -> JsonMap: ...

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

    async def list_models(self, extension_id: str) -> object: ...

    async def get_splash_schema(self, extension_id: str) -> JsonMap | None: ...

    async def get_runtime_options(
        self,
        extension_id: str,
        conversation_id: str | None = None,
        settings: JsonMap | None = None,
    ) -> JsonMap: ...

    async def get_provider_info(
        self,
        extension_id: str,
        conversation_id: str | None = None,
        provider_session_id: str | None = None,
        settings: JsonMap | None = None,
    ) -> JsonMap: ...

    async def read_plan(self, extension_id: str, conversation_id: str) -> JsonMap: ...

    async def interrupt_session(self, extension_id: str, conversation_id: str) -> JsonMap: ...


@dataclass
class AdapterState:
    extension_id: str = ""
    cwd: Path = field(default_factory=_safe_cwd)
    data_dir: Path = field(default_factory=_default_data_dir)
    cache_dir: Path = field(default_factory=_default_cache_dir)
    config_dir: Path = field(default_factory=_default_config_dir)
    extensions_dir: Path = field(default_factory=_default_extensions_dir)
    extension_roots: list[Path] = field(default_factory=list)
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
        self._shutdown_requested = False

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
            if self._shutdown_requested:
                return 0

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
        request_id = rpc_id(message.get("id"))
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
        method = string(message.get("method"))
        params = params_map(message.get("params"))

        if method == AdapterMethod.EXTENSION_INITIALIZE:
            return await self._initialize(params)
        if method == AdapterMethod.EXTENSION_SHUTDOWN:
            await self.stop_supported_handlers()
            self._shutdown_requested = True
            return {"ok": True}
        if method == AdapterMethod.EXTENSION_RELOAD:
            return await self._reload(params)
        if method == AdapterMethod.EXTENSION_INSTALL_DEPENDENCIES:
            return await self._install_dependencies(params)
        if method == AdapterMethod.EXTENSION_PACKAGE_VALIDATE:
            return await self._package_validate(params)
        if method == AdapterMethod.EXTENSION_PACKAGE_INSTALL:
            return await self._package_install(params)
        if method == AdapterMethod.EXTENSION_PACKAGE_UPDATE:
            return await self._package_update(params)
        if method == AdapterMethod.EXTENSION_PACKAGE_REMOVE:
            return await self._package_remove(params)
        if method == AdapterMethod.EXTENSION_DEBUG_PROBE:
            return await self._debug_probe(params)
        if method == AdapterMethod.EXTENSION_WARM_UP:
            return await self._warm_up(params)
        if method == AdapterMethod.EXTENSION_GET_SPLASH_SCHEMA:
            return await self._splash_schema(params)
        if method == AdapterMethod.EXTENSION_GET_RUNTIME_OPTIONS:
            return await self._runtime_options(params)
        if method == AdapterMethod.EXTENSION_GET_PROVIDER_INFO:
            return await self._provider_info(params)
        if method == AdapterMethod.EXTENSION_GET_PLAN:
            return await self._read_plan(params)
        if method == AdapterMethod.EXTENSION_LIST_MODELS:
            return await self._list_models(params)
        if method == AdapterMethod.EXTENSION_LIST_SESSIONS:
            return await self._list_sessions(params)
        if method == AdapterMethod.EXTENSION_SESSION_STATE_GET:
            return await self._live_session_state(params)
        if method == AdapterMethod.EXTENSION_SESSION_UNLOAD:
            return await self._live_session_unload(params)
        if method == AdapterMethod.CONVERSATION_START:
            return await self._conversation_start(params)
        if method == AdapterMethod.CONVERSATION_RESUME:
            return await self._conversation_resume(params)
        if method == AdapterMethod.CONVERSATION_FORK:
            return await self._conversation_fork(params)
        if method == AdapterMethod.CONVERSATION_SEND:
            return await self._conversation_send(params)
        if method == AdapterMethod.CONVERSATION_INTERRUPT:
            return await self._conversation_interrupt(params)
        if method == AdapterMethod.APPROVAL_RESPOND:
            return await self._approval_respond(params)

        raise RpcAdapterError(METHOD_NOT_FOUND, f"Unsupported method: {method}")

    async def _initialize(self, params: JsonMap) -> JsonMap:
        extension_id = optional_string(params.get("extension_id"))
        if extension_id is None:
            raise RpcAdapterError(INVALID_PARAMS, "extension_id is required")
        cwd = optional_path(params.get("cwd")) or _safe_cwd()
        data_dir = optional_path(params.get("data_dir")) or cwd
        cache_dir = optional_path(params.get("cache_dir")) or data_dir
        config_dir = optional_path(params.get("config_dir")) or data_dir
        extension_roots = optional_path_list(params.get("extensions_dirs"))
        legacy_extensions_dir = optional_path(params.get("extensions_dir"))
        if not extension_roots and legacy_extensions_dir is not None:
            extension_roots = [legacy_extensions_dir]
        if not extension_roots:
            extension_roots = list(self._state.extension_roots) or [self._state.extensions_dir]
        extensions_dir = extension_roots[0]
        settings = optional_map(params.get("settings")) or {}

        self._state = AdapterState(
            extension_id=extension_id,
            cwd=cwd,
            data_dir=data_dir,
            cache_dir=cache_dir,
            config_dir=config_dir,
            extensions_dir=extensions_dir,
            extension_roots=extension_roots,
            settings=settings,
            meta=self._state.meta,
        )
        self._ensure_loader_initialized()
        info = self._extension_info(extension_id)
        ext_type = optional_string(info.get("type")) or ""
        active = info.get("active") is True
        handler = self._loader.get_handler(extension_id) if active else None
        conversations_supported = _has_any_callable_attr(handler, CONVERSATION_METHODS)
        models_supported = _has_callable_attr(handler, "list_models")
        sessions_supported = _has_any_callable_attr(handler, SESSION_METHODS)
        interruption_supported = _has_callable_attr(handler, "abort_session")
        fork_supported = _has_any_callable_attr(handler, CONVERSATION_FORK_METHODS)
        return ExtensionInitializeResult(
            extension_id=extension_id,
            provider=ext_type or extension_id,
            capabilities=AdapterCapabilities(
                conversations=conversations_supported,
                models=models_supported,
                sessions=sessions_supported,
                interruption=interruption_supported,
                conversation_fork=fork_supported,
                live_events=conversations_supported,
                transcript_records=conversations_supported,
                extra={
                    "supported": active and handler is not None,
                    "type": ext_type,
                    "manifest": info.get("manifest", {}),
                },
            ),
        ).to_json()

    async def _reload(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        force = params.get("force") is True
        changed_extension_ids = string_list(params.get("changed_extension_ids"))
        await self._stop_reload_targets(changed_extension_ids, force=force)
        await self._stop_reload_target_shells(changed_extension_ids)
        self._loader.reload_extensions(changed_extension_ids, force=force)
        self._apply_enabled_overrides(params.get("enabled_overrides"))
        extensions = await self._refresh_dependency_state()
        wait_ready_extension_id = optional_string(params.get("wait_ready_extension_id"))
        wait_ready = False
        if wait_ready_extension_id:
            wait_ready = await self._wait_ready_if_active(wait_ready_extension_id)
            extensions = list(self._loader.list_extensions())
        return {"ok": True, "extensions": extensions, "wait_ready": wait_ready}

    async def _warm_up(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        self._apply_enabled_overrides(params.get("enabled_overrides"))
        await self._refresh_dependency_state()
        timeout = _positive_float(params.get("timeout"), 60.0)
        results = await self._loader.warm_up_extensions(timeout=timeout)
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
                dependency_result: JsonMap = {
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

    async def _package_validate(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        source_type = required_string(params, "source_type")
        result = self._loader.validate_extension_source(
            source_type=source_type,
            source_path=optional_string(params.get("source_path")),
            repo_url=optional_string(params.get("repo_url")),
            ref=optional_string(params.get("ref")),
            extension_id=optional_string(params.get("extension_id")),
        )
        return dict(result)

    async def _package_install(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        source_type = required_string(params, "source_type")
        result = self._loader.install_extension_source(
            source_type=source_type,
            source_path=optional_string(params.get("source_path")),
            repo_url=optional_string(params.get("repo_url")),
            ref=optional_string(params.get("ref")),
            extension_id=optional_string(params.get("extension_id")),
            allow_override=params.get("allow_override") is True,
        )
        return {
            "ok": bool(result.get("ok")),
            "result": dict(result),
            "extension_id": optional_string(result.get("extension_id")),
            "extensions": list(self._loader.list_extensions()),
        }

    async def _package_update(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        extension_id = required_string(params, "extension_id")
        result = self._loader.update_extension_source(
            extension_id,
            source_type=optional_string(params.get("source_type")),
            source_path=optional_string(params.get("source_path")),
            repo_url=optional_string(params.get("repo_url")),
            ref=optional_string(params.get("ref")),
        )
        return {
            "ok": bool(result.get("ok")),
            "result": dict(result),
            "extension_id": extension_id,
            "extensions": list(self._loader.list_extensions()),
        }

    async def _package_remove(self, params: JsonMap) -> JsonMap:
        self._ensure_loader_initialized()
        extension_id = required_string(params, "extension_id")
        info = self._loader.get_extension_info(extension_id) or {}
        if info.get("source_kind") == "builtin":
            return {
                "ok": False,
                "status": "conflict",
                "message": f"Refusing to remove builtin extension: {extension_id}",
                "extension_id": extension_id,
                "extensions": list(self._loader.list_extensions()),
            }
        result = self._loader.remove_user_extension(extension_id)
        return {
            "ok": bool(result.get("ok")),
            "result": dict(result),
            "extension_id": extension_id,
            "extensions": list(self._loader.list_extensions()),
        }

    async def _debug_probe(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        ensure_manager = params.get("ensure_manager") is True
        info: JsonMap | None = None
        handler: object | None = None
        handler_class: str | None = None
        if self._loader_initialized:
            info = self._loader.get_extension_info(extension_id)
            handler = self._loader.get_handler(extension_id)
            if handler is not None:
                handler_class = type(handler).__name__
        manager_probe = await _framework_shell_manager_probe(ensure_manager)
        return {
            "ok": True,
            "adapter": {
                "pid": os.getpid(),
                "cwd": str(_safe_cwd()),
                "state_cwd": str(self._state.cwd),
                "extensions_dir": str(self._state.extensions_dir),
                "extension_roots": [str(root) for root in self._state.extension_roots],
                "loader_initialized": self._loader_initialized,
                "selected_extension_id": self._state.extension_id,
            },
            "extension": {
                "requested_id": extension_id,
                "known": info is not None,
                "type": optional_string(info.get("type")) if info else None,
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
        for extension_id, enabled in cast(JsonMap, value).items():
            if isinstance(enabled, bool):
                self._loader.set_extension_enabled(extension_id, enabled)

    async def _refresh_dependency_state(self) -> list[JsonMap]:
        for extension in list(self._loader.list_extensions()):
            extension_id = optional_string(extension.get("id"))
            if not extension_id:
                continue
            if self._loader.supports_dependency_check(extension_id):
                result = await self._loader.check_extension_dependencies(extension_id)
            else:
                result: JsonMap = {
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
        if info.get("active") is not True:
            return {
                "models": [],
                "supported": False,
                "reason": f"Extension is not active: {extension_id}",
            }
        result = await self._loader.list_models(extension_id)
        result_map = optional_map(result)
        if result_map is not None:
            models = _json_map_list(result_map.get("models"))
        else:
            models = _json_map_list(result)
        return {
            "models": [
                adapter_model(model).to_json()
                for model in models
                if isinstance(model.get("id"), str)
            ]
        }

    async def _list_sessions(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        list_sessions = getattr(handler, "list_sessions", None)
        if not callable(list_sessions):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support session listing")
        cwd = optional_string(params.get("cwd")) or str(self._state.cwd)
        limit_value = params.get("limit")
        limit = limit_value if isinstance(limit_value, int) and limit_value > 0 else None
        signature = inspect.signature(list_sessions)
        allows_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        call_kwargs: JsonMap = {"cwd": cwd}
        for key, value in params.items():
            if key in {"extension_id", "cwd", "limit"}:
                continue
            if allows_kwargs or key in signature.parameters:
                call_kwargs[key] = value
        result = list_sessions(**call_kwargs)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        result_map = optional_map(result)
        if result_map is not None:
            sessions = _json_map_list(result_map.get("sessions"))
        else:
            sessions = _json_map_list(result)
        normalized: list[JsonMap] = []
        for session in sessions:
            session_id = (
                optional_string(session.get("id"))
                or optional_string(session.get("session_id"))
                or optional_string(session.get("thread_id"))
            )
            if not session_id:
                continue
            label = (
                optional_string(session.get("label"))
                or optional_string(session.get("title"))
                or optional_string(session.get("name"))
            )
            session_cwd = optional_path(session.get("cwd"))
            if session_cwd is None:
                raw_context = optional_map(session.get("context"))
                if raw_context is not None:
                    session_cwd = optional_path(raw_context.get("cwd"))
            created_at = (
                optional_string(session.get("created_at"))
                or optional_string(session.get("createdAt"))
                or optional_string(session.get("start_time"))
                or optional_string(session.get("startTime"))
            )
            updated_at = (
                optional_string(session.get("updated_at"))
                or optional_string(session.get("updatedAt"))
                or optional_string(session.get("modified_time"))
                or optional_string(session.get("modifiedTime"))
            )
            metadata: JsonMap = {}
            raw_metadata = optional_map(session.get("metadata"))
            if raw_metadata is not None:
                for raw_key, raw_value in raw_metadata.items():
                    metadata[raw_key] = raw_value
            for key, value in session.items():
                if key in {
                    "id",
                    "session_id",
                    "thread_id",
                    "label",
                    "title",
                    "name",
                    "cwd",
                    "created_at",
                    "createdAt",
                    "start_time",
                    "startTime",
                    "updated_at",
                    "updatedAt",
                    "modified_time",
                    "modifiedTime",
                    "metadata",
                }:
                    continue
                metadata[key] = value
            normalized.append(
                AdapterSessionInfo(
                    id=session_id,
                    label=label,
                    cwd=session_cwd,
                    created_at=created_at,
                    updated_at=updated_at,
                    metadata=metadata,
                ).to_json()
            )
            if limit is not None and len(normalized) >= limit:
                break
        return {"sessions": normalized}

    async def _live_session_state(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        state_fn = getattr(handler, LIVE_SESSION_STATE_METHOD, None)
        if not callable(state_fn):
            return {
                "ok": True,
                "supported": False,
                "state": "unsupported",
                "loaded": False,
                "unload_supported": False,
            }
        conversation_id = required_string(params, "conversation_id")
        result = await _invoke_handler_with_supported_kwargs(
            state_fn,
            conversation_id=conversation_id,
            provider_session_id=_provider_session_id_param(params),
            settings=optional_map(params.get("settings")) or {},
        )
        result_map = optional_map(result)
        if result_map is not None:
            return _dict_result_with_defaults(result_map, extension_id, conversation_id, params)
        return {
            "ok": False,
            "supported": True,
            "state": "unknown",
            "loaded": False,
            "unload_supported": False,
            "extension_id": extension_id,
            "conversation_id": conversation_id,
            "provider_session_id": _provider_session_id_param(params),
            "error": "Invalid live session state response",
        }

    async def _live_session_unload(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        unload_fn = getattr(handler, LIVE_SESSION_UNLOAD_METHOD, None)
        if not callable(unload_fn):
            return {
                "ok": False,
                "supported": False,
                "state": "unsupported",
                "loaded": False,
                "unload_supported": False,
                "error": "Extension does not implement live session unload",
            }
        conversation_id = required_string(params, "conversation_id")
        result = await _invoke_handler_with_supported_kwargs(
            unload_fn,
            conversation_id=conversation_id,
            provider_session_id=_provider_session_id_param(params),
            settings=optional_map(params.get("settings")) or {},
        )
        result_map = optional_map(result)
        if result_map is not None:
            return _dict_result_with_defaults(result_map, extension_id, conversation_id, params)
        return {
            "ok": False,
            "supported": True,
            "state": "unknown",
            "loaded": False,
            "unload_supported": True,
            "extension_id": extension_id,
            "conversation_id": conversation_id,
            "provider_session_id": _provider_session_id_param(params),
            "error": "Invalid live session unload response",
        }

    async def _splash_schema(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        schema = await self._loader.get_splash_schema(extension_id)
        if isinstance(schema, dict) and schema:
            result = dict(schema)
            result.setdefault("extension_id", extension_id)
            return result
        return {"version": "1", "extension_id": extension_id, "fields": []}

    async def _runtime_options(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        conversation_id = optional_string(params.get("conversation_id"))
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        result = await self._loader.get_runtime_options(
            extension_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        return dict(result)

    async def _provider_info(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._extension_info(extension_id)
        conversation_id = optional_string(params.get("conversation_id"))
        provider_session_id = _provider_session_id_param(params)
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        result = await self._loader.get_provider_info(
            extension_id,
            conversation_id=conversation_id,
            provider_session_id=provider_session_id,
            settings=settings,
        )
        result_map = optional_map(result)
        if result_map is not None:
            defaults: JsonMap = {"extension_id": extension_id}
            if conversation_id is not None:
                defaults["conversation_id"] = conversation_id
            if provider_session_id is not None:
                defaults["provider_session_id"] = provider_session_id
            for key, value in defaults.items():
                result_map.setdefault(key, value)
            return result_map
        return {
            "ok": False,
            "supported": True,
            "extension_id": extension_id,
            "conversation_id": conversation_id,
            "provider_session_id": provider_session_id,
            "error": "Invalid provider info response",
        }

    async def _read_plan(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        info = self._extension_info(extension_id)
        conversation_id = required_string(params, "conversation_id")
        result = await self._loader.read_plan(extension_id, conversation_id)
        result_map = optional_map(result)
        if result_map is not None:
            return _dict_result_with_defaults(result_map, extension_id, conversation_id, params)
        return {
            "extension_id": extension_id,
            "conversation_id": conversation_id,
            "has_plan": bool(info.get("has_plan")),
            "has_todo": bool(info.get("has_todo")),
            "plan_exists": False,
            "plan_content": "",
            "plan_steps": [],
            "supported": True,
        }

    async def _conversation_start(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        init_session = getattr(handler, "init_session", None)
        if not callable(init_session):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support session start")

        conversation_id = required_string(params, "conversation_id")
        cwd = optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        _apply_mcp_context(settings, params)
        _apply_devins_context(settings, params)
        settings["cwd"] = cwd
        settings.setdefault("agent", extension_id)
        result = init_session(conversation_id, extension_id, cwd, settings=settings)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid session start result")
        return ack_from_result(conversation_id, cast(JsonMap, result)).to_json()

    async def _conversation_resume(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        resume_session_with_history = getattr(handler, "resume_session_with_history", None)
        if not callable(resume_session_with_history):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support session resume")

        conversation_id = required_string(params, "conversation_id")
        provider_session_id = _provider_session_id_param(params)
        if not provider_session_id:
            raise RpcAdapterError(INVALID_PARAMS, "provider_session_id or thread_id is required")

        cwd = optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        _apply_mcp_context(settings, params)
        _apply_devins_context(settings, params)
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
            model=optional_string(settings.get("model")),
            settings=settings,
        )
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        if not isinstance(result, dict):
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid session resume result")

        result_map = cast(JsonMap, result)
        ack = ack_from_result(conversation_id, result_map).to_json()
        if result_map.get("ok") is not True:
            return ack

        hydrated_count = 0
        hydrate_transcript = getattr(handler, "hydrate_transcript", None)
        if callable(hydrate_transcript):
            await self._send_import_started(
                conversation_id,
                extension_id=extension_id,
                provider_session_id=provider_session_id,
            )
            try:
                items = hydrate_transcript(
                    session_id=provider_session_id,
                    conversation_id=conversation_id,
                    cwd=cwd,
                    model=optional_string(settings.get("model")),
                    settings=settings,
                )
                if hasattr(items, "__await__"):
                    items = await cast(Awaitable[object], items)
                if isinstance(items, list):
                    item_list = cast(list[object], items)
                    batch: list[JsonMap] = []
                    total_count = len(item_list)
                    await self._send_import_progress(
                        conversation_id,
                        extension_id=extension_id,
                        provider_session_id=provider_session_id,
                        phase="persisting",
                        transcript_count=total_count,
                        persisted_count=0,
                    )
                    for item in item_list:
                        if isinstance(item, dict):
                            hydrated_entry = dict(cast(JsonMap, item))
                            hydrated_entry["_hydrated_history"] = True
                            batch.append(hydrated_entry)
                            hydrated_count += 1
                            if len(batch) >= IMPORT_TRANSCRIPT_BATCH_SIZE:
                                await self._send_import_transcript_batch(
                                    conversation_id,
                                    batch,
                                    extension_id=extension_id,
                                    provider_session_id=provider_session_id,
                                    transcript_count=total_count,
                                    persisted_count=hydrated_count,
                                )
                                batch = []
                    if batch:
                        await self._send_import_transcript_batch(
                            conversation_id,
                            batch,
                            extension_id=extension_id,
                            provider_session_id=provider_session_id,
                            transcript_count=total_count,
                            persisted_count=hydrated_count,
                        )
                    await self._send_import_completed(
                        conversation_id,
                        extension_id=extension_id,
                        provider_session_id=provider_session_id,
                        transcript_count=total_count,
                        persisted_count=hydrated_count,
                    )
                else:
                    await self._send_import_completed(
                        conversation_id,
                        extension_id=extension_id,
                        provider_session_id=provider_session_id,
                        transcript_count=0,
                        persisted_count=0,
                    )
            except Exception as exc:
                print(
                    f"[ExtensionAdapter] {extension_id} hydrate_transcript failed for "
                    f"{conversation_id[:8]}: {exc}",
                    file=sys.stderr,
                )
                hydration_error = f"{type(exc).__name__}: {exc}"
                ack["hydration_error"] = hydration_error
                await self._send_import_failed(
                    conversation_id,
                    extension_id=extension_id,
                    provider_session_id=provider_session_id,
                    error=hydration_error,
                    persisted_count=hydrated_count,
                )
        ack["hydrated_count"] = hydrated_count
        return ack

    async def _conversation_fork(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        fork_fn = getattr(handler, "fork_conversation", None) or getattr(handler, "fork_session", None)
        if not callable(fork_fn):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support conversation fork")

        source_conversation_id = required_string(params, "source_conversation_id")
        conversation_id = required_string(params, "conversation_id")
        provider_session_id = _provider_session_id_param(params)
        if not provider_session_id:
            raise RpcAdapterError(INVALID_PARAMS, "provider_session_id or thread_id is required")

        cwd = optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        _apply_mcp_context(settings, params)
        _apply_devins_context(settings, params)
        settings["cwd"] = cwd
        settings["agent"] = extension_id
        settings["conversation_id"] = conversation_id
        self._seed_conversation_meta(
            source_conversation_id,
            extension_id=extension_id,
            settings=settings,
            cwd=cwd,
            provider_session_id=provider_session_id,
        )
        self._seed_conversation_meta(
            conversation_id,
            extension_id=extension_id,
            settings=settings,
            cwd=cwd,
        )

        result = await _invoke_handler_with_supported_kwargs(
            fork_fn,
            extension_id=extension_id,
            source_conversation_id=source_conversation_id,
            conversation_id=conversation_id,
            target_conversation_id=conversation_id,
            provider_session_id=provider_session_id,
            session_id=provider_session_id,
            cwd=cwd,
            settings=settings,
            metadata=optional_map(params.get("metadata")) or {},
        )
        result_map = optional_map(result)
        if result_map is None:
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid fork result")
        if optional_string(result_map.get("provider_session_id")) is None:
            thread_id = optional_string(result_map.get("thread_id")) or optional_string(result_map.get("session_id"))
            if thread_id:
                result_map["provider_session_id"] = thread_id
        payload = _dict_result_with_defaults(result_map, extension_id, conversation_id, params)
        payload.setdefault("source_conversation_id", source_conversation_id)
        return payload

    async def _conversation_send(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        handle_message = getattr(handler, "handle_message", None)
        if not callable(handle_message):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support conversation send")

        conversation_id = required_string(params, "conversation_id")
        text = required_string(params, "text")
        cwd = optional_string(params.get("cwd")) or str(self._state.cwd)
        settings = merged_settings(self._state.settings, optional_map(params.get("settings")))
        _apply_mcp_context(settings, params)
        _apply_devins_context(settings, params)
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
        return ack_from_result(conversation_id, cast(JsonMap, result)).to_json()

    async def _conversation_interrupt(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        self._supported_handler(extension_id)
        conversation_id = required_string(params, "conversation_id")
        result = await self._loader.interrupt_session(extension_id, conversation_id)
        result_map = optional_map(result)
        if result_map is None:
            raise RpcAdapterError(INTERNAL_ERROR, f"{extension_id} returned invalid interrupt result")
        payload = dict(result_map)
        ok = bool(payload.get("ok"))
        error = optional_string(payload.get("error"))
        metadata = {
            key: value
            for key, value in payload.items()
            if key not in {"ok", "conversation_id", "extension_id", "error"}
        }
        return ConversationControlResult(
            extension_id=extension_id,
            conversation_id=conversation_id,
            ok=ok,
            error=error,
            metadata=metadata,
        ).to_json()

    async def _approval_respond(self, params: JsonMap) -> JsonMap:
        extension_id = self._extension_id_param(params)
        handler = self._supported_handler(extension_id)
        resolver = getattr(handler, "resolve_approval", None)
        if not callable(resolver):
            raise RpcAdapterError(METHOD_NOT_FOUND, f"{extension_id} does not support approvals")

        conversation_id = required_string(params, "conversation_id")
        request_id = required_string(params, "request_id")
        result_payload = params.get("result")
        resolution: JsonMap = dict(optional_map(result_payload) or {})
        decision = optional_string(params.get("decision"))
        if decision and "decision" not in resolution:
            resolution["decision"] = decision
        result = resolver(request_id, resolution)
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[object], result)
        resolved = bool(result)
        return {
            "ok": resolved,
            "resolved": resolved,
            "conversation_id": conversation_id,
            "request_id": request_id,
        }

    def _ensure_loader_initialized(self) -> None:
        if self._loader_initialized:
            return
        extension_roots = list(self._state.extension_roots) or [self._state.extensions_dir]
        self._loader.load_extensions(
            extension_roots,
            self._state.cwd,
            _get_framework_shell_manager,
            self._broadcast,
            self._transcript,
            meta_fns={"load": self._load_meta, "save": self._save_meta},
        )
        self._loader_initialized = True

    def _extension_id_param(self, params: JsonMap) -> str:
        extension_id = optional_string(params.get("extension_id")) or optional_string(self._state.extension_id)
        if extension_id is None:
            raise RpcAdapterError(INVALID_PARAMS, "extension_id is required")
        return extension_id

    def _extension_info(self, extension_id: str) -> JsonMap:
        self._ensure_loader_initialized()
        info = self._loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            raise RpcAdapterError(INVALID_PARAMS, f"Unknown extension: {extension_id}")
        return info

    def _supported_handler(self, extension_id: str) -> object:
        info = self._extension_info(extension_id)
        if info.get("active") is not True:
            raise RpcAdapterError(INVALID_PARAMS, f"Extension is not active: {extension_id}")
        handler = self._loader.get_handler(extension_id)
        if handler is None:
            raise RpcAdapterError(INTERNAL_ERROR, f"No handler loaded for extension: {extension_id}")
        return handler

    async def stop_supported_handlers(self) -> None:
        for extension in self._loader.list_extensions():
            extension_id = optional_string(extension.get("id"))
            if not extension_id:
                continue
            handler = self._loader.get_handler(extension_id)
            stop_client = None
            if handler is not None:
                stop_client = getattr(handler, "stop_client", None) or getattr(
                    handler,
                    "shutdown_client",
                    None,
                )
            if callable(stop_client):
                result = stop_client()
                if hasattr(result, "__await__"):
                    await cast(Awaitable[object], result)

    async def _stop_reload_targets(self, changed_extension_ids: list[str], *, force: bool) -> None:
        if force and not changed_extension_ids:
            await self.stop_supported_handlers()
            return
        if not changed_extension_ids:
            return

        affected_types: set[str] = set()
        for extension_id in changed_extension_ids:
            info = optional_map(self._loader.get_extension_info(extension_id))
            extension_type = optional_string(info.get("type")) if info is not None else None
            if extension_type:
                affected_types.add(extension_type)
        if not affected_types:
            return

        stopped_types: set[str] = set()
        for extension in self._loader.list_extensions():
            extension_id = optional_string(extension.get("id"))
            if not extension_id:
                continue
            extension_type = optional_string(extension.get("type"))
            if not extension_type or extension_type not in affected_types or extension_type in stopped_types:
                continue
            handler = self._loader.get_handler(extension_id)
            stop_client = None
            if handler is not None:
                stop_client = getattr(handler, "stop_client", None) or getattr(
                    handler,
                    "shutdown_client",
                    None,
                )
            if callable(stop_client):
                result = stop_client()
                if hasattr(result, "__await__"):
                    await cast(Awaitable[object], result)
            stopped_types.add(extension_type)

    async def _stop_reload_target_shells(self, changed_extension_ids: list[str]) -> None:
        if not changed_extension_ids:
            return

        target_spec_ids: set[str] = set()
        for extension_id in changed_extension_ids:
            info = optional_map(self._loader.get_extension_info(extension_id))
            if info is None:
                continue
            spec_id = _shellspec_id_from_extension_info(info)
            if spec_id:
                target_spec_ids.add(spec_id)
        if not target_spec_ids:
            return

        mgr = cast(_FrameworkShellManager, await _get_framework_shell_manager())
        records = await mgr.list_shells()
        terminated_shell_ids: set[str] = set()
        for record in records:
            shell_id = optional_string(getattr(record, "id", None))
            if not shell_id or shell_id in terminated_shell_ids:
                continue
            if optional_string(getattr(record, "status", None)) != "running":
                continue
            if optional_string(getattr(record, "spec_id", None)) not in target_spec_ids:
                continue
            if not _shell_record_matches_current_app(record):
                continue
            await mgr.terminate_shell(shell_id, force=True)
            terminated_shell_ids.add(shell_id)

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
        existing_settings = optional_map(meta.get("settings"))
        merged_settings: JsonMap = dict(existing_settings or {})
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

    async def _send_import_started(
        self,
        conversation_id: str,
        *,
        extension_id: str,
        provider_session_id: str,
    ) -> None:
        await self._send_notification(
            AdapterEventMethod.IMPORT_STARTED,
            {
                "conversation_id": conversation_id,
                "extension_id": extension_id,
                "provider_session_id": provider_session_id,
                "phase": "hydrating",
                "status": "starting",
                "message": "Porting in transcript. This can take a while for large transcripts.",
            },
        )

    async def _send_import_progress(
        self,
        conversation_id: str,
        *,
        extension_id: str,
        provider_session_id: str,
        phase: str,
        transcript_count: int,
        persisted_count: int,
    ) -> None:
        await self._send_notification(
            AdapterEventMethod.IMPORT_PROGRESS,
            {
                "conversation_id": conversation_id,
                "extension_id": extension_id,
                "provider_session_id": provider_session_id,
                "phase": phase,
                "status": "in_progress",
                "transcript_count": transcript_count,
                "persisted_count": persisted_count,
            },
        )

    async def _send_import_transcript_batch(
        self,
        conversation_id: str,
        records: list[JsonMap],
        *,
        extension_id: str,
        provider_session_id: str,
        transcript_count: int,
        persisted_count: int,
    ) -> None:
        await self._send_notification(
            AdapterEventMethod.IMPORT_TRANSCRIPT_BATCH,
            {
                "conversation_id": conversation_id,
                "extension_id": extension_id,
                "provider_session_id": provider_session_id,
                "records": records,
                "batch_count": len(records),
                "transcript_count": transcript_count,
                "persisted_count": persisted_count,
            },
        )

    async def _send_import_completed(
        self,
        conversation_id: str,
        *,
        extension_id: str,
        provider_session_id: str,
        transcript_count: int,
        persisted_count: int,
    ) -> None:
        await self._send_notification(
            AdapterEventMethod.IMPORT_COMPLETED,
            {
                "conversation_id": conversation_id,
                "extension_id": extension_id,
                "provider_session_id": provider_session_id,
                "phase": "complete",
                "status": "complete",
                "transcript_count": transcript_count,
                "persisted_count": persisted_count,
                "select_conversation": True,
            },
        )

    async def _send_import_failed(
        self,
        conversation_id: str,
        *,
        extension_id: str,
        provider_session_id: str,
        error: str,
        persisted_count: int,
    ) -> None:
        await self._send_notification(
            AdapterEventMethod.IMPORT_FAILED,
            {
                "conversation_id": conversation_id,
                "extension_id": extension_id,
                "provider_session_id": provider_session_id,
                "phase": "failed",
                "status": "failed",
                "error": error,
                "persisted_count": persisted_count,
            },
        )

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
        value = optional_string(params.get(key))
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
        await adapter.stop_supported_handlers()
        await _close_framework_shell_peer()


def main(argv: Sequence[str] | None = None) -> int:
    if argv:
        raise SystemExit("extension adapter does not accept CLI arguments yet")
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
