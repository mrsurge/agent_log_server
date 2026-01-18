"""
ACP Client Handler for Extension System

Manages ACP agent connections (e.g., Gemini CLI) via framework_shells.
Translates between our conversation system and ACP sessions.

Key concepts:
- Our conversation_id maps to ACP session_id
- We act as the ACP Client, the extension (Gemini) is the ACP Agent
- All communication goes through JSON-RPC over stdio (framework_shells pipe backend)
- ACP updates are translated to our internal event format
"""

import asyncio
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable, Awaitable, TYPE_CHECKING
from dataclasses import dataclass, field
import acp

if TYPE_CHECKING:
    from extensions.acp_router import ACPEventRouter


@dataclass
class ACPSession:
    """Tracks an active ACP session."""
    conversation_id: str  # Our conversation ID
    session_id: Optional[str] = None  # ACP session ID (assigned after session/new)
    extension_id: str = ""
    shell_id: Optional[str] = None  # framework_shells shell ID
    cwd: Optional[str] = None  # Working directory for the session
    router: Optional[Any] = None  # ACPEventRouter instance
    connection: Optional[Any] = None  # acp ClientSideConnection
    initialized: bool = False
    ready: bool = False  # True once agent process is responsive


@dataclass
class ACPExtension:
    """Configuration for an ACP extension."""
    id: str
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    shellspec: str = ""  # Path to shellspec yaml
    path: str = ""  # Path to extension directory (relative to extensions_dir)
    
    @classmethod
    def from_manifest(cls, manifest: Dict[str, Any], extensions_dir: Path, ext_path: str = "") -> "ACPExtension":
        agent = manifest.get("agent", {})
        ext_id = manifest["id"]
        # Default shellspec path based on extension id
        shellspec = agent.get("shellspec", f"shellspec/{ext_id.replace('-', '_')}.yaml")
        return cls(
            id=ext_id,
            name=manifest["name"],
            command=agent.get("command", ""),
            args=agent.get("args", []),
            env=agent.get("env", {}),
            capabilities=manifest.get("capabilities", {}),
            shellspec=shellspec,
            path=ext_path,
        )


class ACPClientHandler:
    """
    ACP Client implementation that bridges our system to ACP agents.
    
    Implements the Client protocol methods that agents can call:
    - read_text_file / write_text_file (file system access)
    - create_terminal / terminal_output / etc (terminal access)
    - request_permission (approval flow)
    - session_update (receive agent updates)
    """
    
    def __init__(
        self,
        conversation_id: str,
        on_update: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.conversation_id = conversation_id
        self.on_update = on_update
        self._conn: Optional[Any] = None
    
    def on_connect(self, conn: Any) -> None:
        """Called when connection is established."""
        self._conn = conn
    
    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        """Receive session updates from the agent."""
        if self.on_update:
            update_dict = {
                "session_id": session_id,
                "update": update.model_dump() if hasattr(update, "model_dump") else update,
            }
            self.on_update(self.conversation_id, update_dict)
    
    async def request_permission(
        self,
        options: List[Any],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> Any:
        """Handle permission requests - auto-approve for now."""
        # TODO: Route through our approval system
        return acp.RequestPermissionResponse(outcome="approved")
    
    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: Optional[int] = None,
        line: Optional[int] = None,
        **kwargs: Any,
    ) -> Any:
        """Read a file from the local filesystem."""
        try:
            p = Path(path)
            if not p.exists():
                return acp.ReadTextFileResponse(content=None, error=f"File not found: {path}")
            content = p.read_text(encoding="utf-8")
            if limit:
                content = content[:limit]
            return acp.ReadTextFileResponse(content=content)
        except Exception as e:
            return acp.ReadTextFileResponse(content=None, error=str(e))
    
    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: Any,
    ) -> Any:
        """Write content to a file."""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return acp.WriteTextFileResponse()
        except Exception as e:
            return acp.WriteTextFileResponse(error=str(e))
    
    # Terminal methods - stubs for now
    async def create_terminal(self, command: str, session_id: str, **kwargs) -> Any:
        return acp.CreateTerminalResponse(terminalId=f"term_{session_id}")
    
    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs) -> Any:
        return acp.TerminalOutputResponse(output="", exitCode=None)
    
    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs) -> Any:
        return acp.WaitForTerminalExitResponse(exitCode=0)
    
    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs) -> Any:
        return acp.KillTerminalCommandResponse()
    
    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs) -> Any:
        return acp.ReleaseTerminalResponse()
    
    async def ext_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return {"error": f"Unknown method: {method}"}
    
    async def ext_notification(self, method: str, params: Dict[str, Any]) -> None:
        pass


