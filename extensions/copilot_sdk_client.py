"""
Copilot SDK Client Handler for Extension System

Manages Copilot CLI agent sessions via the github-copilot-sdk Python package.
Replaces the ACP client handler with a cleaner, SDK-managed approach.

Key advantages over ACP:
- Session resume built-in (client.resume_session)
- SDK manages CLI process lifecycle (no shellspec/FWS pipe needed)
- Streaming via SessionConfig.streaming=True
- All Copilot models including Gemini
- Rich event model via session.on(handler)
"""

import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Awaitable

from copilot import (
    CopilotClient,
    CopilotSession,
    SessionConfig,
    ResumeSessionConfig,
    MessageOptions,
    SessionEvent,
    PermissionRequest,
    PermissionRequestResult,
)

from extensions.copilot_sdk_router import CopilotEventRouter


# ── Global state ────────────────────────────────────────────────────

_client: Optional[CopilotClient] = None
_client_lock: Optional[asyncio.Lock] = None


def _get_client_lock() -> asyncio.Lock:
    """Lazy-init the lock on first use (inside the running event loop)."""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock

# Server callbacks (injected by init_copilot_manager)
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[Dict[str, Callable]] = None

# Session tracking: conversation_id -> CopilotSession
_sessions: Dict[str, CopilotSession] = {}
# Router tracking: conversation_id -> CopilotEventRouter
_routers: Dict[str, CopilotEventRouter] = {}
# Event unsubscribe fns: conversation_id -> unsubscribe callable
_unsubs: Dict[str, Callable] = {}

# Ready state
_ready_event: Optional[asyncio.Event] = None
_initialized: bool = False

# Debug buffer (circular)
_raw_buffer: List[Dict[str, Any]] = []
_RAW_BUFFER_MAX = 200


def _add_to_raw_buffer(direction: str, conversation_id: str, data: Any) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dir": direction,
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": data if isinstance(data, str) else str(data)[:500],
    }
    _raw_buffer.append(entry)
    if len(_raw_buffer) > _RAW_BUFFER_MAX:
        _raw_buffer.pop(0)


def get_raw_buffer(limit: int = 50) -> List[Dict[str, Any]]:
    return _raw_buffer[-limit:]


# ── Permission handler ──────────────────────────────────────────────

def _make_permission_handler(conversation_id: str) -> Callable:
    """
    Create a permission handler for a session.
    
    Currently auto-approves all requests. Future: route to frontend
    approval UI via broadcast_fn.
    """
    def handler(
        request: PermissionRequest,
        context: Dict[str, str],
    ) -> PermissionRequestResult:
        kind = request.get("kind", "unknown")
        tool_call_id = request.get("toolCallId", "")
        _add_to_raw_buffer("in", conversation_id, f"permission_request: {kind} tool={tool_call_id}")
        print(f"[CopilotSDK] Permission request: kind={kind} tool={tool_call_id} convo={conversation_id[:8]}")
        # Auto-approve
        return {"kind": "approved", "rules": []}

    return handler


# ── Event handler factory ───────────────────────────────────────────

def _make_event_handler(conversation_id: str) -> Callable[[SessionEvent], None]:
    """Create an event handler that routes SessionEvents to the conversation's router."""
    def handler(event: SessionEvent) -> None:
        _add_to_raw_buffer("in", conversation_id, f"{event.type.value}: {str(event.data)[:200]}")
        router = _routers.get(conversation_id)
        if router:
            # Schedule the coroutine on the running event loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(router.route_event(event))
            except RuntimeError:
                print(f"[CopilotSDK] No running loop for event routing: {event.type.value}")
        else:
            print(f"[CopilotSDK] No router for {conversation_id[:8]}, event: {event.type.value}")

    return handler


# ── Initialization ──────────────────────────────────────────────────

def init_copilot_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> None:
    """
    Initialize the Copilot SDK manager with server callbacks.
    
    Called by extensions/__init__.py during load_extensions().
    The fws_getter is accepted for interface compat but not used —
    the SDK manages its own CLI process.
    """
    global _broadcast_fn, _transcript_fn, _meta_fns, _initialized
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns or {}
    _initialized = True
    print("[CopilotSDK] Manager initialized")


async def _ensure_client() -> CopilotClient:
    """Get or create the global CopilotClient singleton."""
    global _client
    async with _get_client_lock():
        if _client is None:
            _client = CopilotClient({
                "use_stdio": True,
                "auto_start": True,
                "auto_restart": True,
                "log_level": "info",
            })
            await _client.start()
            print(f"[CopilotSDK] Client started, state={_client.get_state()}")
        return _client


# ── Warm-up ─────────────────────────────────────────────────────────

