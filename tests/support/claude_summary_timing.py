"""Timed summarizer fakes for virtual-clock tests (synthetic lifecycle fixtures).

These extend the scripted SDK fakes of tests/test_runtime_claude_context_summary
with ``Delay`` items and connect/measure gates for the summarizer only, and
record virtual timestamps of queries and interrupts. A delay reproduces the
*ordering* of a slow provider, never its historical cause.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage

from supervisor.runtime.claude import ClaudeBackend
from tests.test_runtime_claude import AUTH
from tests.test_runtime_claude_context_summary import GATE, HANG, SummaryScriptClient, SummaryScriptFactory


class Delay:
    """Keep the summarizer's stream silent for ``seconds`` of (virtual) time."""

    def __init__(self, seconds: float):
        self.seconds = seconds


class TimedScriptClient(SummaryScriptClient):
    async def connect(self):
        await super().connect()
        if self.summarizer and self.factory.summary_connect_gate is not None:
            await self.factory.summary_connect_gate.wait()

    async def query(self, prompt):
        if self.summarizer:
            self.factory.queried_at.append(asyncio.get_running_loop().time())
        await super().query(prompt)

    async def receive_response(self):
        if not self.summarizer:
            async for message in super().receive_response():
                yield message
            return
        while self.script:
            item = self.script.pop(0)
            if isinstance(item, Delay):
                await asyncio.sleep(item.seconds)
                continue
            if item is HANG:
                await asyncio.Event().wait()
            if item is GATE:
                await self.factory.release.wait()
                continue
            yield item
            if isinstance(item, ResultMessage):
                return

    async def interrupt(self):
        self.factory.interrupted_at.append(asyncio.get_running_loop().time())
        await super().interrupt()


class TimedScriptFactory(SummaryScriptFactory):
    def __init__(self, scripts, **kwargs):
        super().__init__(scripts, **kwargs)
        self.queried_at: list[float] = []
        self.interrupted_at: list[float] = []
        self.summary_connect_gate: asyncio.Event | None = None

    def __call__(self, options):
        client = TimedScriptClient(self, options)
        self.clients.append(client)
        return client


class Recorder:
    """Collects backend events and wakes waiters on turn completion."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.completed = asyncio.Event()
        self.completed_at: float | None = None

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if event["method"] == "turn/completed":
            self.completed_at = asyncio.get_running_loop().time()
            self.completed.set()

    def context(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["method"].startswith("thread/context")]

    def turn(self) -> dict[str, Any]:
        return next(event for event in self.events if event["method"] == "turn/completed")["params"]["turn"]


def timed_backend(tmp_path: Path, factory: TimedScriptFactory, recorder: Recorder) -> ClaudeBackend:
    async def default_tool(_request):
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    return ClaudeBackend(tmp_path / "claude-state", recorder.emit, tool_handler=default_tool,
                         client_factory=factory, auth_probe=lambda: dict(AUTH), environment={},
                         cli_path=tmp_path / "official-claude-cli-fixture")


async def start_thread(instance: ClaudeBackend, tmp_path: Path, policy: dict[str, Any] | None = None,
                       developer: str = "Keep the stable constraint.") -> str:
    await instance.request("initialize", {})
    await instance.request("thread/start", {
        "threadId": "thread-1", "model": "sonnet", "cwd": str(tmp_path), "effort": "max",
        "developerInstructions": developer,
        "noNeedForContext": policy or {"enabled": True, "thresholdTokens": 1000, "retainPercent": 10},
        "tools": [{"name": "read_file", "description": "Read one file.", "parameters": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}],
    })
    return instance._record("thread-1")["claudeSessionId"]


async def run_turn(instance: ClaudeBackend, recorder: Recorder, text: str = "Implement the original request.",
                   *, limit: float = 100_000.0) -> dict[str, Any]:
    await instance.request("turn/start", {"threadId": "thread-1", "turnId": "outer",
                                          "input": [{"type": "text", "text": text}]})
    async with asyncio.timeout(limit):
        await recorder.completed.wait()
    return recorder.turn()
