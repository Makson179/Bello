from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from supervisor.runtime.claude import ClaudeBackend, _MCP_REQUEST_CONTEXT
from supervisor.runtime.claude_async import BATCH_TOOL_NAME, ClaudeAsyncBatches
from tests.test_runtime_claude import FakeFactory, backend, result_message, start_thread, wait_completed


def output(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


class BatchClock:
    def __init__(self, batches):
        self.value = 0.0
        self.batches = batches
        batches._now = lambda: self.value

    def advance(self, seconds):
        self.value += seconds
        self.batches._changed.set()


class ControlledTools:
    def __init__(self):
        self.gates = {}
        self.failures = set()
        self.started = []

    async def dispatch(self, call_id, _name, _arguments):
        self.started.append(call_id)
        await self.gates.setdefault(call_id, asyncio.Event()).wait()
        if call_id in self.failures:
            raise ValueError("private host failure")
        return output(call_id)

    def finish(self, call_id):
        self.gates[call_id].set()


async def flush_tasks():
    # Advance event-loop callbacks, without advancing the controlled clock.
    for _ in range(10):
        await asyncio.sleep(0)


def batch_calls(count):
    return [{"name": "read_file", "arguments": {"index": index}} for index in range(count)]


async def announce_batch(batches, clock, tools, batch_id, count):
    running = asyncio.create_task(batches.run(batch_id, batch_calls(count), tools.dispatch))
    await flush_tasks()
    tools.finish(f"{batch_id}:0")
    await flush_tasks()
    assert not running.done()
    clock.advance(1)
    await flush_tasks()
    return await asyncio.wait_for(running, 1)


async def test_initial_completion_starts_full_window_from_completion_time():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    running = asyncio.create_task(batches.run("b", batch_calls(3), tools.dispatch))
    try:
        await flush_tasks()
        clock.advance(0.75)
        tools.finish("b:0")
        await flush_tasks()
        clock.advance(0.05)
        tools.finish("b:1")
        await flush_tasks()
        clock.advance(0.2)
        await flush_tasks()
        assert not running.done(), "Launch time must not shorten the completion window"
        clock.advance(0.75)
        await flush_tasks()
        assert not running.done(), "A sibling inside the window must rearm it"
        clock.advance(0.05)
        await flush_tasks()
        response = await asyncio.wait_for(running, 1)
        assert [block["text"] for block in response["content"][1:4:2]] == ["b:0", "b:1"]
    finally:
        await batches.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_expired_unconsumed_window_is_not_rearmed_by_another_sibling():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 4)
        tools.finish("b:1")
        await flush_tasks()
        clock.advance(1.25)
        tools.finish("b:2")
        await flush_tasks()
        ready = await batches.take_ready()
        assert [block["text"] for block in ready[1::2]] == ["b:1", "b:2"]
        assert batches.pending
        assert await batches.take_ready() == []
    finally:
        await batches.cancel()


async def test_delivery_boundary_records_completion_before_delayed_done_callback():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 4)
        tools.finish("b:1")
        await flush_tasks()
        clock.advance(0.75)
        tools.finish("b:2")
        await asyncio.sleep(0)
        assert batches.jobs["b:2"].task.done()
        assert batches.jobs["b:2"].completed_at is None
        assert await batches.take_ready() == []
        assert batches.jobs["b:2"].completed_at == clock.value
        clock.advance(0.25)
        assert await batches.take_ready() == []
        await flush_tasks()
        clock.advance(0.75)
        ready = await batches.take_ready()
        assert [block["text"] for block in ready[1::2]] == ["b:1", "b:2"]
    finally:
        await batches.cancel()


async def test_concurrent_batch_cannot_erase_final_completion_before_waiter_registration(monkeypatch):
    batches = ClaudeAsyncBatches(grace_seconds=30)
    BatchClock(batches)
    tools = ControlledTools()
    original_wait = batches._wait_for_change
    original_wait_for = asyncio.wait_for
    competing = None

    async def python311_wait_for(awaitable, timeout):
        # Python 3.11 wait_for schedules coroutine inputs in a separate task.
        # Reproduce that registration gap even when testing on newer Python.
        if timeout is not None:
            awaitable = asyncio.ensure_future(awaitable)
        return await original_wait_for(awaitable, timeout)

    async def interleave_wait(*, batch_id=None):
        nonlocal competing
        if batch_id == "a" and batches._batches["a"].ready_at is not None and competing is None:
            tools.finish("a:1")
            competing = asyncio.create_task(batches.run("b", batch_calls(1), tools.dispatch))
        await original_wait(batch_id=batch_id)

    monkeypatch.setattr(asyncio, "wait_for", python311_wait_for)
    monkeypatch.setattr(batches, "_wait_for_change", interleave_wait)
    running = asyncio.create_task(batches.run("a", batch_calls(2), tools.dispatch))
    try:
        await flush_tasks()
        tools.finish("a:0")
        for _ in range(30):
            await asyncio.sleep(0)
        assert competing is not None
        assert all(batches.jobs[f"a:{index}"].task.done() for index in range(2))
        assert running.done(), "The final completion must release immediately, without the 30-second timer"
        assert [block["text"] for block in (await running)["content"][1::2]] == ["a:0", "a:1"]
        assert not competing.done()
    finally:
        await batches.cancel()
        await asyncio.gather(running, *([competing] if competing is not None else []), return_exceptions=True)


