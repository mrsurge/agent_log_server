"""Optional uvloop selection at ALS-owned Python loop boundaries."""
from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import Coroutine
from typing import Protocol, TypeVar, cast

Result = TypeVar("Result")


class LoopModule(Protocol):
    def new_event_loop(self) -> asyncio.AbstractEventLoop: ...


def new_event_loop() -> asyncio.AbstractEventLoop:
    if os.environ.get("ALS_DISABLE_UVLOOP", "").strip().lower() in {"1", "true", "yes", "on"}:
        return asyncio.new_event_loop()
    try:
        module = importlib.import_module("uvloop")
    except ModuleNotFoundError as exc:
        if exc.name != "uvloop":
            raise
        return asyncio.new_event_loop()
    return cast(LoopModule, module).new_event_loop()


def run(coroutine: Coroutine[object, object, Result]) -> Result:
    def factory() -> asyncio.AbstractEventLoop:
        loop = new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

    try:
        with asyncio.Runner(loop_factory=factory) as runner:
            return runner.run(coroutine)
    finally:
        coroutine.close()
        asyncio.set_event_loop(None)
