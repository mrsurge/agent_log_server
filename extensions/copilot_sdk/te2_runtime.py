from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from agent_log_server.te2_runtime import TE2_MCP_SERVER_NAME, build_te2_mcp_streamable_http_url
from agent_log_server.typing_helpers import ObjectMap, coerce_object_map

AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
_REPO_ROOT = Path(os.path.abspath(__file__)).parents[2]
_AGENT_PTY_MCP_SERVER_PATH = _REPO_ROOT / "mcp_agent_pty_server.py"


def build_agent_pty_blocks_local_mcp_server(
    cwd: Optional[str] = None,
    conversation_id: Optional[str] = None,
    appserver_origin: Optional[str] = None,
) -> ObjectMap:
    command = sys.executable.strip() if isinstance(sys.executable, str) and sys.executable.strip() else "python3"
    server: ObjectMap = {
        "type": "local",
        "command": command,
        "args": [str(_AGENT_PTY_MCP_SERVER_PATH)],
        "tools": ["*"],
    }
    env: dict[str, str] = {}
    if isinstance(cwd, str) and cwd.strip():
        env["PWD"] = cwd
        server["cwd"] = cwd
    if isinstance(conversation_id, str) and conversation_id.strip():
        env["CONVERSATION_ID"] = conversation_id.strip()
    if isinstance(appserver_origin, str) and appserver_origin.strip():
        env["AGENT_LOG_SERVER_ORIGIN"] = appserver_origin.strip()
    if env:
        server["env"] = env
    return server


def build_copilot_mcp_servers(
    existing_servers: object,
    *,
    te2_enabled: bool,
    base_url: Optional[str],
    cwd: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Optional[ObjectMap]:
    if existing_servers in (None, ""):
        merged: ObjectMap = {}
    elif isinstance(existing_servers, dict):
        merged = coerce_object_map(existing_servers)
    else:
        raise ValueError("MCP Servers must be a JSON object")

    if te2_enabled:
        merged[TE2_MCP_SERVER_NAME] = {
            "type": "http",
            "url": build_te2_mcp_streamable_http_url(base_url or ""),
            "tools": ["*"],
        }
        merged[AGENT_PTY_BLOCKS_MCP_SERVER_NAME] = build_agent_pty_blocks_local_mcp_server(cwd, conversation_id=conversation_id)

    return merged or None
