from __future__ import annotations

from typing import Dict

ObjectMap = Dict[str, object]

TE2_MCP_SERVER_NAME = "te2-mcp"
TE2_MCP_STREAMABLE_HTTP_ROUTE = "/te2_mcp_http"
AGENT_PTY_BLOCKS_MCP_SERVER_NAME = "agent-pty-blocks"
CODEX_HIGH_CONTEXT_MODEL_CONTEXT_WINDOW = 630000
CODEX_HIGH_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT = 580000
_EAGER_LOAD_TOOLS_KEY = "eager_load_tools"
_APPSERVER_ORIGIN_KEY = "appserver_origin"
_CODEX_EAGER_MCP_FEATURES: ObjectMap = {
    "tool_search": False,
    "tool_search_always_defer_mcp_tools": False,
}


def _optional_map(value: object) -> ObjectMap:
    return {str(key): item for key, item in value.items()} if isinstance(value, dict) else {}


def _optional_string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def te2_mcp_integration_enabled(settings: object) -> bool:
    return isinstance(settings, dict) and settings.get("te2_mcp_integration") is True


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
    enable_high_context_400k: bool = False,
) -> ObjectMap | None:
    if existing_config in (None, ""):
        merged: ObjectMap = {}
    elif isinstance(existing_config, dict):
        merged = _optional_map(existing_config)
    else:
        raise ValueError("Codex config must be a JSON object")

    existing_mcp = merged.get("mcp_servers")
    if existing_mcp in (None, ""):
        mcp_servers: ObjectMap = {}
    elif isinstance(existing_mcp, dict):
        mcp_servers = _optional_map(existing_mcp)
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

    if enable_high_context_400k:
        merged["model_context_window"] = CODEX_HIGH_CONTEXT_MODEL_CONTEXT_WINDOW
        merged["model_auto_compact_token_limit"] = CODEX_HIGH_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT
    else:
        merged.pop("model_context_window", None)
        merged.pop("model_auto_compact_token_limit", None)

    return merged or None


def _wants_eager_tools(value: object) -> bool:
    return _optional_map(value).get(_EAGER_LOAD_TOOLS_KEY) is True


def _any_eager_mcp_server(*server_maps: ObjectMap) -> bool:
    for server_map in server_maps:
        for server in server_map.values():
            if _wants_eager_tools(server):
                return True
    return False


def _force_codex_eager_mcp_exposure(config: ObjectMap) -> None:
    features = _optional_map(config.get("features"))
    features.update(_CODEX_EAGER_MCP_FEATURES)
    config["features"] = features


def _strip_contract_intents(mcp_servers: ObjectMap) -> ObjectMap:
    stripped: ObjectMap = {}
    for name, server in mcp_servers.items():
        server_map = _optional_map(server)
        if server_map:
            server_map.pop(_EAGER_LOAD_TOOLS_KEY, None)
            server_map.pop(_APPSERVER_ORIGIN_KEY, None)
            stripped[name] = server_map
        else:
            stripped[name] = server
    return stripped


def apply_mcp_context(
    existing_config: object,
    settings: object,
    *,
    force_te2_mcp_entry: bool = False,
    enable_high_context_400k: bool = False,
) -> ObjectMap | None:
    merged = build_codex_thread_config(
        existing_config,
        te2_enabled=False,
        base_url=None,
        force_te2_mcp_entry=force_te2_mcp_entry,
        enable_high_context_400k=enable_high_context_400k,
    )
    config = _optional_map(merged)
    context = _optional_map(_optional_map(settings).get("mcp_context"))
    if not context:
        return config or None

    requested_servers = _optional_map(context.get("requested_servers"))
    defaults = _optional_map(context.get("defaults"))
    existing_mcp = _optional_map(config.get("mcp_servers"))
    mcp_servers: ObjectMap = {**existing_mcp, **requested_servers}
    eager_mcp_tools = _any_eager_mcp_server(defaults, requested_servers)

    agent_defaults = _optional_map(defaults.get(AGENT_PTY_BLOCKS_MCP_SERVER_NAME))
    if agent_defaults.get("enabled_by_default") is not False:
        from . import runtime_protocol as runtime

        agent_server = runtime._build_agent_pty_blocks_mcp_server(
            cwd=_optional_string(agent_defaults.get("cwd"))
            or _optional_string(context.get("cwd"))
            or _optional_map(settings).get("cwd"),
            existing_server=mcp_servers.get(AGENT_PTY_BLOCKS_MCP_SERVER_NAME),
            conversation_id=_optional_string(agent_defaults.get("conversation_id"))
            or _optional_string(context.get("conversation_id")),
            appserver_origin=_optional_string(agent_defaults.get("appserver_origin"))
            or _optional_string(context.get("appserver_origin")),
        )
        if agent_server is not None:
            mcp_servers[AGENT_PTY_BLOCKS_MCP_SERVER_NAME] = agent_server
            eager_mcp_tools = eager_mcp_tools or _wants_eager_tools(agent_defaults)

    te2_defaults = _optional_map(defaults.get(TE2_MCP_SERVER_NAME))
    te2_base_url = _optional_string(te2_defaults.get("base_url")) or _optional_string(
        _optional_map(settings).get("te2_base_url")
    )
    if te2_defaults and te2_defaults.get("enabled_by_default") is not False and te2_base_url:
        mcp_servers[TE2_MCP_SERVER_NAME] = {
            "url": build_te2_mcp_streamable_http_url(te2_base_url),
        }
    elif force_te2_mcp_entry:
        mcp_servers.pop(TE2_MCP_SERVER_NAME, None)

    if mcp_servers or force_te2_mcp_entry:
        if eager_mcp_tools:
            _force_codex_eager_mcp_exposure(config)
        mcp_servers = _strip_contract_intents(mcp_servers)
        config["mcp_servers"] = mcp_servers
    else:
        config.pop("mcp_servers", None)

    return config or None
