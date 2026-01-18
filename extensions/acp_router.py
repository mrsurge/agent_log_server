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
        self.current_message_text: str = ""
        self.current_thought_text: str = ""
        self.tool_calls: Dict[str, Dict[str, Any]] = {}  # tool_call_id -> info
        self._turn_counter: int = 0  # Increments each turn for unique reasoning ids
    
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
            else:
                print(f"[ACP] Unhandled request method: {method}")
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
        We need to send a response with outcome: "approved" | "denied" | "cancelled"
        
        For now: auto-approve all requests.
        Future: broadcast to frontend, wait for user decision.
        """
        session_id = params.get("sessionId", "")
        tool_call = params.get("toolCall", {})
        options = params.get("options", [])
        
        tool_call_id = tool_call.get("toolCallId", "")
        title = tool_call.get("title", "Tool Call")
        kind = tool_call.get("kind", "other")
        
        print(f"[ACP] Permission request: id={request_id} tool={title} kind={kind}")
        
        # Broadcast approval request to frontend (for UI indication)
        await self.broadcast({
            "type": "approval_request",
            "conversation_id": self.conversation_id,
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "title": title,
            "kind": kind,
            "options": options,
        })
        
        # TODO: In future, wait for user decision via a pending_approvals queue
        # For now, auto-approve immediately
        outcome = "approved"
        
        # Send response back to agent
        if self.write_response:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "outcome": outcome,
                }
            }
            await self.write_response(response)
            print(f"[ACP] Sent approval response: {outcome}")
        else:
            print(f"[ACP] WARNING: No write_response function - cannot respond to permission request!")

    async def _handle_agent_message_chunk(self, update: Dict[str, Any]) -> None:
        """Handle agent message text chunks (streaming response)."""
        content = update.get("content", {})
        if content.get("type") != "text":
            return
        
        text = content.get("text", "")
        if not text:
            return
        
        self.current_message_text += text
        
        # Broadcast delta to frontend with turn-specific id for proper ordering
        event = {
            "type": "assistant_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_turn_id,
            "delta": text,
        }
        print(f"[ACP] Broadcasting assistant_delta: {len(text)} chars to {self.conversation_id[:8]}")
        await self.broadcast(event)
    
    async def _handle_agent_thought_chunk(self, update: Dict[str, Any]) -> None:
        """Handle agent reasoning/thought chunks."""
        content = update.get("content", {})
        if content.get("type") != "text":
            return
        
        text = content.get("text", "")
        if not text:
            return
        
        self.current_thought_text += text
        
        # Broadcast reasoning delta to frontend with turn-specific id
        # This ensures each turn's reasoning appears as a separate block
        await self.broadcast({
            "type": "reasoning_delta",
            "conversation_id": self.conversation_id,
            "id": self.current_turn_id,  # Unique per turn for proper ordering
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
        }
        
        # Broadcast shell_begin for shell commands (frontend expects this format)
        await self.broadcast({
            "type": "shell_begin",
            "conversation_id": self.conversation_id,
            "id": tool_call_id,
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
                await self.broadcast({
                    "type": "shell_delta",
                    "conversation_id": self.conversation_id,
                    "id": tool_call_id,
                    "delta": text_content,
                })
        elif status == "completed":
            # Send as shell_end
            await self.broadcast({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "exitCode": 0,
                "stdout": text_content,
                "stderr": "",
                "command": tool_call.get("title", "") if tool_call else "",
            })
            
            # Write to transcript
            if tool_call:
                await self.append_transcript(self.conversation_id, {
                    "role": "command",
                    "command": tool_call.get("title", ""),
                    "output": text_content,
                    "status": "completed",
                    "timestamp": utc_ts(),
                })
        elif status == "failed":
            await self.broadcast({
                "type": "shell_end",
                "conversation_id": self.conversation_id,
                "id": tool_call_id,
                "exitCode": 1,
                "stdout": "",
                "stderr": text_content,
                "command": tool_call.get("title", "") if tool_call else "",
            })
            
            # Write to transcript
            if tool_call:
                await self.append_transcript(self.conversation_id, {
                    "role": "command",
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
        
        await self.broadcast({
            "type": "plan",
            "conversation_id": self.conversation_id,
            "steps": steps,
        })
    
    async def _handle_response(self, message: Dict[str, Any]) -> None:
        """Handle JSON-RPC response (e.g., session/prompt completion)."""
        result = message.get("result", {})
        stop_reason = result.get("stopReason", "end_turn")
        
        # Finalize reasoning FIRST (it happened before the message in ACP flow)
        if self.current_thought_text:
            await self.append_transcript(self.conversation_id, {
                "role": "reasoning",
                "text": self.current_thought_text,
                "timestamp": utc_ts(),
            })
            self.current_thought_text = ""
        
        # Finalize message - broadcast finalize event AND write to transcript
        if self.current_message_text:
            # Broadcast finalize event (replaces accumulated deltas with authoritative text)
            await self.broadcast({
                "type": "assistant_finalize",
                "conversation_id": self.conversation_id,
                "text": self.current_message_text,
            })
            
            # Write to transcript for playback
            await self.append_transcript(self.conversation_id, {
                "role": "assistant",
                "text": self.current_message_text,
                "timestamp": utc_ts(),
            })
            self.current_message_text = ""
        
        # Broadcast turn completed
        status = "success" if stop_reason == "end_turn" else "warning"
        if stop_reason in ("refusal", "max_tokens"):
            status = "error"
        
        await self.broadcast({
            "type": "turn_completed",
            "conversation_id": self.conversation_id,
            "stop_reason": stop_reason,
            "status": status,
        })
        
        await self.broadcast({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "idle",
            "active": False,
        })
        
        # Write status to transcript
        await self.append_transcript(self.conversation_id, {
            "role": "status",
            "status": status,
            "stop_reason": stop_reason,
            "timestamp": utc_ts(),
        })
    
    async def _handle_error(self, message: Dict[str, Any]) -> None:
        """Handle JSON-RPC error response."""
        error = message.get("error", {})
        error_msg = error.get("message", "Unknown error")
        error_code = error.get("code", -1)
        
        await self.broadcast({
            "type": "rpc_error",
            "conversation_id": self.conversation_id,
            "message": error_msg,
            "code": error_code,
        })
        
        await self.broadcast({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": error_msg,
            "active": True,
        })
    
    async def on_turn_start(self, text: str) -> None:
        """Called when a new turn starts (user sends message)."""
        self._turn_counter += 1
        self.current_turn_id = f"turn_{self._turn_counter}"
        self.current_message_text = ""
        self.current_thought_text = ""
        self.tool_calls = {}
        
        # Broadcast user message to frontend
        await self.broadcast({
            "type": "message",
            "conversation_id": self.conversation_id,
            "role": "user",
            "text": text,
        })
        
        # Broadcast turn started
        await self.broadcast({
            "type": "turn_started",
            "conversation_id": self.conversation_id,
        })
        
        await self.broadcast({
            "type": "activity",
            "conversation_id": self.conversation_id,
            "label": "thinking",
            "active": True,
        })
        
        # Write user message to transcript
        await self.append_transcript(self.conversation_id, {
            "role": "user",
            "text": text,
            "timestamp": utc_ts(),
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
