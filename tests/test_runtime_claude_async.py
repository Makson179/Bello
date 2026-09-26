from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from supervisor.runtime.claude import ClaudeBackend, _MCP_REQUEST_CONTEXT
from supervisor.runtime.claude_async import BATCH_TOOL_NAME, ClaudeAsyncBatches


def output(text):
    return {"content": [{"type": "text", "text": text}], "isError": False}


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
