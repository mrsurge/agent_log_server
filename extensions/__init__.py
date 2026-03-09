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

    for ext_info in discovered:
        if not ext_info.get("enabled", True):
            continue

        ext_id = ext_info["id"]
        ext_type = ext_info["type"]
        folder = ext_info["folder"]

        _extensions_registry[ext_id] = {
            "id": ext_id,
            "name": ext_info.get("name", ext_id),
            "type": ext_type,
            "path": folder,
        }

        if ext_type not in _extension_handlers:
            handler = _load_handler(folder, ext_type, **_init_args)
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

    try:
        init_fn(
            extensions_dir,
            server_root,
            fws_getter,
            broadcast_fn,
            transcript_fn,
            meta_fns,
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
    return extension_id in _extensions_registry


def list_extensions() -> List[Dict[str, Any]]:
    """List all registered extensions."""
    return list(_extensions_registry.values())


def is_initialized() -> bool:
    """Check if extensions have been loaded."""
    return _initialized


async def warm_up_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """
    Warm up all extensions that support it.
    Returns dict of extension_id -> success.
    """
    results: Dict[str, bool] = {}
    for handler_type, handler in _extension_handlers.items():
        if hasattr(handler, "warm_up_all_extensions"):
            try:
                type_results = await handler.warm_up_all_extensions(timeout=timeout)
                results.update(type_results)
            except Exception as e:
                print(f"[Extensions] Warm-up failed for {handler_type}: {e}")
    return results


def is_extension_ready(extension_id: str) -> bool:
    """Check if an extension has completed warm-up."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
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


def resolve_approval(extension_id: str, request_id: str, decision: str) -> None:
    """Resolve an approval request. Handler must implement resolve_approval()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resolve_approval"):
        handler.resolve_approval(request_id, decision)


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
