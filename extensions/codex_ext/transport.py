import asyncio
import contextlib
import importlib
import json
import os
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Protocol, TypeGuard, TypedDict, cast

import msgspec

from .router import CodexEventRouter
from .runtime_protocol import (
    build_initialize_params,
    decode_response_result,
    encode_server_request_result,
    get_runtime_protocol,
    peek_runtime_protocol,
)

_TRANSPORT_LABEL = "app-server:codex-extension"
_CONFIG_ROOT = Path(os.path.expanduser("~/.cache/app_server"))
_CONVERSATION_DIR = _CONFIG_ROOT / "conversations"
_META_ENVELOPE_START = "\x1eCODEX_META "
_META_ENVELOPE_END = "\x1f"
_APP_SERVER_JSON_DECODER = msgspec.json.Decoder()


class _PipeWriter(Protocol):
    def write(self, data: bytes) -> object: ...

    async def drain(self) -> object: ...


class _OutputSubscription(Protocol):
    async def get(self) -> bytes | None: ...


class _PipeProcess(Protocol):
    stdin: _PipeWriter | None
    returncode: int | None


class _PipeState(Protocol):
    process: _PipeProcess


class _ShellRecord(Protocol):
    id: str
    status: str
    label: str | None
    spec_id: str | None


class ShellManager(Protocol):
    async def get_shell(self, shell_id: str) -> _ShellRecord | None: ...

    async def list_shells(self) -> list[_ShellRecord]: ...

    async def terminate_shell(self, shell_id: str, force: bool = False) -> object: ...

    def get_pipe_state(self, shell_id: str) -> _PipeState | None: ...

    async def subscribe_output_bytes(self, shell_id: str) -> _OutputSubscription: ...

    async def unsubscribe_output_bytes(self, shell_id: str, subscription: _OutputSubscription) -> object: ...

    async def write_to_pipe(self, shell_id: str, data: str) -> object: ...


class _ShellStarterRecord(Protocol):
    id: str


class _Orchestrator(Protocol):
    async def start_from_ref(
        self,
        ref: str,
        *,
        base_dir: Path,
        ctx: Dict[str, str],
        label: str,
        wait_ready: bool,
    ) -> _ShellStarterRecord: ...


class _OrchestratorFactory(Protocol):
    def __call__(self, mgr: ShellManager) -> _Orchestrator: ...


class MetaFns(TypedDict, total=False):
    load: Callable[[str], object]
    save: Callable[[str, Dict[str, object]], None]
    upsert_pending_approval: Callable[[str, Dict[str, object]], None]


class _TransportEvent(TypedDict):
    label: str
    payload: object
    conversation_id: Optional[str]
    thread_id: Optional[str]
    turn_id: Optional[str]
    request_id: Optional[str]


