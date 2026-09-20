from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from supervisor.appserver import AppServerError, AppServerTimeoutError
from supervisor.runtime.transport import WorkerTransport


@pytest.mark.asyncio
async def test_host_callback_can_make_nested_request_without_deadlock(tmp_path):
    events = asyncio.Queue()
    async def emit(event):
        await events.put(event)
    async def tool(request):
        nested = await transport.request("nested", timeout=2)
        return {"content": [{"type": "text", "text": nested["method"]}]}
    async def error(exc):
        await events.put({"error": exc})
    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=emit, tool=tool, on_error=error)
    await transport.start()
    try:
        assert await transport.request("tool", timeout=2) == {"method": "tool"}
        event = await asyncio.wait_for(events.get(), 2)
        assert event["method"] == "tool_done"
        assert event["params"]["result"]["content"][0]["text"] == "nested"
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_worker_cancellation_reaches_running_host_tool(tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def tool(request):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    async def ignore(_):
        pass
    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=ignore, tool=tool, on_error=ignore)
    await transport.start()
    try:
        await transport.request("tool", timeout=2)
        await asyncio.wait_for(started.wait(), 2)
        await transport.request("cancel", timeout=2)
        await asyncio.wait_for(cancelled.wait(), 2)
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_uncertain_request_timeout_is_not_automatically_retried(tmp_path):
    async def ignore(_):
        pass
    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=ignore, tool=ignore, on_error=ignore)
    await transport.start()
    try:
        with pytest.raises(AppServerTimeoutError, match="not retried"):
            await transport.request("ignore", timeout=.1)
        assert transport._sequence == 1
        assert not transport._pending
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_invalid_stream_fails_pending_calls(tmp_path):
    errors = asyncio.Queue()
    async def ignore(_):
        pass
    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=ignore, tool=ignore, on_error=errors.put)
    await transport.start()
    try:
        with pytest.raises(ValueError):
            await transport.request("invalid", timeout=2)
        assert isinstance(await asyncio.wait_for(errors.get(), 2), ValueError)
        assert transport.process is not None and transport.process.returncode is not None
        with pytest.raises(AppServerError, match="not started"):
            await transport.request("nested", timeout=.1)
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_error_callback_can_stop_transport_without_self_wait(tmp_path):
    done = asyncio.Event()

    async def ignore(_):
        pass

    async def failed(_):
        # RuntimeClient.stop invokes engine.stop from a separate gather task.
        await asyncio.gather(transport.stop())
        done.set()

    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=ignore, tool=ignore, on_error=failed)
    await transport.start()
    try:
        with pytest.raises(ValueError):
            await transport.request("invalid", timeout=2)
        await asyncio.wait_for(done.wait(), 2)
        assert transport.process is None
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_still_terminates_worker(tmp_path):
    async def ignore(_):
        pass

    transport = WorkerTransport([sys.executable, str(Path(__file__).with_name("runtime_fake_worker.py"))],
                                tmp_path, emit=ignore, tool=ignore, on_error=ignore)
    await transport.start()
    process = transport.process
    entered, release = asyncio.Event(), asyncio.Event()
    original = transport._terminate_process

    async def delayed_terminate():
        entered.set()
        await release.wait()
        await original()

    transport._terminate_process = delayed_terminate
    task = asyncio.create_task(transport.stop())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert process.returncode is not None
        assert transport.process is None
    finally:
        release.set()
        await transport.stop()
