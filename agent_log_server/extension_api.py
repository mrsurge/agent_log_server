from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import extensions as ext_loader
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

_AsyncAnyCallable = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ExtensionApiDeps:
    package_root: Path
    user_extensions_dir: Path
    config_lock: asyncio.Lock
    broadcast_appserver_ui: _AsyncAnyCallable
    get_fws_manager: Callable[..., Any]
    append_transcript_entry: _AsyncAnyCallable
    load_conversation_meta: Callable[[str], dict[str, Any]]
    save_conversation_meta: Callable[[str, dict[str, Any]], None]
    upsert_pending_approval: Callable[..., Any]
    remove_pending_approval: Callable[[str, Any], bool]
    sanitize_conversation_id: Callable[[str], str]
    conversation_meta_path: Callable[[str], Path]
    load_appserver_config: Callable[[], dict[str, Any]]
    save_appserver_config: Callable[[dict[str, Any]], None]
    ensure_conversation: Callable[..., Awaitable[str | None]]
    merge_extension_bind_settings: Callable[
        [str, str | None, str | None, dict[str, Any] | None],
        dict[str, Any],
    ]
    write_transcript_entries: Callable[[str, list[dict[str, Any]]], Awaitable[Any]]


class ExtensionApi:
    def __init__(self, deps: ExtensionApiDeps) -> None:
        self._deps: ExtensionApiDeps = deps

    def ensure_user_extensions_root(self) -> Path:
        self._deps.user_extensions_dir.mkdir(parents=True, exist_ok=True)
        registry_path = self._deps.user_extensions_dir / "extensions.json"
        if not registry_path.exists():
            registry_path.write_text(
                json.dumps({"version": "1.0", "extensions": []}, indent=2) + "\n",
                encoding="utf-8",
            )
        return self._deps.user_extensions_dir

    @staticmethod
    def normalize_extension_config_entry(raw: Any, default_enabled: bool) -> dict[str, Any]:
        if isinstance(raw, dict):
            enabled = raw.get("enabled")
            return {"enabled": default_enabled if enabled is None else enabled is True}
        if isinstance(raw, bool):
            return {"enabled": raw}
        return {"enabled": default_enabled}

    def normalize_extensions_config(self, raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for ext_id, value in raw.items():
            if not isinstance(ext_id, str) or not ext_id.strip():
                continue
            normalized[ext_id.strip()] = self.normalize_extension_config_entry(value, True)
        return normalized

    def seed_extension_config(self, cfg: dict[str, Any]) -> bool:
        extensions_cfg = self.normalize_extensions_config(cfg.get("extensions"))
        changed = extensions_cfg != cfg.get("extensions")
        cfg["extensions"] = extensions_cfg
        for info in ext_loader.list_extensions():
            if not isinstance(info, dict):
                continue
            ext_id = info.get("id")
            if not isinstance(ext_id, str) or not ext_id:
                continue
            default_enabled = bool(info.get("default_enabled", True))
            current = cfg["extensions"].get(ext_id)
            normalized = self.normalize_extension_config_entry(current, default_enabled)
            if cfg["extensions"].get(ext_id) != normalized:
                cfg["extensions"][ext_id] = normalized
                changed = True
        return changed

    def get_configured_extension_enabled(
        self,
        cfg: dict[str, Any],
        extension_id: str,
        default_enabled: bool = True,
    ) -> bool:
        extensions_cfg = self.normalize_extensions_config(cfg.get("extensions"))
        cfg["extensions"] = extensions_cfg
        entry = extensions_cfg.get(extension_id)
        normalized = self.normalize_extension_config_entry(entry, default_enabled)
        if entry != normalized:
            extensions_cfg[extension_id] = normalized
        return normalized.get("enabled") is True

    def default_active_extension_id(self) -> str | None:
        for info in ext_loader.list_extensions():
            if not isinstance(info, dict):
                continue
            ext_id = info.get("id")
            if not isinstance(ext_id, str) or not ext_id.strip():
                continue
            ext_id = ext_id.strip()
            if info.get("active") is True and ext_loader.has_extension(ext_id):
                return ext_id
        return None

    def conversation_agent(self, meta: dict[str, Any] | None) -> str:
        settings_raw = meta.get("settings") if isinstance(meta, dict) else None
        settings: dict[str, Any] = settings_raw if isinstance(settings_raw, dict) else {}
        agent = settings.get("agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
        return self.default_active_extension_id() or ""

    def clear_active_conversation_if_extension_inactive(self, cfg: dict[str, Any]) -> bool:
        conversation_id = cfg.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return False
        safe_id = self._deps.sanitize_conversation_id(conversation_id)
        if not safe_id or not self._deps.conversation_meta_path(safe_id).exists():
            return False
        meta = self._deps.load_conversation_meta(safe_id)
        agent = self.conversation_agent(meta)
        if not agent:
            return False
        info = ext_loader.get_extension_info(agent)
        if not info:
            cfg["conversation_id"] = None
            cfg["thread_id"] = None
            cfg["turn_id"] = None
            cfg["active_view"] = "splash"
            return True
        if ext_loader.has_extension(agent):
            return False
        cfg["conversation_id"] = None
        cfg["thread_id"] = None
        cfg["turn_id"] = None
        cfg["active_view"] = "splash"
        return True

    def extension_unavailable_detail(self, extension_id: str) -> str | None:
        extension_id = str(extension_id or "").strip()
        if not extension_id:
            return "No active extension available"
        if extension_id == "codex":
            return "Legacy builtin Codex is disabled"
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            return f"Extension unavailable: {extension_id}"
        if info.get("enabled") is not True:
            return f"Extension disabled: {extension_id}"
        status = str(info.get("dependency_status") or "").strip().lower()
        message = info.get("dependency_message")
        if status in {"unmet", "error"}:
            if isinstance(message, str) and message.strip():
                return message.strip()
            return f"Extension dependencies unmet: {extension_id}"
        if not ext_loader.has_extension(extension_id):
            return f"Extension unavailable: {extension_id}"
        return None

    def extension_unavailable_action(self, extension_id: str) -> dict[str, Any] | None:
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            return None
        details_raw = info.get("dependency_details")
        details: dict[str, Any] = details_raw if isinstance(details_raw, dict) else {}
        if details.get("auth_required") and not details.get("authenticated"):
            return {
                "id": "open_splash_settings",
                "label": "Open Splash Settings",
                "extension_id": extension_id,
            }
        return None

    def build_extension_unavailable_warning_event(
        self,
        extension_id: str,
        detail: str | None = None,
    ) -> dict[str, Any]:
        message = (
            detail.strip()
            if isinstance(detail, str) and detail.strip()
            else f"Extension unavailable: {extension_id}"
        )
        action = self.extension_unavailable_action(extension_id)
        if action and "splash settings" not in message.lower():
            message = f"{message} Open splash settings to configure."
        event: dict[str, Any] = {
            "type": "warning",
            "message": message,
        }
        if action:
            event["action"] = action
        return event

    async def emit_extension_unavailable_warning(
        self,
        conversation_id: str | None,
        extension_id: str,
        *,
        detail: str | None = None,
    ) -> None:
        event = self.build_extension_unavailable_warning_event(extension_id, detail=detail)
        if conversation_id:
            event["conversation_id"] = conversation_id
        await self._deps.broadcast_appserver_ui(event)

    async def refresh_extension_runtime_state(
        self,
        extension_ids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            changed = self.seed_extension_config(cfg)
            target_ids = [
                info["id"]
                for info in ext_loader.list_extensions()
                if isinstance(info, dict)
                and isinstance(info.get("id"), str)
                and info.get("id")
            ]
            if extension_ids:
                wanted = {
                    str(ext_id).strip()
                    for ext_id in extension_ids
                    if isinstance(ext_id, str) and ext_id.strip()
                }
                target_ids = [ext_id for ext_id in target_ids if ext_id in wanted]
            enabled_map: dict[str, bool] = {}
            for ext_id in target_ids:
                info = ext_loader.get_extension_info(ext_id) or {}
                enabled_map[ext_id] = self.get_configured_extension_enabled(
                    cfg,
                    ext_id,
                    bool(info.get("default_enabled", True)),
                )
            if changed:
                self._deps.save_appserver_config(cfg)

        results: dict[str, dict[str, Any]] = {}
        for ext_id in target_ids:
            ext_loader.set_extension_enabled(ext_id, enabled_map.get(ext_id, True))
            if ext_loader.supports_dependency_check(ext_id):
                result = await ext_loader.check_extension_dependencies(ext_id)
            else:
                result = {
                    "ok": True,
                    "status": "met",
                    "message": "No dependency check required",
                }
            ext_loader.set_extension_dependency_result(ext_id, result)
            info = ext_loader.get_extension_info(ext_id)
            if isinstance(info, dict):
                results[ext_id] = info

        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            changed = self.seed_extension_config(cfg)
            if self.clear_active_conversation_if_extension_inactive(cfg):
                changed = True
            if changed:
                self._deps.save_appserver_config(cfg)

        return results

    def normalize_extension_package_payload(
        self,
        payload: Any,
        *,
        allow_missing_source_type: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        source_type = str(payload.get("source_type") or "").strip().lower()
        if not source_type and not allow_missing_source_type:
            raise HTTPException(status_code=400, detail="Missing required field: source_type")
        source_path = payload.get("source_path")
        if source_path is None:
            source_path = payload.get("path")
        repo_url = payload.get("repo_url")
        if repo_url is None:
            repo_url = payload.get("url")
        ref = payload.get("ref")
        extension_id = payload.get("extension_id")
        normalized = {
            "source_type": source_type,
            "source_path": (
                str(source_path).strip()
                if isinstance(source_path, str) and source_path.strip()
                else None
            ),
            "repo_url": (
                str(repo_url).strip()
                if isinstance(repo_url, str) and repo_url.strip()
                else None
            ),
            "ref": str(ref).strip() if isinstance(ref, str) and ref.strip() else None,
            "extension_id": (
                str(extension_id).strip()
                if isinstance(extension_id, str) and extension_id.strip()
                else None
            ),
            "allow_override": payload.get("allow_override") is True,
            "install_dependencies": payload.get("install_dependencies") is True,
            "force_reload": payload.get("force_reload") is True,
        }
        if normalized["source_type"] in {"path", "zip"} and not normalized["source_path"]:
            raise HTTPException(
                status_code=400,
                detail=f"source_path is required for source_type={normalized['source_type']}",
            )
        if normalized["source_type"] == "git" and not normalized["repo_url"]:
            raise HTTPException(status_code=400, detail="repo_url is required for source_type=git")
        return normalized

    @staticmethod
    def extension_package_error_detail(result: dict[str, Any]) -> str:
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        errors = result.get("errors")
        if isinstance(errors, list):
            texts = [str(item).strip() for item in errors if str(item).strip()]
            if texts:
                return "; ".join(texts)
        return "Extension package operation failed"

    def raise_extension_package_http_error(self, result: dict[str, Any]) -> None:
        status = str(result.get("status") or "").strip().lower()
        detail = self.extension_package_error_detail(result)
        status_code = 400
        if status == "not_found":
            status_code = 404
        elif status == "conflict":
            status_code = 409
        elif status == "error":
            status_code = 500
        raise HTTPException(status_code=status_code, detail=detail)

    async def reload_extension_registry_runtime(
        self,
        extension_ids: list[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, dict[str, Any]]:
        await asyncio.to_thread(
            ext_loader.reload_extensions,
            changed_extension_ids=extension_ids,
            force=force,
        )
        return await self.refresh_extension_runtime_state()

    async def wait_for_extension_ready_if_active(self, extension_id: str) -> None:
        if not ext_loader.has_extension(extension_id):
            return
        with suppress(Exception):
            await ext_loader.wait_extension_ready(extension_id, timeout=60.0)

    def init_extensions(self) -> None:
        builtin_extensions_dir = (
            Path(ext_loader.__file__).parent
            if isinstance(getattr(ext_loader, "__file__", None), str)
            else self._deps.package_root.parent / "extensions"
        )
        user_extensions_dir = self.ensure_user_extensions_root()
        extension_roots = [
            root
            for root in (builtin_extensions_dir, user_extensions_dir)
            if root.exists()
        ]
        if not extension_roots:
            return
        ext_loader.load_extensions(
            extensions_dir=extension_roots,
            server_root=self._deps.package_root,
            fws_getter=self._deps.get_fws_manager,
            broadcast_fn=self._deps.broadcast_appserver_ui,
            transcript_fn=self._deps.append_transcript_entry,
            meta_fns={
                "load": self._deps.load_conversation_meta,
                "save": self._deps.save_conversation_meta,
                "upsert_pending_approval": self._deps.upsert_pending_approval,
                "remove_pending_approval": self._deps.remove_pending_approval,
            },
        )

    async def api_extensions_list(self) -> dict[str, Any]:
        return {"extensions": ext_loader.list_extensions()}

    async def api_extension_enabled(
        self,
        extension_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            raise HTTPException(status_code=404, detail=f"Extension not found: {extension_id}")
        if "enabled" not in payload:
            raise HTTPException(status_code=400, detail="Missing required field: enabled")
        enabled = payload.get("enabled") is True
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self.seed_extension_config(cfg)
            cfg["extensions"][extension_id] = self.normalize_extension_config_entry(
                cfg["extensions"].get(extension_id),
                bool(info.get("default_enabled", True)),
            )
            cfg["extensions"][extension_id]["enabled"] = enabled
            self._deps.save_appserver_config(cfg)
        states = await self.refresh_extension_runtime_state([extension_id])
        if enabled:
            with suppress(Exception):
                await ext_loader.wait_extension_ready(extension_id, timeout=60.0)
        return {
            "ok": True,
            "extension": states.get(extension_id)
            or ext_loader.get_extension_info(extension_id),
        }

    async def api_extension_install(self, extension_id: str) -> dict[str, Any]:
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            raise HTTPException(status_code=404, detail=f"Extension not found: {extension_id}")
        if not ext_loader.supports_dependency_install(extension_id):
            raise HTTPException(
                status_code=409,
                detail=f"Extension does not support dependency install: {extension_id}",
            )
        result = await ext_loader.install_extension_dependencies(extension_id)
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            self.seed_extension_config(cfg)
            cfg["extensions"][extension_id] = self.normalize_extension_config_entry(
                cfg["extensions"].get(extension_id),
                bool(info.get("default_enabled", True)),
            )
            if result.get("ok"):
                cfg["extensions"][extension_id]["enabled"] = True
            self._deps.save_appserver_config(cfg)
        states = await self.refresh_extension_runtime_state([extension_id])
        if result.get("ok"):
            with suppress(Exception):
                await ext_loader.wait_extension_ready(extension_id, timeout=60.0)
        refreshed = states.get(extension_id) or ext_loader.get_extension_info(extension_id)
        return {"ok": bool(result.get("ok")), "result": result, "extension": refreshed}

    async def api_extensions_validate(
        self,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        normalized = self.normalize_extension_package_payload(payload)
        return await asyncio.to_thread(
            ext_loader.validate_extension_source,
            source_type=normalized["source_type"],
            source_path=normalized["source_path"],
            repo_url=normalized["repo_url"],
            ref=normalized["ref"],
            extension_id=normalized["extension_id"],
        )

    async def api_extensions_install_package(
        self,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        normalized = self.normalize_extension_package_payload(payload)
        result = await asyncio.to_thread(
            ext_loader.install_extension_source,
            source_type=normalized["source_type"],
            source_path=normalized["source_path"],
            repo_url=normalized["repo_url"],
            ref=normalized["ref"],
            extension_id=normalized["extension_id"],
            allow_override=normalized["allow_override"],
        )
        if not result.get("ok"):
            self.raise_extension_package_http_error(result)
        extension_id = str(
            result.get("extension_id") or normalized.get("extension_id") or ""
        ).strip()
        states = await self.reload_extension_registry_runtime(
            [extension_id] if extension_id else None,
            force=normalized["force_reload"],
        )
        dependency_result = None
        if (
            normalized["install_dependencies"]
            and extension_id
            and ext_loader.supports_dependency_install(extension_id)
        ):
            dependency_result = await ext_loader.install_extension_dependencies(extension_id)
            states = await self.refresh_extension_runtime_state()
        if extension_id:
            await self.wait_for_extension_ready_if_active(extension_id)
        refreshed = states.get(extension_id) or ext_loader.get_extension_info(extension_id)
        ok = bool(result.get("ok")) and (
            dependency_result is None or bool(dependency_result.get("ok"))
        )
        return {
            "ok": ok,
            "result": result,
            "dependency_install": dependency_result,
            "extension": refreshed,
        }

    async def api_extension_update_package(
        self,
        extension_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        normalized = self.normalize_extension_package_payload(
            payload,
            allow_missing_source_type=True,
        )
        if normalized["extension_id"] and normalized["extension_id"] != extension_id:
            raise HTTPException(
                status_code=400,
                detail="Payload extension_id does not match route extension_id",
            )
        result = await asyncio.to_thread(
            ext_loader.update_extension_source,
            extension_id,
            source_type=normalized["source_type"] or None,
            source_path=normalized["source_path"],
            repo_url=normalized["repo_url"],
            ref=normalized["ref"],
        )
        if not result.get("ok"):
            self.raise_extension_package_http_error(result)
        states = await self.reload_extension_registry_runtime(
            [extension_id],
            force=normalized["force_reload"],
        )
        dependency_result = None
        if normalized["install_dependencies"] and ext_loader.supports_dependency_install(
            extension_id
        ):
            dependency_result = await ext_loader.install_extension_dependencies(extension_id)
            states = await self.refresh_extension_runtime_state()
        await self.wait_for_extension_ready_if_active(extension_id)
        refreshed = states.get(extension_id) or ext_loader.get_extension_info(extension_id)
        ok = bool(result.get("ok")) and (
            dependency_result is None or bool(dependency_result.get("ok"))
        )
        return {
            "ok": ok,
            "result": result,
            "dependency_install": dependency_result,
            "extension": refreshed,
        }

    async def api_extension_remove_package(self, extension_id: str) -> dict[str, Any]:
        result = await asyncio.to_thread(ext_loader.remove_user_extension, extension_id)
        if not result.get("ok"):
            self.raise_extension_package_http_error(result)
        await self.reload_extension_registry_runtime([extension_id], force=False)
        async with self._deps.config_lock:
            cfg = self._deps.load_appserver_config()
            extensions_cfg = self.normalize_extensions_config(cfg.get("extensions"))
            cfg["extensions"] = extensions_cfg
            changed = extensions_cfg.pop(extension_id, None) is not None
            if self.clear_active_conversation_if_extension_inactive(cfg):
                changed = True
            if changed:
                self._deps.save_appserver_config(cfg)
        return {
            "ok": True,
            "result": result,
            "extensions": ext_loader.list_extensions(),
        }

    async def api_extensions_reload(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        raw_payload = payload if isinstance(payload, dict) else {}
        normalized = self.normalize_extension_package_payload(
            raw_payload,
            allow_missing_source_type=True,
        )
        changed_ids: list[str] = []
        if normalized["extension_id"]:
            changed_ids.append(normalized["extension_id"])
        raw_ids = raw_payload.get("extension_ids")
        if isinstance(raw_ids, list):
            for item in raw_ids:
                if isinstance(item, str) and item.strip() and item.strip() not in changed_ids:
                    changed_ids.append(item.strip())
        states = await self.reload_extension_registry_runtime(
            changed_ids or None,
            force=normalized["force_reload"],
        )
        return {
            "ok": True,
            "extensions": ext_loader.list_extensions(),
            "states": states,
        }

    async def api_extension_get(self, extension_id: str) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        ext = ext_loader.get_extension_info(extension_id)
        if isinstance(ext, dict):
            return ext
        return JSONResponse(
            {"error": f"Extension not found: {extension_id}"},
            status_code=404,
        )

    async def api_extension_settings_schema(
        self,
        extension_id: str,
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            dynamic_schema = await ext_loader.get_settings_schema(extension_id)
        except Exception as exc:
            return JSONResponse({"error": f"Failed to build schema: {exc}"}, status_code=500)
        if isinstance(dynamic_schema, dict):
            return dynamic_schema
        try:
            static_schema = ext_loader.get_static_settings_schema(extension_id)
        except Exception as exc:
            return JSONResponse({"error": f"Failed to load schema: {exc}"}, status_code=500)
        if isinstance(static_schema, dict):
            return static_schema
        return {"version": "1", "fields": []}

    async def api_extension_splash_schema(
        self,
        extension_id: str,
    ) -> dict[str, Any] | JSONResponse:
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            schema = await ext_loader.get_splash_schema(extension_id)
        except Exception as exc:
            return JSONResponse({"error": f"Failed to build splash schema: {exc}"}, status_code=500)
        if isinstance(schema, dict):
            schema.setdefault("extension_id", extension_id)
            return schema
        return {"version": "1", "extension_id": extension_id, "fields": []}

    async def api_extension_splash_action(
        self,
        extension_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any] | JSONResponse:
        info = ext_loader.get_extension_info(extension_id)
        if not isinstance(info, dict):
            raise HTTPException(status_code=404, detail=f"Extension not found: {extension_id}")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Payload must be a JSON object")
        action_id = str(payload.get("action_id") or "").strip()
        if not action_id:
            raise HTTPException(status_code=400, detail="Missing action_id")
        try:
            result = await ext_loader.run_splash_action(
                extension_id,
                action_id=action_id,
                payload=(
                    payload.get("payload")
                    if isinstance(payload.get("payload"), dict)
                    else {}
                ),
            )
            states = await self.refresh_extension_runtime_state([extension_id])
            refreshed = states.get(extension_id) or ext_loader.get_extension_info(extension_id)
            schema = await ext_loader.get_splash_schema(extension_id)
            return {
                "ok": bool(isinstance(result, dict) and result.get("ok")),
                "result": (
                    result
                    if isinstance(result, dict)
                    else {"ok": False, "error": "Invalid splash action result"}
                ),
                "extension": refreshed,
                "schema": (
                    schema
                    if isinstance(schema, dict)
                    else {"version": "1", "extension_id": extension_id, "fields": []}
                ),
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def api_extension_request_cards(
        self,
        extension_id: str,
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            config = await ext_loader.get_request_cards(extension_id)
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to build request-card config: {exc}"},
                status_code=500,
            )
        cards: list[dict[str, Any]] = []
        raw_cards = config.get("cards") if isinstance(config, dict) else None
        if isinstance(raw_cards, list):
            for raw_entry in raw_cards:
                if not isinstance(raw_entry, dict):
                    continue
                module_path = str(raw_entry.get("module") or "").strip().lstrip("/")
                if not module_path:
                    continue
                entry = dict(raw_entry)
                entry["module"] = module_path
                entry["module_url"] = f"/api/extensions/{extension_id}/assets/{module_path}"
                entry["export"] = (
                    str(entry.get("export") or "renderRequestCard").strip()
                    or "renderRequestCard"
                )
                cards.append(entry)
        schemas = (
            config.get("schemas")
            if isinstance(config, dict) and isinstance(config.get("schemas"), dict)
            else {}
        )
        return {
            "extension_id": extension_id,
            "cards": cards,
            "schemas": schemas,
        }

    async def api_extension_asset(self, extension_id: str, asset_path: str) -> FileResponse:
        asset_file = ext_loader.get_extension_asset_path(extension_id, asset_path)
        if asset_file is None:
            raise HTTPException(status_code=404, detail="Extension asset not found")
        return FileResponse(asset_file)

    async def api_extension_models(
        self,
        extension_id: str,
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            result = await ext_loader.list_models(extension_id)
            if isinstance(result, list):
                return {"models": result}
            return result if isinstance(result, dict) else {"models": result}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def api_extension_plan(
        self,
        extension_id: str,
        conversation_id: str | None = Query(None),
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        convo_id = conversation_id
        if not convo_id:
            convo_id = await self._deps.ensure_conversation(create_if_missing=False)
        if not convo_id:
            raise HTTPException(status_code=400, detail="Missing conversation_id")
        try:
            result = await ext_loader.read_plan(extension_id, convo_id)
            if isinstance(result, dict):
                result.setdefault("conversation_id", convo_id)
                result.setdefault("extension_id", extension_id)
                return result
            return {
                "conversation_id": convo_id,
                "extension_id": extension_id,
                "has_plan": False,
                "plan_exists": False,
                "plan_content": "",
                "plan_steps": [],
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def api_extension_sessions(
        self,
        extension_id: str,
        cwd: str | None = Query(None),
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            sessions = await ext_loader.list_sessions(extension_id, cwd=cwd)
            return {"sessions": sessions}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def api_extension_session_resume(
        self,
        extension_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        session_id = payload.get("session_id")
        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id")

        conversation_id = (
            str(payload.get("conversation_id")).strip()
            if isinstance(payload.get("conversation_id"), str)
            and str(payload.get("conversation_id")).strip()
            else None
        )
        if not conversation_id:
            conversation_id = await self._deps.ensure_conversation()
        if not conversation_id:
            raise HTTPException(status_code=500, detail="Failed to create conversation")

        try:
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
            model = payload.get("model") if isinstance(payload.get("model"), str) else None
            settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else None
            bind_settings = self._deps.merge_extension_bind_settings(
                conversation_id,
                cwd,
                model,
                settings,
            )
            result = await ext_loader.resume_session_with_history(
                extension_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=cwd,
                model=model,
                settings=bind_settings,
            )
            if not result.get("ok"):
                return JSONResponse(
                    {"error": result.get("error", "Resume failed")},
                    status_code=500,
                )

            items = await ext_loader.hydrate_transcript(
                extension_id,
                session_id=session_id,
                conversation_id=conversation_id,
                cwd=cwd,
                model=model,
                settings=bind_settings,
            )
            if items:
                await self._deps.write_transcript_entries(conversation_id, items)

            async with self._deps.config_lock:
                cfg = self._deps.load_appserver_config()
                cfg["conversation_id"] = conversation_id
                cfg["thread_id"] = session_id
                cfg["active_view"] = "conversation"
                self._deps.save_appserver_config(cfg)

            return {
                "ok": True,
                "conversation_id": conversation_id,
                "session_id": session_id,
                "history_count": len(items),
            }
        except Exception as exc:
            traceback.print_exc()
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def api_extension_debug_raw(
        self,
        extension_id: str,
        limit: int = Query(50, gt=0, le=200),
    ) -> dict[str, Any] | JSONResponse:
        if not ext_loader.has_extension(extension_id):
            return JSONResponse(
                {"error": f"Extension not found: {extension_id}"},
                status_code=404,
            )
        try:
            items = ext_loader.get_raw_buffer(extension_id, limit)
            return {"items": items}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)


def register_extension_api_routes(app: FastAPI, api: ExtensionApi) -> None:
    def _add(path: str, endpoint: Callable[..., Any], methods: list[str]) -> None:
        app.add_api_route(path, endpoint, methods=methods, response_model=None)

    _add("/api/extensions", api.api_extensions_list, ["GET"])
    _add(
        "/api/extensions/{extension_id}/enabled",
        api.api_extension_enabled,
        ["POST"],
    )
    _add(
        "/api/extensions/{extension_id}/install",
        api.api_extension_install,
        ["POST"],
    )
    _add("/api/extensions/validate", api.api_extensions_validate, ["POST"])
    _add("/api/extensions/install", api.api_extensions_install_package, ["POST"])
    _add(
        "/api/extensions/{extension_id}/update",
        api.api_extension_update_package,
        ["POST"],
    )
    _add(
        "/api/extensions/{extension_id}",
        api.api_extension_remove_package,
        ["DELETE"],
    )
    _add("/api/extensions/reload", api.api_extensions_reload, ["POST"])
    _add("/api/extensions/{extension_id}", api.api_extension_get, ["GET"])
    _add(
        "/api/extensions/{extension_id}/settings_schema",
        api.api_extension_settings_schema,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/splash_schema",
        api.api_extension_splash_schema,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/splash_action",
        api.api_extension_splash_action,
        ["POST"],
    )
    _add(
        "/api/extensions/{extension_id}/request_cards",
        api.api_extension_request_cards,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/assets/{asset_path:path}",
        api.api_extension_asset,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/models",
        api.api_extension_models,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/plan",
        api.api_extension_plan,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/sessions",
        api.api_extension_sessions,
        ["GET"],
    )
    _add(
        "/api/extensions/{extension_id}/sessions/resume",
        api.api_extension_session_resume,
        ["POST"],
    )
    _add(
        "/api/extensions/{extension_id}/debug/raw",
        api.api_extension_debug_raw,
        ["GET"],
    )
