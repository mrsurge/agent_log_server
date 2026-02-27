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
from copilot.types import SessionHooks

from extensions.copilot_sdk_router import CopilotEventRouter, _looks_like_diff, _FILE_CHANGE_TOOLS


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


# ── Permission / Approval handler ───────────────────────────────────

# Pending approval futures: request_id -> asyncio.Future
_pending_approvals: Dict[str, asyncio.Future] = {}


def _get_conversation_settings(conversation_id: str) -> Dict[str, Any]:
    """Read settings from conversation meta.json."""
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta and isinstance(meta.get("settings"), dict):
            return meta["settings"]
    return {}


def resolve_approval(request_id: str, decision: str) -> None:
    """Called from WS handler when user responds to an approval request."""
    fut = _pending_approvals.pop(request_id, None)
    if fut and not fut.done():
        fut.set_result(decision)


def _build_preview_diff(payload: Dict[str, Any], args: Dict[str, Any]) -> None:
    """
    Compute a unified-diff preview from tool arguments and attach to payload.
    Supports edit-style tools (old_str/new_str) and create/write tools (file_text/content).
    Sets payload["diff"] and payload["path"] for the frontend's formatDiff().
    """
    import difflib

    file_path = args.get("path") or args.get("file_path") or args.get("file") or ""
    old_str = args.get("old_str")
    new_str = args.get("new_str")
    file_text = args.get("file_text") or args.get("content") or args.get("new_content")
    command = args.get("command") or args.get("cmd")

    if old_str is not None and new_str is not None:
        # edit/replace style — compute unified diff
        # Ensure trailing newlines so difflib produces separate lines
        old_text = str(old_str)
        new_text = str(new_str)
        if not old_text.endswith("\n"):
            old_text += "\n"
        if not new_text.endswith("\n"):
            new_text += "\n"
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile=file_path or "a", tofile=file_path or "b",
        )
        payload["diff"] = "".join(diff)
        if file_path:
            payload["path"] = file_path
    elif file_text is not None and file_path:
        # create/write style — show as full addition
        ft = str(file_text)
        if not ft.endswith("\n"):
            ft += "\n"
        new_lines = ft.splitlines(keepends=True)
        diff = difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null", tofile=file_path,
        )
        payload["diff"] = "".join(diff)
        payload["path"] = file_path
    elif command and file_path:
        # shell command on a file — just show command + path
        payload["path"] = file_path

def _make_permission_handler(conversation_id: str) -> Callable:
    """
    Create a permission handler for a session.

    Respects approval_policy from conversation settings:
      - auto-approve: silently approve everything
      - suggest: broadcast to frontend, auto-approve on timeout (120s)
      - always-ask: broadcast to frontend, wait indefinitely
    """
    async def handler(
        request: PermissionRequest,
        context: Dict[str, str],
    ) -> PermissionRequestResult:
        kind = request.get("kind", "unknown")
        tool_call_id = request.get("toolCallId", "")
        _add_to_raw_buffer("in", conversation_id, f"permission_request: {kind} tool={tool_call_id}")

        settings = _get_conversation_settings(conversation_id)
        policy = settings.get("approval_policy", "suggest")

        # Auto-approve: no user interaction needed
        if policy == "auto-approve":
            print(f"[CopilotSDK] Auto-approving {kind} tool={tool_call_id} convo={conversation_id[:8]}")
            return {"kind": "approved", "rules": []}

        print(f"[CopilotSDK] Permission request: kind={kind} tool={tool_call_id} policy={policy} convo={conversation_id[:8]}")

        # Build a unique request ID for this approval
        request_id = f"approval_{conversation_id[:8]}_{tool_call_id or id(request)}"

        # Look up tool context from the router if available
        router = _routers.get(conversation_id)
        tool_info = router.tool_calls.get(tool_call_id, {}) if router else {}

        # Build the payload the frontend expects
        payload: Dict[str, Any] = {"kind": kind}
        command = tool_info.get("title", "")
        if command:
            payload["command"] = command
        # Include tool name and raw arguments so frontend can render diffs
        tool_name = tool_info.get("tool_name", "")
        if tool_name:
            payload["tool_name"] = tool_name
        raw_args = tool_info.get("arguments")
        if raw_args:
            payload["arguments"] = raw_args

        # Compute a preview diff from tool arguments if possible
        if isinstance(raw_args, dict):
            _build_preview_diff(payload, raw_args)

        # Create a Future that the WS handler will resolve
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending_approvals[request_id] = fut

        # Broadcast approval_request to frontend
        if _broadcast_fn:
            await _broadcast_fn({
                "type": "approval",
                "conversation_id": conversation_id,
                "id": request_id,
                "kind": kind,
                "tool_call_id": tool_call_id,
                "turn_id": router.current_turn_id if router else "",
                "payload": payload,
            })

        # Wait based on policy
        if policy == "always-ask":
            # No timeout — wait indefinitely for user decision
            decision = await fut
        else:
            # "suggest" — auto-approve after 120s timeout
            try:
                decision = await asyncio.wait_for(fut, timeout=120.0)
            except asyncio.TimeoutError:
                _pending_approvals.pop(request_id, None)
                print(f"[CopilotSDK] Approval timeout for {request_id}, auto-approving")
                decision = "accept"

        if decision == "accept":
            return {"kind": "approved", "rules": []}
        else:
            return {"kind": "denied-interactively-by-user", "rules": []}

    return handler