async def warm_up_extension(
    extension_id: str,
    timeout: float = 60.0,
) -> bool:
    """
    Start the CopilotClient and verify it's responsive.
    Much faster than ACP warm-up since SDK manages the process itself.
    """
    global _ready_event

    if _ready_event and _ready_event.is_set():
        return True

    _ready_event = asyncio.Event()

    try:
        client = await asyncio.wait_for(_ensure_client(), timeout=timeout)
        ping = await client.ping("warmup")
        print(f"[CopilotSDK] Warm-up ping: {ping}")
        _ready_event.set()
        return True
    except Exception as e:
        print(f"[CopilotSDK] Warm-up failed: {e}")
        return False


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """Warm up the Copilot SDK client."""
    result = await warm_up_extension("copilot-sdk", timeout=timeout)
    return {"copilot-sdk": result}


def is_extension_ready(extension_id: str) -> bool:
    return _ready_event.is_set() if _ready_event else False


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    if _ready_event and _ready_event.is_set():
        return True
    return await warm_up_extension(extension_id, timeout=timeout)


# ── Session management ──────────────────────────────────────────────

def requires_eager_session_init(extension_id: str) -> bool:
    """Copilot SDK supports eager session init."""
    return True


def has_session(conversation_id: str) -> bool:
    return conversation_id in _sessions


