from __future__ import annotations

import asyncio
import queue
import threading

import pytest

import supervisor.tui as tui_module
from supervisor.tui import TerminalTUI


class _EofStdin:
    def fileno(self) -> int:
        return 9

    def readline(self) -> str:
        return ""


class _FakeLoop:
    def __init__(self) -> None:
        self.removed: list[int] = []

    def remove_reader(self, fd: int) -> None:
        self.removed.append(fd)


def test_tui_unregisters_stdin_reader_on_eof(monkeypatch) -> None:
    tui = TerminalTUI()
    loop = _FakeLoop()
    tui._loop = loop  # type: ignore[assignment]
    tui._reader_registered = True
    monkeypatch.setattr("supervisor.tui.sys.stdin", _EofStdin())

    tui._on_stdin_ready()

    assert loop.removed == [9]
    assert tui._reader_registered is False


class _FallbackLoop:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def add_reader(self, fd: int, callback) -> None:
        raise NotImplementedError

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.loop.call_soon_threadsafe(callback, *args)


class _BlockingStdin:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def fileno(self) -> int:
        return 9

    def readline(self) -> str:
        self.entered.set()
        self.release.wait()
        return ""


class _QueuedStdin:
    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()

    def fileno(self) -> int:
        return 9

    def readline(self) -> str:
        return self.lines.get()


@pytest.mark.asyncio
async def test_tui_fallback_stop_does_not_wait_for_blocked_input(monkeypatch: pytest.MonkeyPatch) -> None:
    real_loop = asyncio.get_running_loop()
    fallback_loop = _FallbackLoop(real_loop)
    stdin = _BlockingStdin()
    monkeypatch.setattr(tui_module.asyncio, "get_running_loop", lambda: fallback_loop)
    monkeypatch.setattr(tui_module.sys, "stdin", stdin)
    tui = TerminalTUI()

    await tui.start()
    for _ in range(100):
        if stdin.entered.is_set():
            break
        await asyncio.sleep(0.001)

    assert stdin.entered.is_set()
    assert tui._reader_thread is not None
    assert tui._reader_thread.daemon is True
    await asyncio.wait_for(tui.stop(), timeout=0.1)
    assert tui._reader_stop.is_set()

    # Release the test reader after proving stop returned while it was blocked.
    stdin.release.set()
    tui._reader_thread.join(timeout=0.2)


@pytest.mark.asyncio
async def test_tui_fallback_delivers_windows_crlf_input(monkeypatch: pytest.MonkeyPatch) -> None:
    real_loop = asyncio.get_running_loop()
    fallback_loop = _FallbackLoop(real_loop)
    stdin = _QueuedStdin()
    monkeypatch.setattr(tui_module.asyncio, "get_running_loop", lambda: fallback_loop)
    monkeypatch.setattr(tui_module.sys, "stdin", stdin)
    tui = TerminalTUI()

    await tui.start()
    stdin.lines.put("continue with native Windows\r\n")
    command = await asyncio.wait_for(tui.input_queue.get(), timeout=0.5)

    assert command.text == "continue with native Windows"
    await tui.stop()
    stdin.lines.put("")
    assert tui._reader_thread is not None
    tui._reader_thread.join(timeout=0.2)
