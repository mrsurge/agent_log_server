from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Te2SyncHelpers:
    package_root: Path
    config_path: Path

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
