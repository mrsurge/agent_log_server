from __future__ import annotations

# pyright: reportPrivateUsage=false

import asyncio
import os
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


class AskUserToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        kb_server._ask_user_pending_requests.clear()
        kb_server._ask_user_active_by_requestor.clear()

    def tearDown(self) -> None:
        kb_server._ask_user_pending_requests.clear()
        kb_server._ask_user_active_by_requestor.clear()

    def test_ask_user_explicitly_advertises_non_read_only(self) -> None:
        async def scenario() -> object:
            tools = await kb_server.mcp.list_tools()
            for tool in tools:
                if tool.name == "ask_user":
                    return tool.annotations
            self.fail("ask_user tool was not registered")

        annotations = asyncio.run(scenario())

        self.assertIsNotNone(annotations)
        self.assertIs(getattr(annotations, "readOnlyHint", None), False)

    def test_ask_user_registers_unique_request_id_for_stable_requestor(self) -> None:
        class FakeIpcClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []
                self.active_during_ack = False

            async def call(
                self,
                event: str,
                data: object = None,
                *,
                namespace: str | None = None,
                timeout: float | int | None = None,
            ) -> object:
                del namespace, timeout
                self.calls.append((event, data))
                payload = cast(dict[str, object], data)
                if event == "ask_user_begin":
                    request_id = cast(str, payload["request_id"])
                    waiter = kb_server._ask_user_pending_requests[request_id]
                    response_event: kb_server.ObjectMap = {
                        "request_id": request_id,
                        "requestor_id": "conv-test",
                        "response": {"action": "accept", "answer": "Approve"},
                    }
                    asyncio.get_running_loop().call_soon(
                        waiter.set_result,
                        response_event,
                    )
                    return {"ok": True, "request_id": request_id}
                if event == "ask_user_ack":
                    request_id = cast(str, payload["request_id"])
                    self.active_during_ack = (
                        request_id in kb_server._ask_user_pending_requests
                        and kb_server._ask_user_active_by_requestor.get("conv-test") == request_id
                    )
                return {"ok": True}

        async def scenario() -> tuple[dict[str, object], FakeIpcClient]:
            client = FakeIpcClient()
            with (
                patch.dict(os.environ, {"CONVERSATION_ID": "conv-test"}),
                patch.object(kb_server, "_get_appserver_ipc_sio", return_value=client),
            ):
                result = cast(
                    dict[str, object],
                    await kb_server.ask_user(
                        "Proceed?",
                        ["Approve", "Reject"],
                        False,
                        cast(object, object()),  # type: ignore[arg-type]
                    ),
                )
            return result, client

        result, client = asyncio.run(scenario())
        begin_payload = cast(dict[str, object], client.calls[0][1])
        request_id = cast(str, begin_payload["request_id"])
        self.assertEqual(client.calls[0][0], "ask_user_begin")
        self.assertEqual(begin_payload["requestor_id"], "conv-test")
        self.assertTrue(request_id.startswith("ask_user_"))
        self.assertNotEqual(request_id, "conv-test")
        self.assertEqual(client.calls[1][0], "ask_user_ack")
        self.assertEqual(cast(dict[str, object], client.calls[1][1])["request_id"], request_id)
        self.assertTrue(client.active_during_ack)
        self.assertEqual(result["answer"], "Approve")
        self.assertFalse(kb_server._ask_user_pending_requests)
        self.assertFalse(kb_server._ask_user_active_by_requestor)

    def test_ask_user_rejects_a_second_live_request_for_same_requestor(self) -> None:
        async def scenario() -> dict[str, object]:
            loop = asyncio.get_running_loop()
            pending: asyncio.Future[kb_server.ObjectMap] = loop.create_future()
            kb_server._ask_user_pending_requests["ask_user_existing"] = pending
            kb_server._ask_user_active_by_requestor["conv-test"] = "ask_user_existing"
            with patch.dict(os.environ, {"CONVERSATION_ID": "conv-test"}):
                return await kb_server.ask_user(
                    "Another question?",
                    ["Yes"],
                    False,
                    cast(object, object()),  # type: ignore[arg-type]
                )

        result = asyncio.run(scenario())
        self.assertIs(result["ok"], False)
        self.assertIn("already pending", cast(str, result["error"]))

    def test_parallel_ask_user_calls_reserve_the_requestor_slot_before_ipc(self) -> None:
        class BlockingIpcClient:
            def __init__(self) -> None:
                self.begin_entered = asyncio.Event()
                self.release_begin = asyncio.Event()

            async def call(
                self,
                event: str,
                data: object = None,
                *,
                namespace: str | None = None,
                timeout: float | int | None = None,
            ) -> object:
                del namespace, timeout
                payload = cast(dict[str, object], data)
                if event == "ask_user_begin":
                    self.begin_entered.set()
                    await self.release_begin.wait()
                    request_id = cast(str, payload["request_id"])
                    waiter = kb_server._ask_user_pending_requests[request_id]
                    waiter.set_result({
                        "request_id": request_id,
                        "requestor_id": "conv-test",
                        "status": "cancelled",
                    })
                return {"ok": True}

        async def scenario() -> tuple[dict[str, object], dict[str, object]]:
            client = BlockingIpcClient()
            with (
                patch.dict(os.environ, {"CONVERSATION_ID": "conv-test"}),
                patch.object(kb_server, "_get_appserver_ipc_sio", return_value=client),
            ):
                first_task = asyncio.create_task(kb_server.ask_user(
                    "First question?",
                    ["Yes"],
                    False,
                    cast(object, object()),  # type: ignore[arg-type]
                ))
                await client.begin_entered.wait()
                second = cast(
                    dict[str, object],
                    await kb_server.ask_user(
                        "Second question?",
                        ["Yes"],
                        False,
                        cast(object, object()),  # type: ignore[arg-type]
                    ),
                )
                client.release_begin.set()
                first = cast(dict[str, object], await first_task)
                return first, second

        first, second = asyncio.run(scenario())
        self.assertEqual(first["status"], "cancel")
        self.assertIs(second["ok"], False)
        self.assertIn("already pending", cast(str, second["error"]))
        self.assertFalse(kb_server._ask_user_pending_requests)
        self.assertFalse(kb_server._ask_user_active_by_requestor)

    def test_ask_user_cancellation_during_ack_releases_local_ownership(self) -> None:
        class AckBlockingIpcClient:
            def __init__(self) -> None:
                self.ack_entered = asyncio.Event()

            async def call(
                self,
                event: str,
                data: object = None,
                *,
                namespace: str | None = None,
                timeout: float | int | None = None,
            ) -> object:
                del namespace, timeout
                payload = cast(dict[str, object], data)
                request_id = cast(str, payload["request_id"])
                if event == "ask_user_begin":
                    waiter = kb_server._ask_user_pending_requests[request_id]
                    waiter.set_result({
                        "request_id": request_id,
                        "requestor_id": "conv-test",
                        "response": {"action": "accept", "answer": "Yes"},
                    })
                    return {"ok": True}
                if event == "ask_user_ack":
                    self.ack_entered.set()
                    await asyncio.Event().wait()
                return {"ok": True}

        async def scenario() -> None:
            client = AckBlockingIpcClient()
            with (
                patch.dict(os.environ, {"CONVERSATION_ID": "conv-test"}),
                patch.object(kb_server, "_get_appserver_ipc_sio", return_value=client),
            ):
                task = asyncio.create_task(kb_server.ask_user(
                    "Wait for ack?",
                    ["Yes"],
                    False,
                    cast(object, object()),  # type: ignore[arg-type]
                ))
                await client.ack_entered.wait()
                self.assertTrue(kb_server._ask_user_pending_requests)
                self.assertTrue(kb_server._ask_user_active_by_requestor)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(scenario())
        self.assertFalse(kb_server._ask_user_pending_requests)
        self.assertFalse(kb_server._ask_user_active_by_requestor)


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