def _make_write_fn(shell_id: str, fws_mgr: Any) -> Callable[[Dict[str, Any]], Awaitable[None]]:
    """
    Create an async function that writes JSON-RPC responses to the agent's stdin.
    
    This is used by ACPEventRouter to respond to incoming requests like session/request_permission.
    """
    async def write_response(response: Dict[str, Any]) -> None:
        state = fws_mgr.get_pipe_state(shell_id)
        if not state or not state.process.stdin:
            print(f"[ACP] Cannot write response - no stdin for shell {shell_id}")
            return
        
        line = json.dumps(response, ensure_ascii=False) + "\n"
        _add_to_raw_buffer("out", "__response__", line.strip())
        state.process.stdin.write(line.encode("utf-8"))
        await state.process.stdin.drain()
        print(f"[ACP] Wrote response to agent: id={response.get('id')}")
    
    return write_response


class ACPManager:
    """
    Manages ACP extensions and sessions.
    
    - Loads extension manifests from static/extensions/
    - Spawns agent processes via framework_shells
    - Maps our conversation_id to ACP session_id
    - Translates ACP events to our internal format
    """
    
    def __init__(self, extensions_dir: Path, server_root: Path):
        self.extensions_dir = extensions_dir
        self.server_root = server_root
        self.extensions: Dict[str, ACPExtension] = {}
        self.sessions: Dict[str, ACPSession] = {}  # conversation_id -> session
        self._update_callbacks: Dict[str, Callable] = {}
        self._reader_tasks: Dict[str, asyncio.Task] = {}
    
    def load_extensions(self) -> None:
        """Load all extension manifests."""
        extensions_json = self.extensions_dir / "extensions.json"
        if not extensions_json.exists():
            return
        
        data = json.loads(extensions_json.read_text())
        for ext_info in data.get("extensions", []):
            if not ext_info.get("enabled", True):
                continue
            
            ext_path = ext_info.get("path", "")
            manifest_path = self.extensions_dir / ext_path / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                extension = ACPExtension.from_manifest(manifest, self.extensions_dir, ext_path)
                self.extensions[extension.id] = extension
    
    def get_extension(self, extension_id: str) -> Optional[ACPExtension]:
        return self.extensions.get(extension_id)
    
    def list_extensions(self) -> List[Dict[str, Any]]:
        return [
            {"id": ext.id, "name": ext.name, "command": ext.command}
            for ext in self.extensions.values()
        ]
    
    def has_session(self, conversation_id: str) -> bool:
        return conversation_id in self.sessions
    
    def get_session(self, conversation_id: str) -> Optional[ACPSession]:
        return self.sessions.get(conversation_id)
    
    async def start_shell(
        self,
        conversation_id: str,
        extension_id: str,
        cwd: str,
        fws_manager: Any,  # framework_shells manager
    ) -> Optional[str]:
        """
        Start an ACP agent shell via framework_shells.
        Returns the shell_id on success.
        """
        extension = self.extensions.get(extension_id)
        if not extension:
            print(f"[ACP] Extension not found: {extension_id}")
            return None
        
        from framework_shells.orchestrator import Orchestrator
        
        orch = Orchestrator(fws_manager)
        spec_path = self.server_root / extension.shellspec
        
        if not spec_path.exists():
            print(f"[ACP] Shellspec not found: {spec_path}")
            return None
        
        # Extract shell name from spec (e.g., "gemini_acp" from "shellspec/gemini_acp.yaml")
        shell_name = spec_path.stem
        
        # Ensure cwd is absolute
        if not Path(cwd).is_absolute():
            cwd = str(Path(cwd).resolve())
        
        try:
            shell = await orch.start_from_ref(
                f"{spec_path}#{shell_name}",
                base_dir=spec_path.parent,
                ctx={"CWD": cwd, "CONVERSATION_ID": conversation_id},
                label=f"acp:{extension_id}:{conversation_id[:8]}",
                wait_ready=False,
            )
            
            # Create session record with cwd
            session = ACPSession(
                conversation_id=conversation_id,
                extension_id=extension_id,
                shell_id=shell.id,
                cwd=cwd,
            )
            self.sessions[conversation_id] = session
            
            print(f"[ACP] Started shell {shell.id} for {extension_id} conversation {conversation_id}")
            return shell.id
            
        except Exception as e:
            print(f"[ACP] Failed to start shell: {e}")
            return None
    
    async def initialize_acp(
        self,
        conversation_id: str,
        fws_manager: Any,
        on_update: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> bool:
        """
        Initialize ACP connection for a session.
        Must be called after start_shell.
        """
        session = self.sessions.get(conversation_id)
        if not session or not session.shell_id:
            return False
        
        state = fws_manager.get_pipe_state(session.shell_id)
        if not state or not state.process.stdin or not state.process.stdout:
            print(f"[ACP] Pipe not available for shell {session.shell_id}")
            return False
        
        try:
            # Create ACP client handler
            client = ACPClientHandler(
                conversation_id=conversation_id,
                on_update=on_update,
            )
            
            if on_update:
                self._update_callbacks[conversation_id] = on_update
            
            # Connect to agent via ACP
            conn = acp.connect_to_agent(
                client,
                input_stream=state.process.stdin,
                output_stream=state.process.stdout,
            )
            
            # Initialize connection
            init_response = await conn.initialize(
                client_capabilities={
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                client_info={"name": "agent-log-server", "version": "1.0.0"},
            )
            
            print(f"[ACP] Initialized: agent={init_response.agent_info}")
            
            # Create new session
            session_response = await conn.session_new()
            session.session_id = session_response.session_id
            session.connection = conn
            session.initialized = True
            
            print(f"[ACP] Session created: {session.session_id}")
            return True
            
        except Exception as e:
            print(f"[ACP] Initialize failed: {e}")
            return False
    
    async def send_prompt(
        self,
        conversation_id: str,
        text: str,
    ) -> Optional[Dict[str, Any]]:
        """Send a prompt to an ACP session."""
        session = self.sessions.get(conversation_id)
        if not session or not session.connection or not session.initialized:
            return {"error": "Session not initialized"}
        
        try:
            response = await session.connection.session_prompt(
                session_id=session.session_id,
                prompt=[acp.text_block(text)],
            )
            return {
                "ok": True,
                "stop_reason": response.stop_reason if hasattr(response, "stop_reason") else "end_turn",
            }
        except Exception as e:
            return {"error": str(e)}
    
    async def cancel_prompt(self, conversation_id: str) -> bool:
        """Cancel an ongoing prompt."""
        session = self.sessions.get(conversation_id)
        if not session or not session.connection:
            return False
        
        try:
            await session.connection.session_cancel(session_id=session.session_id)
            return True
        except Exception:
            return False
    
    async def close_session(
        self,
        conversation_id: str,
        fws_manager: Any,
    ) -> bool:
        """Close an ACP session and stop the shell."""
        session = self.sessions.pop(conversation_id, None)
        if not session:
            return False
        
        self._update_callbacks.pop(conversation_id, None)
        
        # Cancel reader task if any
        task = self._reader_tasks.pop(conversation_id, None)
        if task:
            task.cancel()
        
        # Stop the shell
        if session.shell_id:
            try:
                await fws_manager.stop_shell(session.shell_id)
            except Exception as e:
                print(f"[ACP] Error stopping shell: {e}")
        
        return True
    
    async def close_all(self, fws_manager: Any) -> None:
        """Close all sessions."""
        for convo_id in list(self.sessions.keys()):
            await self.close_session(convo_id, fws_manager)


# Global manager instance
_manager: Optional[ACPManager] = None
_fws_getter: Optional[Callable] = None
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[Dict[str, Callable]] = None

# Ready events for warm-up: extension_id -> asyncio.Event
_ready_events: Dict[str, asyncio.Event] = {}
# Warm-up shells: extension_id -> shell_id (started eagerly, not tied to a conversation)
_warmup_shells: Dict[str, str] = {}
# Shared shells: extension_id -> shell_id (permanent shell for multiplexing sessions)
_shared_shells: Dict[str, str] = {}

# Debug buffer for raw ACP messages (circular buffer)
_acp_raw_buffer: List[Dict[str, Any]] = []
_ACP_RAW_BUFFER_MAX = 200


def _add_to_raw_buffer(direction: str, conversation_id: str, data: Any) -> None:
    """Add a message to the debug buffer."""
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dir": direction,  # "in" or "out"
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": data if isinstance(data, str) else str(data)[:500],
    }
    _acp_raw_buffer.append(entry)
    if len(_acp_raw_buffer) > _ACP_RAW_BUFFER_MAX:
        _acp_raw_buffer.pop(0)


def get_acp_raw_buffer(limit: int = 50) -> List[Dict[str, Any]]:
    """Get the last N entries from the raw buffer."""
    return _acp_raw_buffer[-limit:]


def get_acp_manager() -> Optional[ACPManager]:
    return _manager


def init_acp_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
) -> ACPManager:
    """
    Initialize the ACP manager with callbacks to server infrastructure.
    
    Args:
        extensions_dir: Path to static/extensions/
        server_root: Path to server root directory
        fws_getter: Async function to get framework_shells manager
        broadcast_fn: Async function to broadcast WebSocket events
        transcript_fn: Async function to append transcript entries
        meta_fns: Optional dict with "load" and "save" functions for conversation meta
    """
    global _manager, _fws_getter, _broadcast_fn, _transcript_fn, _meta_fns
    _manager = ACPManager(extensions_dir, server_root)
    _manager.load_extensions()
    _fws_getter = fws_getter
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns or {}
    return _manager


