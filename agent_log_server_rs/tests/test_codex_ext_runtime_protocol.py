import json
import tempfile
import unittest
from pathlib import Path
from typing import Callable, cast

from extensions.codex_ext.runtime_protocol import RuntimeProtocol, build_request_params
from extensions.codex_ext import runtime_protocol


def _write_schema_bundle(cache_dir: Path, properties: dict[str, object]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "codex_app_server_protocol.v2.schemas.json").write_text(
        json.dumps(
            {
                "definitions": {
                    "ThreadResumeParams": {
                        "type": "object",
                        "properties": properties,
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class CodexRuntimeProtocolTests(unittest.TestCase):
    def test_schema_cache_rejects_stable_resume_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _write_schema_bundle(cache_dir, {"threadId": {"type": "string"}})
            cache_has_experimental_bundle = cast(
                Callable[[Path], bool],
                getattr(runtime_protocol, "_schema_cache_has_experimental_bundle"),
            )

            self.assertFalse(cache_has_experimental_bundle(cache_dir))

    def test_schema_cache_accepts_experimental_resume_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            _write_schema_bundle(
                cache_dir,
                {
                    "threadId": {"type": "string"},
                    "excludeTurns": {"type": "boolean"},
                },
            )
            cache_has_experimental_bundle = cast(
                Callable[[Path], bool],
                getattr(runtime_protocol, "_schema_cache_has_experimental_bundle"),
            )

            self.assertTrue(cache_has_experimental_bundle(cache_dir))
            self.assertTrue((cache_dir / ".experimental-api-schema").exists())

    def test_build_request_params_emits_exclude_turns_when_schema_allows_it(self) -> None:
        protocol = RuntimeProtocol(
            version="codex-cli 0.137.0",
            version_key="0.137.0",
            cache_dir=Path("."),
            schema_path=Path("codex_app_server_protocol.v2.schemas.json"),
            definitions={},
            request_params={
                "thread/resume": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": "string"},
                        "excludeTurns": {"type": "boolean"},
                    },
                }
            },
            responses={},
            server_requests={},
            server_request_responses={},
            notifications={},
            events={},
            server_request_semantics={},
            notification_semantics={},
            event_semantics={},
        )

        params = build_request_params(
            protocol,
            "thread/resume",
            {},
            thread_id="thread_123",
            exclude_turns=True,
        )

        self.assertEqual(params["threadId"], "thread_123")
        self.assertIs(params["excludeTurns"], True)


if __name__ == "__main__":
    unittest.main()
