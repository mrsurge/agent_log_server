"""
Per-conversation SQLite-backed todo tracking.

Each conversation gets its own ``todos.db`` inside its conversation
directory (``~/.cache/app_server/conversations/<id>/todos.db``).

All public helpers accept a *conversation_id* and resolve the DB path
through the shared ``CONVERSATIONS_DIR`` constant that ``server.py``
also uses.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

CONVERSATIONS_DIR: Path | None = None


def configure(conversations_dir: str | Path) -> None:
    """Set the base conversations directory.  Called once at startup."""
    global CONVERSATIONS_DIR
    CONVERSATIONS_DIR = Path(conversations_dir)


def _db_path(conversation_id: str) -> Path:
    assert CONVERSATIONS_DIR is not None, "conversation_todos not configured"
    return CONVERSATIONS_DIR / conversation_id / "todos.db"


def _connect(conversation_id: str) -> sqlite3.Connection:
    path = _db_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    description TEXT   DEFAULT '',
    status     TEXT    DEFAULT 'pending',
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS todo_deps (
    todo_id    INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (todo_id, depends_on),
    FOREIGN KEY (todo_id)    REFERENCES todos(id) ON DELETE CASCADE,
    FOREIGN KEY (depends_on) REFERENCES todos(id) ON DELETE CASCADE
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ── public CRUD ──────────────────────────────────────────────────────

def list_todos(
    conversation_id: str,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """Return all todos, optionally filtered by *status*."""
    conn = _connect(conversation_id)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM todos WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM todos ORDER BY id").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def add_todo(
    conversation_id: str,
    title: str,
    description: str = "",
    status: str = "pending",
) -> dict[str, Any]:
    """Insert a new todo and return it."""
    now = _now()
    conn = _connect(conversation_id)
    try:
        cur = conn.execute(
            "INSERT INTO todos (title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, status, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def update_todo(
    conversation_id: str,
    todo_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Update fields on an existing todo.  Returns the updated row or None."""
    parts: list[str] = []
    params: list[Any] = []
    if title is not None:
        parts.append("title = ?")
        params.append(title)
    if description is not None:
        parts.append("description = ?")
        params.append(description)
    if status is not None:
        parts.append("status = ?")
        params.append(status)
    if not parts:
        return get_todo(conversation_id, todo_id)
    parts.append("updated_at = ?")
    params.append(_now())
    params.append(todo_id)
    conn = _connect(conversation_id)
    try:
        conn.execute(f"UPDATE todos SET {', '.join(parts)} WHERE id = ?", params)
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_todo(conversation_id: str, todo_id: int) -> dict[str, Any] | None:
    """Fetch a single todo by id."""
    conn = _connect(conversation_id)
    try:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def remove_todo(conversation_id: str, todo_id: int) -> bool:
    """Delete a todo.  Returns True if a row was removed."""
    conn = _connect(conversation_id)
    try:
        cur = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def toggle_todo(conversation_id: str, todo_id: int) -> dict[str, Any] | None:
    """Toggle between 'pending' and 'done'.  Returns updated row."""
    row = get_todo(conversation_id, todo_id)
    if row is None:
        return None
    new_status = "done" if row["status"] != "done" else "pending"
    return update_todo(conversation_id, todo_id, status=new_status)


# ── dependency helpers ───────────────────────────────────────────────

def add_dep(conversation_id: str, todo_id: int, depends_on: int) -> bool:
    """Add a dependency.  Returns True on success."""
    conn = _connect(conversation_id)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO todo_deps (todo_id, depends_on) VALUES (?, ?)",
            (todo_id, depends_on),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_dep(conversation_id: str, todo_id: int, depends_on: int) -> bool:
    """Remove a dependency."""
    conn = _connect(conversation_id)
    try:
        cur = conn.execute(
            "DELETE FROM todo_deps WHERE todo_id = ? AND depends_on = ?",
            (todo_id, depends_on),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_ready(conversation_id: str) -> list[dict[str, Any]]:
    """Return pending todos whose dependencies are all done."""
    conn = _connect(conversation_id)
    try:
        rows = conn.execute(
            """
            SELECT t.* FROM todos t
            WHERE t.status = 'pending'
            AND NOT EXISTS (
                SELECT 1 FROM todo_deps td
                JOIN todos dep ON td.depends_on = dep.id
                WHERE td.todo_id = t.id AND dep.status != 'done'
            )
            ORDER BY t.id
            """
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# ── .te2 directory helper ───────────────────────────────────────────

def ensure_te2_dir(cwd: str | Path) -> Path | None:
    """Create ``.te2/`` in *cwd* if it does not exist.  Returns the path."""
    try:
        te2 = Path(cwd) / ".te2"
        te2.mkdir(parents=False, exist_ok=True)
        return te2
    except OSError:
        return None
