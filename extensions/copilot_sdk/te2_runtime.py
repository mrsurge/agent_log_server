from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, TypeAlias, cast

ObjectMap: TypeAlias = dict[str, object]

TE2_MCP_SERVER_NAME = "te2-mcp"
TE2_MCP_STREAMABLE_HTTP_ROUTE = "/te2_mcp_http"
AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
_REPO_ROOT = Path(os.path.abspath(__file__)).parents[2]
_AGENT_PTY_MCP_SERVER_PATH = _REPO_ROOT / "mcp_agent_pty_server.py"


def coerce_object_map(value: dict[object, object]) -> ObjectMap:
    return {str(key): item for key, item in value.items()}


def build_te2_mcp_streamable_http_url(base_url: str) -> str:
    if not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_STREAMABLE_HTTP_ROUTE}"


def build_agent_pty_blocks_local_mcp_server(
    cwd: Optional[str] = None,
    conversation_id: Optional[str] = None,
    appserver_origin: Optional[str] = None,
) -> ObjectMap:
    command = sys.executable.strip() or "python3"
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
        merged = coerce_object_map(cast(dict[object, object], existing_servers))
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
