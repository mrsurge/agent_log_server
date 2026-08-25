from __future__ import annotations

# pyright: reportMissingImports=false, reportMissingModuleSource=false
# pyright: reportUnknownVariableType=false, reportUntypedBaseClass=false
# pyright: reportUnknownMemberType=false

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import cast
import tomllib

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py as _build_py
try:
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
except ImportError:  # pragma: no cover - wheel is a declared build dependency
    _bdist_wheel = None


BINARY_SOURCE_ENV = "ALS_RS_PACKAGED_SERVER_BIN"
BINARY_TARGET_ENV = "ALS_RS_PACKAGED_SERVER_TARGET"
BINARY_PLATFORM_TAG_ENV = "ALS_RS_PACKAGED_SERVER_PLATFORM_TAG"
BINARY_SOURCE_COMMIT_ENV = "ALS_RS_PACKAGED_SERVER_SOURCE_COMMIT"
BINARY_SOURCE_DIRTY_ENV = "ALS_RS_PACKAGED_SERVER_SOURCE_DIRTY"
BINARY_NAME = "als-server"
MANIFEST_NAME = "als-server.manifest.json"


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        self._copy_developer_message_template()
        if _packaged_binary_source() is not None:
            self._copy_binary_release()
        else:
            self._copy_rust_workspace()

    def _copy_developer_message_template(self) -> None:
        source_root = Path(__file__).parent
        template_source = source_root / "DEVELOPER_MESSAGE_TEMPLATE.md"
        if not template_source.is_file():
            return

        build_lib = cast(str, self.build_lib)
        package_target = Path(build_lib) / "agent_log_server_rs" / "DEVELOPER_MESSAGE_TEMPLATE.md"
        package_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_source, package_target)

    def _copy_rust_workspace(self) -> None:
        source_root = Path(__file__).parent
        rust_source = source_root / "rust"
        if not (rust_source / "Cargo.toml").is_file():
            return

        build_lib = cast(str, self.build_lib)
        package_target = Path(build_lib) / "agent_log_server_rs" / "rust"
        if package_target.exists():
            shutil.rmtree(package_target)
        shutil.copytree(
            rust_source,
            package_target,
            ignore=shutil.ignore_patterns(
                "target",
                ".git",
                "__pycache__",
                "*.pyc",
            ),
        )

    def _copy_binary_release(self) -> None:
        source_root = Path(__file__).parent
        binary_source = _packaged_binary_source()
        if binary_source is None:
            raise RuntimeError(f"{BINARY_SOURCE_ENV} is required for a binary-release wheel")
        target = _required_env(BINARY_TARGET_ENV)
        platform_tag = _required_env(BINARY_PLATFORM_TAG_ENV)
        source_commit = _required_env(BINARY_SOURCE_COMMIT_ENV)
        source_dirty = _parse_bool_env(BINARY_SOURCE_DIRTY_ENV)

        build_lib = Path(cast(str, self.build_lib))
        package_root = build_lib / "agent_log_server_rs"
        rust_target = package_root / "rust"
        if rust_target.exists():
            shutil.rmtree(rust_target)
        static_source = source_root / "rust" / "crates" / "als-server" / "src" / "static"
        static_target = rust_target / "crates" / "als-server" / "src" / "static"
        shutil.copytree(
            static_source,
            static_target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        bin_target = package_root / "bin"
        bin_target.mkdir(parents=True, exist_ok=True)
        packaged_binary = bin_target / BINARY_NAME
        shutil.copy2(binary_source, packaged_binary)
        packaged_binary.chmod(packaged_binary.stat().st_mode | 0o755)
        manifest = {
            "schema": 1,
            "package_version": _package_version(source_root),
            "binary": BINARY_NAME,
            "target": target,
            "platform_tag": platform_tag,
            "source_commit": source_commit,
            "source_dirty": source_dirty,
            "sha256": _sha256_file(packaged_binary),
        }
        (bin_target / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _packaged_binary_source() -> Path | None:
    raw = os.environ.get(BINARY_SOURCE_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Packaged ALS-RS server binary does not exist: {path}")
    return path


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for a binary-release wheel")
    return value


def _parse_bool_env(name: str) -> bool:
    value = _required_env(name).lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def _package_version(source_root: Path) -> str:
    pyproject = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    return cast(str, pyproject["project"]["version"])


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


class BinaryDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return _packaged_binary_source() is not None


cmdclass: dict[str, object] = {"build_py": build_py}


if _bdist_wheel is not None:

    class bdist_wheel(_bdist_wheel):
        def finalize_options(self) -> None:
            super().finalize_options()
            if _packaged_binary_source() is not None:
                self.root_is_pure = False

        def get_tag(self) -> tuple[str, str, str]:
            python_tag, abi_tag, platform_tag = super().get_tag()
            if _packaged_binary_source() is None:
                return python_tag, abi_tag, platform_tag
            resolved_platform = _required_env(BINARY_PLATFORM_TAG_ENV).replace("-", "_").replace(".", "_")
            return ("py3", "none", resolved_platform)


    cmdclass["bdist_wheel"] = bdist_wheel


setup(
    cmdclass=cast(dict[str, type], cmdclass),
    distclass=BinaryDistribution,
)
