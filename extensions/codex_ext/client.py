"""
Codex App Server Client

Extension handler for Codex app-server using runtime-generated protocol schema
from the installed binary plus the generic extension hook surface.
"""

import asyncio
import contextlib
import importlib
import json
import os
import shutil
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Protocol, TypeGuard, cast

from .plan_utils import normalize_plan_steps, render_plan_markdown
from .rollout_import import find_rollout_path, preview_entries
from .runtime_protocol import (
    RuntimeProtocol,
    SchemaDict,
    build_runtime_option_descriptors,
    build_thread_list_params,
    build_request_params,
    build_thread_runtime_signature_payload,
    configure_runtime_protocol,
    get_runtime_protocol,
    normalize_thread_list_timestamp,
)
from .transport import CodexAppServerTransport, MetaFns, ShellManager


class _ExtensionsModule(Protocol):
    def get_extension_info(self, extension_id: str) -> object: ...


def _is_object_dict(value: object) -> TypeGuard[Dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[List[object]]:
    return isinstance(value, list)


# Stored references to server callbacks
_broadcast_fn: Optional[Callable[[Dict[str, object]], Awaitable[None]]] = None
_transcript_fn: Optional[Callable[[str, Dict[str, object]], Awaitable[None]]] = None
_meta_fns: Optional[MetaFns] = None
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


def _extensions_module() -> _ExtensionsModule:
    return cast(_ExtensionsModule, importlib.import_module("extensions"))


def _object_dict(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: Dict[str, object] = {}
    for key, item_value in cast(Iterable[tuple[object, object]], value.items()):
        result[str(key)] = item_value
    return result


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
        if _is_object_dict(settings_value):
            merged.update(_object_dict(settings_value))
    if _is_object_dict(settings):
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
    if not _is_object_dict(settings):
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
        manifest = _object_dict(ext_info.get("manifest")) if _is_object_dict(ext_info) else {}
        model = _object_dict(manifest.get("model"))
        model_name = model.get("name")
        if isinstance(model_name, str) and model_name.strip():
            merged["model"] = model_name.strip()
    return merged


def _extract_thread_id_from_result(payload: object) -> Optional[str]:
    if _is_object_dict(payload):
        thread = payload.get("thread")
        if _is_object_dict(thread) and thread.get("id"):
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
    if not _is_object_dict(bucket):
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
        if ext_id.strip()
    ]
    if explicit:
        return explicit
    registered = sorted(
        ext_id
        for ext_id in _registered_extension_ids
        if ext_id.strip()
    )
    return registered or ["codex-ext"]


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

    if label_lower == "account/login/completed" and _is_object_dict(payload):
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

    if label_lower == "account/updated" and _is_object_dict(payload):
        for extension_id in extension_ids:
            _clear_auth_state(extension_id)
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
    message = stderr.decode("utf-8", errors="replace").strip()
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
    state: str = "unavailable",
) -> Dict[str, object]:
    return {
        "supported": False,
        "state": state,
        "text": message,
        "detail": detail,
        "tone": tone,
    }