async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Handle a message for an ACP-based conversation.
    
    This is the main entry point called by server.py's extension router.
    Manages session lifecycle and delegates to ACPManager.
    """
    from extensions.acp_router import ACPEventRouter, parse_acp_line
    
    if not _manager or not _fws_getter or not _broadcast_fn or not _transcript_fn:
        return {"ok": False, "error": "ACP manager not initialized"}
    
    cwd = settings.get("cwd") or os.path.expanduser("~")
    fws_mgr = await _fws_getter()
    
    # Check if session exists for this conversation
    if not _manager.has_session(conversation_id):
        # Use init_session which handles shared shell multiplexing
        result = await init_session(conversation_id, agent_type, cwd)
        if not result.get("ok"):
            return result
    
    # Ensure session has a session_id before sending prompt
    session = _manager.get_session(conversation_id)
    if not session or not session.session_id:
        # Session exists but not fully initialized - wait a bit
        for _ in range(30):  # 3 seconds max
            await asyncio.sleep(0.1)
            session = _manager.get_session(conversation_id)
            if session and session.session_id:
                break
        
        if not session or not session.session_id:
            return {"ok": False, "error": "Session not ready"}
    
    # Send the prompt
    return await _send_prompt(conversation_id, text, fws_mgr)


async def _start_new_session(
    conversation_id: str,
    agent_type: str,
    cwd: str,
    fws_mgr: Any,
) -> Dict[str, Any]:
    """Start a new ACP session from scratch (fallback when no warmed-up session available)."""
    from extensions.acp_router import ACPEventRouter
    
    shell_id = await _manager.start_shell(conversation_id, agent_type, cwd, fws_mgr)
    if not shell_id:
        return {"ok": False, "error": f"Failed to start {agent_type} agent"}
    
    router = ACPEventRouter(
        conversation_id=conversation_id,
        broadcast_fn=_broadcast_fn,
        transcript_fn=_transcript_fn,
        write_fn=_make_write_fn(shell_id, fws_mgr),
    )
    
    session = _manager.get_session(conversation_id)
    if session:
        session.router = router
    
    asyncio.create_task(
        _acp_reader_loop(conversation_id, shell_id, router, fws_mgr),
        name=f"acp-reader:{conversation_id}"
    )
    
    if not await _initialize_session(conversation_id, fws_mgr):
        return {"ok": False, "error": "Failed to initialize ACP connection"}
    
    return {"ok": True, "session_id": session.session_id if session else None}


async def _acp_reader_loop(
    conversation_id: str,
    shell_id: str,
    router: "ACPEventRouter",
    fws_mgr: Any,
) -> None:
    """Read ACP agent stdout and route events."""
    from extensions.acp_router import parse_acp_line
    
    state = fws_mgr.get_pipe_state(shell_id)
    if not state or not state.process.stdout:
        print(f"[ACP] No stdout for shell {shell_id}")
        return
    
    print(f"[ACP] Reader started for {conversation_id}")
    session = _manager.get_session(conversation_id) if _manager else None
    
    try:
        while True:
            line = await state.process.stdout.readline()
            if not line:
                print(f"[ACP] EOF for {conversation_id}")
                _add_to_raw_buffer("in", conversation_id, "[EOF]")
                break
            
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            
            # Log to debug buffer
            _add_to_raw_buffer("in", conversation_id, line_str)
            print(f"[ACP] RAW: {line_str[:200]}")
            
            message = parse_acp_line(line_str)
            
            if message:
                print(f"[ACP] PARSED: method={message.get('method')} id={message.get('id')}")
                await router.route_event(message)
                
                # Capture session_id from session/new response (id=2)
                if message.get("id") == 2 and message.get("result"):
                    result = message["result"]
                    if "sessionId" in result:
                        session_id = result["sessionId"]
                        if session:
                            session.session_id = session_id
                            # Store as thread_id in meta if we have the functions
                            if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
                                meta = _meta_fns["load"](conversation_id)
                                if meta:
                                    meta["thread_id"] = session_id
                                    meta["status"] = "active"  # No longer a draft
                                    _meta_fns["save"](conversation_id, meta)
                            print(f"[ACP] Session ID captured: {session_id}")
            else:
                print(f"[ACP] Could not parse line")
    
    except asyncio.CancelledError:
        print(f"[ACP] Reader cancelled for {conversation_id}")
    except Exception as e:
        print(f"[ACP] Reader error for {conversation_id}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print(f"[ACP] Reader ended for {conversation_id}")


async def _send_session_new(conversation_id: str, cwd: str, fws_mgr: Any) -> bool:
    """
    Send session/new to create an ACP session with the correct CWD.
    Called after adopting a warmed-up process that has already been initialized.
    """
    if not _manager:
        return False
    
    session = _manager.get_session(conversation_id)
    if not session or not session.shell_id:
        return False
    
    state = fws_mgr.get_pipe_state(session.shell_id)
    if not state or not state.process.stdin:
        return False
    
    # Send session/new request with correct cwd
    session_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/new",
        "params": {
            "cwd": cwd,
            "mcpServers": []
        }
    }
    
    line = json.dumps(session_request, ensure_ascii=False) + "\n"
    _add_to_raw_buffer("out", conversation_id, line.strip())
    state.process.stdin.write(line.encode("utf-8"))
    await state.process.stdin.drain()
    print(f"[ACP] Sent session/new for {conversation_id} with cwd={cwd}")
    
    return True


async def _initialize_session(conversation_id: str, fws_mgr: Any) -> bool:
    """Initialize ACP connection (send initialize + session/new)."""
    if not _manager:
        return False
    
    session = _manager.get_session(conversation_id)
    if not session or not session.shell_id:
        return False
    
    state = fws_mgr.get_pipe_state(session.shell_id)
    if not state or not state.process.stdin:
        return False
    
    # Get cwd from session or default to home
    cwd = session.cwd or os.path.expanduser("~")
    
    # Send initialize request
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True
            },
            "clientInfo": {"name": "agent-log-server", "version": "1.0.0"}
        }
    }
    
    line = json.dumps(init_request, ensure_ascii=False) + "\n"
    _add_to_raw_buffer("out", conversation_id, line.strip())
    state.process.stdin.write(line.encode("utf-8"))
    await state.process.stdin.drain()
    print(f"[ACP] Sent initialize for {conversation_id}")
    
    await asyncio.sleep(0.5)
    
    # Send session/new request with cwd
    session_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session/new",
        "params": {
            "cwd": cwd,
            "mcpServers": []
        }
    }
    
    line = json.dumps(session_request, ensure_ascii=False) + "\n"
    _add_to_raw_buffer("out", conversation_id, line.strip())
    state.process.stdin.write(line.encode("utf-8"))
    await state.process.stdin.drain()
    print(f"[ACP] Sent session/new for {conversation_id} with cwd={cwd}")
    
    # Wait for session_id to be captured by reader (poll with timeout)
    for _ in range(20):  # 2 seconds max
        await asyncio.sleep(0.1)
        if session.session_id:
            print(f"[ACP] Got session_id: {session.session_id}")
            session.initialized = True
            return True
    
    print(f"[ACP] Timeout waiting for session_id")
    return False


async def _send_prompt(conversation_id: str, text: str, fws_mgr: Any) -> Dict[str, Any]:
    """Send a prompt to an ACP session."""
    if not _manager:
        return {"ok": False, "error": "ACP manager not initialized"}
    
    session = _manager.get_session(conversation_id)
    if not session:
        return {"ok": False, "error": "Session not found"}
    
    if not session.session_id:
        return {"ok": False, "error": "Session not initialized"}
    
    state = fws_mgr.get_pipe_state(session.shell_id)
    if not state or not state.process.stdin:
        return {"ok": False, "error": "Shell not available"}
    
    # Notify router of turn start (records user message, broadcasts turn_started)
    if session.router:
        await session.router.on_turn_start(text)
    
    # Send session/prompt request
    prompt_request = {
        "jsonrpc": "2.0",
        "id": int(datetime.now(timezone.utc).timestamp() * 1000),
        "method": "session/prompt",
        "params": {
            "sessionId": session.session_id,
            "prompt": [{"type": "text", "text": text}]
        }
    }
    
    line = json.dumps(prompt_request, ensure_ascii=False) + "\n"
    _add_to_raw_buffer("out", conversation_id, line.strip())
    state.process.stdin.write(line.encode("utf-8"))
    await state.process.stdin.drain()
    print(f"[ACP] Sent prompt for {conversation_id}")
    
    return {"ok": True, "session_id": session.session_id}


def list_extensions() -> List[Dict[str, Any]]:
    """List available ACP extensions."""
    if not _manager:
        return []
    return _manager.list_extensions()


def get_extension_config(extension_id: str) -> Optional[Dict[str, Any]]:
    """Get full extension config including manifest data."""
    if not _manager:
        return None
    ext = _manager.get_extension(extension_id)
    if not ext:
        return None
    
    # Load manifest from extension path
    if ext.path:
        manifest_file = _manager.extensions_dir / ext.path / "manifest.json"
        if manifest_file.exists():
            try:
                return json.loads(manifest_file.read_text())
            except:
                pass
    return None


def requires_eager_session_init(extension_id: str) -> bool:
    """Check if an extension requires eager session initialization."""
    config = get_extension_config(extension_id)
    if not config:
        return False
    agent_config = config.get("agent", {})
    return agent_config.get("eagerSessionInit", False)


async def init_session(
    conversation_id: str,
    extension_id: str,
    cwd: str,
) -> Dict[str, Any]:
    """
    Initialize an ACP session for a conversation (eager init on settings save).
    
    Uses a shared shell per extension - all conversations multiplex through one process.
    Each conversation gets its own ACP session via session/new.
    
    Returns {"ok": True, "session_id": ...} on success.
    """
    global _shared_shells
    
    if not _manager or not _fws_getter:
        return {"ok": False, "error": "ACP manager not initialized"}
    
    fws_mgr = await _fws_getter()
    if not fws_mgr:
        return {"ok": False, "error": "Framework shells not available"}
    
    # Expand CWD
    if cwd.startswith("~"):
        cwd = os.path.expanduser(cwd)
    
    # Check if session already exists for this conversation
    if _manager.has_session(conversation_id):
        session = _manager.get_session(conversation_id)
        if session and session.session_id:
            return {"ok": True, "session_id": session.session_id, "already_initialized": True}
    
    # Get or establish shared shell for this extension
    shell_id = _shared_shells.get(extension_id)
    
    if not shell_id:
        # Check if warmup shell is ready - promote it to shared shell
        warmup_convo_id = f"__warmup__{extension_id}"
        warmup_session = _manager.get_session(warmup_convo_id)
        
        if warmup_session and warmup_session.ready and warmup_session.shell_id:
            shell_id = warmup_session.shell_id
            _shared_shells[extension_id] = shell_id
            print(f"[ACP] init_session: promoted warmup shell {shell_id} to shared for {extension_id}")
            # Keep warmup session for tracking, but mark shell as shared
        else:
            # Wait for warmup to complete
            if not is_extension_ready(extension_id):
                print(f"[ACP] init_session: waiting for {extension_id} to be ready...")
                ready = await wait_extension_ready(extension_id, timeout=60.0)
                if not ready:
                    return {"ok": False, "error": f"{extension_id} agent failed to start"}
                
                # Now get the shell
                warmup_session = _manager.get_session(warmup_convo_id)
                if warmup_session and warmup_session.shell_id:
                    shell_id = warmup_session.shell_id
                    _shared_shells[extension_id] = shell_id
                    print(f"[ACP] init_session: promoted warmup shell {shell_id} to shared for {extension_id}")
    
    if not shell_id:
        return {"ok": False, "error": "No shell available for extension"}
    
    # Create a new session object for this conversation (shares the shell)
    session = ACPSession(
        conversation_id=conversation_id,
        extension_id=extension_id,
        shell_id=shell_id,
        cwd=cwd,
        initialized=True,  # Shell is already initialized
        ready=False,  # Session not ready until we get session_id
    )
    _manager.sessions[conversation_id] = session
    
    # Create router for this conversation
    from extensions.acp_router import ACPEventRouter
    router = ACPEventRouter(
        conversation_id=conversation_id,
        broadcast_fn=_broadcast_fn,
        transcript_fn=_transcript_fn,
        write_fn=_make_write_fn(shell_id, fws_mgr),
    )
    session.router = router
    
    # Start reader task for this conversation
    asyncio.create_task(
        _acp_reader_loop(conversation_id, shell_id, router, fws_mgr),
        name=f"acp-reader:{conversation_id}"
    )
    
    # Send session/new with correct CWD
    if not await _send_session_new(conversation_id, cwd, fws_mgr):
        return {"ok": False, "error": "Failed to create ACP session"}
    
    # Wait for session_id to be captured by reader
    for _ in range(30):  # 3 seconds max
        await asyncio.sleep(0.1)
        if session.session_id:
            session.ready = True
            print(f"[ACP] init_session: session ready, id={session.session_id}")
            return {"ok": True, "session_id": session.session_id}
    
    return {"ok": False, "error": "Timeout waiting for session_id"}


async def warm_up_extension(
    extension_id: str,
    timeout: float = 60.0,
) -> bool:
    """
    Eagerly start an ACP extension shell and complete the handshake.
    
    Gemini CLI (Node.js) takes up to 60s to load. This function:
    1. Starts the shell if not already running
    2. Sends initialize request
    3. Waits for initialize response (ready signal)
    4. Completes session/new handshake
    5. Returns True once fully ready
    
    The warmed-up session can be adopted by a conversation later.
    """
    global _warmup_shells, _ready_events
    
    if not _manager or not _fws_getter:
        print(f"[ACP] warm_up_extension: manager not initialized")
        return False
    
    extension = _manager.get_extension(extension_id)
    if not extension:
        print(f"[ACP] warm_up_extension: extension not found: {extension_id}")
        return False
    
    # Check if already warming up or ready
    if extension_id in _ready_events:
        event = _ready_events[extension_id]
        if event.is_set():
            print(f"[ACP] warm_up_extension: {extension_id} already ready")
            return True
        # Wait for existing warm-up
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return event.is_set()
        except asyncio.TimeoutError:
            print(f"[ACP] warm_up_extension: timeout waiting for {extension_id}")
            return False
    
    # Create ready event
    ready_event = asyncio.Event()
    _ready_events[extension_id] = ready_event
    
    fws_mgr = await _fws_getter()
    
    # Start the shell with a warmup conversation ID
    warmup_convo_id = f"__warmup__{extension_id}"
    cwd = os.path.expanduser("~")
    
    shell_id = await _manager.start_shell(warmup_convo_id, extension_id, cwd, fws_mgr)
    if not shell_id:
        print(f"[ACP] warm_up_extension: failed to start shell for {extension_id}")
        return False
    
    _warmup_shells[extension_id] = shell_id
    print(f"[ACP] warm_up_extension: started shell {shell_id} for {extension_id}")
    
    state = fws_mgr.get_pipe_state(shell_id)
    if not state or not state.process.stdin or not state.process.stdout:
        print(f"[ACP] warm_up_extension: no stdin/stdout for shell {shell_id}")
        return False
    
    session = _manager.get_session(warmup_convo_id)
    if not session:
        print(f"[ACP] warm_up_extension: no session created")
        return False
    
    async def _do_handshake():
        # Send initialize request ONLY - session/new happens when real conversation starts
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True
                },
                "clientInfo": {"name": "agent-log-server", "version": "1.0.0"}
            }
        }
        
        line = json.dumps(init_request, ensure_ascii=False) + "\n"
        _add_to_raw_buffer("out", warmup_convo_id, line.strip())
        state.process.stdin.write(line.encode("utf-8"))
        await state.process.stdin.drain()
        print(f"[ACP] warm_up: sent initialize")
        
        # Wait for initialize response
        while True:
            resp_line = await state.process.stdout.readline()
            if not resp_line:
                print(f"[ACP] warm_up: EOF waiting for initialize response")
                return False
            
            resp_str = resp_line.decode("utf-8", errors="replace").strip()
            if not resp_str:
                continue
            
            _add_to_raw_buffer("in", warmup_convo_id, resp_str)
            print(f"[ACP] warm_up: got line: {resp_str[:100]}")
            
            if resp_str.startswith("{"):
                try:
                    msg = json.loads(resp_str)
                    if msg.get("id") == 1 and "result" in msg:
                        # Initialize succeeded - process is ready to accept session/new
                        session.initialized = True
                        session.ready = True
                        print(f"[ACP] warm_up: initialize complete, process ready")
                        return True
                except json.JSONDecodeError:
                    pass
        
        return False
    
    try:
        success = await asyncio.wait_for(_do_handshake(), timeout=timeout)
        if success:
            ready_event.set()
        return success
    except asyncio.TimeoutError:
        print(f"[ACP] warm_up_extension: timeout after {timeout}s for {extension_id}")
        return False


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """
    Warm up all registered ACP extensions in parallel.
    Returns dict of extension_id -> success.
    """
    if not _manager:
        return {}
    
    results = {}
    tasks = []
    ext_ids = []
    
    for ext in _manager.list_extensions():
        ext_id = ext["id"]
        ext_ids.append(ext_id)
        tasks.append(warm_up_extension(ext_id, timeout=timeout))
    
    if tasks:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for ext_id, outcome in zip(ext_ids, outcomes):
            if isinstance(outcome, Exception):
                print(f"[ACP] warm_up_all: {ext_id} failed with {outcome}")
                results[ext_id] = False
            else:
                results[ext_id] = outcome
    
    return results


def is_extension_ready(extension_id: str) -> bool:
    """Check if an extension has completed warm-up."""
    event = _ready_events.get(extension_id)
    return event.is_set() if event else False


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    """Wait for an extension to be ready."""
    event = _ready_events.get(extension_id)
    if not event:
        # Not warming up yet, start warm-up
        return await warm_up_extension(extension_id, timeout=timeout)
    
    if event.is_set():
        return True
    
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return event.is_set()
    except asyncio.TimeoutError:
        return False
