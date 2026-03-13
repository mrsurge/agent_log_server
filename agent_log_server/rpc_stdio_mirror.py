from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from typing import BinaryIO, Optional, Sequence


def _mirror_to_stderr(prefix: str, chunk: bytes) -> None:
    if not chunk:
        return
    text = chunk.decode("utf-8", errors="replace")
    parts = text.splitlines(keepends=True) or [text]
    for part in parts:
        if not part:
            continue
        sys.stderr.write(f"[{prefix}] {part}")
        if not part.endswith("\n"):
            sys.stderr.write("\n")
    sys.stderr.flush()


def _pump_input(proc: subprocess.Popen[bytes], prefix: str) -> None:
    child_stdin = proc.stdin
    if child_stdin is None:
        return
    try:
        while True:
            chunk = sys.stdin.buffer.readline()
            if not chunk:
                break
            _mirror_to_stderr(prefix, chunk)
            child_stdin.write(chunk)
            child_stdin.flush()
    except BrokenPipeError:
        pass
    finally:
        try:
            child_stdin.close()
        except Exception:
            pass


def _pump_output(stream: Optional[BinaryIO], target: Optional[BinaryIO], prefix: str) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            if target is not None:
                try:
                    target.write(chunk)
                    target.flush()
                except BrokenPipeError:
                    return
            _mirror_to_stderr(prefix, chunk)
    except BrokenPipeError:
        return


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror full-duplex stdio traffic to stderr while preserving stdout."
    )
    parser.add_argument("--label", default="rpc", help="Log prefix label")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to exec after --")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing command after --")

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    stdin_thread = threading.Thread(
        target=_pump_input,
        args=(proc, f"{args.label} rpc in"),
        daemon=True,
    )
    stdout_thread = threading.Thread(
        target=_pump_output,
        args=(proc.stdout, sys.stdout.buffer, f"{args.label} rpc out"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_output,
        args=(proc.stderr, None, f"{args.label} err"),
        daemon=True,
    )

    stdin_thread.start()
    stdout_thread.start()
    stderr_thread.start()

    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except Exception:
            pass
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