async def test_late_siblings_rearm_one_shared_deadline_without_polling_reset():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        original = await announce_batch(batches, clock, tools, "b", 4)
        snapshot = deepcopy(original)
        clock.advance(1)
        tools.finish("b:1")
        await flush_tasks()
        assert await batches.take_ready() == []
        clock.advance(0.75)
        tools.finish("b:2")
        await flush_tasks()
        clock.advance(0.25)
        assert await batches.take_ready() == []  # First completion's old deadline.
        assert await batches.run("b", batch_calls(4), tools.dispatch) == snapshot
        clock.advance(0.749)
        assert await batches.take_ready() == []
        clock.advance(0.001)
        ready = await batches.take_ready()
        assert [block["text"] for block in ready[1::2]] == ["b:1", "b:2"]
        assert original == snapshot
        assert await batches.take_ready() == []
        assert batches.pending
        tools.finish("b:3")
        await flush_tasks()
        assert (await batches.take_ready())[1]["text"] == "b:3"
        assert not batches.pending
    finally:
        await batches.cancel()


async def test_waiting_delivery_rearms_and_releases_immediately_when_all_siblings_finish():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    waiter = None
    try:
        await announce_batch(batches, clock, tools, "b", 4)
        waiter = asyncio.create_task(batches.take_ready(wait=True))
        await flush_tasks()
        clock.advance(2)
        tools.finish("b:1")
        await flush_tasks()
        assert not waiter.done()
        clock.advance(0.75)
        tools.finish("b:2")
        await flush_tasks()
        clock.advance(0.25)
        await flush_tasks()
        assert not waiter.done()
        tools.finish("b:3")
        await flush_tasks()
        ready = await asyncio.wait_for(waiter, 1)
        assert [block["text"] for block in ready[1::2]] == ["b:1", "b:2", "b:3"]
        assert not batches.pending
    finally:
        await batches.cancel()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)


async def test_launch_without_results_waits_for_late_completion_window():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    running = asyncio.create_task(batches.run("b", batch_calls(3), tools.dispatch))
    try:
        await flush_tasks()
        clock.advance(20)
        await flush_tasks()
        assert not running.done()
        tools.finish("b:0")
        await flush_tasks()
        assert not running.done()
        clock.advance(0.75)
        tools.finish("b:1")
        await flush_tasks()
        clock.advance(0.25)
        await flush_tasks()
        assert not running.done()
        clock.advance(0.75)
        await flush_tasks()
        response = await asyncio.wait_for(running, 1)
        assert [block["text"] for block in response["content"][1:4:2]] == ["b:0", "b:1"]
        assert "still running" in response["content"][-1]["text"]
    finally:
        await batches.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_new_batch_does_not_extend_older_completion_deadline():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    running = None
    try:
        await announce_batch(batches, clock, tools, "old", 3)
        clock.advance(1)
        tools.finish("old:1")
        await flush_tasks()
        clock.advance(0.75)
        running = asyncio.create_task(batches.run("new", batch_calls(2), tools.dispatch))
        await flush_tasks()
        tools.finish("new:0")
        await flush_tasks()
        assert not running.done()
        clock.advance(0.25)
        await flush_tasks()
        response = await asyncio.wait_for(running, 1)
        assert response["content"][1]["text"] == "old:1"
        assert not any(block["text"].startswith("Completed tool call new:") for block in response["content"])
        assert "awaiting grouped delivery" in response["content"][2]["text"]
        assert await batches.take_ready() == []
        clock.advance(0.75)
        assert (await batches.take_ready())[1]["text"] == "new:0"
        tools.finish("new:1")
        tools.finish("old:2")
        await flush_tasks()
        assert [block["text"] for block in (await batches.take_ready())[1::2]] == ["old:2", "new:1"]
    finally:
        await batches.cancel()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)


