from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, cast

import extensions as ext_loader

from agent_log_server.typing_helpers import ObjectMap, coerce_object_map
from agent_log_server_rs import bootstrap

SETTINGS_RPC_NAMESPACE = "/rpc/settings"
RPC_EVENT = "rpc"
JSONRPC_VERSION = "2.0"


def run_extension_cli(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    json_output = bool(getattr(args, "json_output", False))
    try:
        env = _bootstrap_env(args)
        _initialize_loader(env)
        body = _run_command(args)
        if _should_notify_server(args, body):
            body["server_notify"] = _notify_running_server(args)
    except ValueError as exc:
        _print_error(json_output, {"ok": False, "message": str(exc)})
        return 2
    except Exception as exc:
        _print_error(json_output, {"ok": False, "message": str(exc)})
        return 1

    if json_output:
        print(json.dumps(body, indent=2, sort_keys=True, default=str))
    else:
        _print_human_result(str(getattr(args, "extension_command", "") or ""), body)
    return 0 if body.get("ok") is True else 1


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="als-rs extension",
        description="Manage ALS-RS extension packages in the user extension root.",
    )
    commands = parser.add_subparsers(dest="extension_command", required=True)

    validate_parser = commands.add_parser("validate", help="Validate an extension package source")
    _add_common_flags(validate_parser)
    _add_source_flags(validate_parser, required=True)
    validate_parser.add_argument("--id", dest="extension_id", help="Expected extension id")

    install_parser = commands.add_parser("install", help="Install an extension package")
    _add_common_flags(install_parser)
    _add_source_flags(install_parser, required=True)
    install_parser.add_argument("--id", dest="extension_id", help="Expected extension id")
    install_parser.add_argument("--allow-override", action="store_true", help="Allow overriding a builtin id")
    install_parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="Run dependency installation locally after package install if supported",
    )

    update_parser = commands.add_parser("update", help="Update an installed extension")
    _add_common_flags(update_parser)
    update_parser.add_argument("extension_id", help="Installed extension id")
    _add_source_flags(update_parser, required=False)
    update_parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="Run dependency installation locally after package update if supported",
    )

    remove_parser = commands.add_parser("remove", help="Remove an installed user extension")
    _add_common_flags(remove_parser)
    remove_parser.add_argument("extension_id", help="Installed extension id")

    reload_parser = commands.add_parser("reload", help="Reload local and running-server extension discovery")
    _add_common_flags(reload_parser)
    reload_parser.add_argument("extension_ids", nargs="*", help="Optional extension ids to target")
    reload_parser.add_argument("--force", action="store_true", help="Force local reload")

    list_parser = commands.add_parser("list", help="List locally discovered ALS-RS extensions")
    _add_common_flags(list_parser)

    return parser.parse_args(argv)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", dest="json_output", action="store_true", help="Print JSON output")
    parser.add_argument("--host", default=os.environ.get("ALS_RS_HOST", bootstrap.DEFAULT_HOST))
    parser.add_argument("--port", default=os.environ.get("ALS_RS_PORT", bootstrap.DEFAULT_PORT))
    parser.add_argument("--data-dir", default=os.environ.get("ALS_RS_DATA_DIR"))
    parser.add_argument("--cache-dir", default=os.environ.get("ALS_RS_CACHE_DIR"))
    parser.add_argument("--config-dir", default=os.environ.get("ALS_RS_CONFIG_DIR"))
    parser.add_argument("--static-dir", default=os.environ.get("ALS_RS_STATIC_DIR"))
    parser.add_argument(
        "--no-notify-server",
        action="store_true",
        help="Do not best-effort notify a running ALS-RS server after mutations",
    )
    parser.add_argument(
        "--notify-timeout",
        type=float,
        default=2.0,
        help="Seconds to wait when checking for a running ALS-RS server",
    )


