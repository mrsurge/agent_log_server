"""
Extension Loader

Discovers and loads pluggable agent extensions by scanning subfolders for
manifest.json files.  Each extension lives in its own directory under
extensions/ with:
    manifest.json   — metadata (id, name, type, version, enabled, capabilities)
    client.py       — handler module (init_*_manager, handle_message, etc.)
    router.py       — event translation (optional)
    settings_schema.json — UI schema (optional)

extensions.json is checked first as an explicit override/ordering file.
If absent, subfolders are scanned alphabetically.
"""

import hashlib
import importlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import zipfile
from collections.abc import Awaitable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Optional, cast

from als_deprecated.typing_helpers import ObjectList, ObjectMap, coerce_object_list, coerce_object_map

HandlerModule = types.ModuleType
ObjectCallable = Callable[..., object]
ExtensionInfoList = list[ObjectMap]
ExtensionRegistry = dict[str, ObjectMap]
HandlerBuckets = dict[str, ExtensionInfoList]
RuntimeDescriptorMap = dict[str, ObjectMap]

# Extension type -> handler module
_extension_handlers: dict[str, HandlerModule] = {}
# extension_id -> registry info (id, name, type, path, manifest)
_extensions_registry: ExtensionRegistry = {}
_extension_source_roots: dict[str, Path] = {}
_extension_module_packages: dict[str, str] = {}
_initialized: bool = False

# Callbacks stored for lazy init of discovered extensions
_init_args: ObjectMap = {}
_DYNAMIC_EXTENSION_NAMESPACE = "_app_server_user_extensions"
_EXTENSION_MANIFEST_SCHEMA_VERSION = 1
_SUPPORTED_EXTENSION_MANIFEST_SCHEMA_VERSIONS = {_EXTENSION_MANIFEST_SCHEMA_VERSION}
_INSTALLER_METADATA_SCHEMA_VERSION = 1


def _dict_or_empty(value: object) -> ObjectMap:
    if not isinstance(value, dict):
        return {}
    value_map = cast(dict[object, object], value)
    return {str(key): item for key, item in value_map.items()}


def _object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return cast(list[object], value)


def _callable_attr(value: object, attr: str) -> ObjectCallable | None:
    candidate = getattr(value, attr, None)
    if callable(candidate):
        return candidate
    return None


async def _invoke_maybe_async(func: ObjectCallable, /, *args: object, **kwargs: object) -> object:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await cast(Awaitable[object], result)
    return result


def _normalized_meta_fns(value: object) -> dict[str, Callable[..., object]] | None:
    if not isinstance(value, dict):
        return None
    value_map = cast(dict[object, object], value)
    meta_fns: dict[str, Callable[..., object]] = {}
    for key, item in value_map.items():
        if isinstance(key, str) and callable(item):
            meta_fns[key] = item
    return meta_fns


def _load_json_file(path: Path) -> object:
    return cast(object, json.loads(path.read_text(encoding="utf-8")))


def _deep_merge_manifest(base: object, override: object) -> object:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = _dict_or_empty(cast(object, base))
        override_map = _dict_or_empty(cast(object, override))
        for key, value in override_map.items():
            key_text = str(key)
            merged[key_text] = _deep_merge_manifest(merged.get(key_text), value)
        return merged
    return override


def _manifest_capability_flag(manifest: object, *names: str) -> bool:
    manifest_map = _dict_or_empty(manifest)
    if not manifest_map:
        return False
    capabilities = _dict_or_empty(manifest_map.get("capabilities"))
    if not capabilities:
        return False
    return any(bool(capabilities.get(name)) for name in names)


def _manifest_ui_quote_parsing_enabled(manifest: object) -> bool:
    manifest_map = _dict_or_empty(manifest)
    if not manifest_map:
        return False
    ui = _dict_or_empty(manifest_map.get("ui"))
    if not ui:
        return False
    semantic_shell: ObjectMap | None = None
    for key in ("semanticShellRibbon", "semantic_shell_ribbon"):
        value = ui.get(key)
        if isinstance(value, dict):
            semantic_shell = _dict_or_empty(cast(object, value))
            break
    if semantic_shell is None:
        return False
    return bool(semantic_shell.get("quoteParsing") or semantic_shell.get("quote_parsing"))


def _normalize_tool_render_spec(spec_raw: object) -> ObjectMap | None:
    if isinstance(spec_raw, str):
        text = spec_raw.strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered in {"plain", "markdown"}:
            return {"kind": lowered}
        if lowered == "hljs":
            return {"kind": "hljs"}
        return {"kind": "hljs", "language": text}
    if not isinstance(spec_raw, dict):
        return None
    spec_map = _dict_or_empty(cast(object, spec_raw))
    kind_raw = spec_map.get("kind")
    kind = kind_raw.strip().lower() if isinstance(kind_raw, str) and kind_raw.strip() else ""
    language_raw = spec_map.get("language")
    language = language_raw.strip() if isinstance(language_raw, str) and language_raw.strip() else ""
    if kind not in {"plain", "markdown", "hljs"}:
        if language:
            return {"kind": "hljs", "language": language}
        return None
    spec: ObjectMap = {"kind": kind}
    if kind == "hljs" and language:
        spec["language"] = language
    return spec


def _normalize_tool_render_field_map(fields_raw: object) -> dict[str, ObjectMap]:
    normalized: dict[str, ObjectMap] = {}
    if not isinstance(fields_raw, dict):
        return normalized
    fields = _dict_or_empty(cast(object, fields_raw))
    for key, value in fields.items():
        if not key.strip():
            continue
        spec = _normalize_tool_render_spec(value)
        if spec is None:
            continue
        normalized[key.strip()] = spec
    return normalized


def _normalize_tool_render_rule(rule_raw: object) -> ObjectMap | None:
    if not isinstance(rule_raw, dict):
        return None
    rule_map = _dict_or_empty(cast(object, rule_raw))
    rule: ObjectMap = {}
    for field in ("server", "tool", "serverPrefix", "toolPrefix"):
        value = rule_map.get(field)
        if isinstance(value, str) and value.strip():
            rule[field] = value.strip()
    for field in ("servers", "tools"):
        value = rule_map.get(field)
        if isinstance(value, list):
            items = [item.strip() for item in _object_list(cast(object, value)) if isinstance(item, str) and item.strip()]
            if items:
                rule[field] = items
    for source_key, target_key in (
        ("request", "request"),
        ("args", "request"),
        ("arguments", "request"),
        ("response", "response"),
        ("result", "response"),
    ):
        if target_key in rule:
            continue
        spec = _normalize_tool_render_spec(rule_map.get(source_key))
        if spec is not None:
            rule[target_key] = spec
    for source_key, target_key in (
        ("requestFields", "requestFields"),
        ("argsFields", "requestFields"),
        ("argumentsFields", "requestFields"),
        ("responseFields", "responseFields"),
        ("resultFields", "responseFields"),
    ):
        if target_key in rule:
            continue
        fields = _normalize_tool_render_field_map(rule_map.get(source_key))
        if fields:
            rule[target_key] = fields
    if not rule:
        return None
    return rule


def _manifest_tool_render_policy(manifest: object) -> ObjectMap:
    default_policy: ObjectMap = {
        "default": {
            "request": {"kind": "plain"},
            "response": {"kind": "plain"},
        },
        "rules": [],
    }
    manifest_map = _dict_or_empty(manifest)
    if not manifest_map:
        return default_policy
    ui = _dict_or_empty(manifest_map.get("ui"))
    if not ui:
        return default_policy
    policy_raw = ui.get("toolRenderPolicy")
    if not isinstance(policy_raw, dict):
        return default_policy
    policy = _dict_or_empty(cast(object, policy_raw))
    default_raw = policy.get("default")
    if isinstance(default_raw, dict):
        default_map = _dict_or_empty(cast(object, default_raw))
        normalized_default: ObjectMap = {}
        request_spec = _normalize_tool_render_spec(
            default_map.get("request") or default_map.get("args") or default_map.get("arguments")
        )
        if request_spec is not None:
            normalized_default["request"] = request_spec
        response_spec = _normalize_tool_render_spec(default_map.get("response") or default_map.get("result"))
        if response_spec is not None:
            normalized_default["response"] = response_spec
        request_fields = _normalize_tool_render_field_map(
            default_map.get("requestFields") or default_map.get("argsFields") or default_map.get("argumentsFields")
        )
        if request_fields:
            normalized_default["requestFields"] = request_fields
        response_fields = _normalize_tool_render_field_map(
            default_map.get("responseFields") or default_map.get("resultFields")
        )
        if response_fields:
            normalized_default["responseFields"] = response_fields
        if normalized_default:
            existing_default = _dict_or_empty(default_policy.get("default"))
            default_policy["default"] = {
                **existing_default,
                **normalized_default,
            }
    rules_raw = policy.get("rules")
    if isinstance(rules_raw, list):
        default_policy["rules"] = [
            rule
            for rule in (_normalize_tool_render_rule(rule_raw) for rule_raw in _object_list(cast(object, rules_raw)))
            if rule is not None
        ]
    return default_policy


def _normalize_runtime_options_list(options_raw: object) -> ObjectList:
    options: ObjectList = []
    if not isinstance(options_raw, list):
        return options
    for item in _object_list(cast(object, options_raw)):
        if isinstance(item, str):
            text = item.strip()
            if text:
                options.append({"value": text, "label": text})
            continue
        if not isinstance(item, dict):
            continue
        item_map = _dict_or_empty(cast(object, item))
        value = item_map.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        label = item_map.get("label")
        if not isinstance(label, str) or not label.strip():
            label = value
        option: ObjectMap = {
            "value": value.strip(),
            "label": label.strip(),
        }
        if item_map.get("deprecated") is True:
            option["deprecated"] = True
        options.append(option)
    return options


def _schema_runtime_option_meta(field: ObjectMap) -> ObjectMap:
    meta: ObjectMap = {}
    semantic = coerce_object_map(field.get("semantic"))
    if semantic:
        role = semantic.get("role")
        if isinstance(role, str) and role.strip():
            meta["role"] = role.strip()
            if role.strip() == "approval_policy":
                meta.setdefault("kind", "approval")
                meta.setdefault("footer", True)
            elif role.strip() == "mode":
                meta.setdefault("kind", "mode")
                meta.setdefault("footer", True)
        runtime_key = semantic.get("runtime_key") or semantic.get("runtimeKey")
        if isinstance(runtime_key, str) and runtime_key.strip():
            meta["kind"] = runtime_key.strip()
        footer_label = semantic.get("label") or semantic.get("footer_label") or semantic.get("footerLabel")
        if isinstance(footer_label, str) and footer_label.strip():
            meta["footerLabel"] = footer_label.strip()
        accents_raw = coerce_object_map(semantic.get("accents"))
        if accents_raw:
            semantic_accents: dict[str, str] = {}
            for key, value in accents_raw.items():
                if not isinstance(value, str) or not value.strip():
                    continue
                semantic_accents[key.strip()] = value.strip()
            if semantic_accents:
                meta["accents"] = semantic_accents
    runtime_option = field.get("runtime_option")
    if isinstance(runtime_option, str):
        text = runtime_option.strip()
        if text:
            meta["kind"] = text
    else:
        runtime_option_map = coerce_object_map(runtime_option)
        kind = runtime_option_map.get("kind")
        if isinstance(kind, str) and kind.strip():
            meta["kind"] = kind.strip()
        if runtime_option_map.get("footer") is True:
            meta["footer"] = True
        footer_label = runtime_option_map.get("footer_label")
        if isinstance(footer_label, str) and footer_label.strip():
            meta["footerLabel"] = footer_label.strip()
        accents_raw = coerce_object_map(runtime_option_map.get("accents"))
        if accents_raw:
            accents: dict[str, str] = {}
            for key, value in accents_raw.items():
                if not isinstance(value, str) or not value.strip():
                    continue
                accents[key.strip()] = value.strip()
            if accents:
                meta["accents"] = accents
    dynamic_options_key = field.get("dynamic_options_key")
    if isinstance(dynamic_options_key, str) and dynamic_options_key.strip():
        meta["dynamicOptionsKey"] = dynamic_options_key.strip()
    return meta


