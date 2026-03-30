import asyncio
import importlib
import os
import platform
import shutil
from typing import Any, Dict, Optional


def _codex_binary_path() -> Optional[str]:
    return shutil.which("codex")


def is_android_termux() -> bool:
    system_info = platform.platform()
    uname = platform.uname()
    prefix = os.environ.get("PREFIX", "")
    markers = " ".join(
        (
            system_info,
            getattr(uname, "system", ""),
            getattr(uname, "release", ""),
            prefix,
        )
    ).lower()
    return "android" in markers or "termux" in markers or "com.termux" in markers


def recommended_codex_package() -> str:
    return "@mmmbuto/codex-cli-termux" if is_android_termux() else "@openai/codex"


def recommended_codex_install_command() -> str:
    return f"npm install -g {recommended_codex_package()}"


async def _run_install(package_name: str) -> Dict[str, Any]:
    npm = shutil.which("npm")
    if not npm:
        return {"ok": False, "status": "failed", "message": "npm not found on PATH"}
    proc = await asyncio.create_subprocess_exec(
        npm,
        "install",
        "-g",
        package_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return {
            "ok": False,
            "status": "failed",
            "message": output or f"npm install exited with {proc.returncode}",
        }
    return {"ok": True, "status": "succeeded", "message": output or "Codex CLI installed"}


async def check_dependencies(*, extension_id: str, extension_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    binary = _codex_binary_path()
    if not binary:
        return {
            "ok": False,
            "status": "unmet",
            "message": f"codex not found on PATH. Install with: {recommended_codex_install_command()}",
            "details": {"install_command": recommended_codex_install_command()},
        }

    ext_loader = importlib.import_module("extensions")
    handler = ext_loader.get_handler(extension_id)
    auth_reader = getattr(handler, "get_auth_status", None) if handler else None
    if not callable(auth_reader):
        return {
            "ok": False,
            "status": "error",
            "message": "Codex auth status helper unavailable",
            "details": {"binary": binary},
        }

    auth_status = auth_reader(extension_id=extension_id, refresh=False)
    if asyncio.iscoroutine(auth_status):
        auth_status = await auth_status
    auth_payload = auth_status if isinstance(auth_status, dict) else {}
    details = {
        "binary": binary,
        "auth_required": bool(auth_payload.get("requires_openai_auth")),
        "authenticated": bool(auth_payload.get("authenticated")),
        "login_pending": bool(auth_payload.get("login_pending")),
        "account_type": auth_payload.get("account_type"),
        "account_email": auth_payload.get("account_email"),
        "plan_type": auth_payload.get("plan_type"),
    }

    if auth_payload.get("ok") is False:
        return {
            "ok": False,
            "status": "error",
            "message": auth_payload.get("message") or "Failed to read Codex auth status",
            "details": details,
        }

    if auth_payload.get("requires_openai_auth") and not auth_payload.get("authenticated"):
        return {
            "ok": False,
            "status": "unmet",
            "message": auth_payload.get("message") or "OpenAI auth required",
            "details": details,
        }

    return {
        "ok": True,
        "status": "met",
        "message": auth_payload.get("message") or f"codex available at {binary}",
        "details": details,
    }


async def install_dependencies(*, extension_id: str, extension_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = await _run_install(recommended_codex_package())
    if not result.get("ok"):
        return result
    return await check_dependencies(extension_id=extension_id, extension_info=extension_info)