async def test_unrelated_completions_do_not_rearm_announced_batch():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "a", 3)
        await announce_batch(batches, clock, tools, "b", 3)
        tools.finish("a:1")
        await flush_tasks()
        clock.advance(0.75)
        tools.finish("b:1")
        await flush_tasks()
        clock.advance(0.25)
        ready = await batches.take_ready(wait=True)
        assert [block["text"] for block in ready[1::2]] == ["a:1"]
        assert await batches.take_ready() == []
        clock.advance(0.75)
        assert (await batches.take_ready())[1]["text"] == "b:1"
    finally:
        await batches.cancel()


async def test_failure_counts_as_finished_sibling_and_releases_waiter():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 3)
        tools.finish("b:1")
        await flush_tasks()
        assert await batches.take_ready() == []
        tools.failures.add("b:2")
        tools.finish("b:2")
        await flush_tasks()
        ready = await batches.take_ready(wait=True)
        assert ready[1]["text"] == "b:1"
        assert "failed" in ready[2]["text"]
        assert ready[3]["text"] == "Bello tool host rejected the call."
        assert not batches.pending
    finally:
        await batches.cancel()


async def test_cancel_during_debounce_stops_waiter_without_waiting_for_deadline():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    await announce_batch(batches, clock, tools, "b", 3)
    tools.finish("b:1")
    await flush_tasks()
    waiter = asyncio.create_task(batches.take_ready(wait=True))
    await flush_tasks()
    assert not waiter.done()
    await batches.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 1)
    assert all(job.task.done() for job in batches.jobs.values())


async def test_steering_cancels_debounce_wait_without_losing_queued_results():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 3)
        tools.finish("b:1")
        await flush_tasks()
        waiter = asyncio.create_task(ClaudeBackend._wait_async_delivery(batches))
        await flush_tasks()
        assert await ClaudeBackend._settle_async_wait_for_steer(waiter) == []
        assert not batches.jobs["b:1"].delivered
        assert not batches.jobs["b:2"].task.cancelled()
        clock.advance(0.75)
        replacement = asyncio.create_task(batches.take_ready(wait=True))
        await flush_tasks()
        assert not replacement.done()
        clock.advance(0.25)
        await flush_tasks()
        assert (await asyncio.wait_for(replacement, 1))[1]["text"] == "b:1"
        assert await batches.take_ready() == []
    finally:
        await batches.cancel()


async def test_empty_batch_cannot_return_placeholder_or_empty_envelope():
    batches = ClaudeAsyncBatches()
    with pytest.raises(ValueError, match="at least one call"):
        await batches.run("empty", [], ControlledTools().dispatch)


async def test_fast_batch_launches_together_without_full_grace():
    batches = ClaudeAsyncBatches(grace_seconds=30)
    started = []
    gate = asyncio.Event()

    async def dispatch(call_id, _name, _args):
        started.append(call_id)
        await gate.wait()
        return output(call_id)

    pending = asyncio.create_task(batches.run("batch", [{"name": "read_file", "arguments": {}}] * 2, dispatch))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started == ["batch:0", "batch:1"]
    gate.set()
    result = await asyncio.wait_for(pending, 1)
    assert len(result["content"]) == 4
    assert not batches.pending


async def test_never_returns_placeholders_only_and_late_delivery_is_once():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    gates = [asyncio.Event(), asyncio.Event()]

    async def dispatch(call_id, _name, _args):
        await gates[int(call_id[-1])].wait()
        return output(call_id)

    pending = asyncio.create_task(batches.run("b", [{"name": "exec_command", "arguments": {}}] * 2, dispatch))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not pending.done()
    gates[0].set()
    result = await pending
    snapshot = deepcopy(result)
    assert "still running" in result["content"][-1]["text"]
    assert batches.pending
    gates[1].set()
    late = await batches.take_ready(wait=True)
    assert "b:1" in late[0]["text"]
    assert result == snapshot
    assert await batches.take_ready() == []
    assert not batches.pending


async def test_batch_replay_does_not_repeat_side_effects_and_errors_are_retained():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    called = []

    async def dispatch(call_id, _name, _args):
        called.append(call_id)
        return {"content": [{"type": "text", "text": "bad input"}], "isError": True}

    calls = [{"name": "exec_command", "arguments": {}}]
    first = await batches.run("b", calls, dispatch)
    assert await batches.run("b", calls, dispatch) == first
    assert called == ["b:0"]
    assert "failed" in first["content"][0]["text"]
    assert first["content"][1]["text"] == "bad input"
    with pytest.raises(ValueError, match="different calls"):
        await batches.run("b", [{"name": "exec_command", "arguments": {"changed": True}}], dispatch)
    assert called == ["b:0"]


