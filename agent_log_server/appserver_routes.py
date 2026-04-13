from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional, cast

import extensions as ext_loader
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from agent_log_server import ask_user_interactions
from agent_log_server import conversation_todos as _conv_todos
from agent_log_server.ask_user_interactions import (
    AGENT_PTY_ASK_USER_REQUEST_METHOD,
)
from agent_log_server.prompt_context import load_repo_memory_snapshot
from agent_log_server.typing_helpers import (
    AsyncObjectCallable,
    ObjectEntriesWriter,
    ObjectList,
    ObjectMap,
    RequestId,
)


@dataclass
class AppserverRoutesState:
    draft_hash_cache: dict[str, str] = field(default_factory=dict)
    user_message_buffer: dict[str, ObjectMap] = field(default_factory=dict)


@dataclass(frozen=True)
class AppserverRoutesDeps:
    config_lock: asyncio.Lock
    load_appserver_config: Callable[[], ObjectMap]
    save_appserver_config: Callable[[ObjectMap], None]
    sync_conversation_index: Callable[[ObjectMap], list[str]]
    normalize_pinned_conversation_list: Callable[..., list[str]]
    conversation_display_order: Callable[[ObjectMap], list[str]]
    add_conversation_to_config: Callable[[str, ObjectMap], bool]
    remove_conversation_from_config: Callable[[str, ObjectMap], None]
    default_conversation_meta: Callable[[str], ObjectMap]
    latest_legacy_transcript: Callable[[], Optional[Path]]
    require_conversation_id: Callable[[], Awaitable[str]]
    ensure_conversation: Callable[[], Awaitable[str | None]]
    sanitize_conversation_id: Callable[[str], str]
    conversation_meta_path: Callable[[str], Path]
    conversation_dir: Callable[[str], Path]
    transcript_path: Callable[[str], Path]
    meta_settings: Callable[[ObjectMap], ObjectMap]
    load_conversation_meta: Callable[[str], ObjectMap]
    save_conversation_meta: Callable[[str, ObjectMap], None]
    coerce_json_object: Callable[[object], ObjectMap]
    validate_conversation_pending_approvals: AsyncObjectCallable
    ensure_pending_approvals: Callable[[ObjectMap], dict[str, ObjectMap]]
    find_pending_approval: Callable[[RequestId], tuple[str, ObjectMap] | None]
    remove_pending_approval: Callable[[str, RequestId], bool]
    build_approval_handoff_event: Callable[[str, ObjectMap, ObjectMap], ObjectMap | None]
    append_approval_handoff_transcript_entry: AsyncObjectCallable
    append_transcript_entry: AsyncObjectCallable
    write_transcript_entries: ObjectEntriesWriter
    is_internal_transcript_item: Callable[[object], bool]
    conversation_agent: Callable[[ObjectMap | None], str]
    extension_unavailable_detail: Callable[[str], str | None]
    emit_extension_unavailable_warning: AsyncObjectCallable
    default_active_extension_id: Callable[[], str | None]
    materialize_extension_runtime_settings: Callable[[ObjectMap | None], ObjectMap]
    merge_extension_bind_settings: Callable[..., ObjectMap]
    legacy_builtin_codex_disabled_result: Callable[..., ObjectMap]
    legacy_builtin_codex_disabled_detail: Callable[[], str]
    emit_command_result_mirror: AsyncObjectCallable
    broadcast_appserver_ui: AsyncObjectCallable
    logical_absolute_path: Callable[[str | None, str], Path]
    resolved_existing_path: Callable[[Path, Optional[Path]], Path]
    logical_alias_for_resolved_ancestor: Callable[[Path, Path, Path], Optional[Path]]
    detect_repo_root: Callable[[Path], Path]
    rg_list_files: Callable[[Path], list[str]]
    get_host_project_root: Callable[[], Optional[str]]
    utc_ts: Callable[[], str]
    write_codex_te2_mcp_config: Callable[[bool], None]
    get_debug_mode: Callable[[], bool]
    get_debug_raw_log_path: Callable[[], Optional[Path]]
    set_debug_mode: Callable[[bool], Optional[Path]]


class AppserverMessageIn(BaseModel):
    conversation_id: str
    text: str