def _add_source_flags(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--path", dest="source_path", help="Extension source directory")
    group.add_argument("--zip", dest="zip_path", help="Extension source zip archive")
    group.add_argument("--git", dest="repo_url", help="Extension git repository URL or local path")
    parser.add_argument("--ref", help="Optional git branch/tag/commit")


def _bootstrap_env(args: argparse.Namespace) -> dict[str, str]:
    bootstrap_args = bootstrap.BootstrapArgs(
        host=str(getattr(args, "host", bootstrap.DEFAULT_HOST) or bootstrap.DEFAULT_HOST),
        port=str(getattr(args, "port", bootstrap.DEFAULT_PORT) or bootstrap.DEFAULT_PORT),
        data_dir=_optional_str(getattr(args, "data_dir", None)),
        cache_dir=_optional_str(getattr(args, "cache_dir", None)),
        config_dir=_optional_str(getattr(args, "config_dir", None)),
        static_dir=_optional_str(getattr(args, "static_dir", None)),
        server_bin=None,
        cargo_manifest=None,
        framework_shells_base_dir=os.environ.get("FRAMEWORK_SHELLS_BASE_DIR"),
        framework_shells_secret=os.environ.get("FRAMEWORK_SHELLS_SECRET"),
        framework_shells_repo_fingerprint=os.environ.get("FRAMEWORK_SHELLS_REPO_FINGERPRINT"),
        framework_shells_secret_fingerprint=os.environ.get("FRAMEWORK_SHELLS_SECRET_FINGERPRINT"),
        framework_shells_fws_socketio_server_pid=os.environ.get("FRAMEWORK_SHELLS_FWS_SOCKETIO_SERVER_PID"),
        framework_shells_run_id=os.environ.get("FRAMEWORK_SHELLS_RUN_ID"),
    )
    env = bootstrap._build_env(bootstrap_args)
    os.environ.update(env)
    return env


def _initialize_loader(env: dict[str, str]) -> None:
    user_root = Path(env["ALS_RS_DATA_DIR"]) / "extensions"
    user_root.mkdir(parents=True, exist_ok=True)
    registry_path = user_root / "extensions.json"
    if not registry_path.exists():
        registry_path.write_text(
            json.dumps({"version": "1.0", "extensions": []}, indent=2) + "\n",
            encoding="utf-8",
        )
    builtin_root = Path(env["ALS_RS_EXTENSIONS_DIR"])
    roots = [root for root in (builtin_root, user_root) if root.exists()]
    with redirect_stdout(sys.stderr):
        ext_loader.load_extensions(
            extensions_dir=roots,
            server_root=bootstrap._source_root(),
            fws_getter=_get_framework_shell_manager,
            broadcast_fn=_noop_broadcast,
            transcript_fn=_noop_transcript,
            meta_fns={"load": _load_meta, "save": _save_meta},
        )


async def _get_framework_shell_manager() -> object:
    module = __import__("framework_shells")
    get_manager = getattr(module, "get_manager", None)
    if not callable(get_manager):
        raise RuntimeError("framework_shells.get_manager is unavailable")
    result = get_manager(run_id=os.environ.get("FRAMEWORK_SHELLS_RUN_ID", "app-server"))
    if hasattr(result, "__await__"):
        return await cast(Awaitable[object], result)
    return result


async def _noop_broadcast(_payload: ObjectMap) -> None:
    return None


async def _noop_transcript(_conversation_id: str, _entry: ObjectMap) -> None:
    return None


_META: dict[str, ObjectMap] = {}


def _load_meta(conversation_id: str) -> ObjectMap:
    return _META.setdefault(conversation_id, {"conversation_id": conversation_id, "settings": {}})


def _save_meta(conversation_id: str, meta: ObjectMap) -> None:
    _META[conversation_id] = dict(meta)


def _run_command(args: argparse.Namespace) -> ObjectMap:
    command = str(getattr(args, "extension_command", "") or "").strip().lower()
    if command == "validate":
        payload = _source_payload_from_args(args, allow_missing_source=False)
        return coerce_object_map(
            ext_loader.validate_extension_source(
                source_type=str(payload["source_type"]),
                source_path=_optional_str(payload.get("source_path")),
                repo_url=_optional_str(payload.get("repo_url")),
                ref=_optional_str(payload.get("ref")),
                extension_id=_optional_str(payload.get("extension_id")),
            )
        )
    if command == "install":
        payload = _source_payload_from_args(args, allow_missing_source=False)
        result = coerce_object_map(
            ext_loader.install_extension_source(
                source_type=str(payload["source_type"]),
                source_path=_optional_str(payload.get("source_path")),
                repo_url=_optional_str(payload.get("repo_url")),
                ref=_optional_str(payload.get("ref")),
                extension_id=_optional_str(payload.get("extension_id")),
                allow_override=bool(getattr(args, "allow_override", False)),
            )
        )
        return _finalize_local_mutation(result, _result_extension_id(result, payload), args)
    if command == "update":
        payload = _source_payload_from_args(args, allow_missing_source=True)
        extension_id = _required_attr(args, "extension_id")
        result = coerce_object_map(
            ext_loader.update_extension_source(
                extension_id,
                source_type=_optional_str(payload.get("source_type")),
                source_path=_optional_str(payload.get("source_path")),
                repo_url=_optional_str(payload.get("repo_url")),
                ref=_optional_str(payload.get("ref")),
            )
        )
        return _finalize_local_mutation(result, extension_id, args)
    if command == "remove":
        extension_id = _required_attr(args, "extension_id")
        result = coerce_object_map(ext_loader.remove_user_extension(extension_id))
        if result.get("ok"):
            with redirect_stdout(sys.stderr):
                ext_loader.reload_extensions([extension_id], force=True)
        return {"ok": bool(result.get("ok")), "result": result, "extensions": ext_loader.list_extensions()}
    if command == "reload":
        extension_ids = [
            item
            for item in getattr(args, "extension_ids", [])
            if isinstance(item, str) and item.strip()
        ]
        with redirect_stdout(sys.stderr):
            ext_loader.reload_extensions(extension_ids or None, force=bool(getattr(args, "force", False)))
        return {"ok": True, "extensions": ext_loader.list_extensions()}
    if command == "list":
        return {"ok": True, "extensions": ext_loader.list_extensions()}
    raise ValueError(f"Unknown extension command: {command}")


def _finalize_local_mutation(result: ObjectMap, extension_id: str, args: argparse.Namespace) -> ObjectMap:
    if not result.get("ok"):
        return {"ok": False, "result": result}
    changed_ids = [extension_id] if extension_id else None
    with redirect_stdout(sys.stderr):
        ext_loader.reload_extensions(changed_ids, force=True)
    dependency_install: ObjectMap | None = None
    if bool(getattr(args, "install_dependencies", False)) and extension_id:
        if ext_loader.supports_dependency_install(extension_id):
            dependency_install = coerce_object_map(
                asyncio.run(ext_loader.install_extension_dependencies(extension_id))
            )
        else:
            dependency_install = {
                "ok": True,
                "status": "skipped",
                "message": f"{extension_id} does not declare dependency installation",
            }
    return {
        "ok": result.get("ok") is True and (dependency_install is None or dependency_install.get("ok") is True),
        "result": result,
        "dependency_install": dependency_install,
        "extension": ext_loader.get_extension_info(extension_id) if extension_id else None,
        "extensions": ext_loader.list_extensions(),
    }


def _should_notify_server(args: argparse.Namespace, body: ObjectMap) -> bool:
    if bool(getattr(args, "no_notify_server", False)) or body.get("ok") is not True:
        return False
    return str(getattr(args, "extension_command", "") or "") in {"install", "update", "remove", "reload"}


def _notify_running_server(args: argparse.Namespace) -> ObjectMap:
    timeout = max(0.1, float(getattr(args, "notify_timeout", 2.0) or 2.0))
    try:
        import socketio
    except ImportError as exc:
        return {"ok": False, "notified": False, "warning": f"python-socketio unavailable: {exc}"}

    client = socketio.Client(reconnection=False, request_timeout=timeout)
    url = f"http://{getattr(args, 'host', bootstrap.DEFAULT_HOST)}:{getattr(args, 'port', bootstrap.DEFAULT_PORT)}"
    try:
        client.connect(
            url,
            namespaces=[SETTINGS_RPC_NAMESPACE],
            transports=["websocket"],
            wait_timeout=timeout,
        )
        response = client.call(
            RPC_EVENT,
            {
                "jsonrpc": JSONRPC_VERSION,
                "id": f"als-rs-cli-{uuid.uuid4().hex}",
                "method": "extensions.reload",
                "params": {},
            },
            namespace=SETTINGS_RPC_NAMESPACE,
            timeout=timeout,
        )
        error = _rpc_error(response)
        if error:
            return {"ok": False, "notified": False, "warning": error}
        return {"ok": True, "notified": True, "result": _rpc_result(response)}
    except Exception as exc:
        return {"ok": False, "notified": False, "warning": f"server reload notification skipped: {exc}"}
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def _rpc_error(response: object) -> str | None:
    if not isinstance(response, dict):
        return "invalid RPC response"
    raw_error = response.get("error")
    if not isinstance(raw_error, dict):
        return None
    message = raw_error.get("message")
    return message if isinstance(message, str) and message.strip() else "RPC error"


def _rpc_result(response: object) -> object:
    return response.get("result") if isinstance(response, dict) else None


def _source_payload_from_args(args: argparse.Namespace, *, allow_missing_source: bool) -> ObjectMap:
    payload: ObjectMap = {}
    source_path = getattr(args, "source_path", None)
    zip_path = getattr(args, "zip_path", None)
    repo_url = getattr(args, "repo_url", None)
    if isinstance(source_path, str) and source_path.strip():
        payload["source_type"] = "path"
        payload["source_path"] = source_path.strip()
    elif isinstance(zip_path, str) and zip_path.strip():
        payload["source_type"] = "zip"
        payload["source_path"] = zip_path.strip()
    elif isinstance(repo_url, str) and repo_url.strip():
        payload["source_type"] = "git"
        payload["repo_url"] = repo_url.strip()
    elif not allow_missing_source:
        raise ValueError("One of --path, --zip, or --git is required")
    ref = getattr(args, "ref", None)
    if isinstance(ref, str) and ref.strip():
        payload["ref"] = ref.strip()
    extension_id = getattr(args, "extension_id", None)
    if isinstance(extension_id, str) and extension_id.strip():
        payload["extension_id"] = extension_id.strip()
    return payload


def _result_extension_id(result: ObjectMap, payload: ObjectMap) -> str:
    result_id = result.get("extension_id")
    if isinstance(result_id, str) and result_id.strip():
        return result_id.strip()
    payload_id = payload.get("extension_id")
    if isinstance(payload_id, str) and payload_id.strip():
        return payload_id.strip()
    return ""


def _required_attr(args: argparse.Namespace, attr_name: str) -> str:
    value = getattr(args, attr_name, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"{attr_name} is required")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _print_error(json_output: bool, body: ObjectMap) -> None:
    if json_output:
        print(json.dumps(body, indent=2, sort_keys=True))
        return
    message = body.get("message")
    print(message if isinstance(message, str) and message.strip() else "Extension command failed", file=sys.stderr)


def _print_human_result(command: str, body: ObjectMap) -> None:
    result = coerce_object_map(body.get("result")) if isinstance(body.get("result"), dict) else body
    status = result.get("status")
    print(f"status: {status}" if isinstance(status, str) and status else f"command: {command}")
    extension = coerce_object_map(body.get("extension"))
    for key in ("extension_id", "name", "type", "version", "path", "target_dir"):
        value = result.get(key)
        if value in (None, "") and extension:
            value = extension.get(key)
        if value not in (None, ""):
            print(f"{key}: {value}")
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        print(f"message: {message.strip()}")
    dependency_install = coerce_object_map(body.get("dependency_install"))
    if dependency_install:
        print(f"dependency_install: {dependency_install.get('status') or dependency_install.get('ok')}")
    server_notify = coerce_object_map(body.get("server_notify"))
    if server_notify:
        if server_notify.get("notified") is True:
            print("server_notify: reloaded")
        else:
            warning = server_notify.get("warning")
            print(f"server_notify: {warning or 'skipped'}")
    if command in {"list", "reload"} and isinstance(body.get("extensions"), list):
        print(f"extensions: {len(cast(list[Any], body['extensions']))}")
