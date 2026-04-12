from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from agent_log_server.te2_mcp_config import (
    TE2_MCP_SERVER_NAME,
    build_te2_mcp_streamable_http_url,
)


@dataclass(frozen=True)
class Te2SyncHelpers:
    package_root: Path
    config_path: Path
    codex_config_path: Path
    te2_base_url: Callable[[], str]
    load_appserver_config: Callable[[], dict[str, Any]]

    @property
    def cache_dir(self) -> Path:
        return self.config_path.parent

    @property
    def te2_console_bridge_source_path(self) -> Path:
        return self.package_root / "te2_assets" / "console_bridge.js"

    @property
    def te2_console_bridge_cache_path(self) -> Path:
        return self.cache_dir / "te2_console_bridge.js"

    @property
    def te2_fws_readme_source_path(self) -> Path:
        return self.package_root / "te2_assets" / "framework_shells_README.md"

    @property
    def te2_fws_readme_cache_path(self) -> Path:
        return self.cache_dir / "framework_shells_README.md"

    @property
    def te2_proxy_shell_readme_source_path(self) -> Path:
        return self.package_root / "te2_assets" / "proxy_shell_wrapper_README.md"

    @property
    def te2_proxy_shell_readme_cache_path(self) -> Path:
        return self.cache_dir / "proxy_shell_wrapper_README.md"

    def sync_cached_asset(self, label: str, source_path: Path, cache_path: Path) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            print(f"[Startup] {label} source missing: {source_path}")
            return
        try:
            source_stat = source_path.stat()
            target_stat = cache_path.stat() if cache_path.exists() else None
            if (
                target_stat is not None
                and target_stat.st_size == source_stat.st_size
                and int(target_stat.st_mtime) >= int(source_stat.st_mtime)
            ):
                return
            shutil.copy2(source_path, cache_path)
        except Exception as exc:
            print(f"[Startup] {label} cache sync error: {exc}")

    def sync_te2_console_bridge_cache(self) -> None:
        self.sync_cached_asset(
            "TE2 console bridge",
            self.te2_console_bridge_source_path,
            self.te2_console_bridge_cache_path,
        )

    def sync_te2_fws_readme_cache(self) -> None:
        self.sync_cached_asset(
            "Framework-shells README",
            self.te2_fws_readme_source_path,
            self.te2_fws_readme_cache_path,
        )

    def sync_te2_proxy_shell_readme_cache(self) -> None:
        self.sync_cached_asset(
            "Proxy shell wrapper README",
            self.te2_proxy_shell_readme_source_path,
            self.te2_proxy_shell_readme_cache_path,
        )

    def write_codex_te2_mcp_config(self, enabled: bool) -> None:
        self.codex_config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.codex_config_path.exists():
            raw = self.codex_config_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw) if raw.strip() else tomlkit.document()
        else:
            doc = tomlkit.document()

        mcp_servers: Any = doc.get("mcp_servers")
        if not isinstance(mcp_servers, dict):
            mcp_servers = tomlkit.table()
            doc["mcp_servers"] = mcp_servers

        if enabled:
            te2_table = tomlkit.table()
            te2_table["url"] = build_te2_mcp_streamable_http_url(self.te2_base_url())
            mcp_servers[TE2_MCP_SERVER_NAME] = te2_table
        else:
            mcp_servers.pop(TE2_MCP_SERVER_NAME, None)
            if not list(mcp_servers):
                doc.pop("mcp_servers", None)

        self.codex_config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    def sync_codex_te2_mcp_from_app_config(self) -> None:
        cfg = self.load_appserver_config()
        self.write_codex_te2_mcp_config(cfg.get("te2_mcp_integration") is True)
