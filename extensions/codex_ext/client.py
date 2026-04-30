"""
Codex App Server Client

Extension handler for Codex app-server using runtime-generated protocol schema
from the installed binary plus the generic extension hook surface.
"""

import asyncio
import copy
import contextlib
import importlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, cast

from .plan_utils import normalize_plan_steps, render_plan_markdown
from .rollout_import import find_rollout_path, preview_entries
from .runtime_protocol import (
    RuntimeProtocol,
    build_request_params,
    build_settings_schema,
    build_thread_runtime_signature_payload,
    configure_runtime_protocol,
    get_runtime_protocol,
)
from .transport import CodexAppServerTransport, _MetaFns
from agent_log_server.te2_mcp_config import te2_mcp_integration_enabled


class _ServerModule(Protocol):
    def _te2_base_url(self) -> object: ...

    async def _refresh_extension_runtime_state(self, extension_ids: List[str]) -> None: ...

    def _conversation_transcript_path(self, conversation_id: str) -> object: ...

    def _extension_unavailable_detail(self, extension_id: str) -> object: ...

    async def _emit_extension_unavailable_warning(
        self,
        conversation_id: str,
        extension_id: str,
        *,
        detail: str,
    ) -> None: ...


class _ExtensionsModule(Protocol):
    def get_extension_info(self, extension_id: str) -> object: ...

# Stored references to server callbacks
_broadcast_fn: Optional[Callable] = None
_transcript_fn: Optional[Callable] = None
_meta_fns: Optional[_MetaFns] = None
_registered_extension_ids: set[str] = set()
_ready_extensions: set[str] = set()
_transport: Optional[CodexAppServerTransport] = None
_auth_flow_state: Dict[str, Dict[str, object]] = {}

# Debug buffer (circular)
_raw_buffer: List[Dict[str, object]] = []
_RAW_BUFFER_MAX = 1000


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _add_to_raw_buffer(direction: str, conversation_id: str, data: object) -> None:
    entry: Dict[str, object] = {
        "ts": _utc_ts(),
        "dir": direction,
        "convo": conversation_id[:8] if conversation_id else "?",
        "data": data if isinstance(data, str) else str(data)[:500],
    }
    _raw_buffer.append(entry)
    if len(_raw_buffer) > _RAW_BUFFER_MAX:
        _raw_buffer.pop(0)


def get_raw_buffer(limit: int = 50) -> List[Dict[str, object]]:
    return _raw_buffer[-limit:]


def _server_module() -> _ServerModule:
    return cast(_ServerModule, importlib.import_module("agent_log_server.server"))


def _extensions_module() -> _ExtensionsModule:
    return cast(_ExtensionsModule, importlib.import_module("extensions"))


def _object_dict(value: object) -> Dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _save_meta(conversation_id: str, meta: Dict[str, object]) -> None:
    if _meta_fns and "save" in _meta_fns:
        _meta_fns["save"](conversation_id, meta)


def _merge_runtime_settings(
    conversation_id: str,
    settings: Optional[Dict[str, object]] = None,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, object]:
    merged: Dict[str, object] = {}
    if _meta_fns and "load" in _meta_fns:
        meta = _object_dict(_meta_fns["load"](conversation_id))
        settings_value = meta.get("settings")
        if isinstance(settings_value, dict):
            merged.update(_object_dict(settings_value))
    if isinstance(settings, dict):
        for key, value in settings.items():
            if value is None or value == "":
                continue
            merged[key] = value
    if isinstance(cwd, str) and cwd.strip():
        merged["cwd"] = cwd
    if isinstance(model, str) and model.strip():
        merged["model"] = model
    merged["conversation_id"] = conversation_id
    return _materialize_runtime_settings(merged)


def _thread_runtime_signature(protocol: RuntimeProtocol, settings: Dict[str, object]) -> str:
    payload = build_thread_runtime_signature_payload(protocol, settings)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _materialize_runtime_settings(settings: Optional[Dict[str, object]]) -> Dict[str, object]:
    if not isinstance(settings, dict):
        return {}
    merged = dict(settings)
    model_value = merged.get("model")
    if not isinstance(model_value, str) or not model_value.strip():
        agent_value = merged.get("agent")
        extension_id = agent_value if isinstance(agent_value, str) and agent_value.strip() else "codex"
        try:
            ext_info = _extensions_module().get_extension_info(extension_id)
        except Exception:
            ext_info = None
        manifest = _object_dict(ext_info.get("manifest")) if isinstance(ext_info, dict) else {}
        model = _object_dict(manifest.get("model"))
        model_name = model.get("name")
        if isinstance(model_name, str) and model_name.strip():
            merged["model"] = model_name.strip()
    if te2_mcp_integration_enabled(merged):
        te2_base_url = _server_module()._te2_base_url()
        if isinstance(te2_base_url, str) and te2_base_url.strip():
            merged["te2_base_url"] = te2_base_url
    return merged


def _extract_thread_id_from_result(payload: object) -> Optional[str]:
    if isinstance(payload, dict):
        thread = payload.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
    return None


def _mark_transport_ready() -> None:
    for ext_id in _registered_extension_ids:
        _ready_extensions.add(ext_id)


def _ensure_transport() -> CodexAppServerTransport:
    if _transport is None:
        raise RuntimeError("Codex transport not initialized")
    return _transport


async def _ensure_transport_ready() -> CodexAppServerTransport:
    transport = _ensure_transport()
    await transport.ensure_ready()
    _mark_transport_ready()
    return transport


def _auth_state_bucket(extension_id: str) -> Dict[str, object]:
    key = str(extension_id or "").strip() or "codex-ext"
    bucket = _auth_flow_state.get(key)
    if not isinstance(bucket, dict):
        bucket = {}
        _auth_flow_state[key] = bucket
    return bucket


def _clear_auth_state(extension_id: str) -> None:
    key = str(extension_id or "").strip() or "codex-ext"
    _auth_flow_state.pop(key, None)


