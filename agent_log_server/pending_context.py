"""
Pending Context Update System

Watches repo-local `.repo_memory.md` files via Linux inotify and queues
PendingContextUpdate entries for matching conversations. Matching is based
on conversation meta (agent type, TE2 integration, cwd -> repo memory root),
not the server process cwd.

Only targeted conversations with a valid repo memory file are tracked.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import hashlib
import os
import struct
import sys
import time
from pathlib import Path
from collections.abc import Callable
from typing import TypedDict

from .prompt_context import REPO_MEMORY_FILENAME, load_repo_memory_snapshot
from .repo_memory_delta import build_repo_memory_delta
from .te2_mcp_config import te2_mcp_integration_enabled
from .typing_helpers import ObjectMap, coerce_object_map

# -- Configuration -----------------------------------------------------

TARGETED_AGENT_TYPES = frozenset({"codex-ext-exp"})

# inotify event masks
IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
_WATCH_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_TO

# inotify event struct: wd(i32) + mask(u32) + cookie(u32) + len(u32) = 16 bytes header
_EVENT_HEADER_SIZE = 16
_EVENT_HEADER_FMT = "iIII"

# -- Types -------------------------------------------------------------

# PendingContextUpdate shape:
# {
#     "type": "repo_memory",          # open-ended for future types
#     "mode": "delta" | "full",      # delivery mode for the next turn
#     "content": str,                 # delta markdown or full snapshot
#     "snapshot_content": str,        # latest file snapshot
#     "ts": float,                    # when the change was detected
#     "source_path": str,             # abs path to the changed file
#     "content_hash": str,            # sha256 of latest file snapshot for dedup
# }


class PendingContextUpdate(TypedDict):
    type: str
    mode: str
    content: str
    snapshot_content: str
    ts: float
    source_path: str
    content_hash: str


class ConversationTarget(TypedDict):
    repo_root: str
    memory_path: str


class ExternalQueueResult(TypedDict):
    queued: int
    mode: str
    content_hash: str
    source_path: str


class InotifyEvent(TypedDict):
    wd: int
    mask: int
    cookie: int
    name: str

# -- Module state ------------------------------------------------------

_pending: dict[str, list[PendingContextUpdate]] = {}
_last_content_hash: dict[str, str] = {}  # source_path -> hash of last seen snapshot
_last_content_text: dict[str, str] = {}  # source_path -> last seen snapshot text
_conversation_snapshot_text: dict[str, str] = {}  # conversation_id -> last delivered snapshot text
_meta_loader: Callable[[str], ObjectMap] | None = None
_conversation_lister: Callable[[], list[str]] | None = None
_conversation_targets: dict[str, ConversationTarget] = {}
_watcher_tasks: dict[str, asyncio.Task[None]] = {}
_watcher_stop_events: dict[str, asyncio.Event] = {}
_watcher_conversations: dict[str, set[str]] = {}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# -- Queue management (public API) ------------------------------------

def has_pending(conversation_id: str) -> bool:
    """Check if a conversation has any pending context updates."""
    entries = _pending.get(conversation_id)
    return bool(entries)


def pop_pending(
    conversation_id: str,
    update_type: str | None = None,
) -> PendingContextUpdate | None:
    """Pop the most recent pending update for a conversation.

    If update_type is given, only pops entries of that type.
    Returns None if nothing pending.
    """
    entries = _pending.get(conversation_id)
    if not entries:
        return None

    if update_type is None:
        # Pop most recent (last entry wins)
        entry = entries.pop(-1)
        # Clear older entries since we always want the latest
        entries.clear()
        if not entries:
            _pending.pop(conversation_id, None)
        snapshot_content = entry.get("snapshot_content")
        if entry.get("type") == "repo_memory" and isinstance(snapshot_content, str):
            _conversation_snapshot_text[conversation_id] = snapshot_content
        return entry

    # Find most recent of the requested type
    for i in range(len(entries) - 1, -1, -1):
        if entries[i].get("type") == update_type:
            entry = entries.pop(i)
            # Also remove older entries of the same type
            entries[:] = [e for e in entries if e.get("type") != update_type]
            if not entries:
                _pending.pop(conversation_id, None)
            snapshot_content = entry.get("snapshot_content")
            if entry.get("type") == "repo_memory" and isinstance(snapshot_content, str):
                _conversation_snapshot_text[conversation_id] = snapshot_content
            return entry

    return None


def queue_update(conversation_id: str, update: PendingContextUpdate) -> None:
    """Queue a pending context update for a conversation.

    Replaces any existing entry of the same type (latest wins).
    """
    update_type = update.get("type")
    if conversation_id not in _pending:
        _pending[conversation_id] = []

    entries = _pending[conversation_id]
    # Remove older entries of the same type
    entries[:] = [e for e in entries if e.get("type") != update_type]
    entries.append(update)


def _build_repo_memory_update(
    conversation_id: str,
    *,
    source_path: str,
    previous_content: str,
    current_content: str,
    content_hash: str,
    ts: float,
    delta_override: str | None = None,
) -> PendingContextUpdate:
    base_snapshot = _conversation_snapshot_text.get(conversation_id, "")
    existing_pending = next(
        (entry for entry in _pending.get(conversation_id, []) if entry.get("type") == "repo_memory"),
        None,
    )
    delta_markdown = None
    if (
        isinstance(delta_override, str)
        and delta_override.strip()
        and base_snapshot == previous_content
        and (
            existing_pending is None
            or existing_pending.get("snapshot_content") == current_content
        )
    ):
        delta_markdown = delta_override.strip()
    else:
        delta_markdown = build_repo_memory_delta(
            base_snapshot,
            current_content,
            source_path=source_path,
            ts=ts,
        )
    return {
        "type": "repo_memory",
        "mode": "delta" if isinstance(delta_markdown, str) and delta_markdown.strip() else "full",
        "content": delta_markdown if isinstance(delta_markdown, str) and delta_markdown.strip() else current_content,
        "snapshot_content": current_content,
        "ts": ts,
        "source_path": source_path,
        "content_hash": content_hash,
    }


def queue_external_repo_memory_update(
    source_path: str,
    *,
    previous_content: str,
    current_content: str,
    delta_content: str | None = None,
    ts: float | None = None,
) -> ExternalQueueResult:
    raw_path = str(source_path or "").strip()
    if not raw_path:
        raise ValueError("source_path is required")
    normalized_path = os.path.abspath(os.path.expanduser(raw_path))

    previous_text = previous_content.strip() if isinstance(previous_content, str) else ""
    current_text = current_content.strip() if isinstance(current_content, str) else ""
    update_ts = float(ts) if isinstance(ts, (int, float)) else time.time()
    new_hash = _content_hash(current_text)
    old_hash = _last_content_hash.get(normalized_path)
    _last_content_hash[normalized_path] = new_hash
    _last_content_text[normalized_path] = current_text

    conversations = sorted(_watcher_conversations.get(normalized_path, set()))
    _log(
        f"[pending_context] external_update path={normalized_path} "
        f"hash={old_hash or '-'}->{new_hash} conversations={[c[:8] for c in conversations]}"
    )
    if not conversations:
        _log(f"[pending_context] external update for {normalized_path} but no matching conversations")
        return {
            "queued": 0,
            "mode": "delta" if isinstance(delta_content, str) and delta_content.strip() else "full",
            "content_hash": new_hash,
            "source_path": normalized_path,
        }

    modes: set[str] = set()
    for conversation_id in conversations:
        update = _build_repo_memory_update(
            conversation_id,
            source_path=normalized_path,
            previous_content=previous_text,
            current_content=current_text,
            content_hash=new_hash,
            ts=update_ts,
            delta_override=delta_content,
        )
        modes.add(str(update.get("mode") or "full"))
        queue_update(conversation_id, update)

    mode = modes.pop() if len(modes) == 1 else "mixed"
    _log(
        f"[pending_context] queued external repo_memory {mode} update for "
        f"{len(conversations)} conversation(s): {[c[:8] for c in conversations]}"
    )
    return {
        "queued": len(conversations),
        "mode": mode,
        "content_hash": new_hash,
        "source_path": normalized_path,
    }


def clear_all() -> None:
    """Clear all pending updates. Mainly for testing."""
    _pending.clear()
    _last_content_hash.clear()
    _last_content_text.clear()
    _conversation_snapshot_text.clear()


def _prime_watch_state(memory_path: str) -> None:
    try:
        content = Path(memory_path).read_text(encoding="utf-8").strip()
    except Exception as exc:
        _log(f"[pending_context] failed to prime {memory_path}: {exc}")
        return
    _last_content_text[memory_path] = content
    _last_content_hash[memory_path] = _content_hash(content)


# -- inotify helpers ---------------------------------------------------

def _get_libc():
    """Load libc for inotify syscalls."""
    name = ctypes.util.find_library("c")
    if not name:
        name = "libc.so"
    return ctypes.CDLL(name, use_errno=True)


def _inotify_init(libc) -> int:
    fd = libc.inotify_init()
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"inotify_init failed: {os.strerror(errno)}")
    return fd


def _inotify_add_watch(libc, fd: int, path: bytes, mask: int) -> int:
    wd = libc.inotify_add_watch(fd, path, ctypes.c_uint32(mask))
    if wd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"inotify_add_watch failed for {path}: {os.strerror(errno)}")
    return wd


def _read_inotify_events(fd: int) -> list[InotifyEvent]:
    """Read available inotify events from fd. Non-blocking."""
    buf_size = 4096
    try:
        data = os.read(fd, buf_size)
    except BlockingIOError:
        return []
    if not data:
        return []

    events: list[InotifyEvent] = []
    offset = 0
    while offset + _EVENT_HEADER_SIZE <= len(data):
        wd, mask, cookie, name_len = struct.unpack_from(_EVENT_HEADER_FMT, data, offset)
        offset += _EVENT_HEADER_SIZE
        name = b""
        if name_len > 0 and offset + name_len <= len(data):
            name = data[offset:offset + name_len].rstrip(b"\x00")
            offset += name_len
        events.append({"wd": wd, "mask": mask, "cookie": cookie, "name": name.decode("utf-8", errors="replace")})

    return events


# -- Content hashing ---------------------------------------------------

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# -- Conversation targeting --------------------------------------------

def _watch_target_for_conversation(conversation_id: str) -> ConversationTarget | None:
    if not _meta_loader:
        return None

    try:
        meta = _meta_loader(conversation_id)
    except Exception:
        return None

    settings = coerce_object_map(meta.get("settings"))
    agent = settings.get("agent")
    if not isinstance(agent, str) or agent.strip() not in TARGETED_AGENT_TYPES:
        return None
    if not te2_mcp_integration_enabled(settings):
        return None

    cwd = settings.get("cwd")
    snapshot = load_repo_memory_snapshot(cwd if isinstance(cwd, str) else None)
    if not snapshot["exists"]:
        return None

    repo_root = snapshot["repo_root"]
    memory_path = snapshot["path"]
    if not isinstance(repo_root, str) or not repo_root.strip():
        return None
    if not isinstance(memory_path, str) or not memory_path.strip():
        return None

    return {
        "repo_root": os.path.abspath(repo_root),
        "memory_path": os.path.abspath(memory_path),
    }


def _stop_watch(memory_path: str) -> None:
    stop_event = _watcher_stop_events.pop(memory_path, None)
    if stop_event is not None:
        stop_event.set()

    task = _watcher_tasks.pop(memory_path, None)
    if task is not None and not task.done():
        task.cancel()

    _watcher_conversations.pop(memory_path, None)
    _last_content_hash.pop(memory_path, None)
    _last_content_text.pop(memory_path, None)


def _ensure_watch(memory_path: str) -> None:
    task = _watcher_tasks.get(memory_path)
    if task is not None and not task.done():
        return

    watch_dir = os.path.dirname(memory_path)
    if not os.path.isdir(watch_dir):
        _log(f"[pending_context] watch dir does not exist: {watch_dir}")
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log(f"[pending_context] no running loop; cannot watch {memory_path}")
        return

    _prime_watch_state(memory_path)
    stop_event = asyncio.Event()
    _watcher_stop_events[memory_path] = stop_event
    _watcher_tasks[memory_path] = loop.create_task(
        _watcher_loop(memory_path, stop_event),
        name=f"pending_context:{os.path.basename(watch_dir)}",
    )
    _log(f"[pending_context] started watcher for {memory_path}")


def _unregister_conversation(conversation_id: str) -> None:
    target = _conversation_targets.pop(conversation_id, None)
    _conversation_snapshot_text.pop(conversation_id, None)
    if not target:
        return

    memory_path = target["memory_path"]
    conversations = _watcher_conversations.get(memory_path)
    if not conversations:
        return

    conversations.discard(conversation_id)
    if conversations:
        return

    _stop_watch(memory_path)


def refresh_conversation(conversation_id: str) -> ConversationTarget | None:
    """Refresh one conversation's watcher registration from meta.json."""
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return None

    target = _watch_target_for_conversation(conversation_id)
    current = _conversation_targets.get(conversation_id)
    if target is None:
        _unregister_conversation(conversation_id)
        return None

    memory_path = target["memory_path"]
    snapshot = load_repo_memory_snapshot(target["repo_root"])
    snapshot_content = snapshot["content"]
    if current == target:
        _watcher_conversations.setdefault(memory_path, set()).add(conversation_id)
        _conversation_snapshot_text.setdefault(conversation_id, snapshot_content)
        _ensure_watch(memory_path)
        return target

    _unregister_conversation(conversation_id)
    _conversation_targets[conversation_id] = target
    _watcher_conversations.setdefault(memory_path, set()).add(conversation_id)
    _conversation_snapshot_text[conversation_id] = snapshot_content
    _ensure_watch(memory_path)
    _log(f"[pending_context] tracking {conversation_id[:8]} -> {memory_path}")
    return target


