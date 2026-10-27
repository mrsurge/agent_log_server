from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from extensions.codex_ext.router import CodexEventRouter, ObjectDict
from extensions.codex_ext.runtime_protocol import ProtocolSemanticSpec, RuntimeProtocol
from extensions.codex_ext.transport import CodexAppServerTransport, ShellManager


class _Protocol:
    def event_spec(self, _label: str | None) -> ProtocolSemanticSpec | None:
        return None

    def server_request_spec(self, _label: str) -> ProtocolSemanticSpec | None:
        return None

    def notification_spec(self, label: str) -> ProtocolSemanticSpec | None:
        parts = label.split("/")
        return ProtocolSemanticSpec(label, parts[0], "item", parts[-1], ("item",))


class SubagentActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = CodexEventRouter()
        self.protocol = cast(RuntimeProtocol, _Protocol())

    def route(self, label: str, payload: ObjectDict, thread: str = "parent", turn: str = "parent-turn") -> ObjectDict:
        return self.router.route_event(self.protocol, label=label, payload=payload,
                                       thread_id=thread, turn_id=turn)

    def activity(self, kind: str = "started", phase: str = "started", **extra: object) -> ObjectDict:
        # Generated 0.163.0 ThreadItem/SubAgentActivityThreadItem contract.
        return self.route(f"item/{phase}", {"item": {
            "type": "subAgentActivity", "id": f"activity-{kind}", "kind": kind,
            "agentThreadId": "child", "agentPath": "/root/audit", **extra,
        }})

    def begin(self, turn: str = "child-turn") -> ObjectDict:
        return self.route("turn/started", {"turn": {"id": turn}}, "child", turn)

    def message(self, text: str, turn: str = "child-turn", identity: str = "message") -> ObjectDict:
        return self.route("item/completed", {"item": {
            "type": "agentMessage", "id": identity, "text": text,
        }}, "child", turn)

    def event(self, result: ObjectDict) -> ObjectDict:
        events = cast(list[ObjectDict], result["events"])
        entries = cast(list[ObjectDict], result["transcript_entries"])
        self.assertEqual(len(events), 1)
        event, entry = events[0], entries[0]
        event_fields: ObjectDict = {k: v for k, v in event.items() if k != "type"}
        entry_fields: ObjectDict = {k: v for k, v in entry.items() if k != "role"}
        self.assertEqual(event_fields, entry_fields)
        self.assertEqual(event["type"], entry["role"])
        return event

    def test_schema_activity_pair_registers_child_without_finishing_its_turn(self) -> None:
        bindings: list[str] = ["child"]
        self.assertEqual(self.activity()["bind_thread_ids"], bindings)
        self.assertFalse(self.activity(phase="completed")["events"])
        event = self.event(self.begin())
        self.assertEqual(event["status"], "running")
        self.assertEqual(event["id"], "child:child-turn")
        self.assertEqual(event["agent_turn_id"], "child-turn")
        self.assertNotIn("turn_id", event, "child cannot rotate the parent Rust projection")

    def test_latest_completed_message_replaces_previous_and_mirrors_replay(self) -> None:
        self.activity()
        self.begin()
        self.assertEqual(self.event(self.message("first"))["text"], "first")
        last = self.event(self.message("second", identity="message-2"))
        self.assertEqual(last["text"], "second")
        self.assertEqual(last["name"], "audit")
        self.assertTrue(last["compact"])
        self.assertFalse(self.message("second", identity="message-2")["events"])

    def test_child_notifications_never_clear_or_publish_parent_turn_state(self) -> None:
        self.activity()
        self.begin()
        for label in ("item/agentMessage/delta", "item/started", "turn/plan/updated",
                      "thread/tokenUsage/updated", "thread/status/changed"):
            result = self.route(label, {"delta": "token"}, "child", "child-turn")
            self.assertFalse(result["events"])
            self.assertNotIn("meta_patch", result)
            self.assertNotIn("set_turn_id", result)
        result = self.route("turn/completed", {"turn": {"id": "child-turn", "status": "completed"}}, "child", "child-turn")
        self.assertEqual(self.event(result)["type"], "subagent_end")
        self.assertNotIn("clear_turn_id", result)

    def test_followup_gets_a_distinct_card_and_old_completion_does_not_end_it(self) -> None:
        self.activity()
        self.begin()
        self.message("first")
        self.route("turn/completed", {"turn": {"id": "child-turn"}}, "child", "child-turn")
        event = self.event(self.begin("followup"))
        self.assertEqual(event["id"], "child:followup")
        self.assertEqual(event["text"], "")
        old = self.route("turn/completed", {"turn": {"id": "child-turn"}}, "child", "child-turn")
        self.assertFalse(old["events"])

    def test_interrupted_snapshot_preserves_latest_message(self) -> None:
        self.activity()
        self.begin()
        self.message("progress")
        ended = self.event(self.activity("interrupted"))
        self.assertEqual(ended["status"], "interrupted")
        self.assertEqual(ended["text"], "progress")
        self.assertFalse(ended["success"])
        self.assertFalse(self.activity("interrupted", phase="completed")["events"])

    def test_root_interactions_and_malformed_activities_do_not_capture_main_agent(self) -> None:
        self.activity(agentThreadId="parent", agentPath="/root")
        self.activity(agentThreadId=123)
        self.activity(kind="invalid")
        self.assertFalse(self.router._compact_subagents.agents)

    def test_collab_call_completion_is_not_agent_completion(self) -> None:
        self.activity()
        self.begin()
        for phase in ("started", "completed"):
            result = self.route(f"item/{phase}", {"item": {
                "type": "collabAgentToolCall", "id": "wait", "tool": "wait",
                "status": "completed", "senderThreadId": "parent",
                "receiverThreadIds": ["child"], "agentsStates": {},
            }})
            self.assertFalse(result["events"])

    def test_reset_clears_child_ownership(self) -> None:
        self.activity()
        self.router.reset()
        self.assertFalse(self.router._compact_subagents.agents)

    def test_shell_activity_is_visible_before_any_assistant_message(self) -> None:
        self.activity()
        self.assertEqual(self.event(self.begin())["activity"], "Agent running")
        item: ObjectDict = {"type": "commandExecution", "id": "shell", "command": "cat README.md"}
        started = self.event(self.route("item/started", {"item": item}, "child", "child-turn"))
        self.assertEqual(started["activity"], "Shell: cat README.md")
        self.assertIn("Running", str(started["text"]))
        done = self.event(self.route("item/completed", {"item": {
            **item, "exitCode": 0, "aggregatedOutput": "README contents",
        }}, "child", "child-turn"))
        self.assertIn("README contents", str(done["text"]))
        self.assertEqual(done["status"], "running", "tool completion is not agent completion")

    def test_mcp_and_dynamic_tools_include_arguments_and_bounded_output(self) -> None:
        self.activity()
        self.begin()
        for kind in ("mcpToolCall", "dynamicToolCall"):
            item: ObjectDict = {"type": kind, "id": "tool", "server": "tools", "tool": "inspect",
                                "arguments": {"path": "README.md"}}
            start = self.event(self.route("item/started", {"item": item}, "child", "child-turn"))
            self.assertIn("inspect", str(start["activity"]))
            self.assertIn("README.md", str(start["text"]))
            done = self.event(self.route("item/completed", {"item": {**item,
                "result": {"content": [{"type": "text", "text": "x" * 30000}]},
            }}, "child", "child-turn"))
            self.assertLess(len(str(done["text"])), 17000)
            self.assertNotIn("clear_turn_id", done)

    def test_streamed_response_updates_are_throttled_and_completion_flushes(self) -> None:
        self.activity()
        with patch("extensions.codex_ext.subagent_activity.time.monotonic", return_value=10.0):
            self.begin()
            self.route("item/started", {"item": {"type": "agentMessage", "id": "message"}}, "child", "child-turn")
            result = self.route("item/agentMessage/delta", {"itemId": "message", "delta": "partial"}, "child", "child-turn")
            self.assertFalse(result["events"])
        with patch("extensions.codex_ext.subagent_activity.time.monotonic", return_value=10.2):
            update = self.event(self.route("item/agentMessage/delta", {"itemId": "message", "delta": " response"}, "child", "child-turn"))
            self.assertEqual(update["text"], "partial response")
        self.assertEqual(self.event(self.message("final response"))["text"], "final response")


