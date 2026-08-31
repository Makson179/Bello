from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UserCommand:
    text: str


class TerminalTUI:
    def __init__(self):
        self.input_queue: asyncio.Queue[UserCommand] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_registered = False
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._loop = asyncio.get_running_loop()
        try:
            self._loop.add_reader(sys.stdin.fileno(), self._on_stdin_ready)
            self._reader_registered = True
        except (NotImplementedError, OSError, ValueError):
            self._start_daemon_reader()

    async def stop(self) -> None:
        self._running = False
        if self._reader_registered and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(sys.stdin.fileno())
            self._reader_registered = False
        # ``readline`` cannot be interrupted portably.  The fallback thread is
        # deliberately daemonized and never belongs to asyncio's default
        # executor, so a blocked Windows console read cannot make
        # ``loop.shutdown_default_executor()`` hang during Bello shutdown.
        self._reader_stop.set()

    def render(self, lane: str, message: str, *, payload: dict[str, Any] | None = None) -> None:
        prefix = lane if lane.startswith("[") else f"[{lane}]"
        print(f"{prefix} {message}", flush=True)

    def status(self, message: str) -> None:
        self.render("SYSTEM", message)

    def _start_daemon_reader(self) -> None:
        stop_event = threading.Event()
        self._reader_stop = stop_event

        def read_lines() -> None:
            while not stop_event.is_set():
                try:
                    line = sys.stdin.readline()
                except (EOFError, OSError, ValueError):
                    return
                if line == "":
                    return
                loop = self._loop
                if loop is None or stop_event.is_set():
                    return
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._enqueue_input_line, line)

        self._reader_thread = threading.Thread(
            target=read_lines,
            name="bello-terminal-input",
            daemon=True,
        )
        self._reader_thread.start()

    def _enqueue_input_line(self, line: str) -> None:
        if self._running:
            self.input_queue.put_nowait(UserCommand(text=line.rstrip("\r\n")))

    def _on_stdin_ready(self) -> None:
        try:
            line = sys.stdin.readline()
        except (EOFError, OSError, ValueError):
            line = ""
        if line == "":
            if self._reader_registered and self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.remove_reader(sys.stdin.fileno())
                self._reader_registered = False
            return
        self._enqueue_input_line(line)