def _runtime_option_runtime_key(field: ObjectMap) -> Optional[str]:
    meta = _schema_runtime_option_meta(field)
    kind = meta.get("kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    dynamic_key = meta.get("dynamicOptionsKey")
    if isinstance(dynamic_key, str) and dynamic_key.strip():
        return dynamic_key.strip()
    field_id = field.get("id")
    if isinstance(field_id, str) and field_id.strip():
        return field_id.strip()
    return None


def _normalize_extension_roots(extension_roots: object) -> list[Path]:
    roots: list[Path] = []
    if isinstance(extension_roots, list):
        raw_roots = list(cast(list[object], extension_roots))
    elif isinstance(extension_roots, tuple):
        raw_roots = list(cast(tuple[object, ...], extension_roots))
    elif isinstance(extension_roots, set):
        raw_roots = list(cast(set[object], extension_roots))
    else:
        raw_roots = [extension_roots]
    for raw_root in raw_roots:
        if isinstance(raw_root, Path):
            root = raw_root.expanduser()
        elif isinstance(raw_root, str) and raw_root.strip():
            root = Path(raw_root.strip()).expanduser()
        else:
            continue
        try:
            resolved = root.resolve()
        except Exception:
            resolved = root
        if not resolved.exists() or not resolved.is_dir():
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _sanitize_module_token(value: str) -> str:
    token = "".join(char if char.isalnum() else "_" for char in str(value or ""))
    token = token.strip("_")
    if not token:
        token = "extension"
    if token[0].isdigit():
        token = f"ext_{token}"
    return token


def _module_package_for_root(folder: str, source_root: Path, builtin_root: Optional[Path]) -> str:
    folder_parts = [part for part in PurePosixPath(str(folder)).parts if part not in {"", "."}]
    if builtin_root is not None and source_root == builtin_root:
        tokens = [_sanitize_module_token(part) for part in folder_parts] or ["extension"]
        return "extensions." + ".".join(tokens)
    folder_token = _sanitize_module_token("_".join(folder_parts) if folder_parts else "extension")
    digest = hashlib.sha1(f"{source_root}:{folder}".encode("utf-8")).hexdigest()[:12]
    return f"{_DYNAMIC_EXTENSION_NAMESPACE}.{folder_token}_{digest}"


def _ensure_dynamic_extension_package(module_package: str, extension_root: Path) -> None:
    parent_package, _, _ = module_package.rpartition(".")
    if parent_package and parent_package not in sys.modules:
        parent = types.ModuleType(parent_package)
        parent.__package__ = parent_package
        parent.__path__ = []
        sys.modules[parent_package] = parent
    package = sys.modules.get(module_package)
    package_path = str(extension_root.resolve())
    if package is None:
        package = types.ModuleType(module_package)
        package.__package__ = module_package
        package.__path__ = [package_path]
        sys.modules[module_package] = package
        return
    existing_paths = list(getattr(package, "__path__", []))
    if package_path not in existing_paths:
        existing_paths.append(package_path)
        package.__path__ = existing_paths


def _import_extension_submodule(module_package: str, extension_root: Path, submodule: str) -> HandlerModule:
    if not module_package.startswith(f"{_DYNAMIC_EXTENSION_NAMESPACE}."):
        return importlib.import_module(f"{module_package}.{submodule}")
    _ensure_dynamic_extension_package(module_package, extension_root)
    importlib.invalidate_caches()
    return importlib.import_module(f"{module_package}.{submodule}")


def _abs_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_is_within(candidate: Path, base: Path) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(os.fspath(candidate)), os.path.abspath(os.fspath(base))]) == os.path.abspath(os.fspath(base))
    except Exception:
        return False


def _sanitize_install_folder(value: str) -> str:
    safe: list[str] = []
    for char in str(value or ""):
        if char.isalnum() or char in {"-", "_", "."}:
            safe.append(char)
        else:
            safe.append("_")
    folder = "".join(safe).strip("._-")
    return folder or "extension"


def _coerce_schema_version(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _manifest_version_text(manifest: object) -> str:
    manifest_map = _dict_or_empty(manifest)
    if not manifest_map:
        return ""
    raw_value = manifest_map.get("version")
    return raw_value.strip() if isinstance(raw_value, str) and raw_value.strip() else ""


def _missing_manifest_version_message(extension_id: str) -> str:
    ext_label = str(extension_id or "").strip() or "unknown extension"
    return (
        f"Extension manifest.version is required for {ext_label}; "
        "add a version string such as 0.1.0"
    )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _extension_roots_from_state() -> list[Path]:
    raw_roots = _init_args.get("extension_roots")
    return [root for root in _object_list(cast(object, raw_roots)) if isinstance(root, Path)] if isinstance(raw_roots, list) else []


def _user_extension_root() -> Optional[Path]:
    roots = _extension_roots_from_state()
    if len(roots) >= 2:
        return roots[1]
    return None


def _read_extensions_registry(root: Path) -> ObjectMap:
    registry_path = root / "extensions.json"
    if not registry_path.exists():
        return {"version": "1.0", "extensions": []}
    data = _load_json_file(registry_path)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid extensions.json at {registry_path}")
    registry = _dict_or_empty(cast(object, data))
    entries = registry.get("extensions")
    if not isinstance(entries, list):
        registry["extensions"] = []
    version = registry.get("version")
    if not isinstance(version, str) or not version.strip():
        registry["version"] = "1.0"
    return registry


def _write_extensions_registry(root: Path, registry: ObjectMap) -> None:
    root.mkdir(parents=True, exist_ok=True)
    registry_path = root / "extensions.json"
    tmp_path = registry_path.with_name(f"{registry_path.name}.tmp")
    payload = json.dumps(registry, indent=2, ensure_ascii=False) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(registry_path)


def _registry_install_source(entry: object) -> ObjectMap:
    entry_map = _dict_or_empty(entry)
    if not entry_map:
        return {}
    install_source = _dict_or_empty(entry_map.get("install_source"))
    if install_source:
        return install_source
    installer_meta = _dict_or_empty(entry_map.get("installer_meta"))
    current = _dict_or_empty(installer_meta.get("current"))
    return _dict_or_empty(current.get("install_source"))


def _upsert_registry_entry(root: Path, entry: ObjectMap) -> None:
    registry = _read_extensions_registry(root)
    entries_raw = registry.get("extensions")
    if isinstance(entries_raw, list):
        entries = _object_list(cast(object, entries_raw))
    else:
        entries = []
        registry["extensions"] = entries
    ext_id = entry.get("id")
    if not isinstance(ext_id, str) or not ext_id:
        raise ValueError("Registry entry requires id")
    for index, existing in enumerate(entries):
        if not isinstance(existing, dict):
            continue
        existing_map = _dict_or_empty(cast(object, existing))
        if existing_map.get("id") == ext_id:
            existing_map = _dict_or_empty(cast(object, existing))
            merged = dict(existing_map)
            merged.update(entry)
            entries[index] = merged
            _write_extensions_registry(root, registry)
            return
    entries.append(dict(entry))
    _write_extensions_registry(root, registry)


def _remove_registry_entry(root: Path, extension_id: str) -> bool:
    registry = _read_extensions_registry(root)
    entries_raw = registry.get("extensions")
    if not isinstance(entries_raw, list):
        return False
    entries = _object_list(cast(object, entries_raw))
    filtered: list[object] = [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and _dict_or_empty(cast(object, entry)).get("id") == extension_id
        )
    ]
    if len(filtered) == len(entries):
        return False
    registry["extensions"] = filtered
    _write_extensions_registry(root, registry)
    return True


