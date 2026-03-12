import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from framework_shells.orchestrator import Orchestrator

from .router import route_event as route_codex_event
from .runtime_protocol import get_runtime_protocol

_TRANSPORT_LABEL = "app-server:codex-extension"
_CONFIG_ROOT = Path(os.path.expanduser("~/.cache/app_server"))
_CONVERSATION_DIR = _CONFIG_ROOT / "conversations"
_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"


def _strip_meta_envelope(text: str) -> str:
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1:]
    return text


def _extract_item_text(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    raw_type = str(item.get("type") or "")
    item_type = raw_type.lower()

    if item_type == "message":
        role = str(item.get("role") or "").lower()
        text_parts: List[str] = []
        content = item.get("content") or []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        if not text_parts and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
        text = "\n".join(text_parts)

        if role == "user":
            text = _strip_meta_envelope(text).strip()
            if text:
                return {"role": "user", "text": text}
        elif role in {"assistant", "agent"}:
            text = text.strip()
            if text:
                return {"role": "assistant", "text": text}
        return None

    if item_type in {"usermessage", "user_message"}:
        text_parts: List[str] = []
        content = item.get("content") or []
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        if not text_parts and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
        if not text_parts and isinstance(item.get("message"), str):
            text_parts.append(item["message"])
        text = _strip_meta_envelope("\n".join(text_parts)).strip()
        if text:
            return {"role": "user", "text": text}

    if item_type in {"agentmessage", "assistantmessage", "assistant"}:
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("message") if isinstance(item.get("message"), str) else None
        if isinstance(text, str) and text.strip():
            return {"role": "assistant", "text": text.strip()}

    return None


class CodexAppServerTransport:
    def __init__(
        self,
        *,
        server_root: Path,
        fws_getter: Callable[[], Awaitable[Any]],
        broadcast_fn: Callable[[Dict[str, Any]], Awaitable[None]],
        transcript_fn: Callable[[str, Dict[str, Any]], Awaitable[None]],
        meta_fns: Optional[Dict[str, Callable]],
        raw_log_fn: Callable[[str, str, Any], None],
    ) -> None:
        self._server_root = server_root
        self._fws_getter = fws_getter
        self._broadcast_fn = broadcast_fn
        self._transcript_fn = transcript_fn
        self._meta_fns = meta_fns or {}
        self._raw_log_fn = raw_log_fn

        self._lock = asyncio.Lock()
        self._shell_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._rpc_waiters: Dict[str, asyncio.Future] = {}
        self._request_conversations: Dict[str, Optional[str]] = {}
        self._resumed_threads: set[str] = set()
        self._request_counter = int(time.time() * 1000)

    def is_ready(self) -> bool:
        return bool(
            self._shell_id
            and self._initialized
            and self._reader_task
            and not self._reader_task.done()
        )

    def get_raw_label(self) -> str:
        return "__codex_transport__"

    async def ensure_ready(self) -> None:
        async with self._lock:
            shell_id = await self._get_or_start_shell()
            if not await self._pipe_available(shell_id):
                shell_id = await self._restart_shell(shell_id)
            await self._ensure_reader(shell_id)
            await self._ensure_initialized()

    async def stop(self) -> None:
        async with self._lock:
            await self._terminate_reader()
            shell_id = self._shell_id
            self._shell_id = None
            self._initialized = False
            self._resumed_threads.clear()
            self._fail_waiters("transport stopped")
            if shell_id:
                mgr = await self._fws_getter()
                try:
                    await mgr.terminate_shell(shell_id, force=True)
                except Exception:
                    pass

    def needs_thread_resume(self, thread_id: str) -> bool:
        return thread_id not in self._resumed_threads

    def mark_thread_ready(self, thread_id: Optional[str]) -> None:
        if isinstance(thread_id, str) and thread_id:
            self._resumed_threads.add(thread_id)

    async def rpc_request(
        self,
        method: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        await self.ensure_ready()
        return await self._rpc_request_unchecked(
            method,
            params=params,
            conversation_id=conversation_id,
            timeout=timeout,
        )

    async def _rpc_request_unchecked(
        self,
        method: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
        timeout: float = 6.0,
    ) -> Dict[str, Any]:
        req_id = self._next_request_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._rpc_waiters[req_id] = future
        self._request_conversations[req_id] = conversation_id
        payload: Dict[str, Any] = {"id": int(req_id), "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_payload(payload, conversation_id=conversation_id)
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._rpc_waiters.pop(req_id, None)
            self._request_conversations.pop(req_id, None)
        if not isinstance(response, dict):
            raise RuntimeError("invalid rpc response")
        if response.get("error"):
            error = response.get("error")
            if isinstance(error, dict):
                message = error.get("message") or "rpc error"
            else:
                message = str(error)
            raise RuntimeError(message)
        return response.get("result", response)

    async def notify(
        self,
        method: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        await self.ensure_ready()
        await self._notify_unchecked(method, params=params, conversation_id=conversation_id)

    async def _notify_unchecked(
        self,
        method: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._write_payload(payload, conversation_id=conversation_id)

    async def _get_or_start_shell(self) -> str:
        mgr = await self._fws_getter()

        if self._shell_id:
            shell = await mgr.get_shell(self._shell_id)
            if shell and shell.status == "running":
                return self._shell_id

        adopted = await self._adopt_existing_shell(mgr)
        if adopted:
            self._set_shell(adopted)
            return adopted

        shell_id = await self._start_new_shell(mgr)
        self._set_shell(shell_id)
        return shell_id

    async def _adopt_existing_shell(self, mgr: Any) -> Optional[str]:
        try:
            records = await mgr.list_shells()
        except Exception:
            return None
        for rec in records:
            if rec.status != "running":
                continue
            if (rec.label or "") != _TRANSPORT_LABEL:
                continue
            return rec.id
        return None

    async def _start_new_shell(self, mgr: Any) -> str:
        spec_path = self._server_root / "shellspec" / "app_server.yaml"
        orch = Orchestrator(mgr)
        shell = await orch.start_from_ref(
            f"{spec_path}#app_server",
            base_dir=spec_path.parent,
            ctx={"CWD": self._shell_cwd()},
            label=_TRANSPORT_LABEL,
            wait_ready=False,
        )
        return shell.id

    async def _restart_shell(self, shell_id: str) -> str:
        mgr = await self._fws_getter()
        await self._terminate_reader()
        self._fail_waiters("transport restarted")
        self._initialized = False
        self._resumed_threads.clear()
        try:
            await mgr.terminate_shell(shell_id, force=True)
        except Exception:
            pass
        new_shell_id = await self._start_new_shell(mgr)
        self._set_shell(new_shell_id)
        return new_shell_id

    async def _pipe_available(self, shell_id: str) -> bool:
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        return bool(state and state.process.stdin and state.process.stdout)

    async def _ensure_reader(self, shell_id: str) -> None:
        if self._reader_task and not self._reader_task.done():
            return
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdout:
            raise RuntimeError("codex extension transport pipe not available")
        self._reader_task = asyncio.create_task(
            self._reader_loop(shell_id),
            name="codex-extension-reader",
        )

    async def _terminate_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            await self._rpc_request_unchecked(
                "initialize",
                params={
                    "clientInfo": {
                        "name": "agent_log_server",
                        "title": "Agent Log Server",
                        "version": "0.1.0",
                    }
                },
                timeout=15.0,
            )
        except RuntimeError as exc:
            if "already initialized" not in str(exc).lower():
                raise
        await self._notify_unchecked("initialized", params={})
        self._initialized = True

    async def _write_payload(
        self,
        payload: Dict[str, Any],
        *,
        conversation_id: Optional[str] = None,
    ) -> None:
        shell_id = self._shell_id
        if not shell_id:
            raise RuntimeError("codex extension transport not running")
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdin:
            raise RuntimeError("codex extension transport pipe not available")
        line = json.dumps(payload, ensure_ascii=False)
        self._raw_log_fn("out", conversation_id or self.get_raw_label(), line)
        state.process.stdin.write((line + "\n").encode("utf-8"))
        await state.process.stdin.drain()

    async def _reader_loop(self, shell_id: str) -> None:
        pending_label: Optional[str] = None
        buffer = b""
        max_buffer = 4_000_000
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdout:
            return

        async def _process_line(text: str) -> None:
            nonlocal pending_label
            text = text.strip()
            if not text:
                return

            if "{" in text and not text.lstrip().startswith("{"):
                prefix, rest = text.split("{", 1)
                if prefix.strip() and rest.strip().startswith("{"):
                    pending_label = prefix.strip()
                    text = "{" + rest

            try:
                parsed = json.loads(text)
            except Exception:
                self._raw_log_fn("in", self.get_raw_label(), text)
                if "/" in text or text.endswith("started") or text.endswith("completed"):
                    pending_label = text
                return

            if isinstance(parsed, dict) and "id" in parsed and ("result" in parsed or "error" in parsed) and "method" not in parsed:
                req_id = str(parsed.get("id"))
                conversation_id = self._request_conversations.get(req_id)
                self._raw_log_fn("in", conversation_id or self.get_raw_label(), text)
                waiter = self._rpc_waiters.get(req_id)
                if waiter and not waiter.done():
                    waiter.set_result(parsed)
                return

            label = None
            payload: Any = parsed
            raw_conversation_id: Optional[str] = None
            request_id: Optional[str] = None

            if pending_label:
                label = pending_label
                pending_label = None
                if isinstance(parsed, dict) and isinstance(parsed.get("msg"), dict):
                    payload = parsed.get("msg")
                    raw_conversation_id = parsed.get("conversationId")
                else:
                    payload = parsed
            elif isinstance(parsed, dict):
                if "method" in parsed:
                    label = parsed.get("method")
                    payload = parsed.get("params", parsed)
                    if parsed.get("id") is not None:
                        request_id = str(parsed.get("id"))
                elif isinstance(parsed.get("msg"), dict):
                    msg = parsed.get("msg", {})
                    label = f"codex/event/{msg.get('type', 'event')}"
                    payload = msg
                    raw_conversation_id = parsed.get("conversationId")
                elif "type" in parsed:
                    label = str(parsed.get("type"))
                    payload = parsed

            if isinstance(parsed, dict) and isinstance(parsed.get("conversationId"), str):
                raw_conversation_id = parsed.get("conversationId")

            conversation_id = self._resolve_conversation_id(raw_conversation_id, payload)
            self._raw_log_fn("in", conversation_id or raw_conversation_id or self.get_raw_label(), text)

            if not label:
                return

            thread_id = self._get_thread_id(payload, fallback=raw_conversation_id)
            if conversation_id and thread_id:
                self._persist_thread_id(conversation_id, thread_id)
            turn_id = self._get_turn_id(payload)

            await self._route_transport_event(
                label,
                payload,
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                request_id=request_id,
            )

        try:
            while True:
                chunk = await state.process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > max_buffer and b"\n" not in buffer:
                    self._raw_log_fn("in", self.get_raw_label(), "[warn] dropping oversized line")
                    buffer = b""
                    continue
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        await _process_line(line.decode("utf-8", errors="replace"))
                    except Exception as exc:
                        self._raw_log_fn("err", self.get_raw_label(), f"reader_process_failed {exc}")
                        continue
            if buffer:
                try:
                    await _process_line(buffer.decode("utf-8", errors="replace"))
                except Exception as exc:
                    self._raw_log_fn("err", self.get_raw_label(), f"reader_process_failed {exc}")
        finally:
            self._initialized = False
            self._resumed_threads.clear()
            self._fail_waiters("transport reader stopped")
            if self._shell_id == shell_id:
                self._shell_id = None
            self._reader_task = None

    async def _route_transport_event(
        self,
        label: str,
        payload: Any,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_id: Optional[str],
    ) -> None:
        protocol = await get_runtime_protocol()
        routed = route_codex_event(
            protocol,
            label=label,
            payload=payload,
            thread_id=thread_id,
            turn_id=turn_id,
            extract_item_text=_extract_item_text,
        )
        if not isinstance(routed, dict) or not routed.get("handled"):
            return

        resolved_conversation_id = routed.get("conversation_id") or conversation_id
        transcript_entries = routed.get("transcript_entries")
        if resolved_conversation_id and isinstance(transcript_entries, list):
            for entry in transcript_entries:
                if isinstance(entry, dict):
                    await self._transcript_fn(resolved_conversation_id, entry)

        next_turn_id = routed.get("set_turn_id")
        if resolved_conversation_id and next_turn_id is not None:
            self._persist_turn_id(resolved_conversation_id, next_turn_id)
        elif resolved_conversation_id and routed.get("clear_turn_id"):
            self._persist_turn_id(resolved_conversation_id, None)

        events = routed.get("events")
        if isinstance(events, list):
            for event in events:
                if not isinstance(event, dict):
                    continue
                outbound = dict(event)
                if resolved_conversation_id:
                    outbound["conversation_id"] = resolved_conversation_id
                if request_id is not None and outbound.get("request_id") is None:
                    outbound["request_id"] = request_id
                await self._broadcast_fn(outbound)

    def _resolve_conversation_id(
        self,
        raw_conversation_id: Optional[str],
        payload: Any,
    ) -> Optional[str]:
        if isinstance(raw_conversation_id, str) and self._conversation_exists(raw_conversation_id):
            return raw_conversation_id
        if isinstance(raw_conversation_id, str):
            found = self._find_conversation_by_thread_id(raw_conversation_id)
            if found:
                return found
        thread_id = self._get_thread_id(payload, fallback=None)
        if thread_id:
            return self._find_conversation_by_thread_id(thread_id)
        turn_id = self._get_turn_id(payload)
        if turn_id:
            return self._find_conversation_by_turn_id(turn_id)
        return None

    def _conversation_exists(self, conversation_id: str) -> bool:
        if not conversation_id:
            return False
        return (self._conversation_meta_path(conversation_id)).exists()

    def _conversation_meta_path(self, conversation_id: str) -> Path:
        return _CONVERSATION_DIR / conversation_id / "meta.json"

    def _find_conversation_by_thread_id(self, thread_id: Optional[str]) -> Optional[str]:
        if not thread_id or not _CONVERSATION_DIR.exists():
            return None
        for child in _CONVERSATION_DIR.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("thread_id") == thread_id:
                return child.name
        return None

    def _find_conversation_by_turn_id(self, turn_id: Optional[str]) -> Optional[str]:
        if not turn_id or not _CONVERSATION_DIR.exists():
            return None
        for child in _CONVERSATION_DIR.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("turn_id") == turn_id:
                return child.name
        return None

    def _shell_cwd(self) -> str:
        config_path = _CONFIG_ROOT / "config.json"
        try:
            if config_path.exists():
                data = json.loads(config_path.read_text(encoding="utf-8"))
                cwd = data.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
        except Exception:
            pass
        return str(Path.cwd())

    def _load_meta(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        load = self._meta_fns.get("load")
        if callable(load):
            meta = load(conversation_id)
            return meta if isinstance(meta, dict) else None
        path = self._conversation_meta_path(conversation_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _save_meta(self, conversation_id: str, meta: Dict[str, Any]) -> None:
        save = self._meta_fns.get("save")
        if callable(save):
            save(conversation_id, meta)
            return
        path = self._conversation_meta_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def _persist_thread_id(self, conversation_id: str, thread_id: str) -> None:
        if not conversation_id or not thread_id:
            return
        meta = self._load_meta(conversation_id)
        if not isinstance(meta, dict):
            return
        existing = meta.get("thread_id")
        if existing and existing != thread_id:
            return
        changed = False
        if existing != thread_id:
            meta["thread_id"] = thread_id
            changed = True
        if meta.get("status") != "active":
            meta["status"] = "active"
            changed = True
        if changed:
            self._save_meta(conversation_id, meta)

    def _persist_turn_id(self, conversation_id: str, turn_id: Optional[str]) -> None:
        meta = self._load_meta(conversation_id)
        if not isinstance(meta, dict):
            return
        changed = False
        if turn_id:
            if meta.get("turn_id") != turn_id:
                meta["turn_id"] = turn_id
                changed = True
        elif "turn_id" in meta:
            meta.pop("turn_id", None)
            changed = True
        if changed:
            self._save_meta(conversation_id, meta)

    def _get_thread_id(self, payload: Any, fallback: Optional[str]) -> Optional[str]:
        if isinstance(payload, dict):
            thread = payload.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str) and thread.get("id"):
                return thread["id"]
            for key in ("threadId", "thread_id", "conversationId", "conversation_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(fallback, str) and fallback:
            return fallback
        return None

    def _get_turn_id(self, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        for key in ("turnId", "turn_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        turn = payload.get("turn")
        if isinstance(turn, dict):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id:
                return turn_id
        if payload.get("id") and payload.get("status") is not None:
            value = payload.get("id")
            if isinstance(value, str) and value:
                return value
        return None

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return str(self._request_counter)

    def _set_shell(self, shell_id: str) -> None:
        if self._shell_id != shell_id:
            self._initialized = False
            self._resumed_threads.clear()
        self._shell_id = shell_id

    def _fail_waiters(self, message: str) -> None:
        for waiter in self._rpc_waiters.values():
            if not waiter.done():
                waiter.set_exception(RuntimeError(message))
        self._rpc_waiters.clear()
        self._request_conversations.clear()
