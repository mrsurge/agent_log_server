from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast


def _load_source_impl() -> ModuleType:
    impl_path = Path(__file__).parent.parent / "mcp_agent_pty_http_shim.py"
    if not impl_path.is_file():
        raise RuntimeError(f"agent-pty HTTP shim implementation not found at {impl_path}")
    spec = importlib.util.spec_from_file_location("_agent_log_server_mcp_agent_pty_http_shim_impl", impl_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load agent-pty HTTP shim implementation from {impl_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_impl = _load_source_impl()
app = cast(object, getattr(_impl, "app"))
main = cast(Callable[[], None], getattr(_impl, "main"))


if __name__ == "__main__":
    main()
