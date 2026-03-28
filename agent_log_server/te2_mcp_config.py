from __future__ import annotations

from typing import Any, Dict, Optional

TE2_MCP_SERVER_NAME = "te2-mcp"
TE2_APP_ROUTE_PREFIX = "/api/app/file_editor_cm6"
TE2_MCP_ROUTE = f"{TE2_APP_ROUTE_PREFIX}/te2_mcp"
TE2_MCP_STREAMABLE_HTTP_ROUTE = f"{TE2_APP_ROUTE_PREFIX}/te2_mcp_http"


def te2_mcp_integration_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    return isinstance(settings, dict) and settings.get("te2_mcp_integration") is True


def build_te2_mcp_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_ROUTE}"


def build_te2_mcp_streamable_http_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_STREAMABLE_HTTP_ROUTE}/"


def build_codex_thread_config(
    existing_config: Any,
    *,
    te2_enabled: bool,
    base_url: Optional[str],
    force_te2_mcp_entry: bool = False,
) -> Optional[Dict[str, Any]]:
    if existing_config in (None, ""):
        merged: Dict[str, Any] = {}
    elif isinstance(existing_config, dict):
        merged = dict(existing_config)
    else:
        raise ValueError("Codex config must be a JSON object")

    existing_mcp = merged.get("mcp_servers")
    if existing_mcp in (None, ""):
        mcp_servers: Dict[str, Any] = {}
    elif isinstance(existing_mcp, dict):
        mcp_servers = dict(existing_mcp)
    else:
        raise ValueError("Codex config.mcp_servers must be a JSON object")

    if te2_enabled:
        mcp_servers[TE2_MCP_SERVER_NAME] = {
            "url": build_te2_mcp_streamable_http_url(base_url or ""),
        }
    elif force_te2_mcp_entry:
        mcp_servers.pop(TE2_MCP_SERVER_NAME, None)

    if mcp_servers or force_te2_mcp_entry:
        merged["mcp_servers"] = mcp_servers
    else:
        merged.pop("mcp_servers", None)

    return merged or None


__all__ = [
    "TE2_APP_ROUTE_PREFIX",
    "TE2_MCP_ROUTE",
    "TE2_MCP_SERVER_NAME",
    "TE2_MCP_STREAMABLE_HTTP_ROUTE",
    "build_codex_thread_config",
    "build_te2_mcp_streamable_http_url",
    "build_te2_mcp_url",
    "te2_mcp_integration_enabled",
]
