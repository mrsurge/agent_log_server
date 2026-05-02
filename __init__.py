"""Compatibility package for launching console scripts from the parent dir.

When the current working directory is ``~``, Python sees ``~/agent_log_server``
before the editable-installed package and can resolve ``agent_log_server.server``
to the repo-root ``server.py`` launcher. Put the real package directory first so
console entrypoints still import ``agent_log_server.server`` from the package.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_INNER_PACKAGE = _ROOT / "agent_log_server"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

__path__ = [str(_INNER_PACKAGE), str(_ROOT)]