def _auth_extension_ids(*extension_ids: str) -> List[str]:
    explicit = [
        ext_id.strip()
        for ext_id in extension_ids
        if isinstance(ext_id, str) and ext_id.strip()
    ]
    if explicit:
        return explicit
    registered = sorted(
        ext_id
        for ext_id in _registered_extension_ids
        if isinstance(ext_id, str) and ext_id.strip()
    )
    return registered or ["codex-ext"]


async def _refresh_extension_auth_state(*extension_ids: str) -> None:
    await _server_module()._refresh_extension_runtime_state(_auth_extension_ids(*extension_ids))


async def _handle_auth_transport_event(
    *,
    label: str,
    payload: object,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> List[Dict[str, object]]:
    del thread_id, turn_id, request_id
    label_lower = str(label or "").strip().lower()
    if label_lower not in {"account/login/completed", "account/updated"}:
        return []

    extension_ids = _auth_extension_ids()
    events: List[Dict[str, object]] = []

    if label_lower == "account/login/completed" and isinstance(payload, dict):
        login_id = payload.get("loginId")
        login_id = login_id.strip() if isinstance(login_id, str) and login_id.strip() else None
        success = payload.get("success") is True
        error_message = payload.get("error")
        error_message = error_message.strip() if isinstance(error_message, str) and error_message.strip() else None
        for extension_id in extension_ids:
            pending = _auth_state_bucket(extension_id)
            pending_login_id = pending.get("login_id")
            pending_login_id = (
                pending_login_id.strip()
                if isinstance(pending_login_id, str) and pending_login_id.strip()
                else None
            )
            if login_id and pending_login_id and login_id != pending_login_id:
                continue
            if not success:
                pending.clear()
        await _refresh_extension_auth_state(*extension_ids)
        events.append({"type": "extensions_updated"})
        if not success and error_message:
            warning_event: Dict[str, object] = {
                "type": "warning",
                "message": f"Codex login failed: {error_message}",
            }
            if conversation_id:
                warning_event["conversation_id"] = conversation_id
            events.append(warning_event)
        return events

    if label_lower == "account/updated" and isinstance(payload, dict):
        for extension_id in extension_ids:
            _clear_auth_state(extension_id)
        await _refresh_extension_auth_state(*extension_ids)
        events.append({"type": "extensions_updated"})
    return events


async def _open_url_with_xdg_open(url: str) -> tuple[bool, str]:
    opener = shutil.which("xdg-open")
    if not opener:
        return False, "xdg-open not found"
    target = str(url or "").strip()
    if not target:
        return False, "Empty URL"
    try:
        proc = await asyncio.create_subprocess_exec(
            opener,
            target,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        return False, str(exc)
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1.0)
    except asyncio.TimeoutError:
        return True, ""
    if proc.returncode == 0:
        return True, ""
    message = stderr.decode("utf-8", errors="replace").strip() if isinstance(stderr, (bytes, bytearray)) else ""
    return False, message or f"xdg-open exited with {proc.returncode}"


def _plan_label(plan_type: Optional[str]) -> str:
    if not isinstance(plan_type, str) or not plan_type.strip():
        return ""
    return plan_type.replace("_", " ").replace("-", " ").title()


def _auth_status_detail(status: Dict[str, object]) -> str:
    parts: List[str] = []
    email = status.get("account_email")
    if isinstance(email, str) and email.strip():
        parts.append(email.strip())
    account_type = status.get("account_type")
    if account_type == "apiKey":
        parts.append("API key")
    plan_value = status.get("plan_type")
    plan_type = _plan_label(plan_value if isinstance(plan_value, str) else None)
    if plan_type:
        parts.append(f"{plan_type} plan")
    return "  •  ".join(parts)


def _build_auth_status_message(
    *,
    requires_openai_auth: bool,
    authenticated: bool,
    account_type: Optional[str],
    account_email: Optional[str],
    plan_type: Optional[str],
    login_pending: bool,
) -> str:
    if authenticated:
        if account_type == "chatgpt":
            base = f"Signed in as {account_email or 'your ChatGPT account'}"
            plan_label = _plan_label(plan_type)
            if plan_label:
                base += f" ({plan_label} plan)"
            return base + "."
        if account_type == "apiKey":
            return "Authenticated via API key."
        return "Authenticated."
    if not requires_openai_auth:
        return "OpenAI auth not required for the current provider."
    if login_pending:
        return "ChatGPT login pending. Finish sign-in in the opened browser."
    return "OpenAI auth required. Sign in with ChatGPT from splash settings."


def _settings_info_tone_from_auth(auth_status: Dict[str, object]) -> str:
    status = str(auth_status.get("status") or "").strip().lower()
    if auth_status.get("ok") is False or status == "error":
        return "error"
    if status in {"auth_required", "login_pending"}:
        return "warning"
    return "success"


def _settings_info_tone_from_remaining(remaining_percent: Optional[float]) -> str:
    if remaining_percent is None:
        return "success"
    if remaining_percent <= 10.0:
        return "error"
    if remaining_percent <= 25.0:
        return "warning"
    return "success"


def _format_settings_timestamp(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    with contextlib.suppress(Exception):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return ""


def _usage_info_unavailable(
    message: str,
    *,
    tone: str = "warning",
    detail: str = "",
) -> Dict[str, object]:
    return {
        "text": message,
        "detail": detail,
        "tone": tone,
    }


def _rate_limit_window_detail(label: str, raw_window: object) -> tuple[Optional[float], Optional[str]]:
    if not isinstance(raw_window, dict):
        return None, None
    used_percent = raw_window.get("usedPercent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        return None, None
    remaining_percent = max(0.0, min(100.0, 100.0 - float(used_percent)))
    parts = [f"{label}: {remaining_percent:.0f}% remaining"]
    window_duration = raw_window.get("windowDurationMins")
    if isinstance(window_duration, (int, float)) and not isinstance(window_duration, bool):
        parts.append(f"{float(window_duration):g} min window")
    reset_text = _format_settings_timestamp(raw_window.get("resetsAt"))
    if reset_text:
        parts.append(f"resets {reset_text}")
    return remaining_percent, "  •  ".join(parts)


def _build_rate_limit_lines(snapshot: Dict[str, object]) -> tuple[List[str], List[float]]:
    lines: List[str] = []
    remaining_values: List[float] = []
    limit_name = snapshot.get("limitName") if isinstance(snapshot.get("limitName"), str) else None
    limit_id = snapshot.get("limitId") if isinstance(snapshot.get("limitId"), str) else None
    snapshot_prefix = next(
        (
            value.strip()
            for value in (limit_name, limit_id)
            if isinstance(value, str) and value.strip()
        ),
        "",
    )
    for label, key in (("Primary", "primary"), ("Secondary", "secondary")):
        remaining_percent, detail = _rate_limit_window_detail(label, snapshot.get(key))
        if detail:
            if snapshot_prefix:
                lines.append(f"{snapshot_prefix}: {detail}")
                snapshot_prefix = ""
            else:
                lines.append(detail)
        if remaining_percent is not None:
            remaining_values.append(remaining_percent)

    credits = snapshot.get("credits")
    if isinstance(credits, dict):
        if credits.get("unlimited") is True:
            lines.append("Credits: unlimited")
        elif credits.get("hasCredits") is True:
            balance = credits.get("balance")
            if isinstance(balance, str) and balance.strip():
                lines.append(f"Credits balance: {balance.strip()}")
            else:
                lines.append("Credits available")
    return lines, remaining_values


async def get_usage_info(
    extension_id: str,
    *,
    auth_status: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    resolved_auth_status = auth_status or await get_auth_status(extension_id, refresh=False)
    if resolved_auth_status.get("ok") is False:
        message = resolved_auth_status.get("message")
        return _usage_info_unavailable(
            message if isinstance(message, str) and message else "Failed to read Codex auth status.",
            tone="error",
        )
    status = str(resolved_auth_status.get("status") or "").strip().lower()
    if status == "login_pending":
        return _usage_info_unavailable(
            "Usage unavailable while ChatGPT login is pending.",
            tone="warning",
            detail="Finish sign-in in the opened browser, then reopen settings.",
        )
    if status == "auth_required":
        return _usage_info_unavailable(
            "Usage unavailable until ChatGPT sign-in is complete.",
            tone="warning",
        )
    if not resolved_auth_status.get("requires_openai_auth"):
        return _usage_info_unavailable(
            "Usage info is not required for the current provider.",
            tone="success",
        )
    if resolved_auth_status.get("account_type") == "apiKey":
        return _usage_info_unavailable(
            "Usage info is unavailable for API key authentication.",
            tone="success",
            detail="ChatGPT rate-limit snapshots are only available for ChatGPT-authenticated accounts.",
        )

    try:
        transport = await _ensure_transport_ready()
        raw = await transport.rpc_request(
            "account/rateLimits/read",
            timeout=15.0,
        )
    except Exception as exc:
        return _usage_info_unavailable(
            f"Failed to read Codex usage info: {exc}",
            tone="error",
        )

    payload = raw if isinstance(raw, dict) else {}
    snapshots: List[Dict[str, object]] = []
    rate_limits_by_id = payload.get("rateLimitsByLimitId")
    if isinstance(rate_limits_by_id, dict):
        for limit_id, value in sorted(rate_limits_by_id.items()):
            if not isinstance(value, dict):
                continue
            snapshot = dict(value)
            if not isinstance(snapshot.get("limitId"), str) and isinstance(limit_id, str):
                snapshot["limitId"] = limit_id
            snapshots.append(snapshot)
    legacy_snapshot = payload.get("rateLimits")
    if not snapshots and isinstance(legacy_snapshot, dict):
        snapshots.append(dict(legacy_snapshot))
    if not snapshots:
        return _usage_info_unavailable(
            "Usage info unavailable.",
            tone="warning",
            detail="No rate-limit snapshots were returned.",
        )

    lines: List[str] = []
    remaining_values: List[float] = []
    for snapshot in snapshots:
        snapshot_lines, snapshot_remaining = _build_rate_limit_lines(snapshot)
        lines.extend(snapshot_lines)
        remaining_values.extend(snapshot_remaining)
    if not lines:
        return _usage_info_unavailable(
            "Usage info unavailable.",
            tone="warning",
            detail="No rate-limit window details were returned.",
        )

    minimum_remaining = min(remaining_values) if remaining_values else None
    text = (
        f"Usage remaining: {minimum_remaining:.0f}%"
        if minimum_remaining is not None
        else "Usage details available."
    )
    return {
        "text": text,
        "detail": "\n".join(lines),
        "tone": _settings_info_tone_from_remaining(minimum_remaining),
    }


def _build_information_section_fields(
    auth_status: Dict[str, object],
    usage_info: Dict[str, object],
) -> List[Dict[str, object]]:
    return [
        {
            "id": "information_section",
            "type": "section",
            "label": "Information",
            "description": "Live provider account and usage data.",
        },
        {
            "id": "auth_information",
            "type": "info",
            "label": "Account",
            "text": auth_status.get("message") or "Account status unavailable.",
            "detail": auth_status.get("detail") or "",
            "tone": _settings_info_tone_from_auth(auth_status),
        },
        {
            "id": "usage_information",
            "type": "info",
            "label": "Usage",
            "text": usage_info.get("text") or "Usage info unavailable.",
            "detail": usage_info.get("detail") or "",
            "tone": usage_info.get("tone") or "warning",
        },
    ]


def _normalize_auth_status(
    raw: object,
    *,
    extension_id: str,
    pending: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    payload = raw if isinstance(raw, dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else None
    requires_openai_auth = bool(payload.get("requiresOpenaiAuth"))
    account_type = account.get("type") if isinstance(account, dict) and isinstance(account.get("type"), str) else None
    account_email = account.get("email") if isinstance(account, dict) and isinstance(account.get("email"), str) else None
    plan_type = account.get("planType") if isinstance(account, dict) and isinstance(account.get("planType"), str) else None
    authenticated = isinstance(account, dict)
    pending_state = pending if isinstance(pending, dict) else {}
    login_id = pending_state.get("login_id") if isinstance(pending_state.get("login_id"), str) else None
    auth_url = pending_state.get("auth_url") if isinstance(pending_state.get("auth_url"), str) else None
    login_pending = bool(login_id and requires_openai_auth and not authenticated)
    status = "ready"
    if requires_openai_auth and not authenticated:
        status = "login_pending" if login_pending else "auth_required"
    message = _build_auth_status_message(
        requires_openai_auth=requires_openai_auth,
        authenticated=authenticated,
        account_type=account_type,
        account_email=account_email,
        plan_type=plan_type,
        login_pending=login_pending,
    )
    return {
        "ok": True,
        "extension_id": extension_id,
        "status": status,
        "message": message,
        "requires_openai_auth": requires_openai_auth,
        "authenticated": authenticated,
        "account_type": account_type,
        "account_email": account_email,
        "plan_type": plan_type,
        "login_pending": login_pending,
        "login_id": login_id,
        "auth_url": auth_url,
        "detail": _auth_status_detail({
            "account_email": account_email,
            "account_type": account_type,
            "plan_type": plan_type,
        }),
    }


def _looks_like_auth_required_error(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "auth required",
            "authentication required",
            "requires openai auth",
            "openai auth",
            "login required",
            "not authenticated",
        )
    )


def _looks_like_mcp_startup_error(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return (
        "mcp client for" in text
        or "mcp startup failed" in text
        or ("mcp startup" in text and "failed" in text)
    )


def _looks_like_thread_not_loaded_error(message: object) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "thread not found",
            "conversation not found",
            "no rollout found",
            "thread not loaded",
            "not loaded in memory",
        )
    )


def _build_send_failure_result(error_message: object) -> Dict[str, object]:
    message = str(error_message or "").strip() or "Message send failed"
    failure_kind = "send_failed"
    if _looks_like_mcp_startup_error(message):
        failure_kind = "mcp_startup"
    elif _looks_like_auth_required_error(message):
        failure_kind = "auth_required"
    return {
        "ok": False,
        "error": message,
        "restore_draft": True,
        "surface_error": True,
        "failure_kind": failure_kind,
        "error_type": failure_kind,
        "error_source": "codex-ext",
    }


async def _handle_auth_failure(conversation_id: str, extension_id: str, error_message: str) -> None:
    server = _server_module()
    with contextlib.suppress(Exception):
        await server._refresh_extension_runtime_state([extension_id])
    detail = None
    with contextlib.suppress(Exception):
        detail = server._extension_unavailable_detail(extension_id)
    with contextlib.suppress(Exception):
        detail_text = detail if isinstance(detail, str) and detail else error_message
        await server._emit_extension_unavailable_warning(
            conversation_id,
            extension_id,
            detail=detail_text,
        )


# Thread resume can legitimately take longer than a normal RPC round-trip while the
# app-server finishes startup work and emits the idle virtual ack.
_THREAD_RESUME_TIMEOUT_SECONDS = 45.0


async def _resume_thread_for_rpc_server(
    *,
    conversation_id: str,
    thread_id: str,
    transport: CodexAppServerTransport,
    protocol: RuntimeProtocol,
    merged_settings: Dict[str, object],
    meta: Dict[str, object],
) -> None:
    resume_params = build_request_params(protocol, "thread/resume", merged_settings, thread_id=thread_id)
    await transport.rpc_request(
        "thread/resume",
        params=resume_params,
        conversation_id=conversation_id,
        timeout=_THREAD_RESUME_TIMEOUT_SECONDS,
    )
    transport.mark_thread_ready(thread_id)
    meta["status"] = "active"
    meta["thread_runtime_signature"] = _thread_runtime_signature(protocol, merged_settings)
    meta["settings"] = merged_settings
    _save_meta(conversation_id, meta)


async def get_auth_status(extension_id: str, refresh: bool = False) -> Dict[str, object]:
    pending = dict(_auth_state_bucket(extension_id))
    if pending.get("login_id") and not refresh:
        return _normalize_auth_status(
            {"account": None, "requiresOpenaiAuth": True},
            extension_id=extension_id,
            pending=pending,
        )
    try:
        transport = await _ensure_transport_ready()
        raw = await transport.rpc_request(
            "account/read",
            params={"refreshToken": bool(refresh)},
            timeout=15.0,
        )
    except Exception as exc:
        return {
            "ok": False,
            "extension_id": extension_id,
            "status": "error",
            "message": f"Failed to read Codex auth status: {exc}",
            "requires_openai_auth": False,
            "authenticated": False,
            "account_type": None,
            "account_email": None,
            "plan_type": None,
            "login_pending": bool(pending.get("login_id")),
            "login_id": pending.get("login_id"),
            "auth_url": pending.get("auth_url"),
            "detail": "",
        }

    normalized = _normalize_auth_status(raw, extension_id=extension_id, pending=pending)
    if normalized.get("authenticated") or not normalized.get("requires_openai_auth"):
        _clear_auth_state(extension_id)
        normalized["login_pending"] = False
        normalized["login_id"] = None
        normalized["auth_url"] = None
    return normalized


async def get_splash_schema(extension_id: str) -> Dict[str, object]:
    auth_status = await get_auth_status(extension_id, refresh=False)
    tone = "success"
    if auth_status.get("status") in {"auth_required", "login_pending"}:
        tone = "warning"
    if auth_status.get("status") == "error" or auth_status.get("ok") is False:
        tone = "error"

    fields: List[Dict[str, object]] = [
        {
            "id": "auth_status",
            "type": "status",
            "label": "Authentication",
            "text": auth_status.get("message") or "Status unavailable",
            "detail": auth_status.get("detail") or "",
            "tone": tone,
        }
    ]

    if auth_status.get("status") == "error" or auth_status.get("ok") is False:
        fields.append({
            "id": "refresh_auth_status",
            "type": "action",
            "label": "Authentication",
            "button_label": "Refresh Status",
            "action_id": "refresh_auth_status",
            "description": "Retry the Codex auth status probe.",
        })
    elif auth_status.get("authenticated"):
        fields.append({
            "id": "logout_auth",
            "type": "action",
            "label": "Authentication",
            "button_label": "Log Out",
            "action_id": "logout_auth",
            "description": "Clear the current Codex OpenAI login state.",
        })
    elif auth_status.get("requires_openai_auth"):
        if auth_status.get("login_pending"):
            fields.append({
                "id": "cancel_auth_login",
                "type": "action",
                "label": "Authentication",
                "button_label": "Cancel Login",
                "action_id": "cancel_auth_login",
                "description": "Cancel the pending ChatGPT login flow.",
            })
            fields.append({
                "id": "refresh_auth_status",
                "type": "action",
                "label": "Authentication",
                "button_label": "Refresh Status",
                "action_id": "refresh_auth_status",
                "description": "Refresh auth state after finishing sign-in in the browser.",
            })
        else:
            fields.append({
                "id": "login_chatgpt",
                "type": "action",
                "label": "Authentication",
                "button_label": "Log In with ChatGPT",
                "action_id": "login_chatgpt",
                "description": "Open the Codex ChatGPT login flow in your browser.",
                "open_strategy": "host",
                "opens_window": True,
            })

    return {
        "version": "1",
        "extension_id": extension_id,
        "description": "Splash settings schema for extension-scoped auth controls.",
        "fields": fields,
    }


async def run_splash_action(
    extension_id: str,
    action_id: str,
    payload: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    action = str(action_id or "").strip().lower()
    params = payload if isinstance(payload, dict) else {}
    auth_state = _auth_state_bucket(extension_id)
    try:
        transport = await _ensure_transport_ready()
        if action == "login_chatgpt":
            result = await transport.rpc_request(
                "account/login/start",
                params={"type": "chatgpt"},
                timeout=15.0,
            )
            if not isinstance(result, dict):
                return {"ok": False, "error": "Invalid login response"}
            auth_url = result.get("authUrl")
            login_id = result.get("loginId")
            if not isinstance(auth_url, str) or not auth_url.strip() or not isinstance(login_id, str) or not login_id.strip():
                return {"ok": False, "error": "ChatGPT login URL unavailable"}
            auth_state.clear()
            auth_state.update({
                "login_id": login_id.strip(),
                "auth_url": auth_url.strip(),
            })
            opened_externally, open_error = await _open_url_with_xdg_open(auth_url.strip())
            message = "ChatGPT login started. Finish sign-in in the opened browser."
            if not opened_externally and open_error:
                message = f"ChatGPT login started, but xdg-open failed: {open_error}"
            return {
                "ok": True,
                "message": message,
                "opened_externally": opened_externally,
                "open_url": auth_url.strip(),
            }
        if action == "cancel_auth_login":
            login_id = params.get("login_id") if isinstance(params.get("login_id"), str) else auth_state.get("login_id")
            if not isinstance(login_id, str) or not login_id.strip():
                return {"ok": False, "error": "No pending login to cancel"}
            result = await transport.rpc_request(
                "account/login/cancel",
                params={"loginId": login_id.strip()},
                timeout=15.0,
            )
            auth_state.clear()
            await _refresh_extension_auth_state(extension_id)
            cancel_status = result.get("status") if isinstance(result, dict) else None
            message = "Pending login canceled."
            if cancel_status == "notFound":
                message = "Pending login no longer exists."
            return {"ok": True, "message": message}
        if action == "logout_auth":
            await transport.rpc_request(
                "account/logout",
                params=None,
                timeout=15.0,
            )
            auth_state.clear()
            await _refresh_extension_auth_state(extension_id)
            return {"ok": True, "message": "Logged out of Codex OpenAI auth."}
        if action == "refresh_auth_status":
            status = await get_auth_status(extension_id, refresh=True)
            await _refresh_extension_auth_state(extension_id)
            return {
                "ok": bool(status.get("ok", True)),
                "message": status.get("message") or "Auth status refreshed.",
            }
        return {"ok": False, "error": f"Unknown splash action: {action_id}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _sort_session_entries(entries: List[Dict[str, object]], cwd: Optional[str]) -> List[Dict[str, object]]:
    if not cwd:
        return entries
    resolved_cwd = os.path.realpath(os.path.expanduser(cwd))

    def relevance(entry: Dict[str, object]) -> int:
        ctx = _object_dict(entry.get("context"))
        session_cwd_value = ctx.get("cwd")
        session_cwd = session_cwd_value if isinstance(session_cwd_value, str) else ""
        if not session_cwd:
            return 9
        resolved_session_cwd = os.path.realpath(session_cwd)
        if resolved_session_cwd == resolved_cwd:
            return 0
        if resolved_session_cwd.startswith(resolved_cwd) or resolved_cwd.startswith(resolved_session_cwd):
            return 1
        return 9

    return sorted(entries, key=relevance)


def _build_plan_state(
    steps: List[Dict[str, str]],
    *,
    explanation: Optional[str] = None,
    source: str,
) -> Dict[str, object]:
    normalized_steps = normalize_plan_steps(steps)
    return {
        "has_plan": False,
        "has_todo": True,
        "plan_exists": False,
        "plan_content": render_plan_markdown(normalized_steps, explanation),
        "plan_steps": normalized_steps,
        "plan_source": source,
    }


def _latest_transcript_plan(conversation_id: str) -> Optional[Dict[str, object]]:
    transcript_path = _server_module()._conversation_transcript_path(conversation_id)
    if not isinstance(transcript_path, Path) or not transcript_path.is_file():
        return None
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = cast(object, json.loads(line))
        except Exception:
            continue
        if isinstance(entry, dict) and entry.get("role") == "plan":
            return _object_dict(entry)
    return None


def init_codex_app_server_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[_MetaFns] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    global _broadcast_fn, _transcript_fn, _meta_fns
    global _registered_extension_ids, _transport

    _registered_extension_ids = {
        ext_id
        for ext_id in (registered_extension_ids or [])
        if isinstance(ext_id, str) and ext_id
    }
    _ready_extensions.clear()
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = meta_fns
    configure_runtime_protocol(server_root=server_root, extensions_dir=extensions_dir)
    _transport = CodexAppServerTransport(
        server_root=server_root,
        fws_getter=fws_getter,
        broadcast_fn=broadcast_fn,
        transcript_fn=transcript_fn,
        meta_fns=meta_fns,
        raw_log_fn=_add_to_raw_buffer,
        auth_event_handler=_handle_auth_transport_event,
    )
    print("[Codex] Extension initialized (app-server binary handler)")


def init_codex_ext_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[_MetaFns] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    init_codex_app_server_manager(
        extensions_dir,
        server_root,
        fws_getter,
        broadcast_fn,
        transcript_fn,
        meta_fns,
        registered_extension_ids=registered_extension_ids,
    )


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    results = {ext_id: False for ext_id in sorted(_registered_extension_ids)}
    try:
        await asyncio.wait_for(get_runtime_protocol(), timeout=timeout)
        await asyncio.wait_for(_ensure_transport_ready(), timeout=timeout)
        for ext_id in results:
            results[ext_id] = True
    except Exception as exc:
        print(f"[Codex] warm-up failed: {exc}")
    return results


def is_extension_ready(extension_id: str) -> bool:
    return extension_id in _ready_extensions and _transport is not None and _transport.is_ready()


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    if is_extension_ready(extension_id):
        return True
    try:
        await asyncio.wait_for(get_runtime_protocol(), timeout=timeout)
        await asyncio.wait_for(_ensure_transport_ready(), timeout=timeout)
    except Exception as exc:
        print(f"[Codex] wait_extension_ready failed for {extension_id}: {exc}")
        return False
    _ready_extensions.add(extension_id)
    return True


async def get_settings_schema(extension_id: str) -> Dict[str, object]:
    protocol = await get_runtime_protocol()
    schema = copy.deepcopy(build_settings_schema(protocol, extension_id))
    auth_status = await get_auth_status(extension_id, refresh=False)
    usage_info = await get_usage_info(extension_id, auth_status=auth_status)
    fields = schema.get("fields")
    schema["fields"] = _build_information_section_fields(auth_status, usage_info) + (
        list(fields) if isinstance(fields, list) else []
    )
    schema["cache"] = "none"
    return schema


async def get_request_card_schemas(extension_id: str) -> Dict[str, object]:
    protocol = await get_runtime_protocol()
    methods = (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "item/tool/call",
        "mcpServer/elicitation/request",
    )
    schemas: Dict[str, object] = {}
    for method in methods:
        request_schema = protocol.server_request_schema(method)
        response_schema = protocol.server_request_response_schema(method)
        if isinstance(request_schema, dict) and isinstance(response_schema, dict):
            schemas[method.lower()] = {
                "request": request_schema,
                "response": response_schema,
            }
    return schemas


async def list_models() -> List[Dict[str, object]]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("model/list", params={}, timeout=15.0)
    items = result.get("data", []) if isinstance(result, dict) else []
    models: List[Dict[str, object]] = []
    if not isinstance(items, list):
        return models
    for item in items:
        if not isinstance(item, dict):
            continue
        model = dict(item)
        if not model.get("id"):
            model_id = model.get("name")
            if isinstance(model_id, str) and model_id:
                model["id"] = model_id
        if not model.get("name"):
            display_name = model.get("displayName")
            if isinstance(display_name, str) and display_name:
                model["name"] = display_name
            elif isinstance(model.get("id"), str):
                model["name"] = model["id"]
        models.append(model)
    return models


async def list_sessions(cwd: Optional[str] = None) -> List[Dict[str, object]]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("thread/list", params={"limit": 200}, timeout=15.0)
    items_raw = result.get("data", []) if isinstance(result, dict) else []
    sessions: List[Dict[str, object]] = []
    if not isinstance(items_raw, list):
        return sessions
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        session_id = item.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        entry: Dict[str, object] = {
            "session_id": session_id,
            "summary": item.get("preview") or "",
            "active": False,
        }
        session_cwd = item.get("cwd")
        if isinstance(session_cwd, str) and session_cwd:
            entry["context"] = {"cwd": session_cwd}
        sessions.append(entry)
    return _sort_session_entries(sessions, cwd)


async def route_event(
    extension_id: str,
    label: Optional[str],
    payload: object,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, object]:
    transport = _ensure_transport()
    return await transport.route_event(
        label=label or "",
        payload=payload,
        conversation_id=conversation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        request_id=request_id,
    )


async def read_plan(extension_id: str, conversation_id: str) -> Dict[str, object]:
    if not _meta_fns or "load" not in _meta_fns:
        return {
            "has_plan": False,
            "has_todo": True,
            "plan_exists": False,
            "plan_content": "",
            "plan_steps": [],
            "plan_source": "unavailable",
        }

    meta = _meta_fns["load"](conversation_id)
    active_plan = meta.get("active_plan") if isinstance(meta, dict) and isinstance(meta.get("active_plan"), dict) else None
    if isinstance(active_plan, dict):
        active_steps = normalize_plan_steps(active_plan.get("steps"))
        if active_steps:
            explanation = active_plan.get("explanation")
            return _build_plan_state(
                active_steps,
                explanation=explanation if isinstance(explanation, str) else None,
                source="active_meta",
            )

    transcript_plan = _latest_transcript_plan(conversation_id)
    if isinstance(transcript_plan, dict):
        steps = normalize_plan_steps(transcript_plan.get("steps"))
        if steps:
            explanation = transcript_plan.get("explanation")
            return _build_plan_state(
                steps,
                explanation=explanation if isinstance(explanation, str) else None,
                source="transcript",
            )

    return {
        "has_plan": False,
        "has_todo": True,
        "plan_exists": False,
        "plan_content": "",
        "plan_steps": [],
        "plan_source": "none",
    }


async def resume_session(
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    meta = _object_dict(_meta_fns["load"](conversation_id))
    if not meta:
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}
    thread_id = meta.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"ok": False, "error": f"No thread_id for conversation {conversation_id[:8]}"}

    transport = await _ensure_transport_ready()
    protocol = await get_runtime_protocol()
    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=cwd,
        model=model,
    )
    try:
        await _resume_thread_for_rpc_server(
            conversation_id=conversation_id,
            thread_id=thread_id,
            transport=transport,
            protocol=protocol,
            merged_settings=merged_settings,
            meta=meta,
        )
        _add_to_raw_buffer("out", conversation_id, f"thread_resumed {thread_id[:8]}")
        return {"ok": True, "session_id": thread_id}
    except Exception as exc:
        if _looks_like_auth_required_error(exc):
            settings_dict = _object_dict(meta.get("settings"))
            agent_value = settings_dict.get("agent")
            agent_name = agent_value if isinstance(agent_value, str) and agent_value else "codex-ext"
            await _handle_auth_failure(conversation_id, agent_name, str(exc))
        _add_to_raw_buffer("err", conversation_id, f"resume_failed {exc}")
        return {"ok": False, "error": f"Thread resume failed: {exc}"}


async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, object],
) -> Dict[str, object]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    if not conversation_id or not text:
        return {"ok": False, "error": "conversation_id and text required"}

    meta = _object_dict(_meta_fns["load"](conversation_id))
    if not meta:
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}

    transport = await _ensure_transport_ready()
    protocol = await get_runtime_protocol()
    cwd_value = settings.get("cwd")
    model_value = settings.get("model")
    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=cwd_value if isinstance(cwd_value, str) and cwd_value.strip() else None,
        model=model_value if isinstance(model_value, str) and model_value.strip() else None,
    )
    thread_id_value = meta.get("thread_id")
    thread_id = thread_id_value if isinstance(thread_id_value, str) and thread_id_value else None
    current_signature = _thread_runtime_signature(protocol, merged_settings)

    try:
        if thread_id:
            turn_params = build_request_params(
                protocol,
                "turn/start",
                merged_settings,
                thread_id=thread_id,
                text=text,
            )
            try:
                await transport.rpc_request(
                    "turn/start",
                    params=turn_params,
                    conversation_id=conversation_id,
                    timeout=15.0,
                )
                transport.mark_thread_ready(thread_id)
                meta["status"] = "active"
                meta["settings"] = merged_settings
                _save_meta(conversation_id, meta)
            except Exception as exc:
                if not _looks_like_thread_not_loaded_error(exc):
                    raise
                _add_to_raw_buffer(
                    "out",
                    conversation_id,
                    f"turn_start_resume thread={thread_id[:8]} error={str(exc)[:200]}",
                )
                await _resume_thread_for_rpc_server(
                    conversation_id=conversation_id,
                    thread_id=thread_id,
                    transport=transport,
                    protocol=protocol,
                    merged_settings=merged_settings,
                    meta=meta,
                )
                retry_turn_params = build_request_params(
                    protocol,
                    "turn/start",
                    merged_settings,
                    thread_id=thread_id,
                    text=text,
                )
                await transport.rpc_request(
                    "turn/start",
                    params=retry_turn_params,
                    conversation_id=conversation_id,
                    timeout=15.0,
                )
        else:
            start_params = build_request_params(protocol, "thread/start", merged_settings)
            start_result = await transport.rpc_request(
                "thread/start",
                params=start_params,
                conversation_id=conversation_id,
                timeout=15.0,
            )
            thread_id = _extract_thread_id_from_result(start_result)

            if not thread_id:
                meta = _object_dict(_meta_fns["load"](conversation_id))
                thread_id_value = meta.get("thread_id")
                thread_id = thread_id_value if isinstance(thread_id_value, str) and thread_id_value else None

            if not thread_id:
                return {"ok": False, "error": "Failed to start thread - no thread_id received"}

            transport.mark_thread_ready(thread_id)
            meta["thread_id"] = thread_id
            meta["status"] = "active"
            meta["settings"] = merged_settings
            meta["thread_runtime_signature"] = current_signature
            _save_meta(conversation_id, meta)

            turn_params = build_request_params(
                protocol,
                "turn/start",
                merged_settings,
                thread_id=thread_id,
                text=text,
            )
            await transport.rpc_request(
                "turn/start",
                params=turn_params,
                conversation_id=conversation_id,
                timeout=15.0,
            )

        if not isinstance(thread_id, str) or not thread_id:
            return {"ok": False, "error": "Failed to resolve thread_id after message send"}
        _add_to_raw_buffer("out", conversation_id, f"turn_start thread={thread_id[:8]} text={text[:120]}")
        return {"ok": True, "thread_id": thread_id, "conversation_id": conversation_id}
    except Exception as exc:
        if _looks_like_auth_required_error(exc):
            await _handle_auth_failure(conversation_id, agent_type or "codex-ext", str(exc))
        _add_to_raw_buffer("err", conversation_id, f"handle_message_failed {exc}")
        return _build_send_failure_result(exc)