class _CaptureTransport(CodexAppServerTransport):
    captured_conversation: str | None = None

    async def route_event(self, label: str, payload: object, *, conversation_id: str | None,
                          thread_id: str | None, turn_id: str | None,
                          request_id: str | None) -> ObjectDict:
        self.captured_conversation = conversation_id
        return {"handled": True, "events": [], "transcript_entries": []}


class QueuedChildBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_resolves_binding_established_after_event_was_read(self) -> None:
        async def no_shell() -> ShellManager:
            raise AssertionError("must not start a shell")

        async def broadcast(_event: ObjectDict) -> None:
            raise AssertionError("empty projection must not broadcast")

        async def transcript(_conversation: str, _entry: ObjectDict) -> None:
            raise AssertionError("empty projection must not record")

        transport = _CaptureTransport(server_root=Path.cwd(), fws_getter=no_shell,
                                      broadcast_fn=broadcast, transcript_fn=transcript,
                                      meta_fns=None, raw_log_fn=lambda _d, _l, _p: None)
        # The IO reader queued this child event with conversation_id=None. Its
        # predecessor then registered the binding before ordered dispatch here.
        transport._remember_bindings(conversation_id="conversation", thread_id="child")
        await transport._route_transport_event("item/completed", {"threadId": "child"},
                                               conversation_id=None, thread_id="child",
                                               turn_id="child-turn", request_id=None)
        self.assertEqual(transport.captured_conversation, "conversation")


if __name__ == "__main__":
    unittest.main()
