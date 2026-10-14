#!/usr/bin/env python3
"""Test-only SDK contract reference, never used as a runtime fallback."""
import asyncio
import os
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import ToolAnnotations
from agent_log_server_rs import mcp_tools
from agent_log_server_rs.mcp_registry import RequestContext

mcp = FastMCP(name="agent-pty-blocks", instructions="Agent PTY + block store tools (per-conversation).")


async def _ask_user(question: str, choices: list[str] | None = None, allow_freeform: bool = True, ctx: Context[ServerSession, object, object] | None = None) -> dict[str, object]:
    return await mcp_tools.ask_user(question, choices, allow_freeform, RequestContext(str(ctx.request_id)) if ctx is not None else None)


_ask_user.__name__ = "ask_user"

for name, fn in mcp_tools.mcp.tools.items():
    descriptor = mcp_tools.mcp.descriptors[name]
    mcp.add_tool(
        _ask_user if name == "ask_user" else fn,
        name=name,
        description=str(descriptor["description"]),
        annotations=ToolAnnotations(readOnlyHint=False) if name == "ask_user" else None,
    )
for uri, fn in mcp_tools.mcp.resources.items():
    mcp.resource(uri, name="knowledge", title="Knowledge base", description="Configured KB files plus KB tool usage help.", mime_type="text/plain")(fn)


async def _main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "").strip().lower()
    if transport in {"streamable-http", "streamable_http", "http", "sse"}:
        host = os.environ.get("MCP_HTTP_HOST") or os.environ.get("FASTMCP_HOST")
        port_text = os.environ.get("MCP_HTTP_PORT") or os.environ.get("FASTMCP_PORT")
        try:
            port = int(port_text) if port_text is not None else None
        except (ValueError, TypeError):
            port = None
        path = os.environ.get("MCP_STREAMABLE_HTTP_PATH") or os.environ.get("FASTMCP_STREAMABLE_HTTP_PATH")
        if host and host.strip():
            mcp.settings.host = host.strip()
        if isinstance(port, int):
            mcp.settings.port = port
        if path and path.strip():
            mcp.settings.streamable_http_path = path.strip()
        if transport == "sse":
            await mcp.run_sse_async(mount_path=os.environ.get("MCP_MOUNT_PATH") or None)
        else:
            await mcp.run_streamable_http_async()
        return
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())
