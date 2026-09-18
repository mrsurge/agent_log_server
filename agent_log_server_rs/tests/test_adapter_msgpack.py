from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path
import unittest
from typing import cast
from unittest.mock import patch

from agent_log_server_rs.codec import FrameDecoder, adapter_codec, decode_frame, encode_frame
from agent_log_server_rs.tests.test_extension_adapter_dispatch import ConcurrentAdapter


class AdapterMessagepackTests(unittest.TestCase):
    def test_fixture_and_every_split(self) -> None:
        frame = bytes.fromhex(Path("tests/fixtures/adapter_frame.msgpack.hex").read_text())
        expected = {"jsonrpc": "2.0", "id": 7, "result": {"text": "one\ntwo", "ok": True}}
        self.assertEqual(encode_frame(expected, "messagepack"), frame)
        for split in range(len(frame) + 1):
            decoder = FrameDecoder("messagepack")
            frames = decoder.feed(frame[:split]) + decoder.feed(frame[split:] + frame)
            decoder.finish()
            self.assertEqual([decode_frame(f, "messagepack") for f in frames], [expected, expected])

    def test_partial_invalid_and_oversized_frames(self) -> None:
        decoder = FrameDecoder("messagepack")
        self.assertEqual(decoder.feed(b"\x81\xa1x"), [])
        with self.assertRaisesRegex(ValueError, "truncated"):
            decoder.finish()
        with self.assertRaises(ValueError):
            FrameDecoder("messagepack").feed(b"\xc0")
        with patch("agent_log_server_rs.codec.MAX_FRAME_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                encode_frame({"text": "long string"}, "messagepack")
            with self.assertRaisesRegex(ValueError, "byte limit"):
                FrameDecoder("messagepack").feed(b"\x81\xa1x\xdb\x00\x00\x01\x00abcdef")

    def test_codec_selection(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(adapter_codec(), "messagepack")
        with patch.dict(os.environ, {"ALS_RS_ADAPTER_CODEC": "json"}):
            self.assertEqual(adapter_codec(), "json")
        with patch.dict(os.environ, {"ALS_RS_ADAPTER_CODEC": "typo"}):
            with self.assertRaises(ValueError):
                adapter_codec()

    def test_dispatch_concurrency_and_binary_output(self) -> None:
        async def run() -> bytes:
            adapter = ConcurrentAdapter()
            stdin = io.BytesIO(b"".join(encode_frame({"jsonrpc": "2.0", "id": i, "method": method}, "messagepack")
                for i, method in [(1, "test.slow"), (2, "test.fast")]))
            stdout = io.BytesIO()
            task = asyncio.create_task(adapter.run_stdio(stdin, stdout))
            await asyncio.wait_for(adapter.slow_started.wait(), 1)
            await asyncio.wait_for(adapter.fast_seen.wait(), 1)
            adapter.release_slow.set()
            await asyncio.wait_for(task, 1)
            return stdout.getvalue()

        decoder = FrameDecoder("messagepack")
        with patch.dict(os.environ, {"ALS_RS_ADAPTER_CODEC": "messagepack"}):
            frames = decoder.feed(asyncio.run(run()))
        decoder.finish()
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(isinstance(decode_frame(f, "messagepack"), dict) for f in frames))

    def test_real_adapter_process_both_codecs(self) -> None:
        for codec in ("messagepack", "json"):
            request: dict[str, object] = {"jsonrpc": "2.0", "id": 99, "method": "extension.shutdown", "params": {}}
            result = subprocess.run(
                [sys.executable, "-m", "agent_log_server_rs.adapters.extension_adapter"],
                input=encode_frame(request, codec), capture_output=True, timeout=15,
                env={**os.environ, "ALS_RS_ADAPTER_CODEC": codec},
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            decoder = FrameDecoder(codec)
            frames = decoder.feed(result.stdout)
            decoder.finish()
            values = [decode_frame(f, codec) for f in frames]
            self.assertTrue(any(isinstance(v, dict) and cast(dict[str, object], v).get("id") == 99 and "result" in v for v in values))

    def test_binary_fd_reader_with_split_frames(self) -> None:
        async def run() -> bytes:
            adapter = ConcurrentAdapter()
            read_fd, write_fd = os.pipe()
            output = io.BytesIO()
            with os.fdopen(read_fd, "rb") as stream:
                task = asyncio.create_task(adapter.run_stdio(stream, output))
                try:
                    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "method": "test.fast"}, "messagepack")
                    for i in range(0, len(frame), 3):
                        _ = os.write(write_fd, frame[i:i + 3])
                        await asyncio.sleep(0)
                    await asyncio.wait_for(adapter.fast_seen.wait(), 1)
                finally:
                    os.close(write_fd)
                await asyncio.wait_for(task, 1)
            return output.getvalue()

        with patch.dict(os.environ, {"ALS_RS_ADAPTER_CODEC": "messagepack"}):
            output = asyncio.run(run())
        frames = FrameDecoder("messagepack").feed(output)
        self.assertEqual(len(frames), 1)
        self.assertEqual(decode_frame(frames[0], "messagepack"), {"jsonrpc": "2.0", "id": 1, "result": {"name": "fast"}})
