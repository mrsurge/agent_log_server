from __future__ import annotations

# pyright: reportPrivateUsage=false

import asyncio
import tempfile
import unittest
from collections.abc import Coroutine
from pathlib import Path
from typing import cast
from unittest.mock import patch

import mcp_agent_pty_server as kb_server
from als_deprecated.markdown_sections import SectionNode, parse_markdown
from extensions.codex_ext.router import _shell_command_to_search_spec


class CodexSearchCardContractTests(unittest.TestCase):
    def test_rg_multi_target_command_becomes_search_spec(self) -> None:
        spec = _shell_command_to_search_spec("rg needle src/a.py src/b.py", "/repo")

        self.assertIsNotNone(spec)
        assert spec is not None
        spec_map = spec
        arguments = cast(dict[str, object], spec_map["arguments"])
        self.assertEqual(spec_map["mode"], "rg")
        self.assertEqual(spec_map["path"], "/repo")
        self.assertEqual(spec_map["pattern"], "needle")
        self.assertEqual(arguments["targets"], ["/repo/src/a.py", "/repo/src/b.py"])


class KbSelectorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = "# Top\n\nintro\n\n## Child\n\nbody\n"
        self.nodes = parse_markdown(self.text)
        self.total_lines = len(self.text.splitlines())

    def test_kb_read_resolution_does_not_default_to_file_root(self) -> None:
        result = kb_server._resolve_section_list_or_error(
            self.nodes,
            None,
            "",
            total_lines=self.total_lines,
            allow_root=True,
        )

        self.assertIsInstance(result, dict)
        result_map = cast(dict[str, object], result)
        self.assertEqual(result_map["error"], "InvalidParameter")

    def test_kb_read_resolution_accepts_explicit_file_root(self) -> None:
        result = kb_server._resolve_section_list_or_error(
            self.nodes,
            "",
            "",
            total_lines=self.total_lines,
            allow_root=True,
        )

        self.assertIsInstance(result, list)
        result_nodes = cast(list[SectionNode], result)
        self.assertEqual(result_nodes[0].depth, 0)

    def test_kb_read_resolution_accepts_section_alias(self) -> None:
        result = kb_server._resolve_section_list_or_error(
            self.nodes,
            None,
            "",
            total_lines=self.total_lines,
            allow_root=True,
            section_value="Child",
        )

        self.assertIsInstance(result, list)
        result_nodes = cast(list[SectionNode], result)
        self.assertEqual(result_nodes[0].title, "Child")

    def test_kb_read_resolution_accepts_schema_number(self) -> None:
        result = kb_server._resolve_section_list_or_error(
            self.nodes,
            "2",
            "",
            total_lines=self.total_lines,
            allow_root=True,
        )

        self.assertIsInstance(result, list)
        result_nodes = cast(list[SectionNode], result)
        self.assertEqual(result_nodes[0].title, "Child")


class KbSearchContractTests(unittest.TestCase):
    def _write_kb_root(self, root: Path) -> None:
        (root / ".agent-pty.toml").write_text(
            '[knowledge]\nfiles = ["one.md", "two.md"]\n',
            encoding="utf-8",
        )
        (root / "one.md").write_text("# One\n\nneedle one\n", encoding="utf-8")
        (root / "two.md").write_text("# Two\n\n## Second\n\nneedle two\n", encoding="utf-8")

    def _run_kb_tool(self, value: object) -> str:
        return asyncio.run(cast(Coroutine[object, object, str], value))

    def test_resolve_search_files_defaults_to_all_configured_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kb_root(root)
            with patch.object(kb_server, "_current_project_root", return_value=root):
                result = kb_server._kb_resolve_search_files(None, None)

        self.assertIsInstance(result, list)
        paths = [path.name for path in cast(list[Path], result)]
        self.assertEqual(paths, ["one.md", "two.md"])

    def test_kb_search_without_target_searches_all_files_with_global_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kb_root(root)
            with patch.object(kb_server, "_current_project_root", return_value=root):
                tool_call = cast(object, kb_server.kb_search(query="needle", max_hits=1))
                result = self._run_kb_tool(tool_call)

        self.assertIn("[search: all KB files", result)
        self.assertIn("[1] file one.md section", result)
        self.assertIn("needle one", result)
        self.assertNotIn("needle two", result)

    def test_kb_search_headers_without_target_searches_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kb_root(root)
            with patch.object(kb_server, "_current_project_root", return_value=root):
                tool_call = cast(object, kb_server.kb_search_headers(query="o", max_hits=5))
                result = self._run_kb_tool(tool_call)

        self.assertIn("[search_headers: all KB files", result)
        self.assertIn("one.md: 001 H1 L1 # One", result)
        self.assertIn("two.md: 001 H1 L1 # Two", result)


if __name__ == "__main__":
    unittest.main()
