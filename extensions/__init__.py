"""
Extension Loader

Loads pluggable agent extensions from extensions/extensions.json.
Each extension type maps to a handler module that implements handle_message().

Currently supported types:
- "copilot_sdk": Copilot SDK extensions (GitHub Copilot CLI) -> extensions.copilot_sdk_client
- "acp": ACP protocol extensions (legacy, deprecated) -> extensions.acp_client
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Extension type -> handler module mapping
_extension_handlers: Dict[str, Any] = {}
_extensions_registry: Dict[str, Dict[str, Any]] = {}  # extension_id -> manifest info
_initialized: bool = False


def load_extensions(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> None:
    """
    Load all extensions from extensions.json and initialize handlers.
    
    Args:
        extensions_dir: Path to static/extensions/
        server_root: Path to server root
        fws_getter: Async function to get framework_shells manager
        broadcast_fn: Async function to broadcast WebSocket events
        transcript_fn: Async function to append transcript entries
        meta_fns: Optional dict with "load" and "save" functions for conversation meta
    """
    global _extension_handlers, _extensions_registry, _initialized
    
    extensions_json = extensions_dir / "extensions.json"
    if not extensions_json.exists():
        print("[Extensions] No extensions.json found")
        _initialized = True
        return
    
    try:
        data = json.loads(extensions_json.read_text())
    except Exception as e:
        print(f"[Extensions] Failed to load extensions.json: {e}")
        _initialized = True
        return
    
    # Load each extension
    for ext_info in data.get("extensions", []):
        if not ext_info.get("enabled", True):
            continue
        
        ext_id = ext_info.get("id", "")
        ext_type = ext_info.get("type", "")
        
        if not ext_id or not ext_type:
            continue
        
        # Store in registry
        _extensions_registry[ext_id] = {
            "id": ext_id,
            "name": ext_info.get("name", ext_id),
            "type": ext_type,
            "path": ext_info.get("path", ""),
        }
        
        # Initialize handler for this type if not already done
        if ext_type not in _extension_handlers:
            handler = _load_handler_for_type(
                ext_type,
                extensions_dir,
                server_root,
                fws_getter,
                broadcast_fn,
                transcript_fn,
                meta_fns,
            )
            if handler:
                _extension_handlers[ext_type] = handler
    
    _initialized = True
    print(f"[Extensions] Loaded {len(_extensions_registry)} extension(s): {list(_extensions_registry.keys())}")


def _load_handler_for_type(
    ext_type: str,
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]],
) -> Optional[Any]:
    """Load the handler module for an extension type."""
    if ext_type == "copilot_sdk":
        try:
            from extensions import copilot_sdk_client
            copilot_sdk_client.init_copilot_manager(
                extensions_dir,
                server_root,
                fws_getter,
                broadcast_fn,
                transcript_fn,
                meta_fns,
            )
            return copilot_sdk_client
        except Exception as e:
            print(f"[Extensions] Failed to load Copilot SDK handler: {e}")
            import traceback
            traceback.print_exc()
            return None

    if ext_type == "acp":
        try:
            from extensions import acp_client
            acp_client.init_acp_manager(
                extensions_dir,
                server_root,
                fws_getter,
                broadcast_fn,
                transcript_fn,
                meta_fns,
            )
            return acp_client
        except Exception as e:
            print(f"[Extensions] Failed to load ACP handler: {e}")
            return None
    print(f"[Extensions] Unknown extension type: {ext_type}")
    return None


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
    """Check if an extension requires eager session initialization on settings save."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "requires_eager_session_init"):
        return handler.requires_eager_session_init(extension_id)
    
    return False


async def init_session(
    conversation_id: str,
    extension_id: str,
    cwd: str,
) -> Dict[str, Any]:
    """
    Initialize a session for an extension that requires eager init.
    
    Called when settings are saved for an extension with eagerSessionInit=true.
    """
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return {"ok": False, "error": f"Unknown extension: {extension_id}"}
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "init_session"):
        return await handler.init_session(conversation_id, extension_id, cwd)
    
    return {"ok": True}  # No-op for extensions that don't need it


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
) -> Dict[str, Any]:
    """Resume a session and hydrate transcript. Handler must implement resume_session_with_history()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resume_session_with_history"):
        return await handler.resume_session_with_history(
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
        )
    return {"ok": False, "error": f"Extension {extension_id} does not support session resume"}


async def hydrate_transcript(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
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


def get_raw_buffer(extension_id: str, limit: int = 50) -> Any:
    """Get raw debug buffer. Handler must implement get_raw_buffer()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_raw_buffer"):
        return handler.get_raw_buffer(limit)
    return []
