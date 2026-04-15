from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from agent_log_server import conversation_todos as _conv_todos
from .typing_helpers import ObjectMap, coerce_object_map

ExtensionsConfig = dict[str, ObjectMap]
_NormalizeExtensionsConfig = Callable[[object], ExtensionsConfig]
_MetaSaveCallback = Callable[[str], object]


def _coerce_extensions_config(value: object) -> ExtensionsConfig:
    if not isinstance(value, dict):
        return {}
    normalized: ExtensionsConfig = {}
    for key, item in value.items():
        if isinstance(key, str) and isinstance(item, dict):
            normalized[key] = coerce_object_map(item)
    return normalized


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ConversationStore:
    config_path: Path = field(
        default_factory=lambda: Path(os.path.expanduser("~/.cache/app_server/app_server_config.json"))
    )
    app_server_data_path: Path = field(
        default_factory=lambda: Path(os.path.expanduser("~/.local/share/app_server"))
    )
    _extensions_config_normalizer: _NormalizeExtensionsConfig | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _meta_save_callback: _MetaSaveCallback | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _conv_todos.configure(self.conversations_dir)

    @property
    def cache_dir(self) -> Path:
        return self.config_path.parent

    @property
    def user_extensions_dir(self) -> Path:
        return self.app_server_data_path / "extensions"

    @property
    def legacy_transcript_dir(self) -> Path:
        return self.cache_dir / "transcripts"

    @property
    def conversations_dir(self) -> Path:
        return self.cache_dir / "conversations"

    def set_extensions_config_normalizer(
        self,
        callback: _NormalizeExtensionsConfig | None,
    ) -> None:
        self._extensions_config_normalizer = callback

    def set_meta_save_callback(self, callback: _MetaSaveCallback | None) -> None:
        self._meta_save_callback = callback

    def normalize_extensions_config(self, raw: object) -> ExtensionsConfig:
        callback = self._extensions_config_normalizer
        if callback is None:
            return _coerce_extensions_config(raw)
        normalized = callback(raw)
        return _coerce_extensions_config(normalized)

    def default_appserver_config(self) -> ObjectMap:
        return {
            "cwd": None,
            "thread_id": None,
            "turn_id": None,
            "conversation_id": None,
            "conversations": [],
            "pinned_conversations": [],
            "active_view": "splash",
            "app_server_command": None,
            "shell_id": None,
            "user_name": None,
            "te2_mcp_integration": False,
            "extensions": {},
        }

    def load_appserver_config(self) -> ObjectMap:
        cfg = self.default_appserver_config()
        try:
            if self.config_path.exists():
                data_obj = cast(object, json.loads(self.config_path.read_text(encoding="utf-8")))
                if isinstance(data_obj, dict):
                    data = coerce_object_map(data_obj)
                    cfg.update(data)
                    cfg["extensions"] = self.normalize_extensions_config(cfg.get("extensions"))
            else:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(
                    json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
        except Exception:
            return cfg
        return cfg

    def save_appserver_config(self, cfg: ObjectMap) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    def normalize_conversation_list(self, cfg: ObjectMap) -> list[str]:
        conversations = cfg.get("conversations")
        if not isinstance(conversations, list):
            conversations = []
        out: list[str] = []
        for item in conversations:
            if isinstance(item, str) and item and item not in out:
                out.append(item)
        return out

    def add_conversation_to_config(self, conversation_id: str, cfg: ObjectMap) -> bool:
        conversations = self.normalize_conversation_list(cfg)
        if conversation_id in conversations:
            cfg["conversations"] = conversations
            return False
        conversations.append(conversation_id)
        cfg["conversations"] = conversations
        return True

    def remove_conversation_from_config(self, conversation_id: str, cfg: ObjectMap) -> None:
        conversations = self.normalize_conversation_list(cfg)
        if conversation_id in conversations:
            conversations = [item for item in conversations if item != conversation_id]
        cfg["conversations"] = conversations
        pinned = self.normalize_pinned_conversation_list(cfg, conversations)
        if conversation_id in pinned:
            cfg["pinned_conversations"] = [item for item in pinned if item != conversation_id]

    def normalize_pinned_conversation_list(
        self,
        cfg: ObjectMap,
        valid_ids: list[str] | None = None,
    ) -> list[str]:
        pinned = cfg.get("pinned_conversations")
        if not isinstance(pinned, list):
            pinned = []
        out: list[str] = []
        valid_set = set(valid_ids) if isinstance(valid_ids, list) else None
        for item in pinned:
            if not isinstance(item, str) or not item:
                continue
            if valid_set is not None and item not in valid_set:
                continue
            if item not in out:
                out.append(item)
        cfg["pinned_conversations"] = out
        return out

    def conversation_ids_from_disk(self) -> list[str]:
        if not self.conversations_dir.exists():
            return []
        ids: list[str] = []
        for child in self.conversations_dir.iterdir():
            if not child.is_dir():
                continue
            if (child / "meta.json").exists():
                ids.append(child.name)
        return ids

    def sync_conversation_index(self, cfg: ObjectMap) -> list[str]:
        ids = self.normalize_conversation_list(cfg)
        for conversation_id in self.conversation_ids_from_disk():
            if conversation_id not in ids:
                ids.append(conversation_id)
        cfg["conversations"] = ids
        return ids

    def conversation_display_order(self, cfg: ObjectMap) -> list[str]:
        ids = self.sync_conversation_index(cfg)
        pinned = self.normalize_pinned_conversation_list(cfg, ids)
        pinned_set = set(pinned)
        return pinned + [conversation_id for conversation_id in ids if conversation_id not in pinned_set]

    def find_conversation_by_thread_id(self, thread_id: str | None) -> str | None:
        if not thread_id or not self.conversations_dir.exists():
            return None
        for child in self.conversations_dir.iterdir():
            if not child.is_dir():
                continue
            meta_path = child / "meta.json"
            if not meta_path.exists():
                continue
            try:
                data_obj = cast(object, json.loads(meta_path.read_text(encoding="utf-8")))
            except Exception:
                continue
            data = coerce_object_map(data_obj)
            if isinstance(data, dict) and data.get("thread_id") == thread_id:
                return child.name
        return None

    def sanitize_conversation_id(self, value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
        return safe or "unknown"

    def conversation_dir(self, conversation_id: str) -> Path:
        safe_id = self.sanitize_conversation_id(conversation_id)
        return self.conversations_dir / safe_id

    def conversation_meta_path(self, conversation_id: str) -> Path:
        return self.conversation_dir(conversation_id) / "meta.json"

    def conversation_transcript_path(self, conversation_id: str) -> Path:
        return self.conversation_dir(conversation_id) / "transcript.jsonl"

    def default_conversation_meta(self, conversation_id: str) -> ObjectMap:
        return {
            "conversation_id": conversation_id,
            "created_at": utc_ts(),
            "thread_id": None,
            "pending_approvals": {},
            "ask_user_msg_counter": 0,
            "next_transcript_order_id": 0,
            "active_plan": None,
            "settings": {},
            "status": "draft",
        }

    def load_conversation_meta(self, conversation_id: str) -> ObjectMap:
        path = self.conversation_meta_path(conversation_id)
        if path.exists():
            try:
                data_obj = cast(object, json.loads(path.read_text(encoding="utf-8")))
                if isinstance(data_obj, dict):
                    return coerce_object_map(data_obj)
            except Exception:
                pass
        meta = self.default_conversation_meta(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return meta

    def save_conversation_meta(self, conversation_id: str, meta: ObjectMap) -> None:
        path = self.conversation_meta_path(conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        callback = self._meta_save_callback
        if callback is not None:
            with suppress(Exception):
                callback(conversation_id)

    def latest_legacy_transcript(self) -> Path | None:
        if not self.legacy_transcript_dir.exists():
            return None
        files = sorted(
            self.legacy_transcript_dir.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return files[0] if files else None


conversation_store = ConversationStore()
