"""Goal-map output selection is host-scoped; no provider, process or network calls."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
from unittest.mock import AsyncMock

import pytest

from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.codex import CodexBackend
from supervisor.runtime import codex as codex_module
from supervisor.runtime.codex_distiller import CodexDistillerBridge
from supervisor.runtime.distiller_policy import preserve_tool_output
from supervisor.runtime.output_budget import budget_command_output
from supervisor.runtime.sandbox import SandboxResult
from supervisor.runtime.tools import ToolScope
from tests.test_runtime_distiller_exclusions import harness  # shared fake runner only


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("goal distiller tests cannot spawn processes or use network")
    targets = [(subprocess, "Popen"), (os, "system"), (socket, "create_connection"),
               (asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell")]
    if os.name != "nt":
        # Windows builds an event loop from a connected socket pair, so only POSIX can patch these.
        targets += [(socket.socket, "connect"), (socket.socket, "connect_ex")]
    for owner, name in targets:
        monkeypatch.setattr(owner, name, denied)


@pytest.fixture(autouse=True)
def platform_independent_metadata(monkeypatch):
    """Goal-map metadata is platform independent; keep the native Windows admission gate out of it."""
    monkeypatch.setattr("supervisor.runtime.client._IS_WINDOWS", False)
    monkeypatch.setattr(codex_module, "_IS_WINDOWS", False)


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "GOAL_MAP.md"}),
    ("search", {"path": "./GOAL_MAP.md", "pattern": "attempt"}),
    ("exec_command", {"command": "cat GOAL_MAP.md"}),
    ("exec_command", {"command": "cat ../GOAL_MAP.md", "cwd": "src"}),
    ("exec_command", {"command": "sed -n '1,100p' GOAL_MAP.md; pytest -q"}),
    ("exec_command", {"command": "python -c \"from pathlib import Path; print(Path('GOAL_MAP.md').read_text())\""}),
])
def test_only_host_selected_goal_path_bypasses(tmp_path, name, args):
    goal = tmp_path / "GOAL_MAP.md"  # Deliberately absent: this is not an existence gate.
    assert not preserve_tool_output(name, args, workspace=tmp_path)
    assert preserve_tool_output(name, args, workspace=tmp_path, goal_map_path=goal)


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "notes/GOAL_MAP.md"}),
    ("read_file", {"path": "GOAL_MAP.md.bak"}),
    ("search", {"path": ".", "pattern": "GOAL_MAP.md"}),
    ("exec_command", {"command": "cat notes/GOAL_MAP.md"}),
    ("exec_command", {"command": "printf '%s' GOAL_MAP.md"}),
    ("exec_command", {"command": "cat output.log > GOAL_MAP.md"}),
    ("exec_command", {"command": "sed -i 's/a/b/' GOAL_MAP.md"}),
    ("exec_command", {"command": "git diff -- GOAL_MAP.md"}),
    ("write_file", {"path": "GOAL_MAP.md", "content": "attempt"}),
])
def test_other_paths_and_nonreads_do_not_bypass(tmp_path, name, args):
    assert not preserve_tool_output(name, args, workspace=tmp_path,
                                    goal_map_path=tmp_path / "GOAL_MAP.md")


@pytest.mark.parametrize("name", ["poll_command", "stop_command"])
def test_original_command_and_cwd_preserve_goal_poll(tmp_path, name):
    assert preserve_tool_output(name, {"cwd": "src"}, command="cat ../GOAL_MAP.md",
                                workspace=tmp_path, goal_map_path=tmp_path / "GOAL_MAP.md")
    assert not preserve_tool_output(name, {"cwd": "src"}, command="cat GOAL_MAP.md",
                                    workspace=tmp_path, goal_map_path=tmp_path / "GOAL_MAP.md")


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "GOAL_MAP.md"}),
    ("search", {"path": "GOAL_MAP.md", "pattern": "attempt"}),
    ("exec_command", {"command": "cat GOAL_MAP.md", "max_output_tokens": 100}),
])
async def test_actual_tool_packets_preserve_goal_and_existing_budget(harness, name, args):
    harness.output = "attempt details\n" * 100
    plain = await harness.call(name, {**args, "focus": "Review attempts"}, "goal-off")
    assert len(harness.selector_calls) == 1
    harness.scopes[("coder", "turn")] = ToolScope(
        harness.root, "workspace-write", distiller_enabled=True,
        goal_map_path=harness.root / "GOAL_MAP.md")
    protected = await harness.call(name, {**args, "focus": "Review attempts"}, "goal-on")
    assert not protected["isError"]
    assert len(harness.selector_calls) == 1
    assert protected["content"] != plain["content"]
    if name == "exec_command":
        assert protected["details"]["output"] == budget_command_output(harness.output, 100).text
        assert protected["details"]["outputBudget"]["truncated"]
    elif name == "read_file":
        assert protected["content"][0]["text"] == harness.output


@pytest.mark.parametrize("operation", ["poll_command", "stop_command"])
async def test_actual_yielded_goal_read_keeps_originating_cwd(harness, operation):
    (harness.root / "src").mkdir()
    harness.scopes[("coder", "turn")] = ToolScope(
        harness.root, "workspace-write", distiller_enabled=True,
        goal_map_path=harness.root / "GOAL_MAP.md")
    release = asyncio.Event()
    output = "complete goal attempt evidence\n"

    class Runner:
        def __init__(self, _policy):
            pass

        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            if operation == "stop_command":
                await cancel_event.wait()
            else:
                await release.wait()
            await on_output(output)
            stopped = operation == "stop_command"
            return SandboxResult(output, 130 if stopped else 0, .1, cancelled=stopped)

    harness.host.runner_factory = Runner
    try:
        initial = await harness.call("exec_command", {
            "command": "cat ../GOAL_MAP.md", "cwd": "src", "yield_time_ms": 0,
            "focus": "Read attempts",
        }, "initial")
        assert initial["details"]["status"] == "running"
        release.set()
        args = {"session_id": initial["details"]["sessionId"], "focus": "Read attempts"}
        if operation == "poll_command":
            args["yield_time_ms"] = 1000
        result = await harness.call(operation, args, "followup")
        assert result["details"]["output"] == output
        assert not harness.selector_calls
    finally:
        release.set()
        await harness.host.close()


class Selector:
    def __init__(self):
        self.calls = []

    async def distill(self, *args):
        self.calls.append(args)
        return "short"

    async def close(self):
        pass


async def test_native_bridge_goal_exact_path_without_socket(tmp_path):
    selector = Selector()
    bridge = CodexDistillerBridge(selector, tmp_path / "state", tmp_path / "workspace")
    request = {"log": "full attempt evidence", "focus": "Read attempts", "command": "cat GOAL_MAP.md"}
    assert await bridge._select(request) == "short"
    bridge.register_scope(tmp_path / "workspace", goal_map_path=Path("GOAL_MAP.md"))
    assert await bridge._select(request) == request["log"]
    other = {**request, "command": "cat notes/GOAL_MAP.md"}
    assert await bridge._select(other) == "short"
    assert len(selector.calls) == 2
    assert bridge.metrics["protected"] == 1


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def request(self, method, params, timeout=30):
        self.calls.append((method, deepcopy(params)))
        if method == "thread/start":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": params["turnId"], "status": "inProgress"}}
        return {}

    async def stop(self):
        pass


@pytest.mark.parametrize("entry", ["thread/start", "thread/resume", "turn/start"])
async def test_scope_persists_and_descendants_inherit_host_path(tmp_path, entry):
    root, workspace = tmp_path / "controller", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    goal = workspace / "GOAL_MAP.md"
    native, claude = FakeBackend(), FakeBackend()
    client = RuntimeClient(cwd=root, backends={"codex": native, "claude-code": claude})
    client._distiller = Selector()
    await client.start()
    params = {"cwd": str(workspace), "model": "gpt-6-astra", "sandbox": "workspace-write",
              "runtimeWorkspaceRoots": [str(workspace)], "belloRole": "coder", "config": {"agents": {
                  "enabled": True, "max_concurrent_threads_per_session": 1,
                  "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"]}}}}
    try:
        if entry == "thread/start":
            params["runtimeGoalMapPath"] = str(goal)
        parent = (await client.thread_start(params))["thread"]["id"]
        if entry == "thread/resume":
            await client.request(entry, {"threadId": parent, "runtimeGoalMapPath": str(goal)})
        turn_params = {"threadId": parent}
        if entry == "turn/start":
            turn_params["runtimeGoalMapPath"] = str(goal)
        turn = (await client.turn_start(turn_params))["turn"]["id"]
        assert client._scope_for(parent, turn).goal_map_path == goal
        delegated = await client._delegate("spawn_agent", {
            "model": "claude-code/claude-sonnet-4-6", "effort": "high", "message": "Inspect attempts",
        }, parent, turn)
        child = json.loads(delegated["content"][0]["text"])["agent_id"]
        assert client._threads[child]["runtimeGoalMapPath"] == str(goal)
        child_turn = client._threads[child]["activeTurnId"]
        assert client._scope_for(child, child_turn).goal_map_path == goal
    finally:
        await client.stop()
    restored = RuntimeClient(cwd=root, backends={"codex": FakeBackend(), "claude-code": FakeBackend()})
    restored._distiller = Selector()
    await restored.start()
    try:
        await restored.request("thread/resume", {"threadId": parent})
        turn = (await restored.turn_start({"threadId": parent}))["turn"]["id"]
        assert restored._scope_for(parent, turn).goal_map_path == goal
        assert not goal.exists()
    finally:
        await restored.stop()


async def test_goal_off_adds_no_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = FakeBackend()
    client = RuntimeClient(cwd=tmp_path, backends={"codex": native})
    await client.start()
    try:
        parent = (await client.thread_start({"cwd": str(workspace), "sandbox": "workspace-write",
                                            "model": "gpt-6-astra", "belloRole": "coder"}))["thread"]["id"]
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        assert client._scope_for(parent, turn).goal_map_path is None
        assert "runtimeGoalMapPath" not in client._threads[parent]
        assert all("runtimeGoalMapPath" not in params for _, params in native.calls)
    finally:
        await client.stop()


async def test_native_transport_registers_goal_but_never_forwards_metadata(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    goal = workspace / "GOAL_MAP.md"
    backend = CodexBackend(state_dir=tmp_path / "native", emit=AsyncMock(), tool_handler=AsyncMock())
    backend._bridge = CodexDistillerBridge(Selector(), tmp_path / "bridge")
    backend._selection_verified = True
    monkeypatch.setattr(codex_module, "native_toolchain_read_paths", lambda _root: ())
    params = {"cwd": str(workspace), "sandbox": "workspace-write", "model": "gpt-6-astra",
              "runtimeWorkspaceRoots": [str(workspace)], "distillerEnabled": True,
              "runtimeGoalMapPath": str(goal)}
    try:
        plain = backend._thread_params({k: v for k, v in params.items() if k != "runtimeGoalMapPath"})
        enabled = backend._thread_params(params)
        assert enabled == plain  # No native permission, prompt, tool or provider change.
        assert "runtimeGoalMapPath" not in enabled
        assert (workspace, None, goal) in backend._bridge._scopes
    finally:
        backend._journal.close()


@pytest.mark.parametrize("entry", ["thread/start", "thread/resume"])
async def test_native_goal_scope_survives_lifecycle_and_backend_reload(tmp_path, monkeypatch, entry):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    goal = workspace / "GOAL_MAP.md"
    native_calls = []

    class Native:
        async def request(self, method, params, timeout=30):
            native_calls.append((method, deepcopy(params)))
            if method == "thread/start":
                return {"thread": {"id": "native-thread"}}
            if method == "thread/resume":
                return {"thread": {"id": params["threadId"]}}
            if method == "turn/start":
                return {"turn": {"id": "native-turn", "status": "inProgress"}}
            raise AssertionError("unexpected native fixture method")

    def backend():
        value = CodexBackend(state_dir=tmp_path / "native", emit=AsyncMock())
        value._bridge = CodexDistillerBridge(Selector(), tmp_path / "bridge")
        value._selection_verified = True
        value._client = Native()
        # Only metadata/dispatch seams below are exercised; auth/catalog are fake.
        value._subscription = AsyncMock()
        value._validate = AsyncMock()
        return value

    monkeypatch.setattr(codex_module, "native_toolchain_read_paths", lambda _root: ())
    current = backend()
    params = {"threadId": "host-thread", "cwd": str(workspace), "sandbox": "workspace-write",
              "model": "gpt-6-astra", "runtimeWorkspaceRoots": [str(workspace)], "distillerEnabled": True}
    try:
        if entry == "thread/start":
            params["runtimeGoalMapPath"] = str(goal)
        await current._thread_start(params, 1)
        record = current._threads["host-thread"]
        if entry == "thread/resume":
            await current._resume(record, {"runtimeGoalMapPath": str(goal)}, 1)
        assert (workspace, None, goal) in current._bridge._scopes
    finally:
        current._journal.close()
    restored = backend()
    try:
        await restored._resume(restored._threads["host-thread"], {}, 1)
        assert (workspace, None, goal) in restored._bridge._scopes
        request = {"command": "cat GOAL_MAP.md", "focus": "Read attempts", "log": "complete evidence"}
        assert await restored._bridge._select(request) == request["log"]
        assert not restored._bridge.distiller.calls
        assert all("runtimeGoalMapPath" not in p for _, p in native_calls)
        assert not goal.exists()
    finally:
        restored._journal.close()


async def test_goal_map_path_is_output_metadata_not_a_filesystem_grant(tmp_path):
    root, workspace, outside = tmp_path / "controller", tmp_path / "workspace", tmp_path / "outside"
    for directory in (root, workspace, outside):
        directory.mkdir()
    native = FakeBackend()
    client = RuntimeClient(cwd=root, backends={"codex": native})
    await client.start()
    base = {"cwd": str(workspace), "model": "gpt-6-astra", "sandbox": "workspace-write",
            "runtimeWorkspaceRoots": [str(workspace)], "belloRole": "coder"}
    try:
        scopes = {}
        for name, extra in (("plain", {}), ("goal", {"runtimeGoalMapPath": str(outside / "GOAL_MAP.md")})):
            thread = (await client.thread_start({**base, **extra}))["thread"]["id"]
            turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
            scopes[name] = client._scope_for(thread, turn)
        plain, goal = scopes["plain"], scopes["goal"]
        # Only the output-selection metadata differs: no readable root, sandbox, network or approval change.
        assert goal.goal_map_path == outside / "GOAL_MAP.md" and plain.goal_map_path is None
        assert (goal.root, goal.mode, goal.readable_roots, goal.approval_policy, goal.network_access) == (
            plain.root, plain.mode, plain.readable_roots, plain.approval_policy, plain.network_access)
        assert goal.readable_roots == (workspace,)
        assert not (outside / "GOAL_MAP.md").exists()
    finally:
        await client.stop()


@pytest.mark.parametrize("entry", ["thread/start", "thread/resume", "turn/start"])
@pytest.mark.parametrize("value", [None, "", 0, ["GOAL_MAP.md"], "bad\x00path"])
async def test_invalid_goal_map_metadata_is_dropped_not_adopted(tmp_path, entry, value):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native = FakeBackend()
    client = RuntimeClient(cwd=tmp_path, backends={"codex": native})
    await client.start()
    params = {"cwd": str(workspace), "model": "gpt-6-astra", "sandbox": "workspace-write", "belloRole": "coder"}
    try:
        if entry == "thread/start":
            params["runtimeGoalMapPath"] = value
        parent = (await client.thread_start(params))["thread"]["id"]
        if entry == "thread/resume":
            await client.request(entry, {"threadId": parent, "runtimeGoalMapPath": value})
        turn_params = {"threadId": parent}
        if entry == "turn/start":
            turn_params["runtimeGoalMapPath"] = value
        turn = (await client.turn_start(turn_params))["turn"]["id"]
        assert client._scope_for(parent, turn).goal_map_path is None
        assert "runtimeGoalMapPath" not in client._threads[parent]
        assert all("runtimeGoalMapPath" not in call_params for _, call_params in native.calls)
    finally:
        await client.stop()


async def test_relative_goal_map_resolves_against_the_thread_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = RuntimeClient(cwd=tmp_path, backends={"codex": FakeBackend()})
    await client.start()
    try:
        parent = (await client.thread_start({"cwd": str(workspace), "model": "gpt-6-astra", "sandbox": "workspace-write",
                                            "belloRole": "coder", "runtimeGoalMapPath": "docs/../GOAL_MAP.md"}))["thread"]["id"]
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        assert client._scope_for(parent, turn).goal_map_path == workspace.resolve() / "GOAL_MAP.md"
    finally:
        await client.stop()


async def test_goal_map_outside_the_workspace_grants_no_read_access(harness, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "GOAL_MAP.md").write_text("elsewhere", encoding="utf-8")
    (outside / "sibling.txt").write_text("sibling", encoding="utf-8")
    scope = ToolScope(harness.root, "workspace-write", distiller_enabled=True,
                      goal_map_path=outside / "GOAL_MAP.md")
    harness.scopes[("coder", "turn")] = scope
    for target in (outside / "GOAL_MAP.md", outside / "sibling.txt", outside):
        with pytest.raises(PermissionError):
            harness.host._path(scope, str(target))
        result = await harness.call("read_file", {"path": str(target)}, f"outside-{target.name}")
        assert result["isError"] is True
    # The same name inside the assigned workspace is an ordinary in-scope path.
    assert harness.host._path(scope, "GOAL_MAP.md") == (harness.root / "GOAL_MAP.md").resolve()