async def test_batch_replay_returns_original_envelope_without_consuming_late_output():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    gate = asyncio.Event()
    calls = [{"name": "read_file", "arguments": {}}, {"name": "read_file", "arguments": {"slow": True}}]

    async def dispatch(_call_id, _name, arguments):
        if arguments.get("slow"):
            await gate.wait()
        return output("data")

    original = await batches.run("b", calls, dispatch)
    assert "still running" in original["content"][-1]["text"]
    gate.set()
    await asyncio.sleep(0)
    assert await batches.run("b", calls, dispatch) == original
    assert batches.pending
    assert (await batches.take_ready(wait=True))[0]["text"].startswith("Completed tool call b:1")


async def test_concurrent_batch_retries_commit_one_envelope_and_leave_late_output_separate():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    gates = [asyncio.Event(), asyncio.Event()]
    dispatched = []
    calls = [{"name": "read_file", "arguments": {"index": index}} for index in range(2)]

    async def dispatch(call_id, _name, arguments):
        dispatched.append(call_id)
        await gates[arguments["index"]].wait()
        return output(call_id)

    retries = [asyncio.create_task(batches.run("same", calls, dispatch)) for _ in range(2)]
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert dispatched == ["same:0", "same:1"]
        assert not any(task.done() for task in retries)
        gates[0].set()
        first, second = await asyncio.wait_for(asyncio.gather(*retries), 1)
        assert first == second and first is not second
        assert "still running" in first["content"][-1]["text"]
        snapshot = deepcopy(first)
        gates[1].set()
        late = await asyncio.wait_for(batches.take_ready(wait=True), 1)
        assert late[0]["text"].startswith("Completed tool call same:1")
        assert first == snapshot and second == snapshot
        assert await batches.take_ready() == []
        assert not batches.pending
        assert dispatched == ["same:0", "same:1"]
    finally:
        await batches.cancel()
        await asyncio.gather(*retries, return_exceptions=True)


async def test_cancellation_stops_detached_tasks():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    cancelled = asyncio.Event()

    async def dispatch(_call_id, _name, args):
        if args.get("slow"):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return output("ok")

    await batches.run("b", [{"name": "exec_command", "arguments": {}}, {"name": "exec_command", "arguments": {"slow": True}}], dispatch)
    await batches.cancel()
    assert cancelled.is_set()
    assert all(job.task.done() for job in batches.jobs.values())


