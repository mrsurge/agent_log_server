from typing import cast
import unittest

from extensions.codex_ext.router import CodexEventRouter, ObjectDict
from extensions.codex_ext.runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol


class CompactionProtocol:
    def notification_spec(self, label: str) -> ProtocolSemanticSpec | None:
        if label in {"item/started", "item/completed"}:
            return ProtocolSemanticSpec(name=label, category="item", subject="item", phase=label.split("/")[-1], properties=())
        if label == "item/commandexecution/terminalinteraction":
            return ProtocolSemanticSpec(name=label, category="item", subject="commandexecution", phase="terminalinteraction", properties=())
        return None

    def event_spec(self, event_type: str | None) -> ProtocolSemanticSpec | None:
        return None

    def server_request_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return None


class CompactionCardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CodexEventRouter()
        self.protocol = cast(RuntimeProtocol, CompactionProtocol())

    def send(self, label: str, item_id: str = "compact-1") -> ObjectDict:
        return self.router.route_event(
            self.protocol, label=label,
            payload={"threadId": "thread-1", "turnId": "turn-1", "item": {"type": "contextCompaction", "id": item_id}},
            thread_id="thread-1", turn_id="turn-1",
        )

    def test_completed_card_is_live_and_durable_with_same_identity(self) -> None:
        started = self.send("item/started")
        self.assertEqual(started["transcript_entries"], [])
        completed = self.send("item/completed")
        event = cast(list[ObjectDict], completed["events"])[0]
        record = cast(list[ObjectDict], completed["transcript_entries"])[0]
        self.assertEqual(event["type"], "context_compacted")
        self.assertEqual(record["role"], "context_compacted")
        for key in ("id", "turn_id", "source"):
            self.assertEqual(event[key], record[key])
        self.assertEqual(self.send("item/completed")["events"], [])
        self.assertEqual(self.send("thread/compacted")["events"], [])
        self.assertTrue(self.send("item/completed", "compact-2")["events"])

    def test_legacy_first_does_not_duplicate_modern_completion(self) -> None:
        self.assertTrue(self.send("thread/compacted")["transcript_entries"])
        self.assertEqual(self.send("item/completed")["events"], [])
        self.assertTrue(self.send("item/completed", "compact-2")["transcript_entries"])

    def test_terminal_interaction_distinguishes_read_from_write(self) -> None:
        for stdin, tool in [("", "read_shell"), ("yes\n", "write_shell")]:
            routed = self.router.route_event(
                self.protocol, label="item/commandExecution/terminalInteraction",
                payload={"itemId": "shell-1", "stdin": stdin}, thread_id="thread-1", turn_id="turn-1",
            )
            event = cast(list[ObjectDict], routed["events"])[0]
            self.assertEqual(event["tool"], tool)


if __name__ == "__main__":
    unittest.main()
