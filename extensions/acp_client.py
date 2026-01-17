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
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable
from dataclasses import dataclass, field
import acp


@dataclass
class ACPSession:
    """Tracks an active ACP session."""
    conversation_id: str  # Our conversation ID
    session_id: Optional[str] = None  # ACP session ID (assigned after session/new)
    extension_id: str = ""
    shell_id: Optional[str] = None  # framework_shells shell ID
    connection: Optional[Any] = None  # acp ClientSideConnection
    initialized: bool = False


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
    
    @classmethod
    def from_manifest(cls, manifest: Dict[str, Any], extensions_dir: Path) -> "ACPExtension":
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
            
            manifest_path = self.extensions_dir / ext_info["path"] / "manifest.json"
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text())
                extension = ACPExtension.from_manifest(manifest, self.extensions_dir)
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
        
        try:
            shell = await orch.start_from_ref(
                f"{spec_path}#{shell_name}",
                base_dir=spec_path.parent,
                ctx={"CWD": cwd, "CONVERSATION_ID": conversation_id},
                label=f"acp:{extension_id}:{conversation_id[:8]}",
                wait_ready=False,
            )
            
            # Create session record
            session = ACPSession(
                conversation_id=conversation_id,
                extension_id=extension_id,
                shell_id=shell.id,
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


def get_acp_manager() -> Optional[ACPManager]:
    return _manager


def init_acp_manager(extensions_dir: Path, server_root: Path) -> ACPManager:
    global _manager
    _manager = ACPManager(extensions_dir, server_root)
    _manager.load_extensions()
    return _manager
