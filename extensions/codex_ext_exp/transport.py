import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from framework_shells.orchestrator import Orchestrator

from .router import CodexEventRouter
from .runtime_protocol import (
    build_initialize_params,
    decode_response_result,
    encode_server_request_result,
    get_runtime_protocol,
    peek_runtime_protocol,
)

_TRANSPORT_LABEL = "app-server:codex-experimental"
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
        auth_event_handler: Optional[Callable[..., Awaitable[List[Dict[str, Any]]]]] = None,
    ) -> None:
        self._server_root = server_root
        self._fws_getter = fws_getter
        self._broadcast_fn = broadcast_fn
        self._transcript_fn = transcript_fn
        self._meta_fns = meta_fns or {}
        self._raw_log_fn = raw_log_fn
        self._auth_event_handler = auth_event_handler

        self._lock = asyncio.Lock()
        self._shell_id: Optional[str] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._rpc_waiters: Dict[str, asyncio.Future] = {}
        self._request_conversations: Dict[str, Optional[str]] = {}
        self._resumed_threads: set[str] = set()
        self._thread_conversations: Dict[str, str] = {}
        self._turn_conversations: Dict[str, str] = {}
        self._pending_approval_requests: Dict[str, Dict[str, Any]] = {}
        self._resume_startup_barriers: Dict[str, Dict[str, Any]] = {}
        self._request_counter = int(time.time() * 1000)
        self._stdin: Optional[Any] = None
        self._router = CodexEventRouter()

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
            self._thread_conversations.clear()
            self._turn_conversations.clear()
            self._pending_approval_requests.clear()
            self._clear_resume_startup_barriers("transport stopped")
            self._router.reset()
            self._stdin = None
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

    def runtime_instance_id(self) -> Optional[str]:
        return self._shell_id

    def has_pending_approval(self, request_id: str) -> bool:
        request_id_text = str(request_id or "").strip()
        return bool(request_id_text and request_id_text in self._pending_approval_requests)

    def resolve_approval(self, request_id: str, resolution: Any) -> bool:
        request_id_text = str(request_id or "").strip()
        if not request_id_text:
            return False
        pending = self._pending_approval_requests.pop(request_id_text, None)
        if not pending:
            return False
        writer = self._stdin
        if writer is None or not self._shell_id:
            self._pending_approval_requests[request_id_text] = pending
            return False

        result_value: Dict[str, Any] = {}
        if isinstance(resolution, dict) and isinstance(resolution.get("result"), dict):
            result_value = dict(resolution["result"])
        elif isinstance(resolution, dict):
            result_value = {
                key: value
                for key, value in resolution.items()
                if key not in {"feedback", "kind", "message", "path", "rules"}
            }

        decision = result_value.get("decision")
        if decision is None:
            decision = resolution.get("decision") if isinstance(resolution, dict) else resolution
        if "decision" not in result_value:
            result_value["decision"] = "accept" if str(decision).strip().lower() == "accept" else "decline"

        request_method = self._approval_request_method(pending)
        if request_method:
            protocol = peek_runtime_protocol()
            if protocol is None:
                raise RuntimeError("runtime protocol unavailable for approval resolution")
            encoded_result = encode_server_request_result(protocol, request_method, result_value)
        else:
            encoded_result = {
                "decision": "accept" if str(result_value.get("decision")).strip().lower() == "accept" else "decline",
            }

        response_id: Any = int(request_id_text) if request_id_text.isdigit() else request_id_text
        response_payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": response_id,
            "result": encoded_result,
        }
        line = json.dumps(response_payload, ensure_ascii=False)
        try:
            self._raw_log_fn("out", pending.get("conversation_id") or self.get_raw_label(), line)
            writer.write((line + "\n").encode("utf-8"))
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(writer.drain())
            return True
        except Exception:
            self._pending_approval_requests[request_id_text] = pending
            return False

    def _approval_request_method(self, pending: Dict[str, Any]) -> Optional[str]:
        method = pending.get("method")
        if isinstance(method, str) and method.strip():
            return method.strip().lower()
        kind = str(pending.get("kind") or "").strip().lower()
        if kind == "command":
            return "item/commandexecution/requestapproval"
        if kind == "diff":
            return "item/filechange/requestapproval"
        return None

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
        method_name = str(method or "").strip().lower()
        resume_thread_id = self._extract_resume_thread_id(params) if method_name == "thread/resume" else None
        if conversation_id and resume_thread_id:
            self._begin_resume_startup_barrier(conversation_id, resume_thread_id)
        req_id = self._next_request_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._rpc_waiters[req_id] = future
        self._request_conversations[req_id] = conversation_id
        payload: Dict[str, Any] = {"id": int(req_id), "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_payload(payload, conversation_id=conversation_id)
        try:
            if conversation_id and resume_thread_id:
                response = await self._await_resume_virtual_ack_or_response(
                    req_id=req_id,
                    response_future=future,
                    conversation_id=conversation_id,
                    thread_id=resume_thread_id,
                    timeout=timeout,
                )
            else:
                try:
                    response = await asyncio.wait_for(future, timeout=timeout)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(f"{method} request timed out") from exc
        finally:
            self._rpc_waiters.pop(req_id, None)
            self._request_conversations.pop(req_id, None)
        if not isinstance(response, dict):
            if conversation_id and resume_thread_id:
                self._discard_resume_startup_barrier(conversation_id, reason="invalid rpc response")
            raise RuntimeError("invalid rpc response")
        if response.get("error"):
            if conversation_id and resume_thread_id:
                self._discard_resume_startup_barrier(conversation_id, reason="rpc error")
            error = response.get("error")
            if isinstance(error, dict):
                message = error.get("message") or "rpc error"
            else:
                message = str(error)
            raise RuntimeError(message)
        if response.get("_virtual_ack") is True and conversation_id and resume_thread_id:
            self._remember_bindings(
                conversation_id=conversation_id,
                thread_id=resume_thread_id,
                turn_id=None,
            )
            return {
                "thread": {"id": resume_thread_id},
                "_virtual_ack": True,
                "_virtual_ack_source": response.get("_virtual_ack_source"),
            }
        protocol = await get_runtime_protocol()
        decoded_result = decode_response_result(protocol, method, response.get("result", response))
        self._remember_response_bindings(conversation_id=conversation_id, result=decoded_result)
        return decoded_result

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
            if shell and shell.status == "running" and getattr(shell, "spec_id", "") == "app_server_observed":
                return self._shell_id
            self._shell_id = None

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
            if getattr(rec, "spec_id", "") != "app_server_exp_observed":
                continue
            return rec.id
        return None

    async def _start_new_shell(self, mgr: Any) -> str:
        spec_path = Path(__file__).parent / "shellspec" / "app_server_exp.yaml"
        orch = Orchestrator(mgr)
        shell = await orch.start_from_ref(
            f"{spec_path}#app_server_exp_observed",
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
        self._thread_conversations.clear()
        self._turn_conversations.clear()
        self._clear_resume_startup_barriers("transport restarted")
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
        self._stdin = state.process.stdin
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
        protocol = await get_runtime_protocol()
        try:
            await self._rpc_request_unchecked(
                "initialize",
                params=build_initialize_params(protocol),
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
            self._stdin = None
            raise RuntimeError("codex extension transport pipe not available")
        self._stdin = state.process.stdin
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
                    params = parsed.get("params", parsed)
                    payload = params
                    if isinstance(params, dict):
                        param_conversation_id = params.get("conversationId")
                        if isinstance(param_conversation_id, str) and param_conversation_id:
                            raw_conversation_id = param_conversation_id
                        if (
                            isinstance(label, str)
                            and label.startswith("codex/event/collab_")
                            and isinstance(params.get("msg"), dict)
                        ):
                            payload = dict(params["msg"])
                            for key in ("id", "conversationId", "conversation_id", "threadId", "thread_id", "turnId", "turn_id"):
                                value = params.get(key)
                                if value is not None and payload.get(key) is None:
                                    payload[key] = value
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
            self._note_resume_startup_event(
                conversation_id=conversation_id,
                thread_id=thread_id,
                label=label,
                payload=payload,
            )

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
            self._thread_conversations.clear()
            self._turn_conversations.clear()
            self._pending_approval_requests.clear()
            self._clear_resume_startup_barriers("transport reader stopped")
            self._router.reset()
            self._stdin = None
            self._fail_waiters("transport reader stopped")
            if self._shell_id == shell_id:
                self._shell_id = None
            self._reader_task = None

    async def route_event(
        self,
        label: str,
        payload: Any,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_id: Optional[str],
    ) -> Dict[str, Any]:
        protocol = await get_runtime_protocol()
        routed_payload = dict(payload) if isinstance(payload, dict) else payload
        if request_id is not None and isinstance(routed_payload, dict) and routed_payload.get("_request_id") is None:
            routed_payload["_request_id"] = str(request_id)
        routed = self._router.route_event(
            protocol,
            label=label,
            payload=routed_payload,
            thread_id=thread_id,
            turn_id=turn_id,
            extract_item_text=_extract_item_text,
        )
        if conversation_id and isinstance(routed, dict):
            descriptors = routed.get("approval_descriptors")
            if isinstance(descriptors, list):
                for descriptor in descriptors:
                    if isinstance(descriptor, dict):
                        self._persist_pending_approval(conversation_id, descriptor)
            bind_thread_ids = routed.get("bind_thread_ids")
            if isinstance(bind_thread_ids, list):
                for bound_thread_id in bind_thread_ids:
                    if isinstance(bound_thread_id, str) and bound_thread_id:
                        self._remember_bindings(
                            conversation_id=conversation_id,
                            thread_id=bound_thread_id,
                        )
            clear_ids = routed.get("clear_live_approval_ids")
            if isinstance(clear_ids, list):
                for pending_id in clear_ids:
                    self._pending_approval_requests.pop(str(pending_id or "").strip(), None)
        return routed

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
        routed = await self.route_event(
            label=label,
            payload=payload,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id=request_id,
        )

        extra_events: List[Dict[str, Any]] = []
        if self._auth_event_handler is not None:
            handled_events = await self._auth_event_handler(
                label=label,
                payload=payload,
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                request_id=request_id,
            )
            if isinstance(handled_events, list):
                extra_events = [event for event in handled_events if isinstance(event, dict)]

        if (not isinstance(routed, dict) or not routed.get("handled")) and not extra_events:
            return

        routed_result = routed if isinstance(routed, dict) else {}
        resolved_conversation_id = routed_result.get("conversation_id") or conversation_id
        transcript_entries = routed_result.get("transcript_entries")
        if resolved_conversation_id and isinstance(transcript_entries, list):
            for entry in transcript_entries:
                if isinstance(entry, dict):
                    await self._transcript_fn(resolved_conversation_id, entry)

        next_turn_id = routed_result.get("set_turn_id")
        if resolved_conversation_id and next_turn_id is not None:
            self._persist_turn_id(resolved_conversation_id, next_turn_id)
        elif resolved_conversation_id and routed_result.get("clear_turn_id"):
            self._persist_turn_id(resolved_conversation_id, None)

        events: List[Dict[str, Any]] = []
        routed_events = routed_result.get("events")
        if isinstance(routed_events, list):
            events.extend(event for event in routed_events if isinstance(event, dict))
        events.extend(extra_events)
        for event in events:
            outbound = dict(event)
            if resolved_conversation_id and outbound.get("conversation_id") is None:
                outbound["conversation_id"] = resolved_conversation_id
            if request_id is not None and outbound.get("request_id") is None:
                outbound["request_id"] = request_id
            await self._broadcast_fn(outbound)

    def _persist_pending_approval(self, conversation_id: str, descriptor: Dict[str, Any]) -> None:
        request_id_text = str(descriptor.get("request_id") or descriptor.get("id") or "").strip()
        if not request_id_text:
            return
        meta = self._load_meta(conversation_id) or {}
        settings = meta.get("settings") if isinstance(meta.get("settings"), dict) else {}
        created_at = str(descriptor.get("created_at") or datetime.now(timezone.utc).isoformat())
        render_event = dict(descriptor.get("render_event") or {})
        render_event["type"] = "approval"
        render_event["conversation_id"] = render_event.get("conversation_id") or conversation_id
        render_event["id"] = render_event.get("id") or request_id_text
        render_event["request_id"] = render_event.get("request_id") or request_id_text
        if descriptor.get("turn_id") and not render_event.get("turn_id"):
            render_event["turn_id"] = descriptor.get("turn_id")
        if not render_event.get("created_at"):
            render_event["created_at"] = created_at

        persisted = {
            "request_id": request_id_text,
            "agent": settings.get("agent") or "codex",
            "kind": descriptor.get("kind") or "unknown",
            "request_method": descriptor.get("request_method"),
            "request_params": dict(descriptor.get("request_params") or {}),
            "payload": dict(descriptor.get("payload") or {}),
            "thread_id": descriptor.get("thread_id") or meta.get("thread_id"),
            "turn_id": descriptor.get("turn_id"),
            "runtime_signature": descriptor.get("runtime_signature") or meta.get("thread_runtime_signature"),
            "runtime_instance_id": descriptor.get("runtime_instance_id") or self.runtime_instance_id(),
            "transcript_anchor": dict(descriptor.get("transcript_anchor") or {"turn_id": descriptor.get("turn_id")}),
            "source": descriptor.get("source") or "live",
            "created_at": created_at,
            "render_event": render_event,
        }

        upsert = self._meta_fns.get("upsert_pending_approval")
        if callable(upsert):
            upsert(conversation_id, persisted)
        else:
            meta = meta if isinstance(meta, dict) else {}
            pending = meta.get("pending_approvals") if isinstance(meta.get("pending_approvals"), dict) else {}
            pending[request_id_text] = persisted
            meta["pending_approvals"] = pending
            self._save_meta(conversation_id, meta)

        self._pending_approval_requests[request_id_text] = {
            "conversation_id": conversation_id,
            "kind": persisted.get("kind"),
            "method": persisted.get("request_method"),
            "thread_id": persisted.get("thread_id"),
        }

    def _resolve_conversation_id(
        self,
        raw_conversation_id: Optional[str],
        payload: Any,
    ) -> Optional[str]:
        if isinstance(raw_conversation_id, str) and self._conversation_exists(raw_conversation_id):
            return raw_conversation_id
        turn_id = self._get_turn_id(payload)
        if turn_id:
            mapped = self._turn_conversations.get(turn_id)
            if mapped:
                return mapped
        thread_id = self._get_thread_id(payload, fallback=raw_conversation_id)
        if thread_id:
            mapped = self._thread_conversations.get(thread_id)
            if mapped:
                return mapped
        if isinstance(raw_conversation_id, str):
            found = self._find_conversation_by_thread_id(raw_conversation_id)
            if found:
                return found
        thread_id = self._get_thread_id(payload, fallback=None)
        if thread_id:
            return self._find_conversation_by_thread_id(thread_id)
        if turn_id:
            return self._find_conversation_by_turn_id(turn_id)
        return None

    def _extract_resume_thread_id(self, params: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(params, dict):
            return None
        for key in ("threadId", "thread_id"):
            value = params.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    def _begin_resume_startup_barrier(self, conversation_id: str, thread_id: str) -> Dict[str, Any]:
        existing = self._resume_startup_barriers.get(conversation_id)
        if (
            isinstance(existing, dict)
            and existing.get("thread_id") == thread_id
            and isinstance(existing.get("future"), asyncio.Future)
            and not existing["future"].done()
        ):
            return existing
        future = asyncio.get_running_loop().create_future()
        barrier = {
            "thread_id": thread_id,
            "saw_startup": False,
            "future": future,
        }
        self._resume_startup_barriers[conversation_id] = barrier
        self._remember_bindings(conversation_id=conversation_id, thread_id=thread_id, turn_id=None)
        self._raw_log_fn("out", conversation_id, f"resume_virtual_ack_begin thread={thread_id[:8]}")
        return barrier

    def _clear_resume_startup_barriers(self, reason: str) -> None:
        for barrier in self._resume_startup_barriers.values():
            future = barrier.get("future") if isinstance(barrier, dict) else None
            if isinstance(future, asyncio.Future) and not future.done():
                future.cancel()
        self._resume_startup_barriers.clear()

    def _discard_resume_startup_barrier(self, conversation_id: str, *, reason: Optional[str] = None) -> None:
        barrier = self._resume_startup_barriers.pop(conversation_id, None)
        if not isinstance(barrier, dict):
            return
        future = barrier.get("future")
        if not isinstance(future, asyncio.Future) or future.done():
            return
        if reason:
            future.cancel()
        else:
            future.set_result(None)

    def _transport_event_type(self, label: str, payload: Any) -> Optional[str]:
        special_types = {
            "thread/status/changed",
            "mcp_startup_update",
            "mcp_startup_complete",
        }
        candidates: List[str] = []
        if isinstance(payload, dict) and isinstance(payload.get("type"), str) and payload.get("type"):
            candidates.append(payload["type"])
        if isinstance(payload, dict) and isinstance(payload.get("msg"), dict):
            msg_type = payload["msg"].get("type")
            if isinstance(msg_type, str) and msg_type:
                candidates.append(msg_type)
        if isinstance(label, str) and label:
            if label.startswith("codex/event/"):
                candidates.append(label.split("/", 2)[-1])
            else:
                candidates.append(label)
        protocol = peek_runtime_protocol()
        for candidate in candidates:
            text = str(candidate or "").strip()
            if not text:
                continue
            if text in special_types:
                return text
            if protocol is None or protocol.has_event_type(text):
                return text
        return None

    def _note_resume_startup_event(
        self,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        label: str,
        payload: Any,
    ) -> None:
        if not conversation_id:
            return
        barrier = self._resume_startup_barriers.get(conversation_id)
        if not isinstance(barrier, dict):
            return
        barrier_thread_id = barrier.get("thread_id")
        if isinstance(barrier_thread_id, str) and barrier_thread_id and thread_id and barrier_thread_id != thread_id:
            return
        event_type = self._transport_event_type(label, payload)
        if event_type == "mcp_startup_update":
            barrier["saw_startup"] = True
            msg_payload = payload.get("msg") if isinstance(payload, dict) and isinstance(payload.get("msg"), dict) else payload
            msg_dict = msg_payload if isinstance(msg_payload, dict) else {}
            status = msg_dict.get("status") if isinstance(msg_dict.get("status"), dict) else {}
            state = str(status.get("state") or "").strip().lower()
            if state == "failed":
                future = barrier.get("future")
                if isinstance(future, asyncio.Future) and not future.done():
                    server_name = str(msg_dict.get("server") or "").strip()
                    error_text = str(status.get("error") or "").strip()
                    message = error_text or "MCP startup failed during thread resume"
                    future.set_exception(RuntimeError(message))
                    server_suffix = f" server={server_name}" if server_name else ""
                    self._raw_log_fn(
                        "in",
                        conversation_id,
                        f"resume_virtual_ack_event thread={str(barrier_thread_id or thread_id or '')[:8]} "
                        f"source=mcp_startup_update state=failed{server_suffix}",
                    )
            return
        future = barrier.get("future")
        if not isinstance(future, asyncio.Future) or future.done():
            return
        if event_type == "thread/status/changed":
            status = payload.get("status") if isinstance(payload, dict) and isinstance(payload.get("status"), dict) else {}
            status_type = str(status.get("type") or "").strip().lower()
            if status_type == "idle":
                future.set_result({
                    "source": "thread/status/changed",
                    "status": status_type,
                })
                self._raw_log_fn(
                    "in",
                    conversation_id,
                    f"resume_virtual_ack_event thread={str(barrier_thread_id or thread_id or '')[:8]} "
                    "source=thread/status/changed status=idle",
                )
            return
        if event_type == "mcp_startup_complete":
            barrier["saw_startup"] = True
            future.set_result({
                "source": "mcp_startup_complete",
            })
            self._raw_log_fn(
                "in",
                conversation_id,
                f"resume_virtual_ack_event thread={str(barrier_thread_id or thread_id or '')[:8]} "
                "source=mcp_startup_complete",
            )

    async def _await_resume_virtual_ack_or_response(
        self,
        *,
        req_id: str,
        response_future: asyncio.Future,
        conversation_id: str,
        thread_id: str,
        timeout: float,
    ) -> Dict[str, Any]:
        barrier = self._resume_startup_barriers.get(conversation_id)
        if not isinstance(barrier, dict):
            try:
                return await asyncio.wait_for(response_future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("thread/resume request timed out") from exc
        if barrier.get("thread_id") != thread_id:
            try:
                return await asyncio.wait_for(response_future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("thread/resume request timed out") from exc
        barrier_future = barrier.get("future")
        if not isinstance(barrier_future, asyncio.Future):
            self._discard_resume_startup_barrier(conversation_id)
            try:
                return await asyncio.wait_for(response_future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("thread/resume request timed out") from exc
        self._raw_log_fn(
            "out",
            conversation_id,
            f"resume_virtual_ack_wait thread={thread_id[:8]} timeout={timeout:.2f} req={req_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {response_future, barrier_future},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError as exc:
            self._discard_resume_startup_barrier(conversation_id, reason="thread/resume virtual ack cancelled")
            raise RuntimeError("thread/resume virtual ack cancelled") from exc
        if response_future in done:
            self._discard_resume_startup_barrier(conversation_id)
            try:
                response = response_future.result()
            except asyncio.CancelledError as exc:
                raise RuntimeError("thread/resume response cancelled") from exc
            self._raw_log_fn(
                "in",
                conversation_id,
                f"resume_virtual_ack_fallback thread={thread_id[:8]} source=response req={req_id}",
            )
            return response
        if barrier_future in done:
            try:
                ack_payload = barrier_future.result()
            except asyncio.CancelledError as exc:
                self._discard_resume_startup_barrier(conversation_id)
                raise RuntimeError("thread/resume virtual ack cancelled") from exc
            except Exception as exc:
                if not response_future.done():
                    response_future.cancel()
                self._discard_resume_startup_barrier(conversation_id)
                self._raw_log_fn(
                    "err",
                    conversation_id,
                    f"resume_virtual_ack_fail thread={thread_id[:8]} req={req_id} error={exc}",
                )
                raise RuntimeError(str(exc) or "thread/resume virtual ack failed") from exc
            source = ack_payload.get("source") if isinstance(ack_payload, dict) else "unknown"
            if not response_future.done():
                response_future.cancel()
            self._discard_resume_startup_barrier(conversation_id)
            self._raw_log_fn(
                "in",
                conversation_id,
                f"resume_virtual_ack_release thread={thread_id[:8]} source={source} req={req_id}",
            )
            return {
                "_virtual_ack": True,
                "_virtual_ack_source": source,
            }
        if not response_future.done():
            response_future.cancel()
        self._discard_resume_startup_barrier(conversation_id, reason="thread/resume virtual ack timed out")
        raise RuntimeError("thread/resume virtual ack timed out")

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
        self._thread_conversations[thread_id] = conversation_id
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
        if turn_id:
            self._turn_conversations[turn_id] = conversation_id
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

    def _remember_bindings(
        self,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        if not conversation_id:
            return
        if isinstance(thread_id, str) and thread_id:
            self._thread_conversations[thread_id] = conversation_id
        if isinstance(turn_id, str) and turn_id:
            self._turn_conversations[turn_id] = conversation_id

    def _remember_response_bindings(
        self,
        *,
        conversation_id: Optional[str],
        result: Any,
    ) -> None:
        if not conversation_id or not isinstance(result, dict):
            return
        thread_id: Optional[str] = None
        turn_id: Optional[str] = None
        thread = result.get("thread")
        if isinstance(thread, dict):
            thread_value = thread.get("id")
            if isinstance(thread_value, str) and thread_value:
                thread_id = thread_value
        turn = result.get("turn")
        if isinstance(turn, dict):
            turn_value = turn.get("id")
            if isinstance(turn_value, str) and turn_value:
                turn_id = turn_value
        if thread_id or turn_id:
            self._remember_bindings(
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

    def _get_thread_id(self, payload: Any, fallback: Optional[str]) -> Optional[str]:
        if isinstance(payload, dict):
            thread = payload.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str) and thread.get("id"):
                return thread["id"]
            for key in ("threadId", "thread_id", "conversationId", "conversation_id", "sender_thread_id"):
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