async def test_sdk_late_input_preserves_images():
    source = [{"type": "text", "text": "result"}, {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="}]
    messages = [message async for message in ClaudeBackend._async_prompt(source)]
    assert messages[0]["message"]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    assert source[1]["mimeType"] == "image/png"


async def test_steering_keeps_a_delivery_that_completed_after_wait_selected_the_command():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    gate = asyncio.Event()

    async def dispatch(_call_id, _name, args):
        if args.get("slow"):
            await gate.wait()
        return output("evidence")

    await batches.run("b", [{"name": "read_file", "arguments": {}}, {"name": "read_file", "arguments": {"slow": True}}], dispatch)
    receive_task = asyncio.create_task(ClaudeBackend._wait_async_delivery(batches))
    # Model the exact boundary: the caller has selected its steering command,
    # then the result task finishes before the caller cancels that waiter.
    gate.set()
    await receive_task
    assert not batches.pending
    content = await ClaudeBackend._settle_async_wait_for_steer(receive_task)
    assert content[0]["text"].startswith("Completed tool call b:1")
    assert content[1]["text"] == "evidence"


async def test_earlier_late_result_can_release_an_all_pending_new_batch():
    batches = ClaudeAsyncBatches(grace_seconds=0)
    old_gate = asyncio.Event()
    new_gate = asyncio.Event()

    async def dispatch(_call_id, _name, args):
        if args.get("old"):
            await old_gate.wait()
        if args.get("new"):
            await new_gate.wait()
        return output("evidence")

    await batches.run("old", [{"name": "read_file", "arguments": {}}, {"name": "read_file", "arguments": {"old": True}}], dispatch)
    waiting = asyncio.create_task(batches.run("new", [{"name": "read_file", "arguments": {"new": True}}], dispatch))
    old_gate.set()
    result = await waiting
    assert result["content"][0]["text"].startswith("Completed tool call old:1")
    assert "still running" in result["content"][-1]["text"]
    new_gate.set()
    assert (await batches.take_ready(wait=True))[0]["text"].startswith("Completed tool call new:0")


async def test_on_only_batch_validates_all_original_schemas_before_dispatch(tmp_path):
    dispatched = []

    async def host(request):
        dispatched.append(request)
        return output("ok")

    instance = ClaudeBackend(tmp_path / "state", lambda _event: None, tool_handler=host, client_factory=lambda _options: None, environment={})
    definition = {"name": "read_file", "description": "Read", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}
    record = {"id": "thread", "claudeSessionId": "session", "tools": [definition], "activeTurnId": "turn"}
    assert len(instance._sdk_tools(record, {})) == 1
    record["asyncTools"] = True
    instance._async_batches[("thread", "turn")] = ClaudeAsyncBatches(grace_seconds=0)
    tool = next(tool for tool in instance._sdk_tools(record, {}) if tool.name == BATCH_TOOL_NAME)
    token = _MCP_REQUEST_CONTEXT.set(SimpleNamespace(request_id="batch-1", meta=None))
    try:
        invalid = await tool.handler({"calls": [{"name": "read_file", "arguments": {"path": "ok"}}, {"name": "read_file", "arguments": {}}]})
        assert invalid["is_error"]
        assert dispatched == []
        valid = await tool.handler({"calls": [{"name": "read_file", "arguments": {"path": "ok"}}]})
        assert not valid["is_error"]
        assert len(dispatched) == 1
        assert dispatched[0]["threadId"] == "thread"
        assert dispatched[0]["turnId"] == "turn"
        assert dispatched[0]["callId"].endswith(":0")
    finally:
        _MCP_REQUEST_CONTEXT.reset(token)
        await instance._async_batches[("thread", "turn")].cancel()


def test_usage_sums_sdk_continuations_without_losing_cache_fields():
    assert ClaudeBackend._merge_usage({"input_tokens": 2, "cache_read_input_tokens": 30}, {"input_tokens": 3, "cache_read_input_tokens": 40, "output_tokens": 7}) == {"input_tokens": 5, "cache_read_input_tokens": 70, "output_tokens": 7}


# --- Per-batch collection: windows, exactly-once delivery and cancellation ------------


async def test_late_results_after_a_delivery_open_a_new_window_that_siblings_restart():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 4)  # b:0 was delivered; the clock is at 1.0
        tools.finish("b:1")
        await flush_tasks()  # a new window opens for the late result: until 2.0
        clock.advance(0.5)
        tools.finish("b:2")
        await flush_tasks()  # a sibling inside the window restarts it: until 2.5
        clock.advance(0.75)  # past the first deadline, inside the restarted window
        assert await batches.take_ready() == []
        clock.advance(0.25)
        ready = await batches.take_ready()
        assert [block["text"] for block in ready[1::2]] == ["b:1", "b:2"]
        assert batches.pending, "b:3 is still running"
        assert await batches.take_ready() == [], "nothing is delivered twice"
        tools.finish("b:3")
        await flush_tasks()  # the batch is now finished: no window at all
        assert [block["text"] for block in (await batches.take_ready())[1::2]] == ["b:3"]
        assert not batches.pending and await batches.take_ready() == []
    finally:
        await batches.cancel()


async def test_a_window_that_expires_without_a_deliverable_result_never_wakes_the_waiter():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    new = None
    waiter = None
    try:
        await announce_batch(batches, clock, tools, "a", 2)  # a:1 is announced and running
        waiter = asyncio.create_task(batches.take_ready(wait=True))
        new = asyncio.create_task(batches.run("n", batch_calls(2), tools.dispatch))
        await flush_tasks()
        tools.finish("n:0")
        await flush_tasks()
        clock.advance(1)  # the other batch's window opens and expires: its result is its own response
        await flush_tasks()
        response = await asyncio.wait_for(new, 1)
        assert response["content"][1]["text"] == "n:0"
        clock.advance(30)
        await flush_tasks()
        assert not waiter.done(), "an expiry that delivers nothing of this batch must not wake the model"
        tools.finish("a:1")
        await flush_tasks()
        assert [block["text"] for block in (await asyncio.wait_for(waiter, 1))[1::2]] == ["a:1"]
    finally:
        await batches.cancel()
        await asyncio.gather(*(task for task in (new, waiter) if task is not None), return_exceptions=True)


async def test_cancellation_leaves_nothing_pending_and_stops_every_entry_point():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    await announce_batch(batches, clock, tools, "b", 3)
    waiter = asyncio.create_task(batches.take_ready(wait=True))
    await flush_tasks()
    assert batches.pending and not waiter.done()
    await batches.cancel()
    assert not batches.pending
    assert all(job.task.done() for job in batches.jobs.values())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 1)
    with pytest.raises(asyncio.CancelledError):
        await batches.take_ready()
    with pytest.raises(asyncio.CancelledError):
        await batches.run("late", batch_calls(1), tools.dispatch)
    assert "late:0" not in tools.started, "a cancelled coordinator launches nothing"


