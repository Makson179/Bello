from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from supervisor.appserver import AppServerError, AppServerTimeoutError
from supervisor.config_editor import parameter_defs
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import ProjectConfig, load_project_config, save_project_config
from supervisor.runtime.async_tools import ASYNC_TOOLS_GUIDANCE
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.command_sessions import CommandSessionManager
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.tools import ToolHost, ToolScope, tool_definitions
from tests.test_runtime_client import FakeBackend, start
from tests.test_runtime_command_sessions import QueueRunner, THREAD, TURN, CALL


MODELS = ("gpt-5.6-sol", "claude-code/claude-sonnet-4-6", "openai/gpt-5.6-sol")


@pytest.fixture
def policy_distiller(monkeypatch):
    """Exercise the public switch without weights, worker startup, or network."""
    from supervisor.runtime import distiller
    spy = SimpleNamespace(distill=AsyncMock(return_value="selected"), close=AsyncMock())
    monkeypatch.setattr(distiller, "validate_bundle", lambda _path: None)
    monkeypatch.setattr(distiller, "require_dependencies", lambda: None)
    monkeypatch.setattr(distiller, "LogDistiller", lambda _path: spy)
    return spy


def test_async_switch_defaults_roundtrip_editor_and_cli(tmp_path):
    config = ProjectConfig()
    assert config.async_tools is False
    enabled = replace(config, async_tools=True)
    save_project_config(tmp_path, enabled)
    assert load_project_config(tmp_path, create=False).async_tools is True
    assert _resolve_run_settings(project_config=enabled).async_tools is True
    assert _resolve_run_settings(project_config=enabled, async_tools=False).async_tools is False
    parameter = next(p for p in parameter_defs(config) if p.key == "async_tools")
    assert [option.value for option in parameter.options] == [True, False]
    option = next(p for p in cli.params if p.name == "async_tools")
    assert option.opts == ["--async-tools"] and option.secondary_opts == ["--no-async-tools"]


