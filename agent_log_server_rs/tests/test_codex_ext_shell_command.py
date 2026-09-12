from __future__ import annotations

import shlex
import unittest
from typing import Callable, cast

from extensions.codex_ext import router as router_module
from extensions.codex_ext.router import CodexEventRouter, ObjectDict
from extensions.codex_ext.runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol


class _FakeProtocol:
    def __init__(self) -> None:
        self._notifications = {
            phase: ProtocolSemanticSpec(
                name=f"item/{phase}",
                category="item",
                subject="item",
                phase=phase,
                properties=("item", "threadId", "turnId"),
            )
            for phase in ("started", "completed")
        }
        self._notifications["outputdelta"] = ProtocolSemanticSpec(
            name="item/commandexecution/outputdelta",
            category="item",
            subject="commandexecution",
            phase="outputdelta",
            properties=("itemId", "delta", "threadId", "turnId"),
        )

    def notification_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return self._notifications.get(label.rsplit("/", 1)[-1])

    def event_spec(self, event_type: str | None) -> ProtocolSemanticSpec | None:
        return None

    def server_request_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return None


class CodexShellCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.display_command = cast(
            Callable[[object], str],
            getattr(router_module, "_display_command_text"),
        )

    def test_unwraps_login_shell_with_nested_quotes(self) -> None:
        raw = "/bin/bash -lc 'rg -n '\"'\"'needle'\"'\"' src'"
        self.assertEqual("rg -n 'needle' src", self.display_command(raw))

    def test_unwraps_supported_shell_argv(self) -> None:
        self.assertEqual(
            "printf '%s\\n' hello",
            self.display_command(["/usr/bin/zsh", "-c", "printf '%s\\n' hello"]),
        )

    def test_preserves_non_wrapper_and_invalid_shell_text(self) -> None:
        self.assertEqual("git status --short", self.display_command("git status --short"))
        invalid = "/bin/bash -lc 'unterminated"
        self.assertEqual(invalid, self.display_command(invalid))

    def test_card_detection_preserves_wrapped_argv_boundaries(self) -> None:
        cases = [
            ("_shell_command_to_new_file_spec", "cat > 'new file.py' <<'EOF'\nprint(\"it's quoted\")\nEOF"),
            ("_shell_command_to_view_spec", "sed -n '1,5p' 'file name.py'"),
            ("_shell_command_to_search_spec", "rg -n 'hello world' 'file name.py'"),
            ("_shell_command_to_view_sequence", "cat 'first file.py' && printf '%s\\n' 'DIVIDER' && cat 'second file.py'"),
        ]
        for name, script in cases:
            parser = cast(Callable[[object, str], ObjectDict | None], getattr(router_module, name))
            expected = parser(script, "/project")
            self.assertIsNotNone(expected, name)
            for shell in ("/bin/sh", "/bin/bash", "/usr/bin/zsh"):
                for flag in ("-c", "-lc"):
                    argv = [shell, flag, script]
                    with self.subTest(parser=name, shell=shell, flag=flag):
                        self.assertEqual(expected, parser(argv, "/project"))
                        self.assertEqual(expected, parser(shlex.join(argv), "/project"))

    def test_live_and_replay_use_display_command_and_keep_raw_command(self) -> None:
        router = CodexEventRouter()
        protocol = cast(RuntimeProtocol, _FakeProtocol())
        raw = "/bin/sh -lc 'printf '\"'\"'hello world'\"'\"''"
        base: ObjectDict = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "command-1",
                "type": "commandExecution",
                "command": raw,
            },
        }

        started = router.route_event(
            protocol,
            label="item/started",
            payload=base,
            thread_id="thread-1",
            turn_id="turn-1",
        )
        completed_payload = cast(ObjectDict, {**base, "item": {**cast(ObjectDict, base["item"]), "status": "completed", "aggregatedOutput": "hello world"}})
        completed = router.route_event(
            protocol,
            label="item/completed",
            payload=completed_payload,
            thread_id="thread-1",
            turn_id="turn-1",
        )

        begin = cast(list[ObjectDict], started["events"])[0]
        end = cast(list[ObjectDict], completed["events"])[0]
        replay = cast(list[ObjectDict], completed["transcript_entries"])[0]
        for item in (begin, end, replay):
            self.assertEqual("printf 'hello world'", item["command"])
            self.assertEqual(raw, item["raw_command"])

    def test_heredoc_and_trailing_commands_have_separate_live_and_replay_cards(self) -> None:
        script = "cat > 'new file.py' <<'EOF'\nprint(\"it's quoted\")\nEOF\npython 'new file.py'\nbasedpyright 'new file.py'"
        tail = "python 'new file.py'\nbasedpyright 'new file.py'"
        argv = ["/bin/sh", "-lc", script]
        for command in (script, shlex.join(argv), argv):
            for exit_code in (0, 1):
                with self.subTest(command_type=type(command).__name__, exit_code=exit_code):
                    router = CodexEventRouter()
                    protocol = cast(RuntimeProtocol, _FakeProtocol())
                    item: ObjectDict = {"id": "write-1", "type": "commandExecution", "command": command, "cwd": "/project"}
                    def route(label: str, payload: ObjectDict) -> ObjectDict:
                        return router.route_event(protocol, label=label, payload=payload, thread_id="thread-1", turn_id="turn-1")

                    started = route("item/started", {"item": item})
                    events = cast(list[ObjectDict], started["events"])
                    self.assertEqual("tool_begin", events[0]["type"])
                    begin = next(event for event in events if event["type"] == "shell_begin")
                    self.assertEqual(tail, begin["command"])
                    self.assertNotEqual(events[0]["id"], begin["id"])
                    delta = route("item/commandexecution/outputdelta", {"itemId": "write-1", "delta": "test output\n"})
                    self.assertEqual([{"type": "shell_delta", "id": begin["id"], "delta": "test output\n"}], delta["events"])
                    completed = route("item/completed", {"item": {**item, "status": "completed", "exitCode": exit_code}})
                    final_events = cast(list[ObjectDict], completed["events"])
                    records = cast(list[ObjectDict], completed["transcript_entries"])
                    shell_end = next(event for event in final_events if event["type"] == "shell_end")
                    shell_record = next(record for record in records if record["role"] == "command")
                    tool_record = next(record for record in records if record["role"] == "tool")
                    diff_record = next(record for record in records if record["role"] == "diff")
                    self.assertEqual("", tool_record["output"])
                    self.assertEqual("test output\n", shell_record["output"])
                    self.assertEqual(shell_record["output"], shell_end["stdout"])
                    self.assertEqual(exit_code, shell_record["exit_code"])
                    self.assertEqual(exit_code, shell_end["exitCode"])
                    for record in (begin, shell_end, shell_record):
                        self.assertEqual("write-1:shell", record["id"])
                        self.assertEqual(tail, record["command"])
                        self.assertEqual("invocation", record["result_scope"])
                    self.assertIn("+print(\"it's quoted\")", str(diff_record["text"]))
                    self.assertNotIn("basedpyright", str(diff_record["text"]))

    def test_heredoc_requires_exact_terminator_and_preserves_body(self) -> None:
        parser = cast(Callable[[object, str], ObjectDict | None], getattr(router_module, "_shell_command_to_new_file_spec"))
        self.assertIsNone(parser("cat > file.py <<'EOF'\nprint('EOF')\nEOF_suffix\npytest", "/project"))
        spec = parser("cat > file.py <<'EOF'\nprint('EOF')\nEOF\npytest", "/project")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual("print('EOF')", spec["content"])
        self.assertEqual("pytest", spec["trailing_command"])


if __name__ == "__main__":
    unittest.main()