# ── Pre-tool-use hook (sandbox + web policy) ────────────────────────

# Tool names known to perform web/network access
_WEB_TOOLS = {"web_search", "web_fetch", "fetch_url", "curl", "wget", "http_request"}

# Tool names known to perform file operations
_FILE_TOOLS = {"edit", "create", "write", "read_file", "write_file", "delete", "move",
               "bash", "shell", "exec", "run_command", "apply_patch"}


def _make_pre_tool_use_hook(conversation_id: str) -> Callable:
    """
    Create a pre_tool_use hook that enforces sandbox_policy and web_policy.

    sandbox_policy:
      - allow-all-paths: allow any file path
      - cwd-only: deny file ops outside cwd (default)
      - ask: prompt user for file ops outside cwd

    web_policy:
      - allow: allow web tools
      - deny: block web tools (default)
      - ask: prompt user for web tools
    """
    async def hook(
        input: Dict[str, Any],
        context: Dict[str, str],
    ) -> Optional[Dict[str, Any]]:
        tool_name = input.get("toolName", "")
        tool_args = input.get("toolArgs") or {}
        settings = _get_conversation_settings(conversation_id)

        # ── Web policy check ──
        web_policy = settings.get("web_policy", "deny")
        if tool_name.lower() in _WEB_TOOLS or any(w in tool_name.lower() for w in ("web", "fetch", "url", "http")):
            if web_policy == "deny":
                print(f"[CopilotSDK] Web tool '{tool_name}' denied by web_policy convo={conversation_id[:8]}")
                return {"permissionDecision": "deny", "permissionDecisionReason": "Web access denied by policy"}
            elif web_policy == "ask":
                return {"permissionDecision": "ask"}
            # "allow" → fall through

        # ── Sandbox / directory trust check ──
        sandbox_policy = settings.get("sandbox_policy", "cwd-only")
        if sandbox_policy != "allow-all-paths" and tool_name.lower() in _FILE_TOOLS:
            cwd = settings.get("cwd") or os.path.expanduser("~")
            cwd = os.path.realpath(os.path.expanduser(cwd))
            # Check path arguments
            target_path = None
            if isinstance(tool_args, dict):
                target_path = tool_args.get("path") or tool_args.get("file_path") or tool_args.get("file") or tool_args.get("command")
            if target_path and isinstance(target_path, str) and os.path.sep in target_path:
                real_target = os.path.realpath(os.path.expanduser(target_path))
                if not real_target.startswith(cwd + os.path.sep) and real_target != cwd:
                    if sandbox_policy == "cwd-only":
                        print(f"[CopilotSDK] Path '{target_path}' outside cwd, denied by sandbox_policy convo={conversation_id[:8]}")
                        return {"permissionDecision": "deny",
                                "permissionDecisionReason": f"Path outside working directory ({cwd})"}
                    elif sandbox_policy == "ask":
                        return {"permissionDecision": "ask"}

        # Allow by default
        return None

    return hook


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
        return {"ok": True, "session_id": session.session_id, "already_initialized": True}

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

        # Let SDK generate its own session_id (don't pass ours)
        session = await client.create_session(
            SessionConfig(
                streaming=True,
                working_directory=cwd,
                on_permission_request=_make_permission_handler(conversation_id),
                hooks=SessionHooks(
                    on_pre_tool_use=_make_pre_tool_use_hook(conversation_id),
                ),
            ),
        )
        sdk_session_id = session.session_id
        _sessions[conversation_id] = session

        # Subscribe to events
        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        # Store SDK session_id as thread_id in conversation meta (like codex)
        if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
            meta = _meta_fns["load"](conversation_id)
            if meta:
                meta["thread_id"] = sdk_session_id
                meta["status"] = "active"
                _meta_fns["save"](conversation_id, meta)

        print(f"[CopilotSDK] Session created: convo={conversation_id[:8]} sdk_session={sdk_session_id[:8]} cwd={cwd}")
        _add_to_raw_buffer("out", conversation_id, f"session_created sdk={sdk_session_id[:8]} cwd={cwd}")
        return {"ok": True, "session_id": sdk_session_id}

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
    
    Looks up the SDK session_id from meta["thread_id"], resumes via SDK,
    and keys in-memory state by conversation_id.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # Already active in memory?
    if conversation_id in _sessions:
        s = _sessions[conversation_id]
        return {"ok": True, "session_id": s.session_id, "already_active": True}

    # Look up SDK session ID from conversation meta
    sdk_session_id = None
    if _meta_fns and "load" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        if meta:
            sdk_session_id = meta.get("thread_id")

    if not sdk_session_id:
        return {"ok": False, "error": f"No thread_id (SDK session) for conversation {conversation_id[:8]}"}

    try:
        client = await _ensure_client()

        # Create router keyed by our conversation_id
        router = CopilotEventRouter(
            conversation_id=conversation_id,
            broadcast_fn=_broadcast_fn,
            transcript_fn=_transcript_fn,
        )
        _routers[conversation_id] = router

        config: ResumeSessionConfig = {
            "streaming": True,
            "on_permission_request": _make_permission_handler(conversation_id),
            "hooks": SessionHooks(
                on_pre_tool_use=_make_pre_tool_use_hook(conversation_id),
            ),
        }
        if cwd:
            resolved = os.path.expanduser(cwd) if cwd.startswith("~") else cwd
            config["working_directory"] = resolved
        if model:
            config["model"] = model

        # Resume using the real SDK session ID
        session = await client.resume_session(
            sdk_session_id,
            config,
        )
        # Key in-memory by our conversation_id
        _sessions[conversation_id] = session

        unsub = session.on(_make_event_handler(conversation_id))
        _unsubs[conversation_id] = unsub

        print(f"[CopilotSDK] Session resumed: convo={conversation_id[:8]} sdk_session={sdk_session_id[:8]}")
        _add_to_raw_buffer("out", conversation_id, f"session_resumed sdk={sdk_session_id[:8]}")
        return {"ok": True, "session_id": sdk_session_id}

    except Exception as e:
        print(f"[CopilotSDK] resume_session failed: {e}")
        # If resume fails (session doesn't exist on SDK side), create a new one
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
    Follows the codex pattern: lazy resume on first message, not on conversation select.
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    cwd = settings.get("cwd") or os.path.expanduser("~")

    # Ensure session exists — lazy resume or init
    if conversation_id not in _sessions:
        # Check if this conversation already has a thread_id (needs resume, not init)
        thread_id = None
        if _meta_fns and "load" in _meta_fns:
            meta = _meta_fns["load"](conversation_id)
            if meta:
                thread_id = meta.get("thread_id")

        if thread_id:
            # Existing session — resume it (like codex thread/resume)
            result = await resume_session(conversation_id, cwd=cwd, model=settings.get("model"))
        else:
            # Brand new conversation — create a fresh session
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
        def _safe(obj):
            """Recursively convert SDK objects to JSON-safe dicts/primitives."""
            if obj is None or isinstance(obj, (str, int, float, bool)):
                return obj
            if isinstance(obj, dict):
                return {k: _safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_safe(v) for v in obj]
            if hasattr(obj, "__dict__"):
                return {k: _safe(v) for k, v in vars(obj).items() if not k.startswith("_")}
            return str(obj)

        return [
            {
                "id": m.id,
                "name": getattr(m, "name", m.id),
                "billing": _safe(getattr(m, "billing", None)),
                "capabilities": _safe(getattr(m, "capabilities", None)),
                "policy": _safe(getattr(m, "policy", None)),
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
    Bind a Copilot SDK session to a conversation.

    Session picker flow: user picks an existing SDK session for a new internal
    conversation.  We:
      1. Write thread_id into meta (binding).
      2. Resume the SDK session (so it's live for future messages).
    Transcript hydration is handled separately by hydrate_transcript().
    """
    if not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "Manager not initialized"}

    # 1. Bind thread_id into meta
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

    # 2. Resume the SDK session (creates in-memory session + router)
    result = await resume_session(conversation_id, cwd=cwd, model=model)
    if not result.get("ok"):
        print(f"[CopilotSDK] resume_session_with_history: resume failed: {result}")
        return result

    print(f"[CopilotSDK] Bound session {session_id[:8]} to convo {conversation_id[:8]}")
    return {
        "ok": True,
        "session_id": session_id,
        "conversation_id": conversation_id,
    }


async def hydrate_transcript(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Build flat transcript entries from an existing SDK session's history.

    This is the SDK equivalent of _rollout_preview_entries() for Codex.
    Calls get_messages() on the SDK session and converts each SessionEvent
    into the standard transcript entry format that _write_transcript_entries
    expects: {role, text, ts, ...}.

    Returns a list — server.py writes them to transcript.jsonl.
    """
    from copilot.generated.session_events import SessionEventType

    # Ensure session is resumed so we can call get_messages()
    session = _sessions.get(conversation_id)
    if not session:
        # Try resuming first
        result = await resume_session(conversation_id, cwd=cwd, model=model)
        if not result.get("ok"):
            print(f"[CopilotSDK] hydrate_transcript: resume failed: {result}")
            return []
        session = _sessions.get(conversation_id)
    if not session:
        return []

    try:
        events = await session.get_messages()
    except Exception as e:
        print(f"[CopilotSDK] hydrate_transcript get_messages failed: {e}")
        return []

    print(f"[CopilotSDK] hydrate_transcript: got {len(events)} events for {conversation_id[:8]}")

    items: List[Dict[str, Any]] = []
    ts_now = datetime.now(timezone.utc).isoformat()

    for ev in events:
        try:
            etype = ev.type
            data = ev.data

            if etype == SessionEventType.USER_MESSAGE:
                text = getattr(data, "content", None) or getattr(data, "message", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "user", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.ASSISTANT_MESSAGE:
                text = getattr(data, "content", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "assistant", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.ASSISTANT_REASONING:
                text = getattr(data, "reasoning_text", None) or ""
                if isinstance(text, str) and text.strip():
                    items.append({"role": "reasoning", "text": text.strip(), "ts": ts_now})

            elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
                tool_name = getattr(data, "tool_name", None) or getattr(data, "name", None) or ""
                result_obj = getattr(data, "result", None)
                content = ""
                detailed = ""
                if result_obj and hasattr(result_obj, "content"):
                    content = getattr(result_obj, "content", "") or ""
                    detailed = getattr(result_obj, "detailed_content", "") or ""
                elif isinstance(result_obj, str):
                    content = result_obj
                else:
                    content = str(result_obj or "")
                file_path = getattr(data, "path", None) or ""
                items.append({
                    "role": "command",
                    "command": str(tool_name),
                    "output": content,
                    "exit_code": 0,
                    "ts": ts_now,
                })
                # Append diff only for file-mutating tools
                if detailed and tool_name.lower() in _FILE_CHANGE_TOOLS:
                    items.append({
                        "role": "diff",
                        "path": file_path,
                        "text": detailed,
                        "ts": ts_now,
                    })

            elif etype == SessionEventType.ASSISTANT_USAGE:
                total = getattr(data, "output_tokens", None)
                # Record usage if available, but not critical for hydration
                pass

            # Skip delta events, turn lifecycle, session events — they're
            # intermediate; we only care about completed items for hydration.

        except Exception as ev_err:
            print(f"[CopilotSDK] hydrate_transcript: skipping event: {ev_err}")

    print(f"[CopilotSDK] hydrate_transcript: built {len(items)} transcript entries")
    return items


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
    """Delete our side of the conversation only.
    
    Like codex: removing a conversation removes our meta/transcript,
    but the SDK session persists and can be resumed by a new conversation
    via the session picker.
    """
    return await destroy_session(conversation_id)


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