async def resume_session_with_history(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    extension_id: Optional[str] = None,
) -> Dict[str, object]:
    if not _meta_fns or "load" not in _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    meta = _object_dict(_meta_fns["load"](conversation_id))
    if not meta:
        return {"ok": False, "error": f"Conversation not found: {conversation_id[:8]}"}

    existing = meta.get("thread_id")
    if isinstance(existing, str) and existing and existing != session_id:
        return {"ok": False, "error": f"Conversation already bound to thread {existing[:8]}"}

    merged_settings = _merge_runtime_settings(
        conversation_id,
        settings=settings,
        cwd=cwd,
        model=model,
    )
    if isinstance(extension_id, str) and extension_id:
        merged_settings["agent"] = extension_id

    meta["thread_id"] = session_id
    meta["status"] = "active"
    meta["settings"] = merged_settings
    _save_meta(conversation_id, meta)

    result = await resume_session(
        conversation_id,
        cwd=cwd,
        model=model,
        settings=merged_settings,
    )
    if not result.get("ok"):
        return result
    return {"ok": True, "session_id": session_id, "conversation_id": conversation_id}


async def hydrate_transcript(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> List[Dict[str, object]]:
    del cwd, model, settings
    path = find_rollout_path(session_id)
    if not path:
        _add_to_raw_buffer("err", conversation_id, f"hydrate_transcript rollout_not_found session={session_id[:8]}")
        return []
    preview = await asyncio.to_thread(preview_entries, path, 200000)
    items_value = preview.get("items") if isinstance(preview, dict) else None
    items: List[Dict[str, object]] = []
    if isinstance(items_value, list):
        for item in items_value:
            if isinstance(item, dict):
                items.append({str(key): value for key, value in item.items()})
    _add_to_raw_buffer("out", conversation_id, f"hydrate_transcript imported={len(items)} session={session_id[:8]}")
    return items


def resolve_approval(request_id: str, resolution: object) -> bool:
    transport = _transport
    if transport is None:
        return False
    return transport.resolve_approval(request_id, resolution)


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: Dict[str, object]) -> bool:
    if not isinstance(descriptor, dict):
        return False
    transport = _transport
    if transport is None:
        return False
    meta = _meta_fns["load"](conversation_id) if _meta_fns and "load" in _meta_fns else {}
    if descriptor.get("conversation_id") and descriptor.get("conversation_id") != conversation_id:
        return False
    pending_thread_id = descriptor.get("thread_id")
    current_thread_id = meta.get("thread_id") if isinstance(meta, dict) else None
    if pending_thread_id and current_thread_id and pending_thread_id != current_thread_id:
        return False
    if pending_thread_id and not current_thread_id:
        return False
    runtime_signature = descriptor.get("runtime_signature")
    current_signature = meta.get("thread_runtime_signature") if isinstance(meta, dict) else None
    if runtime_signature and not current_signature:
        return False
    if runtime_signature and current_signature and runtime_signature != current_signature:
        return False
    runtime_instance_id = descriptor.get("runtime_instance_id")
    current_instance_id = transport.runtime_instance_id()
    if runtime_instance_id and not current_instance_id:
        return False
    if runtime_instance_id and current_instance_id and runtime_instance_id != current_instance_id:
        return False
    return transport.has_pending_approval(request_id)


