from __future__ import annotations

# pyright: reportPrivateUsage=false

import unittest
from typing import cast

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


if __name__ == "__main__":
    unittest.main()
