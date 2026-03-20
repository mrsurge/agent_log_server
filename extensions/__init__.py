"""
Extension Loader

Discovers and loads pluggable agent extensions by scanning subfolders for
manifest.json files.  Each extension lives in its own directory under
extensions/ with:
    manifest.json   — metadata (id, name, type, enabled, capabilities)
    client.py       — handler module (init_*_manager, handle_message, etc.)
    router.py       — event translation (optional)
    settings_schema.json — UI schema (optional)

extensions.json is checked first as an explicit override/ordering file.
If absent, subfolders are scanned alphabetically.
"""

import importlib
import inspect
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Extension type -> handler module
_extension_handlers: Dict[str, Any] = {}
# extension_id -> registry info (id, name, type, path, manifest)
_extensions_registry: Dict[str, Dict[str, Any]] = {}
_initialized: bool = False

# Callbacks stored for lazy init of discovered extensions
_init_args: Dict[str, Any] = {}


def _manifest_capability_flag(manifest: Any, *names: str) -> bool:
    if not isinstance(manifest, dict):
        return False
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    return any(bool(capabilities.get(name)) for name in names)


def load_extensions(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> None:
    """
    Discover and load extensions.

    1. If extensions.json exists, load entries from it (explicit ordering).
    2. Otherwise scan extensions/*/manifest.json (alphabetical).
    3. For each enabled extension, import extensions.<folder>.client and
       call its init function with the standard callback set.
    """
    global _extension_handlers, _extensions_registry, _initialized, _init_args

    _init_args = {
        "extensions_dir": extensions_dir,
        "server_root": server_root,
        "fws_getter": fws_getter,
        "broadcast_fn": broadcast_fn,
        "transcript_fn": transcript_fn,
        "meta_fns": meta_fns,
    }

    discovered = _discover_extensions(extensions_dir)

    _extension_handlers = {}
    _extensions_registry = {}

    enabled_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for ext_info in discovered:
        ext_id = ext_info["id"]
        ext_type = ext_info["type"]
        folder = ext_info["folder"]
        manifest = ext_info.get("manifest") if isinstance(ext_info.get("manifest"), dict) else {}
        dependencies = manifest.get("dependencies") if isinstance(manifest.get("dependencies"), dict) else {}
        default_enabled = bool(ext_info.get("enabled", True))

        _extensions_registry[ext_id] = {
            "id": ext_id,
            "name": ext_info.get("name", ext_id),
            "type": ext_type,
            "path": folder,
            "manifest": manifest,
            "capabilities": manifest.get("capabilities", {}) if isinstance(manifest, dict) else {},
            "ui": manifest.get("ui", {}) if isinstance(manifest, dict) else {},
            "has_plan": _manifest_capability_flag(manifest, "hasPlan", "has_plan"),
            "has_todo": _manifest_capability_flag(manifest, "hasTodo", "has_todo"),
            "default_enabled": default_enabled,
            "enabled": default_enabled,
            "dependency_status": "unchecked",
            "dependency_ok": True,
            "dependency_message": "",
            "dependency_details": {},
            "has_dependency_check": bool(dependencies.get("has_check")),
            "has_dependency_install": bool(dependencies.get("has_install")),
            "active": default_enabled,
        }

        if default_enabled:
            enabled_by_type.setdefault(ext_type, []).append(ext_info)

    for ext_info in discovered:
        if not ext_info.get("enabled", True):
            continue
        ext_type = ext_info["type"]
        folder = ext_info["folder"]

        if ext_type not in _extension_handlers:
            handler = _load_handler(
                folder,
                ext_type,
                handler_extensions=enabled_by_type.get(ext_type, []),
                **_init_args,
            )
            if handler:
                _extension_handlers[ext_type] = handler

    _initialized = True
    print(f"[Extensions] Loaded {len(_extensions_registry)} extension(s): "
          f"{list(_extensions_registry.keys())}")


def _discover_extensions(extensions_dir: Path) -> List[Dict[str, Any]]:
    """Return list of extension info dicts from manifests."""
    result: List[Dict[str, Any]] = []

    # Strategy 1: explicit extensions.json
    extensions_json = extensions_dir / "extensions.json"
    if extensions_json.exists():
        try:
            data = json.loads(extensions_json.read_text())
            for entry in data.get("extensions", []):
                folder = entry.get("path", entry.get("id", ""))
                manifest_path = extensions_dir / folder / "manifest.json"
                manifest = {}
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text())
                    except Exception:
                        pass
                result.append({
                    "id": entry.get("id") or manifest.get("id", folder),
                    "name": entry.get("name") or manifest.get("name", folder),
                    "type": entry.get("type") or manifest.get("type", folder),
                    "enabled": entry.get("enabled", manifest.get("enabled", True)),
                    "folder": folder,
                    "manifest": manifest,
                })
            return result
        except Exception as e:
            print(f"[Extensions] Failed to read extensions.json: {e}")

    # Strategy 2: scan subfolders for manifest.json
    for sub in sorted(extensions_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        manifest_path = sub / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            result.append({
                "id": manifest.get("id", sub.name),
                "name": manifest.get("name", sub.name),
                "type": manifest.get("type", sub.name),
                "enabled": manifest.get("enabled", True),
                "folder": sub.name,
                "manifest": manifest,
            })
        except Exception as e:
            print(f"[Extensions] Bad manifest in {sub.name}/: {e}")

    return result


def _load_handler(
    folder: str,
    ext_type: str,
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]],
    handler_extensions: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Any]:
    """Dynamically import extensions.<folder>.client and call its init function."""
    module_path = f"extensions.{folder}.client"
    try:
        mod = importlib.import_module(module_path)
    except Exception as e:
        print(f"[Extensions] Failed to import {module_path}: {e}")
        import traceback
        traceback.print_exc()
        return None

    # Convention: init function is init_<type>_manager(...)
    init_fn_name = f"init_{ext_type}_manager"
    init_fn = getattr(mod, init_fn_name, None)
    if init_fn is None:
        # Fallback: try init_<folder>_manager (folder may differ from type)
        init_fn = getattr(mod, f"init_{folder}_manager", None)
    if init_fn is None:
        # Fallback: scan for any init_*_manager function
        for attr in dir(mod):
            if attr.startswith("init_") and attr.endswith("_manager") and callable(getattr(mod, attr)):
                init_fn = getattr(mod, attr)
                break
    if init_fn is None:
        # Last resort: try generic init_manager
        init_fn = getattr(mod, "init_manager", None)
    if init_fn is None:
        print(f"[Extensions] {module_path} has no {init_fn_name}() or init_manager()")
        return mod  # still return module — may work without init

    init_kwargs: Dict[str, Any] = {}
    try:
        init_signature = inspect.signature(init_fn)
    except (TypeError, ValueError):
        init_signature = None
    if init_signature and "registered_extension_ids" in init_signature.parameters:
        init_kwargs["registered_extension_ids"] = [
            ext_id
            for ext_id in (
                entry.get("id") if isinstance(entry, dict) else None
                for entry in (handler_extensions or [])
            )
            if isinstance(ext_id, str) and ext_id
        ]

    try:
        init_fn(
            extensions_dir,
            server_root,
            fws_getter,
            broadcast_fn,
            transcript_fn,
            meta_fns,
            **init_kwargs,
        )
    except Exception as e:
        print(f"[Extensions] {init_fn_name}() failed: {e}")
        import traceback
        traceback.print_exc()
        return None

    return mod