def _is_object_dict(value: object) -> TypeGuard[Dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[List[object]]:
    return isinstance(value, list)


def _object_dict(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, object] = {}
    for key, item_value in cast(Iterable[tuple[object, object]], value.items()):
        result[str(key)] = item_value
    return result


def _decode_app_server_json_line(raw: bytes | bytearray | memoryview) -> object:
    return cast(object, _APP_SERVER_JSON_DECODER.decode(bytes(raw)))


def _line_text(raw: bytes | bytearray | memoryview) -> str:
    return bytes(raw).decode("utf-8", errors="replace")


def _load_orchestrator_factory() -> _OrchestratorFactory:
    module = importlib.import_module("framework_shells.orchestrator")
    orchestrator = getattr(module, "Orchestrator", None)
    if orchestrator is None:
        raise RuntimeError("framework_shells.orchestrator.Orchestrator unavailable")
    return cast(_OrchestratorFactory, orchestrator)


def _strip_meta_envelope(text: str) -> str:
    if text.startswith(_META_ENVELOPE_START):
        end_idx = text.find(_META_ENVELOPE_END)
        if end_idx != -1:
            return text[end_idx + 1:]
    return text


def _shell_record_subgroups(record: object) -> set[str]:
    raw_subgroups = getattr(record, "subgroups", None)
    if not isinstance(raw_subgroups, (list, tuple, set)):
        return set()
    return {
        subgroup.strip()
        for subgroup in cast(Iterable[object], raw_subgroups)
        if isinstance(subgroup, str) and subgroup.strip()
    }


def _shell_record_matches_current_app(record: object) -> bool:
    expected_app_id = os.environ.get("TE_APP_ID")
    expected = expected_app_id.strip() if isinstance(expected_app_id, str) else ""
    if not expected:
        return True
    record_app_id = getattr(record, "app_id", None)
    return record_app_id == expected or expected in _shell_record_subgroups(record)


def _extract_item_text(item: Dict[str, object]) -> Optional[Dict[str, str]]:
    raw_type = str(item.get("type") or "")
    item_type = raw_type.lower()

    if item_type == "message":
        role = str(item.get("role") or "").lower()
        text_parts: List[str] = []
        content = item.get("content")
        if _is_object_list(content):
            for part in content:
                part_dict = _object_dict(part)
                if part_dict.get("type") == "text":
                    text = part_dict.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        if not text_parts and isinstance(item.get("text"), str):
            text_parts.append(cast(str, item["text"]))
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
        user_text_parts: List[str] = []
        content = item.get("content")
        if _is_object_list(content):
            for part in content:
                part_dict = _object_dict(part)
                if part_dict.get("type") == "text":
                    text = part_dict.get("text")
                    if isinstance(text, str):
                        user_text_parts.append(text)
        if not user_text_parts and isinstance(item.get("text"), str):
            user_text_parts.append(cast(str, item["text"]))
        if not user_text_parts and isinstance(item.get("message"), str):
            user_text_parts.append(cast(str, item["message"]))
        text = _strip_meta_envelope("\n".join(user_text_parts)).strip()
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
        fws_getter: Callable[[], Awaitable[ShellManager]],
        broadcast_fn: Callable[[Dict[str, object]], Awaitable[None]],
        transcript_fn: Callable[[str, Dict[str, object]], Awaitable[None]],
        meta_fns: Optional[MetaFns],
        raw_log_fn: Callable[[str, str, object], None],
        auth_event_handler: Optional[Callable[..., Awaitable[List[Dict[str, object]]]]] = None,
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
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._event_router_task: Optional[asyncio.Task[None]] = None
        self._event_queue: Optional[asyncio.Queue[_TransportEvent]] = None
        self._initialized = False
        self._rpc_waiters: Dict[str, asyncio.Future[Dict[str, object]]] = {}
        self._request_conversations: Dict[str, Optional[str]] = {}
        self._resume_idle_waiters: Dict[str, asyncio.Future[Dict[str, object]]] = {}
        self._resumed_threads: set[str] = set()
        self._thread_conversations: Dict[str, str] = {}
        self._turn_conversations: Dict[str, str] = {}
        self._pending_approval_requests: Dict[str, Dict[str, object]] = {}
        self._write_lock = asyncio.Lock()
        self._request_counter = int(time.time() * 1000)
        self._stdin: Optional[_PipeWriter] = None
        self._stdout_subscription: Optional[_OutputSubscription] = None
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
            self._clear_resume_idle_waiters("transport stopped")
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

    def is_thread_ready(self, thread_id: Optional[str]) -> bool:
        return isinstance(thread_id, str) and thread_id in self._resumed_threads

    def mark_thread_ready(self, thread_id: Optional[str]) -> None:
        if isinstance(thread_id, str) and thread_id:
            self._resumed_threads.add(thread_id)

    def mark_thread_unready(self, thread_id: Optional[str]) -> None:
        if isinstance(thread_id, str) and thread_id:
            self._resumed_threads.discard(thread_id)

    def runtime_instance_id(self) -> Optional[str]:
        return self._shell_id

    def has_pending_approval(self, request_id: str) -> bool:
        request_id_text = str(request_id or "").strip()
        return bool(request_id_text and request_id_text in self._pending_approval_requests)

    def resolve_approval(self, request_id: str, resolution: object) -> bool:
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

        result_value: Dict[str, object] = {}
        resolution_dict = _object_dict(resolution)
        if isinstance(resolution_dict.get("result"), dict):
            result_value = _object_dict(resolution_dict["result"])
        elif resolution_dict:
            result_value = {
                key: value
                for key, value in resolution_dict.items()
                if key not in {"feedback", "kind", "message", "path", "rules"}
            }

        request_method = self._approval_request_method(pending)
        requires_decision = request_method in {
            "item/commandexecution/requestapproval",
            "item/filechange/requestapproval",
        }
        decision = result_value.get("decision")
        if decision is None:
            decision = resolution_dict.get("decision") if resolution_dict else resolution
        if requires_decision and "decision" not in result_value:
            result_value["decision"] = "accept" if str(decision).strip().lower() == "accept" else "decline"
        if request_method:
            protocol = peek_runtime_protocol()
            if protocol is None:
                raise RuntimeError("runtime protocol unavailable for approval resolution")
            encoded_result: object = encode_server_request_result(protocol, request_method, result_value)
        else:
            encoded_result = {
                "decision": "accept" if str(result_value.get("decision")).strip().lower() == "accept" else "decline",
            }

        response_id: int | str = int(request_id_text) if request_id_text.isdigit() else request_id_text
        response_payload: Dict[str, object] = {
            "jsonrpc": "2.0",
            "id": response_id,
            "result": encoded_result,
        }
        line = json.dumps(response_payload, ensure_ascii=False)
        try:
            raw_conversation = pending.get("conversation_id")
            conversation_label = raw_conversation if isinstance(raw_conversation, str) and raw_conversation else self.get_raw_label()
            self._raw_log_fn("out", conversation_label, line)
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

    def _approval_request_method(self, pending: Dict[str, object]) -> Optional[str]:
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
        params: Optional[Dict[str, object]] = None,
        conversation_id: Optional[str] = None,
        timeout: float = 6.0,
    ) -> Dict[str, object]:
        await self.ensure_ready()
        return await self._rpc_request_unchecked(
            method,
            params=params,
            conversation_id=conversation_id,
            timeout=timeout,
        )

    async def rpc_request_unchecked(
        self,
        method: str,
        *,
        params: Optional[Dict[str, object]] = None,
        conversation_id: Optional[str] = None,
        timeout: float = 6.0,
    ) -> Dict[str, object]:
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
        params: Optional[Dict[str, object]] = None,
        conversation_id: Optional[str] = None,
        timeout: float = 6.0,
    ) -> Dict[str, object]:
        req_id = self._next_request_id()
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self._rpc_waiters[req_id] = future
        self._request_conversations[req_id] = conversation_id
        payload: Dict[str, object] = {"id": int(req_id), "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_payload(payload, conversation_id=conversation_id)
        try:
            try:
                response = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"{method} request timed out") from exc
        finally:
            self._rpc_waiters.pop(req_id, None)
            self._request_conversations.pop(req_id, None)
        return await self._decode_rpc_response_result(method, response, conversation_id=conversation_id)

    async def resume_thread_until_idle(
        self,
        *,
        params: Dict[str, object],
        conversation_id: str,
        thread_id: str,
    ) -> Dict[str, object]:
        await self.ensure_ready()
        return await self._resume_thread_until_idle_unchecked(
            params=params,
            conversation_id=conversation_id,
            thread_id=thread_id,
        )

    async def _resume_thread_until_idle_unchecked(
        self,
        *,
        params: Dict[str, object],
        conversation_id: str,
        thread_id: str,
    ) -> Dict[str, object]:
        idle_waiter = self._begin_resume_idle_waiter(conversation_id, thread_id)
        req_id = self._next_request_id()
        response_future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self._rpc_waiters[req_id] = response_future
        self._request_conversations[req_id] = conversation_id
        await self._write_payload(
            {"id": int(req_id), "method": "thread/resume", "params": params},
            conversation_id=conversation_id,
        )
        decoded_response: Optional[Dict[str, object]] = None
        try:
            while True:
                waiters: set[asyncio.Future[Dict[str, object]]] = {idle_waiter}
                if decoded_response is None:
                    waiters.add(response_future)
                done, _pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                if response_future in done and decoded_response is None:
                    response = response_future.result()
                    decoded_response = await self._decode_rpc_response_result(
                        "thread/resume",
                        response,
                        conversation_id=conversation_id,
                    )
                    if idle_waiter.done():
                        break
                    continue
                if idle_waiter in done:
                    if idle_waiter.cancelled():
                        raise RuntimeError("thread/resume idle wait cancelled")
                    idle_waiter.result()
                    break
        finally:
            self._rpc_waiters.pop(req_id, None)
            self._request_conversations.pop(req_id, None)
            current_waiter = self._resume_idle_waiters.get(thread_id)
            if current_waiter is idle_waiter:
                self._resume_idle_waiters.pop(thread_id, None)
        if decoded_response is not None:
            return decoded_response
        self._remember_bindings(conversation_id=conversation_id, thread_id=thread_id, turn_id=None)
        return {
            "thread": {"id": thread_id},
            "_idle_ack": True,
            "_idle_ack_source": "thread/status/changed",
        }

    async def _decode_rpc_response_result(
        self,
        method: str,
        response: Dict[str, object],
        *,
        conversation_id: Optional[str],
    ) -> Dict[str, object]:
        if not _is_object_dict(response):
            raise RuntimeError("invalid rpc response")
        if response.get("error"):
            error = response.get("error")
            if _is_object_dict(error):
                message_value = error.get("message") or "rpc error"
                message = message_value if isinstance(message_value, str) else str(message_value)
            else:
                message = str(error)
            raise RuntimeError(message)
        protocol = await get_runtime_protocol()
        decoded_result: object = decode_response_result(protocol, method, response.get("result", response))
        if not _is_object_dict(decoded_result):
            raise RuntimeError("invalid rpc response result")
        decoded_result_dict = _object_dict(decoded_result)
        self._remember_response_bindings(conversation_id=conversation_id, result=decoded_result_dict)
        return decoded_result_dict

    async def notify(
        self,
        method: str,
        *,
        params: Optional[Dict[str, object]] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        await self.ensure_ready()
        await self._notify_unchecked(method, params=params, conversation_id=conversation_id)

    async def _notify_unchecked(
        self,
        method: str,
        *,
        params: Optional[Dict[str, object]] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        payload: Dict[str, object] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._write_payload(payload, conversation_id=conversation_id)

    async def _get_or_start_shell(self) -> str:
        mgr = await self._fws_getter()

        if self._shell_id:
            shell = await mgr.get_shell(self._shell_id)
            if (
                shell
                and shell.status == "running"
                and getattr(shell, "spec_id", "") == "app_server_observed"
                and _shell_record_matches_current_app(shell)
            ):
                return self._shell_id
            self._shell_id = None

        adopted = await self._adopt_existing_shell(mgr)
        if adopted:
            self._set_shell(adopted)
            return adopted

        shell_id = await self._start_new_shell(mgr)
        self._set_shell(shell_id)
        return shell_id

    async def _adopt_existing_shell(self, mgr: ShellManager) -> Optional[str]:
        try:
            records = await mgr.list_shells()
        except Exception:
            return None
        for rec in records:
            if rec.status != "running":
                continue
            if (rec.label or "") != _TRANSPORT_LABEL:
                continue
            if getattr(rec, "spec_id", "") != "app_server_observed":
                continue
            if not _shell_record_matches_current_app(rec):
                continue
            return rec.id
        return None

    async def _start_new_shell(self, mgr: ShellManager) -> str:
        spec_path = Path(__file__).parent / "shellspec" / "app_server.yaml"
        orch = _load_orchestrator_factory()(mgr)
        shell = await orch.start_from_ref(
            f"{spec_path}#app_server_observed",
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
        self._clear_resume_idle_waiters("transport restarted")
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
        return bool(state and state.process.stdin)

    async def _ensure_reader(self, shell_id: str) -> None:
        if self._reader_task and not self._reader_task.done():
            self._ensure_event_router()
            return
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdin:
            raise RuntimeError("codex extension transport pipe not available")
        self._stdin = state.process.stdin
        subscription = await mgr.subscribe_output_bytes(shell_id)
        self._stdout_subscription = subscription
        self._ensure_event_router()
        self._reader_task = asyncio.create_task(
            self._reader_loop(shell_id, subscription),
            name="codex-extension-reader",
        )

    def _ensure_event_router(self) -> None:
        if self._event_queue is None:
            self._event_queue = asyncio.Queue()
        if self._event_router_task and not self._event_router_task.done():
            return
        self._event_router_task = asyncio.create_task(
            self._event_router_loop(self._event_queue),
            name="codex-extension-event-router",
        )

    async def _terminate_event_router(self) -> None:
        task = self._event_router_task
        self._event_router_task = None
        self._event_queue = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _terminate_reader(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        subscription = self._stdout_subscription
        shell_id = self._shell_id
        if subscription is not None and shell_id and self._stdout_subscription is subscription:
            mgr = await self._fws_getter()
            with contextlib.suppress(Exception):
                await mgr.unsubscribe_output_bytes(shell_id, subscription)
            if self._stdout_subscription is subscription:
                self._stdout_subscription = None
        await self._terminate_event_router()

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
        payload: Dict[str, object],
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
        try:
            async with self._write_lock:
                await mgr.write_to_pipe(shell_id, line + "\n")
        except (KeyError, RuntimeError) as exc:
            self._stdin = None
            raise RuntimeError("codex extension transport pipe not available") from exc

    def _enqueue_transport_event(
        self,
        label: str,
        payload: object,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_id: Optional[str],
    ) -> None:
        self._ensure_event_router()
        queue = self._event_queue
        if queue is None:
            self._raw_log_fn("err", conversation_id or self.get_raw_label(), "event_router_unavailable")
            return
        queue.put_nowait({
            "label": label,
            "payload": payload,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "request_id": request_id,
        })

    async def _event_router_loop(self, queue: asyncio.Queue[_TransportEvent]) -> None:
        while True:
            event = await queue.get()
            try:
                conversation_id = event["conversation_id"]
                thread_id = event["thread_id"]
                if conversation_id and thread_id:
                    self._persist_thread_id(conversation_id, thread_id)
                await self._route_transport_event(
                    event["label"],
                    event["payload"],
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    turn_id=event["turn_id"],
                    request_id=event["request_id"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._raw_log_fn("err", self.get_raw_label(), f"event_router_failed {exc}")

    async def _process_incoming_line(
        self,
        raw_line: bytes | bytearray | memoryview,
        pending_label: Optional[str],
    ) -> Optional[str]:
        raw = bytes(raw_line).strip()
        if not raw:
            return pending_label

        next_pending_label = pending_label
        if b"{" in raw and not raw.lstrip().startswith(b"{"):
            prefix, rest = raw.split(b"{", 1)
            if prefix.strip() and rest.strip().startswith(b"{"):
                next_pending_label = _line_text(prefix.strip()).strip()
                raw = b"{" + rest

        text = _line_text(raw)
        try:
            parsed_value = _decode_app_server_json_line(raw)
        except msgspec.DecodeError:
            self._raw_log_fn("in", self.get_raw_label(), text)
            if "/" in text or text.endswith("started") or text.endswith("completed"):
                next_pending_label = text
            return next_pending_label
        parsed = _object_dict(parsed_value)

        if parsed and "id" in parsed and ("result" in parsed or "error" in parsed) and "method" not in parsed:
            req_id = str(parsed.get("id"))
            conversation_id = self._request_conversations.get(req_id)
            self._raw_log_fn("in", conversation_id or self.get_raw_label(), text)
            waiter = self._rpc_waiters.get(req_id)
            if waiter and not waiter.done():
                waiter.set_result(parsed)
            return next_pending_label

        label = None
        payload: object = parsed_value
        raw_conversation_id: Optional[str] = None
        request_id: Optional[str] = None

        if next_pending_label:
            label = next_pending_label
            next_pending_label = None
            msg = _object_dict(parsed.get("msg"))
            if msg:
                payload = msg
                raw_conversation = parsed.get("conversationId")
                raw_conversation_id = raw_conversation if isinstance(raw_conversation, str) and raw_conversation else None
            else:
                payload = parsed_value
        elif parsed:
            if "method" in parsed:
                label_value = parsed.get("method")
                label = label_value if isinstance(label_value, str) else None
                params_obj = parsed.get("params", parsed_value)
                params = _object_dict(params_obj)
                payload = params
                if params:
                    param_conversation_id = params.get("conversationId")
                    if isinstance(param_conversation_id, str) and param_conversation_id:
                        raw_conversation_id = param_conversation_id
                    if (
                        label is not None
                        and label.startswith("codex/event/collab_")
                        and isinstance(params.get("msg"), dict)
                    ):
                        payload = _object_dict(params["msg"])
                        for key in ("id", "conversationId", "conversation_id", "threadId", "thread_id", "turnId", "turn_id"):
                            value = params.get(key)
                            payload_dict = _object_dict(payload)
                            if value is not None and payload_dict.get(key) is None:
                                payload_dict[key] = value
                                payload = payload_dict
                if parsed.get("id") is not None:
                    request_id = str(parsed.get("id"))
            elif isinstance(parsed.get("msg"), dict):
                msg = _object_dict(parsed.get("msg"))
                label = f"codex/event/{msg.get('type', 'event')}"
                payload = msg
                raw_conversation = parsed.get("conversationId")
                raw_conversation_id = raw_conversation if isinstance(raw_conversation, str) and raw_conversation else None
            elif "type" in parsed:
                label = str(parsed.get("type"))
                payload = parsed

        raw_conversation = parsed.get("conversationId")
        if isinstance(raw_conversation, str) and raw_conversation:
            raw_conversation_id = raw_conversation

        conversation_id = self._resolve_conversation_id(raw_conversation_id, payload)
        self._raw_log_fn("in", conversation_id or raw_conversation_id or self.get_raw_label(), text)

        if not label:
            return next_pending_label

        thread_id = self._get_thread_id(payload, fallback=raw_conversation_id)
        turn_id = self._get_turn_id(payload)
        if conversation_id:
            self._remember_bindings(conversation_id=conversation_id, thread_id=thread_id, turn_id=turn_id)
        self._note_resume_idle_event(
            conversation_id=conversation_id,
            thread_id=thread_id,
            label=label,
            payload=payload,
        )
        self._enqueue_transport_event(
            label,
            payload,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id=request_id,
        )
        return next_pending_label

    async def _reader_loop(self, shell_id: str, subscription: _OutputSubscription) -> None:
        pending_label: Optional[str] = None
        buffer = b""
        max_buffer = 4_000_000
        mgr = await self._fws_getter()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(subscription.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    state = mgr.get_pipe_state(shell_id)
                    if not state or state.process.returncode is not None:
                        break
                    continue
                if not chunk:
                    state = mgr.get_pipe_state(shell_id)
                    if not state or state.process.returncode is not None:
                        break
                    continue
                buffer += chunk
                if len(buffer) > max_buffer and b"\n" not in buffer:
                    self._raw_log_fn("in", self.get_raw_label(), "[warn] dropping oversized line")
                    buffer = b""
                    continue
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        pending_label = await self._process_incoming_line(line, pending_label)
                    except Exception as exc:
                        self._raw_log_fn("err", self.get_raw_label(), f"reader_process_failed {exc}")
                        continue
            if buffer:
                try:
                    await self._process_incoming_line(buffer, pending_label)
                except Exception as exc:
                    self._raw_log_fn("err", self.get_raw_label(), f"reader_process_failed {exc}")
        finally:
            await self._terminate_event_router()
            with contextlib.suppress(Exception):
                await mgr.unsubscribe_output_bytes(shell_id, subscription)
            if self._stdout_subscription is subscription:
                self._stdout_subscription = None
            self._initialized = False
            self._resumed_threads.clear()
            self._thread_conversations.clear()
            self._turn_conversations.clear()
            self._pending_approval_requests.clear()
            self._clear_resume_idle_waiters("transport reader stopped")
            self._router.reset()
            self._stdin = None
            self._fail_waiters("transport reader stopped")
            if self._shell_id == shell_id:
                self._shell_id = None
            self._reader_task = None

    async def route_event(
        self,
        label: str,
        payload: object,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        turn_id: Optional[str],
        request_id: Optional[str],
    ) -> Dict[str, object]:
        protocol = await get_runtime_protocol()
        routed_payload: object = _object_dict(payload) if _is_object_dict(payload) else payload
        if request_id is not None and _is_object_dict(routed_payload) and routed_payload.get("_request_id") is None:
            routed_payload["_request_id"] = str(request_id)
        routed = self._router.route_event(
            protocol,
            label=label,
            payload=routed_payload,
            thread_id=thread_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            extract_item_text=_extract_item_text,
        )
        if conversation_id and _is_object_dict(routed):
            descriptors = routed.get("approval_descriptors")
            if _is_object_list(descriptors):
                for descriptor in descriptors:
                    if _is_object_dict(descriptor):
                        self._persist_pending_approval(conversation_id, descriptor)
            bind_thread_ids = routed.get("bind_thread_ids")
            if _is_object_list(bind_thread_ids):
                for bound_thread_id in bind_thread_ids:
                    if isinstance(bound_thread_id, str) and bound_thread_id:
                        self._remember_bindings(
                            conversation_id=conversation_id,
                            thread_id=bound_thread_id,
                        )
            clear_ids = routed.get("clear_live_approval_ids")
            if _is_object_list(clear_ids):
                for pending_id in clear_ids:
                    self._pending_approval_requests.pop(str(pending_id or "").strip(), None)
        return routed

    async def _route_transport_event(
        self,
        label: str,
        payload: object,
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

        extra_events: List[Dict[str, object]] = []
        if self._auth_event_handler is not None:
            handled_events = await self._auth_event_handler(
                label=label,
                payload=payload,
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
                request_id=request_id,
            )
            if _is_object_list(handled_events):
                extra_events = [event for event in handled_events if _is_object_dict(event)]

        if (not _is_object_dict(routed) or not routed.get("handled")) and not extra_events:
            return

        routed_result = routed if _is_object_dict(routed) else {}
        routed_conversation_id = routed_result.get("conversation_id")
        resolved_conversation_id = (
            routed_conversation_id
            if isinstance(routed_conversation_id, str) and routed_conversation_id
            else conversation_id
        )
        descriptors_by_request: Dict[str, Dict[str, object]] = {}
        routed_descriptors = routed_result.get("approval_descriptors")
        if _is_object_list(routed_descriptors):
            for descriptor in routed_descriptors:
                if not _is_object_dict(descriptor):
                    continue
                request_id_text = str(descriptor.get("request_id") or descriptor.get("id") or "").strip()
                if request_id_text:
                    descriptors_by_request[request_id_text] = descriptor

        transcript_entries = routed_result.get("transcript_entries")
        if resolved_conversation_id and _is_object_list(transcript_entries):
            for entry in transcript_entries:
                if _is_object_dict(entry):
                    await self._transcript_fn(resolved_conversation_id, entry)

        next_turn_id = routed_result.get("set_turn_id")
        if resolved_conversation_id and isinstance(next_turn_id, str) and next_turn_id:
            self._persist_turn_id(resolved_conversation_id, next_turn_id)
        elif resolved_conversation_id and routed_result.get("clear_turn_id"):
            self._persist_turn_id(resolved_conversation_id, None)

        events: List[Dict[str, object]] = []
        routed_events = routed_result.get("events")
        if _is_object_list(routed_events):
            events.extend(event for event in routed_events if _is_object_dict(event))
        events.extend(extra_events)
        for event in events:
            outbound = dict(event)
            if resolved_conversation_id and outbound.get("conversation_id") is None:
                outbound["conversation_id"] = resolved_conversation_id
            if request_id is not None and outbound.get("request_id") is None:
                outbound["request_id"] = request_id
            await self._broadcast_fn(outbound)

    def _persist_pending_approval(self, conversation_id: str, descriptor: Dict[str, object]) -> None:
        request_id_text = str(descriptor.get("request_id") or descriptor.get("id") or "").strip()
        if not request_id_text:
            return
        meta_dict = self._load_meta(conversation_id) or {}
        raw_settings = meta_dict.get("settings")
        settings = _object_dict(raw_settings)
        created_at = str(descriptor.get("created_at") or datetime.now(timezone.utc).isoformat())
        render_event = _object_dict(descriptor.get("render_event"))
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
            "request_params": _object_dict(descriptor.get("request_params")),
            "payload": _object_dict(descriptor.get("payload")),
            "thread_id": descriptor.get("thread_id") or meta_dict.get("thread_id"),
            "turn_id": descriptor.get("turn_id"),
            "runtime_signature": descriptor.get("runtime_signature") or meta_dict.get("thread_runtime_signature"),
            "runtime_instance_id": descriptor.get("runtime_instance_id") or self.runtime_instance_id(),
            "transcript_anchor": _object_dict(descriptor.get("transcript_anchor")) or {"turn_id": descriptor.get("turn_id")},
            "source": descriptor.get("source") or "live",
            "created_at": created_at,
            "render_event": render_event,
        }

        upsert = self._meta_fns.get("upsert_pending_approval")
        if callable(upsert):
            upsert(conversation_id, persisted)
        else:
            raw_pending = meta_dict.get("pending_approvals")
            pending_dict = _object_dict(raw_pending)
            pending_dict[request_id_text] = persisted
            meta_dict["pending_approvals"] = pending_dict
            self._save_meta(conversation_id, meta_dict)

        self._pending_approval_requests[request_id_text] = {
            "conversation_id": conversation_id,
            "kind": persisted.get("kind"),
            "method": persisted.get("request_method"),
            "thread_id": persisted.get("thread_id"),
        }

    def _resolve_conversation_id(
        self,
        raw_conversation_id: Optional[str],
        payload: object,
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
                data = cast(object, json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if _is_object_dict(data) and data.get("thread_id") == thread_id:
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
                data = cast(object, json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if _is_object_dict(data) and data.get("turn_id") == turn_id:
                return child.name
        return None

    def _shell_cwd(self) -> str:
        config_path = _CONFIG_ROOT / "config.json"
        try:
            if config_path.exists():
                data = cast(object, json.loads(config_path.read_text(encoding="utf-8")))
                cwd = _object_dict(data).get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
        except Exception:
            pass
        return str(Path.cwd())

    def _load_meta(self, conversation_id: str) -> Optional[Dict[str, object]]:
        load = self._meta_fns.get("load")
        if callable(load):
            meta = load(conversation_id)
            return _object_dict(meta) if _is_object_dict(meta) else None
        path = self._conversation_meta_path(conversation_id)
        if not path.exists():
            return None
        try:
            data = cast(object, json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None
        return _object_dict(data) if _is_object_dict(data) else None

    def _save_meta(self, conversation_id: str, meta: Dict[str, object]) -> None:
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
        if not _is_object_dict(meta):
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
        if not _is_object_dict(meta):
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

    def _begin_resume_idle_waiter(self, conversation_id: str, thread_id: str) -> asyncio.Future[Dict[str, object]]:
        existing = self._resume_idle_waiters.get(thread_id)
        if existing is not None and not existing.done():
            return existing
        future: asyncio.Future[Dict[str, object]] = asyncio.get_running_loop().create_future()
        self._resume_idle_waiters[thread_id] = future
        self._remember_bindings(conversation_id=conversation_id, thread_id=thread_id, turn_id=None)
        self._raw_log_fn("out", conversation_id, f"resume_idle_wait_begin thread={thread_id[:8]}")
        return future

    def _note_resume_idle_event(
        self,
        *,
        conversation_id: Optional[str],
        thread_id: Optional[str],
        label: str,
        payload: object,
    ) -> None:
        if label != "thread/status/changed" or not thread_id:
            return
        waiter = self._resume_idle_waiters.get(thread_id)
        if waiter is None or waiter.done():
            return
        payload_dict = _object_dict(payload)
        status = _object_dict(payload_dict.get("status"))
        status_type = str(status.get("type") or "").strip().lower()
        if status_type != "idle":
            return
        waiter.set_result({
            "thread_id": thread_id,
            "status": status_type,
        })
        self._raw_log_fn(
            "in",
            conversation_id or self._thread_conversations.get(thread_id) or self.get_raw_label(),
            f"resume_idle_wait_release thread={thread_id[:8]} status=idle",
        )

    def _clear_resume_idle_waiters(self, _reason: str) -> None:
        for waiter in self._resume_idle_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._resume_idle_waiters.clear()

    def _remember_response_bindings(
        self,
        *,
        conversation_id: Optional[str],
        result: object,
    ) -> None:
        result_dict = _object_dict(result)
        if not conversation_id or not result_dict:
            return
        thread_id: Optional[str] = None
        turn_id: Optional[str] = None
        thread = result_dict.get("thread")
        if _is_object_dict(thread):
            thread_value = thread.get("id")
            if isinstance(thread_value, str) and thread_value:
                thread_id = thread_value
        turn = result_dict.get("turn")
        if _is_object_dict(turn):
            turn_value = turn.get("id")
            if isinstance(turn_value, str) and turn_value:
                turn_id = turn_value
        if thread_id or turn_id:
            self._remember_bindings(
                conversation_id=conversation_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )

    def _get_thread_id(self, payload: object, fallback: Optional[str]) -> Optional[str]:
        payload_dict = _object_dict(payload)
        if payload_dict:
            thread = payload_dict.get("thread")
            if _is_object_dict(thread):
                thread_id = thread.get("id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
            for key in ("threadId", "thread_id", "conversationId", "conversation_id", "sender_thread_id"):
                value = payload_dict.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(fallback, str) and fallback:
            return fallback
        return None

    def _get_turn_id(self, payload: object) -> Optional[str]:
        payload_dict = _object_dict(payload)
        if not payload_dict:
            return None
        for key in ("turnId", "turn_id"):
            value = payload_dict.get(key)
            if isinstance(value, str) and value:
                return value
        turn = payload_dict.get("turn")
        if _is_object_dict(turn):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id:
                return turn_id
        if payload_dict.get("id") and payload_dict.get("status") is not None:
            value = payload_dict.get("id")
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
        self._clear_resume_idle_waiters(message)
