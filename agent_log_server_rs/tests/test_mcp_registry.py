from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_log_server_rs import mcp_tools
from agent_log_server_rs.mcp_embedded_runtime import Runtime
from agent_log_server_rs.mcp_registry import RequestContext, object_map

ROOT = Path(__file__).absolute().parents[2]


class RegistryTests(unittest.TestCase):
    def test_sdk_free_import_closure(self) -> None:
        code = '''
import importlib.abc, importlib.machinery, os, sys
os.environ['FRAMEWORK_SHELLS_SECRET'] = 'isolated-registry-import'
class Block(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'mcp','fastmcp','fastapi','pydantic','pydantic_core','cryptography'}:
            # Allow optional find_spec capability checks, forbid actual imports.
            return importlib.machinery.ModuleSpec(fullname, self)
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        raise AssertionError('Unexpected dependency import: ' + module.__name__)
sys.meta_path.insert(0, Block())
from agent_log_server_rs.mcp_embedded_runtime import Runtime
runtime = Runtime()
assert len(runtime.catalog['tools']) == 38
runtime.close()
assert not any(n.split('.')[0] in {'mcp','fastmcp','fastapi','pydantic','pydantic_core','cryptography'} for n in sys.modules)
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_all_input_schemas_match_sdk_argument_validation(self) -> None:
        # SDK remains a test/reference dependency, never a core runtime import.
        from agent_log_server_rs.tests import mcp_sdk_reference as reference
        for name, descriptor in mcp_tools.mcp.descriptors.items():
            schema = object_map(descriptor["inputSchema"])
            properties = object_map(schema["properties"])
            arguments: dict[str, object] = {}
            for key, raw in properties.items():
                field = object_map(raw)
                if "default" in field:
                    continue
                arguments[key] = {"string": "test", "integer": 2, "boolean": True}[str(field["type"])]
            with self.subTest(tool=name):
                tool = reference.mcp._tool_manager.get_tool(name)
                assert tool is not None
                metadata = tool.fn_metadata
                expected = metadata.arg_model.model_validate(metadata.pre_parse_json(arguments)).model_dump_one_level()
                self.assertEqual(mcp_tools.mcp.arguments(name, arguments), expected)
                # Validate each field's supported coercions without executing a handler.
                for key, raw in properties.items():
                    field = object_map(raw)
                    kind = field.get("type")
                    samples: list[object] = [None] if "anyOf" in field else []
                    if kind == "integer":
                        samples += [True, "123", "12.0", "12345678901234567890", 3.0]
                    if kind == "boolean":
                        samples += ["yes", "false", 0, 1]
                    if "anyOf" in field and "choices" == key:
                        samples += ['["Approve", "Reject"]', ["Approve"]]
                    for sample in samples:
                        supplied = {**arguments, key: sample}
                        expected = metadata.arg_model.model_validate(metadata.pre_parse_json(supplied)).model_dump_one_level()
                        self.assertEqual(mcp_tools.mcp.arguments(name, supplied), expected)

    def test_unknown_context_is_not_user_injected(self) -> None:
        args = mcp_tools.mcp.arguments("ask_user", {"question": "Test", "ctx": "untrusted", "unknown": True})
        self.assertNotIn("ctx", args)
        self.assertNotIn("unknown", args)
        with self.assertRaises(ValueError):
            mcp_tools.mcp.arguments("ask_user", {})
        with self.assertRaises(ValueError):
            mcp_tools.mcp.arguments("ask_user", {"question": 123})

    def test_mcp_cancellation_releases_real_ask_user_owner(self) -> None:
        began = threading.Event()
        cancelled = threading.Event()

        class Client:
            connected = True
            async def call(self, event: str, data: object = None, **kwargs: object) -> object:
                if event == "ask_user_begin":
                    began.set()
                if event == "ask_user_cancel":
                    cancelled.set()
                return {"ok": True}

        async def client() -> object:
            return Client()

        with patch.dict(os.environ, {"CONVERSATION_ID": "isolated-registry-test"}), patch.object(mcp_tools, "_get_appserver_ipc_sio", side_effect=client):
            runtime = Runtime()
            try:
                complete = threading.Event()
                future = runtime.submit("tool", "ask_user", {"question": "Test", "choices": ["Approve", "Reject"]}, "1", lambda _: complete.set())
                self.assertTrue(began.wait(timeout=2))
                # Another real tool completes while approval is pending.
                other = runtime.submit("tool", "conv_id", {}, "2", lambda _: None)
                self.assertEqual(other.result(timeout=2)["structuredContent"], {"ok": True, "conversation_id": "isolated-registry-test"})
                future.cancel()
                self.assertTrue(complete.wait(timeout=2))
                self.assertTrue(cancelled.wait(timeout=2))
            finally:
                runtime.close()
            self.assertFalse(mcp_tools._ask_user_pending_requests)
            self.assertFalse(mcp_tools._ask_user_active_by_requestor)

    def test_result_envelopes_match_for_string_and_object(self) -> None:
        async def scenario() -> None:
            result = await mcp_tools.mcp.call("kb_help", {}, RequestContext("1"))
            structured = object_map(result["structuredContent"])
            self.assertIsInstance(structured["result"], str)
            self.assertFalse(result["isError"])
            result = await mcp_tools.mcp.call("ping", {}, RequestContext("2"))
            self.assertTrue(object_map(result["structuredContent"])["ok"])
        asyncio.run(scenario())

    def test_all_handlers_dispatch_once_and_match_sdk_result_envelopes(self) -> None:
        from agent_log_server_rs.tests import mcp_sdk_reference as reference

        async def scenario() -> None:
            for name, descriptor in mcp_tools.mcp.descriptors.items():
                calls: list[dict[str, object]] = []
                output = object_map(descriptor["outputSchema"])
                value: object = "mocked λ text" if "properties" in output else {"ok": True, "nested": [None, True, 7, "λ"]}

                async def stub(**kwargs: object) -> object:
                    calls.append(kwargs)
                    return value

                arguments: dict[str, object] = {}
                schema = object_map(descriptor["inputSchema"])
                for key, raw in object_map(schema["properties"]).items():
                    field = object_map(raw)
                    if "default" not in field:
                        arguments[key] = {"string": "test", "integer": 2, "boolean": True}[str(field["type"])]
                sdk_tool = reference.mcp._tool_manager.get_tool(name)
                assert sdk_tool is not None
                with self.subTest(tool=name), patch.dict(mcp_tools.mcp.tools, {name: stub}), patch.object(sdk_tool, "fn", stub):
                    native = await mcp_tools.mcp.call(name, arguments, RequestContext("fixture"))
                    self.assertEqual(len(calls), 1)
                    sdk_content, sdk_structured = await sdk_tool.run(arguments, convert_result=True)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(native["structuredContent"], sdk_structured)
                    self.assertEqual(native["content"], [content.model_dump(mode="json", exclude_none=True) for content in sdk_content])
        asyncio.run(scenario())