def _rate_limit_window_detail(label: str, raw_window: object) -> tuple[Optional[float], Optional[str]]:
    if not _is_object_dict(raw_window):
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
    if _is_object_dict(credits):
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
            state="error",
        )
    status = str(resolved_auth_status.get("status") or "").strip().lower()
    if status == "login_pending":
        return _usage_info_unavailable(
            "Usage unavailable while ChatGPT login is pending.",
            tone="warning",
            state="login_pending",
            detail="Finish sign-in in the opened browser, then reopen settings.",
        )
    if status == "auth_required":
        return _usage_info_unavailable(
            "Usage unavailable until ChatGPT sign-in is complete.",
            tone="warning",
            state="auth_required",
        )
    if not resolved_auth_status.get("requires_openai_auth"):
        return _usage_info_unavailable(
            "Usage info is not required for the current provider.",
            tone="success",
            state="not_applicable",
        )
    if resolved_auth_status.get("account_type") == "apiKey":
        return _usage_info_unavailable(
            "Usage info is unavailable for API key authentication.",
            tone="success",
            detail="ChatGPT rate-limit snapshots are only available for ChatGPT-authenticated accounts.",
            state="not_applicable",
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
            state="error",
        )

    payload = raw if _is_object_dict(raw) else {}
    snapshots: List[Dict[str, object]] = []
    rate_limits_by_id = payload.get("rateLimitsByLimitId")
    if _is_object_dict(rate_limits_by_id):
        for limit_id, value in sorted(rate_limits_by_id.items()):
            if not _is_object_dict(value):
                continue
            snapshot = _object_dict(value)
            if not isinstance(snapshot.get("limitId"), str):
                snapshot["limitId"] = limit_id
            snapshots.append(snapshot)
    legacy_snapshot = payload.get("rateLimits")
    if not snapshots and _is_object_dict(legacy_snapshot):
        snapshots.append(dict(legacy_snapshot))
    if not snapshots:
        return _usage_info_unavailable(
            "Usage info unavailable.",
            tone="warning",
            detail="No rate-limit snapshots were returned.",
            state="unavailable",
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
            state="unavailable",
        )

    minimum_remaining = min(remaining_values) if remaining_values else None
    text = (
        f"Usage remaining: {minimum_remaining:.0f}%"
        if minimum_remaining is not None
        else "Usage details available."
    )
    return {
        "supported": True,
        "state": "available",
        "text": text,
        "detail": "\n".join(lines),
        "tone": _settings_info_tone_from_remaining(minimum_remaining),
        "remaining_percent": minimum_remaining,
    }


def _provider_status_tone(auth_status: Dict[str, object]) -> str:
    status = str(auth_status.get("status") or "").strip().lower()
    if auth_status.get("ok") is False or status == "error":
        return "error"
    if status in {"auth_required", "login_pending"}:
        return "warning"
    return "success"


def _provider_status_items(auth_status: Dict[str, object]) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    for label, key in (
        ("Account", "account_email"),
        ("Plan", "plan_type"),
        ("Auth", "account_type"),
    ):
        value = auth_status.get(key)
        if isinstance(value, str) and value.strip():
            items.append({"label": label, "value": value.strip()})
    return items


