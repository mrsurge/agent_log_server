"""Read-only installed payload/MCP qualification in a disposable acceptance root.

Invoke with the fresh venv's python -I; never run against a shared harness root.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    from agent_log_server_rs import private_runtime
    import agent_log_server_rs
    package = Path(agent_log_server_rs.__file__).absolute().parent
    assert package.is_relative_to(Path(sys.prefix)), "Imported source checkout, not installed wheel"
    manifest = private_runtime.verify(package)
    assert manifest is not None
    python = package / private_runtime.DIRECTORY / "python/bin/python3.14"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "ALS_", "FRAMEWORK_SHELLS_")) and key not in {"VIRTUAL_ENV", "LD_PRELOAD", "LD_LIBRARY_PATH"}}
    env.update({"HOME": str(root / "home"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
        "FRAMEWORK_SHELLS_SECRET": "isolated-wheel-qualification",
        "FRAMEWORK_SHELLS_BASE_DIR": str(root / "fws"), "MCP_TRANSPORT": "stdio"})
    probe_code = (
        "import sys,json,subprocess;sys.path[:0]=sys.argv[1:3];"
        "from agent_log_server_rs import mcp_bootstrap as b;"
        "r=subprocess.run([sys.argv[3],'--runtime-info'],env=b.runtime_env(),capture_output=True,text=True,check=True);"
        "print(json.dumps({'python':sys.version,'prefix':sys.prefix,'native':json.loads(r.stdout)}))"
    )
    identity = subprocess.run([str(python), "-I", "-S", "-B", "-c", probe_code,
        str(python.parent.parent / "lib/python3.14/site-packages"), str(package.parent), str(package / "bin/als-mcp")],
        env=env, cwd=root, capture_output=True, text=True, timeout=30, check=True)
    retired = {"fastapi", "fastmcp", "mcp", "pydantic", "pydantic-core", "cryptography", "watchfiles"}
    outer = {dist.metadata["Name"].lower().replace("_", "-") for dist in importlib.metadata.distributions()}
    private = {dist.metadata["Name"].lower().replace("_", "-") for dist in importlib.metadata.distributions(path=[str(python.parent.parent / "lib/python3.14/site-packages")])}
    assert not retired.intersection(outer | private), retired.intersection(outer | private)
    assert not {"jinja2", "fastcore", "httpx"}.intersection(private)
    with tempfile.TemporaryFile(mode="w+t") as errors:
        process = subprocess.Popen([sys.executable, "-I", "-B", "-m", "mcp_agent_pty_server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, env=env, cwd=root, text=True, bufsize=1)
        assert process.stdin is not None and process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(payload: dict[str, object]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

        def reply(request_id: int) -> dict[str, object]:
            assert process.stdout is not None
            for _ in range(20):
                if not selector.select(timeout=30):
                    raise RuntimeError("MCP response timeout")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("MCP exited before response")
                value = json.loads(line)
                if value.get("id") == request_id:
                    assert "error" not in value, value
                    return value["result"]
            raise RuntimeError("Unexpected MCP notification flood")

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "isolated-wheel-acceptance", "version": "1"}}})
            reply(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = reply(2)["tools"]
            assert len(tools) == 38, len(tools)
            send({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
            resources = reply(3)["resources"]
            assert len(resources) == 1, len(resources)
            maps = Path(f"/proc/{process.pid}/maps").read_text()
            library = str(package / private_runtime.DIRECTORY / "python/lib/libpython3.14.so.1.0")
            assert library in maps, "Native MCP did not load its bundled libpython"
            process.stdin.close()
            assert process.wait(timeout=15) == 0
        except BaseException:
            errors.seek(0)
            print(errors.read(), file=sys.stderr)
            raise
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    print(json.dumps({"outerPython": sys.version.split()[0], "privateRuntime": json.loads(identity.stdout),
        "tools": len(tools), "resources": len(resources), "bundledLibpythonLoaded": True,
        "retiredDependencies": [], "package": str(package)}, indent=2))


if __name__ == "__main__":
    main()
