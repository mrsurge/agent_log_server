"""
Copilot SDK Event Router

Translates Copilot SDK SessionEvent objects to our internal event format.
This allows Copilot CLI (and all its models) to work with our existing
frontend, transcript, and replay infrastructure.

The router speaks Copilot SDK on one side (SessionEvent from the vendored copilot package)
and our internal format on the other (to _broadcast_appserver_ui).
"""

import json
import re
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Optional, Callable, Awaitable, Protocol, TypeAlias, TypedDict, cast, runtime_checkable
from datetime import datetime, timezone
from uuid import uuid4

from ._vendor.copilot import SessionEvent
from ._vendor.copilot.generated.session_events import (
    Data as SessionEventData,
    SessionEventType,
)
from ..message_card_contracts import (
    build_assistant_delta_event,
    build_assistant_finalize_event,
    build_message_event,
    build_message_transcript_entry,
    build_reasoning_delta_event,
    build_reasoning_finalize_event,
    build_reasoning_transcript_entry,
)
from ..tool_card_contracts import build_tool_card_request, build_tool_card_response


def utc_ts() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _is_debug() -> bool:
    """Check server DEBUG_MODE without circular import."""
    try:
        import server
        return getattr(server, 'DEBUG_MODE', False)
    except ImportError:
        return False


def _looks_like_diff(text: str) -> bool:
    """Check if text looks like a unified diff (not just any tool output)."""
    for line in text.splitlines()[:5]:
        stripped = line.lstrip()
        if stripped.startswith("---") or stripped.startswith("+++") or stripped.startswith("@@"):
            return True
    return False


_COPILOT_VIEW_LINE_RE = re.compile(r"^\s*(\d+)\.(.*)$")


@runtime_checkable
class _SupportsToDict(Protocol):
    def to_dict(self) -> object: ...


PayloadDict: TypeAlias = dict[str, object]
_BroadcastFn: TypeAlias = Callable[[PayloadDict], Awaitable[None]]
_TranscriptFn: TypeAlias = Callable[[str, PayloadDict], Awaitable[None]]
_PlanStateProvider: TypeAlias = Callable[[str, PayloadDict], Awaitable[PayloadDict]]


class ViewLine(TypedDict):
    line_no: int
    content: str


class SubagentState(TypedDict):
    name: str
    intent: str


class ToolRenderState(TypedDict, total=False):
    kind: str
    tool_name: str
    title: str
    activity: str
    server: str
    tool: str
    arguments: PayloadDict
    request: object
    path: str | None
    view_range: object


class ToolCallState(TypedDict):
    id: str
    title: str
    tool_name: str
    arguments: object
    turn_id: str | None
    output: str
    subagent_id: str | None
    render_kind: str
    render_tool: str
    render_server: str
    render_arguments: PayloadDict
    render_request: object
    path: str
    view_range: object


def _object_mapping(value: object) -> PayloadDict | None:
    if isinstance(value, dict):
        return cast(PayloadDict, value)
    return None


