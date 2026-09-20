"""Finish ownership cleanup before propagating caller cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar


T = TypeVar("T")


async def finish_cleanup(operation: Awaitable[T]) -> T:
    # shield alone returns immediately when its caller is cancelled. Keep a
    # strong reference and wait until cleanup actually finishes, even if the
    # caller receives another cancellation while waiting.
    task = asyncio.ensure_future(operation)
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            cancellation.add_note(f"Runtime cleanup failed: {type(exc).__name__}")
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result
