"""Deterministic virtual time for asyncio tests (synthetic timing only).

``VirtualClockLoop.time()`` starts at zero and only advances when the loop
would otherwise sleep: instead of blocking until the next timer, the selector
jumps the clock to it. Every ``asyncio.timeout``, ``asyncio.sleep`` and
``call_later`` therefore fires at its exact virtual deadline, instantly in
real time and independent of machine load. Ready I/O and threadsafe wakeups are
still delivered, because the selector is always polled first.

This reproduces ordering and deadlines, not provider latency: a fake delayed
client is a synthetic lifecycle fixture, never evidence of a historical
network cause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import selectors
from typing import Any, TypeVar

T = TypeVar("T")


class _VirtualSelector:
    def __init__(self, selector: selectors.BaseSelector, loop: "VirtualClockLoop"):
        self._selector = selector
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = self._selector.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            # Nothing is scheduled: only real I/O or a threadsafe call can
            # make progress, so wait for it in real time.
            return self._selector.select(None)
        self._loop.advance(timeout)
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._selector, name)


class VirtualClockLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self._virtual_now = 0.0
        self._selector = _VirtualSelector(self._selector, self)  # type: ignore[assignment]

    def time(self) -> float:
        return self._virtual_now

    def advance(self, seconds: float) -> None:
        if seconds > 0:
            self._virtual_now += seconds


def run_virtual(factory: Callable[[], Awaitable[T]]) -> T:
    """Run one coroutine to completion on a fresh virtual-clock loop.

    The loop is never installed as the thread's current loop, so it cannot
    interfere with the pytest-asyncio loop used by other tests.
    """
    loop = VirtualClockLoop()
    try:
        return loop.run_until_complete(factory())
    finally:
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
