import argparse
import asyncio
import json
import sys
from typing import Any

import extensions as ext_loader


def register_extension_subcommands(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "extension",
        help="Manage extension packages from the CLI",
    )
    parser.set_defaults(command="extension")

    extension_commands = parser.add_subparsers(dest="extension_command", required=True)

    validate_parser = extension_commands.add_parser("validate", help="Validate an extension package source")
    _add_common_flags(validate_parser)
    _add_source_flags(validate_parser, required=True)
    validate_parser.add_argument("--id", dest="extension_id", help="Expected extension id")

    install_parser = extension_commands.add_parser("install", help="Install an extension package")
    _add_common_flags(install_parser)
    _add_source_flags(install_parser, required=True)
    install_parser.add_argument("--id", dest="extension_id", help="Expected extension id")
    install_parser.add_argument("--allow-override", action="store_true", help="Allow overriding a builtin id")
    install_parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="Run extension dependency install after package install if supported",
    )
    install_parser.add_argument(
        "--force-reload",
        action="store_true",
        help="Force a broader local reload after install",
    )

    update_parser = extension_commands.add_parser("update", help="Update an installed extension")
    _add_common_flags(update_parser)
    update_parser.add_argument("extension_id", help="Installed extension id")
    _add_source_flags(update_parser, required=False)
    update_parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="Run extension dependency install after package update if supported",
    )
    update_parser.add_argument(
        "--force-reload",
        action="store_true",
        help="Force a broader local reload after update",
    )

    remove_parser = extension_commands.add_parser("remove", help="Remove an installed user extension")
    _add_common_flags(remove_parser)
    remove_parser.add_argument("extension_id", help="Installed extension id")
    remove_parser.add_argument(
        "--force-reload",
        action="store_true",
        help="Force a broader local reload after removal",
    )

    reload_parser = extension_commands.add_parser("reload", help="Reload local extension discovery/runtime state")
    _add_common_flags(reload_parser)
    reload_parser.add_argument("extension_ids", nargs="*", help="Optional extension ids to target")
    reload_parser.add_argument(
        "--force-reload",
        action="store_true",
        help="Force a broader local reload",
    )


def run_extension_command(args: argparse.Namespace) -> int:
    json_output = bool(getattr(args, "json_output", False))
    try:
        body = _run_extension_command_inner(args)
    except ValueError as exc:
        _print_error(json_output, {"ok": False, "message": str(exc)})
        return 2
    except Exception as exc:
        _print_error(json_output, {"ok": False, "message": str(exc)})
        return 1

    if json_output:
        print(json.dumps(body, indent=2, sort_keys=True))
    else:
        _print_human_result(str(getattr(args, "extension_command", "") or ""), body)
    return 0 if _response_ok(body) else 1


def _run_extension_command_inner(args: argparse.Namespace) -> dict[str, Any]:
    command = str(getattr(args, "extension_command", "") or "").strip().lower()
    if command == "validate":
        payload = _source_payload_from_args(args, allow_missing_source=False)
        return ext_loader.validate_extension_source(
            source_type=str(payload["source_type"]),
            source_path=_optional_str(payload.get("source_path")),
            repo_url=_optional_str(payload.get("repo_url")),
            ref=_optional_str(payload.get("ref")),
            extension_id=_optional_str(payload.get("extension_id")),
        )

    if command == "install":
        payload = _source_payload_from_args(args, allow_missing_source=False)
        result = ext_loader.install_extension_source(
            source_type=str(payload["source_type"]),
            source_path=_optional_str(payload.get("source_path")),
            repo_url=_optional_str(payload.get("repo_url")),
            ref=_optional_str(payload.get("ref")),
            extension_id=_optional_str(payload.get("extension_id")),
            allow_override=bool(getattr(args, "allow_override", False)),
        )
        return _finalize_mutating_result(
            result=result,
            extension_id=_result_extension_id(result, payload),
            install_dependencies=bool(getattr(args, "install_dependencies", False)),
            force_reload=bool(getattr(args, "force_reload", False)),
        )

    if command == "update":
        payload = _source_payload_from_args(args, allow_missing_source=True)
        result = ext_loader.update_extension_source(
            args.extension_id,
            source_type=_optional_str(payload.get("source_type")),
            source_path=_optional_str(payload.get("source_path")),
            repo_url=_optional_str(payload.get("repo_url")),
            ref=_optional_str(payload.get("ref")),
        )
        return _finalize_mutating_result(
            result=result,
            extension_id=str(args.extension_id),
            install_dependencies=bool(getattr(args, "install_dependencies", False)),
            force_reload=bool(getattr(args, "force_reload", False)),
        )

    if command == "remove":
        result = ext_loader.remove_user_extension(str(args.extension_id))
        if result.get("ok"):
            ext_loader.reload_extensions([str(args.extension_id)], force=bool(getattr(args, "force_reload", False)))
        return {
            "ok": bool(result.get("ok")),
            "result": result,
            "extensions": ext_loader.list_extensions(),
        }

    if command == "reload":
        extension_ids = [
            ext_id.strip()
            for ext_id in getattr(args, "extension_ids", [])
            if isinstance(ext_id, str) and ext_id.strip()
        ]
        ext_loader.reload_extensions(extension_ids or None, force=bool(getattr(args, "force_reload", False)))
        return {
            "ok": True,
            "extensions": ext_loader.list_extensions(),
        }

    raise ValueError(f"Unknown extension command: {command}")


