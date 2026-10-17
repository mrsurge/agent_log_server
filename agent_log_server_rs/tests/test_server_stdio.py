"""Isolated native serving/stdio checks; set ALS_TEST_SERVER_BIN to a fresh build."""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import msgspec


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.environ.get("ALS_TEST_SERVER_BIN"), "fresh native binary not selected")
class ServerStdioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.posts: list[str] = []
        posts = self.posts
        class Observer(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_error(404)
            def do_POST(self) -> None:
                posts.append(self.path)
                self.send_error(404)
            def log_message(self, *_args: object) -> None:
                pass
        self.framework = ThreadingHTTPServer(("127.0.0.1", 0), Observer)
        thread = threading.Thread(target=self.framework.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.framework.server_close)
        self.addCleanup(self.framework.shutdown)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ALS_", "AGENT_LOG_", "FRAMEWORK_SHELLS_", "TE_", "TE2_"))}
        self.port = free_port()
        self.env.update({"ALS_RS_HOST": "127.0.0.1", "ALS_RS_PORT": str(self.port),
                         "TE_APP_ID": "als-rs",
                         "TE_FRAMEWORK_URL": f"http://127.0.0.1:{self.framework.server_port}"})
        for name in ("DATA", "CACHE", "CONFIG", "STATIC", "EXTENSIONS"):
            path = root / name.lower()
            path.mkdir()
            self.env[f"ALS_RS_{name}_DIR"] = str(path)

    def start(self, stdio: bool = True) -> subprocess.Popen[bytes]:
        command = [os.environ["ALS_TEST_SERVER_BIN"]]
        if stdio:
            command.append("--stdio")
        process = subprocess.Popen(command, env=self.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def frame(self, process: subprocess.Popen[bytes]) -> dict[str, object]:
        assert process.stdout is not None
        data = bytearray()
        deadline = time.monotonic() + 8
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(0.1):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    break
                data.extend(chunk)
                self.assertLess(len(data), 1024 * 1024)
                try:
                    value = msgspec.msgpack.decode(data)
                except msgspec.DecodeError:
                    continue
                self.assertIsInstance(value, dict)
                return value
        self.fail(f"No valid framework frame (received {len(data)} bytes)")

    def ready(self, process: subprocess.Popen[bytes]) -> None:
        self.assertEqual(self.frame(process), {
            "jsonrpc": "2.0", "protocolVersion": 1, "kind": "notification",
            "method": "app.readiness", "originNid": 0, "originName": "als-rs",
            "targetNid": 1, "targetName": "framework.rust",
            "params": {"status": "ready", "phase": "serving"},
        })
        # Serving is actually available after the protocol readiness fact.
        with urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=3) as response:
            self.assertEqual(response.status, 200)

    def finish(self, process: subprocess.Popen[bytes], *, code: int = 0) -> None:
        output, stderr = process.communicate(timeout=8)
        self.assertEqual(output, b"", "non-protocol stdout or duplicate readiness")
        self.assertEqual(process.returncode, code, stderr.decode(errors="replace"))
        self.assertEqual(self.posts, [], "readiness must never POST over HTTP")

    def test_readiness_then_eof_exits_cleanly(self) -> None:
        process = self.start()
        self.ready(process)
        self.finish(process)

    def test_sigterm_does_not_wait_for_open_stdin(self) -> None:
        process = self.start()
        self.ready(process)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=8), 0)
        self.finish(process)

    def test_corrupt_or_truncated_pipe_is_fatal(self) -> None:
        for raw in (b"\x81", b"\xc1"):
            with self.subTest(raw=raw):
                process = self.start()
                self.ready(process)
                assert process.stdin is not None
                process.stdin.write(raw)
                process.stdin.flush()
                self.finish(process, code=1)

    def test_unsupported_request_receives_framework_error_not_jsonrpc_text(self) -> None:
        process = self.start()
        self.ready(process)
        request = {"jsonrpc": "2.0", "protocolVersion": 1, "kind": "request",
                   "originNid": 1, "originName": "framework.rust",
                   "id": "probe", "method": "not.implemented"}
        assert process.stdin is not None
        for byte in msgspec.msgpack.encode(request):
            process.stdin.write(bytes([byte]))
            process.stdin.flush()
        response = self.frame(process)
        self.assertEqual(response["kind"], "error")
        self.assertEqual(response["id"], "probe")
        self.assertEqual(response["targetName"], "framework.rust")
        self.assertEqual(response["error"], {
            "code": "method_not_found", "message": "ALS framework method not supported", "retryable": False})
        self.finish(process)

    def test_occupied_listener_fails_without_readiness(self) -> None:
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.port))
            occupied.listen()
            self.finish(self.start(), code=1)

    def test_standalone_does_not_consume_stdin_or_emit_framework_readiness(self) -> None:
        process = self.start(stdio=False)
        assert process.stdin is not None
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + 8
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    self.fail("standalone server never became available")
                time.sleep(0.05)
        self.assertIsNone(process.poll())
        process.send_signal(signal.SIGTERM)
        self.finish(process)