async def test_a_cancelled_tool_call_is_a_finished_sibling_delivered_once_as_interrupted():
    batches = ClaudeAsyncBatches()
    clock = BatchClock(batches)
    tools = ControlledTools()
    try:
        await announce_batch(batches, clock, tools, "b", 3)
        batches.jobs["b:1"].task.cancel()
        tools.finish("b:2")
        await flush_tasks()  # every sibling is finished: delivered without waiting for a window
        ready = await batches.take_ready(wait=True)
        assert ready[0]["text"].endswith("— failed:") and ready[1]["text"] == "Tool call was interrupted."
        assert ready[3]["text"] == "b:2"
        assert await batches.take_ready() == [] and not batches.pending
    finally:
        await batches.cancel()


async def test_take_ready_does_not_block_when_nothing_was_announced():
    batches = ClaudeAsyncBatches()
    assert await batches.take_ready() == []
    assert await asyncio.wait_for(batches.take_ready(wait=True), 1) == []


async def test_a_batch_call_cancelled_before_its_response_stops_its_work_and_owes_nothing():
    batches = ClaudeAsyncBatches()
    tools = ControlledTools()
    try:
        running = asyncio.create_task(batches.run("b", batch_calls(2), tools.dispatch))
        await flush_tasks()
        assert batches.pending and tools.started == ["b:0", "b:1"]
        running.cancel()  # the SDK abandons the call before any result was returned
        with pytest.raises(asyncio.CancelledError):
            await running
        await flush_tasks()
        # The model never saw these call IDs: their work stops and nothing stays pending,
        # so no later wake can wait for or deliver them.
        assert not batches.pending
        assert all(job.task.cancelled() for job in batches.jobs.values())
        assert await asyncio.wait_for(batches.take_ready(wait=True), 1) == []
        # A retry of the same call replays the cancellation instead of waiting forever.
        replay = await asyncio.wait_for(batches.run("b", batch_calls(2), tools.dispatch), 1)
        assert replay["is_error"] and "cancelled before its results were returned" in replay["content"][0]["text"]
        assert tools.started == ["b:0", "b:1"], "no work is started twice"
    finally:
        await batches.cancel()


async def test_cancelling_one_of_two_concurrent_batch_waiters_keeps_the_batch_for_the_other():
    batches = ClaudeAsyncBatches()
    tools = ControlledTools()
    try:
        first = asyncio.create_task(batches.run("b", batch_calls(1), tools.dispatch))
        await flush_tasks()
        retry = asyncio.create_task(batches.run("b", batch_calls(1), tools.dispatch))
        await flush_tasks()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert batches.pending, "the concurrent retry still owns the batch"
        tools.finish("b:0")
        await flush_tasks()
        response = await asyncio.wait_for(retry, 1)
        assert not response["is_error"] and response["content"][-1]["text"] == "b:0"
        assert not batches.pending and tools.started == ["b:0"]
    finally:
        await batches.cancel()


# --- The backend around the coordinator ---------------------------------------------


async def call_batch(instance, calls, request_id):
    """Run the run_parallel_tools handler as the SDK's MCP bridge would."""
    record = instance._record("thread-1")
    tool = next(tool for tool in instance._sdk_tools(record, {}) if tool.name == BATCH_TOOL_NAME)
    token = _MCP_REQUEST_CONTEXT.set(SimpleNamespace(request_id=request_id, meta=None))
    try:
        return await tool.handler({"calls": calls})
    finally:
        _MCP_REQUEST_CONTEXT.reset(token)


async def eventually(condition, limit=2):
    async with asyncio.timeout(limit):
        while not condition():
            await asyncio.sleep(0)


async def start_async_turn(instance, factory, tmp_path):
    await start_thread(instance, tmp_path, async_tools=True)
    response = await instance.request("turn/start", {
        "threadId": "thread-1", "turnId": "async-turn", "input": [{"type": "text", "text": "Check the solution"}]})
    turn_id = response["turn"]["id"]
    await eventually(lambda: factory.clients and factory.clients[-1].queries)
    return factory.clients[-1], instance._async_batches[("thread-1", turn_id)], turn_id