def get_handler(extension_id: str) -> Optional[Any]:
    """Get the handler module for an extension by its ID."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return None
    return _extension_handlers.get(ext_info["type"])


def has_extension(extension_id: str) -> bool:
    """Check if an extension is registered."""
    info = _extensions_registry.get(extension_id)
    return bool(info and info.get("active"))


def list_extensions() -> List[Dict[str, Any]]:
    """List all registered extensions."""
    return [dict(info) for info in _extensions_registry.values()]


def get_extension_info(extension_id: str) -> Optional[Dict[str, Any]]:
    """Return registry metadata for one extension."""
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return None
    return dict(info)


def _recompute_extension_active_state(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    info["active"] = bool(info.get("enabled")) and bool(info.get("dependency_ok"))
    return bool(info.get("active"))


def _active_extensions_for_type(ext_type: str) -> List[Dict[str, Any]]:
    return [
        info
        for info in _extensions_registry.values()
        if isinstance(info, dict) and info.get("type") == ext_type and info.get("active")
    ]


def _ensure_handler_loaded_for_extension(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    ext_type = info.get("type")
    folder = info.get("path")
    if not isinstance(ext_type, str) or not ext_type or not isinstance(folder, str) or not folder:
        return False
    if ext_type in _extension_handlers:
        return True
    extensions_dir = _init_args.get("extensions_dir")
    server_root = _init_args.get("server_root")
    fws_getter = _init_args.get("fws_getter")
    broadcast_fn = _init_args.get("broadcast_fn")
    transcript_fn = _init_args.get("transcript_fn")
    meta_fns = _init_args.get("meta_fns")
    if not isinstance(extensions_dir, Path) or not isinstance(server_root, Path):
        return False
    if not callable(fws_getter) or not callable(broadcast_fn) or not callable(transcript_fn):
        return False
    active_entries = _active_extensions_for_type(ext_type)
    handler = _load_handler(
        folder,
        ext_type,
        extensions_dir=extensions_dir,
        server_root=server_root,
        fws_getter=fws_getter,
        broadcast_fn=broadcast_fn,
        transcript_fn=transcript_fn,
        meta_fns=meta_fns,
        handler_extensions=active_entries,
    )
    if handler:
        _extension_handlers[ext_type] = handler
        return True
    return False


def set_extension_enabled(extension_id: str, enabled: bool) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    info["enabled"] = bool(enabled)
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def set_extension_dependency_result(extension_id: str, result: Optional[Dict[str, Any]]) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    payload = result if isinstance(result, dict) else {}
    status = str(payload.get("status") or ("met" if payload.get("ok") else "error")).strip().lower()
    if status not in {"met", "unmet", "error"}:
        status = "met" if payload.get("ok") else "error"
    message = payload.get("message")
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    info["dependency_status"] = status
    info["dependency_ok"] = status == "met"
    info["dependency_message"] = message if isinstance(message, str) else ""
    info["dependency_details"] = details
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def supports_dependency_check(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(isinstance(info, dict) and info.get("has_dependency_check"))


def supports_dependency_install(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(isinstance(info, dict) and info.get("has_dependency_install"))


def _dependency_module_for_extension(extension_id: str) -> Optional[Any]:
    info = _extensions_registry.get(extension_id)
    folder = info.get("path") if isinstance(info, dict) else None
    if not isinstance(folder, str) or not folder:
        return None
    module_path = f"extensions.{folder}.dependencies"
    return importlib.import_module(module_path)


async def _call_dependency_fn(func: Callable[..., Any], extension_id: str) -> Dict[str, Any]:
    result = func(extension_id=extension_id, extension_info=get_extension_info(extension_id))
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return result
    return {"ok": False, "status": "error", "message": "Invalid dependency result"}


async def check_extension_dependencies(extension_id: str) -> Dict[str, Any]:
    if not supports_dependency_check(extension_id):
        return {"ok": True, "status": "met", "message": "No dependency check required"}
    try:
        module = _dependency_module_for_extension(extension_id)
        func = getattr(module, "check_dependencies", None) if module else None
        if not callable(func):
            return {"ok": False, "status": "error", "message": "Dependency check contract missing"}
        return await _call_dependency_fn(func, extension_id)
    except Exception as e:
        return {"ok": False, "status": "error", "message": str(e)}


async def install_extension_dependencies(extension_id: str) -> Dict[str, Any]:
    if not supports_dependency_install(extension_id):
        return {"ok": False, "status": "failed", "message": "Dependency install not supported"}
    try:
        module = _dependency_module_for_extension(extension_id)
        func = getattr(module, "install_dependencies", None) if module else None
        if not callable(func):
            return {"ok": False, "status": "failed", "message": "Dependency install contract missing"}
        result = await _call_dependency_fn(func, extension_id)
        status = str(result.get("status") or ("succeeded" if result.get("ok") else "failed")).strip().lower()
        if status not in {"succeeded", "failed"}:
            status = "succeeded" if result.get("ok") else "failed"
        result["status"] = status
        result["ok"] = status == "succeeded"
        return result
    except Exception as e:
        return {"ok": False, "status": "failed", "message": str(e)}


def _extension_root(extension_id: str) -> Optional[Path]:
    info = _extensions_registry.get(extension_id)
    ext_path = info.get("path") if isinstance(info, dict) else None
    extensions_dir = _init_args.get("extensions_dir")
    if not isinstance(ext_path, str) or not ext_path:
        return None
    if not isinstance(extensions_dir, Path):
        return None
    return extensions_dir / ext_path


def get_static_settings_schema(extension_id: str) -> Optional[Dict[str, Any]]:
    """Load settings_schema.json through loader-owned extension metadata."""
    extension_root = _extension_root(extension_id)
    if extension_root is None:
        return None
    schema_file = extension_root / "settings_schema.json"
    if not schema_file.is_file():
        return None
    return json.loads(schema_file.read_text(encoding="utf-8"))


def is_initialized() -> bool:
    """Check if extensions have been loaded."""
    return _initialized


async def warm_up_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """
    Warm up all extensions that support it.
    Returns dict of extension_id -> success.
    """
    results: Dict[str, bool] = {}
    active_by_type: Dict[str, List[str]] = {}
    for ext_id, info in _extensions_registry.items():
        if not isinstance(info, dict) or not info.get("active"):
            continue
        ext_type = info.get("type")
        if isinstance(ext_type, str) and ext_type:
            active_by_type.setdefault(ext_type, []).append(ext_id)
    for handler_type, handler in _extension_handlers.items():
        active_ids = active_by_type.get(handler_type, [])
        if not active_ids:
            continue
        if hasattr(handler, "warm_up_all_extensions"):
            try:
                type_results = await handler.warm_up_all_extensions(timeout=timeout)
                if isinstance(type_results, dict) and type_results:
                    results.update(type_results)
                    ready = all(bool(type_results.get(ext_id, False)) for ext_id in active_ids)
                else:
                    ready = True
                for ext_id in active_ids:
                    results.setdefault(ext_id, ready)
            except Exception as e:
                print(f"[Extensions] Warm-up failed for {handler_type}: {e}")
                for ext_id in active_ids:
                    results[ext_id] = False
    return results


def is_extension_ready(extension_id: str) -> bool:
    """Check if an extension has completed warm-up."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    if not ext_info.get("active"):
        return False
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "is_extension_ready"):
        return handler.is_extension_ready(extension_id)
    
    return True  # Non-ACP extensions are always ready


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    """Wait for an extension to be ready."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    if not ext_info.get("active"):
        return False
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "wait_extension_ready"):
        return await handler.wait_extension_ready(extension_id, timeout=timeout)
    
    return True  # Non-ACP extensions are always ready


def requires_eager_session_init(extension_id: str) -> bool:
    """Deprecated — eager init removed. Sessions init on first message only."""
    return False


async def list_models(extension_id: str) -> Any:
    """List models for an extension. Handler must implement list_models()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "list_models"):
        return await handler.list_models()
    return {"models": []}


