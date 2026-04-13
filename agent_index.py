from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TypeAlias, TypeVar, cast

ManifestMap: TypeAlias = dict[str, object]

T = TypeVar("T")

AGENT_CACHE_DIRNAME = "agent_messaging"
AGENT_INDEX_DIRNAME = "agent_index"
MANIFEST_FILENAME = "manifest.json"


def _coerce_manifest_map(value: object) -> ManifestMap:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _type_name(expected_type: type[object]) -> str:
    return getattr(expected_type, "__name__", str(expected_type))


@dataclass
class AgentRecord:
    agent_id: str
    path: Path
    manifest: ManifestMap

    @property
    def enabled(self) -> bool:
        return bool(self.manifest.get("enabled", False))

    @property
    def profiles(self) -> list[str]:
        profiles = self.manifest.get("profiles")
        if isinstance(profiles, dict):
            return [str(key) for key in profiles.keys()]
        return []


def get_cache_home() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home).expanduser()
    return Path.home() / ".cache"


def get_agent_cache_root() -> Path:
    return get_cache_home() / AGENT_CACHE_DIRNAME


def get_agent_index_dir() -> Path:
    return get_agent_cache_root() / AGENT_INDEX_DIRNAME


def get_repo_agent_index_dir(repo_root: Optional[Path] = None) -> Path:
    if repo_root is None:
        repo_root = Path.cwd()
    return repo_root / AGENT_INDEX_DIRNAME


def ensure_agent_index_dirs() -> None:
    get_agent_index_dir().mkdir(parents=True, exist_ok=True)


def install_templates_to_cache(repo_root: Optional[Path] = None, overwrite: bool = False) -> list[Path]:
    src_root = get_repo_agent_index_dir(repo_root)
    dst_root = get_agent_index_dir()
    ensure_agent_index_dirs()

    installed: list[Path] = []
    if not src_root.exists():
        return installed

    for entry in src_root.iterdir():
        if not entry.is_dir():
            continue
        dst_dir = dst_root / entry.name
        if dst_dir.exists():
            if not overwrite:
                continue
            shutil.rmtree(dst_dir)
        shutil.copytree(entry, dst_dir)
        installed.append(dst_dir)
    return installed


def iter_manifest_paths(root: Optional[Path] = None) -> Iterable[Path]:
    if root is None:
        root = get_agent_index_dir()
    if not root.exists():
        return []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / MANIFEST_FILENAME
        if manifest_path.exists():
            yield manifest_path


def _require_key(obj: ManifestMap, key: str, expected_type: type[T], ctx: str) -> T:
    if key not in obj:
        raise ValueError(f"{ctx}: missing required key '{key}'")
    value = obj[key]
    if not isinstance(value, expected_type):
        raise ValueError(f"{ctx}: key '{key}' must be {_type_name(cast(type[object], expected_type))}")
    return value


def _optional_key(obj: ManifestMap, key: str, expected_type: type[object], ctx: str) -> None:
    if key in obj and not isinstance(obj[key], expected_type):
        raise ValueError(f"{ctx}: key '{key}' must be {_type_name(expected_type)}")


def validate_manifest(manifest: ManifestMap, path: Path) -> ManifestMap:
    ctx = str(path)
    _require_key(manifest, "schema_version", int, ctx)
    _require_key(manifest, "agent_id", str, ctx)
    _require_key(manifest, "pseudonym", str, ctx)
    _optional_key(manifest, "display_name", str, ctx)
    _optional_key(manifest, "enabled", bool, ctx)

    profiles = _require_key(manifest, "profiles", dict, ctx)
    if not profiles:
        raise ValueError(f"{ctx}: profiles must not be empty")
    for profile_name, profile in profiles.items():
        if not isinstance(profile_name, str):
            raise ValueError(f"{ctx}: profile names must be strings")
        if not isinstance(profile, dict):
            raise ValueError(f"{ctx}: profile '{profile_name}' must be an object")
        profile_map = _coerce_manifest_map(profile)
        _optional_key(profile_map, "enabled", bool, ctx)
        _require_key(profile_map, "backend", str, ctx)
        _require_key(profile_map, "shellspec_ref", str, ctx)
        _require_key(profile_map, "mode", str, ctx)
        _optional_key(profile_map, "execution", dict, ctx)
        _optional_key(profile_map, "env", dict, ctx)

    _optional_key(manifest, "tags", list, ctx)
    _optional_key(manifest, "permissions", dict, ctx)
    _optional_key(manifest, "ui", dict, ctx)
    return manifest


def load_manifest(path: Path) -> ManifestMap:
    data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest must be a JSON object")
    return validate_manifest(_coerce_manifest_map(data), path)


def load_agent_registry(root: Optional[Path] = None) -> list[AgentRecord]:
    records: list[AgentRecord] = []
    for manifest_path in iter_manifest_paths(root):
        manifest = load_manifest(manifest_path)
        records.append(
            AgentRecord(
                agent_id=_require_key(manifest, "agent_id", str, str(manifest_path)),
                path=manifest_path.parent,
                manifest=manifest,
            )
        )
    return records
