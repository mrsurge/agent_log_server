from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from typing import cast

from packaging.requirements import Requirement

ROOT = Path(__file__).absolute().parents[2]


def _table(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _field(value: object, *keys: str) -> object:
    for key in keys:
        value = _table(value)[key]
    return value


def _strings(value: object) -> list[str]:
    assert isinstance(value, list)
    values = cast(list[object], value)
    assert all(isinstance(item, str) for item in values)
    return cast(list[str], values)


class DependencyCleanupTests(unittest.TestCase):
    def test_runtime_metadata_has_no_retired_web_schema_or_mcp_dependencies(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text().splitlines()
        self.assertIn("framework-shells==0.2.65", requirements)
        names = {Requirement(line).name.lower().replace("_", "-") for line in requirements if line.strip() and not line.startswith("#")}
        self.assertFalse(names & {"fastapi", "fastmcp", "mcp", "pydantic", "pydantic-core", "cryptography"})
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        test_requirements = _strings(_field(metadata, "project", "optional-dependencies", "test"))
        self.assertIn("mcp==1.30.0", test_requirements)
        self.assertNotIn("mcp_agent_pty_http_shim", _strings(_field(metadata, "tool", "setuptools", "py-modules")))
        self.assertNotIn("codex-agent", _table(_field(metadata, "project", "scripts")))
        self.assertFalse((ROOT / "mcp_agent_pty_http_shim.py").exists())

    def test_copilot_extension_is_not_registered_or_packaged(self) -> None:
        registry = _table(cast(object, json.loads((ROOT / "extensions/extensions.json").read_text())))
        entries = registry["extensions"]
        assert isinstance(entries, list)
        self.assertEqual([_table(entry)["id"] for entry in cast(list[object], entries)], ["codex-ext"])
        self.assertFalse((ROOT / "extensions/copilot_sdk").exists())
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertFalse(any("copilot" in key for key in _table(_field(metadata, "tool", "setuptools", "package-data"))))

    def test_optional_uvloop_distributions_are_target_exclusive(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
        requirements = [Requirement(value) for value in _strings(_field(metadata, "project", "optional-dependencies", "performance"))]
        runtime = [Requirement(value) for value in (ROOT / "requirements.txt").read_text().splitlines()
                   if value.startswith(("uvloop==", "uvloop-te2=="))]
        self.assertEqual(len(runtime), 2)
        for system, machine, expected in [("linux", "x86_64", ["uvloop"]), ("android", "aarch64", ["uvloop-te2"]), ("win32", "AMD64", []), ("linux", "aarch64", [])]:
            actual = [req.name for req in requirements if req.marker is not None and req.marker.evaluate({"sys_platform": system, "platform_machine": machine})]
            self.assertEqual(actual, expected)
            self.assertEqual([req.name for req in runtime if req.marker is not None and req.marker.evaluate(
                {"sys_platform": system, "platform_machine": machine})], expected)

    def test_native_adapter_import_has_no_python_web_schema_or_mcp_stack(self) -> None:
        code = '''
import importlib.abc, sys
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi','fastmcp','mcp','pydantic','pydantic_core','cryptography'}:
            raise AssertionError('Unexpected native adapter import: ' + fullname)
sys.meta_path.insert(0, Blocker())
import agent_log_server_rs.adapters.extension_adapter
import extensions.codex_ext.client
assert not any(n.split('.')[0] in {'fastapi','fastmcp','mcp','pydantic','pydantic_core','cryptography'} for n in sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stdio_mcp_has_native_registry_not_python_sdk(self) -> None:
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