async def get_settings_schema(extension_id: str) -> Optional[Dict[str, Any]]:
    """Get a dynamic settings schema for an extension when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_settings_schema"):
        return await handler.get_settings_schema(extension_id=extension_id)
    return None


def _runtime_option_from_schema_field(field: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(field, dict):
        return None
    options_raw = field.get("options")
    options: List[Dict[str, Any]] = []
    if isinstance(options_raw, list):
        for item in options_raw:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    options.append({"value": text, "label": text})
                continue
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            label = item.get("label")
            if not isinstance(label, str) or not label.strip():
                label = value
            option: Dict[str, Any] = {
                "value": value.strip(),
                "label": label.strip(),
            }
            if item.get("deprecated") is True:
                option["deprecated"] = True
            options.append(option)
    return {
        "settingKey": field.get("id"),
        "label": field.get("label") or field.get("id") or "",
        "default": field.get("default") if isinstance(field.get("default"), str) else "",
        "options": options,
    }


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Get generic runtime-option descriptors for shared frontend controls."""
    ext_info = _extensions_registry.get(extension_id) or {}
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_runtime_options"):
        result = await handler.get_runtime_options(
            extension_id=extension_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        if isinstance(result, dict):
            result.setdefault("has_plan", bool(ext_info.get("has_plan")))
            result.setdefault("has_todo", bool(ext_info.get("has_todo")))
            return result
        return {
            "agent": extension_id,
            "has_plan": bool(ext_info.get("has_plan")),
            "has_todo": bool(ext_info.get("has_todo")),
        }

    schema = await get_settings_schema(extension_id)
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return {
            "agent": extension_id,
            "has_plan": bool(ext_info.get("has_plan")),
            "has_todo": bool(ext_info.get("has_todo")),
        }

    approval_field = next(
        (field for field in fields if isinstance(field, dict) and field.get("id") in {"approvalPolicy", "approval_policy"}),
        None,
    )
    sandbox_field = next(
        (field for field in fields if isinstance(field, dict) and field.get("id") in {"sandboxPolicy", "sandbox_policy", "sandbox"}),
        None,
    )
    return {
        "agent": extension_id,
        "has_plan": bool(ext_info.get("has_plan")),
        "has_todo": bool(ext_info.get("has_todo")),
        "approval": _runtime_option_from_schema_field(approval_field),
        "sandbox": _runtime_option_from_schema_field(sandbox_field),
    }


async def route_event(
    extension_id: str,
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Route a live backend event through an extension-owned router when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "route_event"):
        return await handler.route_event(
            extension_id=extension_id,
            label=label,
            payload=payload,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id=request_id,
        )
    return {"handled": False}


async def read_plan(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    """Read current plan state for an extension conversation when supported."""
    ext_info = _extensions_registry.get(extension_id) or {}
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "read_plan"):
        result = await handler.read_plan(
            extension_id=extension_id,
            conversation_id=conversation_id,
        )
        if isinstance(result, dict):
            result.setdefault("has_plan", bool(ext_info.get("has_plan")))
            result.setdefault("has_todo", bool(ext_info.get("has_todo")))
            return result
    return {
        "has_plan": bool(ext_info.get("has_plan")),
        "has_todo": bool(ext_info.get("has_todo")),
        "plan_exists": False,
        "plan_content": "",
        "plan_steps": [],
    }


async def list_sessions(extension_id: str, cwd: Optional[str] = None) -> Any:
    """List sessions for an extension. Handler must implement list_sessions()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "list_sessions"):
        return await handler.list_sessions(cwd=cwd)
    return []


async def resume_session_with_history(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resume a session and hydrate transcript. Handler must implement resume_session_with_history()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resume_session_with_history"):
        return await handler.resume_session_with_history(
            extension_id=extension_id,
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
    return {"ok": False, "error": f"Extension {extension_id} does not support session resume"}


async def hydrate_transcript(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Get flat transcript entries for an existing session (port-in).

    Handler must implement hydrate_transcript(session_id, conversation_id, ...).
    Returns a list of transcript entries in the standard format:
      {role: "user"|"assistant"|"reasoning"|"command"|"diff", text: "...", ...}
    Server.py writes these via _write_transcript_entries — same as bind-rollout.
    """
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "hydrate_transcript"):
        return await handler.hydrate_transcript(
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
    return []


def resolve_approval(extension_id: str, request_id: str, resolution: Any) -> bool:
    """Resolve an approval request. Handler must implement resolve_approval()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resolve_approval"):
        return bool(handler.resolve_approval(request_id, resolution))
    return False


def validate_pending_approval(
    extension_id: str,
    conversation_id: str,
    request_id: str,
    descriptor: Dict[str, Any],
) -> bool:
    """Validate whether a persisted approval is still actionable for an extension."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "validate_pending_approval"):
        return bool(handler.validate_pending_approval(conversation_id, request_id, descriptor))
    return False


async def shutdown_extension(extension_id: str) -> None:
    """Shutdown an extension. Handler must implement shutdown_client()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "shutdown_client"):
        await handler.shutdown_client()


async def interrupt_session(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    """Interrupt/abort the active turn for an extension session."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "abort_session"):
        ok = await handler.abort_session(conversation_id)
        return {"ok": ok, "conversation_id": conversation_id}
    return {"ok": False, "error": f"Extension {extension_id} does not support interrupt"}


def get_raw_buffer(extension_id: str, limit: int = 50) -> Any:
    """Get raw debug buffer. Handler must implement get_raw_buffer()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_raw_buffer"):
        return handler.get_raw_buffer(limit)
    return []
