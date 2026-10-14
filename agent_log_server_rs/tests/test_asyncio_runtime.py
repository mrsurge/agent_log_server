from __future__ import annotations

import asyncio
import os
import threading
import unittest
from unittest.mock import patch

from agent_log_server_rs import asyncio_runtime as runtime


class OptionalLoopTests(unittest.TestCase):
    def test_absent_disabled_present_and_broken(self) -> None:
        missing = ModuleNotFoundError("uvloop absent", name="uvloop")
        with patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "0"}), patch.object(runtime.importlib, "import_module", side_effect=missing):
            loop = runtime.new_event_loop()
            self.assertIsInstance(loop, asyncio.AbstractEventLoop)
            loop.close()
        with patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "1"}), patch.object(runtime.importlib, "import_module") as importer:
            loop = runtime.new_event_loop()
            loop.close()
            importer.assert_not_called()
        class Module:
            def new_event_loop(self) -> asyncio.AbstractEventLoop:
                return asyncio.new_event_loop()
        with patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "0"}), patch.object(runtime.importlib, "import_module", return_value=Module()) as importer:
            loop = runtime.new_event_loop()
            loop.close()
            importer.assert_called_once_with("uvloop")
        for error in (ImportError("ABI failure"), ModuleNotFoundError("missing dependency", name="dependency")):
            with self.subTest(error=error), patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "0"}), patch.object(runtime.importlib, "import_module", side_effect=error):
                with self.assertRaises(type(error)):
                    runtime.new_event_loop()

    def test_runner_closes_tasks_and_unstarted_coroutine(self) -> None:
        cleanup: list[bool] = []
        async def pending() -> None:
            try:
                await asyncio.sleep(60)
            finally:
                cleanup.append(True)
        async def work() -> int:
            asyncio.create_task(pending())
            await asyncio.sleep(0)
            return await asyncio.to_thread(lambda: 7)
        with patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "1"}):
            self.assertEqual(runtime.run(work()), 7)
        self.assertEqual(cleanup, [True])
        coroutine = work()
        with patch.object(runtime, "new_event_loop", side_effect=ImportError("ABI failure")):
            with self.assertRaises(ImportError):
                runtime.run(coroutine)
        self.assertIsNone(coroutine.cr_frame)

    def test_runner_factory_is_owned_by_calling_thread(self) -> None:
        owners: list[int] = []
        loops: list[asyncio.AbstractEventLoop] = []
        class Module:
            def new_event_loop(self) -> asyncio.AbstractEventLoop:
                owners.append(threading.get_ident())
                loop = asyncio.new_event_loop()
                loops.append(loop)
                return loop
        async def work() -> int:
            self.assertIs(asyncio.get_running_loop(), loops[-1])
            return 42
        results: list[int] = []
        with patch.dict(os.environ, {"ALS_DISABLE_UVLOOP": "0"}), patch.object(runtime.importlib, "import_module", return_value=Module()):
            thread = threading.Thread(target=lambda: results.append(runtime.run(work())))
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [42])
        self.assertEqual(owners, [thread.ident])
        self.assertTrue(loops[0].is_closed())
