from __future__ import annotations

from agent_log_server.te2_runtime import TE2_MCP_SERVER_NAME, build_te2_mcp_streamable_http_url
from agent_log_server.typing_helpers import ObjectMap, coerce_object_map

from .te2_runtime import (
    AGENT_PTY_BLOCKS_MCP_SERVER_NAME,
    build_agent_pty_blocks_local_mcp_server,
)


def _optional_map(value: object) -> ObjectMap:
    return coerce_object_map(value) if isinstance(value, dict) else {}


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def apply_mcp_context(settings: object) -> ObjectMap:
    merged = _optional_map(settings)
    context = _optional_map(merged.get("mcp_context"))
    if not context:
        return merged

    requested_servers = _optional_map(context.get("requested_servers")) or _optional_map(merged.get("mcp_servers"))
    defaults = _optional_map(context.get("defaults"))
    mcp_servers = dict(requested_servers)

    agent_defaults = _optional_map(defaults.get(AGENT_PTY_BLOCKS_MCP_SERVER_NAME))
    if agent_defaults.get("enabled_by_default") is not False:
        agent_cwd = _optional_string(agent_defaults.get("cwd")) or _optional_string(context.get("cwd")) or _optional_string(merged.get("cwd"))
        agent_conversation_id = _optional_string(agent_defaults.get("conversation_id")) or _optional_string(context.get("conversation_id"))
        mcp_servers[AGENT_PTY_BLOCKS_MCP_SERVER_NAME] = build_agent_pty_blocks_local_mcp_server(
            agent_cwd,
            conversation_id=agent_conversation_id,
        )

    te2_defaults = _optional_map(defaults.get(TE2_MCP_SERVER_NAME))
    te2_base_url = _optional_string(te2_defaults.get("base_url")) or _optional_string(merged.get("te2_base_url"))
    if te2_defaults and te2_defaults.get("enabled_by_default") is not False and te2_base_url:
        mcp_servers[TE2_MCP_SERVER_NAME] = {
            "type": "http",
            "url": build_te2_mcp_streamable_http_url(te2_base_url),
            "tools": ["*"],
        }

    if mcp_servers:
        merged["mcp_servers"] = mcp_servers
    else:
        merged.pop("mcp_servers", None)
    return merged
