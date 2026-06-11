from __future__ import annotations

import unittest
from typing import Callable, cast

from extensions.codex_ext import client


class CodexClientResumeTests(unittest.TestCase):
    def test_thread_not_found_error_must_match_bound_thread(self) -> None:
        looks_like_thread_not_loaded = cast(
            Callable[[object, str | None], bool],
            getattr(client, "_looks_like_thread_not_loaded_error"),
        )

        self.assertTrue(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: thread not found: thread_123",
                "thread_123",
            )
        )
        self.assertFalse(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: thread not found: thread_abc",
                "thread_123",
            )
        )
        self.assertFalse(
            looks_like_thread_not_loaded(
                "JSON-RPC error -32600: conversation not found",
                "thread_123",
            )
        )


if __name__ == "__main__":
    unittest.main()