def late_tools():
    gates = {name: asyncio.Event() for name in ("slow-1", "slow-2")}

    async def host(request):
        path = request["arguments"]["path"]
        if path in gates:
            await gates[path].wait()
            return output(f"late {path}")
        return output("ready evidence")

    return gates, host


def mixed_batch(slow):
    return [{"name": "read_file", "arguments": {"path": "fast"}}, {"name": "read_file", "arguments": {"path": slow}}]


async def test_late_siblings_of_one_batch_arrive_in_one_appended_prompt_exactly_once(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(messages=[result_message(result="final checked result")], complete_after_queries=2)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        clock = BatchClock(coordinator)
        first_prompt = deepcopy(client.queries[0])
        calls = mixed_batch("slow-1") + [{"name": "read_file", "arguments": {"path": "slow-2"}}]
        running = asyncio.create_task(call_batch(instance, calls, "batch-0"))
        await flush_tasks()
        clock.advance(1)  # the fast sibling's window expires: the batch answers
        await flush_tasks()
        response = await asyncio.wait_for(running, 1)
        assert "still running" in response["content"][-1]["text"] and not response["is_error"]
        await client.messages.put(result_message(result="waiting"))
        await flush_tasks()
        assert len(client.queries) == 1 and not any(event["method"] == "turn/completed" for event in events)
        gates["slow-1"].set()
        await flush_tasks()
        clock.advance(0.5)
        gates["slow-2"].set()  # the last sibling finishes the batch: no window is left to wait for
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "completed"
        assert len(client.queries) == 2, "one appended prompt, no repeat"
        assert client.queries[0] == first_prompt, "the sent history is never rewritten"
        texts = [block.get("text") for block in client.queries[1][0]["message"]["content"]]
        assert texts.count("late slow-1") == 1 and texts.count("late slow-2") == 1
        assert not coordinator.pending and len([e for e in events if e["method"] == "turn/completed"]) == 1
    finally:
        gates["slow-1"].set()
        gates["slow-2"].set()
        await instance.stop()


async def test_a_sibling_restarts_the_window_before_the_model_is_woken(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(messages=[result_message(result="final checked result")], complete_after_queries=2)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        clock = BatchClock(coordinator)
        gates["slow-3"] = asyncio.Event()
        calls = mixed_batch("slow-1") + [{"name": "read_file", "arguments": {"path": "slow-2"}},
                                         {"name": "read_file", "arguments": {"path": "slow-3"}}]
        running = asyncio.create_task(call_batch(instance, calls, "batch-0"))
        await flush_tasks()
        clock.advance(1)
        await asyncio.wait_for(running, 1)
        await client.messages.put(result_message(result="waiting"))
        await flush_tasks()
        gates["slow-1"].set()
        await flush_tasks()  # opens the window: until 3.0
        clock.advance(0.5)
        gates["slow-2"].set()
        await flush_tasks()  # restarts it: until 3.5; slow-3 keeps the batch unfinished
        clock.advance(0.75)  # past the first deadline
        await flush_tasks()
        assert len(client.queries) == 1, "the restarted window is still open: the model sleeps"
        clock.advance(0.25)
        for _ in range(100):
            await asyncio.sleep(0)
            if len(client.queries) == 2:
                break
        assert len(client.queries) == 2
        texts = [block.get("text") for block in client.queries[1][0]["message"]["content"]]
        assert texts.count("late slow-1") == 1 and texts.count("late slow-2") == 1 and "late slow-3" not in texts
        assert coordinator.pending, "slow-3 is still running and will arrive on its own"
        await flush_tasks()  # the scripted final answer arrives while slow-3 is still pending
        assert not any(event["method"] == "turn/completed" for event in events)
        gates["slow-3"].set()
        await eventually(lambda: len(client.queries) == 3)
        third = [block.get("text") for block in client.queries[2][0]["message"]["content"]]
        assert third.count("late slow-3") == 1 and "late slow-1" not in third and "late slow-2" not in third
        await client.messages.put(result_message(result="final checked result"))
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "completed" and not coordinator.pending
    finally:
        for gate in gates.values():
            gate.set()
        await instance.stop()


async def test_unrelated_batches_collect_their_late_results_independently(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(complete_after_queries=None)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        clock = BatchClock(coordinator)
        for number, slow in enumerate(("slow-1", "slow-2")):
            running = asyncio.create_task(call_batch(instance, mixed_batch(slow), f"batch-{number}"))
            await flush_tasks()
            clock.advance(1)
            await flush_tasks()
            await asyncio.wait_for(running, 1)
        await client.messages.put(result_message(result="waiting"))
        await flush_tasks()
        gates["slow-1"].set()
        await flush_tasks()
        clock.advance(0.5)
        gates["slow-2"].set()  # another batch: it must not restart the first batch's window
        await flush_tasks()
        clock.advance(0.5)  # the first batch's own window ends at 3.0
        for _ in range(100):
            await asyncio.sleep(0)
            if len(client.queries) == 2:
                break
        assert len(client.queries) == 2
        first = [block.get("text") for block in client.queries[1][0]["message"]["content"]]
        assert "late slow-1" in first and "late slow-2" not in first
        await client.messages.put(result_message(result="waiting again"))
        clock.advance(1)  # the second batch's own window
        for _ in range(100):
            await asyncio.sleep(0)
            if len(client.queries) == 3:
                break
        assert len(client.queries) == 3
        second = [block.get("text") for block in client.queries[2][0]["message"]["content"]]
        assert second.count("late slow-2") == 1 and "late slow-1" not in second
        assert not coordinator.pending
    finally:
        for gate in gates.values():
            gate.set()
        await instance.stop()


async def test_interrupt_while_waiting_for_late_results_leaves_nothing_pending_and_the_thread_usable(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(complete_after_queries=None)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        coordinator.grace_seconds = 0  # the fast sibling answers at once: no real waiting in this test
        response = await call_batch(instance, mixed_batch("slow-1"), "batch-0")
        assert "still running" in response["content"][-1]["text"]
        await client.messages.put(result_message(result="waiting"))
        await eventually(lambda: ("thread-1", turn_id) in instance._async_usage)
        await asyncio.wait_for(instance.request("turn/interrupt", {"threadId": "thread-1", "turnId": turn_id}), 2)
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "interrupted"
        assert ("thread-1", turn_id) not in instance._async_batches and not coordinator.pending
        assert all(job.task.done() for job in coordinator.jobs.values())
        assert len(client.queries) == 1, "the model was never woken for the cancelled work"
        # Exactly one terminal event, and no stuck state: the thread runs the next turn.
        assert len([e for e in events if e["method"] == "turn/completed"]) == 1
        events.clear()
        await instance.request("turn/start", {
            "threadId": "thread-1", "turnId": "next-turn", "input": [{"type": "text", "text": "Continue"}]})
        await eventually(lambda: factory.clients[-1] is not client and factory.clients[-1].queries)
        await factory.clients[-1].messages.put(result_message(result="done"))
        assert (await wait_completed(events))["params"]["turn"]["status"] == "completed"
    finally:
        gates["slow-1"].set()
        await instance.stop()


async def test_a_result_with_only_an_unanswered_batch_call_finishes_without_an_empty_wake(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(complete_after_queries=None)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        # The SDK ends the response while its batch call is still unanswered (no result
        # is ready, so no envelope was committed and no call ID was announced).
        running = asyncio.create_task(call_batch(instance, [{"name": "read_file", "arguments": {"path": "slow-1"}}],
                                                 "batch-0"))
        await flush_tasks()
        assert coordinator.pending and not running.done()
        await client.messages.put(result_message(result="final checked result"))
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "completed"
        assert len(client.queries) == 1, "nothing was owed: the model is not woken with an empty delivery"
        assert completed["params"]["turn"]["usage"]["input_tokens"] == 7, "the held-back result is counted once"
        assert len([e for e in events if e["method"] == "turn/completed"]) == 1
        await eventually(lambda: running.done())
        assert all(job.task.done() for job in coordinator.jobs.values())
    finally:
        gates["slow-1"].set()
        await instance.stop()


async def test_backend_stop_while_waiting_for_late_results_completes_the_turn_once(tmp_path):
    gates, host = late_tools()
    factory = FakeFactory(complete_after_queries=None)
    instance = backend(tmp_path, factory, events := [], tool_handler=host)
    try:
        client, coordinator, turn_id = await start_async_turn(instance, factory, tmp_path)
        coordinator.grace_seconds = 0
        await call_batch(instance, mixed_batch("slow-1"), "batch-0")
        await client.messages.put(result_message(result="waiting"))
        await eventually(lambda: ("thread-1", turn_id) in instance._async_usage)
        await asyncio.wait_for(instance.stop(), 5)
        completions = [event for event in events if event["method"] == "turn/completed"]
        assert len(completions) == 1 and completions[0]["params"]["turn"]["status"] == "interrupted"
        assert not coordinator.pending and ("thread-1", turn_id) not in instance._async_batches
    finally:
        gates["slow-1"].set()
