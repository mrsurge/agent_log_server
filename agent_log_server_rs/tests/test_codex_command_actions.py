from __future__ import annotations

import unittest
from typing import cast

from extensions.codex_ext.router import CodexEventRouter, ObjectDict
from extensions.codex_ext.runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol


class _Protocol:
    def notification_spec(self, label: str) -> ProtocolSemanticSpec | None:
        if label in {"item/started", "item/completed"}:
            return ProtocolSemanticSpec(name=label, category="item", subject="item",
                                        phase=label.rsplit("/", 1)[1], properties=("item",))
        if label == "item/commandexecution/outputdelta":
            return ProtocolSemanticSpec(name=label, category="item", subject="commandexecution",
                                        phase="outputdelta", properties=("itemId", "delta"))
        return None

    def event_spec(self, event_type: str | None) -> ProtocolSemanticSpec | None:
        return None

    def server_request_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return None


class CommandActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CodexEventRouter()
        self.protocol = cast(RuntimeProtocol, _Protocol())

    def route(self, phase: str, item: ObjectDict) -> ObjectDict:
        return self.router.route_event(self.protocol, label=f"item/{phase}", payload={"item": item},
                                       thread_id="thread", turn_id="turn")

    def item(self, actions: object, command: object = "opaque reader invocation") -> ObjectDict:
        return {"id": "command", "type": "commandExecution", "command": command,
                "cwd": "/project", "commandActions": actions}

    def read(self, path: str = "file.py") -> ObjectDict:
        return {"type": "read", "name": "file.py", "path": path, "command": "opaque read"}

    def finish(self, item: ObjectDict, output: str = "hello\n", **fields: object) -> ObjectDict:
        return self.route("completed", {**item, "status": "completed", "aggregatedOutput": output, **fields})

    def mirror(self, result: ObjectDict, role: str) -> ObjectDict:
        events = cast(list[ObjectDict], result["events"])
        entries = cast(list[ObjectDict], result["transcript_entries"])
        event = next(value for value in events if value.get("type") == role)
        record = next(value for value in entries if value.get("role") == role)
        for key, value in event.items():
            if key != "type":
                self.assertEqual(record.get(key), value, f"live/replay mismatch: {key}")
        return event

    def test_semantic_read_is_independent_of_shell_wrapper(self) -> None:
        for command in ("/bin/sh -l -c 'opaque read'", "/bin/bash -lc 'opaque read'",
                        ["/data/data/com.termux/files/usr/bin/sh", "-l", "-c", "opaque read"]):
            with self.subTest(command=command):
                self.setUp()
                item = self.item([self.read()], command)
                started = self.route("started", item)
                self.assertFalse(any(event.get("type") == "shell_begin"
                                     for event in cast(list[ObjectDict], started["events"])))
                event = self.mirror(self.finish(item), "view")
                self.assertEqual(event["path"], "/project/file.py")
                self.assertEqual(event["content"], "hello\n")
                self.assertNotIn("lines", event, "semantic Read alone cannot imply line 1")

    def test_numbered_read_output_keeps_real_source_lines(self) -> None:
        item = self.item([self.read("/elsewhere/file.py")])
        self.route("started", item)
        event = self.mirror(self.finish(item, "101: hello\n102: world\n"), "view")
        self.assertEqual(event["lines"], [{"line_no": 101, "content": "hello"},
                                          {"line_no": 102, "content": "world"}])

    def test_semantic_read_retains_requested_range_without_numbered_output(self) -> None:
        for command in ("sed -n '323,350p' file.py", "nl -ba file.py | sed -n '323,350p'"):
            with self.subTest(command=command):
                self.setUp()
                action = {**self.read(), "command": command}
                item = self.item([action], command)
                self.route("started", item)
                event = self.mirror(self.finish(item, "unnumbered output\n"), "view")
                self.assertEqual(event["view_range"], [323, 350])
                self.assertEqual(event["title"], "file.py  Lines 323–350")
                self.assertNotIn("lines", event)

    def test_semantic_read_does_not_take_another_files_range(self) -> None:
        action = {**self.read(), "command": "sed -n '20,30p' other.py"}
        event = self.mirror(self.finish(self.item([action])), "view")
        self.assertIsNone(event["view_range"])
        self.assertEqual(event["title"], "file.py")

    def test_search_and_list_use_authoritative_dtos_with_unchanged_output(self) -> None:
        for kind in ("search", "listFiles"):
            with self.subTest(kind=kind):
                self.setUp()
                action: ObjectDict = {"type": kind, "command": "opaque command", "path": "src"}
                if kind == "search":
                    action["query"] = "needle"
                item = self.item([action])
                self.route("started", item)
                event = self.mirror(self.finish(item, "15: output stays exactly this\n"), "search")
                self.assertEqual(event["path"], "/project/src")
                self.assertEqual(event["mode"], "search" if kind == "search" else "glob")
                self.assertEqual(event["content"], "15: output stays exactly this\n")

    def test_null_search_fields_are_valid_and_use_command_cwd(self) -> None:
        item = self.item([{"type": "search", "command": "search", "path": None, "query": None}])
        self.route("started", item)
        event = self.mirror(self.finish(item), "search")
        self.assertEqual(event["path"], "/project")
        self.assertEqual(event["pattern"], "")

    def test_present_unknown_empty_or_malformed_actions_never_reparse_cat(self) -> None:
        for actions in ([], None, {}, [None], [{"type": {}, "command": "cat file.py"}],
                        [{"type": "unknown", "command": "cat file.py"}],
                        [{"type": "read", "command": "cat file.py", "path": None, "name": "file.py"}]):
            with self.subTest(actions=actions):
                self.setUp()
                item = self.item(actions, "cat file.py")
                started = self.route("started", item)
                self.assertEqual(cast(list[ObjectDict], started["events"])[0]["type"], "shell_begin")
                completed = self.finish(item)
                self.assertEqual(cast(list[ObjectDict], completed["events"])[0]["type"], "shell_end")
                self.assertEqual(cast(list[ObjectDict], completed["transcript_entries"])[0]["role"], "command")

    def test_multi_action_output_is_never_copied_into_fake_per_file_cards(self) -> None:
        for actions in ([self.read("a.py"), self.read("b.py")],
                        [self.read(), {"type": "unknown", "command": "mutate something"}]):
            self.setUp()
            item = self.item(actions, "cat a.py && cat b.py")
            self.route("started", item)
            result = self.finish(item, "combined output")
            entries = cast(list[ObjectDict], result["transcript_entries"])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["role"], "command")
            self.assertEqual(entries[0]["output"], "combined output")

    def test_explicit_unknown_heredoc_is_not_a_synthetic_filechange(self) -> None:
        command = "cat > new.py <<'EOF'\nhello\nEOF"
        item = self.item([{"type": "unknown", "command": command}], command)
        self.route("started", item)
        result = self.finish(item)
        self.assertEqual([entry["role"] for entry in cast(list[ObjectDict], result["transcript_entries"])], ["command"])

    def test_legacy_peer_without_actions_keeps_existing_view_parser(self) -> None:
        item = self.item([], "cat file.py")
        del item["commandActions"]
        self.route("started", item)
        self.mirror(self.finish(item), "view")

    def test_completed_only_read_is_classified(self) -> None:
        self.mirror(self.finish(self.item([self.read()])), "view")

    def test_sparse_completion_retains_started_semantics_and_streamed_output(self) -> None:
        self.route("started", self.item([self.read()]))
        delta = self.router.route_event(self.protocol, label="item/commandexecution/outputdelta",
            payload={"itemId": "command", "delta": "from delta\n"}, thread_id="thread", turn_id="turn")
        self.assertEqual(delta["events"], [])
        event = self.mirror(self.route("completed", {"id": "command", "type": "commandExecution",
                                                  "status": "completed", "exitCode": 0}), "view")
        self.assertEqual(event["content"], "from delta\n")

    def test_failed_read_preserves_error_as_command_not_a_successful_view(self) -> None:
        item = self.item([self.read()])
        self.route("started", item)
        result = self.finish(item, "permission denied", exitCode=1)
        self.assertEqual(cast(list[ObjectDict], result["events"])[0]["type"], "shell_end")

    def test_contradictory_late_metadata_does_not_orphan_a_started_run_card(self) -> None:
        started = self.item([{"type": "unknown", "command": "opaque read"}])
        self.route("started", started)
        result = self.finish(self.item([self.read()]))
        self.assertEqual(cast(list[ObjectDict], result["events"])[0]["type"], "shell_end")
        self.assertEqual(cast(list[ObjectDict], result["transcript_entries"])[0]["role"], "command")


if __name__ == "__main__":
    unittest.main()
