from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        self._copy_developer_message_template()
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


setup(cmdclass={"build_py": build_py})