def _finalize_mutating_result(
    *,
    result: dict[str, Any],
    extension_id: str,
    install_dependencies: bool,
    force_reload: bool,
) -> dict[str, Any]:
    if not result.get("ok"):
        return {"ok": False, "result": result}

    changed_ids = [extension_id] if extension_id else None
    ext_loader.reload_extensions(changed_ids, force=force_reload)

    dependency_result = None
    if install_dependencies and extension_id and ext_loader.supports_dependency_install(extension_id):
        dependency_result = asyncio.run(ext_loader.install_extension_dependencies(extension_id))
    ok = bool(result.get("ok")) and (
        dependency_result is None or bool(dependency_result.get("ok"))
    )
    extension = ext_loader.get_extension_info(extension_id) if extension_id else None
    return {
        "ok": ok,
        "result": result,
        "dependency_install": dependency_result,
        "extension": extension,
    }


def _result_extension_id(result: dict[str, Any], payload: dict[str, object]) -> str:
    result_id = result.get("extension_id")
    if isinstance(result_id, str) and result_id.strip():
        return result_id.strip()
    payload_id = payload.get("extension_id")
    if isinstance(payload_id, str) and payload_id.strip():
        return payload_id.strip()
    return ""


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Print the raw JSON response",
    )


def _add_source_flags(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--path", dest="source_path", help="Extension source directory")
    group.add_argument("--zip", dest="zip_path", help="Extension source zip archive")
    group.add_argument("--git", dest="repo_url", help="Extension git repository URL or local path")
    parser.add_argument("--ref", help="Optional git branch/tag/commit")


def _source_payload_from_args(
    args: argparse.Namespace,
    *,
    allow_missing_source: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {}
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


def _response_ok(body: dict[str, Any]) -> bool:
    value = body.get("ok")
    return value is True


def _print_error(json_output: bool, body: dict[str, Any]) -> None:
    if json_output:
        print(json.dumps(body, indent=2, sort_keys=True))
        return
    message = body.get("message")
    if isinstance(message, str) and message.strip():
        print(message.strip(), file=sys.stderr)
        return
    print("Extension command failed", file=sys.stderr)


def _print_human_result(command: str, body: dict[str, Any]) -> None:
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    extension = body.get("extension") if isinstance(body.get("extension"), dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if isinstance(status, str) and status.strip():
        print(f"status: {status.strip()}")
    else:
        print(f"command: {command}")

    for key in ("extension_id", "name", "type", "version", "path", "target_dir"):
        value = result.get(key) if isinstance(result, dict) else None
        if value in (None, "") and isinstance(extension, dict):
            value = extension.get(key)
        if value not in (None, ""):
            print(f"{key}: {value}")

    message = result.get("message") if isinstance(result, dict) else None
    if isinstance(message, str) and message.strip():
        print(f"message: {message.strip()}")

    warnings = result.get("warnings") if isinstance(result, dict) else None
    if isinstance(warnings, list) and warnings:
        print("warnings:")
        for item in warnings:
            print(f"  - {item}")

    errors = result.get("errors") if isinstance(result, dict) else None
    if isinstance(errors, list) and errors:
        print("errors:")
        for item in errors:
            print(f"  - {item}")

    dependency_install = body.get("dependency_install")
    if isinstance(dependency_install, dict):
        dep_status = (
            dependency_install.get("status")
            or dependency_install.get("message")
            or dependency_install.get("ok")
        )
        print(f"dependency_install: {dep_status}")

    if command == "reload":
        extensions = body.get("extensions")
        if isinstance(extensions, list):
            print(f"extensions: {len(extensions)}")
