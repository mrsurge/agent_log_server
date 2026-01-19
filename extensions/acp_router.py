"""
ACP Event Router

Translates ACP protocol events to our internal event format.
This allows Gemini (and other ACP agents) to work with our existing
frontend, transcript, and replay infrastructure.

The router speaks ACP on one side (from gemini --experimental-acp)
and our internal format on the other (to _broadcast_appserver_ui).
"""

import json
from typing import Any, Dict, List, Optional, Callable, Awaitable
from datetime import datetime, timezone


def utc_ts() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


class ACPEventRouter:
    """
    Translates ACP session/update events to our internal event format.
    
    ACP sends notifications like:
    {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "...",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Hello"}
            }
        }
    }
    
    We translate to our format:
    {
        "type": "codex_event",
        "event_type": "delta",
        "delta": "Hello",
        ...
    }
    """
    
    def __init__(
        self,
        conversation_id: str,
        broadcast_fn: Callable[[Dict[str, Any]], Awaitable[None]],
        transcript_fn: Callable[[str, Dict[str, Any]], Awaitable[None]],
        write_fn: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
    ):
        """
        Args:
            conversation_id: Our conversation ID
            broadcast_fn: Async function to broadcast to WebSocket (_broadcast_appserver_ui)
            transcript_fn: Async function to append to transcript (_append_transcript_entry)
            write_fn: Async function to send JSON-RPC responses back to agent
        """
        self.conversation_id = conversation_id
        self.broadcast = broadcast_fn
        self.append_transcript = transcript_fn
        self.write_response = write_fn
        
        # State tracking
        self.current_turn_id: Optional[str] = None
        self.current_message_id: Optional[str] = None  # Unique ID for assistant message block
        self.current_reasoning_id: Optional[str] = None  # Unique ID for reasoning block
        self.current_message_text: str = ""
        self.current_thought_text: str = ""
        self.tool_calls: Dict[str, Dict[str, Any]] = {}  # tool_call_id -> info
        self._turn_counter: int = 0  # Increments each turn for unique reasoning ids
        self._seq: int = 0  # Global sequence number for event ordering
        
        # Block tracking for interleaved reasoning/message
        self._block_counter: int = 0  # Increments each time we switch block type
        self._last_block_type: Optional[str] = None  # "reasoning" or "message"
    
    def _next_seq(self) -> int:
        """Get next sequence number for event ordering."""
        self._seq += 1
        return self._seq
    
    async def _emit(self, event: Dict[str, Any]) -> None:
        """Broadcast event with sequence number for ordering."""
        event["seq"] = self._next_seq()
        await self.broadcast(event)
    
    async def _record(self, entry: Dict[str, Any]) -> None:
        """Append to transcript with sequence number for ordering."""
        entry["seq"] = self._seq  # Use current seq (same as last broadcast)
        await self.append_transcript(self.conversation_id, entry)
    
    async def route_event(self, message: Dict[str, Any]) -> None:
        """
        Route an ACP JSON-RPC message to appropriate handler.
        
        Args:
            message: Parsed JSON-RPC message from ACP agent stdout
        """
        method = message.get("method", "")
        params = message.get("params", {})
        msg_id = message.get("id")
        
        # Check if this is a REQUEST (method + id) vs NOTIFICATION (method only)
        if method and msg_id is not None:
            # Incoming request from agent - needs a response
            if method == "session/request_permission":
                await self._handle_request_permission(msg_id, params)
            elif method == "fs/read_text_file":
                await self._handle_read_text_file(msg_id, params)
            elif method == "fs/write_text_file":
                await self._handle_write_text_file(msg_id, params)
            else:
                print(f"[ACP] Unhandled request method: {method}")
                # Send error response for unknown methods
                if self.write_response:
                    await self.write_response({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}
                    })
        elif method == "session/update":
            await self._handle_session_update(params)
        elif message.get("result") is not None:
            # Response to a request (e.g., session/prompt response)
            await self._handle_response(message)
        elif message.get("error") is not None:
            await self._handle_error(message)
    
    async def _handle_session_update(self, params: Dict[str, Any]) -> None:
        """Handle session/update notification."""
        session_id = params.get("sessionId", "")
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")
        
        if update_type == "agent_message_chunk":
            await self._handle_agent_message_chunk(update)
        elif update_type == "agent_thought_chunk":
            await self._handle_agent_thought_chunk(update)
        elif update_type == "tool_call":
            await self._handle_tool_call_start(update)
        elif update_type == "tool_call_update":
            await self._handle_tool_call_update(update)
        elif update_type == "plan":
            await self._handle_plan(update)
        elif update_type == "user_message_chunk":
            # Echo of user message - we already have it, skip
            pass
        elif update_type == "available_commands_update":
            # Slash commands - could expose later
            pass
        elif update_type == "current_mode_update":
            # Agent mode changes - could expose later
            pass
    
    async def _handle_request_permission(self, request_id: Any, params: Dict[str, Any]) -> None:
        """
        Handle session/request_permission request from agent.
        
        The agent is requesting permission to execute a tool call.
        We need to send a response with the optionId from the provided options.
        
        For now: auto-approve all requests using "proceed_once" or first allow option.
        Future: broadcast to frontend, wait for user decision.
        """
        session_id = params.get("sessionId", "")
        tool_call = params.get("toolCall", {})
        options = params.get("options", [])
        
        tool_call_id = tool_call.get("toolCallId", "")
        title = tool_call.get("title", "Tool Call")
        kind = tool_call.get("kind", "other")
        
        print(f"[ACP] Permission request: id={request_id} tool={title} kind={kind}")
        print(f"[ACP] Options: {options}")
        
        # Broadcast approval request to frontend (for UI indication)
        await self._emit({
            "type": "approval_request",
            "conversation_id": self.conversation_id,
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "title": title,
            "kind": kind,
            "options": options,
            "turn_id": self.current_turn_id,
        })
        
        # Find the "proceed_once" or first allow option to auto-approve
        option_id = "proceed_once"  # Default
        for opt in options:
            opt_id = opt.get("optionId", "")
            opt_kind = opt.get("kind", "")
            if opt_id == "proceed_once" or opt_kind == "allow_once":
                option_id = opt_id
                break
            elif opt_kind in ("allow_always", "allow_always_and_save"):
                option_id = opt_id  # Fallback to allow_always if no allow_once
        
        # Send response back to agent per ACP spec:
        # {"result": {"outcome": {"outcome": "selected", "optionId": "..."}}}
        if self.write_response:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": option_id
                    }
                }
            }
            await self.write_response(response)
            print(f"[ACP] Sent approval response: {option_id}")
        else:
            print(f"[ACP] WARNING: No write_response function - cannot respond to permission request!")

    async def _handle_read_text_file(self, request_id: Any, params: Dict[str, Any]) -> None:
        """
        Handle fs/read_text_file request from agent.
        
        The agent wants to read a file from the filesystem.
        """
        from pathlib import Path
        
        path = params.get("path", "")
        limit = params.get("limit")
        tool_call_id = f"read_{request_id}"
        
        print(f"[ACP] fs/read_text_file: {path}")
        
        # Emit shell_begin for the read operation
        await self._emit({
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": f"read: {path}",
            "cwd": "",
        })
        
        try:
            p = Path(path)
            if not p.exists():
                result = {"content": None, "error": f"File not found: {path}"}
                error_msg = f"File not found: {path}"
                # Emit shell_end with error
                await self._emit({
                    "type": "shell_end",
                    "conversation_id": self.conversation_id,
                    "id": tool_call_id,
                    "turn_id": self.current_turn_id,
                    "exitCode": 1,
                    "stdout": "",
                    "stderr": error_msg,
                    "command": f"read: {path}",
                })
            else:
                content = p.read_text(encoding="utf-8")
                if limit:
                    content = content[:limit]
                result = {"content": content}
                
                await self._emit({
                    "type": "shell_end",
                    "conversation_id": self.conversation_id,
                    "id": tool_call_id,
                    "turn_id": self.current_turn_id,
                    "exitCode": 0,
                    "stdout": content,
                    "stderr": "",
                    "command": f"read: {path}",
                })
                
                # Record to transcript
                await self._record({
                    "role": "command",
                    "id": tool_call_id,
                    "turn_id": self.current_turn_id,
                    "command": f"read: {path}",
                    "output": content,
                    "status": "completed",
                    "timestamp": utc_ts(),
                })
        except Exception as e:
            result = {"content": None, "error": str(e)}
            await self._emit({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "exitCode": 1,
                "stdout": "",
                "stderr": str(e),
                "command": f"read: {path}",
            })
        
        if self.write_response:
            await self.write_response({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            })
            print(f"[ACP] Sent read_text_file response: {len(result.get('content', '') or '')} chars")
        else:
            print(f"[ACP] WARNING: No write_response - cannot respond to read_text_file!")

    async def _handle_write_text_file(self, request_id: Any, params: Dict[str, Any]) -> None:
        """
        Handle fs/write_text_file request from agent.
        
        The agent wants to write content to a file.
        """
        from pathlib import Path
        
        path = params.get("path", "")
        content = params.get("content", "")
        tool_call_id = f"write_{request_id}"
        
        print(f"[ACP] fs/write_text_file: {path} ({len(content)} chars)")
        
        # Emit shell_begin for the write operation
        await self._emit({
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": f"write: {path}",
            "cwd": "",
        })
        
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            result = {}
            
            # Emit shell_end with success
            await self._emit({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "exitCode": 0,
                "stdout": f"Wrote {len(content)} chars to {path}",
                "stderr": "",
                "command": f"write: {path}",
            })
            
            # Record to transcript
            await self._record({
                "role": "command",
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "command": f"write: {path}",
                "output": f"Wrote {len(content)} chars",
                "status": "completed",
                "timestamp": utc_ts(),
            })
        except Exception as e:
            result = {"error": str(e)}
            await self._emit({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": self.current_turn_id,
                "exitCode": 1,
                "stdout": "",
                "stderr": str(e),
                "command": f"write: {path}",
            })
        
        if self.write_response:
            await self.write_response({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            })
            print(f"[ACP] Sent write_text_file response")
        else:
            print(f"[ACP] WARNING: No write_response - cannot respond to write_text_file!")

    async def _handle_agent_message_chunk(self, update: Dict[str, Any]) -> None:
        """Handle agent message text chunks (streaming response)."""
        content = update.get("content", {})
        if content.get("type") != "text":
            return
        
        text = content.get("text", "")
        if not text:
            return
        
        self.current_message_text += text
        
        # Switch to message block if we were in reasoning
        if self._last_block_type != "message":
            self._block_counter += 1
            self._last_block_type = "message"
            self.current_message_id = f"msg_{self._turn_counter}_{self._block_counter}"
        
        await self._emit({
            "type": "assistant_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_message_id,
            "delta": text,
        })
    
    async def _handle_agent_thought_chunk(self, update: Dict[str, Any]) -> None:
        """Handle agent reasoning/thought chunks."""
        content = update.get("content", {})
        if content.get("type") != "text":
            return
        
        text = content.get("text", "")
        if not text:
            return
        
        self.current_thought_text += text
        
        # Switch to reasoning block if we were in message
        if self._last_block_type != "reasoning":
            self._block_counter += 1
            self._last_block_type = "reasoning"
            self.current_reasoning_id = f"reasoning_{self._turn_counter}_{self._block_counter}"
        
        await self._emit({
            "type": "reasoning_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_reasoning_id,
            "delta": text,
        })
    
    async def _handle_tool_call_start(self, update: Dict[str, Any]) -> None:
        """Handle tool call start notification."""
        tool_call_id = update.get("toolCallId", "")
        title = update.get("title", "Tool Call")
        kind = update.get("kind", "other")  # shell, edit, read, other
        status = update.get("status", "pending")
        
        self.tool_calls[tool_call_id] = {
            "id": tool_call_id,
            "title": title,
            "kind": kind,
            "status": status,
            "content": [],
            "turn_id": self.current_turn_id,
        }
        
        # Broadcast shell_begin for shell commands (frontend expects this format)
        await self._emit({
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
            "turn_id": self.current_turn_id,
            "command": title,
            "cwd": "",
        })
    
    async def _handle_tool_call_update(self, update: Dict[str, Any]) -> None:
        """Handle tool call progress/completion."""
        tool_call_id = update.get("toolCallId", "")
        status = update.get("status", "")
        content = update.get("content", [])
        
        tool_call = self.tool_calls.get(tool_call_id)
        if tool_call:
            tool_call["status"] = status
            if content:
                tool_call["content"].extend(content)
        
        turn_id = tool_call.get("turn_id", self.current_turn_id) if tool_call else self.current_turn_id
        
        # Extract text content for display
        text_content = ""
        for item in content:
            if isinstance(item, dict):
                c = item.get("content", {})
                if isinstance(c, dict) and c.get("type") == "text":
                    text_content += c.get("text", "")
        
        if status == "in_progress":
            # Send as shell_delta for streaming output
            if text_content:
                await self._emit({
                    "type": "shell_delta",
                    "conversation_id": self.conversation_id,
                    "id": tool_call_id,
                    "turn_id": turn_id,
                    "delta": text_content,
                })
        elif status == "completed":
            # Send as shell_end
            await self._emit({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": turn_id,
                "exitCode": 0,
                "stdout": text_content,
                "stderr": "",
                "command": tool_call.get("title", "") if tool_call else "",
            })
            
            # Write to transcript
            if tool_call:
                await self._record({
                    "role": "command",
                    "id": tool_call_id,
                    "turn_id": turn_id,
                    "command": tool_call.get("title", ""),
                    "output": text_content,
                    "status": "completed",
                    "timestamp": utc_ts(),
                })
        elif status == "failed":
            await self._emit({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "turn_id": turn_id,
                "exitCode": 1,
                "stdout": "",
                "stderr": text_content,
                "command": tool_call.get("title", "") if tool_call else "",
            })
            
            # Write to transcript
            if tool_call:
                await self._record({
                    "role": "command",
                    "id": tool_call_id,
                    "turn_id": turn_id,
                    "command": tool_call.get("title", ""),
                    "output": text_content,
                    "status": "failed",
                    "timestamp": utc_ts(),
                })
    
    async def _handle_plan(self, update: Dict[str, Any]) -> None:
        """Handle agent plan updates."""
        entries = update.get("entries", [])
        
        steps = []
        for entry in entries:
            steps.append({
                "content": entry.get("content", ""),
                "status": entry.get("status", "pending"),
                "priority": entry.get("priority", "medium"),
            })
        
        await self._emit({
            "type": "plan",
            "conversation_id": self.conversation_id,
            "id": f"plan_{self.current_turn_id}",
            "turn_id": self.current_turn_id,
            "steps": steps,
        })
    
    async def _handle_response(self, message: Dict[str, Any]) -> None:
        """Handle JSON-RPC response (e.g., session/prompt completion)."""
        result = message.get("result", {})
        stop_reason = result.get("stopReason", "end_turn")
        
        # Finalize reasoning FIRST (it happened before the message in ACP flow)
        if self.current_thought_text:
            await self._record({
                "role": "reasoning",
                "id": self.current_reasoning_id,
                "text": self.current_thought_text,
                "timestamp": utc_ts(),
                "turn_id": self.current_turn_id,
            })
            self.current_thought_text = ""
        
        # Finalize message - broadcast finalize event AND write to transcript
        if self.current_message_text:
            # Broadcast finalize event (replaces accumulated deltas with authoritative text)
            await self._emit({
                "type": "assistant_finalize",
                "conversation_id": self.conversation_id,
                "id": self.current_message_id,  # Match the delta id
                "text": self.current_message_text,
                "turn_id": self.current_turn_id,
            })
            
            # Write to transcript for playback
            await self._record({
                "role": "assistant",
                "id": self.current_message_id,
                "text": self.current_message_text,
                "timestamp": utc_ts(),
                "turn_id": self.current_turn_id,
            })
            self.current_message_text = ""
        
        # Broadcast turn completed
        status = "success" if stop_reason == "end_turn" else "warning"
        if stop_reason in ("refusal", "max_tokens"):
            status = "error"
        
        await self._emit({
            "type": "turn_completed",
            "conversation_id": self.conversation_id,
            "stop_reason": stop_reason,
            "status": status,
            "turn_id": self.current_turn_id,
        })
        
        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "idle",
            "active": False,
            "turn_id": self.current_turn_id,
        })
        
        # Write status to transcript
        await self._record({
            "role": "status",
            "status": status,
            "stop_reason": stop_reason,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })
    
    async def _handle_error(self, message: Dict[str, Any]) -> None:
        """Handle JSON-RPC error response."""
        error = message.get("error", {})
        error_msg = error.get("message", "Unknown error")
        error_code = error.get("code", -1)
        
        await self._emit({
            "type": "rpc_error",
            "conversation_id": self.conversation_id,
            "message": error_msg,
            "code": error_code,
            "turn_id": self.current_turn_id,
        })
        
        await self._emit({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": error_msg,
            "active": True,
            "turn_id": self.current_turn_id,
        })
    
    async def on_turn_start(self, text: str) -> None:
        """Called when a new turn starts (user sends message)."""
        self._turn_counter += 1
        self.current_turn_id = f"turn_{self._turn_counter}"
        self.current_message_id = f"msg_{self._turn_counter}_0"  # Will be updated on first chunk
        self.current_reasoning_id = f"reasoning_{self._turn_counter}_0"  # Will be updated on first chunk
        self.current_message_text = ""
        self.current_thought_text = ""
        self.tool_calls = {}
        self._block_counter = 0  # Reset block counter for new turn
        self._last_block_type = None  # Reset block type tracking
        
        user_msg_id = f"user_{self._turn_counter}"
        
        # Broadcast user message to frontend
        await self._emit({
            "type": "message",
            "conversation_id": self.conversation_id,
            "id": user_msg_id,
            "role": "user",
            "text": text,
            "turn_id": self.current_turn_id,
        })
        
        # Broadcast turn started
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
        
        # Write user message to transcript
        await self._record({
            "role": "user",
            "id": user_msg_id,
            "text": text,
            "timestamp": utc_ts(),
            "turn_id": self.current_turn_id,
        })
    
    def _kind_to_item_type(self, kind: str) -> str:
        """Map ACP tool kind to our item type."""
        mapping = {
            "shell": "shell",
            "edit": "fileChange",
            "read": "fileRead",
            "other": "tool",
        }
        return mapping.get(kind, "tool")


def parse_acp_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a line of ACP output (JSON-RPC).
    
    Returns None if line is not valid JSON or not a JSON-RPC message.
    """
    line = line.strip()
    if not line:
        return None
    
    try:
        data = json.loads(line)
        if isinstance(data, dict) and ("method" in data or "result" in data or "error" in data):
            return data
        return None
    except json.JSONDecodeError:
        return None
