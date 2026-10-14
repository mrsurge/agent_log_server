"""Read-only real-catalog parity in disposable directories, no active owners."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

ROOT = Path(__file__).absolute().parents[2]
BIN = Path(__file__).absolute().parent / "target/debug/als-mcp"


def strip_nulls(value):
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


async def main():
    with tempfile.TemporaryDirectory(prefix="real-mcp-", dir=ROOT / ".codex-scratch") as tmp:
        root = Path(tmp)
        (root / "knowledge.md").write_text("# Disposable knowledge\n\n## Details\nOnly test data.\n")
        (root / ".agent-pty.toml").write_text('[knowledge]\nfiles = ["knowledge.md"]\n')
        env = dict(os.environ)
        env.update({
            "FRAMEWORK_SHELLS_SECRET": "disposable-native-mcp-proof",
            "FRAMEWORK_SHELLS_BASE_DIR": tmp,
            "CONVERSATION_ID": "disposable-real-tools",
            "PWD": tmp,
            "MCP_TRANSPORT": "stdio",
            "ALS_MCP_PROBE_PYTHON_ROOT": str(ROOT),
            "ALS_DISABLE_UVLOOP": "1",
        })
        for key in ("TE2_CACHE_HOME", "TE2_CONFIG_HOME", "TE2_DATA_HOME", "TE2_RUNTIME_HOME"):
            env[key] = str(root / key.lower())
        # Validate unchanged client invocation through the new exec bootstrap.
        parameters = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_agent_pty_server.py")], env=env, cwd=tmp)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                resources = await session.list_resources()
                actual = {
                    "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools.tools],
                    "resources": [r.model_dump(mode="json", by_alias=True) for r in resources.resources],
                }
                expected = json.loads((ROOT / "agent_log_server_rs/mcp_catalog.json").read_text())
                assert strip_nulls(actual) == strip_nulls(expected)
                result = await session.call_tool("conv_id", {})
                assert result.structuredContent == {"ok": True, "conversation_id": "disposable-real-tools"}
                ping = await session.call_tool("ping", {})
                assert ping.structuredContent["ok"] is True
                # Required-field validation must not invoke IPC or mutate anything.
                invalid = await session.call_tool("ask_user", {})
                assert invalid.isError
                help_result = await session.call_tool("kb_help", {})
                assert help_result.structuredContent["result"] == help_result.content[0].text
                resource = await session.read_resource("kb://knowledge")
                assert "kb_help" in resource.contents[0].text
                assert "knowledge.md" in resource.contents[0].text
                try:
                    await session.call_tool("not-a-real-tool", {})
                except McpError:
                    pass
                else:
                    raise AssertionError("Unknown tool was not rejected")
                await session.send_ping()
        print("Native real catalog: 38 tools/one resource; discovery, read-only calls, validation and resource read passed.")


if __name__ == "__main__":
    # First source build is intentionally visible and may take minutes.
    asyncio.run(asyncio.wait_for(main(), timeout=900))
