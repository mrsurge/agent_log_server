from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile


_RELEASE_SCRIPT = Path(__file__).parents[2] / "release" / "build_binary_wheel.py"
_SPEC = importlib.util.spec_from_file_location(
    "agent_log_server_release_binary_wheel_test",
    _RELEASE_SCRIPT,
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Unable to load release helpers from {_RELEASE_SCRIPT}")
release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release)


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


class RefreshPackagedDigestTests(unittest.TestCase):
    def test_removes_directory_members_and_rebuilds_manifest_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            wheel = Path(temp_root) / "candidate.whl"
            binary_name = "agent_log_server_rs/bin/als-server"
            manifest_name = "agent_log_server_rs/bin/als-server.manifest.json"
            record_name = "agent_log_server-0.2.118.dist-info/RECORD"
            binary = b"native-server-payload"
            manifest = json.dumps({"sha256": "stale"}).encode()

            with ZipFile(wheel, "w") as archive:
                archive.writestr("agent_log_server.libs/", b"")
                archive.writestr(binary_name, binary)
                archive.writestr(manifest_name, manifest)
                archive.writestr(record_name, "stale,sha256=stale,5\n")

            release._refresh_packaged_digest(wheel)  # pyright: ignore[reportPrivateUsage]

            with ZipFile(wheel) as archive:
                infos = archive.infolist()
                self.assertFalse(any(info.is_dir() for info in infos))
                payloads = {
                    info.filename: archive.read(info.filename)
                    for info in infos
                }

            refreshed_manifest = json.loads(payloads[manifest_name])
            self.assertEqual(
                refreshed_manifest["sha256"],
                hashlib.sha256(binary).hexdigest(),
            )

            rows = list(
                csv.reader(io.StringIO(payloads[record_name].decode("utf-8")))
            )
            record = {name: (digest, size) for name, digest, size in rows}
            self.assertEqual(record[record_name], ("", ""))
            for name in (binary_name, manifest_name):
                self.assertEqual(record[name][0], _record_digest(payloads[name]))
                self.assertEqual(record[name][1], str(len(payloads[name])))


if __name__ == "__main__":
    unittest.main()