def refresh_all_conversations() -> int:
    """Refresh watcher registrations for every known conversation."""
    if not _conversation_lister:
        return 0

    try:
        conversation_ids = _conversation_lister()
    except Exception:
        return 0

    live_ids = {
        str(cid).strip()
        for cid in conversation_ids
        if isinstance(cid, str) and cid.strip()
    }

    for stale_id in [cid for cid in list(_conversation_targets) if cid not in live_ids]:
        _unregister_conversation(stale_id)

    tracked = 0
    for conversation_id in sorted(live_ids):
        if refresh_conversation(conversation_id):
            tracked += 1

    return tracked


# -- File change handler -----------------------------------------------

def _handle_file_change(source_path: str) -> int:
    """Read changed file, compare hash, queue updates for matching conversations.

    Returns number of conversations queued.
    """
    try:
        content = Path(source_path).read_text(encoding="utf-8").strip()
    except Exception as exc:
        _log(f"[pending_context] failed to read {source_path}: {exc}")
        return 0

    new_hash = _content_hash(content)
    old_hash = _last_content_hash.get(source_path)
    if new_hash == old_hash:
        _log(f"[pending_context] unchanged path={source_path} hash={new_hash}")
        return 0

    previous_content = _last_content_text.get(source_path, "")
    result = queue_external_repo_memory_update(
        source_path,
        previous_content=previous_content,
        current_content=content,
        ts=time.time(),
    )
    return int(result.get("queued") or 0)