def _mapping_str(mapping: PayloadDict, *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_card_request_payload(server_name: str, tool_name: str, arguments: object) -> object:
    return cast(object, build_tool_card_request(server_name, tool_name, arguments))


def _tool_card_response_payload(server_name: str, tool_name: str, response: object) -> object:
    return cast(object, build_tool_card_response(server_name, tool_name, response))


def _parse_copilot_view_lines(content: str) -> Optional[list[ViewLine]]:
    if not isinstance(content, str):
        return None
    if not content:
        return []

    parsed: list[ViewLine] = []
    for raw_line in content.splitlines():
        match = _COPILOT_VIEW_LINE_RE.match(raw_line)
        if not match:
            return None
        line_content = match.group(2)
        if line_content[:1] in {" ", "\t"}:
            line_content = line_content[1:]
        parsed.append({
            "line_no": int(match.group(1)),
            "content": line_content,
        })
    return parsed


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_value(asdict(value))
    if isinstance(value, _SupportsToDict):
        try:
            return _json_safe_value(value.to_dict())
        except Exception:
            pass
    return str(value)


# Tools that mutate files — only these get post-execution diff entries
_FILE_CHANGE_TOOLS = {
    "edit", "create", "write", "write_file", "apply_patch", "delete",
    "move", "rename", "insert", "replace", "patch",
}

_KNOWN_MCP_PREFIXES = (
    "agent-pty-blocks",
    "te2-mcp",
    "github-mcp-server",
    "simple-memory",
)


class CopilotEventRouter:
    """
    Translates Copilot SDK SessionEvent objects to our internal event format.

    Copilot SDK sends SessionEvent objects via session.on(handler).
    We translate to our format:
    {
        "type": "assistant_delta",
        "conversation_id": "...",
        "id": "msg_1_2",
        "delta": "Hello",
        ...
    }
    """

    def __init__(
        self,
        conversation_id: str,
        broadcast_fn: _BroadcastFn,
        transcript_fn: _TranscriptFn,
        debug_trace: bool = False,
        plan_state_provider: Optional[_PlanStateProvider] = None,
        initial_model: Optional[str] = None,
        model_context_window_resolver: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
    ):
        self.conversation_id = conversation_id
        self.broadcast = broadcast_fn
        self.append_transcript = transcript_fn
        self.debug_trace = self._coerce_bool(debug_trace)
        self.plan_state_provider = plan_state_provider
        self._active_model: Optional[str] = self._normalize_id(initial_model)
        self._resolved_context_window: Optional[int] = None
        self._model_context_window_resolver = model_context_window_resolver

        # State tracking
        self.current_turn_id: Optional[str] = None
        self.current_message_id: Optional[str] = None
        self.current_reasoning_id: Optional[str] = None
        self.current_message_text: str = ""
        self.current_message_subagent_id: Optional[str] = None
        self.current_thought_text: str = ""
        self.tool_calls: dict[str, ToolCallState] = {}
        self._seq: int = 0

        # Block tracking for interleaved reasoning/message
        self._last_block_type: Optional[str] = None
        self._suppressed_tools: set = set()  # tool_call_ids for UI-only tools (e.g. report_intent)
        self._active_subagents: dict[str, SubagentState] = {}  # tool_call_id -> subagent info
        self._known_subagent_ids: set = set()  # all seen subagent ids for validation
        self._subagent_tool_ids: set = set()  # tool_call_ids that belong to a subagent
        self._recorded_reasoning_ids: set = set()  # reasoning ids already persisted this turn

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _coerce_bool(value: object) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def set_debug_trace(self, enabled: object) -> None:
        self.debug_trace = self._coerce_bool(enabled)

    @staticmethod
    def _normalize_mode_kind(value: object) -> Optional[str]:
        candidate = cast(object, value.value) if isinstance(value, Enum) else value
        if candidate is None:
            return None
        text = str(candidate).strip()
        return text or None

    @staticmethod
    def _coerce_int(value: object) -> Optional[int]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        try:
            text = str(value).strip()
            if not text:
                return None
            return int(float(text))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _first_int(cls, *values: object) -> Optional[int]:
        for value in values:
            coerced = cls._coerce_int(value)
            if coerced is not None:
                return coerced
        return None

    def _remember_active_model(self, data: SessionEventData) -> None:
        candidate = None
        for value in (
            data.current_model,
            data.selected_model,
            data.new_model,
            data.model,
        ):
            normalized = self._normalize_id(value)
            if normalized:
                candidate = normalized
                break
        if candidate and candidate != self._active_model:
            self._active_model = candidate
            self._resolved_context_window = None

    async def _resolve_context_window(self, raw_value: object = None) -> Optional[int]:
        explicit = self._coerce_int(raw_value)
        if explicit is not None and explicit > 0:
            self._resolved_context_window = explicit
            return explicit
        if self._resolved_context_window is not None and self._resolved_context_window > 0:
            return self._resolved_context_window
        if not self._active_model or not callable(self._model_context_window_resolver):
            return None
        try:
            resolved = await self._model_context_window_resolver(self._active_model)
        except Exception:
            return None
        resolved_int = self._coerce_int(resolved)
        if resolved_int is not None and resolved_int > 0:
            self._resolved_context_window = resolved_int
            return resolved_int
        return None

    async def _emit_token_count(
        self,
        *,
        total: object,
        input_tokens: object = None,
        output_tokens: object = None,
        cached_input_tokens: object = None,
        context_window: object = None,
        source: Optional[str] = None,
    ) -> None:
        total_int = self._coerce_int(total)
        if total_int is None:
            return

        input_int = self._coerce_int(input_tokens)
        output_int = self._coerce_int(output_tokens)
        cached_int = self._coerce_int(cached_input_tokens)
        context_window_int = await self._resolve_context_window(context_window)

        event: PayloadDict = {
            "type": "token_count",
            "conversation_id": self.conversation_id,
            "total": total_int,
            "turn_id": self.current_turn_id,
        }
        entry: PayloadDict = {
            "role": "token_usage",
            "total": total_int,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        }

        if input_int is not None:
            event["input_tokens"] = input_int
            entry["input_tokens"] = input_int
        if output_int is not None:
            event["output_tokens"] = output_int
            entry["output_tokens"] = output_int
        if cached_int is not None:
            event["cached_input_tokens"] = cached_int
            entry["cached_input_tokens"] = cached_int
        if input_int is not None and cached_int is not None:
            active_context = max(0, input_int - cached_int)
            event["active_context"] = active_context
            entry["active_context"] = active_context
        if context_window_int is not None and context_window_int > 0:
            event["context_window"] = context_window_int
            entry["context_window"] = context_window_int
        if source:
            event["source"] = source
            entry["source"] = source

        await self._emit(event)
        await self._record(entry)

    async def _handle_context_compacted(self, data: SessionEventData, *, source: str) -> None:
        context_window = self._coerce_int(data.token_limit)
        total = self._first_int(
            data.post_compaction_tokens,
            data.post_truncation_tokens_in_messages,
            data.current_tokens,
            data.pre_compaction_tokens,
            data.pre_truncation_tokens_in_messages,
        )

        if total is not None:
            await self._emit_token_count(
                total=total,
                context_window=context_window,
                source=source,
            )

        messages_removed = self._first_int(
            data.messages_removed,
            data.messages_removed_during_truncation,
        )
        tokens_removed = self._first_int(
            data.tokens_removed,
            data.tokens_removed_during_truncation,
        )
        compacted_event: PayloadDict = {
            "type": "context_compacted",
            "conversation_id": self.conversation_id,
            "turn_id": self.current_turn_id,
            "source": source,
        }
        compacted_entry: PayloadDict = {
            "role": "context_compacted",
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
            "source": source,
        }
        if messages_removed is not None:
            compacted_event["messages_removed"] = messages_removed
            compacted_entry["messages_removed"] = messages_removed
        if tokens_removed is not None:
            compacted_event["tokens_removed"] = tokens_removed
            compacted_entry["tokens_removed"] = tokens_removed

        await self._emit(compacted_event)
        await self._record(compacted_entry)

    async def _emit_mode(
        self,
        kind_raw: object,
        *,
        source: str,
        previous_raw: object = None,
    ) -> None:
        kind = self._normalize_mode_kind(kind_raw)
        if not kind:
            return
        previous_kind = self._normalize_mode_kind(previous_raw)
        event = {
            "type": "mode",
            "conversation_id": self.conversation_id,
            "kind": kind,
            "turn_id": self.current_turn_id,
        }
        entry = {
            "role": "mode",
            "kind": kind,
            "turn_id": self.current_turn_id,
            "timestamp": utc_ts(),
            "event": source,
        }
        if previous_kind:
            event["previous_kind"] = previous_kind
            entry["previous_kind"] = previous_kind
        await self._emit(event)
        await self._record(entry)

    async def _emit(self, event: PayloadDict) -> None:
        # EVERY _emit() MUST HAVE A MATCHING _record() WITH THE SAME FIELDS.
        # THE TRANSCRIPT IS THE REPLAY SOURCE. IF IT'S NOT RECORDED, IT DOESN'T EXIST ON PLAYBACK.
        event["seq"] = self._next_seq()
        await self.broadcast(event)

    async def _record(self, entry: PayloadDict) -> None:
        # EVERY _record() MUST MIRROR THE CORRESPONDING _emit() — SAME KEYS, SAME VALUES.
        # REPLAY MUST BE AN EXACT MIRROR OF THE LIVE FEED. NO EXCEPTIONS.
        entry["seq"] = self._seq
        await self.append_transcript(self.conversation_id, entry)

    @staticmethod
    def _normalize_id(value: object) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _is_known_subagent_id(self, value: object) -> bool:
        candidate = self._normalize_id(value)
        if not candidate:
            return False
        return candidate in self._active_subagents or candidate in self._known_subagent_ids

    def _resolve_message_subagent_id(self, event: SessionEvent) -> Optional[str]:
        data = event.data
        candidate = self._normalize_id(data.parent_tool_call_id)
        if candidate and self._is_known_subagent_id(candidate):
            return candidate
        return None

    def _resolve_subagent_event_id(
        self,
        event: SessionEvent,
        *,
        allow_single_active: bool = False,
    ) -> Optional[str]:
        data = event.data
        for candidate in (
            data.tool_call_id,
            data.parent_tool_call_id,
        ):
            resolved = self._normalize_id(candidate)
            if resolved and self._is_known_subagent_id(resolved):
                return resolved
        if allow_single_active and len(self._active_subagents) == 1:
            return next(iter(self._active_subagents))
        return None

    def _resolve_reasoning_id(self, data: SessionEventData | None = None) -> Optional[str]:
        for candidate in (
            self._extract_reasoning_id(data),
            self.current_reasoning_id,
        ):
            normalized = self._normalize_id(candidate)
            if normalized:
                return normalized
        return None

    def _extract_reasoning_id(self, data: SessionEventData | None = None) -> Optional[str]:
        if data is None:
            return None
        return self._normalize_id(data.reasoning_id)

    def _extract_message_id(self, data: SessionEventData | None = None) -> Optional[str]:
        if data is None:
            return None
        return self._normalize_id(data.message_id)

    @staticmethod
    def _build_local_id(prefix: str, token: Optional[str] = None) -> str:
        normalized = str(token).strip() if token is not None else ""
        return f"{prefix}_{normalized or uuid4().hex}"

    @staticmethod
    def _extract_reasoning_text(data: SessionEventData | None) -> str:
        if data is None:
            return ""
        return data.reasoning_text or ""

    def _ensure_message_entry_id(self) -> str:
        if not self.current_message_id:
            self.current_message_id = self._build_local_id("msg")
        return self.current_message_id

    def _ensure_reasoning_entry_id(self) -> str:
        if not self.current_reasoning_id:
            self.current_reasoning_id = self._build_local_id("reasoning")
        return self.current_reasoning_id

    async def _record_reasoning(self, text: str, *, data: SessionEventData | None = None) -> bool:
        if not text:
            return False

        reasoning_id = self._resolve_reasoning_id(data)
        if reasoning_id and reasoning_id in self._recorded_reasoning_ids:
            self.current_thought_text = ""
            return False

        if not reasoning_id:
            reasoning_id = self._build_local_id("reasoning")
            self.current_reasoning_id = reasoning_id

        await self._emit(build_reasoning_finalize_event(
            entry_id=reasoning_id,
            text=text,
            conversation_id=self.conversation_id,
            turn_id=self.current_turn_id,
        ))
        await self._record(build_reasoning_transcript_entry(
            entry_id=reasoning_id,
            text=text,
            timestamp=utc_ts(),
            turn_id=self.current_turn_id,
        ))

        self._recorded_reasoning_ids.add(reasoning_id)
        self.current_thought_text = ""
        return True

    async def _finalize_message(self, text: str, *, subagent_id: Optional[str]) -> None:
        entry_id = self._ensure_message_entry_id()
        await self._emit(build_assistant_finalize_event(
            entry_id=entry_id,
            text=text,
            conversation_id=self.conversation_id,
            turn_id=self.current_turn_id,
            subagent_id=subagent_id,
        ))
        await self._record(build_message_transcript_entry(
            role="assistant",
            entry_id=entry_id,
            text=text,
            timestamp=utc_ts(),
            turn_id=self.current_turn_id,
            subagent_id=subagent_id,
        ))

        self.current_message_text = ""
        self.current_message_subagent_id = None

    async def _trace_subagent_provenance(
        self,
        event: SessionEvent,
        *,
        scope: str,
        decision: str,
        resolved_subagent_id: Optional[str] = None,
    ) -> None:
        if not self.debug_trace:
            return
        data = event.data
        payload = {
            "type": "debug_trace",
            "role": "debug_trace",
            "internal": True,
            "conversation_id": self.conversation_id,
            "scope": scope,
            "decision": decision,
            "event_type": event.type.value,
            "sdk_event_id": self._normalize_id(event.id),
            "parent_id": self._normalize_id(event.parent_id),
            "tool_call_id": self._normalize_id(data.tool_call_id),
            "parent_tool_call_id": self._normalize_id(data.parent_tool_call_id),
            "resolved_subagent_id": self._normalize_id(resolved_subagent_id),
            "turn_id": self.current_turn_id,
            "active_subagent_ids": sorted(str(key) for key in self._active_subagents.keys()),
        }
        await self._emit(dict(payload))
        await self._record(dict(payload))

    async def _emit_subagent_end(self, subagent_id: str, *, summary: str, success: bool) -> None:
        subagent_event = {
            "type": "subagent_end",
            "conversation_id": self.conversation_id,
            "id": subagent_id,
            "turn_id": self.current_turn_id,
            "summary": summary,
            "success": success,
        }
        await self._emit(subagent_event)
        await self._record({
            "role": "subagent_end",
            "id": subagent_id,
            "turn_id": self.current_turn_id,
            "summary": summary,
            "success": success,
            "timestamp": utc_ts(),
        })

    async def route_event(self, event: SessionEvent) -> None:
        """Route a Copilot SDK SessionEvent to the appropriate handler."""
        etype = event.type
        data = event.data
        self._remember_active_model(data)
        if _is_debug():
            print(f"[ROUTER-EVENT] type={etype} id={event.id} parent_id={event.parent_id} data_type={type(data).__name__}")

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            await self._handle_message_delta(event)
        elif etype == SessionEventType.ASSISTANT_REASONING_DELTA:
            await self._handle_reasoning_delta(data)
        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            await self._handle_message_complete(event)
        elif etype == SessionEventType.ASSISTANT_REASONING:
            await self._handle_reasoning_complete(data)
        elif etype == SessionEventType.ASSISTANT_TURN_START:
            await self._handle_turn_start(data)
        elif etype == SessionEventType.ASSISTANT_TURN_END:
            await self._handle_turn_end(data)
        elif etype == SessionEventType.TOOL_EXECUTION_START:
            await self._handle_tool_start(event)
        elif etype == SessionEventType.TOOL_EXECUTION_COMPLETE:
            await self._handle_tool_complete(event)
        elif etype == SessionEventType.TOOL_EXECUTION_PROGRESS:
            await self._handle_tool_progress(event)
        elif etype == SessionEventType.TOOL_EXECUTION_PARTIAL_RESULT:
            await self._handle_tool_progress(event)
        elif etype == SessionEventType.ASSISTANT_INTENT:
            await self._handle_intent(data)
        elif etype == SessionEventType.ASSISTANT_USAGE:
            await self._handle_usage(data, source="assistant.usage")
        elif etype == SessionEventType.SESSION_USAGE_INFO:
            await self._handle_usage(data, source="session.usage_info")
        elif etype == SessionEventType.SESSION_CONTEXT_CHANGED:
            await self._handle_usage(data, source="session.context_changed")
        elif etype == SessionEventType.SESSION_ERROR:
            await self._handle_error(data)
        elif etype == SessionEventType.SESSION_START:
            pass  # Handled by client
        elif etype == SessionEventType.SESSION_RESUME:
            pass  # Handled by client
        elif etype == SessionEventType.SESSION_COMPACTION_START:
            pass
        elif etype == SessionEventType.SESSION_COMPACTION_COMPLETE:
            await self._handle_context_compacted(data, source="session.compaction_complete")
        elif etype == SessionEventType.SESSION_TRUNCATION:
            await self._handle_context_compacted(data, source="session.truncation")
        elif etype == SessionEventType.SESSION_IDLE:
            await self._emit({
                "type": "activity",
                "conversation_id": self.conversation_id,
                "label": "idle",
                "active": False,
                "turn_id": self.current_turn_id,
            })
        elif etype == SessionEventType.SESSION_MODE_CHANGED:
            await self._handle_mode_changed(data)
        elif etype == SessionEventType.SESSION_PLAN_CHANGED:
            await self._handle_plan_changed(data)
        # Subagent events
        elif etype == SessionEventType.SUBAGENT_STARTED:
            sa_id = data.tool_call_id or str(event.id)
            intent = data.intent or ""
            name = data.agent_display_name or data.agent_name or "subagent"
            self._active_subagents[sa_id] = {"name": name, "intent": intent}
            self._known_subagent_ids.add(sa_id)
            await self._trace_subagent_provenance(
                event,
                scope="subagent_lifecycle",
                decision="subagent_started_registered",
                resolved_subagent_id=sa_id,
            )
            if _is_debug():
                print(f"[SUBAGENT-DEBUG] SUBAGENT_STARTED: sa_id={sa_id} event.id={event.id} "
                      f"data.tool_call_id={data.tool_call_id} event.parent_id={event.parent_id}")
            sa_evt = {
                "type": "subagent_start",
                "conversation_id": self.conversation_id,
                "id": sa_id,
                "turn_id": self.current_turn_id,
                "name": name,
                "intent": intent,
            }
            await self._emit(sa_evt)
            await self._record({
                "role": "subagent_start",
                "id": sa_id,
                "turn_id": self.current_turn_id,
                "name": name,
                "intent": intent,
                "timestamp": utc_ts(),
            })
        elif etype == SessionEventType.SUBAGENT_SELECTED:
            pass  # Selection happens before start, no UI needed
        elif etype == SessionEventType.SUBAGENT_DESELECTED:
            sa_id = self._resolve_subagent_event_id(event, allow_single_active=True)
            await self._trace_subagent_provenance(
                event,
                scope="subagent_lifecycle",
                decision="subagent_deselected_resolved" if sa_id else "subagent_deselected_unresolved",
                resolved_subagent_id=sa_id,
            )
            if sa_id and sa_id in self._active_subagents:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary="", success=True)
        elif etype == SessionEventType.SUBAGENT_COMPLETED:
            sa_id = self._resolve_subagent_event_id(event, allow_single_active=True)
            summary = data.summary or ""
            success = data.success if isinstance(data.success, bool) else True
            await self._trace_subagent_provenance(
                event,
                scope="subagent_lifecycle",
                decision="subagent_completed_resolved" if sa_id else "subagent_completed_unresolved",
                resolved_subagent_id=sa_id,
            )
            if sa_id:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary=summary, success=success)
        elif etype == SessionEventType.SUBAGENT_FAILED:
            sa_id = self._resolve_subagent_event_id(event, allow_single_active=True)
            error_value = data.error or data.error_reason or ""
            error = error_value if isinstance(error_value, str) else str(error_value)
            fail_summary = f"Failed: {error}" if error else "Failed"
            await self._trace_subagent_provenance(
                event,
                scope="subagent_lifecycle",
                decision="subagent_failed_resolved" if sa_id else "subagent_failed_unresolved",
                resolved_subagent_id=sa_id,
            )
            if sa_id:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary=fail_summary, success=False)

    # ── Message deltas ──────────────────────────────────────────────

    async def _handle_message_delta(self, event: SessionEvent) -> None:
        data = event.data
        text = data.delta_content or ""
        if not text:
            return

        started_new_message = False
        message_id = self._extract_message_id(data)
        if message_id and message_id != self.current_message_id:
            self.current_message_id = message_id
            self.current_message_subagent_id = None
            started_new_message = True
        elif not message_id and self._last_block_type != "message":
            self.current_message_id = self._build_local_id("msg")
            self.current_message_subagent_id = None
            started_new_message = True
        self._last_block_type = "message"

        self.current_message_text += text

        resolved_subagent_id = self._resolve_message_subagent_id(event)
        if resolved_subagent_id:
            self.current_message_subagent_id = resolved_subagent_id
            subagent_id = resolved_subagent_id
        else:
            subagent_id = self.current_message_subagent_id
        if started_new_message:
            await self._trace_subagent_provenance(
                event,
                scope="message_provenance",
                decision="message_bound_from_parent_tool_call" if subagent_id else "message_top_level",
                resolved_subagent_id=subagent_id,
            )

        entry_id = self._ensure_message_entry_id()
        await self._emit(build_assistant_delta_event(
            entry_id=entry_id,
            delta=text,
            conversation_id=self.conversation_id,
            turn_id=self.current_turn_id,
            subagent_id=subagent_id,
        ))

    async def _handle_reasoning_delta(self, data: SessionEventData) -> None:
        text = data.delta_content or ""
        if not text:
            return

        self.current_thought_text += text

        reasoning_id = self._extract_reasoning_id(data)
        if reasoning_id:
            self.current_reasoning_id = reasoning_id
        elif self._last_block_type != "reasoning":
            self.current_reasoning_id = self._build_local_id("reasoning")
        self._last_block_type = "reasoning"

        reasoning_id = self._ensure_reasoning_entry_id()
        await self._emit(build_reasoning_delta_event(
            entry_id=reasoning_id,
            delta=text,
            conversation_id=self.conversation_id,
            turn_id=self.current_turn_id,
        ))

    # ── Message/reasoning complete ──────────────────────────────────

    async def _handle_message_complete(self, event: SessionEvent) -> None:
        """Authoritative complete message (replaces accumulated deltas)."""
        data = event.data
        provider_message_id = self._extract_message_id(data)
        if provider_message_id:
            self.current_message_id = provider_message_id
        content = data.content or self.current_message_text
        if not content:
            return

        resolved_subagent_id = self._resolve_message_subagent_id(event)
        if resolved_subagent_id:
            self.current_message_subagent_id = resolved_subagent_id
            subagent_id = resolved_subagent_id
        else:
            subagent_id = self.current_message_subagent_id

        # For streamed top-level replies, replay should mirror live ordering:
        # if reasoning deltas already appeared, persist that reasoning first,
        # then finalize the assistant message before any later tool card.
        if self.current_thought_text and not subagent_id:
            reasoning_text = self._extract_reasoning_text(data) or self.current_thought_text
            await self._record_reasoning(reasoning_text, data=data)

        # A top-level streamed assistant reply with real content should finalize
        # here, not at turn_end. Tool-request envelopes can also arrive as
        # assistant.message with empty content; those are ignored above.
        if self.current_message_text and not subagent_id:
            self.current_message_text = content
            self._last_block_type = "message"
            await self._finalize_message(content, subagent_id=subagent_id)
            return

        trace_needed = False
        # Always bump block counter for each complete message.
        # SDK sends ASSISTANT_MESSAGE (complete) without preceding deltas for
        # subagent messages. Each complete message must get a unique ID.
        if not self.current_message_text or content != self.current_message_text:
            self._last_block_type = "message"
            if not provider_message_id:
                self.current_message_id = self._build_local_id("msg")
            self.current_message_subagent_id = None
            trace_needed = True

        if trace_needed:
            await self._trace_subagent_provenance(
                event,
                scope="message_provenance",
                decision="message_complete_bound_from_parent_tool_call" if subagent_id else "message_complete_top_level",
                resolved_subagent_id=subagent_id,
            )

        await self._finalize_message(content, subagent_id=subagent_id)

    async def _handle_reasoning_complete(self, data: SessionEventData) -> None:
        text = self._extract_reasoning_text(data) or self.current_thought_text
        await self._record_reasoning(text, data=data)

    # ── Turn lifecycle ──────────────────────────────────────────────

    async def _handle_turn_start(self, data: SessionEventData) -> None:
        """SDK-initiated turn start (assistant begins processing)."""
        # Reset block tracking so new turn's messages get fresh IDs
        self._last_block_type = None
        self.current_message_text = ""
        self.current_message_subagent_id = None
        self.current_thought_text = ""
        self._recorded_reasoning_ids.clear()

        await self._emit_mode(
            data.agent_mode,
            source="assistant.turn_start",
        )

        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "thinking",
            "active": True,
            "turn_id": self.current_turn_id,
        })

    async def _handle_turn_end(self, data: SessionEventData) -> None:
        """Assistant turn completed."""
        # Flush any pending reasoning
        if self.current_thought_text:
            await self._record_reasoning(self.current_thought_text)

        # Flush any pending message
        if self.current_message_text:
            subagent_id = self.current_message_subagent_id
            await self._finalize_message(self.current_message_text, subagent_id=subagent_id)

        await self._emit({
            "type": "turn_completed",
            "conversation_id": self.conversation_id,
            "stop_reason": "end_turn",
            "status": "success",
            "turn_id": self.current_turn_id,
        })

        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "idle",
            "active": False,
            "turn_id": self.current_turn_id,
        })

        await self._record({
            "role": "status",
            "status": "success",
            "stop_reason": "end_turn",
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })

    # ── Tool execution ──────────────────────────────────────────────

    # Tools that are UI-only (routed to ribbon/status, not rendered as shell cards)
    _UI_TOOLS = {"report_intent"}
    _HIDDEN_UI_TOOLS = {"ask_user"}

    # Tools that are "read-only explorers" — sanitized card + ribbon
    _EXPLORE_TOOLS = {"view", "glob", "grep", "rg"}

    @staticmethod
    def _coerce_tool_arguments(raw_args: object) -> PayloadDict:
        direct_args = _object_mapping(raw_args)
        if direct_args is not None:
            return direct_args
        if isinstance(raw_args, str):
            text = raw_args.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    parsed = cast(object, json.loads(text))
                except Exception:
                    parsed = None
                parsed_args = _object_mapping(parsed)
                if parsed_args is not None:
                    return parsed_args
        return {}

    @staticmethod
    def _split_known_flattened_mcp_name(tool_name: str) -> tuple[Optional[str], Optional[str]]:
        for prefix in _KNOWN_MCP_PREFIXES:
            needle = f"{prefix}-"
            if tool_name.startswith(needle):
                return prefix, tool_name[len(needle):]
        return None, None

    def _build_tool_render_state(self, data: SessionEventData, tool_name: str, raw_args: object) -> ToolRenderState:
        args = self._coerce_tool_arguments(raw_args)
        file_path = _mapping_str(args, "path", "file_path")

        mcp_server_name = data.mcp_server_name or None
        mcp_tool_name = data.mcp_tool_name or None
        if not mcp_server_name and not mcp_tool_name:
            mcp_server_name, mcp_tool_name = self._split_known_flattened_mcp_name(tool_name)

        if mcp_server_name or mcp_tool_name:
            display_tool = mcp_tool_name or tool_name or "tool"
            display_server = mcp_server_name or ""
            display_args = dict(args) if isinstance(args, dict) else {}

            if display_server == "agent-pty-blocks" and display_tool in {"agent_log_post", "agent_log_post_await"}:
                message_value = display_args.get("message")
                if isinstance(message_value, str) and "\n" not in message_value:
                    message_value = f"{message_value}\n"
                shaped_args: PayloadDict = {}
                who_value = display_args.get("who")
                if who_value is not None:
                    shaped_args["Who"] = who_value
                if message_value is not None:
                    shaped_args["Message"] = message_value
                return {
                    "kind": "mcp",
                    "tool_name": tool_name,
                    "title": "Agent-log write",
                    "activity": "writing agent log",
                    "server": "",
                    "tool": "Agent-log write",
                    "arguments": shaped_args,
                    "request": _tool_card_request_payload("", "Agent-log write", shaped_args),
                    "path": file_path,
                }

            return {
                "kind": "mcp",
                "tool_name": tool_name,
                "title": display_tool,
                "activity": f"calling {display_tool}",
                "server": display_server,
                "tool": display_tool,
                "arguments": display_args,
                "request": _tool_card_request_payload(display_server, display_tool, display_args),
                "path": file_path,
            }

        if tool_name == "view":
            card_label, ribbon_label, file_path = self._sanitize_tool_label(tool_name, raw_args)
            return {
                "kind": "view",
                "tool_name": tool_name,
                "title": card_label,
                "activity": ribbon_label,
                "path": file_path,
                "view_range": args.get("view_range"),
                "arguments": args,
            }

        if tool_name in {"glob", "grep", "rg"}:
            return {
                "kind": "search",
                "tool_name": tool_name,
                "title": "search",
                "activity": "Exploring",
                "tool": tool_name,
                "arguments": args,
                "path": file_path,
            }

        if tool_name == "bash":
            card_label, ribbon_label, file_path = self._sanitize_tool_label(tool_name, raw_args)
            return {
                "kind": "shell",
                "tool_name": tool_name,
                "title": card_label,
                "activity": ribbon_label,
                "path": file_path,
                "arguments": args,
            }

        if tool_name == "edit":
            display_args = {}
            if file_path:
                display_args["path"] = file_path
            return {
                "kind": "tool",
                "tool_name": tool_name,
                "title": "apply_patch",
                "activity": "Patching",
                "server": "",
                "tool": "apply_patch",
                "arguments": display_args,
                "path": file_path,
            }

        display_args: PayloadDict
        if tool_name in {"edit", "create", "write", "write_file"}:
            display_args = {}
            path_value = args.get("path")
            if path_value is not None:
                display_args["path"] = path_value
        elif tool_name == "apply_patch":
            display_args = {}
            if file_path:
                display_args["path"] = file_path
        elif args:
            display_args = args
        elif isinstance(raw_args, str) and raw_args.strip():
            display_args = {"input": raw_args}
        else:
            display_args = {}

        return {
            "kind": "tool",
            "tool_name": tool_name,
            "title": tool_name,
            "activity": f"running {tool_name}",
            "server": "",
            "tool": tool_name,
            "arguments": display_args,
            "request": _tool_card_request_payload("", tool_name, display_args),
            "path": file_path,
        }

    def _sanitize_tool_label(self, tool_name: str, raw_args: object) -> tuple[str, str, Optional[str]]:
        """
        Sanitize SDK tool call into (card_label, ribbon_label, path).
        Returns (command for card header, activity label for left ribbon, file path or None).
        """
        args = _object_mapping(raw_args) or {}

        if tool_name == "bash":
            cmd = _mapping_str(args, "command")
            desc = _mapping_str(args, "description")
            ribbon = desc if desc else "executing"
            return (cmd, ribbon, None)

        if tool_name == "view":
            path = _mapping_str(args, "path")
            vrange = args.get("view_range")
            # Build human-readable label
            short_path = path.rsplit("/", 1)[-1] if path else ""
            if vrange and isinstance(vrange, (list, tuple)) and len(vrange) >= 2:
                label = f"{short_path}  Lines {vrange[0]}–{vrange[1]}"
            elif vrange and isinstance(vrange, (list, tuple)) and len(vrange) == 1:
                label = f"{short_path}  Line {vrange[0]}+"
            elif short_path:
                label = short_path
            else:
                label = "Exploring"
            return (label, "Reading", path)

        if tool_name in ("glob", "grep"):
            pattern = _mapping_str(args, "pattern")
            label = f"{tool_name} {pattern}" if pattern else tool_name
            return (label, "Exploring", None)

        if tool_name in ("edit", "create"):
            path = _mapping_str(args, "path")
            short_path = path.rsplit("/", 1)[-1] if path else tool_name
            label = f"{tool_name} {short_path}"
            return (label, "Editing", path)

        if tool_name == "apply_patch":
            # Extract file path from patch content (*** Update File: /path)
            patch_text: object = raw_args if isinstance(raw_args, str) else args.get("patch", "")
            import re
            m = re.search(r'\*\*\* (?:Update|Add|Delete) File: (.+)', str(patch_text))
            path = m.group(1).strip() if m else ""
            short_path = path.rsplit("/", 1)[-1] if path else "patch"
            return (f"apply_patch {short_path}", "Patching", path)

        # Default: tool_name + args as before
        args_str = ""
        if args:
            parts = [f"{k}={v}" for k, v in args.items()]
            args_str = " " + " ".join(parts)
        elif isinstance(raw_args, str):
            args_str = " " + raw_args
        return (f"{tool_name}{args_str}", "executing", None)

    async def _handle_tool_start(self, event: SessionEvent) -> None:
        data = event.data
        # SDK uses data.tool_call_id as the stable tool call identifier
        tool_call_id = data.tool_call_id or str(event.id)
        tool_name = data.tool_name or data.mcp_tool_name or "tool"
        parent_id = data.parent_tool_call_id or (str(event.parent_id) if event.parent_id else None)

        # Check if this tool belongs to a subagent
        subagent_id = None
        if parent_id and parent_id in self._active_subagents:
            subagent_id = parent_id
            self._subagent_tool_ids.add(tool_call_id)
        elif tool_call_id in self._subagent_tool_ids:
            subagent_id = tool_call_id  # edge case
        await self._trace_subagent_provenance(
            event,
            scope="tool_provenance",
            decision=(
                "tool_bound_from_parent_tool_call"
                if parent_id and subagent_id == parent_id
                else "tool_bound_from_tool_call_id"
                if subagent_id
                else "tool_top_level"
            ),
            resolved_subagent_id=subagent_id,
        )
        
        if self._active_subagents and _is_debug():
            print(f"[SUBAGENT-DEBUG] tool_start: tool_call_id={tool_call_id} parent_id={parent_id} "
                  f"active_subagents={list(self._active_subagents.keys())} matched={subagent_id}")

        # Intercept UI-only tools — route to ribbon, suppress shell card
        if tool_name in self._UI_TOOLS:
            raw_args = cast(object, data.arguments)
            intent = ""
            arg_map = _object_mapping(raw_args)
            if arg_map is not None:
                intent = _mapping_str(arg_map, "intent")
            if intent:
                await self._emit({
                    "type": "thought",
                    "conversation_id": self.conversation_id,
                    "text": intent,
                    "turn_id": self.current_turn_id,
                })
            self._suppressed_tools.add(tool_call_id)
            return

        if tool_name in self._HIDDEN_UI_TOOLS and not self.debug_trace:
            self._suppressed_tools.add(tool_call_id)
            return

        raw_args = cast(object, data.arguments)
        render_state = self._build_tool_render_state(data, tool_name, raw_args)
        render_title = render_state.get("title", tool_name)
        render_tool_name = render_state.get("tool_name", tool_name)
        render_kind = render_state.get("kind", "tool")
        render_tool = render_state.get("tool", tool_name)
        render_server = render_state.get("server", "")
        render_arguments = render_state.get("arguments", {})
        render_request = render_state.get("request", render_arguments)
        render_path = render_state.get("path") or ""
        render_view_range = render_state.get("view_range")

        self.tool_calls[tool_call_id] = {
            "id": tool_call_id,
            "title": render_title,
            "tool_name": render_tool_name,
            "arguments": raw_args,
            "turn_id": self.current_turn_id,
            "output": "",
            "subagent_id": subagent_id,
            "render_kind": render_kind,
            "render_tool": render_tool,
            "render_server": render_server,
            "render_arguments": render_arguments,
            "render_request": render_request,
            "path": render_path,
            "view_range": render_view_range,
        }

        # Mark block type change so next message/reasoning delta gets a new ID
        self._last_block_type = "tool"

        if render_kind in {"view", "search"}:
            await self._emit({
                "type": "activity",
                "conversation_id": self.conversation_id,
                "label": render_state.get("activity", "reading"),
                "active": True,
                "turn_id": self.current_turn_id,
                **({"subagent_id": subagent_id} if subagent_id else {}),
            })
            return

        if render_kind == "shell":
            shell_begin_evt = {
                "type": "shell_begin",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "command": render_title,
                "activity": render_state.get("activity", "executing"),
                "cwd": "",
            }
            if render_path:
                shell_begin_evt["path"] = render_path
            if subagent_id:
                shell_begin_evt["subagent_id"] = subagent_id
            await self._emit(shell_begin_evt)
            return

        tool_begin_evt = {
            "type": "tool_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "tool": render_tool,
            "arguments": render_arguments,
            "request": render_request,
        }
        if render_server:
            tool_begin_evt["server"] = render_server
        if render_path:
            tool_begin_evt["path"] = render_path
        if subagent_id:
            tool_begin_evt["subagent_id"] = subagent_id
        await self._emit(tool_begin_evt)

    async def _handle_tool_complete(self, event: SessionEvent) -> None:
        data = event.data
        tool_call_id = data.tool_call_id or str(event.parent_id or event.id)

        # Suppressed UI-only tools — skip shell_end and transcript
        if tool_call_id in self._suppressed_tools:
            self._suppressed_tools.discard(tool_call_id)
            return

        result_obj = data.result
        content = ""
        detailed = ""
        if result_obj:
            content = result_obj.content or ""
            detailed = result_obj.detailed_content or ""
        else:
            output = cast(object, data.output)
            content = data.content or (output if isinstance(output, str) else "") or ""
        tool_call = self.tool_calls.get(tool_call_id)
        tool_name = ((tool_call.get("tool_name") if tool_call else "") or "").lower()
        render_kind = (tool_call.get("render_kind") if tool_call else "") or "tool"
        render_tool = (tool_call.get("render_tool") if tool_call else "") or tool_name or "tool"
        render_server = (tool_call.get("render_server") if tool_call else "") or ""
        render_arguments = tool_call.get("render_arguments") if tool_call else {}
        file_path = data.path or ""
        if not file_path:
            args = _object_mapping(tool_call.get("arguments")) if tool_call else None
            if args is not None:
                file_path = _mapping_str(args, "path", "file_path")
        if not file_path:
            file_path = (tool_call.get("path") if tool_call else "") or ""
        # apply_patch: extract path from "Modified/Added N file(s): /path" content
        if not file_path and content:
            import re
            m = re.search(r'(?:Modified|Added|Deleted) \d+ file\(s\): (.+)', content)
            if m:
                file_path = m.group(1).strip()

        subagent_id = tool_call.get("subagent_id") if tool_call else None

        # Determine success/failure
        has_error = bool(getattr(data, "error", None) or getattr(data, "error_reason", None))
        exit_code = 1 if has_error else 0

        if render_kind == "view":
            view_content = content or ((tool_call.get("output") if tool_call else "") or "")
            view_lines = _parse_copilot_view_lines(view_content)
            view_title = (tool_call.get("title") if tool_call else "") or "view"
            view_range = tool_call.get("view_range") if tool_call else None
            view_evt = {
                "type": "view",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "title": view_title,
                "path": file_path,
                "content": view_content,
            }
            if view_lines is not None:
                view_evt["lines"] = view_lines
            if view_range is not None:
                view_evt["view_range"] = view_range
            if subagent_id:
                view_evt["subagent_id"] = subagent_id
            await self._emit(view_evt)

            record_entry = {
                "role": "view",
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "title": view_title,
                "path": file_path,
                "content": view_content,
                "timestamp": utc_ts(),
                "subagent_id": subagent_id,
            }
            if view_lines is not None:
                record_entry["lines"] = view_lines
            if view_range is not None:
                record_entry["view_range"] = view_range
            await self._record(record_entry)
        elif render_kind == "search":
            search_content = content or ((tool_call.get("output") if tool_call else "") or "")
            search_title = (tool_call.get("title") if tool_call else "") or render_tool or "search"
            search_evt = {
                "type": "search",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "title": search_title,
                "mode": render_tool or tool_name or "search",
                "path": file_path,
                "pattern": render_arguments.get("pattern"),
                "arguments": render_arguments,
                "content": search_content,
            }
            if subagent_id:
                search_evt["subagent_id"] = subagent_id
            await self._emit(search_evt)

            record_entry = {
                "role": "search",
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "title": search_title,
                "mode": render_tool or tool_name or "search",
                "path": file_path,
                "pattern": render_arguments.get("pattern"),
                "arguments": render_arguments,
                "content": search_content,
                "timestamp": utc_ts(),
                "subagent_id": subagent_id,
            }
            await self._record(record_entry)
        elif render_kind == "shell":
            # For edit/create/apply_patch tools: suppress verbose output, add status emoji
            cmd_label = (tool_call.get("title") if tool_call else "") or ""
            stdout = content
            if tool_name in ("edit", "create", "apply_patch"):
                status_emoji = "🔴" if has_error else "🟢"
                # For apply_patch, extract just the file path from content
                if tool_name == "apply_patch" and not has_error:
                    import re
                    m = re.search(r'(?:Modified|Added|Deleted) \d+ file\(s\): (.+)', content)
                    short = m.group(1).split('/')[-1] if m else content[:60]
                    cmd_label = f"apply_patch {short} {status_emoji}"
                else:
                    cmd_label = f"{cmd_label} {status_emoji}"
                # Only show error output, not the verbose echo
                stdout = content if has_error else ""

            shell_end_evt = {
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "exitCode": exit_code,
                "stdout": stdout,
                "stderr": "",
                "command": cmd_label,
            }
            if file_path:
                shell_end_evt["path"] = file_path
            if subagent_id:
                shell_end_evt["subagent_id"] = subagent_id
            await self._emit(shell_end_evt)

            record_entry = {
                "role": "command",
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "command": cmd_label,
                "output": stdout,
                "status": "completed" if not has_error else "error",
                "timestamp": utc_ts(),
                "subagent_id": subagent_id,
            }
            if file_path:
                record_entry["path"] = file_path
            await self._record(record_entry)
        else:
            result_payload: object | None = None
            if content:
                result_payload = content
            elif data.error_reason:
                result_payload = data.error_reason
            elif data.error:
                result_payload = data.error
            result_payload = _json_safe_value(result_payload)
            request_payload = (tool_call.get("render_request") if tool_call else None) or render_arguments
            response_payload = _json_safe_value(
                _tool_card_response_payload(render_server, render_tool, result_payload)
            )

            tool_end_evt = {
                "type": "tool_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "tool": render_tool,
                "arguments": render_arguments,
                "request": request_payload,
                "result": result_payload,
                "response": response_payload,
                "is_error": has_error,
            }
            if render_server:
                tool_end_evt["server"] = render_server
            if file_path:
                tool_end_evt["path"] = file_path
            if subagent_id:
                tool_end_evt["subagent_id"] = subagent_id
            await self._emit(tool_end_evt)

            transcript_role = "mcp_tool" if render_kind == "mcp" else "tool"
            record_entry = {
                "role": transcript_role,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "tool": render_tool,
                "arguments": render_arguments,
                "request": request_payload,
                "result": result_payload,
                "response": response_payload,
                "is_error": has_error,
                "timestamp": utc_ts(),
                "subagent_id": subagent_id,
            }
            if render_server:
                record_entry["server"] = render_server
            if file_path:
                record_entry["path"] = file_path
            await self._record(record_entry)

        # Emit diff for file-mutating tools only (not view/grep/read)
        diff_text = detailed
        if not diff_text and tool_name in _FILE_CHANGE_TOOLS:
            # apply_patch and similar tools may put diff in content or arguments
            if content and (content.lstrip().startswith(("---", "@@", "diff ", "+++"))):
                diff_text = content
            if not diff_text:
                raw_args = tool_call.get("arguments") if tool_call else {}
                if isinstance(raw_args, str):
                    # Arguments may arrive as a JSON string
                    try:
                        import json
                        raw_args = cast(object, json.loads(raw_args))
                    except Exception:
                        raw_args = {}
                raw_args_map = _object_mapping(raw_args)
                if raw_args_map is not None:
                    diff_text = (
                        _mapping_str(raw_args_map, "patch")
                        or _mapping_str(raw_args_map, "diff")
                        or _mapping_str(raw_args_map, "content")
                    )
        if diff_text and tool_name in _FILE_CHANGE_TOOLS:
            diff_evt = {
                "type": "diff",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "path": file_path,
                "text": diff_text,
            }
            if subagent_id:
                diff_evt["subagent_id"] = subagent_id
            await self._emit(diff_evt)
            await self._record({
                "role": "diff",
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "path": file_path,
                "text": diff_text,
                "timestamp": utc_ts(),
                "subagent_id": subagent_id,
            })

    async def _handle_tool_progress(self, event: SessionEvent) -> None:
        data = event.data
        tool_call_id = data.tool_call_id or str(event.parent_id or event.id)

        # Suppressed UI-only tools — skip shell_delta
        if tool_call_id in self._suppressed_tools:
            return

        # Progress can come via partial_output, progress_message, or content
        content = data.partial_output or data.progress_message or data.content or ""
        # For PARTIAL_RESULT events, check result object too
        if not content and data.result:
            content = getattr(data.result, "content", "") or ""

        if content:
            tool_call = self.tool_calls.get(tool_call_id)
            if tool_call:
                tool_call["output"] += content

            render_kind = (tool_call.get("render_kind") if tool_call else "") or "tool"
            if render_kind in {"view", "search"}:
                return

            delta_evt = {
                "type": "shell_delta" if render_kind == "shell" else "tool_delta",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "delta": content,
            }
            if render_kind != "shell":
                delta_evt["tool"] = (
                    (tool_call.get("render_tool") if tool_call else "")
                    or (tool_call.get("tool_name") if tool_call else "")
                    or "tool"
                )
                render_server = (tool_call.get("render_server") if tool_call else "") or ""
                if render_server:
                    delta_evt["server"] = render_server
            subagent_id = tool_call.get("subagent_id") if tool_call else None
            if subagent_id:
                delta_evt["subagent_id"] = subagent_id
            await self._emit(delta_evt)

    # ── Intent / usage / error ──────────────────────────────────────

    async def _handle_intent(self, data: SessionEventData) -> None:
        intent = data.intent or ""
        if intent:
            await self._emit({
                "type": "thought",
                "conversation_id": self.conversation_id,
                "text": intent,
                "turn_id": self.current_turn_id,
            })

    async def _handle_usage(self, data: SessionEventData, *, source: str = "assistant.usage") -> None:
        input_tokens = self._coerce_int(data.input_tokens)
        output_tokens = self._coerce_int(data.output_tokens)
        cache_read = self._coerce_int(data.cache_read_tokens)
        context_window = self._coerce_int(data.token_limit)
        current_tokens = self._coerce_int(data.current_tokens)

        if current_tokens is not None:
            total = current_tokens
        elif input_tokens is not None or output_tokens is not None:
            total = (input_tokens or 0) + (output_tokens or 0)
        else:
            return

        await self._emit_token_count(
            total=total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cache_read,
            context_window=context_window,
            source=source,
        )

    async def _handle_error(self, data: SessionEventData) -> None:
        msg = data.message or "Unknown error"
        error_type = data.error_type or ""
        status_code = self._coerce_int(data.status_code)
        provider_call_id = data.provider_call_id or ""
        stack = data.stack or ""
        source = "session.error"

        error_event: PayloadDict = {
            "type": "error",
            "conversation_id": self.conversation_id,
            "message": msg,
            "turn_id": self.current_turn_id,
            "source": source,
        }
        error_entry: PayloadDict = {
            "role": "error",
            "message": msg,
            "turn_id": self.current_turn_id,
            "timestamp": utc_ts(),
            "source": source,
            "event": source,
        }
        if error_type:
            error_event["error_type"] = error_type
            error_entry["error_type"] = error_type
        if status_code is not None:
            error_event["status_code"] = status_code
            error_entry["status_code"] = status_code
        if provider_call_id:
            error_event["provider_call_id"] = provider_call_id
            error_entry["provider_call_id"] = provider_call_id
        if stack:
            error_event["stack"] = stack
            error_entry["stack"] = stack

        await self._emit(error_event)
        await self._record(error_entry)

        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "error",
            "active": False,
            "turn_id": self.current_turn_id,
        })

    async def _handle_plan_changed(self, data: SessionEventData) -> None:
        plan_content = data.plan_content
        content = plan_content if isinstance(plan_content, str) else ""
        plan_doc_update = {
            "plan_exists": bool(content.strip()),
            "plan_content": content,
            "plan_path": data.path if isinstance(data.path, str) else None,
            "plan_source": "sdk",
        }
        if self.plan_state_provider is not None:
            state = await self.plan_state_provider(self.conversation_id, plan_doc_update)
        else:
            state = {
                "has_plan": True,
                "has_todo": True,
                "plan_exists": bool(content.strip()),
                "plan_content": content,
                "plan_steps": [],
                "plan_path": plan_doc_update["plan_path"],
                "plan_source": "sdk",
            }
        await self._emit({
            "type": "plan_state",
            "conversation_id": self.conversation_id,
            **state,
            "plan_operation": self._normalize_mode_kind(data.operation),
            "recommended_action": data.recommended_action,
            "turn_id": self.current_turn_id,
        })

    async def _handle_mode_changed(self, data: SessionEventData) -> None:
        await self._emit_mode(
            data.new_mode or data.mode,
            source="session.mode_changed",
            previous_raw=data.previous_mode,
        )

    # ── Called externally by client ─────────────────────────────────

    async def on_turn_start(self, text: str, *, turn_token: Optional[str] = None) -> None:
        """Called when a new turn starts (user sends message)."""
        local_turn_token = self._normalize_id(turn_token) or uuid4().hex
        self.current_turn_id = self._build_local_id("turn", local_turn_token)
        self.current_message_id = None
        self.current_reasoning_id = None
        self.current_message_text = ""
        self.current_message_subagent_id = None
        self.current_thought_text = ""
        self.tool_calls = {}
        self._last_block_type = None
        self._recorded_reasoning_ids.clear()

        user_msg_id = self._build_local_id("user", local_turn_token)

        await self._emit(build_message_event(
            role="user",
            entry_id=user_msg_id,
            text=text,
            conversation_id=self.conversation_id,
            turn_id=self.current_turn_id,
        ))

        await self._emit({
            "type": "turn_started",
            "conversation_id": self.conversation_id,
            "turn_id": self.current_turn_id,
        })

        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "thinking",
            "active": True,
            "turn_id": self.current_turn_id,
        })

        await self._record(build_message_transcript_entry(
            role="user",
            entry_id=user_msg_id,
            text=text,
            timestamp=utc_ts(),
            turn_id=self.current_turn_id,
        ))
