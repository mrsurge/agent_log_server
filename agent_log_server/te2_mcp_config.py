from __future__ import annotations

from .typing_helpers import ObjectMap, coerce_object_map

TE2_MCP_SERVER_NAME = "te2-mcp"
TE2_APP_ROUTE_PREFIX = ""
TE2_MCP_ROUTE = "/te2_mcp"
TE2_MCP_STREAMABLE_HTTP_ROUTE = "/te2_mcp_http"


def te2_mcp_integration_enabled(settings: object) -> bool:
    return isinstance(settings, dict) and settings.get("te2_mcp_integration") is True


def build_te2_mcp_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_ROUTE}"


def build_te2_mcp_streamable_http_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_STREAMABLE_HTTP_ROUTE}"


def build_codex_thread_config(
    existing_config: object,
    *,
    te2_enabled: bool,
    base_url: str | None,
    force_te2_mcp_entry: bool = False,
) -> ObjectMap | None:
    if existing_config in (None, ""):
        merged: ObjectMap = {}
    elif isinstance(existing_config, dict):
        merged = coerce_object_map(existing_config)
    else:
        raise ValueError("Codex config must be a JSON object")

    existing_mcp = merged.get("mcp_servers")
    if existing_mcp in (None, ""):
        mcp_servers: ObjectMap = {}
    elif isinstance(existing_mcp, dict):
        mcp_servers = coerce_object_map(existing_mcp)
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