async def init_session(
    conversation_id: str,
    extension_id: str,
    cwd: str,
) -> Dict[str, Any]:
    """
    Create a new Copilot session for a conversation.
    
    Called eagerly on settings save (eagerSessionInit) or on first message.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Copilot SDK manager not initialized"}

    # Already has session?
    if conversation_id in _sessions:
        session = _sessions[conversation_id]
        return {"ok": True, "session_id": conversation_id, "already_initialized": True}

    if cwd.startswith("~"):
        cwd = os.path.expanduser(cwd)

    try:
        client = await _ensure_client()

        # Create router for this conversation
        router = CopilotEventRouter(
            conversation_id=conversation_id,
            broadcast_fn=_broadcast_fn,
            transcript_fn=_transcript_fn,
        )
        _routers[conversation_id] = router

        # Create session with our conversation_id as session_id for easy resume
        session = await client.create_session(
            SessionConfig(
                session_id=conversation_id,
                streaming=True,
                working_directory=cwd,
                on_permission_request=_make_permission_handler(conversation_id),
            ),
        )
        _sessions[conversation_id] = session

        # Subscribe to events
        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        # Store session info in conversation meta
        if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
            meta = _meta_fns["load"](conversation_id)
            if meta:
                meta["thread_id"] = conversation_id
                meta["status"] = "active"
                _meta_fns["save"](conversation_id, meta)

        print(f"[CopilotSDK] Session created: {conversation_id[:8]} cwd={cwd}")
        _add_to_raw_buffer("out", conversation_id, f"session_created cwd={cwd}")
        return {"ok": True, "session_id": conversation_id}

    except Exception as e:
        print(f"[CopilotSDK] init_session failed: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


async def resume_session(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resume an existing Copilot session (survives server restarts).
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # Already active in memory?
    if conversation_id in _sessions:
        return {"ok": True, "session_id": conversation_id, "already_active": True}

    try:
        client = await _ensure_client()

        # Create router
        router = CopilotEventRouter(
            conversation_id=conversation_id,
            broadcast_fn=_broadcast_fn,
            transcript_fn=_transcript_fn,
        )
        _routers[conversation_id] = router

        config: ResumeSessionConfig = {
            "streaming": True,
            "on_permission_request": _make_permission_handler(conversation_id),
        }
        if cwd:
            resolved = os.path.expanduser(cwd) if cwd.startswith("~") else cwd
            config["working_directory"] = resolved
        if model:
            config["model"] = model

        session = await client.resume_session(
            conversation_id,
            config,
        )
        _sessions[conversation_id] = session

        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        print(f"[CopilotSDK] Session resumed: {conversation_id[:8]}")
        _add_to_raw_buffer("out", conversation_id, "session_resumed")
        return {"ok": True, "session_id": conversation_id}

    except Exception as e:
        print(f"[CopilotSDK] resume_session failed: {e}")
        # If resume fails (session doesn't exist on disk), create a new one
        cwd_resolved = cwd or os.path.expanduser("~")
        return await init_session(conversation_id, "copilot-sdk", cwd_resolved)


# ── Message handling ────────────────────────────────────────────────

async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle a user message for a Copilot SDK conversation.
    
    Main entry point called by server.py extension router.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    cwd = settings.get("cwd") or os.path.expanduser("~")

    # Ensure session exists
    if conversation_id not in _sessions:
        result = await init_session(conversation_id, agent_type, cwd)
        if not result.get("ok"):
            return result

    session = _sessions.get(conversation_id)
    if not session:
        return {"ok": False, "error": "Session not found after init"}

    router = _routers.get(conversation_id)
    if not router:
        return {"ok": False, "error": "Router not found"}

    # Notify router of turn start
    await router.on_turn_start(text)

    try:
        _add_to_raw_buffer("out", conversation_id, f"prompt: {text[:200]}")

        # Fire-and-forget send — events come via session.on() handler
        await session.send(
            MessageOptions(prompt=text),
        )

        return {"ok": True, "session_id": conversation_id}

    except Exception as e:
        print(f"[CopilotSDK] send failed: {e}")
        _add_to_raw_buffer("out", conversation_id, f"send_error: {e}")
        return {"ok": False, "error": str(e)}


# ── Model listing ───────────────────────────────────────────────────

async def list_models() -> List[Dict[str, Any]]:
    """List available models from the Copilot CLI."""
    try:
        client = await _ensure_client()
        models = await client.list_models()
        return [
            {
                "id": m.id,
                "name": getattr(m, "name", m.id),
                "billing": getattr(m, "billing", None),
                "capabilities": {
                    "supports_reasoning_effort": getattr(
                        getattr(m, "capabilities", None), "supports", {}
                    ) if hasattr(m, "capabilities") else {},
                },
                "policy": {
                    "is_default": getattr(getattr(m, "policy", None), "is_default", False)
                    if hasattr(m, "policy") else False,
                },
            }
            for m in models
        ]
    except Exception as e:
        print(f"[CopilotSDK] list_models failed: {e}")
        return []


# ── Session listing ─────────────────────────────────────────────────

async def list_sessions(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all known sessions from the Copilot CLI.
    
    If cwd is provided, sessions are sorted with CWD-matching sessions first.
    """
    try:
        client = await _ensure_client()
        sessions = await client.list_sessions()
        
        result = []
        for s in sessions:
            entry: Dict[str, Any] = {
                "session_id": s.sessionId,
                "start_time": getattr(s, "startTime", None),
                "modified_time": getattr(s, "modifiedTime", None),
                "is_remote": getattr(s, "isRemote", False),
                "summary": getattr(s, "summary", None),
            }
            # Check if session has context (newer SDK versions)
            ctx = getattr(s, "context", None)
            if ctx:
                entry["context"] = {
                    "cwd": getattr(ctx, "cwd", None),
                    "git_root": getattr(ctx, "gitRoot", None),
                    "repository": getattr(ctx, "repository", None),
                    "branch": getattr(ctx, "branch", None),
                }
            # Check if this session is currently active in our server
            entry["active"] = s.sessionId in _sessions
            result.append(entry)
        
        # Sort: CWD-matching first, then by modified_time descending
        if cwd:
            resolved_cwd = os.path.expanduser(cwd) if cwd.startswith("~") else cwd
            resolved_cwd = os.path.realpath(resolved_cwd)
            
            def relevance(entry):
                ctx = entry.get("context") or {}
                session_cwd = ctx.get("cwd") or ""
                session_git = ctx.get("git_root") or ""
                # Exact CWD match = highest priority
                if session_cwd and os.path.realpath(session_cwd) == resolved_cwd:
                    return 0
                # Same git root = next priority
                if session_git:
                    try:
                        import subprocess
                        git_root = subprocess.run(
                            ["git", "-C", resolved_cwd, "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True, timeout=2
                        ).stdout.strip()
                        if git_root and os.path.realpath(session_git) == os.path.realpath(git_root):
                            return 1
                    except Exception:
                        pass
                # Fallback: check if CWD is a parent/child of session CWD
                if session_cwd:
                    r = os.path.realpath(session_cwd)
                    if r.startswith(resolved_cwd) or resolved_cwd.startswith(r):
                        return 2
                return 9
            
            for entry in result:
                entry["_relevance"] = relevance(entry)
            # Sort: relevance ascending, modified_time descending within group
            from functools import cmp_to_key
            def _session_cmp(a, b):
                ra, rb = a["_relevance"], b["_relevance"]
                if ra != rb:
                    return -1 if ra < rb else 1
                ma, mb = a.get("modified_time") or "", b.get("modified_time") or ""
                if ma != mb:
                    return 1 if ma < mb else -1
                return 0
            result.sort(key=cmp_to_key(_session_cmp))
            for entry in result:
                entry.pop("_relevance", None)
        
        return result
    except Exception as e:
        print(f"[CopilotSDK] list_sessions failed: {e}")
        return []


async def resume_session_with_history(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resume a Copilot session and load its history into a conversation transcript.
    
    This is the main entry point for the session picker flow:
    1. Resume the SDK session (reconnect to persisted state)
    2. Fetch message history via session.get_messages()
    3. Convert to transcript entries and write them
    4. Bind session_id as the conversation's thread_id
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # Resume the session
    result = await resume_session(session_id, cwd=cwd, model=model)
    if not result.get("ok"):
        return result

    session = _sessions.get(session_id)
    if not session:
        return {"ok": False, "error": "Session not in memory after resume"}

    # Fetch conversation history from the SDK
    try:
        messages = await session.get_messages()
    except Exception as e:
        print(f"[CopilotSDK] get_messages failed: {e}")
        messages = []

    # Convert SessionEvents to transcript entries
    transcript_items = []
    for msg in messages:
        entry = _session_event_to_transcript(msg)
        if entry:
            transcript_items.append(entry)

    # Update conversation meta
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta:
            meta["thread_id"] = session_id
            meta["status"] = "active"
            settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
            settings["agent"] = "copilot-sdk"
            if cwd:
                settings["cwd"] = cwd
            if model:
                settings["model"] = model
            meta["settings"] = settings
            _meta_fns["save"](conversation_id, meta)

    # Remap the session to conversation_id if different
    if session_id != conversation_id:
        _sessions[conversation_id] = _sessions.pop(session_id, session)
        router = _routers.pop(session_id, None)
        if router:
            router.conversation_id = conversation_id
            _routers[conversation_id] = router
        unsub = _unsubs.pop(session_id, None)
        if unsub:
            _unsubs[conversation_id] = unsub

    print(f"[CopilotSDK] Resumed session {session_id[:8]} into convo {conversation_id[:8]}, {len(transcript_items)} history items")
    return {
        "ok": True,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "history_count": len(transcript_items),
        "items": transcript_items,
    }


def _session_event_to_transcript(event) -> Optional[Dict[str, Any]]:
    """Convert a SessionEvent from get_messages() to a transcript entry."""
    from copilot import SessionEventType
    
    etype = event.type
    data = event.data
    ts = getattr(event, "timestamp", None) or ""

    if etype == SessionEventType.ASSISTANT_MESSAGE:
        content = getattr(data, "content", None) or ""
        if content:
            return {"role": "assistant", "text": content, "timestamp": ts, "id": str(event.id)}
    
    elif etype == SessionEventType.ASSISTANT_REASONING:
        text = getattr(data, "reasoning_text", None) or getattr(data, "content", None) or ""
        if text:
            return {"role": "reasoning", "text": text, "timestamp": ts, "id": str(event.id)}

    elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
        content = getattr(data, "content", None) or ""
        source = getattr(data, "source", None) or "tool"
        return {
            "role": "command",
            "command": source,
            "output": content,
            "status": "completed",
            "timestamp": ts,
            "id": str(event.parent_id or event.id),
        }

    elif etype == SessionEventType.USER_MESSAGE:
        content = getattr(data, "content", None) or ""
        if content:
            return {"role": "user", "text": content, "timestamp": ts, "id": str(event.id)}

    return None


# ── Cleanup ─────────────────────────────────────────────────────────

async def destroy_session(conversation_id: str) -> bool:
    """Destroy a session (keeps data on disk for resume)."""
    unsub = _unsubs.pop(conversation_id, None)
    if unsub:
        unsub()

    session = _sessions.pop(conversation_id, None)
    _routers.pop(conversation_id, None)

    if session:
        try:
            await session.destroy()
            print(f"[CopilotSDK] Session destroyed: {conversation_id[:8]}")
            return True
        except Exception as e:
            print(f"[CopilotSDK] destroy_session error: {e}")
    return False


async def delete_session(conversation_id: str) -> bool:
    """Permanently delete a session and its data."""
    await destroy_session(conversation_id)
    try:
        client = await _ensure_client()
        await client.delete_session(conversation_id)
        print(f"[CopilotSDK] Session deleted: {conversation_id[:8]}")
        return True
    except Exception as e:
        print(f"[CopilotSDK] delete_session error: {e}")
        return False


async def stop_client() -> None:
    """Stop the global CopilotClient (server shutdown)."""
    global _client
    if _client:
        try:
            errors = await _client.stop()
            if errors:
                print(f"[CopilotSDK] Stop errors: {errors}")
            _client = None
            print("[CopilotSDK] Client stopped")
        except Exception as e:
            print(f"[CopilotSDK] stop_client error: {e}")


# ── Abort ───────────────────────────────────────────────────────────

async def abort_session(conversation_id: str) -> bool:
    """Abort the current request in a session."""
    session = _sessions.get(conversation_id)
    if not session:
        return False
    try:
        await session.abort()
        print(f"[CopilotSDK] Aborted: {conversation_id[:8]}")
        return True
    except Exception as e:
        print(f"[CopilotSDK] abort error: {e}")
        return False


# ── Shutdown alias (for server.py lifespan) ─────────────────────────

shutdown_client = stop_client