class KbWriteContractTests(unittest.TestCase):
    def _run_kb_tool(self, value: object) -> str:
        return asyncio.run(cast(Coroutine[object, object, str], value))

    def _write_kb_root(self, root: Path, body: str) -> None:
        (root / ".agent-pty.toml").write_text(
            '[knowledge]\nfiles = ["one.md"]\n',
            encoding="utf-8",
        )
        (root / "one.md").write_text(body, encoding="utf-8")

    def test_kb_write_heading_creation_normalizes_markdown_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kb_root(root, "# Top\n\nbody\n")
            with patch.object(kb_server, "_current_project_root", return_value=root):
                tool_call = cast(object, kb_server.kb_write(
                    target="one.md",
                    section="Top",
                    mode="create_child",
                    heading_title="Child",
                    content="child body",
                ))
                result = self._run_kb_tool(tool_call)
            text = (root / "one.md").read_text(encoding="utf-8")

        self.assertIn("[kb_write: WRITTEN", result)
        self.assertEqual(text, "# Top\n\nbody\n\n## Child\n\nchild body\n")

    def test_kb_write_heading_creation_can_preserve_raw_spacing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_kb_root(root, "# Top\n\nbody\n")
            with patch.object(kb_server, "_current_project_root", return_value=root):
                tool_call = cast(object, kb_server.kb_write(
                    target="one.md",
                    section="Top",
                    mode="create_child",
                    heading_title="Child",
                    content="child body",
                    spacing="preserve",
                ))
                result = self._run_kb_tool(tool_call)
            text = (root / "one.md").read_text(encoding="utf-8")

        self.assertIn("[kb_write: WRITTEN", result)
        self.assertEqual(text, "# Top\n\nbody\n## Child\nchild body\n")


if __name__ == "__main__":
    unittest.main()
