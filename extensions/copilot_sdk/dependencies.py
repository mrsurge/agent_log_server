import asyncio
import shutil
import sys
from typing import Optional, TypeAlias

PayloadMap: TypeAlias = dict[str, object]

WRAPPER_REPO_URL = "git+https://github.com/XrSurge/copilot_runtime_manager.git"


def _copilot_binary_path() -> Optional[str]:
    return shutil.which("copilot")


def _manager_binary_path() -> Optional[str]:
    return shutil.which("copilot-runtime-manager")


async def _run(*args: str) -> PayloadMap:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "message": output,
    }


async def check_dependencies(*, extension_id: str, extension_info: Optional[PayloadMap] = None) -> PayloadMap:
    binary = _copilot_binary_path()
    if binary:
        return {
            "ok": True,
            "status": "met",
            "message": f"copilot available at {binary}",
            "details": {"binary": binary},
        }
    manager = _manager_binary_path()
    if manager:
        return {
            "ok": False,
            "status": "unmet",
            "message": "copilot not found on PATH. Managed installer is available.",
            "details": {"manager": manager},
        }
    return {
        "ok": False,
        "status": "unmet",
        "message": "copilot not found on PATH. Managed installer is not installed.",
        "details": {"install_repo": WRAPPER_REPO_URL},
    }


async def install_dependencies(*, extension_id: str, extension_info: Optional[PayloadMap] = None) -> PayloadMap:
    manager = _manager_binary_path()
    if not manager:
        pip_result = await _run(sys.executable, "-m", "pip", "install", "--upgrade", WRAPPER_REPO_URL)
        if not pip_result.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "message": pip_result.get("message") or "Failed to install copilot-runtime-manager",
            }
        manager = _manager_binary_path()
        if not manager:
            return {
                "ok": False,
                "status": "failed",
                "message": "copilot-runtime-manager did not appear on PATH after pip install",
            }

    install_result = await _run(manager, "install")
    if not install_result.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "message": install_result.get("message") or "Managed Copilot install failed",
        }

    activate_result = await _run(manager, "activate")
    if not activate_result.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "message": activate_result.get("message") or "Managed Copilot activation failed",
        }

    checked = await check_dependencies(extension_id=extension_id, extension_info=extension_info)
    details = checked.get("details")
    detail_map = details if isinstance(details, dict) else {}
    if checked.get("ok"):
        return {
            "ok": True,
            "status": "succeeded",
            "message": checked.get("message") or "Copilot installed",
            "details": detail_map,
        }
    return {
        "ok": False,
        "status": "failed",
        "message": checked.get("message") or "Copilot install verification failed",
        "details": detail_map,
    }