async def abort_session(conversation_id: str) -> bool:
    if not _meta_fns or "load" not in _meta_fns:
        return False
    meta = _meta_fns["load"](conversation_id)
    if not isinstance(meta, dict):
        return False
    thread_id = meta.get("thread_id")
    turn_id = meta.get("turn_id")
    if not isinstance(thread_id, str) or not thread_id:
        return False
    if not isinstance(turn_id, str) or not turn_id:
        return False

    try:
        transport = await _ensure_transport_ready()
        protocol = await get_runtime_protocol()
        params = build_request_params(protocol, "turn/interrupt", {}, thread_id=thread_id, turn_id=turn_id)
        await transport.rpc_request(
            "turn/interrupt",
            params=params,
            conversation_id=conversation_id,
            timeout=10.0,
        )
        _add_to_raw_buffer("out", conversation_id, f"turn_interrupt thread={thread_id[:8]} turn={turn_id[:8]}")
        return True
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"interrupt_failed {exc}")
        return False


async def compact_session(conversation_id: str) -> Dict[str, object]:
    """Send thread/compact/start to the extension-owned app-server transport.

    Try compact directly first, then resume and retry only for the canonical
    thread-not-loaded startup errors.
    """
    if not _meta_fns or "load" not in _meta_fns:
        return {"ok": False, "error": "meta_fns not available"}
    meta = _object_dict(_meta_fns["load"](conversation_id))
    if not meta:
        return {"ok": False, "error": "conversation not found"}
    thread_id = meta.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        return {"ok": False, "error": "no active thread"}

    try:
        transport = await _ensure_transport_ready()
        protocol = await get_runtime_protocol()
        existing_settings = _object_dict(meta.get("settings"))
        existing_cwd = existing_settings.get("cwd")
        existing_model = existing_settings.get("model")
        merged_settings = _merge_runtime_settings(
            conversation_id,
            settings=existing_settings,
            cwd=existing_cwd if isinstance(existing_cwd, str) and existing_cwd else None,
            model=existing_model if isinstance(existing_model, str) and existing_model else None,
        )
        params = build_request_params(protocol, "thread/compact/start", {}, thread_id=thread_id)
        try:
            await transport.rpc_request(
                "thread/compact/start",
                params=params,
                conversation_id=conversation_id,
                timeout=30.0,
            )
            transport.mark_thread_ready(thread_id)
            meta["status"] = "active"
            meta["settings"] = merged_settings
            _save_meta(conversation_id, meta)
        except Exception as exc:
            if not _looks_like_thread_not_loaded_error(exc):
                raise
            await _resume_thread_for_rpc_server(
                conversation_id=conversation_id,
                thread_id=thread_id,
                transport=transport,
                protocol=protocol,
                merged_settings=merged_settings,
                meta=meta,
            )
            _add_to_raw_buffer("out", conversation_id, f"compact_resume thread={thread_id[:8]}")
            params = build_request_params(protocol, "thread/compact/start", {}, thread_id=thread_id)
            await transport.rpc_request(
                "thread/compact/start",
                params=params,
                conversation_id=conversation_id,
                timeout=30.0,
            )
        _add_to_raw_buffer("out", conversation_id, f"compact_start thread={thread_id[:8]}")
        return {"ok": True, "thread_id": thread_id, "conversation_id": conversation_id}
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"compact_failed {exc}")
        return {"ok": False, "error": str(exc), "thread_id": thread_id, "conversation_id": conversation_id}


async def shutdown_client() -> None:
    if _transport is not None:
        await _transport.stop()
