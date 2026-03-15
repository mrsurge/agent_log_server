"""
Copilot SDK Event Router

Translates Copilot SDK SessionEvent objects to our internal event format.
This allows Copilot CLI (and all its models) to work with our existing
frontend, transcript, and replay infrastructure.

The router speaks Copilot SDK on one side (SessionEvent from the vendored copilot package)
and our internal format on the other (to _broadcast_appserver_ui).
"""

from typing import Any, Dict, Optional, Callable, Awaitable
from datetime import datetime, timezone

from ._vendor.copilot import SessionEvent
from ._vendor.copilot.generated.session_events import SessionEventType


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


# Tools that mutate files — only these get post-execution diff entries
_FILE_CHANGE_TOOLS = {
    "edit", "create", "write", "write_file", "apply_patch", "delete",
    "move", "rename", "insert", "replace", "patch",
}


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
        self.current_message_subagent_id: Optional[str] = None
        self.current_thought_text: str = ""
        self.tool_calls: Dict[str, Dict[str, Any]] = {}
        self._turn_counter: int = 0
        self._seq: int = 0

        # Block tracking for interleaved reasoning/message
        self._block_counter: int = 0
        self._last_block_type: Optional[str] = None
        self._suppressed_tools: set = set()  # tool_call_ids for UI-only tools (e.g. report_intent)
        self._active_subagents: Dict[str, Dict[str, Any]] = {}  # tool_call_id -> subagent info
        self._subagent_tool_ids: set = set()  # tool_call_ids that belong to a subagent

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _emit(self, event: Dict[str, Any]) -> None:
        # EVERY _emit() MUST HAVE A MATCHING _record() WITH THE SAME FIELDS.
        # THE TRANSCRIPT IS THE REPLAY SOURCE. IF IT'S NOT RECORDED, IT DOESN'T EXIST ON PLAYBACK.
        event["seq"] = self._next_seq()
        await self.broadcast(event)

    async def _record(self, entry: Dict[str, Any]) -> None:
        # EVERY _record() MUST MIRROR THE CORRESPONDING _emit() — SAME KEYS, SAME VALUES.
        # REPLAY MUST BE AN EXACT MIRROR OF THE LIVE FEED. NO EXCEPTIONS.
        entry["seq"] = self._seq
        await self.append_transcript(self.conversation_id, entry)

    @staticmethod
    def _normalize_id(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _resolve_message_subagent_id(self, event: SessionEvent) -> Optional[str]:
        data = event.data
        return (
            self._normalize_id(getattr(data, "parent_tool_call_id", None))
            or self._normalize_id(getattr(event, "parent_id", None))
        )

    def _resolve_subagent_event_id(
        self,
        event: SessionEvent,
        *,
        allow_single_active: bool = False,
    ) -> Optional[str]:
        data = event.data
        for candidate in (
            getattr(data, "tool_call_id", None),
            getattr(data, "parent_tool_call_id", None),
            getattr(event, "parent_id", None),
            getattr(event, "id", None),
        ):
            resolved = self._normalize_id(candidate)
            if resolved:
                return resolved
        if allow_single_active and len(self._active_subagents) == 1:
            return next(iter(self._active_subagents))
        return None

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
            sa_id = getattr(data, "tool_call_id", None) or str(event.id)
            intent = getattr(data, "intent", "") or ""
            name = getattr(data, "agent_display_name", None) or getattr(data, "agent_name", None) or "subagent"
            self._active_subagents[sa_id] = {"name": name, "intent": intent}
            if _is_debug():
                print(f"[SUBAGENT-DEBUG] SUBAGENT_STARTED: sa_id={sa_id} event.id={event.id} "
                      f"data.tool_call_id={getattr(data, 'tool_call_id', None)} event.parent_id={event.parent_id}")
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
            if sa_id and sa_id in self._active_subagents:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary="", success=True)
        elif etype == SessionEventType.SUBAGENT_COMPLETED:
            sa_id = self._resolve_subagent_event_id(event, allow_single_active=True)
            summary = getattr(data, "summary", "") or ""
            success = getattr(data, "success", True)
            if sa_id:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary=summary, success=success)
        elif etype == SessionEventType.SUBAGENT_FAILED:
            sa_id = self._resolve_subagent_event_id(event, allow_single_active=True)
            error = getattr(data, "error", "") or getattr(data, "error_reason", "") or ""
            fail_summary = f"Failed: {error}" if error else "Failed"
            if sa_id:
                self._active_subagents.pop(sa_id, None)
                await self._emit_subagent_end(sa_id, summary=fail_summary, success=False)

    # ── Message deltas ──────────────────────────────────────────────

    async def _handle_message_delta(self, event: SessionEvent) -> None:
        data = event.data
        text = getattr(data, "delta_content", None) or ""
        if not text:
            return

        if self._last_block_type != "message":
            self._block_counter += 1
            self._last_block_type = "message"
            self.current_message_id = f"msg_{self._turn_counter}_{self._block_counter}"
            self.current_message_subagent_id = None

        self.current_message_text += text

        subagent_id = self._resolve_message_subagent_id(event)
        if subagent_id:
            self.current_message_subagent_id = subagent_id
        else:
            subagent_id = self.current_message_subagent_id

        evt = {
            "type": "assistant_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_message_id,
            "delta": text,
            "turn_id": self.current_turn_id,
        }
        if subagent_id:
            evt["subagent_id"] = subagent_id
        await self._emit(evt)

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

    async def _handle_message_complete(self, event: SessionEvent) -> None:
        """Authoritative complete message (replaces accumulated deltas)."""
        data = event.data
        content = getattr(data, "content", None) or self.current_message_text
        if not content:
            return

        # Always bump block counter for each complete message.
        # SDK sends ASSISTANT_MESSAGE (complete) without preceding deltas for
        # subagent messages. Each complete message must get a unique ID.
        if not self.current_message_text or content != self.current_message_text:
            self._block_counter += 1
            self._last_block_type = "message"
            self.current_message_id = f"msg_{self._turn_counter}_{self._block_counter}"
            self.current_message_subagent_id = None

        subagent_id = self._resolve_message_subagent_id(event)
        if subagent_id:
            self.current_message_subagent_id = subagent_id
        else:
            subagent_id = self.current_message_subagent_id

        finalize_evt = {
            "type": "assistant_finalize",
            "conversation_id": self.conversation_id,
            "id": self.current_message_id,
            "text": content,
            "turn_id": self.current_turn_id,
        }
        if subagent_id:
            finalize_evt["subagent_id"] = subagent_id
        await self._emit(finalize_evt)

        record = {
            "role": "assistant",
            "id": self.current_message_id,
            "text": content,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        }
        if subagent_id:
            record["subagent_id"] = subagent_id
        await self._record(record)

        self.current_message_text = ""
        self.current_message_subagent_id = None

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
        # Reset block tracking so new turn's messages get fresh IDs
        self._last_block_type = None
        self.current_message_text = ""
        self.current_message_subagent_id = None
        self.current_thought_text = ""

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
            subagent_id = self.current_message_subagent_id
            flush_finalize = {
                "type": "assistant_finalize",
                "conversation_id": self.conversation_id,
                "id": self.current_message_id,
                "text": self.current_message_text,
                "turn_id": self.current_turn_id,
            }
            if subagent_id:
                flush_finalize["subagent_id"] = subagent_id
            await self._emit(flush_finalize)
            flush_record = {
                "role": "assistant",
                "id": self.current_message_id,
                "text": self.current_message_text,
                "timestamp": utc_ts(),
                "turn_id": self.current_turn_id,
            }
            if subagent_id:
                flush_record["subagent_id"] = subagent_id
            await self._record(flush_record)
            self.current_message_text = ""
            self.current_message_subagent_id = None

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

    # Tools that are "read-only explorers" — sanitized card + ribbon
    _EXPLORE_TOOLS = {"view", "glob", "grep"}

    def _sanitize_tool_label(self, tool_name: str, raw_args: Any) -> tuple:
        """
        Sanitize SDK tool call into (card_label, ribbon_label, path).
        Returns (command for card header, activity label for left ribbon, file path or None).
        """
        args = raw_args if isinstance(raw_args, dict) else {}

        if tool_name == "bash":
            cmd = args.get("command", "")
            desc = args.get("description", "")
            ribbon = desc if desc else "executing"
            return (cmd, ribbon, None)

        if tool_name == "view":
            path = args.get("path", "")
            vrange = args.get("view_range", None)
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
            pattern = args.get("pattern", "")
            label = f"{tool_name} {pattern}" if pattern else tool_name
            return (label, "Exploring", None)

        if tool_name in ("edit", "create"):
            path = args.get("path", "")
            short_path = path.rsplit("/", 1)[-1] if path else tool_name
            label = f"{tool_name} {short_path}"
            return (label, "Editing", path)

        if tool_name == "apply_patch":
            # Extract file path from patch content (*** Update File: /path)
            patch_text = raw_args if isinstance(raw_args, str) else args.get("patch", "")
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
        parent_id = getattr(data, "parent_tool_call_id", None) or (str(event.parent_id) if event.parent_id else None)

        # Check if this tool belongs to a subagent
        subagent_id = None
        if parent_id and parent_id in self._active_subagents:
            subagent_id = parent_id
            self._subagent_tool_ids.add(tool_call_id)
        elif tool_call_id in self._subagent_tool_ids:
            subagent_id = tool_call_id  # edge case
        
        if self._active_subagents and _is_debug():
            print(f"[SUBAGENT-DEBUG] tool_start: tool_call_id={tool_call_id} parent_id={parent_id} "
                  f"active_subagents={list(self._active_subagents.keys())} matched={subagent_id}")

        # Intercept UI-only tools — route to ribbon, suppress shell card
        if tool_name in self._UI_TOOLS:
            raw_args = data.arguments
            intent = ""
            if isinstance(raw_args, dict):
                intent = raw_args.get("intent", "")
            if intent:
                await self._emit({
                    "type": "thought",
                    "conversation_id": self.conversation_id,
                    "text": intent,
                    "turn_id": self.current_turn_id,
                })
            self._suppressed_tools.add(tool_call_id)
            return

        raw_args = data.arguments
        card_label, ribbon_label, file_path = self._sanitize_tool_label(tool_name, raw_args)

        self.tool_calls[tool_call_id] = {
            "id": tool_call_id,
            "title": card_label,
            "tool_name": tool_name,
            "arguments": raw_args,
            "turn_id": self.current_turn_id,
            "output": "",
            "subagent_id": subagent_id,
        }

        # Mark block type change so next message/reasoning delta gets a new ID
        self._last_block_type = "tool"

        shell_begin_evt = {
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": card_label,
            "activity": ribbon_label,
            "cwd": "",
        }
        if file_path:
            shell_begin_evt["path"] = file_path
        if subagent_id:
            shell_begin_evt["subagent_id"] = subagent_id
        await self._emit(shell_begin_evt)

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
            content = getattr(result_obj, "content", "") or ""
            detailed = getattr(result_obj, "detailed_content", "") or ""
        else:
            content = data.content or data.output or ""
        tool_call = self.tool_calls.get(tool_call_id, {})
        tool_name = (tool_call.get("tool_name") or "").lower()
        file_path = data.path or ""
        if not file_path:
            args = tool_call.get("arguments") or {}
            if isinstance(args, dict):
                file_path = args.get("path") or args.get("file_path") or ""
        # apply_patch: extract path from "Modified/Added N file(s): /path" content
        if not file_path and content:
            import re
            m = re.search(r'(?:Modified|Added|Deleted) \d+ file\(s\): (.+)', content)
            if m:
                file_path = m.group(1).strip()

        subagent_id = tool_call.get("subagent_id")

        # Determine success/failure
        has_error = bool(getattr(data, "error", None) or getattr(data, "error_reason", None))
        exit_code = 1 if has_error else 0

        # For edit/create/apply_patch tools: suppress verbose output, add status emoji
        cmd_label = tool_call.get("title", "")
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

        # Emit diff for file-mutating tools only (not view/grep/read)
        diff_text = detailed
        if not diff_text and tool_name in _FILE_CHANGE_TOOLS:
            # apply_patch and similar tools may put diff in content or arguments
            if content and (content.lstrip().startswith(("---", "@@", "diff ", "+++"))):
                diff_text = content
            if not diff_text:
                raw_args = tool_call.get("arguments") or {}
                if isinstance(raw_args, str):
                    # Arguments may arrive as a JSON string
                    try:
                        import json
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}
                if isinstance(raw_args, dict):
                    diff_text = raw_args.get("patch") or raw_args.get("diff") or raw_args.get("content") or ""
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

            delta_evt = {
                "type": "shell_delta",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "delta": content,
            }
            subagent_id = tool_call.get("subagent_id") if tool_call else None
            if subagent_id:
                delta_evt["subagent_id"] = subagent_id
            await self._emit(delta_evt)

    # ── Intent / usage / error ──────────────────────────────────────

    async def _handle_intent(self, data: Any) -> None:
        intent = getattr(data, "intent", None) or ""
        if intent:
            await self._emit({
                "type": "thought",
                "conversation_id": self.conversation_id,
                "text": intent,
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
        self.current_message_subagent_id = None
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
