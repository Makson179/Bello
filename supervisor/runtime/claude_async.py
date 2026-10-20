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
    completed_at: float | None = None


@dataclass
class _Batch:
    jobs: list[_Job]
    ready_at: float | None = None


class ClaudeAsyncBatches:
    def __init__(self, *, grace_seconds: float = 1.0):
        self.grace_seconds = grace_seconds
        self.jobs: dict[str, _Job] = {}
        self.batch_specs: dict[str, list[dict[str, Any]]] = {}
        self.batch_responses: dict[str, dict[str, Any]] = {}
        self._batches: dict[str, _Batch] = {}
        self._waiting: dict[str, int] = {}
        self._changed = asyncio.Event()
        self.closed = False

    @property
    def pending(self) -> bool:
        # A cancelled coordinator owes nothing: its tasks were cancelled and no
        # later delivery can ever claim them.
        return not self.closed and any(not job.delivered for job in self.jobs.values())

    async def run(
        self,
        batch_id: str,
        calls: list[dict[str, Any]],
        dispatch: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        if self.closed:
            raise asyncio.CancelledError
        if not calls:
            raise ValueError("Asynchronous batches must contain at least one call")
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
            batch = _Batch(jobs)
            self._batches[batch_id] = batch
            for call_id, call in zip(ids, calls, strict=True):
                task = asyncio.create_task(self._dispatch(dispatch, call_id, call["name"], deepcopy(call["arguments"])))
                job = _Job(call_id, call["name"], task)
                self.jobs[call_id] = job
                jobs.append(job)
                task.add_done_callback(lambda _task, batch_id=batch_id, job=job: self._completed(batch_id, job))
        self._waiting[batch_id] = self._waiting.get(batch_id, 0) + 1
        try:
            while True:
                self._changed.clear()
                if self.closed:
                    raise asyncio.CancelledError
                # Commit envelopes without yielding: concurrent MCP retries must
                # replay the original response, not claim a later completion.
                if batch_id in self.batch_responses:
                    return deepcopy(self.batch_responses[batch_id])
                content = self._take_ready(batch_id=batch_id)
                if content:
                    for job in jobs:
                        if not job.delivered:
                            job.announced = True
                            state = "awaiting grouped delivery" if job.task.done() else "still running"
                            content.append({"type": "text", "text": f"Tool call {job.call_id} ({job.name}) is {state}. Its result will arrive automatically; do not poll."})
                    response = {"content": content, "is_error": False}
                    self.batch_responses[batch_id] = deepcopy(response)
                    self._changed.set()
                    return response
                await self._wait_for_change(batch_id=batch_id)
        finally:
            self._waiting[batch_id] -= 1
            if not self._waiting[batch_id] and batch_id not in self.batch_responses and not self.closed:
                self._abandon(batch_id)

    def _abandon(self, batch_id: str) -> None:
        """Settle a batch call that ended before any response was committed.

        The SDK can cancel a run_parallel_tools call, for example on its own tool
        timeout. The model never saw these call IDs, so their work is stopped and
        nothing is owed: no later wake waits for or delivers them. A retry of the
        same call replays this cancellation instead of waiting forever.
        """
        batch = self._batches.get(batch_id)
        for job in batch.jobs if batch is not None else ():
            if not job.delivered:
                job.task.cancel()
                job.delivered = True
        self.batch_responses[batch_id] = {
            "content": [{"type": "text", "text": "This batch call was cancelled before its results were returned; "
                                                 "its tool calls were stopped."}],
            "is_error": True,
        }
        self._changed.set()

    async def take_ready(self, *, wait: bool = False) -> list[dict[str, Any]]:
        while True:
            if wait:
                self._changed.clear()
            if self.closed:
                raise asyncio.CancelledError
            content = self._take_ready()
            if content or not wait or not any(job.announced and not job.delivered for job in self.jobs.values()):
                return content
            await self._wait_for_change()

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def _completed(self, batch_id: str, job: _Job) -> None:
        if job.completed_at is not None:
            return
        job.completed_at = self._now()
        batch = self._batches[batch_id]
        if batch.ready_at is None or job.completed_at < batch.ready_at:
            # Only a completion in this original batch can rearm its window.
            # Polling, retries, and newly launched batches never move it.
            # Once ready, retain that eligibility until results are claimed.
            batch.ready_at = job.completed_at + self.grace_seconds
        self._changed.set()

    def _take_ready(self, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        now = self._now()
        for current_id, batch in self._batches.items():
            # A task can finish before its done callback gets an event-loop
            # turn. Account for it before deciding whether to release a group.
            for job in batch.jobs:
                if job.task.done() and job.completed_at is None:
                    self._completed(current_id, job)
            if not all(job.task.done() for job in batch.jobs) and (batch.ready_at is None or now < batch.ready_at):
                continue
            delivered = False
            for job in batch.jobs:
                if job.delivered or not job.task.done() or not (job.announced or current_id == batch_id):
                    continue
                content.extend(self._result(job))
                job.delivered = True
                delivered = True
            if delivered:
                batch.ready_at = None
        return content

    async def _wait_for_change(self, *, batch_id: str | None = None) -> None:
        deadlines = [
            batch.ready_at
            for current_id, batch in self._batches.items()
            if batch.ready_at is not None
            and any(not job.delivered and job.task.done() and (job.announced or current_id == batch_id) for job in batch.jobs)
        ]
        timeout = max(0.0, min(deadlines) - self._now()) if deadlines else None
        try:
            # Register the event waiter in this task. Python 3.11 wait_for()
            # schedules a separate task, leaving a gap where another batch
            # can clear a completion notification before it is observed.
            async with asyncio.timeout(timeout):
                await self._changed.wait()
        except TimeoutError:
            pass

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
        self._changed.set()
        for job in self.jobs.values():
            if not job.task.done():
                job.task.cancel()
        await asyncio.gather(*(job.task for job in self.jobs.values()), return_exceptions=True)
