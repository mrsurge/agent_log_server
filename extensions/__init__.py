"""
Extension Loader

Discovers and loads pluggable agent extensions by scanning subfolders for
manifest.json files.  Each extension lives in its own directory under
extensions/ with:
    manifest.json   — metadata (id, name, type, enabled, capabilities)
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
import sys
import types
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

# Extension type -> handler module
_extension_handlers: Dict[str, Any] = {}
# extension_id -> registry info (id, name, type, path, manifest)
_extensions_registry: Dict[str, Dict[str, Any]] = {}
_extension_source_roots: Dict[str, Path] = {}
_extension_module_packages: Dict[str, str] = {}
_initialized: bool = False

# Callbacks stored for lazy init of discovered extensions
_init_args: Dict[str, Any] = {}
_DYNAMIC_EXTENSION_NAMESPACE = "_app_server_user_extensions"


def _deep_merge_manifest(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge_manifest(merged.get(key), value)
        return merged
    return override


def _manifest_capability_flag(manifest: Any, *names: str) -> bool:
    if not isinstance(manifest, dict):
        return False
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    return any(bool(capabilities.get(name)) for name in names)


def _manifest_ui_quote_parsing_enabled(manifest: Any) -> bool:
    if not isinstance(manifest, dict):
        return False
    ui = manifest.get("ui")
    if not isinstance(ui, dict):
        return False
    semantic_shell = None
    for key in ("semanticShellRibbon", "semantic_shell_ribbon"):
        value = ui.get(key)
        if isinstance(value, dict):
            semantic_shell = value
            break
    if not isinstance(semantic_shell, dict):
        return False
    return bool(semantic_shell.get("quoteParsing") or semantic_shell.get("quote_parsing"))


def _normalize_tool_render_spec(spec_raw: Any) -> Optional[Dict[str, Any]]:
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
    kind_raw = spec_raw.get("kind")
    kind = kind_raw.strip().lower() if isinstance(kind_raw, str) and kind_raw.strip() else ""
    language_raw = spec_raw.get("language")
    language = language_raw.strip() if isinstance(language_raw, str) and language_raw.strip() else ""
    if kind not in {"plain", "markdown", "hljs"}:
        if language:
            return {"kind": "hljs", "language": language}
        return None
    spec: Dict[str, Any] = {"kind": kind}
    if kind == "hljs" and language:
        spec["language"] = language
    return spec


def _normalize_tool_render_field_map(fields_raw: Any) -> Dict[str, Dict[str, Any]]:
    normalized: Dict[str, Dict[str, Any]] = {}
    if not isinstance(fields_raw, dict):
        return normalized
    for key, value in fields_raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        spec = _normalize_tool_render_spec(value)
        if spec is None:
            continue
        normalized[key.strip()] = spec
    return normalized


def _normalize_tool_render_rule(rule_raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(rule_raw, dict):
        return None
    rule: Dict[str, Any] = {}
    for field in ("server", "tool", "serverPrefix", "toolPrefix"):
        value = rule_raw.get(field)
        if isinstance(value, str) and value.strip():
            rule[field] = value.strip()
    for field in ("servers", "tools"):
        value = rule_raw.get(field)
        if isinstance(value, list):
            items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
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
        spec = _normalize_tool_render_spec(rule_raw.get(source_key))
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
        fields = _normalize_tool_render_field_map(rule_raw.get(source_key))
        if fields:
            rule[target_key] = fields
    if not rule:
        return None
    return rule


def _manifest_tool_render_policy(manifest: Any) -> Dict[str, Any]:
    default_policy: Dict[str, Any] = {
        "default": {
            "request": {"kind": "plain"},
            "response": {"kind": "plain"},
        },
        "rules": [],
    }
    if not isinstance(manifest, dict):
        return default_policy
    ui = manifest.get("ui")
    if not isinstance(ui, dict):
        return default_policy
    policy_raw = ui.get("toolRenderPolicy")
    if not isinstance(policy_raw, dict):
        return default_policy
    default_raw = policy_raw.get("default")
    if isinstance(default_raw, dict):
        normalized_default: Dict[str, Any] = {}
        request_spec = _normalize_tool_render_spec(
            default_raw.get("request") or default_raw.get("args") or default_raw.get("arguments")
        )
        if request_spec is not None:
            normalized_default["request"] = request_spec
        response_spec = _normalize_tool_render_spec(default_raw.get("response") or default_raw.get("result"))
        if response_spec is not None:
            normalized_default["response"] = response_spec
        request_fields = _normalize_tool_render_field_map(
            default_raw.get("requestFields") or default_raw.get("argsFields") or default_raw.get("argumentsFields")
        )
        if request_fields:
            normalized_default["requestFields"] = request_fields
        response_fields = _normalize_tool_render_field_map(
            default_raw.get("responseFields") or default_raw.get("resultFields")
        )
        if response_fields:
            normalized_default["responseFields"] = response_fields
        if normalized_default:
            default_policy["default"] = {
                **default_policy["default"],
                **normalized_default,
            }
    rules_raw = policy_raw.get("rules")
    if isinstance(rules_raw, list):
        default_policy["rules"] = [
            rule
            for rule in (_normalize_tool_render_rule(rule_raw) for rule_raw in rules_raw)
            if rule is not None
        ]
    return default_policy


def _normalize_runtime_options_list(options_raw: Any) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    if not isinstance(options_raw, list):
        return options
    for item in options_raw:
        if isinstance(item, str):
            text = item.strip()
            if text:
                options.append({"value": text, "label": text})
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            label = value
        option: Dict[str, Any] = {
            "value": value.strip(),
            "label": label.strip(),
        }
        if item.get("deprecated") is True:
            option["deprecated"] = True
        options.append(option)
    return options


def _schema_runtime_option_meta(field: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    runtime_option = field.get("runtime_option")
    if isinstance(runtime_option, str):
        text = runtime_option.strip()
        if text:
            meta["kind"] = text
    elif isinstance(runtime_option, dict):
        kind = runtime_option.get("kind")
        if isinstance(kind, str) and kind.strip():
            meta["kind"] = kind.strip()
        if runtime_option.get("footer") is True:
            meta["footer"] = True
        footer_label = runtime_option.get("footer_label")
        if isinstance(footer_label, str) and footer_label.strip():
            meta["footerLabel"] = footer_label.strip()
        accents_raw = runtime_option.get("accents")
        if isinstance(accents_raw, dict):
            accents: Dict[str, str] = {}
            for key, value in accents_raw.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                if not isinstance(value, str) or not value.strip():
                    continue
                accents[key.strip()] = value.strip()
            if accents:
                meta["accents"] = accents
    dynamic_options_key = field.get("dynamic_options_key")
    if isinstance(dynamic_options_key, str) and dynamic_options_key.strip():
        meta["dynamicOptionsKey"] = dynamic_options_key.strip()
    return meta


def _runtime_option_runtime_key(field: Dict[str, Any]) -> Optional[str]:
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


def _normalize_extension_roots(extension_roots: Any) -> List[Path]:
    roots: List[Path] = []
    raw_roots = extension_roots if isinstance(extension_roots, (list, tuple, set)) else [extension_roots]
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


def _import_extension_submodule(module_package: str, extension_root: Path, submodule: str) -> Any:
    if not module_package.startswith(f"{_DYNAMIC_EXTENSION_NAMESPACE}."):
        return importlib.import_module(f"{module_package}.{submodule}")
    _ensure_dynamic_extension_package(module_package, extension_root)
    importlib.invalidate_caches()
    return importlib.import_module(f"{module_package}.{submodule}")


def load_extensions(
    extensions_dir: Any,
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]] = None,
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
    _extensions_registry = {}
    _extension_source_roots = {}
    _extension_module_packages = {}

    enabled_by_type: Dict[str, List[Dict[str, Any]]] = {}
    for ext_info in discovered:
        ext_id = ext_info["id"]
        ext_type = ext_info["type"]
        folder = ext_info["folder"]
        manifest = ext_info.get("manifest") if isinstance(ext_info.get("manifest"), dict) else {}
        dependencies = manifest.get("dependencies") if isinstance(manifest.get("dependencies"), dict) else {}
        default_enabled = bool(ext_info.get("enabled", True))
        source_root = ext_info.get("source_root")
        module_package = ext_info.get("module_package")

        if isinstance(source_root, Path):
            _extension_source_roots[ext_id] = source_root
        if isinstance(module_package, str) and module_package:
            _extension_module_packages[ext_id] = module_package

        _extensions_registry[ext_id] = {
            "id": ext_id,
            "name": ext_info.get("name", ext_id),
            "type": ext_type,
            "path": folder,
            "manifest": manifest,
            "capabilities": manifest.get("capabilities", {}) if isinstance(manifest, dict) else {},
            "ui": manifest.get("ui", {}) if isinstance(manifest, dict) else {},
            "has_plan": _manifest_capability_flag(manifest, "hasPlan", "has_plan"),
            "has_todo": _manifest_capability_flag(manifest, "hasTodo", "has_todo"),
            "has_plan_modes": _manifest_capability_flag(manifest, "hasPlanModes", "has_plan_modes"),
            "default_enabled": default_enabled,
            "enabled": default_enabled,
            "dependency_status": "unchecked",
            "dependency_ok": True,
            "dependency_message": "",
            "dependency_details": {},
            "has_dependency_check": bool(dependencies.get("has_check")),
            "has_dependency_install": bool(dependencies.get("has_install")),
            "active": default_enabled,
        }

        if default_enabled:
            enabled_by_type.setdefault(ext_type, []).append(ext_info)

    for ext_info in discovered:
        if not ext_info.get("enabled", True):
            continue
        ext_type = ext_info["type"]
        folder = ext_info["folder"]

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


def _discover_extensions_in_root(extensions_dir: Path, builtin_root: Optional[Path]) -> List[Dict[str, Any]]:
    """Return list of extension info dicts from manifests."""
    result: List[Dict[str, Any]] = []
    if not extensions_dir.exists() or not extensions_dir.is_dir():
        return result

    # Strategy 1: explicit extensions.json
    extensions_json = extensions_dir / "extensions.json"
    if extensions_json.exists():
        try:
            data = json.loads(extensions_json.read_text())
            for entry in data.get("extensions", []):
                folder = entry.get("path", entry.get("id", ""))
                manifest_path = extensions_dir / folder / "manifest.json"
                manifest = {}
                if manifest_path.exists():
                    try:
                        manifest = json.loads(manifest_path.read_text())
                    except Exception:
                        pass
                if isinstance(entry.get("manifest_overrides"), dict):
                    manifest = _deep_merge_manifest(manifest, entry["manifest_overrides"])
                result.append({
                    "id": entry.get("id") or manifest.get("id", folder),
                    "name": entry.get("name") or manifest.get("name", folder),
                    "type": entry.get("type") or manifest.get("type", folder),
                    "enabled": entry.get("enabled", manifest.get("enabled", True)),
                    "folder": folder,
                    "manifest": manifest,
                    "source_root": extensions_dir,
                    "module_package": _module_package_for_root(folder, extensions_dir, builtin_root),
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
            manifest = json.loads(manifest_path.read_text())
            result.append({
                "id": manifest.get("id", sub.name),
                "name": manifest.get("name", sub.name),
                "type": manifest.get("type", sub.name),
                "enabled": manifest.get("enabled", True),
                "folder": sub.name,
                "manifest": manifest,
                "source_root": extensions_dir,
                "module_package": _module_package_for_root(sub.name, extensions_dir, builtin_root),
            })
        except Exception as e:
            print(f"[Extensions] Bad manifest in {sub.name}/: {e}")

    return result


def _discover_extensions(extension_roots: List[Path]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
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


def _load_handler(
    ext_info: Dict[str, Any],
    server_root: Path,
    fws_getter: Callable,
    broadcast_fn: Callable,
    transcript_fn: Callable,
    meta_fns: Optional[Dict[str, Callable]],
    handler_extensions: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Any]:
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
    init_fn = getattr(mod, init_fn_name, None)
    if init_fn is None:
        # Fallback: try init_<folder>_manager (folder may differ from type)
        init_fn = getattr(mod, f"init_{folder}_manager", None)
    if init_fn is None:
        # Fallback: scan for any init_*_manager function
        for attr in dir(mod):
            if attr.startswith("init_") and attr.endswith("_manager") and callable(getattr(mod, attr)):
                init_fn = getattr(mod, attr)
                break
    if init_fn is None:
        # Last resort: try generic init_manager
        init_fn = getattr(mod, "init_manager", None)
    if init_fn is None:
        print(f"[Extensions] {module_path} has no {init_fn_name}() or init_manager()")
        return mod  # still return module — may work without init

    init_kwargs: Dict[str, Any] = {}
    try:
        init_signature = inspect.signature(init_fn)
    except (TypeError, ValueError):
        init_signature = None
    if init_signature and "registered_extension_ids" in init_signature.parameters:
        init_kwargs["registered_extension_ids"] = [
            ext_id
            for ext_id in (
                entry.get("id") if isinstance(entry, dict) else None
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


def get_handler(extension_id: str) -> Optional[Any]:
    """Get the handler module for an extension by its ID."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return None
    return _extension_handlers.get(ext_info["type"])


