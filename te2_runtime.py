from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

TE2_MCP_SERVER_NAME = "te2-mcp"
TE2_APP_ROUTE_PREFIX = "/api/app/file_editor_cm6"
TE2_MCP_ROUTE = f"{TE2_APP_ROUTE_PREFIX}/te2_mcp"
TE2_MCP_STREAMABLE_HTTP_ROUTE = f"{TE2_APP_ROUTE_PREFIX}/te2_mcp_http"
_DEFAULT_TEMPLATE_PATH = (
    Path(__file__).resolve().parent
    / "DEVELOPER_MESSAGE_TEMPLATE.md"
)


def te2_mcp_integration_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    return isinstance(settings, dict) and settings.get("te2_mcp_integration") is True


def _template_path(template_path: Optional[str] = None) -> Path:
    raw = template_path
    if not raw:
        raw = os.environ.get("TE2_DEVELOPER_MESSAGE_TEMPLATE_PATH")
    if isinstance(raw, str) and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return _DEFAULT_TEMPLATE_PATH


@lru_cache(maxsize=8)
def load_te2_developer_message_template(template_path: Optional[str] = None) -> str:
    path = _template_path(template_path)
    if not path.exists():
        raise FileNotFoundError(f"TE2 developer message template not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"TE2 developer message template is empty: {path}")
    return text


def build_te2_mcp_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_ROUTE}"


def build_te2_mcp_streamable_http_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("TE2 base URL is required")
    return f"{base_url.rstrip('/')}{TE2_MCP_STREAMABLE_HTTP_ROUTE}/"


def build_effective_developer_instructions(
    user_instructions: Any,
    *,
    te2_enabled: bool,
    template_path: Optional[str] = None,
) -> Optional[str]:
    user_text = user_instructions.strip() if isinstance(user_instructions, str) else ""
    if not te2_enabled:
        return user_text or None
    template = load_te2_developer_message_template(template_path)
    if not user_text:
        return template
    if template in user_text:
        return user_text
    return f"{template}\n\n{user_text}"


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

    return merged or None


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
