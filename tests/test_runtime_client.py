from __future__ import annotations

import asyncio
import json

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime.client import RuntimeClient


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.stopped = False

    async def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": params["turnId"], "status": "inProgress"}}
        return {}

    async def stop(self):
        self.stopped = True


async def make_client(tmp_path, *, approval_handler=None):
    root = tmp_path / "project"
    workspace = tmp_path / "snapshot"
    root.mkdir()
    workspace.mkdir()
    pi, claude = FakeBackend(), FakeBackend()
    client = RuntimeClient(cwd=root, backends={"pi": pi, "claude-code": claude}, server_request_handler=approval_handler)
    await client.start()
    return client, workspace, pi, claude


async def start(client, workspace, model="gpt-5.6-sol", **extra):
    response = await client.thread_start({"cwd": str(workspace), "runtimeWorkspaceRoots": [str(workspace)],
        "model": model, "sandbox": "workspace-write", "approvalPolicy": "on-request", **extra})
    return response["thread"]["id"]


@pytest.mark.asyncio
async def test_routes_models_without_changing_billing(tmp_path):
    client, workspace, pi, claude = await make_client(tmp_path)
    try:
        await start(client, workspace)
        await start(client, workspace, "claude-code/claude-sonnet-4-6")
        assert pi.calls[0][1]["provider"] == "openai-codex"
        assert pi.calls[0][1]["model"] == "gpt-5.6-sol"
        assert claude.calls[0][1]["provider"] == "claude-code"
        assert not any(t["name"] == "spawn_agent" for t in pi.calls[0][1]["tools"])
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_thread_lifecycle_notifications_are_emitted_once_by_host(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    received = []

    async def notify(message):
        received.append(message)

    client.notification_handler = notify
    try:
        thread = await start(client, workspace)
        await client._emit({"method": "thread/started", "params": {"thread": {"id": thread}}}, engine="pi")
        await client.request("thread/archive", {"threadId": thread})
        await client._emit({"method": "thread/closed", "params": {"threadId": thread}}, engine="pi")
        assert [message.method for message in received] == ["thread/started", "thread/closed"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_failed_cleanup_is_reported_after_other_engines_are_stopped(tmp_path):
    client, workspace, pi, claude = await make_client(tmp_path)
    await start(client, workspace)

    async def fail():
        raise RuntimeError("transport did not terminate")

    pi.stop = fail
    with pytest.raises(AppServerError, match="cleanup failed for pi"):
        await client.stop()
    assert claude.stopped
    assert client._journal is None
    assert not client._started


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_completed", [False, True])
async def test_engine_failure_fences_turns_and_closes_cross_engine_children_before_notification(tmp_path, parent_completed):
    client, workspace, _, claude = await make_client(tmp_path)
    try:
        parent = await start(client, workspace)
        child = await start(client, workspace, "claude-code/sonnet", parentThreadId=parent)
        parent_turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        child_turn = (await client.turn_start({"threadId": child}))["turn"]["id"]
        if parent_completed:
            await client._emit({"method": "turn/completed", "params": {
                "threadId": parent, "turn": {"id": parent_turn, "status": "completed"},
            }}, engine="pi")
        observed = []

        async def notify(error):
            observed.append(error)
            assert "activeTurnId" not in client._threads[parent]
            assert client._threads[child]["closed"]

        client.transport_error_handler = notify
        error = AppServerError("Pi stream failed")
        await client._engine_failed("pi", error)
        assert observed == [error]
        if not parent_completed:
            assert client._threads[parent]["interruptedTurnId"] == parent_turn
        assert ("turn/interrupt", {"threadId": child, "turnId": child_turn}) in claude.calls
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["turn/interrupt", "thread/archive", "stop"])
async def test_cancelled_cleanup_finishes_before_returning_to_caller(tmp_path, operation):
    client, workspace, pi, claude = await make_client(tmp_path)
    thread = await start(client, workspace)
    turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = pi.request

    async def slow_request(method, params, timeout=30):
        if method == "turn/interrupt":
            entered.set()
            await release.wait()
            finished.set()
        return await original(method, params, timeout)

    async def slow_stop():
        entered.set()
        await release.wait()
        pi.stopped = True
        finished.set()

    pi.request, pi.stop = slow_request, slow_stop
    task = asyncio.create_task(client.stop() if operation == "stop" else client.request(
        operation, {"threadId": thread, "turnId": turn}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert "activeTurnId" not in client._threads[thread]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert finished.is_set()
        if operation == "thread/archive":
            assert any(method == "thread/archive" for method, _ in pi.calls)
        if operation == "stop":
            assert pi.stopped and claude.stopped
            assert client._journal is None
            assert not client._started
    finally:
        release.set()
        await client.stop()


@pytest.mark.asyncio
async def test_optional_catalog_failure_does_not_hide_other_engine(tmp_path):
    client, _, pi, claude = await make_client(tmp_path)

    async def list_pi(method, params, timeout=30):
        return {"data": [{"id": "gpt-5.6-sol", "qualifiedId": "openai-codex/gpt-5.6-sol"}]}

    async def missing_claude(method, params, timeout=30):
        raise AppServerError("Claude Code is not signed in")

    pi.request, claude.request = list_pi, missing_claude
    try:
        result = await client.request("model/list", {"engines": ["pi", "claude-code"], "optionalEngines": True})
        assert result["data"][-1]["id"] == "openai-codex/gpt-5.6-sol"
        assert "claude-code" in result["unavailableEngines"]
        with pytest.raises(AppServerError, match="not signed in"):
            await client.request("model/list", {"engines": ["claude-code"]})
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["turn/interrupt", "thread/archive"])
async def test_one_failed_child_cleanup_cannot_skip_parent_or_siblings(tmp_path, operation):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        parent = await start(client, workspace)
        child = await start(client, workspace, parentThreadId=parent)
        sibling = await start(client, workspace, parentThreadId=parent)
        turns = {thread: (await client.turn_start({"threadId": thread}))["turn"]["id"]
                 for thread in (parent, child, sibling)}
        original = pi.request

        async def fail_child(method, params, timeout=30):
            response = await original(method, params, timeout)
            if method == "thread/archive" and params["threadId"] == child:
                raise AppServerError("child archive failed")
            return response

        pi.request = fail_child
        pi.calls.clear()
        with pytest.raises(AppServerError, match="cleanup attempts were made"):
            await client.request(operation, {"threadId": parent, "turnId": turns[parent]})
        assert ("turn/interrupt", {"threadId": parent, "turnId": turns[parent]}) in pi.calls
        assert any(method == "thread/archive" and params["threadId"] == sibling for method, params in pi.calls)
        if operation == "thread/archive":
            assert any(method == operation and params["threadId"] == parent for method, params in pi.calls)
            assert client._threads[parent]["closed"]
        assert all(not client._threads[thread].get("activeTurnId") for thread in turns)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_rejected_turn_keeps_previous_effort_and_next_turn_sends_it_explicitly(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace, effort="high")
        original = pi.request

        async def reject(method, params, timeout=30):
            if method == "turn/start" and params.get("effort") == "ultra":
                raise AppServerError("unsupported effort")
            return await original(method, params, timeout)

        pi.request = reject
        with pytest.raises(AppServerError, match="unsupported effort"):
            await client.turn_start({"threadId": thread, "effort": "ultra"})
        assert client._threads[thread]["effort"] == "high"
        await client.turn_start({"threadId": thread})
        assert pi.calls[-1][1]["effort"] == "high"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_engine_default_effort_is_recorded_but_explicit_profile_cannot_be_changed(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    original = pi.request

    async def resolved(method, params, timeout=30):
        response = await original(method, params, timeout)
        if method == "thread/start":
            response["thread"]["reasoningEffort"] = "medium"
        return response

    pi.request = resolved
    try:
        thread = await start(client, workspace)
        assert client._public_thread(thread)["reasoningEffort"] == "medium"
        await client.turn_start({"threadId": thread})
        assert pi.calls[-1][1]["effort"] == "medium"
        with pytest.raises(AppServerError, match="changed the requested"):
            await start(client, workspace, effort="high")
        assert pi.calls[-1][0] == "thread/archive"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_rejects_workspace_with_private_runtime_state(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        with pytest.raises(AppServerError, match="private runtime"):
            await start(client, client.cwd)
        assert not pi.calls
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_active_turn_ownership_and_scope_are_host_owned(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        with pytest.raises(AppServerError, match="weaken"):
            await client.turn_start({"threadId": thread, "sandboxPolicy": {"type": "dangerFullAccess"}})
        with pytest.raises(AppServerError, match="expand"):
            await client.turn_start({"threadId": thread, "runtimeWorkspaceRoots": [str(tmp_path)]})
        turn = (await client.turn_start({"threadId": thread, "input": [{"type": "text", "text": "task"}]}))["turn"]["id"]
        assert client._scope_for(thread, turn).root == workspace
        with pytest.raises(AppServerError, match="already active"):
            await client.turn_start({"threadId": thread})
        with pytest.raises(AppServerError, match="stale"):
            client._scope_for(thread, "old-turn")
        await client.turn_interrupt(thread, turn)
        with pytest.raises(AppServerError, match="stale"):
            client._scope_for(thread, turn)
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_completed_notification_cannot_be_lost_before_waiter(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        await client._emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}})
        message = await client.wait_for_notification(lambda m: m.method == "turn/completed" and m.params["turn"]["id"] == turn, timeout=.01)
        assert message.params["threadId"] == thread
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_approval_denies_without_handler(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        assert await client._approve("item/commandExecution/requestApproval", {"command": "true", "cwd": str(workspace)}) is False
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_approval_round_trip_does_not_deadlock(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    async def handler(message):
        await client.respond(message.request_id, {"decision": "accept"})
    client.server_request_handler = handler
    try:
        assert await client._approve("item/commandExecution/requestApproval", {"command": "true", "cwd": str(workspace)}) is True
        assert not client._approval_waiters
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_cross_provider_children_have_same_scope_and_enforced_profile(tmp_path):
    client, workspace, pi, claude = await make_client(tmp_path)
    try:
        thread = await start(client, workspace, config={"agents": {"enabled": True, "max_concurrent_threads_per_session": 1,
            "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"]}}})
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        with pytest.raises(AppServerError, match="allowed profile"):
            await client._delegate("spawn_agent", {"model": "claude-code/claude-sonnet-4-6", "effort": "max", "message": "task"}, thread, turn)
        result = await client._delegate("spawn_agent", {"model": "claude-code/claude-sonnet-4-6", "effort": "high", "message": "task"}, thread, turn)
        child = json.loads(result["content"][0]["text"])["agent_id"]
        assert client._threads[child]["cwd"] == str(workspace)
        assert client._threads[child]["sandbox"] == "workspace-write"
        assert client._threads[child]["config"]["agents"]["enabled"]
        assert claude.calls[-1][0] == "turn/start"
        await client.turn_interrupt(thread, turn)
        assert client._threads[child]["closed"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_unrelated_agent_cannot_control_child(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        owner = await start(client, workspace, config={"agents": {"enabled": True}})
        other = await start(client, workspace)
        turn = (await client.turn_start({"threadId": owner}))["turn"]["id"]
        with pytest.raises(AppServerError, match="own children"):
            await client._delegate("close_agent", {"agent_id": other}, owner, turn)
        assert not client._threads[other]["closed"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_backend_cannot_send_events_for_a_different_engine(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        with pytest.raises(AppServerError, match="foreign thread"):
            await client._emit({"method": "item/completed", "params": {"threadId": thread, "turnId": turn}}, engine="claude-code")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_late_events_cannot_change_next_turn_or_readiness(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    received = []
    async def notify(message):
        received.append(message)
    client.notification_handler = notify
    try:
        thread = await start(client, workspace)
        old = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        await client.turn_interrupt(thread, old)
        current = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        received.clear()
        await client._emit({"method": "item/completed", "params": {"threadId": thread, "turnId": old,
            "item": {"type": "agentMessage", "text": "all done"}}})
        await client._emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": old, "status": "completed"}}})
        assert not received
        assert client._threads[thread]["activeTurnId"] == current
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_uncertain_turn_start_is_fenced_and_interrupted_not_replayed(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        original = pi.request
        async def uncertain(method, params, timeout=30):
            result = await original(method, params, timeout)
            if method == "turn/start":
                raise asyncio.TimeoutError("reply lost after dispatch")
            return result
        pi.request = uncertain
        with pytest.raises(asyncio.TimeoutError):
            await client.turn_start({"threadId": thread})
        assert not client._threads[thread].get("activeTurnId")
        methods = [method for method, _ in pi.calls]
        assert methods.count("turn/start") == 1
        assert methods[-1] == "turn/interrupt"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_uncertain_thread_start_is_closed_without_replaying_creation(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    original = pi.request

    async def uncertain(method, params, timeout=30):
        result = await original(method, params, timeout)
        if method == "thread/start":
            raise asyncio.TimeoutError("created session but acknowledgement was lost")
        return result

    pi.request = uncertain
    try:
        with pytest.raises(asyncio.TimeoutError):
            await start(client, workspace)
        assert [method for method, _ in pi.calls] == ["thread/start", "thread/archive"]
        assert pi.calls[0][1]["threadId"] == pi.calls[1][1]["threadId"]
        assert all(record["closed"] for record in client._threads.values())
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_failed_child_turn_start_closes_the_new_child(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        parent = await start(client, workspace, config={"agents": {"enabled": True,
            "allowed_profiles": {"gpt-5.6-luna": ["high"]}}})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        original = pi.request

        async def reject_child(method, params, timeout=30):
            result = await original(method, params, timeout)
            if method == "turn/start" and params["threadId"] != parent:
                raise AppServerError("child provider rejected the request")
            return result

        pi.request = reject_child
        with pytest.raises(AppServerError, match="child provider rejected"):
            await client._delegate("spawn_agent", {"model": "gpt-5.6-luna", "effort": "high", "message": "inspect"}, parent, turn)
        child = next(key for key, record in client._threads.items() if record.get("parentThreadId") == parent)
        assert client._threads[child]["closed"]
        assert not client._threads[child].get("activeTurnId")
        assert pi.calls[-1] == ("thread/archive", {"threadId": child})
        assert client._threads[parent]["activeTurnId"] == turn
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_fast_completion_is_reported_in_start_reply(tmp_path):
    client, workspace, pi, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        original = pi.request
        async def immediate(method, params, timeout=30):
            result = await original(method, params, timeout)
            if method == "turn/start":
                await client._emit({"method": "turn/completed", "params": {"threadId": thread,
                    "turn": {"id": params["turnId"], "status": "completed"}}})
            return result
        pi.request = immediate
        result = await client.turn_start({"threadId": thread})
        assert result["turn"]["status"] == "completed"
        assert not client._threads[thread].get("activeTurnId")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_public_thread_list_keeps_identity_activity_and_effort_without_private_config(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        thread = await start(client, workspace, "claude-code/claude-sonnet-4-6", developerInstructions="private role text")
        await client.turn_start({"threadId": thread, "effort": "high"})
        public = (await client.thread_list())["data"][0]
        assert public["model"] == "claude-code/claude-sonnet-4-6"
        assert public["status"] == "active"
        assert public["reasoningEffort"] == "high"
        assert "developerInstructions" not in public
        assert "tools" not in public
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_reviewer_children_cannot_delegate_but_idle_children_do_not_exhaust_concurrency(tmp_path):
    client, workspace, _, _ = await make_client(tmp_path)
    try:
        parent = await start(client, workspace, config={"agents": {"enabled": True, "role": "completion_review",
            "max_concurrent_threads_per_session": 1, "allowed_profiles": {"gpt-5.6-luna": ["high"]}}})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        args = {"model": "gpt-5.6-luna", "effort": "high", "message": "inspect"}
        first = await client._delegate("spawn_agent", args, parent, turn)
        child = json.loads(first["content"][0]["text"])["agent_id"]
        child_turn = client._threads[child]["activeTurnId"]
        assert not client._threads[child]["config"]["agents"]["enabled"]
        with pytest.raises(AppServerError, match="disabled"):
            await client._delegate("spawn_agent", args, child, child_turn)
        with pytest.raises(AppServerError, match="concurrency"):
            await client._delegate("spawn_agent", args, parent, turn)
        await client._emit({"method": "turn/completed", "params": {"threadId": child, "turn": {"id": child_turn, "status": "completed"}}})
        second = await client._delegate("spawn_agent", args, parent, turn)
        assert second != first
        with pytest.raises(AppServerError, match="concurrency"):
            await client._delegate("send_message", {"agent_id": child, "message": "one more check"}, parent, turn)
        assert not client._threads[child].get("activeTurnId")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stop_persists_interruption_without_replaying_turn(tmp_path):
    from supervisor.runtime.journal import RuntimeJournal
    client, workspace, _, _ = await make_client(tmp_path)
    thread = await start(client, workspace)
    turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
    await client.stop()
    journal = RuntimeJournal(client.state_dir)
    try:
        record = journal.threads()[thread]
        assert "activeTurnId" not in record
        assert record["interruptedTurnId"] == turn
    finally:
        journal.close()
