from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "als-server"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _rust_host_target() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in output.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise RuntimeError("Unable to determine the Rust host target")


def _source_commit() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()


def _source_is_dirty() -> bool:
    output = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT,
        text=True,
    )
    return bool(output.strip())


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if name in {
            ".git",
            ".venv",
            "build",
            "dist",
            "node_modules",
            "target",
            "__pycache__",
            ".pytest_cache",
            ".codex-scratch",
        }:
            ignored.add(name)
        elif name.endswith(".egg-info"):
            ignored.add(name)
    return ignored


def _scratch_root() -> Path:
    configured = os.environ.get("TMPDIR", "").strip()
    root = Path(configured).expanduser() if configured else ROOT / ".codex-scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_server(*, target: str, cargo_target_dir: Path) -> Path:
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(cargo_target_dir)
    _run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(ROOT / "rust" / "Cargo.toml"),
            "-p",
            SERVER_NAME,
            "--release",
            "--target",
            target,
        ],
        cwd=ROOT,
        env=env,
    )
    binary = cargo_target_dir / target / "release" / SERVER_NAME
    if not binary.is_file():
        raise FileNotFoundError(f"Built ALS-RS server is missing: {binary}")
    return binary


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _refresh_packaged_digest(wheel_path: Path) -> None:
    with ZipFile(wheel_path) as archive:
        # A wheel is a ZIP of files.  auditwheel may leave explicit directory
        # entries in its repaired archive, but re-emitting those entries makes
        # later auditwheel inspection try to open the directory as an ELF file.
        # Drop them while rebuilding RECORD; ZIP readers recreate directories
        # from the file paths without needing directory members.
        entries = [
            (info, archive.read(info.filename))
            for info in archive.infolist()
            if not info.is_dir()
        ]

    binary_name = "agent_log_server_rs/bin/als-server"
    manifest_name = "agent_log_server_rs/bin/als-server.manifest.json"
    record_names = [info.filename for info, _payload in entries if info.filename.endswith(".dist-info/RECORD")]
    if len(record_names) != 1:
        raise RuntimeError(f"Expected one wheel RECORD, found {record_names}")
    record_name = record_names[0]
    payloads = {info.filename: payload for info, payload in entries}
    if binary_name not in payloads or manifest_name not in payloads:
        raise RuntimeError("Repaired ALS-RS wheel is missing its binary-release payload")

    loaded_manifest = cast(object, json.loads(payloads[manifest_name]))
    if not isinstance(loaded_manifest, dict):
        raise RuntimeError("ALS-RS packaged server manifest must be an object")
    manifest = cast(dict[str, object], loaded_manifest)
    manifest["sha256"] = hashlib.sha256(payloads[binary_name]).hexdigest()
    payloads[manifest_name] = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"

    record_buffer = io.StringIO(newline="")
    record_writer = csv.writer(record_buffer, lineterminator="\n")
    for info, _payload in entries:
        name = info.filename
        if name == record_name:
            continue
        payload = payloads[name]
        record_writer.writerow((name, _record_digest(payload), str(len(payload))))
    record_writer.writerow((record_name, "", ""))
    payloads[record_name] = record_buffer.getvalue().encode("utf-8")

    temporary = wheel_path.with_name(f".{wheel_path.name}.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for info, _payload in entries:
                archive.writestr(info, payloads[info.filename])
        os.replace(temporary, wheel_path)
    finally:
        temporary.unlink(missing_ok=True)


def _repair_linux_wheel(raw_wheel: Path, *, out_dir: Path, platform_tag: str) -> Path:
    scratch_root = _scratch_root()
    with tempfile.TemporaryDirectory(prefix="als-auditwheel-", dir=scratch_root) as temp_dir:
        repaired_dir = Path(temp_dir) / "wheelhouse"
        repaired_dir.mkdir()
        auditwheel = shutil.which("auditwheel")
        auditwheel_command = [auditwheel] if auditwheel else [sys.executable, "-m", "auditwheel"]
        _run(
            [
                *auditwheel_command,
                "repair",
                "--plat",
                platform_tag,
                "--wheel-dir",
                str(repaired_dir),
                str(raw_wheel),
            ],
            cwd=ROOT,
        )
        repaired = sorted(repaired_dir.glob("*.whl"))
        if len(repaired) != 1:
            raise RuntimeError(f"Expected one repaired ALS-RS wheel, found {repaired}")
        _refresh_packaged_digest(repaired[0])
        destination = out_dir / repaired[0].name
        shutil.copy2(repaired[0], destination)
    return destination


def build_binary_wheel(
    *,
    out_dir: Path,
    target: str,
    platform_tag: str,
    binary: Path | None,
    allow_dirty: bool,
    skip_auditwheel: bool,
) -> Path:
    source_dirty = _source_is_dirty()
    if source_dirty and not allow_dirty:
        raise RuntimeError(
            "Refusing to build a binary-release wheel from a dirty source tree; "
            "pass --allow-dirty only for an unpublished local candidate"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    scratch_root = _scratch_root()
    cargo_target_dir = Path(
        os.environ.get("CARGO_TARGET_DIR", scratch_root / "cargo-target")
    ).expanduser()
    resolved_binary = binary or _build_server(
        target=target,
        cargo_target_dir=cargo_target_dir,
    )
    if not resolved_binary.is_file():
        raise FileNotFoundError(f"ALS-RS server binary does not exist: {resolved_binary}")

    with tempfile.TemporaryDirectory(prefix="als-wheel-stage-", dir=scratch_root) as temp_dir:
        stage_root = Path(temp_dir) / "source"
        raw_wheel_dir = Path(temp_dir) / "wheelhouse"
        raw_wheel_dir.mkdir()
        shutil.copytree(ROOT, stage_root, ignore=_copy_ignore)
        env = os.environ.copy()
        env.update(
            {
                "ALS_RS_PACKAGED_SERVER_BIN": str(resolved_binary.resolve()),
                "ALS_RS_PACKAGED_SERVER_TARGET": target,
                "ALS_RS_PACKAGED_SERVER_PLATFORM_TAG": platform_tag,
                "ALS_RS_PACKAGED_SERVER_SOURCE_COMMIT": _source_commit(),
                "ALS_RS_PACKAGED_SERVER_SOURCE_DIRTY": "1" if source_dirty else "0",
            }
        )
        _run(
            [
                sys.executable,
                "setup.py",
                "bdist_wheel",
                "--dist-dir",
                str(raw_wheel_dir),
                "--plat-name",
                platform_tag,
            ],
            cwd=stage_root,
            env=env,
        )
        raw_wheels = sorted(raw_wheel_dir.glob("*.whl"))
        if len(raw_wheels) != 1:
            raise RuntimeError(f"Expected one raw ALS-RS wheel, found {raw_wheels}")
        raw_wheel = raw_wheels[0]
        is_android = target.endswith("-android")
        if skip_auditwheel:
            if not is_android:
                raise RuntimeError("--skip-auditwheel is only valid for Android/Bionic wheels")
            destination = out_dir / raw_wheel.name
            shutil.copy2(raw_wheel, destination)
            return destination
        if is_android:
            raise RuntimeError("Android/Bionic wheels require --skip-auditwheel")
        return _repair_linux_wheel(raw_wheel, out_dir=out_dir, platform_tag=platform_tag)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a verified ALS-RS binary-release wheel")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    parser.add_argument(
        "--target",
        help="Rust target triple; defaults to the active rustc host when omitted",
    )
    parser.add_argument(
        "--plat-name",
        required=True,
        help="Audited wheel platform tag, for example manylinux_2_28_x86_64",
    )
    parser.add_argument("--binary", help="Use an already-built als-server binary")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow an unpublished candidate and record source_dirty=true in its manifest",
    )
    parser.add_argument(
        "--skip-auditwheel",
        action="store_true",
        help="Skip Linux ELF repair for a natively built Android/Bionic wheel",
    )
    args = parser.parse_args()
    out_dir = cast(str, args.out_dir)
    target_arg = cast(str | None, args.target)
    target = target_arg or _rust_host_target()
    platform_tag = cast(str, args.plat_name)
    binary_arg = cast(str | None, args.binary)
    allow_dirty = cast(bool, args.allow_dirty)
    skip_auditwheel = cast(bool, args.skip_auditwheel)
    wheel = build_binary_wheel(
        out_dir=Path(out_dir).expanduser().resolve(),
        target=target,
        platform_tag=platform_tag,
        binary=Path(binary_arg).expanduser().resolve() if binary_arg else None,
        allow_dirty=allow_dirty,
        skip_auditwheel=skip_auditwheel,
    )
    print(wheel)


if __name__ == "__main__":
    main()
