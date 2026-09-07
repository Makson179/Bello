from __future__ import annotations

import asyncio
import json

import pytest

from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.sandbox import SandboxResult
from supervisor.runtime.tools import ToolHost, ToolScope


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    journal = RuntimeJournal(tmp_path / "state")
    scopes, approvals, executions, events = {}, [], [], []
    scopes[("thread", "turn")] = ToolScope(root, "workspace-write")

    class Runner:
        def __init__(self, policy):
            self.policy = policy

        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            executions.append((self.policy, command, cwd))
            if on_output:
                await on_output("ok\n")
            return SandboxResult("ok\n", 0, .01)

    async def approve(method, params):
        approvals.append((method, params))
        return params["command"] == "approved-command"

    async def emit(message):
        events.append(message)

    async def delegate(*_args):
        raise AssertionError("unexpected delegation")

    tools = ToolHost(journal, lambda thread, turn: scopes[(thread, turn)], approve, emit, delegate, runner_factory=Runner)
    yield tools, root, scopes, approvals, executions, events
    journal.close()


async def call(host, name="exec_command", arguments=None, call_id="call"):
    return await host[0].call({"threadId": "thread", "turnId": "turn", "callId": call_id,
                               "name": name, "arguments": arguments or {"command": "python tests.py"}})


@pytest.mark.asyncio
async def test_normal_commands_remain_contained_without_new_approval_round_trip(host):
    result = await call(host)
    assert not result["isError"]
    assert host[3] == []
    assert host[4][0][0].mode == "workspace-write"
    assert [event["method"] for event in host[5]] == [
        "item/started", "item/commandExecution/outputDelta", "item/completed"]


@pytest.mark.asyncio
async def test_provider_call_id_can_repeat_in_another_turn_without_replaying_first(host):
    first = await call(host, call_id="provider-local-id")
    assert await call(host, call_id="provider-local-id") == first
    host[2][("thread", "next-turn")] = host[2][("thread", "turn")]
    await host[0].call({"threadId": "thread", "turnId": "next-turn", "callId": "provider-local-id",
                       "name": "exec_command", "arguments": {"command": "a different legitimate command"}})
    assert len(host[4]) == 2
    starts = [event["params"]["itemId"] for event in host[5] if event["method"] == "item/started"]
    assert len(set(starts)) == 2


@pytest.mark.asyncio
async def test_never_policy_allows_contained_review_commands_but_never_escalates(host):
    host[2][("thread", "turn")] = ToolScope(host[1], "read-only", approval_policy="never")
    await call(host)
    denied = await call(host, arguments={"command": "approved-command", "sandbox_permissions": "require_escalated"}, call_id="escape")
    assert denied["isError"]
    assert host[3] == []
    assert len(host[4]) == 1
    assert host[4][0][0].mode == "read-only"


@pytest.mark.asyncio
async def test_escalation_needs_exact_approval_and_does_not_change_later_scope(host):
    denied = await call(host, arguments={"command": "denied-command", "sandbox_permissions": "require_escalated"})
    assert denied["isError"]
    assert not host[4]
    accepted = await call(host, arguments={"command": "approved-command", "sandbox_permissions": "require_escalated"}, call_id="approved")
    assert not accepted["isError"]
    await call(host, call_id="ordinary")
    assert [run[0].mode for run in host[4]] == ["danger-full-access", "workspace-write"]
    assert host[2][("thread", "turn")].mode == "workspace-write"


@pytest.mark.asyncio
async def test_scope_is_rechecked_after_an_approval(host):
    async def approve(*_args):
        host[2].clear()
        return True
    host[0].approve = approve
    result = await call(host, arguments={"command": "approved-command", "sandbox_permissions": "require_escalated"})
    assert result["isError"]
    assert not host[4]


@pytest.mark.asyncio
async def test_completed_tool_call_cannot_execute_twice(host):
    first, second = await call(host), await call(host)
    assert first == second
    assert len(host[4]) == 1


@pytest.mark.asyncio
async def test_same_tool_identity_cannot_execute_different_command(host):
    await call(host)
    with pytest.raises(Exception, match="different"):
        await call(host, arguments={"command": "something-else"})
    assert len(host[4]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "../outside.txt"}),
    ("write_file", {"path": "../outside.txt", "content": "bad"}),
    ("read_file", {"path": ".env"}),
    ("exec_command", {"command": "true", "cwd": ".."}),
])
async def test_tools_cannot_expand_assigned_file_scope(host, name, args):
    assert (await call(host, name, args))["isError"]
    assert not host[4]


