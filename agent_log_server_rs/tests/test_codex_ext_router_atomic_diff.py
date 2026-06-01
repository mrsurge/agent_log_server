from __future__ import annotations

import unittest
from typing import cast

from extensions.codex_ext.router import CodexEventRouter, ObjectDict
from extensions.codex_ext.runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol


class _FakeProtocol:
    def __init__(self) -> None:
        self._notifications = {
            "item/completed": ProtocolSemanticSpec(
                name="item/completed",
                category="item",
                subject="item",
                phase="completed",
                properties=("item", "threadId", "turnId"),
            ),
            "turn/diff/updated": ProtocolSemanticSpec(
                name="turn/diff/updated",
                category="turn",
                subject="diff",
                phase="updated",
                properties=("diff", "threadId", "turnId"),
            ),
        }

    def notification_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return self._notifications.get(label)

    def event_spec(self, event_type: str | None) -> ProtocolSemanticSpec | None:
        return None

    def server_request_spec(self, label: str) -> ProtocolSemanticSpec | None:
        return None


class CodexAtomicDiffRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CodexEventRouter()
        self.protocol = cast(RuntimeProtocol, _FakeProtocol())

    def _completed_filechange(self, item_id: str, diff_text: str) -> ObjectDict:
        return self.router.route_event(
            self.protocol,
            label="item/completed",
            payload={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": item_id,
                    "type": "fileChange",
                    "status": "completed",
                    "changes": [{
                        "path": "src/example.ts",
                        "kind": "update",
                        "diff": diff_text,
                    }],
                },
            },
            thread_id="thread-1",
            turn_id="turn-1",
        )

    def test_completed_filechange_emits_atomic_conversation_diff(self) -> None:
        result = self._completed_filechange("call-one", "@@ -1 +1 @@\n-old\n+new\n")

        entries = cast(list[ObjectDict], result["transcript_entries"])
        diff_entries = [entry for entry in entries if entry.get("role") == "diff"]

        self.assertEqual([entry.get("role") for entry in entries], ["tool", "diff"])
        self.assertEqual(len(diff_entries), 1)
        self.assertEqual(diff_entries[0]["path"], "src/example.ts")
        self.assertEqual(diff_entries[0]["event"], "item/completed")
        self.assertIn("+new", cast(str, diff_entries[0]["text"]))

    def test_same_filechange_diff_content_is_not_coalesced_across_writes(self) -> None:
        first = self._completed_filechange("call-one", "@@ -1 +1 @@\n-old\n+new\n")
        second = self._completed_filechange("call-two", "@@ -1 +1 @@\n-old\n+new\n")

        first_diff = [
            entry
            for entry in cast(list[ObjectDict], first["transcript_entries"])
            if entry.get("role") == "diff"
        ][0]
        second_diff = [
            entry
            for entry in cast(list[ObjectDict], second["transcript_entries"])
            if entry.get("role") == "diff"
        ][0]

        self.assertNotEqual(first_diff["item_id"], second_diff["item_id"])
        self.assertIn("call-one", cast(str, first_diff["item_id"]))
        self.assertIn("call-two", cast(str, second_diff["item_id"]))

    def test_turn_diff_updated_is_not_a_conversation_diff_row(self) -> None:
        result = self.router.route_event(
            self.protocol,
            label="turn/diff/updated",
            payload={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "diff": "diff --git a/src/example.ts b/src/example.ts\n@@ -1 +1 @@\n-old\n+new\n",
            },
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertTrue(result["handled"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["transcript_entries"], [])


if __name__ == "__main__":
    unittest.main()
