"""
Extension Loader

Loads pluggable agent extensions from extensions/extensions.json.
Each extension type maps to a handler module that implements handle_message().

Currently supported types:
- "acp": ACP protocol extensions (Gemini CLI, etc.) -> extensions.acp_client
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
    
    # Future: add more extension types here
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
    Warm up all ACP extensions (start shells, wait for ready).
    
    Call this on server startup to eagerly start slow-loading agents like Gemini.
    Returns dict of extension_id -> success.
    """
    results: Dict[str, bool] = {}
    
    # Warm up ACP extensions
    if "acp" in _extension_handlers:
        handler = _extension_handlers["acp"]
        if hasattr(handler, "warm_up_all_extensions"):
            acp_results = await handler.warm_up_all_extensions(timeout=timeout)
            results.update(acp_results)
    
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
