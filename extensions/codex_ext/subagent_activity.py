"""Compact projection of generated ThreadItem sub-agent activity contracts.

The item notification phase is not the agent lifecycle: both phases carry the
same activity. Child messages are projected on completion, not re-recorded once
per token. Existing subagent_start/end upserts keep live and replay identical.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeGuard

ObjectDict = dict[str, object]


def _record(value: object) -> TypeGuard[ObjectDict]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


@dataclass
class Agent:
    path: str
    card_id: str = ""
    turn_id: str = ""
    text: str = ""
    status: str = "running"
    ended: bool = True
    last_message: str = ""


class SubagentActivityProjection:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.seen: dict[str, None] = {}

    def _empty(self) -> ObjectDict:
        return {"handled": True, "events": [], "transcript_entries": []}

    def _snapshot(self, thread: str, agent: Agent, *, end: bool = False) -> ObjectDict:
        kind: Literal["subagent_start", "subagent_end"] = "subagent_end" if end else "subagent_start"
        data: ObjectDict = {
            "id": agent.card_id, "name": agent.path.rsplit("/", 1)[-1],
            "agent_path": agent.path, "agent_thread_id": thread,
            # turn_id controls the conversation's Rust active-turn projection.
            # A child must never rotate that projection away from the parent.
            "agent_turn_id": agent.turn_id, "text": agent.text,
            "compact": True, "status": agent.status,
            "success": agent.status not in {"failed", "interrupted"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return {"handled": True, "events": [{"type": kind, **data}],
                "transcript_entries": [{"role": kind, **data}]}

    def _begin(self, thread: str, agent: Agent, turn: str) -> None:
        if agent.card_id and agent.turn_id == turn:
            return
        agent.turn_id = turn
        agent.card_id = f"{thread}:{turn}"
        agent.text = ""
        agent.last_message = ""
        agent.status = "running"
        agent.ended = False

    def route(self, label: str, payload: ObjectDict, thread: str, turn: str) -> ObjectDict | None:
        agent: Agent | None
        item_value = payload.get("item")
        item = item_value if _record(item_value) else {}
        item_type = _text(item.get("type"))
        if item_type == "subAgentActivity" and label in {"item/started", "item/completed"}:
            child = _text(item.get("agentThreadId"))
            path = _text(item.get("agentPath"))
            identity = _text(item.get("id"))
            kind = _text(item.get("kind"))
            # An interaction can address the root/parent; never classify it as a child.
            if (not child or not path or path.rstrip("/").count("/") < 2 or child == thread
                    or kind not in {"started", "interacted", "completed", "interrupted"}):
                return self._empty()
            agent = self.agents.setdefault(child, Agent(path))
            agent.path = path
            result = self._empty()
            result["bind_thread_ids"] = [child]
            if not identity or identity in self.seen:
                return result
            self.seen[identity] = None
            if len(self.seen) > 4096:
                del self.seen[next(iter(self.seen))]
            if kind in {"completed", "interrupted"} and agent.card_id and not agent.ended:
                agent.status = "interrupted" if kind == "interrupted" else "completed"
                agent.ended = True
                result = self._snapshot(child, agent, end=True)
                result["bind_thread_ids"] = [child]
            return result

        # collabAgentToolCall.status describes the call, not an agent's turn.
        if item_type == "collabAgentToolCall" and label in {"item/started", "item/completed"}:
            states = item.get("agentsStates")
            receivers = item.get("receiverThreadIds")
            bindings: list[str] = []
            if _list(receivers):
                for receiver in receivers:
                    if isinstance(receiver, str) and receiver in self.agents:
                        bindings.append(receiver)
            result = self._empty()
            result["bind_thread_ids"] = bindings
            if _record(states):
                for child, raw_state in states.items():
                    agent = self.agents.get(child)
                    if agent is None or not _record(raw_state) or not agent.card_id:
                        continue
                    message = _text(raw_state.get("message"))
                    if message:
                        agent.text = message
                    status = _text(raw_state.get("status"))
                    if status in {"completed", "errored", "interrupted", "shutdown", "notFound"}:
                        agent.status = "failed" if status in {"errored", "notFound"} else status
                        agent.ended = True
                        snapshot = self._snapshot(child, agent, end=True)
                        for key in ("events", "transcript_entries"):
                            target, values = result[key], snapshot[key]
                            if _list(target) and _list(values):
                                target.extend(values)
            return result

        agent = self.agents.get(thread)
        if agent is None or not label.startswith(("item/", "turn/", "thread/")):
            return None
        turn_obj = payload.get("turn")
        actual_turn = _text(turn_obj.get("id")) if _record(turn_obj) else ""
        actual_turn = actual_turn or turn or agent.turn_id
        if label == "turn/started":
            self._begin(thread, agent, actual_turn)
            return self._snapshot(thread, agent)
        if label == "item/completed" and item_type == "agentMessage":
            self._begin(thread, agent, actual_turn)
            message_id = _text(item.get("id"))
            if message_id and message_id == agent.last_message:
                return self._empty()
            agent.last_message = message_id
            agent.text = _text(item.get("text"))
            return self._snapshot(thread, agent, end=agent.ended)
        if label == "turn/completed":
            if agent.ended or not agent.card_id or agent.turn_id != actual_turn:
                return self._empty()
            status = _text(turn_obj.get("status")) if _record(turn_obj) else ""
            agent.status = status or "completed"
            agent.ended = True
            return self._snapshot(thread, agent, end=True)
        # Do not leak child tool/status/plan/token events into the parent projection.
        # Server approval requests are not notifications and retain the normal path.
        if label.endswith(("requestapproval", "requestuserinput")):
            return None
        return self._empty()