@pytest.mark.asyncio
async def test_model_validation_excludes_runtime_only_async_capability(tmp_path):
    backend = FakeBackend()
    client = RuntimeClient(cwd=tmp_path, backends={"codex": backend})
    client.configure_run(async_tools=True)
    try:
        for role in ("runtime", "coder", "completion_review", "adversary"):
            await client.request("model/validate", {"model": "gpt-5.6-sol", "belloRole": role})
            assert backend.calls[-1][1]["asyncTools"] is (role != "runtime")
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("runtime", [False, True])
@pytest.mark.parametrize("model", ["gpt-5.6-sol", "claude-code/claude-sonnet-4-6", "openai/gpt-5.6-sol"])
async def test_async_policy_reaches_all_roles_and_survives_resume(tmp_path, enabled, runtime, model):
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    client = RuntimeClient(cwd=root, backends={key: FakeBackend() for key in ("codex", "claude-code", "pi")})
    client.configure_run(runtime_enabled=runtime, async_tools=enabled)
    try:
        for role in ("coder", "completion_review", "adversary", "runtime"):
            thread = await start(client, workspace, model, belloRole=role, developerInstructions="Original instructions.")
            record = client._threads[thread]
            active = enabled and role != "runtime"
            assert record["asyncTools"] is active
            assert (ASYNC_TOOLS_GUIDANCE in record["developerInstructions"]) is active
            assert record["distillerEnabled"] is False
            await client.request("thread/resume", {"threadId": thread})
            backend = client._engines[record["engine"]]
            assert backend.calls[-1][1]["asyncTools"] is active
            with pytest.raises(AppServerError, match="async tools policy"):
                await client.request("thread/resume", {"threadId": thread, "asyncTools": not active})
        with pytest.raises(AppServerError, match="stopping"):
            client.configure_run(runtime_enabled=runtime, async_tools=not enabled)
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coder", "completion_review", "adversary"])
@pytest.mark.parametrize("parent_model", MODELS)
@pytest.mark.parametrize("enabled", [False, True])
async def test_children_inherit_async_across_engines_and_revision(
    tmp_path, role, parent_model, enabled, policy_distiller,
):
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    client = RuntimeClient(cwd=root, backends={key: FakeBackend() for key in ("codex", "claude-code", "pi")})
    client.configure_run(async_tools=enabled, log_distiller={
        "enabled": True, "model_path": str(tmp_path / "offline-bundle"),
    })
    try:
        parent = await start(client, workspace, parent_model, belloRole=role, config={"agents": {
            "enabled": True, "role": role, "max_concurrent_threads_per_session": 3,
            "allowed_profiles": {model: ["high"] for model in MODELS},
        }})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        for model in MODELS:
            reply = await client._delegate("spawn_agent", {"model": model, "effort": "high", "message": "inspect"}, parent, turn)
            child = json.loads(reply["content"][0]["text"])["agent_id"]
            record = client._threads[child]
            assert record["asyncTools"] is enabled
            assert record.get("developerInstructions", "").count(ASYNC_TOOLS_GUIDANCE) == int(enabled)
            assert record["distillerEnabled"] is (role == "coder")
            assert record["belloRole"] == role
            assert record["config"]["agents"]["enabled"] is (role == "coder")
            scope = client._scope_for(child, record["activeTurnId"])
            assert scope.async_tools is enabled
            assert scope.distiller_enabled is (role == "coder")
            # Finish and resume the actual child, not just a newly made parent.
            await client._emit({"method": "turn/completed", "params": {
                "threadId": child, "turn": {"id": record["activeTurnId"], "status": "completed"},
            }})
            await client.request("thread/resume", {"threadId": child})
            routed = client._engines[record["engine"]].calls[-1][1]
            assert routed["asyncTools"] is enabled
            assert routed["developerInstructions"].count(ASYNC_TOOLS_GUIDANCE) == int(enabled)
            continued = (await client.turn_start({"threadId": child}))["turn"]["id"]
            scope = client._scope_for(child, continued)
            assert (scope.async_tools, scope.distiller_enabled) == (enabled, role == "coder")
        revision = await start(client, workspace, parent_model, belloRole=role)
        assert client._threads[revision]["asyncTools"] is enabled
        assert client._threads[revision]["distillerEnabled"] is (role == "coder")
        policy_distiller.distill.assert_not_awaited()
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coder", "completion_review", "adversary"])
@pytest.mark.parametrize("parent_model", MODELS)
@pytest.mark.parametrize("operation", ["turn/interrupt", "thread/archive"])
async def test_parent_cleanup_stops_mixed_provider_child_and_queued_tools(
    tmp_path, role, parent_model, operation,
):
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    client = RuntimeClient(cwd=root, backends={key: FakeBackend() for key in ("codex", "claude-code", "pi")})
    client.configure_run(async_tools=True)
    child_model = MODELS[(MODELS.index(parent_model) + 1) % len(MODELS)]
    runner = QueueRunner()
    created = []

    def factory(_policy):
        created.append(runner)
        return runner

    calls = []
    try:
        parent = await start(client, workspace, parent_model, belloRole=role, config={"agents": {
            "enabled": True, "role": role, "max_concurrent_threads_per_session": 1,
            "allowed_profiles": {child_model: ["high"]},
        }})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        reply = await client._delegate("spawn_agent", {
            "model": child_model, "effort": "high", "message": "inspect",
        }, parent, turn)
        child = json.loads(reply["content"][0]["text"])["agent_id"]
        child_turn = client._threads[child]["activeTurnId"]
        assert client._threads[child]["engine"] != client._threads[parent]["engine"]
        client._host.runner_factory = factory
        client._host.sessions.max_active_per_turn = 1
        calls = [asyncio.create_task(client._call_tool({
            "threadId": child, "turnId": child_turn, "callId": f"child-{index}",
            "name": "exec_command", "arguments": {"command": "tests"},
        })) for index in range(2)]
        await asyncio.wait_for(runner.started.wait(), 1)
        await asyncio.sleep(0)
        assert len(created) == 1
        await asyncio.wait_for(client.request(operation, {"threadId": parent, "turnId": turn}), 1)
        outcomes = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 1)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert len(created) == 1, "The queued command must not start during child cleanup"
        assert client._threads[child]["closed"]
        assert not client._threads[child].get("activeTurnId")
        assert not client._host._active
        child_calls = client._engines[client._threads[child]["engine"]].calls
        assert any(method == "turn/interrupt" and args["threadId"] == child for method, args in child_calls)
        assert any(method == "thread/archive" and args["threadId"] == child for method, args in child_calls)
        with pytest.raises(AppServerError, match="inactive or stale"):
            client._scope_for(child, child_turn)
    finally:
        await client.stop()
        await asyncio.gather(*calls, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("enabled", [False, True])
async def test_completion_cycle_preserves_coder_resume_and_fresh_revision_policy(
    tmp_path, model, enabled, policy_distiller,
):
    from supervisor.schemas import BelloConfig
    from supervisor.state import StateStore
    from supervisor.supervisor_agent import StatelessSupervisorAgent
    from tests.test_reviewer_async_roles import DecisionBackend

    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    task = workspace / "TASK.md"
    task.write_text("Implement the requested behavior.\n")
    store = StateStore(workspace)
    store.initialize_bello(BelloConfig(project_root=str(workspace), task_path=str(task)), overwrite=True)
    backends = {name: DecisionBackend() for name in ("codex", "claude-code", "pi")}
    client = RuntimeClient(cwd=root, backends=backends)
    client.configure_run(async_tools=enabled, log_distiller={
        "enabled": True, "model_path": str(tmp_path / "offline-bundle"),
    })
    agent = StatelessSupervisorAgent(client, store, task, model=model)

    async def finish(thread):
        # The fake provider returns a completed RPC result but does not emit
        # native notifications; provide the same terminal event explicitly.
        await client._emit({"method": "turn/completed", "params": {
            "threadId": thread,
            "turn": {"id": client._threads[thread]["activeTurnId"], "status": "completed"},
        }})

    try:
        coder = await start(client, workspace, model, belloRole="coder")
        await client.turn_start({"threadId": coder})
        await finish(coder)
        reviewer_ids = []
        for wake in (7, 8):
            await agent.decide_completion(agent.build_packet(wake_sequence=wake, current_summary="check"))
            reviewer = agent.completion_thread_id
            reviewer_ids.append(reviewer)
            assert client._threads[reviewer]["asyncTools"] is enabled
            assert client._threads[reviewer]["distillerEnabled"] is False
            await finish(reviewer)
            await client.request("thread/resume", {"threadId": coder})
            turn = (await client.turn_start({"threadId": coder}))["turn"]["id"]
            scope = client._scope_for(coder, turn)
            assert scope.async_tools is enabled
            assert scope.distiller_enabled is True
            await finish(coder)
        assert reviewer_ids[0] == reviewer_ids[1], "A repeated review should retain its own thread"
        await agent.close_completion_review()
        revision = await start(client, workspace, model, belloRole="coder")
        revision_turn = (await client.turn_start({"threadId": revision}))["turn"]["id"]
        scope = client._scope_for(revision, revision_turn)
        assert scope.async_tools is enabled
        assert scope.distiller_enabled is True
        assert client._threads[revision]["developerInstructions"].count(ASYNC_TOOLS_GUIDANCE) == int(enabled)
        policy_distiller.distill.assert_not_awaited()
    finally:
        await agent.close_completion_review()
        await client.stop()


@pytest.mark.asyncio
async def test_command_waits_for_terminal_not_intermediate_output(tmp_path):
    manager = CommandSessionManager(tmp_path / "state")
    runner = QueueRunner()
    operation = asyncio.create_task(manager.start(thread_id=THREAD, turn_id=TURN, call_id=CALL,
        runner=runner, command="tests", cwd=tmp_path, timeout=120, yield_time_ms=0, wait_for_completion=True))
    try:
        await runner.started.wait()
        runner.emit("first\n")
        await asyncio.sleep(0.01)
        assert not operation.done()
        runner.emit("last\n")
        runner.finish()
        result = await asyncio.wait_for(operation, 1)
        assert result["status"] == "completed"
        assert result["output"] == "first\nlast\n"
        assert runner.calls == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_command_cancellation_stops_owned_process(tmp_path):
    manager = CommandSessionManager(tmp_path / "state")
    runner = QueueRunner()
    operation = asyncio.create_task(manager.start(thread_id=THREAD, turn_id=TURN, call_id=CALL,
        runner=runner, command="tests", cwd=tmp_path, timeout=120, wait_for_completion=True))
    try:
        await runner.started.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        result = await manager.poll(thread_id=THREAD, turn_id=TURN, session_id=manager.session_id(THREAD, TURN, CALL))
        assert result["status"] == "cancelled"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_host_distills_once_after_complete_output(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    journal = RuntimeJournal(tmp_path / "state")
    runner = QueueRunner()
    distilled = []
    async def ignore(*args):
        return True
    async def distill(text, focus, command):
        distilled.append((text, focus))
        return "selected"
    host = ToolHost(journal, lambda *_: ToolScope(root, "workspace-write", async_tools=True,
        distiller_enabled=True), ignore, ignore, ignore, runner_factory=lambda _: runner, distill=distill)
    operation = asyncio.create_task(host.call({"threadId": THREAD, "turnId": TURN, "callId": CALL,
        "name": "exec_command", "arguments": {"command": "run tests", "focus": "test errors", "yield_time_ms": 0}}))
    try:
        await runner.started.wait()
        runner.emit("first line\n")
        await asyncio.sleep(0.01)
        assert not operation.done() and not distilled
        runner.emit("error line\n")
        runner.finish(1)
        result = await asyncio.wait_for(operation, 1)
        assert len(distilled) == 1 and distilled[0] == ("first line\nerror line\n", "test errors")
        assert result["details"]["output"] == "selected"
        assert result["details"]["exitCode"] == 1
    finally:
        await host.close()
        journal.close()


def test_off_tool_descriptions_unchanged_and_on_explains_wait():
    before = tool_definitions()
    assert before == tool_definitions(async_tools=False)
    command = next(t for t in tool_definitions(async_tools=True) if t["name"] == "exec_command")
    assert "do not poll" in command["description"]


@pytest.mark.asyncio
async def test_host_queues_excess_commands_without_model_limit_errors(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    journal = RuntimeJournal(tmp_path / "state")
    runners = [QueueRunner(), QueueRunner()]
    created = []
    def factory(_):
        runner = runners[len(created)]
        created.append(runner)
        return runner
    async def ignore(*args):
        return True
    host = ToolHost(journal, lambda *_: ToolScope(root, "workspace-write", async_tools=True),
                    ignore, ignore, ignore, runner_factory=factory)
    host.sessions.max_active_per_turn = 1
    operations = [asyncio.create_task(host.call({"threadId": THREAD, "turnId": TURN, "callId": str(index),
        "name": "exec_command", "arguments": {"command": "tests", "yield_time_ms": 0}})) for index in range(2)]
    try:
        await runners[0].started.wait()
        await asyncio.sleep(0.01)
        assert len(created) == 1 and not any(task.done() for task in operations)
        runners[0].finish()
        await asyncio.wait_for(runners[1].started.wait(), 1)
        runners[1].finish()
        results = await asyncio.wait_for(asyncio.gather(*operations), 1)
        assert all(not result["isError"] for result in results)
    finally:
        await host.close()
        journal.close()


@pytest.mark.asyncio
async def test_child_wait_timeout_is_internal_and_delivery_is_once(tmp_path, monkeypatch):
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    backend = FakeBackend()
    client = RuntimeClient(cwd=root, backends={"codex": backend})
    client.configure_run(async_tools=True)
    try:
        parent = await start(client, workspace, belloRole="coder", config={"agents": {
            "enabled": True, "role": "coder", "max_concurrent_threads_per_session": 2,
            "allowed_profiles": {"gpt-5.6-sol": ["high"]},
        }})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        reply = await client._delegate("spawn_agent", {"model": "gpt-5.6-sol", "effort": "high", "message": "inspect"}, parent, turn)
        child = json.loads(reply["content"][0]["text"])["agent_id"]
        child_turn = client._threads[child]["activeTurnId"]
        waits = []
        async def wait(predicate, *, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise AppServerTimeoutError("not ready yet")
            await client._emit({"method": "turn/completed", "params": {
                "threadId": child, "turn": {"id": child_turn, "status": "completed"}}})
        monkeypatch.setattr(client, "wait_for_notification", wait)
        original_request = backend.request
        async def request(method, params, timeout=30):
            if method == "thread/read":
                return {"thread": {"turns": [{"id": child_turn, "status": "completed", "items": [
                    {"type": "agentMessage", "text": "finding"}]}]}}
            return await original_request(method, params, timeout)
        monkeypatch.setattr(backend, "request", request)
        async def receive():
            packet = await client._delegate("wait_agent", {"agent_id": child, "timeout": 1}, parent, turn)
            return json.loads(packet["content"][0]["text"])
        assert (await receive())["messages"] == ["finding"]
        assert waits == [60, 60]
        assert (await receive())["messages"] == []
        await client._emit({"method": "turn/completed", "params": {
            "threadId": parent, "turn": {"id": turn, "status": "interrupted"}}})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        # There is no backend delivery ACK at host dispatch time. New parent
        # turns replay once instead of losing findings after an interruption.
        assert (await receive())["messages"] == ["finding"]
        assert (await receive())["messages"] == []
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_terminal_event_fences_and_cancels_queued_host_commands(tmp_path, status):
    root, workspace = tmp_path / "project", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    client = RuntimeClient(cwd=root, backends={"codex": FakeBackend()})
    client.configure_run(async_tools=True)
    runner = QueueRunner()
    created = []
    def factory(_):
        created.append(runner)
        return runner
    try:
        thread = await start(client, workspace, belloRole="coder")
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        client._host.sessions.max_active_per_turn = 1
        client._host.runner_factory = factory
        calls = [asyncio.create_task(client._call_tool({"threadId": thread, "turnId": turn,
            "callId": str(index), "name": "exec_command", "arguments": {"command": "tests"}}))
            for index in range(2)]
        await runner.started.wait()
        await asyncio.sleep(0.01)
        assert len(created) == 1
        await asyncio.wait_for(client._emit({"method": "turn/completed", "params": {
            "threadId": thread, "turn": {"id": turn, "status": status}}}), 1)
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
        assert len(created) == 1, "Queued command must never spawn during terminal cleanup"
        assert not client._host._active
        assert not client._threads[thread].get("activeTurnId")
    finally:
        await client.stop()
