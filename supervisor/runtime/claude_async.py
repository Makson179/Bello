"""Event-driven batches over the official Claude SDK's MCP boundary.

The SDK owns the LLM loop and cannot accept an unfinished MCP call's result
later. A batch therefore returns actual ready results plus immutable pending
IDs; its remaining results are appended by a subsequent query in that same
SDK session. Never return a batch made only of pending placeholders.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


BATCH_TOOL_NAME = "run_parallel_tools"
BATCH_GUIDANCE = (
    "Use run_parallel_tools to start necessary independent Bello tool calls together. "
    "Do not put dependent actions, or actions that modify the same data, in one batch. "
    "Each batch returns ready results and may list still-running call IDs. Those results "
    "arrive automatically in later messages. Continue only required independent work; "
    "otherwise end your response to wait. Do not poll or launch duplicate work. "
    "Do not give a final review verdict until all pending results have arrived."
)


@dataclass
class _Job:
    call_id: str
    name: str
    task: asyncio.Task[dict[str, Any]]
    delivered: bool = False
    announced: bool = False


class ClaudeAsyncBatches:
    def __init__(self, *, grace_seconds: float = 1.0):
        self.grace_seconds = grace_seconds
        self.jobs: dict[str, _Job] = {}
        self.batch_specs: dict[str, list[dict[str, Any]]] = {}
        self.batch_responses: dict[str, dict[str, Any]] = {}
        self.closed = False

    @property
    def pending(self) -> bool:
        return any(not job.delivered for job in self.jobs.values())

    async def run(
        self,
        batch_id: str,
        calls: list[dict[str, Any]],
        dispatch: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.closed:
            raise asyncio.CancelledError
        ids = [f"{batch_id}:{index}" for index in range(len(calls))]
        if batch_id in self.batch_specs and self.batch_specs[batch_id] != calls:
            raise ValueError("Asynchronous batch ID was reused with different calls")
        if batch_id in self.batch_responses:
            return deepcopy(self.batch_responses[batch_id])
        if any(call_id in self.jobs for call_id in ids):
            # MCP retry identities are stable. Do not execute repeated batches.
            if not all(call_id in self.jobs for call_id in ids):
                raise ValueError("Partially duplicated asynchronous batch")
            jobs = [self.jobs[call_id] for call_id in ids]
        else:
            self.batch_specs[batch_id] = deepcopy(calls)
            jobs = []
            for call_id, call in zip(ids, calls, strict=True):
                task = asyncio.create_task(self._dispatch(dispatch, call_id, call["name"], deepcopy(call["arguments"])))
                job = _Job(call_id, call["name"], task)
                self.jobs[call_id] = job
                jobs.append(job)
        tasks = {job.task for job in jobs}
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=self.grace_seconds)
            if not done:
                earlier = {job.task for job in self.jobs.values() if job.announced and not job.delivered}
                await asyncio.wait(tasks | earlier, return_when=asyncio.FIRST_COMPLETED)
        # Concurrent delivery retries can both wait on the same futures.
        # Once one envelope is committed, every retry must return that exact
        # envelope, not consume newly-ready late results under the old ID.
        if batch_id in self.batch_responses:
            return deepcopy(self.batch_responses[batch_id])
        # A previous batch may finish while every new command is still busy.
        # Return its real output now, instead of waiting for this batch alone.
        content: list[dict[str, Any]] = await self.take_ready()
        for job in jobs:
            if job.task.done():
                content.extend(self._result(job))
                job.delivered = True
            else:
                job.announced = True
                content.append({"type": "text", "text": f"Tool call {job.call_id} ({job.name}) is still running. Its result will arrive automatically; do not poll."})
        response = {"content": content, "is_error": False}
        self.batch_responses[batch_id] = deepcopy(response)
        return response

    async def take_ready(self, *, wait: bool = False) -> list[dict[str, Any]]:
        candidates = [job for job in self.jobs.values() if job.announced and not job.delivered]
        if wait and candidates and not any(job.task.done() for job in candidates):
            await asyncio.wait({job.task for job in candidates}, return_when=asyncio.FIRST_COMPLETED)
        content: list[dict[str, Any]] = []
        for job in candidates:
            if job.task.done():
                content.extend(self._result(job))
                job.delivered = True
        return content

    @staticmethod
    async def _dispatch(dispatch: Callable[..., Awaitable[dict[str, Any]]], call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await dispatch(call_id, name, arguments)
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                raise ValueError("Invalid tool result")
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            # Detailed host errors are already journaled; avoid leaking
            # transport credentials from arbitrary exception messages.
            return {"content": [{"type": "text", "text": "Bello tool host rejected the call."}], "isError": True}

    @staticmethod
    def _result(job: _Job) -> list[dict[str, Any]]:
        if job.task.cancelled():
            result = {"content": [{"type": "text", "text": "Tool call was interrupted."}], "isError": True}
        else:
            result = job.task.result()
        failed = result.get("isError", result.get("is_error", False))
        return [
            {"type": "text", "text": f"Completed tool call {job.call_id} ({job.name}){' — failed' if failed else ''}:"},
            *deepcopy(result["content"]),
        ]

    async def cancel(self) -> None:
        self.closed = True
        for job in self.jobs.values():
            if not job.task.done():
                job.task.cancel()
        await asyncio.gather(*(job.task for job in self.jobs.values()), return_exceptions=True)