def has_extension(extension_id: str) -> bool:
    """Check if an extension is registered."""
    info = _extensions_registry.get(extension_id)
    return bool(info and info.get("active"))


def list_extensions() -> List[Dict[str, Any]]:
    """List all registered extensions."""
    return [dict(info) for info in _extensions_registry.values()]


def get_extension_info(extension_id: str) -> Optional[Dict[str, Any]]:
    """Return registry metadata for one extension."""
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return None
    return dict(info)


def get_extension_ui_features(extension_id: str) -> Dict[str, Any]:
    """Return manifest-driven frontend behavior flags for one extension."""
    info = _extensions_registry.get(extension_id)
    manifest = info.get("manifest") if isinstance(info, dict) and isinstance(info.get("manifest"), dict) else {}
    return {
        "semanticShellRibbon": {
            "quoteParsing": _manifest_ui_quote_parsing_enabled(manifest),
        },
        "toolRenderPolicy": _manifest_tool_render_policy(manifest),
    }


def _recompute_extension_active_state(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    info["active"] = bool(info.get("enabled")) and bool(info.get("dependency_ok"))
    return bool(info.get("active"))


def _active_extensions_for_type(ext_type: str) -> List[Dict[str, Any]]:
    return [
        info
        for info in _extensions_registry.values()
        if isinstance(info, dict) and info.get("type") == ext_type and info.get("active")
    ]


def _ensure_handler_loaded_for_extension(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
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
    meta_fns = _init_args.get("meta_fns")
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
    if not isinstance(info, dict):
        return False
    info["enabled"] = bool(enabled)
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def set_extension_dependency_result(extension_id: str, result: Optional[Dict[str, Any]]) -> bool:
    info = _extensions_registry.get(extension_id)
    if not isinstance(info, dict):
        return False
    payload = result if isinstance(result, dict) else {}
    status = str(payload.get("status") or ("met" if payload.get("ok") else "error")).strip().lower()
    if status not in {"met", "unmet", "error"}:
        status = "met" if payload.get("ok") else "error"
    message = payload.get("message")
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    info["dependency_status"] = status
    info["dependency_ok"] = status == "met"
    info["dependency_message"] = message if isinstance(message, str) else ""
    info["dependency_details"] = details
    became_active = _recompute_extension_active_state(extension_id)
    if became_active:
        _ensure_handler_loaded_for_extension(extension_id)
    return True


def supports_dependency_check(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(isinstance(info, dict) and info.get("has_dependency_check"))


def supports_dependency_install(extension_id: str) -> bool:
    info = _extensions_registry.get(extension_id)
    return bool(isinstance(info, dict) and info.get("has_dependency_install"))


def _dependency_module_for_extension(extension_id: str) -> Optional[Any]:
    info = _extensions_registry.get(extension_id)
    module_package = _extension_module_packages.get(extension_id)
    extension_root = _extension_root(extension_id)
    if not isinstance(module_package, str) or not module_package:
        return None
    if not isinstance(extension_root, Path):
        return None
    return _import_extension_submodule(module_package, extension_root, "dependencies")


async def _call_dependency_fn(func: Callable[..., Any], extension_id: str) -> Dict[str, Any]:
    result = func(extension_id=extension_id, extension_info=get_extension_info(extension_id))
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        return result
    return {"ok": False, "status": "error", "message": "Invalid dependency result"}


async def check_extension_dependencies(extension_id: str) -> Dict[str, Any]:
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


async def install_extension_dependencies(extension_id: str) -> Dict[str, Any]:
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
    ext_path = info.get("path") if isinstance(info, dict) else None
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


def get_static_settings_schema(extension_id: str) -> Optional[Dict[str, Any]]:
    """Load settings_schema.json through loader-owned extension metadata."""
    extension_root = _extension_root(extension_id)
    if extension_root is None:
        return None
    schema_file = extension_root / "settings_schema.json"
    if not schema_file.is_file():
        return None
    return json.loads(schema_file.read_text(encoding="utf-8"))


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


def is_initialized() -> bool:
    """Check if extensions have been loaded."""
    return _initialized


async def warm_up_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    """
    Warm up all extensions that support it.
    Returns dict of extension_id -> success.
    """
    results: Dict[str, bool] = {}
    active_by_type: Dict[str, List[str]] = {}
    for ext_id, info in _extensions_registry.items():
        if not isinstance(info, dict) or not info.get("active"):
            continue
        ext_type = info.get("type")
        if isinstance(ext_type, str) and ext_type:
            active_by_type.setdefault(ext_type, []).append(ext_id)
    for handler_type, handler in _extension_handlers.items():
        active_ids = active_by_type.get(handler_type, [])
        if not active_ids:
            continue
        if hasattr(handler, "warm_up_all_extensions"):
            try:
                type_results = await handler.warm_up_all_extensions(timeout=timeout)
                if isinstance(type_results, dict) and type_results:
                    results.update(type_results)
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
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "is_extension_ready"):
        return handler.is_extension_ready(extension_id)
    
    return True  # Non-ACP extensions are always ready


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    """Wait for an extension to be ready."""
    ext_info = _extensions_registry.get(extension_id)
    if not ext_info:
        return False
    if not ext_info.get("active"):
        return False
    
    handler = _extension_handlers.get(ext_info["type"])
    if handler and hasattr(handler, "wait_extension_ready"):
        return await handler.wait_extension_ready(extension_id, timeout=timeout)
    
    return True  # Non-ACP extensions are always ready


def requires_eager_session_init(extension_id: str) -> bool:
    """Deprecated — eager init removed. Sessions init on first message only."""
    return False


async def list_models(extension_id: str) -> Any:
    """List models for an extension. Handler must implement list_models()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "list_models"):
        return await handler.list_models()
    return {"models": []}


async def get_settings_schema(extension_id: str) -> Optional[Dict[str, Any]]:
    """Get a dynamic settings schema for an extension when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_settings_schema"):
        return await handler.get_settings_schema(extension_id=extension_id)
    return None


async def get_splash_schema(extension_id: str) -> Optional[Dict[str, Any]]:
    """Get a splash-settings schema for an extension when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_splash_schema"):
        return await handler.get_splash_schema(extension_id=extension_id)
    return None


async def run_splash_action(
    extension_id: str,
    action_id: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a splash-settings action for an extension when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "run_splash_action"):
        result = await handler.run_splash_action(
            extension_id=extension_id,
            action_id=action_id,
            payload=payload if isinstance(payload, dict) else None,
        )
        if isinstance(result, dict):
            return result
    return {"ok": False, "error": f"Extension {extension_id} does not support splash actions"}


def _normalize_request_card_entries(manifest: Any) -> List[Dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    ui = manifest.get("ui") if isinstance(manifest.get("ui"), dict) else {}
    raw_entries = ui.get("requestCards")
    if not isinstance(raw_entries, list):
        raw_entries = ui.get("request_cards")
    if not isinstance(raw_entries, list):
        raw_entries = manifest.get("requestCards")
    if not isinstance(raw_entries, list):
        raw_entries = manifest.get("request_cards")
    if not isinstance(raw_entries, list):
        return []

    entries: List[Dict[str, Any]] = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            continue
        module_path = raw_entry.get("module") or raw_entry.get("module_path")
        if not isinstance(module_path, str) or not module_path.strip():
            continue
        raw_matches = raw_entry.get("matches")
        if not isinstance(raw_matches, list) and isinstance(raw_entry.get("match"), dict):
            raw_matches = [raw_entry["match"]]
        matches: List[Dict[str, Any]] = []
        if isinstance(raw_matches, list):
            for raw_match in raw_matches:
                if not isinstance(raw_match, dict):
                    continue
                match: Dict[str, Any] = {}
                request_method = raw_match.get("requestMethod") or raw_match.get("request_method")
                if isinstance(request_method, str) and request_method.strip():
                    match["request_method"] = request_method.strip().lower()
                kind = raw_match.get("kind")
                if isinstance(kind, str) and kind.strip():
                    match["kind"] = kind.strip()
                if match:
                    matches.append(match)
        entries.append({
            "id": raw_entry.get("id") or f"request-card-{index}",
            "module": module_path.strip().lstrip("/"),
            "export": raw_entry.get("export") or raw_entry.get("exportName") or "renderRequestCard",
            "matches": matches,
        })
    return entries


async def get_request_cards(extension_id: str) -> Dict[str, Any]:
    info = get_extension_info(extension_id) or {}
    manifest = info.get("manifest") if isinstance(info.get("manifest"), dict) else {}
    cards = _normalize_request_card_entries(manifest)
    schemas: Dict[str, Any] = {}
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_request_card_schemas"):
        result = await handler.get_request_card_schemas(extension_id=extension_id)
        if isinstance(result, dict):
            schemas = result
    return {
        "cards": cards,
        "schemas": schemas,
    }


def _runtime_option_from_schema_field(
    field: Optional[Dict[str, Any]],
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not isinstance(field, dict):
        return None
    setting_key = field.get("id")
    if not isinstance(setting_key, str) or not setting_key.strip():
        return None
    descriptor: Dict[str, Any] = {
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
        descriptor["accents"] = dict(accents)
    if isinstance(meta.get("dynamicOptionsKey"), str):
        descriptor["dynamicOptionsKey"] = meta["dynamicOptionsKey"]
    return descriptor


def _merge_runtime_option_descriptor(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
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
        accents: Dict[str, str] = {}
        for key, value in accents_raw.items():
            if not isinstance(key, str) or not key.strip():
                continue
            if not isinstance(value, str) or not value.strip():
                continue
            accents[key.strip()] = value.strip()
        if accents:
            merged["accents"] = accents
    return merged


def _runtime_descriptors_from_schema(
    fields: List[Any],
    settings: Optional[Dict[str, Any]],
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, str], List[str]]:
    descriptors: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}
    quick_controls: List[str] = []
    for raw_field in fields:
        if not isinstance(raw_field, dict):
            continue
        descriptor = _runtime_option_from_schema_field(raw_field, settings=settings)
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
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Get generic runtime-option descriptors for shared frontend controls."""
    ext_info = _extensions_registry.get(extension_id) or {}
    schema = await get_settings_schema(extension_id)
    fields = schema.get("fields") if isinstance(schema, dict) else None
    schema_fields = fields if isinstance(fields, list) else []
    schema_descriptors, schema_aliases, schema_quick_controls = _runtime_descriptors_from_schema(
        schema_fields,
        settings,
    )
    handler = get_handler(extension_id)
    result: Dict[str, Any] = {}
    if handler and hasattr(handler, "get_runtime_options"):
        raw_result = await handler.get_runtime_options(
            extension_id=extension_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        if isinstance(raw_result, dict):
            result = dict(raw_result)

    result.setdefault("agent", extension_id)
    result.setdefault("has_plan", bool(ext_info.get("has_plan")))
    result.setdefault("has_todo", bool(ext_info.get("has_todo")))
    result.setdefault("has_plan_modes", bool(ext_info.get("has_plan_modes")))

    merged_fields: Dict[str, Dict[str, Any]] = {}
    existing_fields = result.get("fields")
    if isinstance(existing_fields, dict):
        for key, value in existing_fields.items():
            if isinstance(key, str) and isinstance(value, dict):
                merged_fields[key] = dict(value)

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
            merged_descriptor = _merge_runtime_option_descriptor(merged_descriptor, source_descriptor)
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
            merged_descriptor = _merge_runtime_option_descriptor(descriptor, existing_descriptor)
            merged_descriptor["settingKey"] = field_id
            result[runtime_key] = merged_descriptor
        else:
            result[runtime_key] = dict(descriptor)

    quick_controls: List[str] = []
    existing_quick_controls = result.get("quickControls")
    if isinstance(existing_quick_controls, list):
        for item in existing_quick_controls:
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
    payload: Any,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Route a live backend event through an extension-owned router when supported."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "route_event"):
        return await handler.route_event(
            extension_id=extension_id,
            label=label,
            payload=payload,
            conversation_id=conversation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id=request_id,
        )
    return {"handled": False}


async def read_plan(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    """Read current plan state for an extension conversation when supported."""
    ext_info = _extensions_registry.get(extension_id) or {}
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "read_plan"):
        result = await handler.read_plan(
            extension_id=extension_id,
            conversation_id=conversation_id,
        )
        if isinstance(result, dict):
            result.setdefault("has_plan", bool(ext_info.get("has_plan")))
            result.setdefault("has_todo", bool(ext_info.get("has_todo")))
            return result
    return {
        "has_plan": bool(ext_info.get("has_plan")),
        "has_todo": bool(ext_info.get("has_todo")),
        "plan_exists": False,
        "plan_content": "",
        "plan_steps": [],
    }


async def list_sessions(extension_id: str, cwd: Optional[str] = None) -> Any:
    """List sessions for an extension. Handler must implement list_sessions()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "list_sessions"):
        return await handler.list_sessions(cwd=cwd)
    return []


async def resume_session_with_history(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resume a session and hydrate transcript. Handler must implement resume_session_with_history()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resume_session_with_history"):
        return await handler.resume_session_with_history(
            extension_id=extension_id,
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
    return {"ok": False, "error": f"Extension {extension_id} does not support session resume"}


async def hydrate_transcript(
    extension_id: str,
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Get flat transcript entries for an existing session (port-in).

    Handler must implement hydrate_transcript(session_id, conversation_id, ...).
    Returns a list of transcript entries in the standard format:
      {role: "user"|"assistant"|"reasoning"|"command"|"diff", text: "...", ...}
    Server.py writes these via _write_transcript_entries — same as bind-rollout.
    """
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "hydrate_transcript"):
        return await handler.hydrate_transcript(
            session_id=session_id,
            conversation_id=conversation_id,
            cwd=cwd,
            model=model,
            settings=settings,
        )
    return []


def resolve_approval(extension_id: str, request_id: str, resolution: Any) -> bool:
    """Resolve an approval request. Handler must implement resolve_approval()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "resolve_approval"):
        return bool(handler.resolve_approval(request_id, resolution))
    return False


def validate_pending_approval(
    extension_id: str,
    conversation_id: str,
    request_id: str,
    descriptor: Dict[str, Any],
) -> bool:
    """Validate whether a persisted approval is still actionable for an extension."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "validate_pending_approval"):
        return bool(handler.validate_pending_approval(conversation_id, request_id, descriptor))
    return False


async def shutdown_extension(extension_id: str) -> None:
    """Shutdown an extension. Handler must implement shutdown_client()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "shutdown_client"):
        await handler.shutdown_client()


async def interrupt_session(extension_id: str, conversation_id: str) -> Dict[str, Any]:
    """Interrupt/abort the active turn for an extension session."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "abort_session"):
        ok = await handler.abort_session(conversation_id)
        return {"ok": ok, "conversation_id": conversation_id}
    return {"ok": False, "error": f"Extension {extension_id} does not support interrupt"}


def get_raw_buffer(extension_id: str, limit: int = 50) -> Any:
    """Get raw debug buffer. Handler must implement get_raw_buffer()."""
    handler = get_handler(extension_id)
    if handler and hasattr(handler, "get_raw_buffer"):
        return handler.get_raw_buffer(limit)
    return []
