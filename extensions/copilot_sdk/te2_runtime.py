from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from agent_log_server.te2_runtime import TE2_MCP_SERVER_NAME, build_te2_mcp_url

AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PTY_MCP_SERVER_PATH = _REPO_ROOT / "mcp_agent_pty_server.py"


def build_agent_pty_blocks_local_mcp_server() -> Dict[str, Any]:
    command = sys.executable.strip() if isinstance(sys.executable, str) and sys.executable.strip() else "python3"
    return {
        "type": "local",
        "command": command,
        "args": [str(_AGENT_PTY_MCP_SERVER_PATH)],
        "cwd": str(_REPO_ROOT),
        "tools": ["*"],
    }


def build_copilot_mcp_servers(
    existing_servers: Any,
    *,
    te2_enabled: bool,
    base_url: Optional[str],
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
        merged.setdefault(
            AGENT_PTY_BLOCKS_MCP_SERVER_NAME,
            build_agent_pty_blocks_local_mcp_server(),
        )

    return merged or None
