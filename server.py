#!/usr/bin/env python3
import asyncio
import json
import os
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

app = FastAPI()

# --- Config & State ---
LOG_PATH: Optional[Path] = None
_lock = asyncio.Lock()

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        data = json.dumps(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(data)
            except Exception:
                pass

manager = ConnectionManager()

# --- Helpers ---
def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("", encoding="utf-8")

async def append_record(record: Dict[str, Any]) -> None:
    assert LOG_PATH is not None
    line = json.dumps(record, ensure_ascii=False)
    async with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    await manager.broadcast(record)

def read_records(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    assert LOG_PATH is not None
    if not LOG_PATH.exists():
        return []
    
    records = []
    with LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    
    if limit is not None and limit > 0:
        return records[-limit:]
    return records

# --- Models ---
class MessageIn(BaseModel):
    who: str
    message: str

# --- Routes ---
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    return templates.TemplateResponse("template.html", {"request": request})

@app.get("/api/health")
async def api_health():
    return {"ok": True, "ts": utc_ts()}

@app.get("/api/messages")
async def get_messages(limit: int = Query(None, gt=0)):
    return read_records(limit=limit)

@app.post("/api/messages", status_code=201)
async def post_message(msg: MessageIn):
    who = msg.who.strip()
    text = msg.message.strip()
    if not who or not text:
        return JSONResponse({"error": "Both 'who' and 'message' are required"}, status_code=400)

    record = {"ts": utc_ts(), "who": who, "message": text}
    await append_record(record)
    return record

@app.post("/api/shutdown")
async def api_shutdown():
    try:
        await append_record({"ts": utc_ts(), "who": "server", "message": "shutdown requested"})
    except Exception:
        pass
    
    loop = asyncio.get_event_loop()
    loop.call_later(0.1, os._exit, 0)
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- Startup ---
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--log", default="agent_chat.log.jsonl")
    p.add_argument("--port", type=int, default=12356)
    p.add_argument("--host", default="127.0.0.1")
    return p.parse_args()

def main():
    global LOG_PATH
    args = parse_args()
    
    log_p = Path(args.log)
    if not log_p.is_absolute():
        log_p = Path.cwd() / log_p
    ensure_log_file(log_p)
    LOG_PATH = log_p

    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            r = {"ts": utc_ts(), "who": "server", "message": f"started on {args.host}:{args.port}"}
            f.write(json.dumps(r) + "\n")
    except Exception:
        pass

    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
