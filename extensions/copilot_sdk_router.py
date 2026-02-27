"""
Copilot SDK Event Router

Translates Copilot SDK SessionEvent objects to our internal event format.
This allows Copilot CLI (and all its models) to work with our existing
frontend, transcript, and replay infrastructure.

The router speaks Copilot SDK on one side (SessionEvent from copilot package)
and our internal format on the other (to _broadcast_appserver_ui).
"""

from typing import Any, Dict, Optional, Callable, Awaitable
from datetime import datetime, timezone

from copilot import SessionEvent
from copilot.generated.session_events import SessionEventType


def utc_ts() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


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
        broadcast_fn: Callable[[Dict[str, Any]], Awaitable[None]],
        transcript_fn: Callable[[str, Dict[str, Any]], Awaitable[None]],
    ):
        self.conversation_id = conversation_id
        self.broadcast = broadcast_fn
        self.append_transcript = transcript_fn

        # State tracking
        self.current_turn_id: Optional[str] = None
        self.current_message_id: Optional[str] = None
        self.current_reasoning_id: Optional[str] = None
        self.current_message_text: str = ""
        self.current_thought_text: str = ""
        self.tool_calls: Dict[str, Dict[str, Any]] = {}
        self._turn_counter: int = 0
        self._seq: int = 0

        # Block tracking for interleaved reasoning/message
        self._block_counter: int = 0
        self._last_block_type: Optional[str] = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _emit(self, event: Dict[str, Any]) -> None:
        event["seq"] = self._next_seq()
        await self.broadcast(event)

    async def _record(self, entry: Dict[str, Any]) -> None:
        entry["seq"] = self._seq
        await self.append_transcript(self.conversation_id, entry)

    async def route_event(self, event: SessionEvent) -> None:
        """Route a Copilot SDK SessionEvent to the appropriate handler."""
        etype = event.type
        data = event.data

        if etype == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            await self._handle_message_delta(data)
        elif etype == SessionEventType.ASSISTANT_REASONING_DELTA:
            await self._handle_reasoning_delta(data)
        elif etype == SessionEventType.ASSISTANT_MESSAGE:
            await self._handle_message_complete(data)
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
        elif etype == SessionEventType.ASSISTANT_INTENT:
            await self._handle_intent(data)
        elif etype == SessionEventType.ASSISTANT_USAGE:
            await self._handle_usage(data)
        elif etype == SessionEventType.SESSION_ERROR:
            await self._handle_error(data)
        elif etype == SessionEventType.SESSION_START:
            pass  # Handled by client
        elif etype == SessionEventType.SESSION_RESUME:
            pass  # Handled by client
        elif etype == SessionEventType.SESSION_IDLE:
            await self._emit({
                "type": "activity",
                "conversation_id": self.conversation_id,
                "label": "idle",
                "active": False,
                "turn_id": self.current_turn_id,
            })
        # Subagent events
        elif etype == SessionEventType.SUBAGENT_STARTED:
            await self._emit({
                "type": "activity",
                "conversation_id": self.conversation_id,
                "label": f"subagent: {getattr(data, 'intent', 'working')}",
                "active": True,
                "turn_id": self.current_turn_id,
            })
        elif etype in (SessionEventType.SUBAGENT_COMPLETED, SessionEventType.SUBAGENT_FAILED):
            pass  # Turn end will handle final state

    # ── Message deltas ──────────────────────────────────────────────

    async def _handle_message_delta(self, data: Any) -> None:
        text = getattr(data, "delta_content", None) or ""
        if not text:
            return

        self.current_message_text += text

        if self._last_block_type != "message":
            self._block_counter += 1
            self._last_block_type = "message"
            self.current_message_id = f"msg_{self._turn_counter}_{self._block_counter}"

        await self._emit({
            "type": "assistant_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_message_id,
            "delta": text,
            "turn_id": self.current_turn_id,
        })

    async def _handle_reasoning_delta(self, data: Any) -> None:
        text = getattr(data, "delta_content", None) or ""
        if not text:
            return

        self.current_thought_text += text

        if self._last_block_type != "reasoning":
            self._block_counter += 1
            self._last_block_type = "reasoning"
            self.current_reasoning_id = f"reasoning_{self._turn_counter}_{self._block_counter}"

        await self._emit({
            "type": "reasoning_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_reasoning_id,
            "delta": text,
            "turn_id": self.current_turn_id,
        })

    # ── Message/reasoning complete ──────────────────────────────────

    async def _handle_message_complete(self, data: Any) -> None:
        """Authoritative complete message (replaces accumulated deltas)."""
        content = getattr(data, "content", None) or self.current_message_text
        if not content:
            return

        await self._emit({
            "type": "assistant_finalize",
            "conversation_id": self.conversation_id,
            "id": self.current_message_id,
            "text": content,
            "turn_id": self.current_turn_id,
        })

        await self._record({
            "role": "assistant",
            "id": self.current_message_id,
            "text": content,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })

        self.current_message_text = ""

    async def _handle_reasoning_complete(self, data: Any) -> None:
        text = getattr(data, "reasoning_text", None) or self.current_thought_text
        if not text:
            return

        await self._record({
            "role": "reasoning",
            "id": self.current_reasoning_id,
            "text": text,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })

        self.current_thought_text = ""

    # ── Turn lifecycle ──────────────────────────────────────────────

    async def _handle_turn_start(self, data: Any) -> None:
        """SDK-initiated turn start (assistant begins processing)."""
        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "thinking",
            "active": True,
            "turn_id": self.current_turn_id,
        })

    async def _handle_turn_end(self, data: Any) -> None:
        """Assistant turn completed."""
        # Flush any pending reasoning
        if self.current_thought_text:
            await self._record({
                "role": "reasoning",
                "id": self.current_reasoning_id,
                "text": self.current_thought_text,
                "timestamp": utc_ts(),
                "turn_id": self.current_turn_id,
            })
            self.current_thought_text = ""

        # Flush any pending message
        if self.current_message_text:
            await self._emit({
                "type": "assistant_finalize",
                "conversation_id": self.conversation_id,
                "id": self.current_message_id,
                "text": self.current_message_text,
                "turn_id": self.current_turn_id,
            })
            await self._record({
                "role": "assistant",
                "id": self.current_message_id,
                "text": self.current_message_text,
                "timestamp": utc_ts(),
                "turn_id": self.current_turn_id,
            })
            self.current_message_text = ""

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

    async def _handle_tool_start(self, event: SessionEvent) -> None:
        data = event.data
        tool_call_id = str(event.id)
        # Try to extract a meaningful command label
        content = getattr(data, "content", None) or ""
        tool_name = getattr(data, "source", None) or "tool"

        self.tool_calls[tool_call_id] = {
            "id": tool_call_id,
            "title": content or tool_name,
            "turn_id": self.current_turn_id,
            "output": "",
        }

        # Mark block type change so next message/reasoning delta gets a new ID
        self._last_block_type = "tool"

        await self._emit({
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": content or tool_name,
            "cwd": "",
        })

    async def _handle_tool_complete(self, event: SessionEvent) -> None:
        data = event.data
        tool_call_id = str(event.parent_id or event.id)
        content = getattr(data, "content", None) or ""
        tool_call = self.tool_calls.get(tool_call_id, {})

        await self._emit({
            "type": "shell_end",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "exitCode": 0,
            "stdout": content,
            "stderr": "",
            "command": tool_call.get("title", ""),
        })

        await self._record({
            "role": "command",
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": tool_call.get("title", ""),
            "output": content,
            "status": "completed",
            "timestamp": utc_ts(),
        })

    async def _handle_tool_progress(self, event: SessionEvent) -> None:
        data = event.data
        tool_call_id = str(event.parent_id or event.id)
        content = getattr(data, "content", None) or ""

        if content:
            tool_call = self.tool_calls.get(tool_call_id)
            if tool_call:
                tool_call["output"] += content

            await self._emit({
                "type": "shell_delta",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "delta": content,
            })

    # ── Intent / usage / error ──────────────────────────────────────

    async def _handle_intent(self, data: Any) -> None:
        intent = getattr(data, "intent", None) or ""
        if intent:
            await self._emit({
                "type": "activity",
                "conversation_id": self.conversation_id,
                "label": intent,
                "active": True,
                "turn_id": self.current_turn_id,
            })

    async def _handle_usage(self, data: Any) -> None:
        input_tokens = getattr(data, "input_tokens", None) or 0
        output_tokens = getattr(data, "output_tokens", None) or 0
        cache_read = getattr(data, "cache_read_tokens", None) or 0
        total = input_tokens + output_tokens

        await self._emit({
            "type": "token_usage",
            "conversation_id": self.conversation_id,
            "total": total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cache_read,
            "turn_id": self.current_turn_id,
        })

        await self._record({
            "role": "token_usage",
            "total": total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cache_read,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })

    async def _handle_error(self, data: Any) -> None:
        msg = getattr(data, "message", None) or "Unknown error"
        error_type = getattr(data, "error_type", None) or ""

        await self._emit({
            "type": "rpc_error",
            "conversation_id": self.conversation_id,
            "message": msg,
            "error_type": error_type,
            "turn_id": self.current_turn_id,
        })

        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": msg,
            "active": True,
            "turn_id": self.current_turn_id,
        })

    # ── Called externally by client ─────────────────────────────────

    async def on_turn_start(self, text: str) -> None:
        """Called when a new turn starts (user sends message)."""
        self._turn_counter += 1
        self.current_turn_id = f"turn_{self._turn_counter}"
        self.current_message_id = f"msg_{self._turn_counter}_0"
        self.current_reasoning_id = f"reasoning_{self._turn_counter}_0"
        self.current_message_text = ""
        self.current_thought_text = ""
        self.tool_calls = {}
        self._block_counter = 0
        self._last_block_type = None

        user_msg_id = f"user_{self._turn_counter}"

        await self._emit({
            "type": "message",
            "conversation_id": self.conversation_id,
            "id": user_msg_id,
            "role": "user",
            "text": text,
            "turn_id": self.current_turn_id,
        })

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

        await self._record({
            "role": "user",
            "id": user_msg_id,
            "text": text,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })
