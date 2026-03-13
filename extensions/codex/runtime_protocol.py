import asyncio
import contextlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from te2_runtime import (
    build_codex_thread_config,
    build_effective_developer_instructions,
    te2_mcp_integration_enabled,
)

_server_root: Optional[Path] = None
_extensions_dir: Optional[Path] = None
_runtime_lock: Optional[asyncio.Lock] = None
_runtime_protocol: Optional["RuntimeProtocol"] = None


@dataclass
class RuntimeProtocol:
    version: str
    version_key: str
    cache_dir: Path
    schema_path: Path
    definitions: Dict[str, Any]
    request_params: Dict[str, Dict[str, Any]]
    notifications: Dict[str, Dict[str, Any]]
    events: Dict[str, Dict[str, Any]]
    settings_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def request_schema(self, method: str) -> Optional[Dict[str, Any]]:
        return self.request_params.get(method.lower())

    def notification_schema(self, method: str) -> Optional[Dict[str, Any]]:
        return self.notifications.get(method.lower())

    def has_notification(self, method: str) -> bool:
        return method.lower() in self.notifications

    def has_event_type(self, event_type: str) -> bool:
        return event_type in self.events

    def event_schema(self, event_type: str) -> Optional[Dict[str, Any]]:
        return self.events.get(event_type)


def configure_runtime_protocol(
    server_root: Optional[Path],
    extensions_dir: Optional[Path],
) -> None:
    global _server_root, _extensions_dir, _runtime_protocol
    _server_root = server_root
    _extensions_dir = extensions_dir
    _runtime_protocol = None


def _get_runtime_lock() -> asyncio.Lock:
    global _runtime_lock
    if _runtime_lock is None:
        _runtime_lock = asyncio.Lock()
    return _runtime_lock


def _cache_root() -> Path:
    return Path.home() / ".cache" / "agent_log_server" / "codex_app_server_schema"


def _schema_bundle_path(cache_dir: Path) -> Path:
    return cache_dir / "codex_app_server_protocol.v2.schemas.json"


def _version_key(raw_version: str) -> str:
    match = re.search(r"(\d+\.\d+\.\d+)", raw_version)
    token = match.group(1) if match else raw_version.strip()
    token = re.sub(r"[^A-Za-z0-9._-]+", "_", token).strip("_")
    return token or "unknown"


def _normalize_identifier(value: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", "-", value.strip())
    text = text.replace("_", "-").replace(" ", "-")
    text = re.sub(r"-+", "-", text)
    return text.lower()


def _display_label(value: str) -> str:
    overrides = {
        "on-failure": "On Failure",
        "on-request": "On Request",
        "read-only": "Read Only",
        "workspace-write": "Workspace Write",
        "danger-full-access": "Danger Full Access",
        "xhigh": "Extra High",
    }
    if value in overrides:
        return overrides[value]
    return value.replace("-", " ").replace("_", " ").title()


def _schema_options(values: List[str]) -> List[Dict[str, str]]:
    return [{"value": value, "label": _display_label(value)} for value in values]


def _expand_path(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    return os.path.expanduser(text) if text.startswith("~") else text


async def _run_process(*args: str, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise RuntimeError(f"command timed out: {' '.join(args)}")
    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{' '.join(args)} failed: {message or f'exit {proc.returncode}'}")
    return stdout.decode("utf-8", errors="replace").strip()


def _cleanup_old_cache_dirs(root: Path, keep_name: str) -> None:
    for child in root.iterdir():
        if not child.is_dir() or child.name == keep_name or child.name.startswith(".tmp-"):
            continue
        shutil.rmtree(child, ignore_errors=True)


async def _ensure_schema_bundle() -> tuple[str, str, Path]:
    version_raw = await _run_process("codex", "--version", timeout=15.0)
    version_key = _version_key(version_raw)
    cache_root = _cache_root()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_dir = cache_root / version_key
    schema_path = _schema_bundle_path(cache_dir)
    if schema_path.exists():
        _cleanup_old_cache_dirs(cache_root, keep_name=version_key)
        return version_raw, version_key, schema_path

    temp_dir = cache_root / f".tmp-{version_key}-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        await _run_process(
            "codex",
            "app-server",
            "generate-json-schema",
            "--out",
            str(temp_dir),
            timeout=60.0,
        )
        generated_schema = _schema_bundle_path(temp_dir)
        if not generated_schema.exists():
            raise RuntimeError(
                f"schema bundle missing expected file: {generated_schema}"
            )
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        temp_dir.rename(cache_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    _cleanup_old_cache_dirs(cache_root, keep_name=version_key)
    return version_raw, version_key, schema_path


def _resolve_schema(spec: Any, definitions: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        return {}
    if "$ref" in spec:
        ref = spec["$ref"]
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            return _resolve_schema(definitions.get(ref.rsplit("/", 1)[-1], {}), definitions)
        return {}
    if isinstance(spec.get("allOf"), list):
        merged: Dict[str, Any] = {k: v for k, v in spec.items() if k != "allOf"}
        merged_props: Dict[str, Any] = {}
        merged_required: List[str] = []
        for part in spec["allOf"]:
            resolved = _resolve_schema(part, definitions)
            if isinstance(resolved.get("properties"), dict):
                merged_props.update(resolved["properties"])
            if isinstance(resolved.get("required"), list):
                merged_required.extend(str(item) for item in resolved["required"])
            for key, value in resolved.items():
                if key in {"properties", "required"}:
                    continue
                merged.setdefault(key, value)
        if isinstance(merged.get("properties"), dict):
            merged_props.update(merged["properties"])
        if merged_props:
            merged["properties"] = merged_props
        if merged_required or isinstance(merged.get("required"), list):
            merged["required"] = list(dict.fromkeys(merged_required + list(merged.get("required") or [])))
        return merged
    return spec


def _schema_string_enums(spec: Any, definitions: Dict[str, Any]) -> List[str]:
    values: List[str] = []

    def collect(node: Any) -> None:
        resolved = _resolve_schema(node, definitions)
        if not isinstance(resolved, dict):
            return
        enum_values = resolved.get("enum")
        if isinstance(enum_values, list):
            for item in enum_values:
                if isinstance(item, str) and item not in values:
                    values.append(item)
        for key in ("anyOf", "oneOf", "allOf"):
            variants = resolved.get(key)
            if isinstance(variants, list):
                for variant in variants:
                    collect(variant)

    collect(spec)
    return values


def _schema_tagged_union_variants(spec: Any, definitions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    variants: Dict[str, Dict[str, Any]] = {}

    def collect(node: Any) -> None:
        resolved = _resolve_schema(node, definitions)
        if not isinstance(resolved, dict):
            return
        props = resolved.get("properties")
        if isinstance(props, dict):
            type_prop = _resolve_schema(props.get("type"), definitions)
            enum_values = type_prop.get("enum") if isinstance(type_prop, dict) else None
            if isinstance(enum_values, list) and len(enum_values) == 1 and isinstance(enum_values[0], str):
                variants[enum_values[0]] = resolved
        for key in ("anyOf", "oneOf", "allOf"):
            items = resolved.get(key)
            if isinstance(items, list):
                for item in items:
                    collect(item)

    collect(spec)
    return variants


def _build_request_registry(definitions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    union = definitions.get("ClientRequest", {})
    for variant in union.get("oneOf") or []:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        method_prop = props.get("method")
        params_prop = props.get("params")
        method_values = method_prop.get("enum") if isinstance(method_prop, dict) else None
        if not isinstance(method_values, list) or not method_values:
            continue
        method = method_values[0]
        if isinstance(method, str):
            registry[method.lower()] = _resolve_schema(params_prop, definitions)
    return registry


def _build_notification_registry(definitions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    union = definitions.get("ServerNotification", {})
    for variant in union.get("oneOf") or []:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        method_prop = props.get("method")
        params_prop = props.get("params")
        method_values = method_prop.get("enum") if isinstance(method_prop, dict) else None
        if not isinstance(method_values, list) or not method_values:
            continue
        method = method_values[0]
        if isinstance(method, str):
            registry[method.lower()] = _resolve_schema(params_prop, definitions)
    return registry


def _build_event_registry(definitions: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    union = definitions.get("EventMsg", {})
    for variant in union.get("oneOf") or []:
        if not isinstance(variant, dict):
            continue
        props = variant.get("properties")
        if not isinstance(props, dict):
            continue
        type_prop = props.get("type")
        type_values = type_prop.get("enum") if isinstance(type_prop, dict) else None
        if not isinstance(type_values, list) or not type_values:
            continue
        event_type = type_values[0]
        if isinstance(event_type, str):
            registry[event_type] = variant
    return registry


def _match_allowed_string(value: str, allowed: List[str]) -> Optional[str]:
    normalized_value = _normalize_identifier(value)
    if value == "unlessTrusted":
        normalized_value = _normalize_identifier("untrusted")
    for item in allowed:
        if normalized_value == _normalize_identifier(item):
            return item
    return None


def _coerce_schema_value(
    value: Any,
    schema: Dict[str, Any],
    definitions: Dict[str, Any],
    settings: Dict[str, Any],
) -> Any:
    if value is None or value == "":
        return None

    enum_values = _schema_string_enums(schema, definitions)
    if enum_values:
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            matched = _match_allowed_string(value["type"], enum_values)
            if matched is not None:
                return matched
        if isinstance(value, str):
            matched = _match_allowed_string(value, enum_values)
            if matched is not None:
                return matched

    tagged_variants = _schema_tagged_union_variants(schema, definitions)
    if tagged_variants:
        if isinstance(value, dict) and isinstance(value.get("type"), str):
            matched_tag = None
            for tag in tagged_variants:
                if _normalize_identifier(value["type"]) == _normalize_identifier(tag):
                    matched_tag = tag
                    break
            if matched_tag:
                out = dict(value)
                out["type"] = matched_tag
                if matched_tag == "workspaceWrite" and "writableRoots" not in out:
                    cwd = _expand_path(settings.get("cwd"))
                    if cwd:
                        out["writableRoots"] = [cwd]
                return out
        if isinstance(value, str):
            for tag, variant in tagged_variants.items():
                if _normalize_identifier(value) != _normalize_identifier(tag):
                    continue
                out: Dict[str, Any] = {"type": tag}
                props = variant.get("properties") if isinstance(variant.get("properties"), dict) else {}
                if tag == "workspaceWrite" and "writableRoots" in props:
                    cwd = _expand_path(settings.get("cwd"))
                    if cwd:
                        out["writableRoots"] = [cwd]
                return out

    resolved = _resolve_schema(schema, definitions)
    type_decl = resolved.get("type")
    if isinstance(type_decl, list) and "string" in type_decl and isinstance(value, str):
        text = value.strip()
        return text or None
    if type_decl == "string" and isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    return None


def _first_setting(settings: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = settings.get(key)
        if value is None or value == "":
            continue
        return value
    return None


def _apply_setting_binding(
    params: Dict[str, Any],
    props: Dict[str, Any],
    definitions: Dict[str, Any],
    settings: Dict[str, Any],
    source_keys: List[str],
    target_candidates: List[str],
) -> None:
    value = _first_setting(settings, source_keys)
    if value is None or value == "":
        return
    for target in target_candidates:
        schema = props.get(target)
        if not isinstance(schema, dict):
            continue
        coerced = _coerce_schema_value(value, schema, definitions, settings)
        if coerced is not None:
            params[target] = coerced
        return


def build_request_params(
    protocol: RuntimeProtocol,
    method: str,
    settings: Dict[str, Any],
    *,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    text: Optional[str] = None,
    force_te2_config: bool = False,
) -> Dict[str, Any]:
    schema = protocol.request_schema(method)
    if not isinstance(schema, dict):
        raise RuntimeError(f"runtime protocol missing request schema for {method}")
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    params: Dict[str, Any] = {}

    if "threadId" in props:
        if not thread_id:
            raise RuntimeError(f"{method} requires thread_id")
        params["threadId"] = thread_id
    if "turnId" in props:
        if not turn_id:
            raise RuntimeError(f"{method} requires turn_id")
        params["turnId"] = turn_id
    if "input" in props:
        if text is None:
            raise RuntimeError(f"{method} requires text input")
        params["input"] = [{"type": "text", "text": text}]

    normalized_settings = dict(settings)
    cwd = _expand_path(normalized_settings.get("cwd"))
    if cwd:
        normalized_settings["cwd"] = cwd

    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["cwd"], ["cwd"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["model"], ["model"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["approvalPolicy"], ["approvalPolicy"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["sandboxPolicy", "sandbox"], ["sandboxPolicy", "sandbox"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["reasoning_effort", "effort"], ["reasoningEffort", "effort"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["summary"], ["summary"])
    _apply_setting_binding(params, props, protocol.definitions, normalized_settings, ["developer_instructions"], ["developerInstructions", "baseInstructions"])

    if "config" in props:
        config = build_codex_thread_config(
            normalized_settings.get("config"),
            te2_enabled=te2_mcp_integration_enabled(normalized_settings),
            base_url=normalized_settings.get("te2_base_url"),
            force_te2_mcp_entry=force_te2_config,
        )
        if config:
            params["config"] = config
    if "developerInstructions" in props and "developerInstructions" not in params:
        developer_instructions = build_effective_developer_instructions(
            normalized_settings.get("developer_instructions"),
            te2_enabled=te2_mcp_integration_enabled(normalized_settings),
        )
        if developer_instructions:
            params["developerInstructions"] = developer_instructions

    return params


def build_thread_runtime_signature_payload(
    protocol: RuntimeProtocol,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    return build_request_params(
        protocol,
        "thread/start",
        settings,
        force_te2_config=True,
    )


def build_settings_schema(protocol: RuntimeProtocol, extension_id: str) -> Dict[str, Any]:
    cached = protocol.settings_cache.get(extension_id)
    if cached is not None:
        return cached

    thread_start = protocol.request_schema("thread/start") or {}
    turn_start = protocol.request_schema("turn/start") or {}
    thread_props = thread_start.get("properties") if isinstance(thread_start.get("properties"), dict) else {}
    turn_props = turn_start.get("properties") if isinstance(turn_start.get("properties"), dict) else {}

    approval_values = _schema_string_enums(thread_props.get("approvalPolicy", {}), protocol.definitions)
    sandbox_values = _schema_string_enums(thread_props.get("sandbox", {}), protocol.definitions)
    effort_values = _schema_string_enums(turn_props.get("effort", {}), protocol.definitions)

    schema = {
        "version": "1",
        "description": "Runtime-generated settings schema for the Codex app-server extension",
        "generated_from": str(protocol.schema_path),
        "codex_version": protocol.version,
        "fields": [
            {
                "id": "cwd",
                "type": "path",
                "label": "Working Directory",
                "placeholder": "~/project",
                "default": "~",
                "required": True,
                "browse": True,
            },
            {
                "id": "session",
                "type": "session_picker",
                "label": "Session",
                "placeholder": "(new session)",
                "source": f"/api/extensions/{extension_id}/sessions",
                "resume_endpoint": f"/api/extensions/{extension_id}/sessions/resume",
            },
            {
                "id": "model",
                "type": "select",
                "label": "Model",
                "options": [],
                "dynamic_source": f"/api/extensions/{extension_id}/models",
                "placeholder": "Use server default",
                "default": "",
            },
            {
                "id": "reasoning_effort",
                "type": "select",
                "label": "Reasoning Effort",
                "options": _schema_options(effort_values),
                "placeholder": "Select model first",
                "default": "",
            },
            {
                "id": "approvalPolicy",
                "type": "select",
                "label": "Approval Policy",
                "options": _schema_options(approval_values),
                "placeholder": "Use server default",
                "default": "",
            },
            {
                "id": "sandboxPolicy",
                "type": "select",
                "label": "Sandbox Policy",
                "options": _schema_options(sandbox_values),
                "placeholder": "Use server default",
                "default": "",
            },
            {
                "id": "developer_instructions",
                "type": "textarea",
                "label": "Developer Instructions",
                "placeholder": "Additional runtime instructions appended to the Codex thread configuration",
                "rows": 6,
                "default": "",
            },
        ],
    }
    protocol.settings_cache[extension_id] = schema
    return schema


async def get_runtime_protocol() -> RuntimeProtocol:
    global _runtime_protocol
    if _runtime_protocol is not None:
        return _runtime_protocol

    async with _get_runtime_lock():
        if _runtime_protocol is not None:
            return _runtime_protocol
        version, version_key, schema_path = await _ensure_schema_bundle()
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        definitions = schema.get("definitions")
        if not isinstance(definitions, dict):
            raise RuntimeError(f"runtime schema missing definitions: {schema_path}")
        _runtime_protocol = RuntimeProtocol(
            version=version,
            version_key=version_key,
            cache_dir=schema_path.parent,
            schema_path=schema_path,
            definitions=definitions,
            request_params=_build_request_registry(definitions),
            notifications=_build_notification_registry(definitions),
            events=_build_event_registry(definitions),
        )
        return _runtime_protocol
