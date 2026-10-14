"""Real SDK client qualification in disposable roots; SDK is test-only."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import subprocess

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import ClientNotification, CancelledNotification, CancelledNotificationParams

ROOT = Path(__file__).absolute().parents[2]
PROBE = Path(__file__).absolute().parent / "target/debug/als-mcp-embedding-probe"


async def main():
    with tempfile.TemporaryDirectory(prefix="mcp-proof-", dir=ROOT / ".codex-scratch") as temporary:
        env = dict(os.environ)
        env.update({
            "FRAMEWORK_SHELLS_SECRET": "isolated-proof-not-a-live-secret",
            "FRAMEWORK_SHELLS_BASE_DIR": temporary,
            "CONVERSATION_ID": "isolated-mcp-proof",
            "PWD": temporary,
            "MCP_TRANSPORT": "stdio",
            "ALS_DISABLE_UVLOOP": "1",
            "PYTHONPATH": str(ROOT),
        })
        for key in ("TE2_CACHE_HOME", "TE2_CONFIG_HOME", "TE2_DATA_HOME", "TE2_RUNTIME_HOME"):
            env[key] = str(Path(temporary) / key.lower())
        # Baseline discovery only: no production tool is executed.
        import sys
        baseline = StdioServerParameters(command=sys.executable, args=[str(ROOT / "agent_log_server_rs/tests/mcp_sdk_reference.py")], env=env, cwd=temporary)
        async with stdio_client(baseline) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                resources = await session.list_resources()
                assert len(tools.tools) == 38
                assert len(resources.resources) == 1
                catalog = {
                    "tools": [tool.model_dump(mode="json", by_alias=True) for tool in tools.tools],
                    "resources": [resource.model_dump(mode="json", by_alias=True) for resource in resources.resources],
                }
                expected = json.loads((ROOT / "agent_log_server_rs/mcp_catalog.json").read_text())
                assert catalog == expected, "Retained SDK catalog changed after extraction"
                # Generated qualification artifact, not a live registry.
                target = Path(__file__).absolute().parent / "target/baseline-catalog.json"
                target.write_text(json.dumps(catalog, indent=2) + "\n")
                await session.send_ping()
        parameters = StdioServerParameters(command=str(PROBE), env=env, cwd=temporary)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 3
                echo = await session.call_tool("probe_echo", {"value": "direct PyO3 objects"})
                assert echo.content[0].text == "direct PyO3 objects"
                request_id = session._request_id  # SDK test-only correlation; no production dependency.
                waiting = asyncio.create_task(session.call_tool("probe_wait", {"milliseconds": 5000}))
                await asyncio.sleep(0.02)
                # Prove both requests run on the persistent Python loop concurrently.
                for _ in range(100):
                    state = await session.call_tool("probe_state", {})
                    if "active=1" in state.content[0].text:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("Python wait never became active")
                quick = await asyncio.wait_for(session.call_tool("probe_echo", {"value": "not blocked"}), timeout=1)
                assert quick.content[0].text == "not blocked"
                await session.send_notification(ClientNotification(CancelledNotification(
                    params=CancelledNotificationParams(requestId=request_id, reason="embedding proof cancellation")
                )))
                # Cancellation permits the server to omit a reply; stop the
                # client's local waiter separately from the wire notification.
                waiting.cancel()
                try:
                    await waiting
                except asyncio.CancelledError:
                    pass
                for _ in range(100):
                    state = await session.call_tool("probe_state", {})
                    if state.content[0].text == "active=0;cancelled=1":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("MCP cancellation did not clean Python task")
                try:
                    invalid = await session.call_tool("probe_wait", {"milliseconds": 5001})
                except McpError as error:
                    assert error.error.code == -32602
                else:
                    assert invalid.isError
                await session.send_ping()
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "eof-proof", "version": "1"}
        }}
        eof = subprocess.run([str(PROBE)], input=json.dumps(initialize) + "\n", capture_output=True, text=True, env=env, cwd=temporary, timeout=5)
        assert eof.returncode == 0, eof.stderr
        assert json.loads(eof.stdout.splitlines()[0])["id"] == 1
        print("Baseline: 38 tools/1 resource. Embedding: echo, concurrent wait, cancellation, validation, ping and clean EOF passed.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=30))
