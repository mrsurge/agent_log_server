from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import socketio
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel


@dataclass
class ConnectionManager:
    active_connections: list[WebSocket] = field(default_factory=list)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        with contextlib.suppress(ValueError):
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        data = json.dumps(message)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(data)
            except Exception:
                with contextlib.suppress(ValueError):
                    self.active_connections.remove(connection)


class MessageIn(BaseModel):
    who: str
    message: str


class AwaitIn(BaseModel):
    after_msg_num: int
    from_who: str | None = None
    timeout_ms: int = 180000


class AgentLogSubsystem:
    def __init__(
        self,
        *,
        socketio_server: socketio.AsyncServer,
        utc_ts: Callable[[], str],
        namespace: str = "/appserver",
    ) -> None:
        self._socketio_server = socketio_server
        self._utc_ts = utc_ts
        self._namespace = namespace
        self._manager = ConnectionManager()
        self._lock = asyncio.Lock()
        self._next_msg_num = 1
        self._log_path: Path | None = None

    @property
    def log_path(self) -> Path | None:
        return self._log_path

    def default_log_dir(self) -> Path:
        return Path.home() / ".cache" / "app_server" / "agent-log"

    def resolve_log_path(self, raw: str) -> Path:
        text = str(raw or "").strip()
        if not text:
            return self.default_log_dir() / "agent_chat.log.jsonl"

        expanded = Path(os.path.expanduser(text))
        if expanded.is_absolute():
            return expanded

        if text.startswith("./") or text.startswith("../") or "/" in text:
            return Path.cwd() / expanded

        return self.default_log_dir() / expanded.name

    def initialize_log_path(self, raw: str) -> Path:
        log_path = self.resolve_log_path(raw)
        self.ensure_log_file(log_path)
        self._log_path = log_path
        self._init_msg_num()
        return log_path

    def ensure_log_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")

    def _init_msg_num(self) -> None:
        log_path = self._log_path
        if log_path is None or not log_path.exists():
            self._next_msg_num = 1
            return

        records: list[dict[str, Any]] = []
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)

        if not records:
            self._next_msg_num = 1
            return

        needs_rewrite = any("msg_num" not in record for record in records)
        if needs_rewrite:
            for idx, record in enumerate(records, start=1):
                record["msg_num"] = idx
            with log_path.open("w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._next_msg_num = len(records) + 1
            return

        max_num = max(
            record.get("msg_num", 0)
            for record in records
            if isinstance(record.get("msg_num", 0), int)
        )
        self._next_msg_num = int(max_num) + 1

    def _delete_record_by_msg_num(self, msg_num: int) -> bool:
        log_path = self._log_path
        if log_path is None or not log_path.exists():
            return False

        records: list[dict[str, Any]] = []
        found = False
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("msg_num") == msg_num:
                    found = True
                    continue
                records.append(record)

        if found:
            with log_path.open("w", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return found

    async def append_record(self, record: dict[str, Any]) -> None:
        log_path = self._log_path
        if log_path is None:
            raise RuntimeError("Agent log not initialized")
        async with self._lock:
            if "msg_num" not in record:
                record["msg_num"] = self._next_msg_num
                self._next_msg_num += 1
            elif isinstance(record.get("msg_num"), int) and int(record["msg_num"]) >= self._next_msg_num:
                self._next_msg_num = int(record["msg_num"]) + 1
            line = json.dumps(record, ensure_ascii=False)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        await self._manager.broadcast(record)
        with contextlib.suppress(Exception):
            await self._socketio_server.emit("agent_log_message", record, namespace=self._namespace)

    def read_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        log_path = self._log_path
        if log_path is None or not log_path.exists():
            return []

        records: list[dict[str, Any]] = []
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)

        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    def get_record_by_msg_num(self, msg_num: int) -> dict[str, Any] | None:
        log_path = self._log_path
        if log_path is None or not log_path.exists():
            return None
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("msg_num") == msg_num:
                    return record
        return None

    async def api_get_messages(
        self,
        limit: Annotated[int | None, Query(gt=0)] = None,
    ) -> list[dict[str, Any]]:
        return self.read_records(limit=limit)

    async def api_get_message_by_num(self, msg_num: int) -> dict[str, Any] | JSONResponse:
        record = self.get_record_by_msg_num(msg_num)
        if record is None:
            return JSONResponse({"error": f"Message {msg_num} not found"}, status_code=404)
        return record

    async def api_delete_message_by_num(self, msg_num: int) -> dict[str, Any] | JSONResponse:
        async with self._lock:
            deleted = self._delete_record_by_msg_num(msg_num)
        if not deleted:
            return JSONResponse({"error": f"Message {msg_num} not found"}, status_code=404)
        return {"ok": True, "deleted": msg_num}

    async def api_post_message(self, msg: MessageIn) -> dict[str, Any] | JSONResponse:
        who = msg.who.strip()
        text = msg.message.strip()
        if not who or not text:
            return JSONResponse({"error": "Both 'who' and 'message' are required"}, status_code=400)

        record = {"ts": self._utc_ts(), "who": who, "message": text}
        await self.append_record(record)
        return record

    async def api_await_message(self, req: AwaitIn) -> dict[str, Any] | JSONResponse:
        after_num = req.after_msg_num
        from_who = req.from_who.strip() if req.from_who else None
        timeout_s = max(1, min(req.timeout_ms, 600000)) / 1000.0
        poll_interval = 0.5
        elapsed = 0.0

        while elapsed < timeout_s:
            for record in self.read_records():
                rec_num = record.get("msg_num")
                if rec_num is None or rec_num <= after_num:
                    continue
                if from_who and record.get("who") != from_who:
                    continue
                return record
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return JSONResponse({"error": "timeout", "after_msg_num": after_num}, status_code=408)

    async def websocket_endpoint(self, websocket: WebSocket) -> None:
        await self._manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            self._manager.disconnect(websocket)

    def register_routes(self, app: FastAPI) -> None:
        app.add_api_route("/api/messages", self.api_get_messages, methods=["GET"], response_model=None)
        app.add_api_route("/api/messages/{msg_num}", self.api_get_message_by_num, methods=["GET"], response_model=None)
        app.add_api_route("/api/messages/{msg_num}", self.api_delete_message_by_num, methods=["DELETE"], response_model=None)
        app.add_api_route("/api/messages", self.api_post_message, methods=["POST"], status_code=201, response_model=None)
        app.add_api_route("/api/messages/await", self.api_await_message, methods=["POST"], response_model=None)
        app.add_api_websocket_route("/ws", self.websocket_endpoint)
