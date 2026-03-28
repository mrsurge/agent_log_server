from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from agent_log_server.te2_runtime import TE2_MCP_SERVER_NAME, build_te2_mcp_url

AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
_REPO_ROOT = Path(os.path.abspath(__file__)).parents[2]
_AGENT_PTY_MCP_SERVER_PATH = _REPO_ROOT / "mcp_agent_pty_server.py"


def build_agent_pty_blocks_local_mcp_server(cwd: Optional[str] = None) -> Dict[str, Any]:
    command = sys.executable.strip() if isinstance(sys.executable, str) and sys.executable.strip() else "python3"
    server: Dict[str, Any] = {
        "type": "local",
        "command": command,
        "args": [str(_AGENT_PTY_MCP_SERVER_PATH)],
        "tools": ["*"],
    }
    if isinstance(cwd, str) and cwd.strip():
        server["cwd"] = cwd
        server["env"] = {"PWD": cwd}
    return server


def build_copilot_mcp_servers(
    existing_servers: Any,
    *,
    te2_enabled: bool,
    base_url: Optional[str],
    cwd: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if existing_servers in (None, ""):
        merged: Dict[str, Any] = {}
    elif isinstance(existing_servers, dict):
        merged = dict(existing_servers)
    else:
        raise ValueError("MCP Servers must be a JSON object")

    if te2_enabled:
        merged[TE2_MCP_SERVER_NAME] = {
            "type": "sse",
            "url": build_te2_mcp_url(base_url or ""),
            "tools": ["*"],
        }
        merged[AGENT_PTY_BLOCKS_MCP_SERVER_NAME] = build_agent_pty_blocks_local_mcp_server(cwd)

    return merged or None
