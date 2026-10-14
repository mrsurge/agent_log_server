from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest

from packaging.requirements import Requirement

ROOT = Path(__file__).absolute().parents[2]


class DependencyCleanupTests(unittest.TestCase):
    def test_metadata_retains_real_mcp_dependency_not_fastapi_or_http_shim(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text().splitlines()
        self.assertFalse(any(line.startswith("fastapi") for line in requirements))
        self.assertIn("fastmcp==3.4.7", requirements)
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertNotIn("mcp_agent_pty_http_shim", metadata["tool"]["setuptools"]["py-modules"])
        self.assertNotIn("codex-agent", metadata["project"]["scripts"])
        self.assertFalse((ROOT / "mcp_agent_pty_http_shim.py").exists())

    def test_optional_uvloop_distributions_are_target_exclusive(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = [Requirement(value) for value in metadata["project"]["optional-dependencies"]["performance"]]
        for system, machine, expected in [("linux", "x86_64", ["uvloop"]), ("android", "aarch64", ["uvloop-te2"]), ("win32", "AMD64", []), ("linux", "aarch64", [])]:
            actual = [req.name for req in requirements if req.marker is not None and req.marker.evaluate({"sys_platform": system, "platform_machine": machine})]
            self.assertEqual(actual, expected)

    def test_native_adapter_import_has_no_python_web_schema_or_mcp_stack(self) -> None:
        code = '''
import importlib.abc, sys
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi','fastmcp','mcp','pydantic','pydantic_core','cryptography'}:
            raise AssertionError('Unexpected native adapter import: ' + fullname)
sys.meta_path.insert(0, Blocker())
import agent_log_server_rs.adapters.extension_adapter
assert not any(n.split('.')[0] in {'fastapi','fastmcp','mcp','pydantic','pydantic_core','cryptography'} for n in sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stdio_mcp_still_has_real_sdk_contract_pending_migration(self) -> None:
        wrapper = ast.parse((ROOT / "mcp_agent_pty_server.py").read_text())
        imports = [node.module for node in ast.walk(wrapper) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("mcp.server.fastmcp", imports)
        tree = ast.parse((ROOT / "agent_log_server_rs/mcp_tools.py").read_text())
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("mcp.server.fastmcp", imports)
        tools = resources = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "mcp":
                        tools += decorator.func.attr == "tool"
                        resources += decorator.func.attr == "resource"
        self.assertEqual((tools, resources), (38, 1))

    def test_active_loop_boundaries_use_shared_factory(self) -> None:
        adapter = (ROOT / "agent_log_server_rs/adapters/extension_adapter.py").read_text()
        mcp = (ROOT / "mcp_agent_pty_server.py").read_text()
        cli = (ROOT / "agent_log_server_rs/extension_cli.py").read_text()
        self.assertIn("return run(amain())", adapter)
        self.assertIn("from agent_log_server_rs.mcp_bootstrap import main", mcp)
        self.assertNotIn("from agent_log_server_rs.asyncio_runtime", mcp)
        self.assertIn("run(ext_loader.install_extension_dependencies(extension_id))", cli)
