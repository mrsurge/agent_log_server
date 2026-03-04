"""
Codex App Server Client

Manages the codex app-server binary via framework_shells pipe transport.
This is the handler module that ext_loader discovers and initializes.

The binary communicates via JSON-RPC over stdin/stdout.  Most heavy lifting
is still in server.py's _route_appserver_event (legacy); the collab event
routing has been extracted into router.py.
"""

from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Stored references to server callbacks
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[Dict[str, Callable]] = None
_fws_getter: Optional[Callable] = None
_extensions_dir: Optional[Path] = None
_server_root: Optional[Path] = None


def init_codex_app_server_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> None:
    """
    Initialize the codex app-server extension.

    Called by ext_loader during startup.  Stores callback references for
    later use when handling messages and routing events.
    """
    global _broadcast_fn, _transcript_fn, _meta_fns
    global _fws_getter, _extensions_dir, _server_root

    _extensions_dir = extensions_dir
    _server_root = server_root
    _fws_getter = fws_getter
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns

    print("[Codex] Extension initialized (app-server binary handler)")