# -- Watcher loop ------------------------------------------------------

async def _watcher_loop(
    watch_path: str,
    stop_event: asyncio.Event,
) -> None:
    """Async loop that watches a repo memory file via inotify and dispatches changes."""
    libc = _get_libc()
    fd = _inotify_init(libc)

    # Set fd to non-blocking
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    # Watch the directory (inotify watches dirs, we filter by filename)
    watch_dir = os.path.dirname(watch_path)
    watch_filename = os.path.basename(watch_path)

    try:
        wd = _inotify_add_watch(libc, fd, watch_dir.encode("utf-8"), _WATCH_MASK)
    except OSError as exc:
        _log(f"[pending_context] inotify_add_watch failed: {exc}")
        os.close(fd)
        return

    _log(f"[pending_context] watching {watch_path} (dir wd={wd})")
    loop = asyncio.get_running_loop()

    try:
        while True:
            if stop_event.is_set():
                break

            # Use asyncio to wait for fd readability
            ready = asyncio.Event()
            loop.add_reader(fd, ready.set)
            ready_task = asyncio.create_task(ready.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {ready_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if stop_event.is_set():
                    break
            finally:
                try:
                    loop.remove_reader(fd)
                except Exception:
                    pass
                for task in (ready_task, stop_task):
                    if not task.done():
                        task.cancel()

            events = _read_inotify_events(fd)
            matching_events = [evt for evt in events if evt.get("name") == watch_filename]

            if matching_events:
                for evt in matching_events:
                    mask = int(evt.get("mask") or 0)
                    _log(
                        f"[pending_context] inotify hit path={watch_path} "
                        f"name={watch_filename} mask=0x{mask:08x}"
                    )
                # Small debounce — editors may write multiple times
                await asyncio.sleep(0.3)
                _handle_file_change(watch_path)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _log(f"[pending_context] watcher error: {exc}")
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        _log(f"[pending_context] watcher stopped for {watch_path}")


# -- Public lifecycle API ----------------------------------------------

async def start_watcher(
    meta_loader: Callable[[str], ObjectMap],
    conversation_lister: Callable[[], list[str]],
) -> None:
    """Initialize watcher tracking for repo memory files referenced by conversations."""
    global _meta_loader, _conversation_lister

    stop_watcher()

    _meta_loader = meta_loader
    _conversation_lister = conversation_lister
    tracked = refresh_all_conversations()
    _log(
        f"[pending_context] initialized tracking for "
        f"{tracked} conversation(s) across {len(_watcher_tasks)} file(s)"
    )


def stop_watcher() -> None:
    """Stop all repo memory watchers if running."""
    global _meta_loader, _conversation_lister

    for stop_event in list(_watcher_stop_events.values()):
        stop_event.set()

    for task in list(_watcher_tasks.values()):
        if not task.done():
            task.cancel()

    _watcher_tasks.clear()
    _watcher_stop_events.clear()
    _watcher_conversations.clear()
    _conversation_targets.clear()
    _conversation_snapshot_text.clear()
    _last_content_hash.clear()
    _last_content_text.clear()
    _meta_loader = None
    _conversation_lister = None


def is_watching() -> bool:
    """Check if any repo memory watcher is currently running."""
    return any(task is not None and not task.done() for task in _watcher_tasks.values())


__all__ = [
    "TARGETED_AGENT_TYPES",
    "clear_all",
    "has_pending",
    "is_watching",
    "pop_pending",
    "queue_external_repo_memory_update",
    "queue_update",
    "refresh_all_conversations",
    "refresh_conversation",
    "start_watcher",
    "stop_watcher",
]