class AppserverRoutes:
    def __init__(self, deps: AppserverRoutesDeps, state: AppserverRoutesState) -> None:
        self._deps = deps
        self._state = state

    def _buffer_user_message(self, conversation_id: str, text: str, agent_type: str) -> None:
        if not conversation_id:
            return
        self._state.user_message_buffer[conversation_id] = {
            "text": text if isinstance(text, str) else "",
            "agent_type": agent_type if isinstance(agent_type, str) else "",
        }

    def _clear_user_message_buffer(self, conversation_id: str) -> None:
        if conversation_id:
            self._state.user_message_buffer.pop(conversation_id, None)

    def _peek_user_message_buffer(self, conversation_id: str) -> Optional[str]:
        entry = self._state.user_message_buffer.get(conversation_id)
        if not isinstance(entry, dict):
            return None
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            return None
        return text

    async def _restore_buffered_user_message_draft(self, conversation_id: str) -> Optional[str]:
        buffered_text = self._peek_user_message_buffer(conversation_id)
        if not isinstance(buffered_text, str) or not buffered_text:
            return None
        await self.api_appserver_conversation_draft(
            {
                "conversation_id": conversation_id,
                "draft": buffered_text,
            }
        )
        return buffered_text

    async def _emit_conversation_error_event(
        self,
        conversation_id: str,
        *,
        message: object,
        source: Optional[str] = None,
        error_type: Optional[str] = None,
        status_code: int | float | None = None,
        provider_call_id: Optional[str] = None,
        details: Optional[str] = None,
        stack: Optional[str] = None,
        code: object = None,
        turn_id: Optional[str] = None,
    ) -> None:
        if not conversation_id:
            return
        message_text = str(message or "").strip() or "Message send failed"
        transcript_entry: ObjectMap = {
            "role": "error",
            "message": message_text,
            "text": message_text,
            "timestamp": self._deps.utc_ts(),
        }
        if isinstance(source, str) and source.strip():
            transcript_entry["source"] = source.strip()
        if isinstance(error_type, str) and error_type.strip():
            transcript_entry["error_type"] = error_type.strip()
        if isinstance(status_code, (int, float)):
            transcript_entry["status_code"] = int(status_code)
        if isinstance(provider_call_id, str) and provider_call_id.strip():
            transcript_entry["provider_call_id"] = provider_call_id.strip()
        if isinstance(details, str) and details.strip():
            transcript_entry["details"] = details
        if isinstance(stack, str) and stack.strip():
            transcript_entry["stack"] = stack
        if code is not None:
            transcript_entry["code"] = code
        if isinstance(turn_id, str) and turn_id.strip():
            transcript_entry["turn_id"] = turn_id.strip()
        await self._deps.append_transcript_entry(conversation_id, transcript_entry)

        event: ObjectMap = {
            "type": "error",
            "conversation_id": conversation_id,
            "message": message_text,
        }
        if isinstance(source, str) and source.strip():
            event["source"] = source.strip()
        if isinstance(error_type, str) and error_type.strip():
            event["error_type"] = error_type.strip()
        if isinstance(status_code, (int, float)):
            event["status_code"] = int(status_code)
        if isinstance(provider_call_id, str) and provider_call_id.strip():
            event["provider_call_id"] = provider_call_id.strip()
        if isinstance(details, str) and details.strip():
            event["details"] = details
        if isinstance(stack, str) and stack.strip():
            event["stack"] = stack
        if code is not None:
            event["code"] = code
        if isinstance(turn_id, str) and turn_id.strip():
            event["turn_id"] = turn_id.strip()
        await self._deps.broadcast_appserver_ui(event)
        await self._deps.broadcast_appserver_ui(
            {
                "type": "activity",
                "conversation_id": conversation_id,
                "label": "error",
                "active": False,
            }
        )

    async def _apply_send_message_contract(
        self,
        conversation_id: str,
        agent_type: str,
        result: object,
    ) -> ObjectMap:
        normalized = self._deps.coerce_json_object(result) if isinstance(result, dict) else {
            "ok": False,
            "error": "Invalid send result from agent handler",
        }
        if normalized.get("ok") is True:
            self._clear_user_message_buffer(conversation_id)
            return normalized

        restore_draft = normalized.get("restore_draft") is True
        if restore_draft:
            restored_text = await self._restore_buffered_user_message_draft(conversation_id)
            normalized["draft_restored"] = bool(restored_text)

        surface_error = normalized.get("surface_error")
        if surface_error is True or (restore_draft and surface_error is not False):
            error_source = normalized.get("error_source")
            source = error_source if isinstance(error_source, str) and error_source.strip() else agent_type
            error_type_value = normalized.get("error_type")
            failure_kind = normalized.get("failure_kind")
            resolved_error_type = (
                error_type_value
                if isinstance(error_type_value, str) and error_type_value.strip()
                else failure_kind if isinstance(failure_kind, str) and failure_kind.strip() else None
            )
            status_code_value = normalized.get("status_code")
            resolved_status_code = (
                status_code_value
                if isinstance(status_code_value, (int, float))
                else None
            )
            provider_call_id_value = normalized.get("provider_call_id")
            provider_call_id = (
                provider_call_id_value
                if isinstance(provider_call_id_value, str) and provider_call_id_value.strip()
                else None
            )
            details_value = normalized.get("details")
            details = details_value if isinstance(details_value, str) else None
            stack_value = normalized.get("stack")
            stack = stack_value if isinstance(stack_value, str) else None
            turn_id_value = normalized.get("turn_id")
            turn_id = turn_id_value if isinstance(turn_id_value, str) and turn_id_value.strip() else None
            await self._emit_conversation_error_event(
                conversation_id,
                message=normalized.get("error") or "Message send failed",
                source=source,
                error_type=resolved_error_type,
                status_code=resolved_status_code,
                provider_call_id=provider_call_id,
                details=details,
                stack=stack,
                code=normalized.get("code"),
                turn_id=turn_id,
            )

        self._clear_user_message_buffer(conversation_id)
        return normalized

    async def process_mention(self, payload: ObjectMap) -> ObjectMap:
        def _maybe_int(value: object) -> int | None:
            if value is None or not isinstance(value, (int, float, str)):
                return None
            return int(value)

        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Missing or invalid 'path'")
        path = path.strip()
        if "`" in path:
            raise ValueError("Invalid 'path' (backticks not supported)")
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id, str):
            raise ValueError("Invalid 'conversation_id'")

        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            active_conversation_id = cfg.get("conversation_id")
            active_view = cfg.get("active_view", "splash")

        if not conversation_id:
            conversation_id = active_conversation_id

        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("No active conversation selected")

        conversation_id = conversation_id.strip()

        if not self._deps.conversation_meta_path(conversation_id).exists():
            raise FileNotFoundError("Conversation not found")

        queued = True
        line_no = payload.get("lineNo")
        end_line_no = payload.get("endLineNo")
        col = payload.get("col")
        end_col = payload.get("endCol")
        content = payload.get("content")
        if isinstance(content, str):
            lines = content.split("\n")
            if len(lines) > 20:
                content = "\n".join(lines[:20]) + f"\n... (truncated, {len(lines)} total lines)"

        line_no_int = _maybe_int(line_no)
        end_line_no_int = _maybe_int(end_line_no)
        col_int = _maybe_int(col)
        end_col_int = _maybe_int(end_col)

        mention_evt: ObjectMap = {
            "type": "mention_insert",
            "path": path,
            "conversation_id": conversation_id,
        }
        if line_no_int is not None:
            mention_evt["lineNo"] = line_no_int
        if end_line_no_int is not None:
            mention_evt["endLineNo"] = end_line_no_int
        if col_int is not None:
            mention_evt["col"] = col_int
        if end_col_int is not None:
            mention_evt["endCol"] = end_col_int
        if content:
            mention_evt["content"] = str(content)

        if active_view == "conversation" and active_conversation_id == conversation_id:
            await self._deps.broadcast_appserver_ui(mention_evt)
            queued = False
        else:
            meta = self._deps.load_conversation_meta(conversation_id)
            draft = meta.get("draft")
            if not isinstance(draft, str):
                draft = ""
            token = self._encode_draft_mention_token(
                path,
                line_no=line_no_int,
                end_line_no=end_line_no_int,
                col=col_int,
                end_col=end_col_int,
                content=str(content) if isinstance(content, str) and content else None,
            )
            if not token:
                token = str(path or "")
            if draft and not draft.endswith((" ", "\n", "\t")):
                draft = draft + " " + token
            else:
                draft = draft + token
            meta["draft"] = draft
            self._deps.save_conversation_meta(conversation_id, meta)
            self._state.draft_hash_cache[conversation_id] = hashlib.sha256(draft.encode("utf-8")).hexdigest()

        return {
            "ok": True,
            "queued": queued,
            "conversation_id": conversation_id,
            "path": path,
        }

    @staticmethod
    def _encode_draft_mention_token(
        path: str,
        *,
        line_no: int | None = None,
        end_line_no: int | None = None,
        col: int | None = None,
        end_col: int | None = None,
        content: str | None = None,
    ) -> str:
        if not path:
            return ""
        suffix = ""
        if isinstance(line_no, int) and line_no > 0:
            suffix = f":{line_no}"
            if isinstance(col, int) and col > 0:
                suffix += f":{col}"
            if isinstance(end_line_no, int) and end_line_no > 0:
                suffix += f"-{end_line_no}"
                if isinstance(end_col, int) and end_col > 0:
                    suffix += f":{end_col}"
        token = f"`{path}{suffix}`"
        if isinstance(content, str) and content.strip():
            token += f"\n```\n{content}\n```"
        return token

    async def api_health(self) -> ObjectMap:
        return {"ok": True, "ts": self._deps.utc_ts()}

    async def api_appserver_config(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            ids = self._deps.sync_conversation_index(cfg)
            self._deps.normalize_pinned_conversation_list(cfg, ids)
            self._deps.save_appserver_config(cfg)
            return cfg

    async def api_appserver_conversation(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
        convo_id = cfg.get("conversation_id")
        meta: ObjectMap | None = None
        if isinstance(convo_id, str) and convo_id and self._deps.conversation_meta_path(convo_id).exists():
            validated = await self._deps.validate_conversation_pending_approvals(
                convo_id,
                self._deps.load_conversation_meta(convo_id),
            )
            meta = validated if isinstance(validated, dict) else None
        if not meta:
            meta = {
                "conversation_id": convo_id,
                "thread_id": None,
                "pending_approvals": {},
                "settings": {},
                "status": "none",
            }
        meta["active_view"] = cfg.get("active_view", "splash")
        return meta

    async def api_appserver_conversation_meta(self, conversation_id: str) -> ObjectMap:
        convo_id = self._deps.sanitize_conversation_id(conversation_id)
        if not convo_id or not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail="Conversation not found")
        validated = await self._deps.validate_conversation_pending_approvals(
            convo_id,
            self._deps.load_conversation_meta(convo_id),
        )
        return validated if isinstance(validated, dict) else self._deps.load_conversation_meta(convo_id)

    async def api_appserver_conversation_update(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        raw_conversation_id = payload.get("conversation_id")
        if not isinstance(raw_conversation_id, str) or not raw_conversation_id.strip():
            convo_id = await self._deps.require_conversation_id()
        else:
            convo_id = self._deps.sanitize_conversation_id(raw_conversation_id.strip())
            if not self._deps.conversation_meta_path(convo_id).exists():
                raise HTTPException(status_code=404, detail="Conversation not found")

        meta = self._deps.load_conversation_meta(convo_id)
        settings = payload.get("settings")
        meta_settings = self._deps.meta_settings(meta)
        if isinstance(settings, dict):
            for key, value in settings.items():
                if value is None or value == "":
                    if key in meta_settings:
                        meta_settings.pop(key, None)
                else:
                    meta_settings[key] = value
        meta["settings"] = meta_settings
        raw_thread_id = payload.get("thread_id")
        thread_id = raw_thread_id.strip() if isinstance(raw_thread_id, str) and raw_thread_id.strip() else None
        picked_session: Optional[str] = None
        if not thread_id and isinstance(settings, dict):
            session_value = settings.get("session")
            picked_session = session_value.strip() if isinstance(session_value, str) and session_value.strip() else None
            thread_id = picked_session
            if thread_id:
                meta_settings.pop("session", None)
                meta["settings"] = meta_settings
        if thread_id and not meta.get("thread_id"):
            meta["thread_id"] = thread_id
            meta["status"] = "active"
        self._deps.save_conversation_meta(convo_id, meta)
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self._deps.add_conversation_to_config(convo_id, cfg)
            if not (isinstance(raw_conversation_id, str) and raw_conversation_id.strip()):
                cfg["conversation_id"] = convo_id
                cfg["active_view"] = cfg.get("active_view") or "conversation"
            self._deps.save_appserver_config(cfg)

        final_settings = self._deps.meta_settings(meta)
        saved_agent = final_settings.get("agent")
        agent_type = saved_agent.strip() if isinstance(saved_agent, str) and saved_agent.strip() else ""
        if picked_session and agent_type and ext_loader.has_extension(agent_type):
            saved_cwd = final_settings.get("cwd")
            cwd = saved_cwd if isinstance(saved_cwd, str) and saved_cwd.strip() else "~"
            model_value = final_settings.get("model")
            model = model_value if isinstance(model_value, str) and model_value.strip() else None
            bind_settings = self._deps.merge_extension_bind_settings(
                convo_id,
                cwd=cwd,
                model=model,
                settings=final_settings,
            )
            try:
                bind_result = await ext_loader.resume_session_with_history(
                    agent_type,
                    session_id=picked_session,
                    conversation_id=convo_id,
                    cwd=cwd,
                    model=model,
                    settings=bind_settings,
                )
                if not bind_result.get("ok"):
                    print(f"[WARN] Session bind failed: {bind_result}")
                else:
                    items = await ext_loader.hydrate_transcript(
                        agent_type,
                        session_id=picked_session,
                        conversation_id=convo_id,
                        cwd=cwd,
                        model=model,
                        settings=bind_settings,
                    )
                    if items:
                        transcript_items = [item for item in items if isinstance(item, dict)]
                        await self._deps.write_transcript_entries(convo_id, transcript_items)
                        print(f"[INFO] Hydrated {len(transcript_items)} transcript entries for {convo_id[:8]}")
            except Exception as exc:
                print(f"[WARN] Session bind+hydrate failed: {exc}")
        return meta

    async def api_appserver_conversation_draft(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        draft = payload.get("draft", "")
        if not isinstance(draft, str):
            draft = ""

        requested_conversation_id = payload.get("conversation_id")
        if isinstance(requested_conversation_id, str) and requested_conversation_id.strip():
            convo_id = self._deps.sanitize_conversation_id(requested_conversation_id.strip())
        else:
            convo_id = await self._deps.require_conversation_id()

        if not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail="Conversation not found")

        draft_hash = hashlib.sha256(draft.encode("utf-8")).hexdigest()
        cached_hash = self._state.draft_hash_cache.get(convo_id)
        if cached_hash == draft_hash:
            return {"status": "unchanged", "conversation_id": convo_id}

        self._state.draft_hash_cache[convo_id] = draft_hash
        meta = self._deps.load_conversation_meta(convo_id)
        meta["draft"] = draft
        self._deps.save_conversation_meta(convo_id, meta)

        await self._deps.broadcast_appserver_ui(
            {
                "type": "draft_update",
                "conversation_id": convo_id,
                "draft": draft,
                "draft_hash": draft_hash,
            }
        )

        return {"status": "saved", "conversation_id": convo_id, "draft_hash": draft_hash}

    async def api_appserver_repo_memory(
        self,
        conversation_id: Optional[str] = Query(None),
    ) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()

        resolved_conversation_id = conversation_id
        if not isinstance(resolved_conversation_id, str) or not resolved_conversation_id.strip():
            cfg_conversation_id = cfg.get("conversation_id")
            resolved_conversation_id = cfg_conversation_id if isinstance(cfg_conversation_id, str) else None

        cfg_cwd = cfg.get("cwd")
        cwd = cfg_cwd if isinstance(cfg_cwd, str) and cfg_cwd.strip() else None
        if isinstance(resolved_conversation_id, str) and resolved_conversation_id:
            meta = self._deps.load_conversation_meta(resolved_conversation_id)
            settings = self._deps.meta_settings(meta)
            settings_cwd = settings.get("cwd")
            convo_cwd = settings_cwd if isinstance(settings_cwd, str) and settings_cwd.strip() else None
            if convo_cwd:
                cwd = convo_cwd

        if not isinstance(cwd, str) or not cwd.strip():
            host_project_root = self._deps.get_host_project_root()
            if isinstance(host_project_root, str) and host_project_root.strip():
                cwd = host_project_root

        snapshot = load_repo_memory_snapshot(cwd)
        snapshot["conversation_id"] = (
            resolved_conversation_id if isinstance(resolved_conversation_id, str) and resolved_conversation_id else None
        )
        return {"ok": True, **snapshot}

    async def api_appserver_conversations(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            ids = self._deps.conversation_display_order(cfg)
            self._deps.save_appserver_config(cfg)
        if not ids and self._deps.latest_legacy_transcript():
            await self._deps.ensure_conversation()
            async with self._deps.config_lock:
                cfg = self._deps.load_appserver_config()
                ids = self._deps.conversation_display_order(cfg)
                self._deps.save_appserver_config(cfg)
        pinned_ids = self._deps.normalize_pinned_conversation_list(cfg)
        pinned_set = set(pinned_ids)
        items: ObjectList = []
        for convo_id in ids:
            if not convo_id:
                continue
            if self._deps.conversation_meta_path(convo_id).exists():
                validated = await self._deps.validate_conversation_pending_approvals(
                    convo_id,
                    self._deps.load_conversation_meta(convo_id),
                )
                meta = validated if isinstance(validated, dict) else self._deps.load_conversation_meta(convo_id)
            else:
                meta = {
                    "conversation_id": convo_id,
                    "thread_id": None,
                    "pending_approvals": {},
                    "settings": {},
                    "status": "none",
                }
            entry = dict(meta)
            entry["pinned"] = convo_id in pinned_set
            items.append(entry)
        return {
            "items": items,
            "active_conversation_id": cfg.get("conversation_id"),
            "active_view": cfg.get("active_view", "splash"),
            "pinned_conversations": pinned_ids,
        }

    async def api_appserver_conversation_create(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> ObjectMap:
        convo_id = uuid.uuid4().hex
        meta = self._deps.default_conversation_meta(convo_id)
        if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
            meta["settings"] = self._deps.coerce_json_object(payload.get("settings"))
        self._deps.save_conversation_meta(convo_id, meta)
        meta_settings = self._deps.meta_settings(meta)
        cwd = meta_settings.get("cwd", "")
        if isinstance(cwd, str) and cwd.strip():
            _conv_todos.ensure_te2_dir(os.path.expanduser(cwd.strip()))
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self._deps.add_conversation_to_config(convo_id, cfg)
            cfg["conversation_id"] = convo_id
            cfg["active_view"] = "conversation"
            cfg["thread_id"] = meta.get("thread_id")
            self._deps.save_appserver_config(cfg)
        return meta

    async def api_appserver_conversation_select(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        convo_id = payload.get("conversation_id") or payload.get("id")
        if not isinstance(convo_id, str) or not convo_id.strip():
            raise HTTPException(status_code=400, detail="Missing conversation_id")
        convo_id = convo_id.strip()
        if not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail="Conversation not found")

        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            prev_convo_id = cfg.get("conversation_id")
        if (
            isinstance(prev_convo_id, str)
            and prev_convo_id
            and prev_convo_id != convo_id
            and self._deps.conversation_meta_path(prev_convo_id).exists()
        ):
            prev_meta = self._deps.load_conversation_meta(prev_convo_id)
            if prev_meta.get("status") == "draft" and not prev_meta.get("thread_id"):
                prev_path = self._deps.conversation_dir(prev_convo_id)
                if prev_path.exists():
                    for child in prev_path.glob("**/*"):
                        if child.is_file():
                            try:
                                child.unlink()
                            except Exception:
                                pass
                    try:
                        for child in sorted(prev_path.glob("**/*"), reverse=True):
                            if child.is_dir():
                                child.rmdir()
                        prev_path.rmdir()
                    except Exception:
                        pass
                    async with self._deps.config_lock:
                        cfg = self._deps.load_appserver_config()
                        self._deps.remove_conversation_from_config(prev_convo_id, cfg)
                        self._deps.save_appserver_config(cfg)

        meta = self._deps.load_conversation_meta(convo_id)
        agent = self._deps.conversation_agent(meta)
        unavailable_detail = self._deps.extension_unavailable_detail(agent)
        if unavailable_detail:
            raise HTTPException(status_code=409, detail=unavailable_detail)

        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self._deps.add_conversation_to_config(convo_id, cfg)
            cfg["conversation_id"] = convo_id
            view = payload.get("view")
            if view in {"splash", "conversation"}:
                cfg["active_view"] = view
            else:
                cfg["active_view"] = "conversation"
            cfg["thread_id"] = meta.get("thread_id")
            self._deps.save_appserver_config(cfg)
        return meta

    async def api_appserver_conversation_pins(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        requested = payload.get("pinned_conversations")
        if not isinstance(requested, list):
            raise HTTPException(status_code=400, detail="pinned_conversations must be a list")
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            valid_ids = self._deps.sync_conversation_index(cfg)
            valid_set = set(valid_ids)
            pinned: list[str] = []
            for item in requested:
                if not isinstance(item, str) or not item.strip():
                    continue
                convo_id = self._deps.sanitize_conversation_id(item.strip())
                if not convo_id or convo_id not in valid_set or convo_id in pinned:
                    continue
                pinned.append(convo_id)
            cfg["pinned_conversations"] = pinned
            self._deps.save_appserver_config(cfg)
        return {"ok": True, "pinned_conversations": pinned}

    async def api_appserver_conversation_delete(self, conversation_id: str) -> ObjectMap:
        if not conversation_id:
            raise HTTPException(status_code=400, detail="Missing conversation_id")
        convo_id = self._deps.sanitize_conversation_id(conversation_id)
        path = self._deps.conversation_dir(convo_id)
        if path.exists():
            for child in path.glob("**/*"):
                if child.is_file():
                    try:
                        child.unlink()
                    except Exception:
                        pass
            try:
                for child in sorted(path.glob("**/*"), reverse=True):
                    if child.is_dir():
                        child.rmdir()
                path.rmdir()
            except Exception:
                pass
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self._deps.remove_conversation_from_config(convo_id, cfg)
            if cfg.get("conversation_id") == convo_id:
                cfg["conversation_id"] = None
                cfg["thread_id"] = None
                cfg["active_view"] = "splash"
            self._deps.save_appserver_config(cfg)
        return {"ok": True}

    async def api_appserver_set_view(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        view = payload.get("view")
        if view not in {"splash", "conversation"}:
            raise HTTPException(status_code=400, detail="view must be 'splash' or 'conversation'")
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            cfg["active_view"] = view
            self._deps.save_appserver_config(cfg)
            return cfg

    async def api_fs_list(self, path: Optional[str] = Query(None)) -> ObjectMap:
        logical = self._deps.logical_absolute_path(path, "~")
        resolved = self._deps.resolved_existing_path(logical, self._deps.logical_absolute_path("~", "~"))
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="Path not found")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")
        items: ObjectList = []
        try:
            with os.scandir(resolved) as it:
                for entry in it:
                    try:
                        is_link = entry.is_symlink()
                        is_dir = entry.is_dir(follow_symlinks=True)
                        is_file = entry.is_file(follow_symlinks=True)
                    except Exception:
                        is_dir = False
                        is_file = False
                        is_link = False
                    if is_dir:
                        entry_type = "directory"
                    elif is_file:
                        entry_type = "file"
                    elif is_link:
                        entry_type = "symlink"
                    else:
                        entry_type = "other"
                    items.append(
                        {
                            "name": entry.name,
                            "path": str(logical / entry.name),
                            "type": entry_type,
                            "is_symlink": is_link,
                        }
                    )
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to list directory") from exc

        items.sort(
            key=lambda item: (
                0 if item.get("type") == "directory" else 1,
                str(item.get("name") or "").lower(),
            )
        )
        parent = str(logical.parent) if logical.parent != logical else None
        return {"path": str(logical), "parent": parent, "items": items}

    async def api_fs_search(
        self,
        query: str = Query(...),
        root: Optional[str] = Query(None),
        limit: int = Query(200, gt=0),
    ) -> ObjectMap:
        if not query.strip():
            return {"root": None, "items": []}
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise HTTPException(status_code=400, detail="Invalid regex") from exc
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
        cfg_cwd = cfg.get("cwd")
        base = root or (cfg_cwd if isinstance(cfg_cwd, str) else None) or os.getcwd()
        logical_base = self._deps.logical_absolute_path(base, os.getcwd())
        resolved = self._deps.resolved_existing_path(
            logical_base,
            self._deps.logical_absolute_path(os.getcwd(), os.getcwd()),
        )
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="Root not found")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="Root is not a directory")
        repo_root = self._deps.detect_repo_root(resolved)
        logical_repo_root = self._deps.logical_alias_for_resolved_ancestor(logical_base, resolved, repo_root)
        if logical_repo_root is None:
            repo_root = resolved
            logical_repo_root = logical_base
        try:
            files = self._deps.rg_list_files(repo_root)
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Failed to search repo") from exc
        items: ObjectList = []
        seen: set[str] = set()
        for rel in files:
            full_path = repo_root / rel
            full = str(self._deps.resolved_existing_path(full_path, full_path))
            logical_full = str(logical_repo_root / rel)
            if pattern.search(rel) or pattern.search(full):
                if full not in seen:
                    seen.add(full)
                    items.append({"name": Path(rel).name, "path": logical_full, "type": "file"})
                    if len(items) >= limit:
                        break
            for parent in Path(rel).parents:
                if parent == Path("."):
                    continue
                parent_rel = str(parent)
                parent_full_path = repo_root / parent_rel
                parent_full = str(self._deps.resolved_existing_path(parent_full_path, parent_full_path))
                logical_parent = str(logical_repo_root / parent_rel)
                if parent_full in seen:
                    continue
                if pattern.search(parent_rel) or pattern.search(parent_full):
                    seen.add(parent_full)
                    items.append({"name": Path(parent_rel).name, "path": logical_parent, "type": "directory"})
                    if len(items) >= limit:
                        break
            if len(items) >= limit:
                break
        items.sort(
            key=lambda item: (
                0 if item.get("type") == "directory" else 1,
                str(item.get("name") or "").lower(),
            )
        )
        return {"root": str(logical_repo_root), "items": items}

    def _read_transcript_range_data(
        self,
        conversation_id: str,
        *,
        offset: int,
        limit: int,
        include_internal: bool,
    ) -> ObjectMap:
        path = self._deps.transcript_path(str(conversation_id))
        if not path.exists():
            return {"conversation_id": str(conversation_id), "total": 0, "offset": 0, "items": []}

        total = 0
        items: ObjectList = []
        if offset < 0:
            buf: deque[ObjectMap] = deque(maxlen=limit)
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record_raw = cast(object, json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record_raw, dict):
                        continue
                    record = self._deps.coerce_json_object(record_raw)
                    if not include_internal and self._deps.is_internal_transcript_item(record):
                        continue
                    total += 1
                    buf.append(record)
            items = list(buf)
            offset = max(0, total - len(items))
        else:
            start = max(0, offset)
            end = start + limit
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record_raw = cast(object, json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record_raw, dict):
                        continue
                    record = self._deps.coerce_json_object(record_raw)
                    if not include_internal and self._deps.is_internal_transcript_item(record):
                        continue
                    if start <= total < end:
                        items.append(record)
                    total += 1
            offset = start

        return {
            "conversation_id": str(conversation_id),
            "total": total,
            "offset": offset,
            "items": items,
        }

    async def api_appserver_transcript(
        self,
        conversation_id: Optional[str] = Query(None),
    ) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            convo_id = conversation_id or cfg.get("conversation_id")
        if not convo_id:
            return {"conversation_id": None, "items": []}
        path = self._deps.transcript_path(str(convo_id))
        if not path.exists():
            return {"conversation_id": str(convo_id), "items": []}
        items: ObjectList = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record_raw = cast(object, json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record_raw, dict):
                        record = self._deps.coerce_json_object(record_raw)
                        items.append(record)
        except Exception:
            return {"conversation_id": str(convo_id), "items": []}
        return {"conversation_id": str(convo_id), "items": items}

    async def api_appserver_transcript_range(
        self,
        conversation_id: Optional[str] = Query(None),
        offset: int = Query(0),
        limit: int = Query(120, gt=0, le=500),
        include_internal: bool = Query(False),
    ) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            convo_id = conversation_id or cfg.get("conversation_id")
        if not convo_id:
            return {"conversation_id": None, "total": 0, "offset": 0, "items": []}
        return self._read_transcript_range_data(
            str(convo_id),
            offset=offset,
            limit=limit,
            include_internal=include_internal,
        )

    async def api_appserver_config_update(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Config payload must be a JSON object")
        if "user_name" in payload:
            user_name = payload.get("user_name")
            if user_name is None:
                payload["user_name"] = None
            elif isinstance(user_name, str):
                payload["user_name"] = user_name.strip() or None
            else:
                raise HTTPException(status_code=400, detail="user_name must be a string or null")
        if "te2_mcp_integration" in payload:
            payload["te2_mcp_integration"] = payload.get("te2_mcp_integration") is True
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            cfg.update(payload)
            if "te2_mcp_integration" in payload:
                self._deps.write_codex_te2_mcp_config(cfg.get("te2_mcp_integration") is True)
            self._deps.save_appserver_config(cfg)
            return cfg

    async def api_appserver_set_cwd(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        cwd = payload.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            raise HTTPException(status_code=400, detail="Missing or invalid 'cwd'")
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            cfg["cwd"] = cwd
            self._deps.save_appserver_config(cfg)
        convo_id = await self._deps.require_conversation_id()
        meta = self._deps.load_conversation_meta(convo_id)
        settings = self._deps.meta_settings(meta)
        settings["cwd"] = cwd
        meta["settings"] = settings
        self._deps.save_conversation_meta(convo_id, meta)
        _conv_todos.ensure_te2_dir(os.path.expanduser(cwd.strip()))
        return cfg

    async def api_appserver_thread_start(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            thread_id = None
            if isinstance(payload, dict):
                thread_id = payload.get("thread_id") or payload.get("id")
            if thread_id:
                cfg["thread_id"] = thread_id
                self._deps.save_appserver_config(cfg)
            return {"ok": True, "thread_id": cfg.get("thread_id"), "note": "stub"}

    async def api_appserver_thread_kill(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            cfg["thread_id"] = None
            cfg["turn_id"] = None
            self._deps.save_appserver_config(cfg)
            return {"ok": True}

    async def api_appserver_stop(self) -> ObjectMap:
        return self._deps.legacy_builtin_codex_disabled_result(ok=True)

    async def api_appserver_start(self) -> ObjectMap:
        return self._deps.legacy_builtin_codex_disabled_result(ok=True, running=False)

    async def api_appserver_status(self) -> ObjectMap:
        return self._deps.legacy_builtin_codex_disabled_result(ok=True, running=False, shell_id=None)

    async def api_appserver_message(self, payload: AppserverMessageIn) -> ObjectMap:
        convo_id = payload.conversation_id
        text = payload.text
        if not convo_id or not text:
            raise HTTPException(status_code=400, detail="conversation_id and text required")
        if not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail=f"Conversation not found: {convo_id}")
        meta = self._deps.load_conversation_meta(convo_id)
        if not meta:
            raise HTTPException(status_code=404, detail=f"Conversation not found: {convo_id}")

        settings_raw = meta.get("settings")
        settings = settings_raw if isinstance(settings_raw, dict) else {}
        agent_type = self._deps.conversation_agent(meta)
        runtime_settings = self._deps.materialize_extension_runtime_settings(settings)
        if ext_loader.has_extension(agent_type):
            self._buffer_user_message(convo_id, text, agent_type)
            try:
                result = await ext_loader.handle_message(
                    agent_type,
                    convo_id,
                    text,
                    runtime_settings,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            return await self._apply_send_message_contract(convo_id, agent_type, result)

        unavailable_detail = self._deps.extension_unavailable_detail(agent_type)
        detail = (
            self._deps.legacy_builtin_codex_disabled_detail()
            if agent_type == "codex"
            else (unavailable_detail or self._deps.legacy_builtin_codex_disabled_detail())
        )
        self._buffer_user_message(convo_id, text, agent_type)
        await self._deps.emit_extension_unavailable_warning(convo_id, agent_type or "unknown", detail=detail)
        return await self._apply_send_message_contract(
            convo_id,
            agent_type,
            self._deps.legacy_builtin_codex_disabled_result(
                error=detail,
                legacy_disabled=(agent_type == "codex"),
                restore_draft=True,
                surface_error=True,
                error_source=agent_type or "codex",
                error_type=(
                    "legacy_builtin_disabled"
                    if agent_type == "codex"
                    else "extension_unavailable"
                ),
                ),
            )

    async def _rpc_conversation_send(
        self,
        params: ObjectMap,
    ) -> ObjectMap:
        conversation_id_raw = params.get("conversation_id")
        text_raw = params.get("text")
        conversation_id = (
            self._deps.sanitize_conversation_id(conversation_id_raw.strip())
            if isinstance(conversation_id_raw, str) and conversation_id_raw.strip()
            else ""
        )
        text = text_raw if isinstance(text_raw, str) else ""
        if not conversation_id or not text:
            raise HTTPException(status_code=400, detail="conversation_id and text required")

        result = await self.api_appserver_message(
            AppserverMessageIn(conversation_id=conversation_id, text=text),
        )
        normalized = dict(result) if isinstance(result, dict) else {
            "ok": False,
            "error": "Invalid send result",
        }
        normalized.setdefault("conversation_id", conversation_id)
        normalized["accepted"] = normalized.get("ok") is True
        return normalized

    async def _rpc_conversation_interrupt(
        self,
        params: ObjectMap,
    ) -> ObjectMap:
        result = await self.api_appserver_interrupt(params)
        return self._deps.coerce_json_object(result) if isinstance(result, dict) else {
            "ok": False,
            "error": "Invalid interrupt result",
        }

    async def _rpc_conversation_compact(
        self,
        params: ObjectMap,
    ) -> ObjectMap:
        result = await self.api_appserver_compact(params)
        return self._deps.coerce_json_object(result) if isinstance(result, dict) else {
            "ok": False,
            "error": "Invalid compact result",
        }

    def _jsonrpc_success(self, request_id: RequestId, result: ObjectMap) -> ObjectMap:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }

    def _jsonrpc_error(
        self,
        request_id: RequestId,
        *,
        code: int,
        message: str,
        data: ObjectMap | None = None,
    ) -> ObjectMap:
        error: ObjectMap = {
            "code": code,
            "message": message,
        }
        if isinstance(data, dict) and data:
            error["data"] = data
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": error,
        }

    def _jsonrpc_error_from_http_exception(
        self,
        request_id: RequestId,
        *,
        method: str,
        exc: HTTPException,
    ) -> ObjectMap:
        status_code = int(getattr(exc, "status_code", 500) or 500)
        detail = exc.detail
        message = detail if isinstance(detail, str) and detail.strip() else "Request failed"
        if status_code == 400:
            code = -32602
            error_code = "INVALID_REQUEST"
        elif status_code == 404:
            code = -32004
            error_code = "NOT_FOUND"
        elif status_code == 409:
            code = -32009
            error_code = "CONFLICT"
        else:
            code = -32603
            error_code = "INTERNAL_ERROR"
        return self._jsonrpc_error(
            request_id,
            code=code,
            message=message,
            data={
                "code": error_code,
                "status_code": status_code,
                "method": method,
            },
        )

    async def _rpc_conversation_replay_get_chunk(
        self,
        params: ObjectMap,
    ) -> ObjectMap:
        def _parse_int(value: object, *, detail: str, default: int | None = None) -> int:
            candidate = default if value is None else value
            if isinstance(candidate, bool):
                raise HTTPException(status_code=400, detail=detail)
            if isinstance(candidate, int):
                return candidate
            if isinstance(candidate, str):
                try:
                    return int(candidate)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=detail) from exc
            raise HTTPException(status_code=400, detail=detail)

        conversation_id_raw = params.get("conversation_id")
        conversation_id = str(conversation_id_raw or "").strip()
        if conversation_id:
            conversation_id = self._deps.sanitize_conversation_id(conversation_id)
        else:
            async with self._deps.config_lock:
                cfg = self._deps.load_appserver_config()
                active_conversation_id = cfg.get("conversation_id")
            if isinstance(active_conversation_id, str) and active_conversation_id.strip():
                conversation_id = active_conversation_id.strip()

        cursor = params.get("cursor")
        if cursor is None:
            cursor = {}
        if not isinstance(cursor, dict):
            raise HTTPException(status_code=400, detail="cursor must be an object")
        cursor_map = self._deps.coerce_json_object(cursor)
        offset = _parse_int(cursor_map.get("offset", 0), detail="cursor.offset must be an integer")
        max_entries = _parse_int(params.get("max_entries", 500), detail="max_entries must be an integer")
        max_entries = min(max(max_entries, 1), 500)
        max_bytes = _parse_int(params.get("max_bytes", 524288), detail="max_bytes must be an integer")
        max_bytes = max(max_bytes, 1)

        format_name = str(params.get("format", "jsonl") or "jsonl").strip().lower()
        if format_name != "jsonl":
            raise HTTPException(status_code=400, detail="Only format=jsonl is supported")

        include_internal = params.get("include_internal") is True
        if not conversation_id:
            return {
                "conversation_id": None,
                "replay_id": f"replay_{uuid.uuid4().hex[:12]}",
                "frame": {
                    "format": "jsonl",
                    "offset": 0,
                    "item_count": 0,
                    "total_count": 0,
                    "chunk_index": 0,
                    "complete": True,
                    "next_cursor": None,
                    "jsonl": "",
                },
            }

        range_data = self._read_transcript_range_data(
            conversation_id,
            offset=offset,
            limit=max_entries,
            include_internal=include_internal,
        )
        items_value = range_data.get("items")
        items: ObjectList = []
        if isinstance(items_value, list):
            items = [self._deps.coerce_json_object(item) for item in items_value if isinstance(item, dict)]
        actual_offset = _parse_int(range_data.get("offset"), detail="Invalid replay offset", default=0)
        total_count = _parse_int(range_data.get("total"), detail="Invalid replay total", default=0)

        jsonl_parts: list[str] = []
        kept_item_count = 0
        encoded_bytes = 0
        for item in items:
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            line_size = len(line.encode("utf-8"))
            if kept_item_count > 0 and encoded_bytes + line_size > max_bytes:
                break
            jsonl_parts.append(line)
            kept_item_count += 1
            encoded_bytes += line_size

        if not jsonl_parts and items:
            jsonl_parts.append(json.dumps(items[0], ensure_ascii=False, separators=(",", ":")) + "\n")
            kept_item_count = 1

        next_offset = actual_offset + kept_item_count
        complete = next_offset >= total_count
        return {
            "conversation_id": range_data["conversation_id"],
            "replay_id": f"replay_{uuid.uuid4().hex[:12]}",
            "frame": {
                "format": "jsonl",
                "offset": actual_offset,
                "item_count": kept_item_count,
                "total_count": total_count,
                "chunk_index": actual_offset // max(max_entries, 1),
                "complete": complete,
                "next_cursor": None if complete else {"offset": next_offset},
                "jsonl": "".join(jsonl_parts),
            },
        }

    async def api_appserver_rpc(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        return self._deps.legacy_builtin_codex_disabled_result()

    async def api_conversations_rpc(
        self,
        payload: ObjectMap,
    ) -> ObjectMap:
        request_id_raw = payload.get("id") if isinstance(payload, dict) else None
        request_id: RequestId = request_id_raw if isinstance(request_id_raw, (str, int)) else None
        if not isinstance(payload, dict):
            return self._jsonrpc_error(
                request_id,
                code=-32600,
                message="Invalid request",
                data={"code": "INVALID_REQUEST", "reason": "Payload must be an object"},
            )

        if payload.get("jsonrpc") != "2.0":
            return self._jsonrpc_error(
                request_id,
                code=-32600,
                message="Invalid request",
                data={"code": "INVALID_REQUEST", "reason": "jsonrpc must be '2.0'"},
            )

        method = payload.get("method")
        if not isinstance(method, str) or not method.strip():
            return self._jsonrpc_error(
                request_id,
                code=-32600,
                message="Invalid request",
                data={"code": "INVALID_REQUEST", "reason": "method is required"},
            )

        params = payload.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._jsonrpc_error(
                request_id,
                code=-32602,
                message="Invalid params",
                data={"code": "INVALID_REQUEST", "reason": "params must be an object"},
            )

        handlers: dict[str, Callable[[ObjectMap], Awaitable[ObjectMap]]] = {
            "conversation.send": self._rpc_conversation_send,
            "conversation.interrupt": self._rpc_conversation_interrupt,
            "conversation.compact": self._rpc_conversation_compact,
            "conversation.replay.getChunk": self._rpc_conversation_replay_get_chunk,
        }
        handler = handlers.get(method)
        if handler is None:
            return self._jsonrpc_error(
                request_id,
                code=-32601,
                message="Method not found",
                data={"code": "NOT_FOUND", "method": method},
            )

        try:
            result = await handler(params)
        except HTTPException as exc:
            return self._jsonrpc_error_from_http_exception(
                request_id,
                method=method,
                exc=exc,
            )
        except Exception as exc:
            return self._jsonrpc_error(
                request_id,
                code=-32603,
                message="Internal error",
                data={"code": "INTERNAL_ERROR", "reason": str(exc)},
            )
        return self._jsonrpc_success(request_id, result)

    async def api_appserver_approval_record(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        status = payload.get("status")
        diff = payload.get("diff")
        path = payload.get("path")
        request_id = payload.get("request_id", payload.get("item_id"))
        result_payload = payload.get("result") if isinstance(payload.get("result"), dict) else None
        request_method = payload.get("request_method")
        request_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else None
        decision = payload.get("decision")
        if decision is None and isinstance(result_payload, dict):
            decision = result_payload.get("decision")
        if status not in ("accepted", "declined", "cancelled"):
            raise HTTPException(status_code=400, detail="Invalid status")
        cfg = self._deps.load_appserver_config()
        convo_id = cfg.get("conversation_id")
        if isinstance(convo_id, str) and convo_id and self._deps.conversation_meta_path(convo_id).exists() and request_id is not None:
            meta = self._deps.load_conversation_meta(convo_id)
            pending = self._deps.ensure_pending_approvals(meta)
            if str(request_id).strip() not in pending:
                return {"ok": True, "skipped": True}
        if isinstance(convo_id, str) and convo_id:
            await self._deps.append_transcript_entry(
                convo_id,
                {
                    "role": "approval",
                    "status": status,
                    "decision": decision,
                    "result": result_payload,
                    "request_method": request_method,
                    "payload": request_payload,
                    "diff": diff,
                    "path": path,
                    "request_id": request_id,
                    "item_id": request_id,
                    "turn_id": payload.get("turn_id"),
                    "event": "approval_decision",
                },
            )
        if status == "declined" and diff:
            await self._deps.broadcast_appserver_ui(
                {
                    "type": "diff_declined",
                    "id": request_id,
                    "text": diff,
                    "path": path,
                }
            )
        return {"ok": True}

    async def api_appserver_approval_response(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        request_id_raw = payload.get("request_id", payload.get("requestId", payload.get("id")))
        request_id = str(request_id_raw or "").strip()
        if not request_id:
            raise HTTPException(status_code=400, detail="Missing request_id")
        resolution = self._deps.coerce_json_object(payload.get("result"))
        for key in ("decision", "kind", "rules", "feedback", "message", "path"):
            if key in payload and key not in resolution:
                resolution[key] = payload.get(key)
        conversation_id = payload.get("conversation_id")
        descriptor: ObjectMap | None = None
        meta: ObjectMap | None = None
        print(
            f"[ask_user server] approval_response request_id={request_id} conversation_id={conversation_id or '-'} payload_keys={sorted(payload.keys())}",
            flush=True,
        )

        if isinstance(conversation_id, str) and conversation_id.strip():
            requested_conversation_id = self._deps.sanitize_conversation_id(conversation_id.strip())
            if requested_conversation_id and self._deps.conversation_meta_path(requested_conversation_id).exists():
                validated = await self._deps.validate_conversation_pending_approvals(
                    requested_conversation_id,
                    self._deps.load_conversation_meta(requested_conversation_id),
                )
                meta = validated if isinstance(validated, dict) else self._deps.load_conversation_meta(requested_conversation_id)
                pending = self._deps.ensure_pending_approvals(meta)
                pending_descriptor = pending.get(request_id)
                descriptor = pending_descriptor if isinstance(pending_descriptor, dict) else None
                if isinstance(descriptor, dict):
                    conversation_id = requested_conversation_id

        if not isinstance(descriptor, dict):
            found = self._deps.find_pending_approval(request_id)
            if not isinstance(found, tuple) or len(found) != 2:
                raise HTTPException(status_code=409, detail="Approval is no longer pending")
            conversation_id, _ = found
            validated = await self._deps.validate_conversation_pending_approvals(
                conversation_id,
                self._deps.load_conversation_meta(conversation_id),
            )
            meta = validated if isinstance(validated, dict) else self._deps.load_conversation_meta(conversation_id)
            pending = self._deps.ensure_pending_approvals(meta)
            pending_descriptor = pending.get(request_id)
            descriptor = pending_descriptor if isinstance(pending_descriptor, dict) else None
            if not isinstance(descriptor, dict):
                raise HTTPException(status_code=409, detail="Approval is no longer pending")

        meta_settings = self._deps.meta_settings(meta or {})
        agent = str(descriptor.get("agent") or meta_settings.get("agent") or "codex").strip() or "codex"
        request_method = str(descriptor.get("request_method") or "").strip().lower()
        print(
            f"[ask_user server] approval_response matched request_id={request_id} conversation_id={conversation_id} agent={agent} request_method={request_method}",
            flush=True,
        )
        if request_method == AGENT_PTY_ASK_USER_REQUEST_METHOD:
            print(
                f"[ask_user server] approval_response submit request_id={request_id} result={resolution!r}",
                flush=True,
            )
            submitted = await ask_user_interactions.submit_user_response(request_id, resolution)
            if not submitted.get("ok"):
                print(
                    f"[ask_user server] approval_response submit_failed request_id={request_id} error={submitted.get('error')!r}",
                    flush=True,
                )
                raise HTTPException(
                    status_code=409,
                    detail=submitted.get("error") or "Approval is stale or no longer actionable",
                )
            print(
                f"[ask_user server] approval_response submitted request_id={request_id} awaiting_harness_ack={submitted.get('awaiting_harness_ack')}",
                flush=True,
            )
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "request_id": request_id,
                "result": resolution,
                "awaiting_harness_ack": True,
            }

        resolved = False
        if agent == "codex":
            self._deps.remove_pending_approval(str(conversation_id), request_id)
            raise HTTPException(status_code=409, detail=self._deps.legacy_builtin_codex_disabled_detail())
        if ext_loader.has_extension(agent):
            resolved = bool(ext_loader.resolve_approval(agent, request_id, resolution))
        else:
            self._deps.remove_pending_approval(str(conversation_id), request_id)
            raise HTTPException(status_code=409, detail=f"No approval resolver for agent: {agent}")

        if not resolved:
            self._deps.remove_pending_approval(str(conversation_id), request_id)
            raise HTTPException(status_code=409, detail="Approval is stale or no longer actionable")

        handoff_event = self._deps.build_approval_handoff_event(str(conversation_id), descriptor, resolution)
        if isinstance(handoff_event, dict):
            await self._deps.append_approval_handoff_transcript_entry(str(conversation_id), handoff_event)
        self._deps.remove_pending_approval(str(conversation_id), request_id)
        if isinstance(handoff_event, dict):
            handoff_event_dict = handoff_event
            await self._deps.broadcast_appserver_ui(handoff_event_dict)
            payload_value = handoff_event_dict.get("payload")
            handoff_payload = payload_value if isinstance(payload_value, dict) else {}
            diff = handoff_event_dict.get("diff") or handoff_payload.get("diff")
            path = handoff_event_dict.get("path") or handoff_payload.get("path")
            if handoff_event_dict.get("status") == "declined" and diff:
                await self._deps.broadcast_appserver_ui(
                    {
                        "type": "diff_declined",
                        "id": request_id,
                        "text": diff,
                        "path": path,
                        "conversation_id": conversation_id,
                    }
                )
        decision = resolution.get("decision")
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "decision": decision,
            "result": resolution,
            "handoff_event": handoff_event,
        }

    async def api_appserver_interrupt(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> ObjectMap:
        convo_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        if not isinstance(convo_id, str) or not convo_id:
            raise HTTPException(status_code=400, detail="Missing required field: conversation_id")
        convo_id = self._deps.sanitize_conversation_id(convo_id)
        if not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail=f"Conversation not found: {convo_id}")
        meta = self._deps.load_conversation_meta(convo_id)
        agent_type = self._deps.conversation_agent(meta)
        unavailable_detail = self._deps.extension_unavailable_detail(agent_type)
        if unavailable_detail and agent_type != "codex":
            raise HTTPException(status_code=409, detail=unavailable_detail)
        if ext_loader.has_extension(agent_type):
            result = self._deps.coerce_json_object(await ext_loader.interrupt_session(agent_type, convo_id))
            if not result.get("ok"):
                error_value = result.get("error", "Interrupt failed")
                error_detail = error_value if isinstance(error_value, str) and error_value else "Interrupt failed"
                raise HTTPException(status_code=409, detail=error_detail)
            await ask_user_interactions.cancel_interactions(
                conversation_id=convo_id,
                turn_id=meta.get("turn_id"),
                resolution={"status": "interrupted"},
            )
            return result

        return self._deps.legacy_builtin_codex_disabled_result(
            conversation_id=convo_id,
            thread_id=meta.get("thread_id"),
            turn_id=meta.get("turn_id"),
        )

    async def api_appserver_shell_exec(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> ObjectMap:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        command = payload.get("command", "")
        if not command:
            raise HTTPException(status_code=400, detail="No command provided")
        detail = self._deps.legacy_builtin_codex_disabled_detail()
        return self._deps.legacy_builtin_codex_disabled_result(
            exitCode=1,
            stdout="",
            stderr=detail,
            error=detail,
        )

    async def api_appserver_compact(
        self,
        payload: Annotated[ObjectMap | None, Body()] = None,
    ) -> ObjectMap:
        convo_id = payload.get("conversation_id") if isinstance(payload, dict) else None
        if not isinstance(convo_id, str) or not convo_id:
            raise HTTPException(status_code=400, detail="Missing required field: conversation_id")
        convo_id = self._deps.sanitize_conversation_id(convo_id)
        if not self._deps.conversation_meta_path(convo_id).exists():
            raise HTTPException(status_code=404, detail=f"Conversation not found: {convo_id}")
        meta = self._deps.load_conversation_meta(convo_id)
        thread_id = meta.get("thread_id")
        if not thread_id:
            raise HTTPException(status_code=409, detail="No active thread to compact")

        agent_type = self._deps.conversation_agent(meta)
        unavailable_detail = self._deps.extension_unavailable_detail(agent_type)
        if unavailable_detail and agent_type != "codex":
            raise HTTPException(status_code=409, detail=unavailable_detail)
        if ext_loader.has_extension(agent_type):
            await self._deps.emit_command_result_mirror(
                convo_id,
                command="context compact",
                output=f"request: sending thread/compact/start for thread {thread_id}",
                event="thread/compact/start/request",
                source="system",
                shared_fields={"thread_id": thread_id, "phase": "request"},
            )
            result = self._deps.coerce_json_object(await ext_loader.compact_session(agent_type, convo_id))
            if not result.get("ok"):
                error_value = result.get("error", "compact failed")
                error_detail = error_value if isinstance(error_value, str) and error_value else "compact failed"
                await self._deps.emit_command_result_mirror(
                    convo_id,
                    command="context compact",
                    output=f"error: thread/compact/start failed for thread {thread_id}: {error_detail}",
                    event="thread/compact/start/error",
                    exit_code=1,
                    source="system",
                    shared_fields={"thread_id": thread_id, "phase": "error", "error": True},
                )
                raise HTTPException(status_code=500, detail=f"thread/compact/start failed: {error_detail}")
            await self._deps.emit_command_result_mirror(
                convo_id,
                command="context compact",
                output=f"response: app-server accepted thread/compact/start for thread {thread_id}; waiting for thread/compacted",
                event="thread/compact/start/response",
                source="system",
                shared_fields={"thread_id": thread_id, "phase": "response"},
            )
            return {"ok": True, "thread_id": thread_id, "conversation_id": convo_id}

        return self._deps.legacy_builtin_codex_disabled_result(
            conversation_id=convo_id,
            thread_id=thread_id,
        )

    async def api_appserver_mention(
        self,
        payload: Annotated[ObjectMap, Body(...)],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        try:
            result = await self.process_mention(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(
            result,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def api_appserver_mention_options(self) -> Response:
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, PUT, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "86400",
            },
        )

    async def api_appserver_initialize(self) -> ObjectMap:
        return self._deps.legacy_builtin_codex_disabled_result(ok=True)

    async def api_appserver_models(self) -> ObjectMap:
        return self._deps.legacy_builtin_codex_disabled_result(ok=True, data=[])

    async def api_appserver_runtime_options(
        self,
        conversation_id: Optional[str] = Query(None),
        agent: Optional[str] = Query(None),
    ) -> ObjectMap:
        resolved_agent = str(agent or "").strip()
        resolved_conversation_id = str(conversation_id or "").strip()
        meta: ObjectMap | None = None

        if resolved_conversation_id:
            safe_id = self._deps.sanitize_conversation_id(resolved_conversation_id)
            if safe_id and self._deps.conversation_meta_path(safe_id).exists():
                resolved_conversation_id = safe_id
                meta = self._deps.load_conversation_meta(safe_id)
                if not resolved_agent:
                    settings = self._deps.meta_settings(meta)
                    saved_agent = settings.get("agent")
                    if isinstance(saved_agent, str) and saved_agent.strip():
                        resolved_agent = saved_agent.strip()
            else:
                resolved_conversation_id = ""

        if not resolved_agent:
            async with self._deps.config_lock:
                cfg = self._deps.load_appserver_config()
            cfg_conversation_id = cfg.get("conversation_id")
            if isinstance(cfg_conversation_id, str) and cfg_conversation_id.strip():
                safe_id = self._deps.sanitize_conversation_id(cfg_conversation_id.strip())
                if safe_id and self._deps.conversation_meta_path(safe_id).exists():
                    resolved_conversation_id = safe_id
                    meta = self._deps.load_conversation_meta(safe_id)
                    settings = self._deps.meta_settings(meta)
                    saved_agent = settings.get("agent")
                    if isinstance(saved_agent, str) and saved_agent.strip():
                        resolved_agent = saved_agent.strip()

        if not resolved_agent:
            resolved_agent = self._deps.default_active_extension_id() or ""

        settings = self._deps.meta_settings(meta or {})
        unavailable_detail = self._deps.extension_unavailable_detail(resolved_agent)
        if unavailable_detail:
            raise HTTPException(status_code=409, detail=unavailable_detail)

        if ext_loader.has_extension(resolved_agent):
            result = await ext_loader.get_runtime_options(
                resolved_agent,
                conversation_id=resolved_conversation_id or None,
                settings=settings,
            )
            if isinstance(result, dict):
                result.setdefault("agent", resolved_agent)
                return result
        raise HTTPException(status_code=409, detail=f"Extension unavailable: {resolved_agent or 'unknown'}")

    async def api_appserver_debug_raw(
        self,
        limit: int = Query(200, gt=0, le=500),
    ) -> ObjectMap:
        _ = limit
        return {"items": []}

    async def api_appserver_debug_state(self) -> ObjectMap:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
        convo_id = cfg.get("conversation_id")
        meta = self._deps.load_conversation_meta(convo_id) if isinstance(convo_id, str) and convo_id and self._deps.conversation_meta_path(convo_id).exists() else None
        debug_raw_log_path = self._deps.get_debug_raw_log_path()
        return {
            "config": cfg,
            "conversation": meta,
            "legacy_builtin_codex_disabled": True,
            "debug_mode": self._deps.get_debug_mode(),
            "debug_raw_log_path": str(debug_raw_log_path) if debug_raw_log_path else None,
        }

    async def api_appserver_debug_toggle(
        self,
        enabled: Annotated[bool, Body(..., embed=True)],
    ) -> ObjectMap:
        debug_raw_log_path = self._deps.set_debug_mode(enabled)
        return {
            "debug_mode": self._deps.get_debug_mode(),
            "debug_raw_log_path": str(debug_raw_log_path) if debug_raw_log_path else None,
        }


def register_appserver_routes(app: FastAPI, routes: AppserverRoutes) -> None:
    def _add(path: str, endpoint: Callable[..., object], methods: list[str]) -> None:
        app.add_api_route(path, endpoint, methods=methods, response_model=None)

    _add("/api/health", routes.api_health, ["GET"])
    _add("/api/appserver/config", routes.api_appserver_config, ["GET"])
    _add("/api/appserver/conversation", routes.api_appserver_conversation, ["GET"])
    _add(
        "/api/appserver/conversations/{conversation_id}/meta",
        routes.api_appserver_conversation_meta,
        ["GET"],
    )
    _add("/api/appserver/conversation", routes.api_appserver_conversation_update, ["POST"])
    _add("/api/appserver/conversation/draft", routes.api_appserver_conversation_draft, ["POST"])
    _add("/api/appserver/repo_memory", routes.api_appserver_repo_memory, ["GET"])
    _add("/api/appserver/conversations", routes.api_appserver_conversations, ["GET"])
    _add("/api/appserver/conversations", routes.api_appserver_conversation_create, ["POST"])
    _add("/api/appserver/conversations/select", routes.api_appserver_conversation_select, ["POST"])
    _add("/api/appserver/conversations/pins", routes.api_appserver_conversation_pins, ["POST"])
    _add(
        "/api/appserver/conversations/{conversation_id}",
        routes.api_appserver_conversation_delete,
        ["DELETE"],
    )
    _add("/api/appserver/view", routes.api_appserver_set_view, ["POST"])
    _add("/api/fs/list", routes.api_fs_list, ["GET"])
    _add("/api/fs/search", routes.api_fs_search, ["GET"])
    _add("/api/appserver/transcript", routes.api_appserver_transcript, ["GET"])
    _add("/api/appserver/transcript/range", routes.api_appserver_transcript_range, ["GET"])
    _add("/api/appserver/config", routes.api_appserver_config_update, ["POST"])
    _add("/api/appserver/cwd", routes.api_appserver_set_cwd, ["POST"])
    _add("/api/appserver/thread/start", routes.api_appserver_thread_start, ["POST"])
    _add("/api/appserver/thread/kill", routes.api_appserver_thread_kill, ["POST"])
    _add("/api/appserver/stop", routes.api_appserver_stop, ["POST"])
    _add("/api/appserver/start", routes.api_appserver_start, ["POST"])
    _add("/api/appserver/status", routes.api_appserver_status, ["GET"])
    _add("/api/appserver/message", routes.api_appserver_message, ["POST"])
    _add("/api/appserver/rpc", routes.api_appserver_rpc, ["POST"])
    _add("/api/appserver/approval_record", routes.api_appserver_approval_record, ["POST"])
    _add("/api/appserver/approval_response", routes.api_appserver_approval_response, ["POST"])
    _add("/api/appserver/interrupt", routes.api_appserver_interrupt, ["POST"])
    _add("/api/appserver/shell/exec", routes.api_appserver_shell_exec, ["POST"])
    _add("/api/appserver/compact", routes.api_appserver_compact, ["POST"])
    _add("/api/appserver/mention", routes.api_appserver_mention, ["POST", "PUT"])
    _add("/api/appserver/mention", routes.api_appserver_mention_options, ["OPTIONS"])
    _add("/api/appserver/initialize", routes.api_appserver_initialize, ["POST"])
    _add("/api/appserver/models", routes.api_appserver_models, ["GET"])
    _add("/api/appserver/runtime_options", routes.api_appserver_runtime_options, ["GET"])
    _add("/api/appserver/debug/raw", routes.api_appserver_debug_raw, ["GET"])
    _add("/api/appserver/debug/state", routes.api_appserver_debug_state, ["GET"])
    _add("/api/appserver/debug/toggle", routes.api_appserver_debug_toggle, ["POST"])
