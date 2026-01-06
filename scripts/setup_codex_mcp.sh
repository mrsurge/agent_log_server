#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P)
NAME="agent-pty-blocks"
SERVER_PATH="$REPO_ROOT/mcp_agent_pty_server.py"
HOST="${MCP_HOST:-127.0.0.1}"
PORT="${MCP_PORT:-12360}"
PATH_SUFFIX="${MCP_PATH:-/mcp}"

FWS_SECRET=""
if command -v python >/dev/null 2>&1; then
  FWS_SECRET=$(python - <<'PY'
import hashlib
from pathlib import Path
repo_root = Path.cwd().resolve()
fingerprint = hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:16]
secret_file = Path.home()/".cache"/"framework_shells"/"runtimes"/fingerprint/"secret"
print(secret_file.read_text().strip() if secret_file.exists() else "")
PY
)
fi

if [ ! -f "$SERVER_PATH" ]; then
  echo "Error: mcp_agent_pty_server.py not found at $SERVER_PATH" >&2
  exit 1
fi

NAME="$NAME" REPO_ROOT="$REPO_ROOT" SERVER_PATH="$SERVER_PATH" FWS_SECRET="$FWS_SECRET" HOST="$HOST" PORT="$PORT" PATH_SUFFIX="$PATH_SUFFIX" python - <<'PY'
import os
import re
from pathlib import Path

name = os.environ["NAME"]
repo_root = os.environ["REPO_ROOT"]
server_path = os.environ["SERVER_PATH"]
fws_secret = os.environ.get("FWS_SECRET", "")

cfg_path = Path(os.path.expanduser("~/.codex/config.toml"))
cfg_path.parent.mkdir(parents=True, exist_ok=True)

if cfg_path.exists():
    original = cfg_path.read_text(encoding="utf-8", errors="replace")
else:
    original = ""

lines = original.splitlines()
target = f"mcp_servers.{name}"
out = []
in_target = False

section_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")

for line in lines:
    m = section_re.match(line)
    if m:
        sect = m.group(1).strip()
        if sect == target:
            in_target = True
            continue
        if in_target:
            in_target = False
        out.append(line)
        continue
    if in_target:
        continue
    out.append(line)

if out and out[-1].strip() != "":
    out.append("")

def toml_str(val: str) -> str:
    return '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'

out.append(f"[{target}]")
url = f"http://{os.environ.get('HOST')}:{os.environ.get('PORT')}{os.environ.get('PATH_SUFFIX')}"
out.append(f"url = {toml_str(url)}")
out.append("")

new_content = "\n".join(out)

if new_content != original:
    cfg_path.write_text(new_content, encoding="utf-8")
    print(f"Configured MCP server '{name}' in {cfg_path}")
else:
    print(f"MCP server '{name}' already configured in {cfg_path}")
PY
