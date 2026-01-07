#!/usr/bin/env python3
import asyncio
import json
import os
import secrets
import time
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Body, HTTPException
import uvicorn

from framework_shells import get_manager as get_framework_shell_manager
from framework_shells.orchestrator import Orchestrator


def _ensure_framework_shells_secret() -> None:
    """Derive a stable secret from cwd/repo root if not already set."""
    if os.environ.get("FRAMEWORK_SHELLS_SECRET"):
        return
    repo_root = str(Path(__file__).resolve().parent)
    fingerprint = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:16]
    base_dir = Path(os.path.expanduser("~/.cache/framework_shells"))
    secret_dir = base_dir / "runtimes" / fingerprint
    secret_file = secret_dir / "secret"
    if secret_file.exists():
        secret = secret_file.read_text(encoding="utf-8").strip()
    else:
        secret_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        secret_file.write_text(secret, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except Exception:
            pass
    os.environ["FRAMEWORK_SHELLS_SECRET"] = secret
    os.environ["FRAMEWORK_SHELLS_REPO_FINGERPRINT"] = fingerprint
    os.environ["FRAMEWORK_SHELLS_BASE_DIR"] = str(base_dir)
    os.environ.setdefault("FRAMEWORK_SHELLS_RUN_ID", "app-server")


def _manager_run_id() -> str:
    return os.environ.get("FRAMEWORK_SHELLS_RUN_ID") or "app-server"


async def _get_fws_manager():
    return await get_framework_shell_manager(run_id=_manager_run_id())


def _agent_pty_root(conversation_id: str) -> Path:
    safe = "".join(ch for ch in conversation_id if ch.isalnum() or ch in ("-", "_"))
    return Path(os.path.expanduser("~/.cache/app_server/conversations")) / safe / "agent_pty"


def _rcfile_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "bashrc_agent_pty.sh"


def _marker_path(conversation_id: str) -> Path:
    return _agent_pty_root(conversation_id) / "markers.log"


def _termux_env_overrides() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if os.environ.get("PREFIX"):
        env["PATH"] = f"{os.environ.get('PREFIX')}/bin:" + os.environ.get("PATH", "")
        env["TERMUX_VERSION"] = os.environ.get("TERMUX_VERSION", "1")
        lib = f"{os.environ.get('PREFIX')}/lib/libtermux-exec.so"
        if Path(lib).exists():
            env["LD_PRELOAD"] = lib
    elif Path("/data/data/com.termux/files/usr").exists():
        env["PATH"] = "/data/data/com.termux/files/usr/bin:" + os.environ.get("PATH", "")
        env["TERMUX_VERSION"] = os.environ.get("TERMUX_VERSION", "1")
        lib = "/data/data/com.termux/files/usr/lib/libtermux-exec.so"
        if Path(lib).exists():
            env["LD_PRELOAD"] = lib
    return env


def _write_rcfile(path: Path, marker_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = r"""
# Termux guard: ensure env + shebang compatibility
if [ -n "${PREFIX:-}" ] && [ -x "${PREFIX}/bin/env" ]; then
  export PATH="${PREFIX}/bin:${PATH}"
  if [ -z "${TERMUX_VERSION:-}" ]; then
    export TERMUX_VERSION="1"
  fi
  if [ -f "${PREFIX}/lib/libtermux-exec.so" ]; then
    export LD_PRELOAD="${PREFIX}/lib/libtermux-exec.so"
  fi
elif [ -d "/data/data/com.termux/files/usr" ]; then
  export PATH="/data/data/com.termux/files/usr/bin:${PATH}"
  if [ -z "${TERMUX_VERSION:-}" ]; then
    export TERMUX_VERSION="1"
  fi
  if [ -f "/data/data/com.termux/files/usr/lib/libtermux-exec.so" ]; then
    export LD_PRELOAD="/data/data/com.termux/files/usr/lib/libtermux-exec.so"
  fi
fi

__FWS_MARKER_FILE="__FWS_MARKER_FILE_PATH__"
: > "$__FWS_MARKER_FILE"
exec 3>>"$__FWS_MARKER_FILE"

__FWS_SEQ=0
__FWS_LAST_SEQ=""
__FWS_IN_MARKER=0
__FWS_READY=0
__FWS_MANUAL="${__FWS_MANUAL:-0}"

__fws_b64() { printf %s "$1" | base64 | tr -d '\n'; }
__fws_now_ms() {
  date +%s%3N 2>/dev/null && return 0
  python - <<'PY'
import time
print(int(time.time() * 1000))
PY
}

__fws_emit_begin() {
  local cmd="$1"
  local cwd="$2"
  local ts="$3"
  local seq="$4"
  local cwd_b64="$(__fws_b64 "$cwd")"
  local cmd_b64="$(__fws_b64 "$cmd")"
  printf '\n__FWS_BLOCK_BEGIN__ seq=%s ts=%s cwd_b64=%s cmd_b64=%s\n' "$seq" "$ts" "$cwd_b64" "$cmd_b64" >&3
}

__fws_emit_end() {
  local exit_code="$1"
  local ts="$2"
  local seq="$3"
  printf '\n__FWS_BLOCK_END__ seq=%s ts=%s exit=%s\n' "$seq" "$ts" "$exit_code" >&3
}

__fws_emit_prompt() {
  local exit_code="${1:-$?}"
  local ts="$(__fws_now_ms)"
  local cwd="$(pwd -P 2>/dev/null || pwd)"
  local cwd_b64="$(__fws_b64 "$cwd")"
  printf '\n__FWS_PROMPT__ ts=%s cwd_b64=%s exit=%s\n' "$ts" "$cwd_b64" "$exit_code" >&3
}

__fws_should_ignore_cmd() {
  local cmd="$1"
  case "$cmd" in
    PS1=*|PROMPT_COMMAND=*|__FWS_READY=*|__FWS_SEQ=*|__FWS_LAST_SEQ=*|__FWS_IN_MARKER=*|trap*|shopt*|set\ +o*|set\ -o*)
      return 0
      ;;
    *__FWS_BLOCK_BEGIN__*|*__FWS_BLOCK_END__*|*__FWS_PROMPT__*) return 0 ;;
    __fws_*|__FWS_*) return 0 ;;
  esac
  return 1
}

if [ "${__FWS_MANUAL}" = "1" ]; then
  __FWS_READY=1
  __fws_manual_precmd() {
    local ec="$?"
    __fws_emit_prompt "$ec"
  }
  PROMPT_COMMAND="__fws_manual_precmd"
else
  trap '__fws_preexec' DEBUG
  __fws_preexec() {
    if [ "${__FWS_IN_MARKER}" = "1" ]; then return 0; fi
    if [ "${__FWS_READY}" != "1" ]; then return 0; fi
    case "$-" in *i*) ;; *) return 0 ;; esac
    local cmd="${BASH_COMMAND}"
    if __fws_should_ignore_cmd "$cmd"; then return 0; fi
    __FWS_IN_MARKER=1
    __FWS_SEQ=$((__FWS_SEQ + 1))
    __FWS_LAST_SEQ="$__FWS_SEQ"
    local ts="$(__fws_now_ms)"
    local cwd="$(pwd -P 2>/dev/null || pwd)"
    __fws_emit_begin "$cmd" "$cwd" "$ts" "$__FWS_SEQ"
    __FWS_IN_MARKER=0
  }

  __fws_precmd() {
    if [ "${__FWS_IN_MARKER}" = "1" ]; then return 0; fi
    if [ "${__FWS_READY}" != "1" ]; then
      __FWS_READY=1
      return 0
    fi
    local ec="$?"
    __fws_emit_end "$ec" "$(__fws_now_ms)" "$__FWS_LAST_SEQ"
    __fws_emit_prompt "$ec"
  }
  PROMPT_COMMAND="__fws_precmd"
fi
"""
    content = content.replace("__FWS_MARKER_FILE_PATH__", str(marker_path))
    path.write_text(content, encoding="utf-8")


CONFIG_PATH = Path(os.path.expanduser("~/.cache/app_server/shell_manager.json"))


def _write_registry(host: str, port: int) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "url": f"http://{host}:{port}",
        "pid": os.getpid(),
        "ts": int(time.time() * 1000),
    }
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


app = FastAPI()


@app.on_event("startup")
async def _startup() -> None:
    _ensure_framework_shells_secret()
    host = os.environ.get("SHELL_MANAGER_HOST", "127.0.0.1")
    port = int(os.environ.get("SHELL_MANAGER_PORT", "12361"))
    _write_registry(host, port)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "pid": os.getpid()}