async def get_provider_info(
    extension_id: str,
    conversation_id: Optional[str] = None,
    provider_session_id: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    del conversation_id, provider_session_id, settings
    auth_status = await get_auth_status(extension_id, refresh=False)
    status_state = str(auth_status.get("status") or "unknown").strip() or "unknown"
    status_payload: Dict[str, object] = {
        "supported": True,
        "state": status_state,
        "text": auth_status.get("message") if isinstance(auth_status.get("message"), str) else "Provider status unavailable.",
        "detail": auth_status.get("detail") if isinstance(auth_status.get("detail"), str) else "",
        "tone": _provider_status_tone(auth_status),
        "items": _provider_status_items(auth_status),
    }
    usage_payload = await get_usage_info(extension_id, auth_status=auth_status)
    return {
        "ok": auth_status.get("ok") is not False,
        "supported": True,
        "extension_id": extension_id,
        "provider": "codex",
        "status": status_payload,
        "usage": usage_payload,
    }


def _normalize_auth_status(
    raw: object,
    *,
    extension_id: str,
    pending: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    payload = raw if _is_object_dict(raw) else {}
    account_value = payload.get("account")
    account = account_value if _is_object_dict(account_value) else None
    requires_openai_auth = bool(payload.get("requiresOpenaiAuth"))
    account_type_value = account.get("type") if account is not None else None
    account_email_value = account.get("email") if account is not None else None
    plan_type_value = account.get("planType") if account is not None else None
    account_type = account_type_value if isinstance(account_type_value, str) else None
    account_email = account_email_value if isinstance(account_email_value, str) else None
    plan_type = plan_type_value if isinstance(plan_type_value, str) else None
    authenticated = account is not None
    pending_state = pending if _is_object_dict(pending) else {}
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
    exclude_turns: bool = False,
) -> None:
    resume_params = build_request_params(
        protocol,
        "thread/resume",
        merged_settings,
        thread_id=thread_id,
        exclude_turns=exclude_turns,
    )
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
    params = payload if _is_object_dict(payload) else {}
    auth_state = _auth_state_bucket(extension_id)
    try:
        transport = await _ensure_transport_ready()
        if action == "login_chatgpt":
            result = await transport.rpc_request(
                "account/login/start",
                params={"type": "chatgpt"},
                timeout=15.0,
            )
            if not _is_object_dict(result):
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
            cancel_status = result.get("status") if _is_object_dict(result) else None
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
            return {"ok": True, "message": "Logged out of Codex OpenAI auth."}
        if action == "refresh_auth_status":
            status = await get_auth_status(extension_id, refresh=True)
            return {
                "ok": bool(status.get("ok", True)),
                "message": status.get("message") or "Auth status refreshed.",
            }
        return {"ok": False, "error": f"Unknown splash action: {action_id}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _normalize_session_sort_key(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"created", "created_at"}:
        return "created_at"
    return "updated_at"


def _session_sort_marker(value: object) -> tuple[int, float, str]:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        return (1, 0.0, "")
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    with contextlib.suppress(ValueError):
        return (0, -datetime.fromisoformat(normalized).timestamp(), raw)
    return (0, 0.0, raw)


def _sort_session_entries(
    entries: List[Dict[str, object]],
    cwd: Optional[str],
    sort_key: str = "updated_at",
) -> List[Dict[str, object]]:
    resolved_cwd = os.path.realpath(os.path.expanduser(cwd)) if cwd else ""
    normalized_sort_key = _normalize_session_sort_key(sort_key)

    def relevance(entry: Dict[str, object]) -> int:
        if not resolved_cwd:
            return 9
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

    ordered = sorted(entries, key=lambda entry: str(entry.get("session_id") or entry.get("id") or ""))
    ordered.sort(
        key=lambda entry: (
            _session_sort_marker(
                entry.get(normalized_sort_key) or entry.get("updated_at") or entry.get("created_at") or ""
            ),
            str(entry.get("session_id") or entry.get("id") or ""),
        ),
    )
    ordered.sort(key=relevance)
    return ordered


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


def init_codex_app_server_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable[[], Awaitable[ShellManager]],
    broadcast_fn: Callable[[Dict[str, object]], Awaitable[None]],
    transcript_fn: Callable[[str, Dict[str, object]], Awaitable[None]],
    meta_fns: Optional[MetaFns] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    global _broadcast_fn, _transcript_fn, _meta_fns
    global _registered_extension_ids, _transport

    _registered_extension_ids = {
        ext_id
        for ext_id in (registered_extension_ids or [])
        if ext_id
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
    fws_getter: Callable[[], Awaitable[ShellManager]],
    broadcast_fn: Callable[[Dict[str, object]], Awaitable[None]],
    transcript_fn: Callable[[str, Dict[str, object]], Awaitable[None]],
    meta_fns: Optional[MetaFns] = None,
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


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    protocol = await get_runtime_protocol()
    merged = _merge_runtime_settings(
        conversation_id or "",
        settings=settings,
    ) if conversation_id else _materialize_runtime_settings(settings or {})
    result = build_runtime_option_descriptors(
        protocol,
        merged,
        mode_options=await _collaboration_mode_options(),
    )
    result["agent"] = extension_id
    return result


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
        if _is_object_dict(request_schema) and _is_object_dict(response_schema):
            schemas[method.lower()] = {
                "request": request_schema,
                "response": response_schema,
            }
    return schemas


async def list_models() -> List[Dict[str, object]]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("model/list", params={}, timeout=15.0)
    items: object = result.get("data")
    models: List[Dict[str, object]] = []
    if not _is_object_list(items):
        return models
    for item in items:
        if not _is_object_dict(item):
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


async def _collaboration_mode_options() -> List[SchemaDict]:
    transport = await _ensure_transport_ready()
    result = await transport.rpc_request("collaborationMode/list", params={}, timeout=15.0)
    items: object = result.get("data")
    options: List[SchemaDict] = []
    seen: set[str] = set()
    if not _is_object_list(items):
        return options
    for item in items:
        if not _is_object_dict(item):
            continue
        mode = item.get("mode")
        if not isinstance(mode, str) or not mode or mode in seen:
            continue
        name = item.get("name")
        options.append({
            "value": mode,
            "label": name if isinstance(name, str) and name else mode,
        })
        seen.add(mode)
    return options


async def list_sessions(cwd: Optional[str] = None, sort: Optional[str] = None) -> List[Dict[str, object]]:
    protocol = await get_runtime_protocol()
    transport = await _ensure_transport_ready()
    sort_key = _normalize_session_sort_key(sort)
    result = await transport.rpc_request(
        "thread/list",
        params=build_thread_list_params(protocol, limit=200),
        timeout=15.0,
    )
    items_raw: object = result.get("data")
    sessions: List[Dict[str, object]] = []
    if not _is_object_list(items_raw):
        return sessions
    for item in items_raw:
        if not _is_object_dict(item):
            continue
        session_id = item.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        entry: Dict[str, object] = {
            "session_id": session_id,
            "summary": item.get("preview") or "",
            "active": False,
        }
        created_at = normalize_thread_list_timestamp(protocol, "createdAt", item.get("createdAt"))
        if created_at:
            entry["created_at"] = created_at
        updated_at = normalize_thread_list_timestamp(protocol, "updatedAt", item.get("updatedAt"))
        if updated_at:
            entry["updated_at"] = updated_at
        elif created_at:
            entry["updated_at"] = created_at
        session_cwd = item.get("cwd")
        if isinstance(session_cwd, str) and session_cwd:
            entry["context"] = {"cwd": session_cwd}
        sessions.append(entry)
    return _sort_session_entries(sessions, cwd, sort_key)


def _bound_thread_id(conversation_id: str, provider_session_id: Optional[str]) -> Optional[str]:
    if isinstance(provider_session_id, str) and provider_session_id.strip():
        return provider_session_id.strip()
    if _meta_fns and "load" in _meta_fns:
        meta = _object_dict(_meta_fns["load"](conversation_id))
        thread_id = meta.get("thread_id") or meta.get("provider_session_id")
        if isinstance(thread_id, str) and thread_id.strip():
            return thread_id.strip()
    return None


async def get_live_session_state(
    conversation_id: str,
    provider_session_id: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    thread_id = _bound_thread_id(conversation_id, provider_session_id)
    if not thread_id:
        return {
            "ok": True,
            "supported": True,
            "state": "unbound",
            "loaded": False,
            "unload_supported": False,
        }

    transport = _transport
    if transport is None or not transport.is_ready():
        return {
            "ok": True,
            "supported": True,
            "state": "cold",
            "loaded": False,
            "unload_supported": True,
            "provider_session_id": thread_id,
        }

    busy = False
    if _meta_fns and "load" in _meta_fns:
        meta = _object_dict(_meta_fns["load"](conversation_id))
        busy = isinstance(meta.get("turn_id"), str) and bool(str(meta.get("turn_id")).strip())

    try:
        await get_runtime_protocol()
        result = await transport.rpc_request_unchecked(
            "thread/loaded/list",
            params={},
            conversation_id=conversation_id,
            timeout=5.0,
        )
        loaded_ids: object = result.get("data")
        loaded = thread_id in loaded_ids if _is_object_list(loaded_ids) else transport.is_thread_ready(thread_id)
        return {
            "ok": True,
            "supported": True,
            "state": "loaded" if loaded else "cold",
            "loaded": bool(loaded),
            "busy": busy,
            "unload_supported": True,
            "provider_session_id": thread_id,
        }
    except Exception as exc:
        loaded = transport.is_thread_ready(thread_id)
        return {
            "ok": False,
            "supported": True,
            "state": "loaded" if loaded else "unknown",
            "loaded": loaded,
            "busy": busy,
            "unload_supported": True,
            "provider_session_id": thread_id,
            "error": str(exc),
        }


async def unload_live_session(
    conversation_id: str,
    provider_session_id: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    thread_id = _bound_thread_id(conversation_id, provider_session_id)
    if not thread_id:
        return {
            "ok": False,
            "supported": True,
            "state": "unbound",
            "loaded": False,
            "unload_supported": False,
            "error": "No provider session is bound",
        }
    if _meta_fns and "load" in _meta_fns:
        meta = _object_dict(_meta_fns["load"](conversation_id))
        turn_id = meta.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            return {
                "ok": False,
                "supported": True,
                "state": "busy",
                "loaded": True,
                "busy": True,
                "unload_supported": True,
                "provider_session_id": thread_id,
                "error": "Cannot unload while a turn is active",
            }

    transport = _transport
    if transport is None or not transport.is_ready():
        if transport is not None:
            transport.mark_thread_unready(thread_id)
        return {
            "ok": True,
            "supported": True,
            "state": "cold",
            "loaded": False,
            "unload_supported": True,
            "provider_session_id": thread_id,
        }

    try:
        protocol = await get_runtime_protocol()
        params = build_request_params(protocol, "thread/unsubscribe", {}, thread_id=thread_id)
        result = await transport.rpc_request_unchecked(
            "thread/unsubscribe",
            params=params,
            conversation_id=conversation_id,
            timeout=10.0,
        )
        transport.mark_thread_unready(thread_id)
        status = result.get("status") if _is_object_dict(result) else None
        return {
            "ok": True,
            "supported": True,
            "state": "cold",
            "loaded": False,
            "unload_supported": True,
            "provider_session_id": thread_id,
            "provider_status": status or "unloaded",
        }
    except Exception as exc:
        return {
            "ok": False,
            "supported": True,
            "state": "unknown",
            "loaded": transport.is_thread_ready(thread_id),
            "unload_supported": True,
            "provider_session_id": thread_id,
            "error": str(exc),
        }


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
    active_plan = meta.get("active_plan") if _is_object_dict(meta) and isinstance(meta.get("active_plan"), dict) else None
    if _is_object_dict(active_plan):
        active_steps = normalize_plan_steps(active_plan.get("steps"))
        if active_steps:
            explanation = active_plan.get("explanation")
            return _build_plan_state(
                active_steps,
                explanation=explanation if isinstance(explanation, str) else None,
                source="active_meta",
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
            exclude_turns=True,
        )
        _add_to_raw_buffer("out", conversation_id, f"thread_resumed {thread_id[:8]}")
        return {"ok": True, "provider_session_id": thread_id, "session_id": thread_id}
    except Exception as exc:
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
                    exclude_turns=True,
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

        if not thread_id:
            return {"ok": False, "error": "Failed to resolve thread_id after message send"}
        _add_to_raw_buffer("out", conversation_id, f"turn_start thread={thread_id[:8]} text={text[:120]}")
        return {
            "ok": True,
            "provider_session_id": thread_id,
            "thread_id": thread_id,
            "conversation_id": conversation_id,
        }
    except Exception as exc:
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
    return {
        "ok": True,
        "provider_session_id": session_id,
        "session_id": session_id,
        "conversation_id": conversation_id,
    }


async def fork_conversation(
    extension_id: str,
    source_conversation_id: str,
    conversation_id: str,
    provider_session_id: str,
    cwd: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    metadata: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    del metadata
    if not _meta_fns or "save" not in _meta_fns:
        return {"ok": False, "error": "Manager not initialized"}
    if not provider_session_id:
        return {"ok": False, "error": "provider_session_id is required"}
    if not conversation_id:
        return {"ok": False, "error": "target conversation_id is required"}

    try:
        transport = await _ensure_transport_ready()
        protocol = await get_runtime_protocol()
        settings_map = settings if _is_object_dict(settings) else {}
        model_value = settings_map.get("model")
        merged_settings = _merge_runtime_settings(
            conversation_id,
            settings=settings_map,
            cwd=cwd,
            model=model_value if isinstance(model_value, str) and model_value.strip() else None,
        )
        merged_settings["agent"] = extension_id
        merged_settings["conversation_id"] = conversation_id
        params = build_request_params(
            protocol,
            "thread/fork",
            merged_settings,
            thread_id=provider_session_id,
            exclude_turns=True,
        )
        fork_result = await transport.rpc_request(
            "thread/fork",
            params=params,
            conversation_id=conversation_id,
            timeout=30.0,
        )
        thread_id = _extract_thread_id_from_result(fork_result)
        if not thread_id:
            return {"ok": False, "error": "thread/fork did not return a thread id"}
        transport.mark_thread_ready(thread_id)
        target_meta = _object_dict(_meta_fns["load"](conversation_id)) if _meta_fns and "load" in _meta_fns else {}
        target_meta["conversation_id"] = conversation_id
        target_meta["agent_type"] = extension_id
        target_meta["extension_id"] = extension_id
        target_meta["thread_id"] = thread_id
        target_meta["provider_session_id"] = thread_id
        target_meta["status"] = "active"
        target_meta["settings"] = merged_settings
        target_meta["forked_from_conversation_id"] = source_conversation_id
        target_meta["forked_from_provider_session_id"] = provider_session_id
        _save_meta(conversation_id, target_meta)
        _add_to_raw_buffer(
            "out",
            conversation_id,
            f"thread_fork source={provider_session_id[:8]} target={thread_id[:8]}",
        )
        return {
            "ok": True,
            "accepted": True,
            "conversation_id": conversation_id,
            "source_conversation_id": source_conversation_id,
            "provider_session_id": thread_id,
            "thread_id": thread_id,
            "source_provider_session_id": provider_session_id,
        }
    except Exception as exc:
        _add_to_raw_buffer("err", conversation_id, f"fork_failed {exc}")
        return {"ok": False, "error": f"Thread fork failed: {exc}"}


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
    items_value = preview.get("items") if _is_object_dict(preview) else None
    items: List[Dict[str, object]] = []
    if _is_object_list(items_value):
        for item in items_value:
            if _is_object_dict(item):
                items.append({str(key): value for key, value in item.items()})
    _add_to_raw_buffer("out", conversation_id, f"hydrate_transcript imported={len(items)} session={session_id[:8]}")
    return items


def resolve_approval(request_id: str, resolution: object) -> bool:
    transport = _transport
    if transport is None:
        return False
    return transport.resolve_approval(request_id, resolution)


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: Dict[str, object]) -> bool:
    transport = _transport
    if transport is None:
        return False
    meta: object = _meta_fns["load"](conversation_id) if _meta_fns and "load" in _meta_fns else {}
    if descriptor.get("conversation_id") and descriptor.get("conversation_id") != conversation_id:
        return False
    pending_thread_id = descriptor.get("thread_id")
    current_thread_id = meta.get("thread_id") if _is_object_dict(meta) else None
    if pending_thread_id and current_thread_id and pending_thread_id != current_thread_id:
        return False
    if pending_thread_id and not current_thread_id:
        return False
    runtime_signature = descriptor.get("runtime_signature")
    current_signature = meta.get("thread_runtime_signature") if _is_object_dict(meta) else None
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
    if not _is_object_dict(meta):
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
                exclude_turns=True,
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
