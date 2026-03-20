import asyncio
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
    if binary:
        return {
            "ok": True,
            "status": "met",
            "message": f"codex available at {binary}",
            "details": {"binary": binary},
        }
    return {
        "ok": False,
        "status": "unmet",
        "message": f"codex not found on PATH. Install with: {recommended_codex_install_command()}",
        "details": {"install_command": recommended_codex_install_command()},
    }


async def install_dependencies(*, extension_id: str, extension_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = await _run_install(recommended_codex_package())
    if not result.get("ok"):
        return result
    return await check_dependencies(extension_id=extension_id, extension_info=extension_info)