def _clear_extension_module_cache(module_packages: list[str]) -> None:
    package_names = sorted(
        {
            package
            for package in module_packages
            if package
        },
        key=len,
        reverse=True,
    )
    for package in package_names:
        for name in list(sys.modules.keys()):
            if name == package or name.startswith(f"{package}."):
                sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _extension_runtime_signature(info: ObjectMap) -> str:
    payload = {
        "id": info.get("id"),
        "name": info.get("name"),
        "type": info.get("type"),
        "path": info.get("path"),
        "manifest": info.get("manifest"),
        "default_enabled": info.get("default_enabled"),
        "source_kind": info.get("source_kind"),
        "install_source": info.get("install_source"),
        "version": info.get("version"),
        "schema_version": info.get("schema_version"),
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _build_extension_state(
    discovered: ExtensionInfoList,
    builtin_root: Optional[Path],
) -> tuple[ExtensionRegistry, dict[str, Path], dict[str, str], HandlerBuckets]:
    registry: ExtensionRegistry = {}
    source_roots: dict[str, Path] = {}
    module_packages: dict[str, str] = {}
    enabled_by_type: HandlerBuckets = {}

    for ext_info in discovered:
        ext_id = ext_info.get("id")
        ext_type = ext_info.get("type")
        folder = ext_info.get("folder")
        if not isinstance(ext_id, str) or not ext_id:
            continue
        if not isinstance(ext_type, str) or not ext_type:
            continue
        if not isinstance(folder, str) or not folder:
            continue
        manifest = _dict_or_empty(ext_info.get("manifest"))
        dependencies = _dict_or_empty(manifest.get("dependencies"))
        default_enabled = bool(ext_info.get("enabled", True))
        source_root = ext_info.get("source_root")
        module_package = ext_info.get("module_package")
        registry_entry = _dict_or_empty(ext_info.get("registry_entry"))
        install_source = _registry_install_source(registry_entry)
        installer_meta = _dict_or_empty(registry_entry.get("installer_meta"))
        current_meta = _dict_or_empty(installer_meta.get("current"))
        version_text = _manifest_version_text(manifest)
        manifest_ok = bool(version_text)
        manifest_message = "" if manifest_ok else _missing_manifest_version_message(ext_id)
        schema_version = manifest.get("schema_version")
        if schema_version is None:
            schema_version = registry_entry.get("schema_version")
        if schema_version is None:
            schema_version = current_meta.get("schema_version")

        if isinstance(source_root, Path):
            source_roots[ext_id] = source_root
        if isinstance(module_package, str) and module_package:
            module_packages[ext_id] = module_package

        source_kind = "builtin"
        if isinstance(source_root, Path) and builtin_root is not None and source_root != builtin_root:
            source_kind = "user"

        registry[ext_id] = {
            "id": ext_id,
            "name": ext_info.get("name", ext_id),
            "type": ext_type,
            "path": folder,
            "manifest": manifest,
            "capabilities": _dict_or_empty(manifest.get("capabilities")),
            "ui": _dict_or_empty(manifest.get("ui")),
            "has_plan": _manifest_capability_flag(manifest, "hasPlan", "has_plan"),
            "has_todo": _manifest_capability_flag(manifest, "hasTodo", "has_todo"),
            "has_plan_modes": _manifest_capability_flag(manifest, "hasPlanModes", "has_plan_modes"),
            "default_enabled": default_enabled,
            "enabled": default_enabled,
            "dependency_status": "unchecked" if manifest_ok else "error",
            "dependency_ok": manifest_ok,
            "dependency_message": "" if manifest_ok else manifest_message,
            "dependency_details": {},
            "has_dependency_check": bool(dependencies.get("has_check")),
            "has_dependency_install": bool(dependencies.get("has_install")),
            "active": default_enabled and manifest_ok,
            "source_kind": source_kind,
            "source_root": str(source_root) if isinstance(source_root, Path) else "",
            "install_source": dict(install_source) if install_source else {},
            "installer_meta": dict(installer_meta) if installer_meta else {},
            "version": version_text,
            "schema_version": schema_version,
            "manifest_ok": manifest_ok,
            "manifest_message": manifest_message,
        }

        if default_enabled and manifest_ok:
            enabled_by_type.setdefault(ext_type, []).append(ext_info)

    return registry, source_roots, module_packages, enabled_by_type


def load_extensions(
    extensions_dir: object,
    server_root: Path,
    fws_getter: Callable[..., object],
    broadcast_fn: Callable[..., object],
    transcript_fn: Callable[..., object],
    meta_fns: Optional[dict[str, Callable[..., object]]] = None,
) -> None:
    """
    Discover and load extensions.

    1. If extensions.json exists, load entries from it (explicit ordering).
    2. Otherwise scan extensions/*/manifest.json (alphabetical).
    3. For each enabled extension, import extensions.<folder>.client and
       call its init function with the standard callback set.
    """
    global _extension_handlers, _extensions_registry, _extension_source_roots
    global _extension_module_packages, _initialized, _init_args

    extension_roots = _normalize_extension_roots(extensions_dir)
    primary_root = extension_roots[0] if extension_roots else None

    _init_args = {
        "extensions_dir": primary_root,
        "extension_roots": extension_roots,
        "server_root": server_root,
        "fws_getter": fws_getter,
        "broadcast_fn": broadcast_fn,
        "transcript_fn": transcript_fn,
        "meta_fns": meta_fns,
    }

    discovered = _discover_extensions(extension_roots)

    _extension_handlers = {}
    (
        _extensions_registry,
        _extension_source_roots,
        _extension_module_packages,
        enabled_by_type,
    ) = _build_extension_state(discovered, primary_root)

    for ext_info in discovered:
        if not ext_info.get("enabled", True):
            continue
        ext_type = ext_info.get("type")
        if not isinstance(ext_type, str) or not ext_type:
            continue

        if ext_type not in _extension_handlers:
            handler = _load_handler(
                ext_info,
                handler_extensions=enabled_by_type.get(ext_type, []),
                server_root=server_root,
                fws_getter=fws_getter,
                broadcast_fn=broadcast_fn,
                transcript_fn=transcript_fn,
                meta_fns=meta_fns,
            )
            if handler:
                _extension_handlers[ext_type] = handler

    _initialized = True
    print(f"[Extensions] Loaded {len(_extensions_registry)} extension(s): "
          f"{list(_extensions_registry.keys())}")


def _discover_extensions_in_root(extensions_dir: Path, builtin_root: Optional[Path]) -> ExtensionInfoList:
    """Return list of extension info dicts from manifests."""
    result: ExtensionInfoList = []
    if not extensions_dir.exists() or not extensions_dir.is_dir():
        return result

    # Strategy 1: explicit extensions.json
    extensions_json = extensions_dir / "extensions.json"
    if extensions_json.exists():
        try:
            data_raw = _load_json_file(extensions_json)
            data = _dict_or_empty(data_raw)
            entries = data.get("extensions")
            for raw_entry in _object_list(cast(object, entries)) if isinstance(entries, list) else []:
                entry = _dict_or_empty(raw_entry)
                folder_raw = entry.get("path")
                if not isinstance(folder_raw, str) or not folder_raw.strip():
                    fallback_id = entry.get("id")
                    folder_raw = fallback_id if isinstance(fallback_id, str) else ""
                folder = folder_raw.strip()
                manifest_path = extensions_dir / folder / "manifest.json"
                manifest: ObjectMap = {}
                if manifest_path.exists():
                    try:
                        manifest_data = _load_json_file(manifest_path)
                        manifest = _dict_or_empty(manifest_data)
                    except Exception:
                        pass
                overrides = entry.get("manifest_overrides")
                if isinstance(overrides, dict):
                    manifest = _dict_or_empty(_deep_merge_manifest(manifest, cast(object, overrides)))
                result.append({
                    "id": entry.get("id") or manifest.get("id", folder),
                    "name": entry.get("name") or manifest.get("name", folder),
                    "type": entry.get("type") or manifest.get("type", folder),
                    "enabled": entry.get("enabled", manifest.get("enabled", True)),
                    "folder": folder,
                    "manifest": manifest,
                    "source_root": extensions_dir,
                    "module_package": _module_package_for_root(folder, extensions_dir, builtin_root),
                    "registry_entry": dict(entry),
                })
            return result
        except Exception as e:
            print(f"[Extensions] Failed to read extensions.json: {e}")

    # Strategy 2: scan subfolders for manifest.json
    for sub in sorted(extensions_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        manifest_path = sub / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            discovered_manifest = _load_json_file(manifest_path)
            manifest = _dict_or_empty(discovered_manifest)
            result.append({
                "id": manifest.get("id", sub.name),
                "name": manifest.get("name", sub.name),
                "type": manifest.get("type", sub.name),
                "enabled": manifest.get("enabled", True),
                "folder": sub.name,
                "manifest": manifest,
                "source_root": extensions_dir,
                "module_package": _module_package_for_root(sub.name, extensions_dir, builtin_root),
                "registry_entry": {},
            })
        except Exception as e:
            print(f"[Extensions] Bad manifest in {sub.name}/: {e}")

    return result


def _discover_extensions(extension_roots: list[Path]) -> ExtensionInfoList:
    merged: ExtensionRegistry = {}
    builtin_root = extension_roots[0] if extension_roots else None
    for root in extension_roots:
        for ext_info in _discover_extensions_in_root(root, builtin_root):
            ext_id = ext_info.get("id")
            if not isinstance(ext_id, str) or not ext_id:
                continue
            if ext_id in merged:
                print(f"[Extensions] Duplicate extension id {ext_id} from {root}; overriding earlier root")
                merged.pop(ext_id, None)
            merged[ext_id] = ext_info
    return list(merged.values())


def reload_extensions(
    changed_extension_ids: Optional[list[str]] = None,
    *,
    force: bool = False,
) -> ExtensionInfoList:
    global _extension_handlers, _extensions_registry, _extension_source_roots
    global _extension_module_packages

    if not _initialized or not _init_args:
        raise RuntimeError("Extensions have not been initialized")

    extension_roots = _normalize_extension_roots(_init_args.get("extension_roots") or _init_args.get("extensions_dir"))
    primary_root = extension_roots[0] if extension_roots else None
    discovered = _discover_extensions(extension_roots)
    (
        new_registry,
        new_source_roots,
        new_module_packages,
        enabled_by_type,
    ) = _build_extension_state(discovered, primary_root)

    old_registry = dict(_extensions_registry)
    old_handlers = dict(_extension_handlers)
    old_module_packages = dict(_extension_module_packages)

    changed_ids: set[str] = {
        ext_id.strip()
        for ext_id in (changed_extension_ids or [])
        if ext_id.strip()
    }
    changed_ids.update(set(old_registry.keys()) ^ set(new_registry.keys()))
    for ext_id in set(old_registry.keys()) & set(new_registry.keys()):
        if _extension_runtime_signature(old_registry[ext_id]) != _extension_runtime_signature(new_registry[ext_id]):
            changed_ids.add(ext_id)

    changed_types: set[str] = set()
    if force:
        changed_types.update(
            ext_type
            for ext_type in (
                info.get("type")
                for info in [*old_registry.values(), *new_registry.values()]
            )
            if isinstance(ext_type, str) and ext_type
        )
    else:
        for ext_id in changed_ids:
            for info in (old_registry.get(ext_id), new_registry.get(ext_id)):
                ext_type = info.get("type") if info is not None else None
                if isinstance(ext_type, str) and ext_type:
                    changed_types.add(ext_type)

    module_packages_to_clear: set[str] = set()
    for ext_id, module_package in old_module_packages.items():
        old_info = old_registry.get(ext_id)
        ext_type = old_info.get("type") if old_info is not None else None
        if force or (isinstance(ext_type, str) and ext_type in changed_types):
            module_packages_to_clear.add(module_package)
    for ext_id, module_package in new_module_packages.items():
        new_info = new_registry.get(ext_id)
        ext_type = new_info.get("type") if new_info is not None else None
        if force or (isinstance(ext_type, str) and ext_type in changed_types):
            module_packages_to_clear.add(module_package)
    _clear_extension_module_cache(list(module_packages_to_clear))

    preserved_handlers: dict[str, HandlerModule] = {}
    new_types = {
        ext_type
        for ext_type in (
            info.get("type")
            for info in new_registry.values()
        )
        if isinstance(ext_type, str) and ext_type
    }
    for ext_type, handler in old_handlers.items():
        if ext_type in new_types and ext_type not in changed_types:
            preserved_handlers[ext_type] = handler

    _extensions_registry = new_registry
    _extension_source_roots = new_source_roots
    _extension_module_packages = new_module_packages
    _extension_handlers = preserved_handlers

    for ext_info in discovered:
        if not ext_info.get("enabled", True):
            continue
        ext_type = ext_info.get("type")
        if not isinstance(ext_type, str) or not ext_type:
            continue
        if ext_type in _extension_handlers:
            continue
        server_root = _init_args.get("server_root")
        fws_getter = _init_args.get("fws_getter")
        broadcast_fn = _init_args.get("broadcast_fn")
        transcript_fn = _init_args.get("transcript_fn")
        meta_fns = _normalized_meta_fns(_init_args.get("meta_fns"))
        if not isinstance(server_root, Path):
            continue
        if not callable(fws_getter) or not callable(broadcast_fn) or not callable(transcript_fn):
            continue
        handler = _load_handler(
            ext_info,
            handler_extensions=enabled_by_type.get(ext_type, []),
            server_root=server_root,
            fws_getter=fws_getter,
            broadcast_fn=broadcast_fn,
            transcript_fn=transcript_fn,
            meta_fns=meta_fns,
        )
        if handler:
            _extension_handlers[ext_type] = handler

    print(
        f"[Extensions] Reloaded {len(_extensions_registry)} extension(s): "
        f"{list(_extensions_registry.keys())}"
    )
    return list_extensions()


def _load_handler(
    ext_info: ObjectMap,
    server_root: Path,
    fws_getter: Callable[..., object],
    broadcast_fn: Callable[..., object],
    transcript_fn: Callable[..., object],
    meta_fns: Optional[dict[str, Callable[..., object]]],
    handler_extensions: Optional[ExtensionInfoList] = None,
) -> Optional[HandlerModule]:
    """Dynamically import the extension client module and call its init function."""
    folder = ext_info.get("folder")
    ext_type = ext_info.get("type")
    source_root = ext_info.get("source_root")
    module_package = ext_info.get("module_package")
    if not isinstance(folder, str) or not folder:
        return None
    if not isinstance(ext_type, str) or not ext_type:
        return None
    if not isinstance(source_root, Path):
        return None
    if not isinstance(module_package, str) or not module_package:
        return None
    extension_root = (source_root / folder).resolve()
    module_path = f"{module_package}.client"
    try:
        mod = _import_extension_submodule(module_package, extension_root, "client")
    except Exception as e:
        print(f"[Extensions] Failed to import {module_path}: {e}")
        import traceback
        traceback.print_exc()
        return None

    # Convention: init function is init_<type>_manager(...)
    init_fn_name = f"init_{ext_type}_manager"
    init_fn = _callable_attr(mod, init_fn_name)
    if init_fn is None:
        # Fallback: try init_<folder>_manager (folder may differ from type)
        init_fn = _callable_attr(mod, f"init_{folder}_manager")
    if init_fn is None:
        # Fallback: scan for any init_*_manager function
        for attr in dir(mod):
            candidate = _callable_attr(mod, attr)
            if attr.startswith("init_") and attr.endswith("_manager") and candidate is not None:
                init_fn = candidate
                break
    if init_fn is None:
        # Last resort: try generic init_manager
        init_fn = _callable_attr(mod, "init_manager")
    if init_fn is None:
        print(f"[Extensions] {module_path} has no {init_fn_name}() or init_manager()")
        return mod  # still return module — may work without init

    init_kwargs: ObjectMap = {}
    try:
        init_signature = inspect.signature(init_fn)
    except (TypeError, ValueError):
        init_signature = None
    if init_signature and "registered_extension_ids" in init_signature.parameters:
        init_kwargs["registered_extension_ids"] = [
            ext_id
            for ext_id in (
                entry.get("id")
                for entry in (handler_extensions or [])
            )
            if isinstance(ext_id, str) and ext_id
        ]

    try:
        init_fn(
            source_root,
            server_root,
            fws_getter,
            broadcast_fn,
            transcript_fn,
            meta_fns,
            **init_kwargs,
        )
    except Exception as e:
        print(f"[Extensions] {init_fn_name}() failed: {e}")
        import traceback
        traceback.print_exc()
        return None

    return mod


def get_handler(extension_id: str) -> HandlerModule | None:
    """Get the handler module for an extension by its ID."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return None
    ext_type = ext_info.get("type")
    if not isinstance(ext_type, str) or not ext_type:
        return None
    return _extension_handlers.get(ext_type)


def has_extension(extension_id: str) -> bool:
    """Check if an extension is registered."""
    info = _extensions_registry.get(extension_id)
    return bool(info and info.get("active"))


def list_extensions() -> ExtensionInfoList:
    """List all registered extensions."""
    return [dict(info) for info in _extensions_registry.values()]


def get_extension_info(extension_id: str) -> ObjectMap | None:
    """Return registry metadata for one extension."""
    info = _extensions_registry.get(extension_id)
    if info is None:
        return None
    return dict(info)


def get_extension_ui_features(extension_id: str) -> ObjectMap:
    """Return manifest-driven frontend behavior flags for one extension."""
    info = _extensions_registry.get(extension_id)
    manifest_raw = info.get("manifest") if info is not None else None
    manifest = _dict_or_empty(cast(object, manifest_raw)) if isinstance(manifest_raw, dict) else {}
    return {
        "semanticShellRibbon": {
            "quoteParsing": _manifest_ui_quote_parsing_enabled(manifest),
        },
        "toolRenderPolicy": _manifest_tool_render_policy(manifest),
    }


def _recompute_extension_active_state(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if info is None:
        return False
    info["active"] = (
        bool(info.get("enabled"))
        and bool(info.get("manifest_ok", True))
        and bool(info.get("dependency_ok"))
    )
    return bool(info.get("active"))


def _active_extensions_for_type(ext_type: str) -> ExtensionInfoList:
    return [
        info
        for info in _extensions_registry.values()
        if info.get("type") == ext_type and info.get("active")
    ]


def _ensure_handler_loaded_for_extension(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if info is None:
        return False
    ext_type = info.get("type")
    folder = info.get("path")
    source_root = _extension_source_roots.get(extension_id)
    module_package = _extension_module_packages.get(extension_id)
    if not isinstance(ext_type, str) or not ext_type or not isinstance(folder, str) or not folder:
        return False
    if not isinstance(source_root, Path):
        return False
    if not isinstance(module_package, str) or not module_package:
        return False
    if ext_type in _extension_handlers:
        return True
    server_root = _init_args.get("server_root")
    fws_getter = _init_args.get("fws_getter")
    broadcast_fn = _init_args.get("broadcast_fn")
    transcript_fn = _init_args.get("transcript_fn")
    meta_fns = _normalized_meta_fns(_init_args.get("meta_fns"))
    if not isinstance(server_root, Path):
        return False
    if not callable(fws_getter) or not callable(broadcast_fn) or not callable(transcript_fn):
        return False
    active_entries = _active_extensions_for_type(ext_type)
    handler = _load_handler(
        {
            "folder": folder,
            "type": ext_type,
            "source_root": source_root,
            "module_package": module_package,
        },
        server_root=server_root,
        fws_getter=fws_getter,
        broadcast_fn=broadcast_fn,
        transcript_fn=transcript_fn,
        meta_fns=meta_fns,
        handler_extensions=active_entries,
    )
    if handler:
        _extension_handlers[ext_type] = handler
        return True
    return False


def set_extension_enabled(extension_id: str, enabled: bool) -> bool:
    info = _extensions_registry.get(extension_id)
    if info is None:
        return False
    info["enabled"] = bool(enabled)
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def set_extension_dependency_result(extension_id: str, result: ObjectMap | None) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    payload = result if result is not None else {}
    status = str(payload.get("status") or ("met" if payload.get("ok") else "error")).strip().lower()
    if status not in {"met", "unmet", "error"}:
        status = "met" if payload.get("ok") else "error"
    message = payload.get("message")
    details_raw = payload.get("details")
    details = _dict_or_empty(cast(object, details_raw)) if isinstance(details_raw, dict) else {}
    message_text = message if isinstance(message, str) else ""
    if not bool(info.get("manifest_ok", True)):
        status = "error"
        manifest_message = info.get("manifest_message")
        if isinstance(manifest_message, str) and manifest_message.strip():
            message_text = manifest_message.strip()
    info["dependency_status"] = status
    info["dependency_ok"] = bool(info.get("manifest_ok", True)) and status == "met"
    info["dependency_message"] = message_text
    info["dependency_details"] = details
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def supports_dependency_check(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(info is not None and info.get("has_dependency_check"))


def supports_dependency_install(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(info is not None and info.get("has_dependency_install"))


def _dependency_module_for_extension(extension_id: str) -> Optional[HandlerModule]:
    module_package = _extension_module_packages.get(extension_id)
    extension_root = _extension_root(extension_id)
    if not isinstance(module_package, str) or not module_package:
        return None
    if not isinstance(extension_root, Path):
        return None
    return _import_extension_submodule(module_package, extension_root, "dependencies")


async def _call_dependency_fn(func: Callable[..., object], extension_id: str) -> ObjectMap:
    result = await _invoke_maybe_async(
        func,
        extension_id=extension_id,
        extension_info=get_extension_info(extension_id),
    )
    if isinstance(result, dict):
        return _dict_or_empty(cast(object, result))
    return {"ok": False, "status": "error", "message": "Invalid dependency result"}


async def check_extension_dependencies(extension_id: str) -> ObjectMap:
    if not supports_dependency_check(extension_id):
        return {"ok": True, "status": "met", "message": "No dependency check required"}
    try:
        module = _dependency_module_for_extension(extension_id)
        func = getattr(module, "check_dependencies", None) if module else None
        if not callable(func):
            return {"ok": False, "status": "error", "message": "Dependency check contract missing"}
        return await _call_dependency_fn(func, extension_id)
    except Exception as e:
        return {"ok": False, "status": "error", "message": str(e)}


async def install_extension_dependencies(extension_id: str) -> ObjectMap:
    if not supports_dependency_install(extension_id):
        return {"ok": False, "status": "failed", "message": "Dependency install not supported"}
    try:
        module = _dependency_module_for_extension(extension_id)
        func = getattr(module, "install_dependencies", None) if module else None
        if not callable(func):
            return {"ok": False, "status": "failed", "message": "Dependency install contract missing"}
        result = await _call_dependency_fn(func, extension_id)
        status = str(result.get("status") or ("succeeded" if result.get("ok") else "failed")).strip().lower()
        if status not in {"succeeded", "failed"}:
            status = "succeeded" if result.get("ok") else "failed"
        result["status"] = status
        result["ok"] = status == "succeeded"
        return result
    except Exception as e:
        return {"ok": False, "status": "failed", "message": str(e)}


def _extension_root(extension_id: str) -> Optional[Path]:
    info = _extensions_registry.get(extension_id)
    ext_path = info.get("path") if info is not None else None
    source_root = _extension_source_roots.get(extension_id)
    if not isinstance(ext_path, str) or not ext_path:
        return None
    if not isinstance(source_root, Path):
        return None
    candidate = (source_root / ext_path).resolve()
    try:
        candidate.relative_to(source_root.resolve())
    except ValueError:
        return None
    return candidate


def get_static_settings_schema(extension_id: str) -> ObjectMap | None:
    """Load settings_schema.json through loader-owned extension metadata."""
    extension_root = _extension_root(extension_id)
    if extension_root is None:
        return None
    schema_file = extension_root / "settings_schema.json"
    if not schema_file.is_file():
        return None
    schema = _load_json_file(schema_file)
    return _dict_or_empty(cast(object, schema)) if isinstance(schema, dict) else None


def get_extension_asset_path(extension_id: str, asset_path: str) -> Optional[Path]:
    extension_root = _extension_root(extension_id)
    if extension_root is None:
        return None
    normalized = str(PurePosixPath(str(asset_path or "")).as_posix()).lstrip("/")
    parts = PurePosixPath(normalized).parts
    if not parts or parts[0] not in {"ui", "static"}:
        return None
    candidate = (extension_root / normalized).resolve()
    try:
        candidate.relative_to(extension_root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _normalize_staged_extension_root(staging_root: Path) -> Path:
    manifest_path = staging_root / "manifest.json"
    if manifest_path.is_file():
        return staging_root
    child_roots = [
        child
        for child in staging_root.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / "manifest.json").is_file()
    ]
    if len(child_roots) == 1:
        return child_roots[0]
    raise ValueError(
        "Could not determine extension root; expected manifest.json at the package root or in one enclosing folder"
    )


def _scan_for_symlinks(root: Path) -> list[str]:
    issues: list[str] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in dirnames:
            candidate = current_path / name
            if candidate.is_symlink():
                issues.append(f"Symlinked directories are not supported: {candidate.relative_to(root)}")
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink():
                issues.append(f"Symlinked files are not supported: {candidate.relative_to(root)}")
    return issues


def _validate_manifest_file_reference(
    root: Path,
    raw_path: object,
    *,
    field_label: str,
    errors: list[str],
) -> None:
    if not isinstance(raw_path, str) or not raw_path.strip():
        errors.append(f"{field_label} must be a non-empty relative file path")
        return
    file_part = raw_path.split("#", 1)[0].strip()
    if not file_part:
        errors.append(f"{field_label} must include a file path before any #fragment")
        return
    posix_path = PurePosixPath(file_part)
    if posix_path.is_absolute() or any(part == ".." for part in posix_path.parts):
        errors.append(f"{field_label} must stay within the extension root")
        return
    candidate = root.joinpath(*[part for part in posix_path.parts if part not in {"", "."}])
    if not _path_is_within(candidate, root):
        errors.append(f"{field_label} must stay within the extension root")
        return
    if not candidate.is_file():
        errors.append(f"{field_label} not found: {file_part}")


def _validate_staged_extension_root(
    staged_root: Path,
    *,
    expected_extension_id: Optional[str] = None,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    manifest_path = staged_root / "manifest.json"
    manifest: dict[str, object] = {}
    if not manifest_path.is_file():
        return {
            "ok": False,
            "status": "validation_failed",
            "errors": ["manifest.json is required"],
            "warnings": warnings,
        }
    try:
        manifest_raw = _load_json_file(manifest_path)
    except Exception as exc:
        return {
            "ok": False,
            "status": "validation_failed",
            "errors": [f"manifest.json is not valid JSON: {exc}"],
            "warnings": warnings,
        }
    if not isinstance(manifest_raw, dict):
        return {
            "ok": False,
            "status": "validation_failed",
            "errors": ["manifest.json must contain a JSON object"],
            "warnings": warnings,
        }
    manifest = _dict_or_empty(cast(object, manifest_raw))

    schema_missing = "schema_version" not in manifest
    schema_version = _coerce_schema_version(
        manifest.get("schema_version", _EXTENSION_MANIFEST_SCHEMA_VERSION)
    )
    if schema_version is None:
        errors.append("manifest.schema_version must be a positive integer")
    elif schema_version not in _SUPPORTED_EXTENSION_MANIFEST_SCHEMA_VERSIONS:
        errors.append(
            f"Unsupported manifest schema_version: {schema_version} "
            f"(supported: {sorted(_SUPPORTED_EXTENSION_MANIFEST_SCHEMA_VERSIONS)})"
        )
    elif schema_missing:
        warnings.append(
            f"manifest.schema_version missing; assuming {_EXTENSION_MANIFEST_SCHEMA_VERSION} for compatibility"
        )

    normalized_fields: dict[str, str] = {}
    for field_name in ("id", "name", "type", "version"):
        raw_value = manifest.get(field_name)
        if not isinstance(raw_value, str) or not raw_value.strip():
            errors.append(f"manifest.{field_name} is required and must be a non-empty string")
            continue
        value = raw_value.strip()
        normalized_fields[field_name] = value
        if field_name in {"id", "type"} and any(sep in value for sep in ("/", "\\")):
            errors.append(f"manifest.{field_name} must not contain path separators")

    extension_id = normalized_fields.get("id", "")
    if expected_extension_id and extension_id and extension_id != expected_extension_id:
        errors.append(
            f"manifest.id mismatch: expected {expected_extension_id}, found {extension_id}"
        )

    compat_present = "compat" in manifest
    compat_raw = manifest.get("compat")
    compat: dict[str, object] = {}
    if compat_present:
        if compat_raw is None:
            compat = {}
        elif isinstance(compat_raw, dict):
            compat = _dict_or_empty(cast(object, compat_raw))
        else:
            errors.append("manifest.compat must be an object when present")
    else:
        warnings.append(
            "manifest.compat missing; defaulting to schema_version-only compatibility"
        )
    allowed_compat_keys = {"app_server_manifest_min", "app_server_manifest_max"}
    if compat:
        unknown_keys = sorted(key for key in compat.keys() if key not in allowed_compat_keys)
        if unknown_keys:
            warnings.append(
                "manifest.compat includes unrecognized keys: " + ", ".join(unknown_keys)
            )
    compat_min = _coerce_schema_version(compat.get("app_server_manifest_min"))
    compat_max = _coerce_schema_version(compat.get("app_server_manifest_max"))
    if compat_min is not None and compat_max is not None and compat_min > compat_max:
        errors.append(
            "manifest.compat.app_server_manifest_min must not exceed "
            "manifest.compat.app_server_manifest_max"
        )
    if schema_version is not None and compat_min is not None and schema_version < compat_min:
        errors.append(
            f"manifest.schema_version {schema_version} is below compat.app_server_manifest_min {compat_min}"
        )
    if schema_version is not None and compat_max is not None and schema_version > compat_max:
        errors.append(
            f"manifest.schema_version {schema_version} exceeds compat.app_server_manifest_max {compat_max}"
        )

    client_file = staged_root / "client.py"
    if not client_file.is_file():
        errors.append("client.py is required")

    errors.extend(_scan_for_symlinks(staged_root))

    agent = _dict_or_empty(manifest.get("agent"))
    shellspec = agent.get("shellspec")
    if shellspec is not None:
        _validate_manifest_file_reference(
            staged_root,
            shellspec,
            field_label="manifest.agent.shellspec",
            errors=errors,
        )

    ui = _dict_or_empty(manifest.get("ui"))
    request_cards = ui.get("requestCards")
    if not isinstance(request_cards, list):
        request_cards = ui.get("request_cards")
    if isinstance(request_cards, list):
        for index, entry in enumerate(_object_list(cast(object, request_cards))):
            if not isinstance(entry, dict):
                errors.append(f"manifest.ui.requestCards[{index}] must be an object")
                continue
            entry_map = _dict_or_empty(cast(object, entry))
            _validate_manifest_file_reference(
                staged_root,
                entry_map.get("module"),
                field_label=f"manifest.ui.requestCards[{index}].module",
                errors=errors,
            )

    folder = _sanitize_install_folder(extension_id or staged_root.name)
    return {
        "ok": not errors,
        "status": "validated" if not errors else "validation_failed",
        "errors": errors,
        "warnings": warnings,
        "extension_id": extension_id,
        "name": normalized_fields.get("name", ""),
        "type": normalized_fields.get("type", ""),
        "version": normalized_fields.get("version", ""),
        "schema_version": schema_version,
        "folder": folder,
        "manifest": manifest,
        "compat": {
            "mode": "explicit" if compat_present else "schema_version_only",
            "app_server_manifest_min": compat_min,
            "app_server_manifest_max": compat_max,
        },
    }


def _stage_extension_from_path(source_path: str, workspace_root: Path) -> tuple[Path, dict[str, object]]:
    source = Path(str(source_path or "")).expanduser()
    source_abs = _abs_path(source)
    if not source_abs.exists() or not source_abs.is_dir():
        raise ValueError(f"Extension source path not found: {source_abs}")
    staged = workspace_root / "path_source"
    shutil.copytree(source_abs, staged, symlinks=True)
    return _normalize_staged_extension_root(staged), {
        "type": "path",
        "path": os.fspath(source_abs),
    }


def _stage_extension_from_zip(zip_path: str, workspace_root: Path) -> tuple[Path, dict[str, object]]:
    archive = Path(str(zip_path or "")).expanduser()
    archive_abs = _abs_path(archive)
    if not archive_abs.exists() or not archive_abs.is_file():
        raise ValueError(f"Extension archive not found: {archive_abs}")
    extract_root = workspace_root / "zip_source"
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_abs) as archive_handle:
        members = archive_handle.infolist()
        if not members:
            raise ValueError("Extension archive is empty")
        for member in members:
            name = member.filename
            if not name:
                raise ValueError("Extension archive contains an invalid entry name")
            normalized = PurePosixPath(name)
            if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
                raise ValueError(f"Extension archive entry escapes the package root: {name}")
            mode = (member.external_attr >> 16) & 0o170000
            if stat.S_ISLNK(mode):
                raise ValueError(f"Extension archive contains symlink entry: {name}")
        archive_handle.extractall(extract_root)
    return _normalize_staged_extension_root(extract_root), {
        "type": "zip",
        "path": os.fspath(archive_abs),
    }


def _run_git_command(args: list[str], cwd: Optional[Path] = None) -> str:
    if shutil.which("git") is None:
        raise ValueError("git is not available on PATH")
    result = subprocess.run(
        ["git", *args],
        cwd=os.fspath(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ValueError(message)
    return result.stdout.strip()


def _git_submodule_paths(repo_root: Path) -> list[str]:
    if not (repo_root / ".gitmodules").is_file():
        return []
    try:
        output = _run_git_command(
            ["config", "--file", ".gitmodules", "--get-regexp", "path"],
            cwd=repo_root,
        )
    except Exception:
        return []
    paths: list[str] = []
    for line in output.splitlines():
        _, _, value = line.partition(" ")
        rel_path = value.strip()
        if rel_path and rel_path not in paths:
            paths.append(rel_path)
    return paths


def _copy_local_submodule_worktrees(source_repo: Path, clone_root: Path) -> list[str]:
    copied: list[str] = []
    for rel_path in _git_submodule_paths(source_repo):
        source_path = source_repo / rel_path
        if not source_path.exists():
            continue
        dest_path = clone_root / rel_path
        if dest_path.exists():
            shutil.rmtree(dest_path, ignore_errors=True)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, dest_path, symlinks=True)
        copied.append(rel_path)
    return copied


def _stage_extension_from_git(
    repo_url: str,
    ref: Optional[str],
    workspace_root: Path,
) -> tuple[Path, dict[str, object]]:
    repo_text = str(repo_url or "").strip()
    if not repo_text:
        raise ValueError("repo_url is required for git installs")
    repo_candidate = Path(repo_text).expanduser()
    clone_source = os.fspath(_abs_path(repo_candidate)) if repo_candidate.exists() else repo_text
    clone_root = workspace_root / "git_source"
    _run_git_command(["clone", clone_source, os.fspath(clone_root)])
    ref_text = str(ref or "").strip()
    if ref_text:
        _run_git_command(["checkout", ref_text], cwd=clone_root)
    submodule_paths = _git_submodule_paths(clone_root)
    materialized_from_local: list[str] = []
    materialization_method = "none"
    if repo_candidate.exists() and repo_candidate.is_dir() and submodule_paths:
        materialized_from_local = _copy_local_submodule_worktrees(repo_candidate, clone_root)
    if submodule_paths:
        if len(materialized_from_local) == len(submodule_paths):
            materialization_method = "local_worktree_overlay"
        else:
            _run_git_command(["submodule", "sync", "--recursive"], cwd=clone_root)
            _run_git_command(["submodule", "update", "--init", "--recursive"], cwd=clone_root)
            materialization_method = (
                "local_worktree_overlay+git_recursive_update"
                if materialized_from_local
                else "git_recursive_update"
            )
    commit = _run_git_command(["rev-parse", "HEAD"], cwd=clone_root)
    return _normalize_staged_extension_root(clone_root), {
        "type": "git",
        "repo_url": clone_source,
        "ref": ref_text,
        "commit": commit,
        "submodules": {
            "gitmodules_present": bool(submodule_paths),
            "paths": list(submodule_paths),
            "materialized_from_local": list(materialized_from_local),
            "method": materialization_method,
        },
    }


def _user_registry_entry(extension_id: str) -> Optional[dict[str, object]]:
    user_root = _user_extension_root()
    if user_root is None:
        return None
    registry = _read_extensions_registry(user_root)
    entries_raw = registry.get("extensions")
    if not isinstance(entries_raw, list):
        return None
    for entry in _object_list(cast(object, entries_raw)):
        if not isinstance(entry, dict):
            continue
        entry_map = _dict_or_empty(cast(object, entry))
        if entry_map.get("id") == extension_id:
            return dict(entry_map)
    return None


def _registry_path_owner(root: Path, folder: str, *, excluding_id: Optional[str] = None) -> Optional[dict[str, object]]:
    registry = _read_extensions_registry(root)
    entries_raw = registry.get("extensions")
    if not isinstance(entries_raw, list):
        return None
    for entry in _object_list(cast(object, entries_raw)):
        if not isinstance(entry, dict):
            continue
        entry_map = _dict_or_empty(cast(object, entry))
        if entry_map.get("path") != folder:
            continue
        if excluding_id and entry_map.get("id") == excluding_id:
            continue
        return dict(entry_map)
    return None


def _resolve_install_target_folder(
    extension_id: str,
    validation: dict[str, object],
    existing_user_entry: Optional[dict[str, object]],
) -> tuple[str, str]:
    existing_path = existing_user_entry.get("path") if isinstance(existing_user_entry, dict) else None
    if isinstance(existing_path, str) and existing_path.strip():
        return existing_path.strip(), "registry.path"
    target_folder = str(validation.get("folder") or "").strip() or _sanitize_install_folder(extension_id)
    return target_folder, "manifest.id"


def _build_installer_metadata(
    *,
    existing_user_entry: Optional[dict[str, object]],
    source_meta: dict[str, object],
    validation: dict[str, object],
    target_folder: str,
    path_authority: str,
    action: str,
) -> dict[str, object]:
    now = _now_utc_iso()
    existing_meta = _dict_or_empty(
        existing_user_entry.get("installer_meta") if isinstance(existing_user_entry, dict) else None
    )
    installed_at_raw = existing_meta.get("installed_at")
    installed_at = installed_at_raw.strip() if isinstance(installed_at_raw, str) and installed_at_raw.strip() else None
    if not installed_at and isinstance(existing_user_entry, dict):
        raw_installed_at = existing_user_entry.get("installed_at")
        if isinstance(raw_installed_at, str) and raw_installed_at.strip():
            installed_at = raw_installed_at.strip()
    if not installed_at:
        installed_at = now

    meta: dict[str, object] = {
        "schema_version": _INSTALLER_METADATA_SCHEMA_VERSION,
        "identity_authority": "manifest.id",
        "path_authority": path_authority,
        "install_source_authority": "install_source",
        "source_folder_authority": "ignored",
        "installed_at": installed_at,
        "updated_at": now,
        "last_action": action,
        "current": {
            "path": target_folder,
            "version": validation.get("version"),
            "schema_version": validation.get("schema_version"),
            "install_source": dict(source_meta),
            "compat": _dict_or_empty(validation.get("compat")),
        },
    }
    if isinstance(existing_user_entry, dict):
        previous_source = _registry_install_source(existing_user_entry)
        previous_snapshot: dict[str, object] = {
            "path": existing_user_entry.get("path"),
            "version": existing_user_entry.get("version"),
            "schema_version": existing_user_entry.get("schema_version"),
            "install_source": dict(previous_source) if previous_source else {},
            "captured_at": now,
        }
        previous_updated = existing_meta.get("updated_at")
        if isinstance(previous_updated, str) and previous_updated.strip():
            previous_snapshot["previous_updated_at"] = previous_updated.strip()
        meta["previous"] = previous_snapshot
    return meta


def _install_staged_extension(
    staged_root: Path,
    validation: dict[str, object],
    *,
    source_meta: dict[str, object],
    allow_override: bool = False,
    expect_existing: bool = False,
) -> dict[str, object]:
    if not validation.get("ok"):
        return dict(validation)
    user_root = _user_extension_root()
    if user_root is None:
        return {
            "ok": False,
            "status": "error",
            "message": "User extension root is not configured",
        }
    user_root.mkdir(parents=True, exist_ok=True)
    extension_id = str(validation.get("extension_id") or "").strip()
    existing_info = _extensions_registry.get(extension_id)
    existing_user_entry = _user_registry_entry(extension_id)
    if expect_existing and existing_user_entry is None:
        return {
            "ok": False,
            "status": "not_found",
            "message": f"User-installed extension not found: {extension_id}",
        }
    if (
        existing_info
        and existing_info.get("source_kind") == "builtin"
        and existing_user_entry is None
        and not allow_override
    ):
        return {
            "ok": False,
            "status": "conflict",
            "message": f"Builtin extension id already exists: {extension_id}",
        }

    target_folder, path_authority = _resolve_install_target_folder(
        extension_id,
        validation,
        existing_user_entry,
    )

    path_owner = _registry_path_owner(user_root, target_folder, excluding_id=extension_id)
    if path_owner is not None:
        return {
            "ok": False,
            "status": "conflict",
            "message": f"Extension path already owned by {path_owner.get('id')}: {target_folder}",
        }

    live_target = user_root / target_folder
    temp_target = user_root / f".{target_folder}.install-tmp"
    backup_target = user_root / f".{target_folder}.install-bak"
    if live_target.exists() and live_target.is_symlink():
        return {
            "ok": False,
            "status": "conflict",
            "message": f"Refusing to replace symlinked extension target: {live_target}",
        }
    if temp_target.exists():
        shutil.rmtree(temp_target, ignore_errors=True)
    if backup_target.exists():
        shutil.rmtree(backup_target, ignore_errors=True)
    shutil.copytree(staged_root, temp_target, symlinks=True)
    try:
        if live_target.exists():
            live_target.rename(backup_target)
        temp_target.rename(live_target)
        if backup_target.exists():
            shutil.rmtree(backup_target, ignore_errors=True)
    except Exception:
        shutil.rmtree(temp_target, ignore_errors=True)
        if backup_target.exists() and not live_target.exists():
            backup_target.rename(live_target)
        raise

    manifest_info = _dict_or_empty(validation.get("manifest"))
    enabled_default = bool(manifest_info.get("enabled", True))
    if isinstance(existing_user_entry, dict) and "enabled" in existing_user_entry:
        enabled_default = existing_user_entry.get("enabled") is True
    installer_meta = _build_installer_metadata(
        existing_user_entry=existing_user_entry,
        source_meta=source_meta,
        validation=validation,
        target_folder=target_folder,
        path_authority=path_authority,
        action="update" if existing_user_entry else "install",
    )
    registry_entry = {
        "id": extension_id,
        "name": validation.get("name"),
        "type": validation.get("type"),
        "path": target_folder,
        "enabled": enabled_default,
        "version": validation.get("version"),
        "schema_version": validation.get("schema_version"),
        "install_source": dict(source_meta),
        "installer_meta": installer_meta,
        "installed_at": installer_meta.get("installed_at"),
        "updated_at": installer_meta.get("updated_at"),
    }
    _upsert_registry_entry(user_root, registry_entry)
    return {
        "ok": True,
        "status": "updated" if existing_user_entry else "installed",
        "extension_id": extension_id,
        "name": validation.get("name"),
        "type": validation.get("type"),
        "version": validation.get("version"),
        "schema_version": validation.get("schema_version"),
        "path": target_folder,
        "path_authority": path_authority,
        "target_dir": os.fspath(live_target),
        "warnings": (
            _object_list(cast(object, warnings_value))
            if isinstance((warnings_value := validation.get("warnings")), list)
            else []
        ),
        "install_source": dict(source_meta),
        "installer_meta": installer_meta,
    }


def validate_extension_from_path(source_path: str, extension_id: Optional[str] = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_validate_") as tmp:
        staged_root, source_meta = _stage_extension_from_path(source_path, Path(tmp))
        result = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        result["install_source"] = source_meta
        return result


def validate_extension_from_zip(zip_path: str, extension_id: Optional[str] = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_validate_") as tmp:
        staged_root, source_meta = _stage_extension_from_zip(zip_path, Path(tmp))
        result = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        result["install_source"] = source_meta
        return result


def validate_extension_from_git(
    repo_url: str,
    ref: Optional[str] = None,
    extension_id: Optional[str] = None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_validate_") as tmp:
        staged_root, source_meta = _stage_extension_from_git(repo_url, ref, Path(tmp))
        result = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        result["install_source"] = source_meta
        return result


def validate_extension_source(
    *,
    source_type: str,
    source_path: Optional[str] = None,
    repo_url: Optional[str] = None,
    ref: Optional[str] = None,
    extension_id: Optional[str] = None,
) -> dict[str, object]:
    source_kind = str(source_type or "").strip().lower()
    if source_kind == "path":
        return validate_extension_from_path(source_path or "", extension_id=extension_id)
    if source_kind == "zip":
        return validate_extension_from_zip(source_path or "", extension_id=extension_id)
    if source_kind == "git":
        return validate_extension_from_git(repo_url or "", ref=ref, extension_id=extension_id)
    return {
        "ok": False,
        "status": "validation_failed",
        "errors": [f"Unsupported extension source_type: {source_type}"],
        "warnings": [],
    }


def install_extension_from_path(
    source_path: str,
    extension_id: Optional[str] = None,
    *,
    allow_override: bool = False,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_install_") as tmp:
        staged_root, source_meta = _stage_extension_from_path(source_path, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=allow_override,
            expect_existing=False,
        )


def install_extension_from_zip(
    zip_path: str,
    extension_id: Optional[str] = None,
    *,
    allow_override: bool = False,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_install_") as tmp:
        staged_root, source_meta = _stage_extension_from_zip(zip_path, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=allow_override,
            expect_existing=False,
        )


def install_extension_from_git(
    repo_url: str,
    ref: Optional[str] = None,
    extension_id: Optional[str] = None,
    *,
    allow_override: bool = False,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="app_server_ext_install_") as tmp:
        staged_root, source_meta = _stage_extension_from_git(repo_url, ref, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=str(extension_id or "").strip() or None,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=allow_override,
            expect_existing=False,
        )


def install_extension_source(
    *,
    source_type: str,
    source_path: Optional[str] = None,
    repo_url: Optional[str] = None,
    ref: Optional[str] = None,
    extension_id: Optional[str] = None,
    allow_override: bool = False,
) -> dict[str, object]:
    source_kind = str(source_type or "").strip().lower()
    if source_kind == "path":
        return install_extension_from_path(
            source_path or "",
            extension_id=extension_id,
            allow_override=allow_override,
        )
    if source_kind == "zip":
        return install_extension_from_zip(
            source_path or "",
            extension_id=extension_id,
            allow_override=allow_override,
        )
    if source_kind == "git":
        return install_extension_from_git(
            repo_url or "",
            ref=ref,
            extension_id=extension_id,
            allow_override=allow_override,
        )
    return {
        "ok": False,
        "status": "validation_failed",
        "message": f"Unsupported extension source_type: {source_type}",
    }


def update_extension_from_path(
    extension_id: str,
    source_path: Optional[str] = None,
) -> dict[str, object]:
    current_entry = _user_registry_entry(extension_id)
    current_source = _registry_install_source(current_entry)
    path_value = str(source_path or current_source.get("path") or "").strip()
    if not path_value:
        return {
            "ok": False,
            "status": "validation_failed",
            "message": f"No path source recorded for extension {extension_id}",
        }
    with tempfile.TemporaryDirectory(prefix="app_server_ext_update_") as tmp:
        staged_root, source_meta = _stage_extension_from_path(path_value, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=extension_id,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=False,
            expect_existing=True,
        )


def update_extension_from_zip(
    extension_id: str,
    zip_path: Optional[str] = None,
) -> dict[str, object]:
    current_entry = _user_registry_entry(extension_id)
    current_source = _registry_install_source(current_entry)
    path_value = str(zip_path or current_source.get("path") or "").strip()
    if not path_value:
        return {
            "ok": False,
            "status": "validation_failed",
            "message": f"No zip source recorded for extension {extension_id}",
        }
    with tempfile.TemporaryDirectory(prefix="app_server_ext_update_") as tmp:
        staged_root, source_meta = _stage_extension_from_zip(path_value, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=extension_id,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=False,
            expect_existing=True,
        )


def update_extension_from_git(
    extension_id: str,
    repo_url: Optional[str] = None,
    ref: Optional[str] = None,
) -> dict[str, object]:
    current_entry = _user_registry_entry(extension_id)
    current_source = _registry_install_source(current_entry)
    repo_value = str(repo_url or current_source.get("repo_url") or "").strip()
    ref_value = str(ref or current_source.get("ref") or "").strip() or None
    if not repo_value:
        return {
            "ok": False,
            "status": "validation_failed",
            "message": f"No git source recorded for extension {extension_id}",
        }
    with tempfile.TemporaryDirectory(prefix="app_server_ext_update_") as tmp:
        staged_root, source_meta = _stage_extension_from_git(repo_value, ref_value, Path(tmp))
        validation = _validate_staged_extension_root(
            staged_root,
            expected_extension_id=extension_id,
        )
        return _install_staged_extension(
            staged_root,
            validation,
            source_meta=source_meta,
            allow_override=False,
            expect_existing=True,
        )


def update_extension_source(
    extension_id: str,
    *,
    source_type: Optional[str] = None,
    source_path: Optional[str] = None,
    repo_url: Optional[str] = None,
    ref: Optional[str] = None,
) -> dict[str, object]:
    current_entry = _user_registry_entry(extension_id)
    current_source = _registry_install_source(current_entry)
    source_kind = str(source_type or current_source.get("type") or "").strip().lower()
    if source_kind == "path":
        return update_extension_from_path(extension_id, source_path=source_path)
    if source_kind == "zip":
        return update_extension_from_zip(extension_id, zip_path=source_path)
    if source_kind == "git":
        return update_extension_from_git(extension_id, repo_url=repo_url, ref=ref)
    return {
        "ok": False,
        "status": "validation_failed",
        "message": f"Unsupported extension source_type for update: {source_type or current_source.get('type')}",
    }


def remove_user_extension(extension_id: str) -> dict[str, object]:
    user_root = _user_extension_root()
    if user_root is None:
        return {
            "ok": False,
            "status": "error",
            "message": "User extension root is not configured",
        }
    current_entry = _user_registry_entry(extension_id)
    if current_entry is None:
        return {
            "ok": False,
            "status": "not_found",
            "message": f"User-installed extension not found: {extension_id}",
        }
    folder = current_entry.get("path")
    if not isinstance(folder, str) or not folder.strip():
        return {
            "ok": False,
            "status": "error",
            "message": f"Installed extension path missing for {extension_id}",
        }
    live_target = user_root / folder
    if live_target.exists():
        if not _path_is_within(live_target, user_root):
            return {
                "ok": False,
                "status": "error",
                "message": f"Refusing to remove path outside user extension root: {live_target}",
            }
        if live_target.is_symlink():
            return {
                "ok": False,
                "status": "error",
                "message": f"Refusing to remove symlinked extension target: {live_target}",
            }
        if live_target.is_dir():
            shutil.rmtree(live_target)
        else:
            live_target.unlink()
    _remove_registry_entry(user_root, extension_id)
    return {
        "ok": True,
        "status": "removed",
        "extension_id": extension_id,
        "path": folder,
    }


def is_initialized() -> bool:
    """Check if extensions have been loaded."""
    return _initialized


async def warm_up_extensions(timeout: float = 60.0) -> dict[str, bool]:
    """
    Warm up all extensions that support it.
    Returns dict of extension_id -> success.
    """
    results: dict[str, bool] = {}
    active_by_type: dict[str, list[str]] = {}
    for ext_id, info in _extensions_registry.items():
        if not info.get("active"):
            continue
        ext_type = info.get("type")
        if isinstance(ext_type, str) and ext_type:
            active_by_type.setdefault(ext_type, []).append(ext_id)
    for handler_type, handler in _extension_handlers.items():
        active_ids = active_by_type.get(handler_type, [])
        if not active_ids:
            continue
        warm_up_fn = _callable_attr(handler, "warm_up_all_extensions")
        if warm_up_fn is not None:
            try:
                type_results = _dict_or_empty(await _invoke_maybe_async(warm_up_fn, timeout=timeout))
                if type_results:
                    for ext_id, ready_value in type_results.items():
                        results[ext_id] = bool(ready_value)
                    ready = all(bool(type_results.get(ext_id, False)) for ext_id in active_ids)
                else:
                    ready = True
                for ext_id in active_ids:
                    results.setdefault(ext_id, ready)
            except Exception as e:
                print(f"[Extensions] Warm-up failed for {handler_type}: {e}")
                for ext_id in active_ids:
                    results[ext_id] = False
    return results


def is_extension_ready(extension_id: str) -> bool:
    """Check if an extension has completed warm-up."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    if not ext_info.get("active"):
        return False
    
    ext_type = ext_info.get("type")
    if not isinstance(ext_type, str) or not ext_type:
        return False
    handler = _extension_handlers.get(ext_type)
    is_ready_fn = _callable_attr(handler, "is_extension_ready")
    if is_ready_fn is not None:
        return bool(is_ready_fn(extension_id))
    
    return True  # Extensions without a readiness hook are treated as ready


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    """Wait for an extension to be ready."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    if not ext_info.get("active"):
        return False
    
    ext_type = ext_info.get("type")
    if not isinstance(ext_type, str) or not ext_type:
        return False
    handler = _extension_handlers.get(ext_type)
    wait_ready_fn = _callable_attr(handler, "wait_extension_ready")
    if wait_ready_fn is not None:
        return bool(await _invoke_maybe_async(wait_ready_fn, extension_id, timeout=timeout))
    
    return True  # Extensions without a readiness hook are treated as ready


def requires_eager_session_init(extension_id: str) -> bool:
    """Deprecated — eager init removed. Sessions init on first message only."""
    return False


async def list_models(extension_id: str, **params: object) -> object:
    """List models for an extension. Handler must implement list_models()."""
    list_models_fn = _callable_attr(get_handler(extension_id), "list_models")
    if list_models_fn is not None:
        if params:
            signature = inspect.signature(list_models_fn)
            allows_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            call_kwargs = {
                key: value
                for key, value in params.items()
                if allows_kwargs or key in signature.parameters
            }
            if call_kwargs:
                return await _invoke_maybe_async(list_models_fn, **call_kwargs)
        return await _invoke_maybe_async(list_models_fn)
    return {"models": []}


async def get_settings_schema(extension_id: str) -> Optional[dict[str, object]]:
    """Get the file-owned settings schema for an extension."""
    static_schema = get_static_settings_schema(extension_id)
    if isinstance(static_schema, dict) and static_schema:
        return dict(static_schema)
    return None


async def get_splash_schema(extension_id: str) -> Optional[dict[str, object]]:
    """Get a splash-settings schema for an extension when supported."""
    get_schema_fn = _callable_attr(get_handler(extension_id), "get_splash_schema")
    if get_schema_fn is not None:
        result = await _invoke_maybe_async(get_schema_fn, extension_id=extension_id)
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return None


async def run_splash_action(
    extension_id: str,
    action_id: str,
    payload: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Run a splash-settings action for an extension when supported."""
    run_action_fn = _callable_attr(get_handler(extension_id), "run_splash_action")
    if run_action_fn is not None:
        result = await _invoke_maybe_async(
            run_action_fn,
            extension_id=extension_id,
            action_id=action_id,
            payload=payload if isinstance(payload, dict) else None,
        )
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"ok": False, "error": f"Extension {extension_id} does not support splash actions"}


def _normalize_request_card_entries(manifest: object) -> list[dict[str, object]]:
    if not isinstance(manifest, dict):
        return []
    manifest_map = _dict_or_empty(cast(object, manifest))
    ui = _dict_or_empty(manifest_map.get("ui"))
    raw_entries = ui.get("requestCards")
    if not isinstance(raw_entries, list):
        raw_entries = ui.get("request_cards")
    if not isinstance(raw_entries, list):
        raw_entries = manifest_map.get("requestCards")
    if not isinstance(raw_entries, list):
        raw_entries = manifest_map.get("request_cards")
    if not isinstance(raw_entries, list):
        return []

    entries: list[dict[str, object]] = []
    for index, raw_entry in enumerate(_object_list(cast(object, raw_entries))):
        if not isinstance(raw_entry, dict):
            continue
        raw_entry_map = _dict_or_empty(cast(object, raw_entry))
        module_path = raw_entry_map.get("module") or raw_entry_map.get("module_path")
        if not isinstance(module_path, str) or not module_path.strip():
            continue
        raw_matches: object = raw_entry_map.get("matches")
        match_raw = raw_entry_map.get("match")
        if not isinstance(raw_matches, list) and isinstance(match_raw, dict):
            raw_matches = [cast(object, match_raw)]
        matches: list[dict[str, object]] = []
        if isinstance(raw_matches, list):
            for raw_match in _object_list(cast(object, raw_matches)):
                if not isinstance(raw_match, dict):
                    continue
                raw_match_map = _dict_or_empty(cast(object, raw_match))
                match: dict[str, object] = {}
                request_method = raw_match_map.get("requestMethod") or raw_match_map.get("request_method")
                if isinstance(request_method, str) and request_method.strip():
                    match["request_method"] = request_method.strip().lower()
                kind = raw_match_map.get("kind")
                if isinstance(kind, str) and kind.strip():
                    match["kind"] = kind.strip()
                if match:
                    matches.append(match)
        entries.append({
            "id": raw_entry_map.get("id") or f"request-card-{index}",
            "module": module_path.strip().lstrip("/"),
            "export": raw_entry_map.get("export") or raw_entry_map.get("exportName") or "renderRequestCard",
            "matches": matches,
        })
    return entries


async def get_request_cards(extension_id: str) -> dict[str, object]:
    info = get_extension_info(extension_id) or {}
    manifest_raw = info.get("manifest")
    manifest = _dict_or_empty(cast(object, manifest_raw)) if isinstance(manifest_raw, dict) else {}
    cards = _normalize_request_card_entries(manifest)
    schemas: dict[str, object] = {}
    get_schemas_fn = _callable_attr(get_handler(extension_id), "get_request_card_schemas")
    if get_schemas_fn is not None:
        result = await _invoke_maybe_async(get_schemas_fn, extension_id=extension_id)
        if isinstance(result, dict):
            schemas = _dict_or_empty(cast(object, result))
    return {
        "cards": cards,
        "schemas": schemas,
    }


def _provider_info_unsupported(extension_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "supported": False,
        "extension_id": extension_id,
        "status": {
            "supported": False,
            "state": "unsupported",
            "text": "Provider status is not implemented by this extension.",
            "detail": "",
            "tone": "neutral",
        },
        "usage": {
            "supported": False,
            "state": "unsupported",
            "text": "Provider usage is not implemented by this extension.",
            "detail": "",
            "tone": "neutral",
        },
    }


def _normalize_provider_info_result(
    extension_id: str,
    result: dict[str, object],
) -> dict[str, object]:
    normalized = dict(result)
    normalized.setdefault("ok", True)
    normalized.setdefault("supported", True)
    normalized.setdefault("extension_id", extension_id)
    status = normalized.get("status")
    if not isinstance(status, dict):
        normalized["status"] = {
            "supported": False,
            "state": "unavailable",
            "text": "Provider status unavailable.",
            "detail": "",
            "tone": "neutral",
        }
    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        normalized["usage"] = {
            "supported": False,
            "state": "unavailable",
            "text": "Provider usage unavailable.",
            "detail": "",
            "tone": "neutral",
        }
    return normalized


async def get_provider_info(
    extension_id: str,
    conversation_id: Optional[str] = None,
    provider_session_id: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Get provider status/usage DTO for schema-declared settings info fields."""
    get_provider_info_fn = _callable_attr(get_handler(extension_id), "get_provider_info")
    if get_provider_info_fn is None:
        return _provider_info_unsupported(extension_id)
    raw_result = await _invoke_maybe_async(
        get_provider_info_fn,
        extension_id=extension_id,
        conversation_id=conversation_id,
        provider_session_id=provider_session_id,
        settings=settings,
    )
    if isinstance(raw_result, dict):
        return _normalize_provider_info_result(extension_id, _dict_or_empty(cast(object, raw_result)))
    return {
        "ok": False,
        "supported": True,
        "extension_id": extension_id,
        "status": {
            "supported": False,
            "state": "error",
            "text": "Provider status unavailable.",
            "detail": "Extension returned an invalid provider info DTO.",
            "tone": "error",
        },
        "usage": {
            "supported": False,
            "state": "error",
            "text": "Provider usage unavailable.",
            "detail": "Extension returned an invalid provider info DTO.",
            "tone": "error",
        },
    }


async def run_schema_interaction(
    extension_id: str,
    interaction_id: str,
    action: Optional[str] = None,
    inputs: Optional[dict[str, object]] = None,
    values: Optional[dict[str, object]] = None,
    params: Optional[dict[str, object]] = None,
    conversation_id: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Run a schema-declared settings interaction against an extension."""
    run_fn = _callable_attr(get_handler(extension_id), "run_schema_interaction")
    if run_fn is None:
        return {
            "ok": False,
            "supported": False,
            "extension_id": extension_id,
            "interaction_id": interaction_id,
            "conversation_id": conversation_id,
            "error": "Extension does not implement schema interactions",
        }
    raw_result = await _invoke_maybe_async(
        run_fn,
        extension_id=extension_id,
        interaction_id=interaction_id,
        action=action,
        inputs=inputs if isinstance(inputs, dict) else {},
        values=values if isinstance(values, dict) else {},
        params=params if isinstance(params, dict) else {},
        conversation_id=conversation_id,
        settings=settings if isinstance(settings, dict) else {},
    )
    if isinstance(raw_result, dict):
        normalized = _dict_or_empty(cast(object, raw_result))
        normalized.setdefault("ok", True)
        normalized.setdefault("supported", True)
        normalized.setdefault("extension_id", extension_id)
        normalized.setdefault("interaction_id", interaction_id)
        if conversation_id is not None:
            normalized.setdefault("conversation_id", conversation_id)
        return normalized
    return {
        "ok": False,
        "supported": True,
        "extension_id": extension_id,
        "interaction_id": interaction_id,
        "conversation_id": conversation_id,
        "error": "Extension returned an invalid schema interaction DTO",
    }


def _runtime_option_from_schema_field(
    field: Optional[dict[str, object]],
    settings: Optional[dict[str, object]] = None,
) -> Optional[dict[str, object]]:
    if not isinstance(field, dict):
        return None
    setting_key = field.get("id")
    if not isinstance(setting_key, str) or not setting_key.strip():
        return None
    descriptor: dict[str, object] = {
        "settingKey": setting_key.strip(),
        "runtimeKey": _runtime_option_runtime_key(field) or setting_key.strip(),
        "label": field.get("label") or setting_key.strip(),
        "type": field.get("type") if isinstance(field.get("type"), str) else "",
        "default": field.get("default"),
        "current": settings.get(setting_key) if isinstance(settings, dict) else None,
        "options": _normalize_runtime_options_list(field.get("options")),
    }
    meta = _schema_runtime_option_meta(field)
    if meta.get("footer") is True:
        descriptor["footer"] = True
    if isinstance(meta.get("footerLabel"), str):
        descriptor["footerLabel"] = meta["footerLabel"]
    accents = meta.get("accents")
    if isinstance(accents, dict) and accents:
        descriptor["accents"] = dict(_dict_or_empty(cast(object, accents)))
    if isinstance(meta.get("dynamicOptionsKey"), str):
        descriptor["dynamicOptionsKey"] = meta["dynamicOptionsKey"]
    return descriptor


def _merge_runtime_option_descriptor(base: dict[str, object], override: dict[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key in ("settingKey", "runtimeKey", "label", "type", "default", "current", "dynamicOptionsKey", "footerLabel"):
        if key not in override:
            continue
        value = override.get(key)
        if key in {"settingKey", "runtimeKey", "label", "type", "dynamicOptionsKey", "footerLabel"}:
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()
            continue
        if value is not None:
            merged[key] = value
    if override.get("footer") is True:
        merged["footer"] = True
    options = _normalize_runtime_options_list(override.get("options"))
    if options:
        merged["options"] = options
    accents_raw = override.get("accents")
    if isinstance(accents_raw, dict):
        accents_map = _dict_or_empty(cast(object, accents_raw))
        accents: dict[str, str] = {}
        for key, value in accents_map.items():
            if not key.strip():
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            accents[key.strip()] = value.strip()
        if accents:
            merged["accents"] = accents
    return merged


def _runtime_descriptors_from_schema(
    fields: list[object],
    settings: Optional[dict[str, object]],
) -> tuple[dict[str, dict[str, object]], dict[str, str], list[str]]:
    descriptors: dict[str, dict[str, object]] = {}
    aliases: dict[str, str] = {}
    quick_controls: list[str] = []
    for raw_field in fields:
        if not isinstance(raw_field, dict):
            continue
        descriptor = _runtime_option_from_schema_field(_dict_or_empty(cast(object, raw_field)), settings=settings)
        if not isinstance(descriptor, dict):
            continue
        field_id = descriptor.get("settingKey")
        runtime_key = descriptor.get("runtimeKey")
        if not isinstance(field_id, str) or not field_id:
            continue
        descriptors[field_id] = descriptor
        if isinstance(runtime_key, str) and runtime_key:
            aliases.setdefault(runtime_key, field_id)
            if descriptor.get("footer") is True and runtime_key not in quick_controls:
                quick_controls.append(runtime_key)
    return descriptors, aliases, quick_controls


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Get generic runtime-option descriptors for shared frontend controls."""
    ext_info = _extensions_registry.get(extension_id) or {}
    schema = await get_settings_schema(extension_id)
    fields = schema.get("fields") if isinstance(schema, dict) else None
    schema_fields = _object_list(cast(object, fields)) if isinstance(fields, list) else []
    schema_descriptors, schema_aliases, schema_quick_controls = _runtime_descriptors_from_schema(
        schema_fields,
        settings,
    )
    result: dict[str, object] = {}
    get_runtime_options_fn = _callable_attr(get_handler(extension_id), "get_runtime_options")
    if get_runtime_options_fn is not None:
        raw_result = await _invoke_maybe_async(
            get_runtime_options_fn,
            extension_id=extension_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        if isinstance(raw_result, dict):
            result = _dict_or_empty(cast(object, raw_result))

    result.setdefault("agent", extension_id)
    result.setdefault("has_plan", bool(ext_info.get("has_plan")))
    result.setdefault("has_todo", bool(ext_info.get("has_todo")))
    result.setdefault("has_plan_modes", bool(ext_info.get("has_plan_modes")))

    merged_fields: dict[str, dict[str, object]] = {}
    existing_fields = result.get("fields")
    if isinstance(existing_fields, dict):
        for key, value in _dict_or_empty(cast(object, existing_fields)).items():
            if isinstance(value, dict):
                merged_fields[key] = _dict_or_empty(cast(object, value))

    for field_id, descriptor in schema_descriptors.items():
        merged_descriptor = dict(descriptor)
        existing_descriptor = merged_fields.get(field_id)
        if isinstance(existing_descriptor, dict):
            merged_descriptor = _merge_runtime_option_descriptor(merged_descriptor, existing_descriptor)

        source_key = merged_descriptor.get("dynamicOptionsKey")
        if not isinstance(source_key, str) or not source_key:
            source_key = merged_descriptor.get("runtimeKey") if isinstance(merged_descriptor.get("runtimeKey"), str) else None
        source_descriptor = result.get(source_key) if isinstance(source_key, str) else None
        if isinstance(source_descriptor, dict):
            merged_descriptor = _merge_runtime_option_descriptor(
                merged_descriptor,
                _dict_or_empty(cast(object, source_descriptor)),
            )
            merged_descriptor["settingKey"] = field_id

        merged_fields[field_id] = merged_descriptor

    if merged_fields:
        result["fields"] = merged_fields

    for runtime_key, field_id in schema_aliases.items():
        descriptor = merged_fields.get(field_id)
        if not isinstance(descriptor, dict):
            continue
        existing_descriptor = result.get(runtime_key)
        if isinstance(existing_descriptor, dict):
            merged_descriptor = _merge_runtime_option_descriptor(
                descriptor,
                _dict_or_empty(cast(object, existing_descriptor)),
            )
            merged_descriptor["settingKey"] = field_id
            result[runtime_key] = merged_descriptor
        else:
            result[runtime_key] = dict(descriptor)

    quick_controls: list[str] = []
    existing_quick_controls = result.get("quickControls")
    if isinstance(existing_quick_controls, list):
        for item in _object_list(cast(object, existing_quick_controls)):
            if isinstance(item, str) and item.strip() and item.strip() not in quick_controls:
                quick_controls.append(item.strip())
    for runtime_key in schema_quick_controls:
        if runtime_key == "mode" and not bool(result.get("has_plan_modes")):
            continue
        if runtime_key not in quick_controls:
            quick_controls.append(runtime_key)
    if quick_controls:
        result["quickControls"] = quick_controls

    if schema_descriptors or result:
        return result

    return {
        "agent": extension_id,
        "has_plan": bool(ext_info.get("has_plan")),
        "has_todo": bool(ext_info.get("has_todo")),
        "has_plan_modes": bool(ext_info.get("has_plan_modes")),
    }


async def route_event(
    extension_id: str,
    label: Optional[str],
    payload: object,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict[str, object]:
    """Route a live backend event through an extension-owned router when supported."""
    route_event_fn = _callable_attr(get_handler(extension_id), "route_event")
    if route_event_fn is not None:
        result = await _invoke_maybe_async(
            route_event_fn,
            extension_id=extension_id,
            label=label,
            payload=payload,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id=request_id,
        )
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"handled": False}


async def read_plan(extension_id: str, conversation_id: str) -> dict[str, object]:
    """Read current plan state for an extension conversation when supported."""
    ext_info = _extensions_registry.get(extension_id) or {}
    read_plan_fn = _callable_attr(get_handler(extension_id), "read_plan")
    if read_plan_fn is not None:
        result = await _invoke_maybe_async(
            read_plan_fn,
            extension_id=extension_id,
            conversation_id=conversation_id,
        )
        if isinstance(result, dict):
            plan_info = _dict_or_empty(cast(object, result))
            plan_info.setdefault("has_plan", bool(ext_info.get("has_plan")))
            plan_info.setdefault("has_todo", bool(ext_info.get("has_todo")))
            return plan_info
    return {
        "has_plan": bool(ext_info.get("has_plan")),
        "has_todo": bool(ext_info.get("has_todo")),
        "plan_exists": False,
        "plan_content": "",
        "plan_steps": [],
    }


async def list_sessions(extension_id: str, cwd: Optional[str] = None) -> object:
    """List sessions for an extension. Handler must implement list_sessions()."""
    list_sessions_fn = _callable_attr(get_handler(extension_id), "list_sessions")
    if list_sessions_fn is not None:
        return await _invoke_maybe_async(list_sessions_fn, cwd=cwd)
    return []


def live_session_request_enabled(extension_id: str) -> bool:
    """Return whether an extension manifest opted into live session requests."""
    info = get_extension_info(extension_id)
    if not isinstance(info, dict):
        return False
    capabilities = _dict_or_empty(info.get("capabilities"))
    return capabilities.get("live_session_request") is True or capabilities.get("liveSessionRequest") is True


async def get_live_session_state(
    extension_id: str,
    conversation_id: str,
    provider_session_id: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Query cold-safe provider in-memory session state when manifest-enabled."""
    if not live_session_request_enabled(extension_id):
        return {
            "ok": True,
            "supported": False,
            "state": "unsupported",
            "loaded": False,
            "unload_supported": False,
        }
    state_fn = _callable_attr(get_handler(extension_id), "get_live_session_state")
    if state_fn is None:
        return {
            "ok": True,
            "supported": False,
            "state": "unsupported",
            "loaded": False,
            "unload_supported": False,
        }
    result = await _invoke_maybe_async(
        state_fn,
        conversation_id=conversation_id,
        provider_session_id=provider_session_id,
        settings=settings or {},
    )
    if isinstance(result, dict):
        return _dict_or_empty(cast(object, result))
    return {
        "ok": False,
        "supported": True,
        "state": "unknown",
        "loaded": False,
        "unload_supported": False,
        "error": "Invalid live session state response",
    }


async def unload_live_session(
    extension_id: str,
    conversation_id: str,
    provider_session_id: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Release provider in-memory session state when manifest-enabled."""
    if not live_session_request_enabled(extension_id):
        return {
            "ok": False,
            "supported": False,
            "state": "unsupported",
            "loaded": False,
            "unload_supported": False,
            "error": "Extension does not support live session requests",
        }
    unload_fn = _callable_attr(get_handler(extension_id), "unload_live_session")
    if unload_fn is None:
        return {
            "ok": False,
            "supported": False,
            "state": "unsupported",
            "loaded": False,
            "unload_supported": False,
            "error": "Extension does not implement live session unload",
        }
    result = await _invoke_maybe_async(
        unload_fn,
        conversation_id=conversation_id,
        provider_session_id=provider_session_id,
        settings=settings or {},
    )
    if isinstance(result, dict):
        return _dict_or_empty(cast(object, result))
    return {
        "ok": False,
        "supported": True,
        "state": "unknown",
        "loaded": False,
        "unload_supported": True,
        "error": "Invalid live session unload response",
    }


async def resume_session_with_history(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Bind/import a remote session/thread into a local conversation.

    Handler must implement resume_session_with_history().

    Existing harness-conversation reload is transcript-first and lazy-resumes on
    first send; transcript hydration remains a separate hydrate_transcript()
    concern for port-in/import flows.

    See acp/AGENT_EXTENSION_INTEGRATION.md and CODEX_APP_SERVER_EXTENSION.md.
    """
    resume_fn = _callable_attr(get_handler(extension_id), "resume_session_with_history")
    if resume_fn is not None:
        result = await _invoke_maybe_async(
            resume_fn,
            extension_id=extension_id,
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"ok": False, "error": f"Extension {extension_id} does not support session resume"}


async def fork_conversation(
    extension_id: str,
    source_conversation_id: str,
    conversation_id: str,
    provider_session_id: str,
    cwd: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
    metadata: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Fork a provider conversation/session into a new local conversation id."""
    fork_fn = _callable_attr(get_handler(extension_id), "fork_conversation")
    if fork_fn is None:
        fork_fn = _callable_attr(get_handler(extension_id), "fork_session")
    if fork_fn is not None:
        result = await _invoke_maybe_async(
            fork_fn,
            extension_id=extension_id,
            source_conversation_id=source_conversation_id,
            conversation_id=conversation_id,
            target_conversation_id=conversation_id,
            provider_session_id=provider_session_id,
            session_id=provider_session_id,
            cwd=cwd,
            settings=settings,
            metadata=metadata or {},
        )
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"ok": False, "error": f"Extension {extension_id} does not support conversation fork"}


async def hydrate_transcript(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[dict[str, object]] = None,
) -> list[dict[str, object]]:
    """Get flat transcript entries for a new local conversation from port-in/import.

    Handler must implement hydrate_transcript(session_id, conversation_id, ...).
    Existing harness-conversation reload replays the already-local transcript and
    should not use vendor history loading for ordinary reload/select.

    See acp/AGENT_EXTENSION_INTEGRATION.md and CODEX_APP_SERVER_EXTENSION.md.

    Returns a list of transcript entries in the standard format:
      {role: "user"|"assistant"|"reasoning"|"command"|"diff", text: "...", ...}
    Server writes these via _write_transcript_entries during port-in/import.
    """
    hydrate_fn = _callable_attr(get_handler(extension_id), "hydrate_transcript")
    if hydrate_fn is not None:
        result = await _invoke_maybe_async(
            hydrate_fn,
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
        if isinstance(result, list):
            return coerce_object_list(cast(object, result))
    return []


async def handle_message(
    extension_id: str,
    conversation_id: str,
    text: str,
    settings: Optional[dict[str, object]] = None,
) -> dict[str, object]:
    """Send a message through an extension handler when supported."""
    handle_message_fn = _callable_attr(get_handler(extension_id), "handle_message")
    if handle_message_fn is not None:
        result = await _invoke_maybe_async(
            handle_message_fn,
            conversation_id,
            text,
            extension_id,
            settings or {},
        )
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"ok": False, "error": f"Extension {extension_id} does not support message sending"}


def resolve_approval(extension_id: str, request_id: str, resolution: object) -> bool:
    """Resolve an approval request. Handler must implement resolve_approval()."""
    resolve_fn = _callable_attr(get_handler(extension_id), "resolve_approval")
    if resolve_fn is not None:
        return bool(resolve_fn(request_id, resolution))
    return False


def validate_pending_approval(
    extension_id: str,
    conversation_id: str,
    request_id: str,
    descriptor: dict[str, object],
) -> bool:
    """Validate whether a persisted approval is still actionable for an extension."""
    validate_fn = _callable_attr(get_handler(extension_id), "validate_pending_approval")
    if validate_fn is not None:
        return bool(validate_fn(conversation_id, request_id, descriptor))
    return False


async def shutdown_extension(extension_id: str) -> None:
    """Shutdown an extension. Handler must implement shutdown_client()."""
    shutdown_fn = _callable_attr(get_handler(extension_id), "shutdown_client")
    if shutdown_fn is not None:
        await _invoke_maybe_async(shutdown_fn)


async def interrupt_session(extension_id: str, conversation_id: str) -> dict[str, object]:
    """Interrupt/abort the active turn for an extension session."""
    abort_fn = _callable_attr(get_handler(extension_id), "abort_session")
    if abort_fn is not None:
        ok = await _invoke_maybe_async(abort_fn, conversation_id)
        return {"ok": ok, "conversation_id": conversation_id}
    return {"ok": False, "error": f"Extension {extension_id} does not support interrupt"}


async def compact_session(extension_id: str, conversation_id: str) -> dict[str, object]:
    """Compact/condense the context window for an extension session."""
    compact_fn = _callable_attr(get_handler(extension_id), "compact_session")
    if compact_fn is not None:
        result = await _invoke_maybe_async(compact_fn, conversation_id)
        if isinstance(result, dict):
            return _dict_or_empty(cast(object, result))
    return {"ok": False, "error": f"Extension {extension_id} does not support compact"}


def get_raw_buffer(extension_id: str, limit: int = 50) -> object:
    """Get raw debug buffer. Handler must implement get_raw_buffer()."""
    get_buffer_fn = _callable_attr(get_handler(extension_id), "get_raw_buffer")
    if get_buffer_fn is not None:
        return get_buffer_fn(limit)
    return []