@pytest.mark.asyncio
async def test_readonly_scope_rejects_write_before_execution(host):
    host[2][("thread", "turn")] = ToolScope(host[1], "read-only")
    assert (await call(host, "write_file", {"path": "a.txt", "content": "x"}))["isError"]
    assert not host[4]


@pytest.mark.asyncio
async def test_contained_file_write_uses_trusted_resolved_helper(host):
    result = await call(host, "write_file", {"path": "a.txt", "content": "x"})
    assert not result["isError"]
    assert not host[3]
    assert host[4][0][0].mode == "workspace-write"
    assert ".venv/bin/python" not in host[4][0][1]


@pytest.mark.asyncio
async def test_cancelled_tool_remains_uncertain_not_replayed(host):
    started = asyncio.Event()
    class BlockingRunner:
        def __init__(self, policy):
            pass
        async def run(self, *_args, **_kwargs):
            started.set()
            await asyncio.Future()
    host[0].runner_factory = BlockingRunner
    task = asyncio.create_task(call(host))
    await started.wait()
    await host[0].cancel_turn("thread")
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(Exception, match="uncertain"):
        await call(host)


@pytest.mark.asyncio
async def test_unknown_tools_and_malformed_arguments_never_execute(host):
    with pytest.raises(ValueError, match="unknown tool"):
        await call(host, "native_shell", {"command": "oops"})
    with pytest.raises(Exception, match="Additional properties"):
        await call(host, arguments={"command": "true", "model": "other"})
    assert not host[4]


@pytest.mark.asyncio
async def test_command_model_budget_does_not_truncate_controller_evidence(host):
    output = "HEADER\n" + "message\n" * 3500 + "FINAL ERROR\n"
    class Runner:
        def __init__(self, policy):
            pass
        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            await on_output(output)
            return SandboxResult(output, 1, .1)
    host[0].runner_factory = Runner
    result = await call(host)
    packet = json.loads(result["content"][0]["text"])
    assert packet["outputBudget"]["truncated"]
    assert "HEADER" not in packet["output"]
    assert "FINAL ERROR" in packet["output"]
    assert host[5][-1]["params"]["item"]["aggregatedOutput"] == output
    assert packet["exitCode"] == 1


@pytest.mark.asyncio
async def test_file_output_is_decoded_before_line_budget_and_pagination(host):
    output = "".join(f"{line}: source\n" for line in range(1, 3001))
    raw = json.dumps({"text": output, "offset": 1, "returned_lines": 3000, "total_lines": 3000})
    class Runner:
        def __init__(self, policy):
            pass
        async def run(self, command, cwd, timeout, on_output=None):
            return SandboxResult(raw, 0, .1)
    host[0].runner_factory = Runner
    result = await call(host, "read_file", {"path": "source.txt", "limit": 3000})
    assert result["content"][0]["text"].startswith("1: source\n")
    assert "offset=2001" in result["content"][0]["text"]
    assert result["details"]["outputBudget"]["nextOffset"] == 2001
    assert host[5][-1]["params"]["item"]["aggregatedOutput"] == raw


@pytest.mark.asyncio
async def test_yielded_command_has_model_visible_handle_and_one_terminal_event(host):
    class Runner:
        def __init__(self, policy):
            pass
        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            await on_output("server ready\n")
            await cancel_event.wait()
            return SandboxResult("server ready\n", 130, .1, cancelled=True)
    host[0].runner_factory = Runner
    first = await call(host, arguments={"command": "long-running-server", "yield_time_ms": 0})
    packet = json.loads(first["content"][0]["text"])
    assert packet["status"] == "running"
    assert not any(event["method"] == "item/completed" for event in host[5])
    polled = await call(host, "poll_command", {"session_id": packet["sessionId"]}, call_id="poll")
    assert json.loads(polled["content"][0]["text"])["status"] == "running"
    stopped = await call(host, "stop_command", {"session_id": packet["sessionId"]}, call_id="stop")
    assert not stopped["isError"]
    assert json.loads(stopped["content"][0]["text"])["status"] == "cancelled"
    completed = [event for event in host[5] if event["method"] == "item/completed"]
    assert len(completed) == 1
    assert completed[0]["params"]["item"]["status"] == "interrupted"