@app.post("/shells/ensure")
async def shells_ensure(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id required")
    cwd = payload.get("cwd")
    _ensure_framework_shells_secret()
    mgr = await _get_fws_manager()
    label = f"agent-pty:{conversation_id}"
    rec = await mgr.find_shell_by_label(label, status="running")
    if not rec:
        root = _agent_pty_root(conversation_id)
        root.mkdir(parents=True, exist_ok=True)
        rcfile = _rcfile_path(conversation_id)
        marker_path = _marker_path(conversation_id)
        _write_rcfile(rcfile, marker_path)
        ctx = {
            "PROJECT_ROOT": str(Path(__file__).resolve().parent),
            "CONVERSATION_ID": conversation_id,
            "RCFILE": str(rcfile),
            "CWD": cwd or str(Path.cwd()),
        }
        env_overrides = {"__FWS_MANUAL": "1", **_termux_env_overrides()}
        spec_ref = "shellspec/mcp_agent_pty.yaml#agent_pty_shell"
        rec = await Orchestrator(mgr).start_from_ref(
            spec_ref,
            base_dir=Path(__file__).resolve().parent,
            ctx=ctx,
            label=label,
            env_overrides=env_overrides,
            wait_ready=True,
        )
    return {"ok": True, "shell_id": rec.id, "label": rec.label, "status": rec.status}


def _main() -> None:
    host = os.environ.get("SHELL_MANAGER_HOST", "127.0.0.1")
    port = int(os.environ.get("SHELL_MANAGER_PORT", "12361"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    _main()
