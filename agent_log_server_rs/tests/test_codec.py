from __future__ import annotations

import unittest
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_log_server_rs.codec import (
    AdapterDecodeError,
    AdapterEncodeError,
    decode_json_line,
    encode_json_line,
)


class _Kind(StrEnum):
    LIVE = "event.live"


@dataclass(frozen=True)
class _Payload:
    path: Path


class AdapterCodecTests(unittest.TestCase):
    def test_encode_json_line_matches_adapter_wire_shape(self) -> None:
        encoded = encode_json_line(
            {
                "jsonrpc": "2.0",
                "method": _Kind.LIVE,
                "params": {
                    "path": Path("/repo/file.py"),
                    "text": "hello",
                    "nested": _Payload(Path("/repo/other.py")),
                },
            }
        )

        self.assertEqual(
            encoded,
            '{"jsonrpc":"2.0","method":"event.live","params":{"path":"/repo/file.py","text":"hello","nested":{"path":"/repo/other.py"}}}\n',
        )

    def test_decode_json_line_returns_plain_python_values(self) -> None:
        decoded = decode_json_line('{"jsonrpc":"2.0","id":1,"params":{"ok":true}}\n')

        self.assertEqual(decoded, {"jsonrpc": "2.0", "id": 1, "params": {"ok": True}})

    def test_decode_error_is_adapter_specific(self) -> None:
        with self.assertRaises(AdapterDecodeError):
            decode_json_line("{not json")

    def test_encode_error_is_adapter_specific(self) -> None:
        class Unsupported:
            pass

        with self.assertRaises(AdapterEncodeError):
            encode_json_line({"value": Unsupported()})


if __name__ == "__main__":
    unittest.main()
