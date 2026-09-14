from __future__ import annotations

import json

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.sandbox import SandboxResult
from tests.test_runtime_client import FakeBackend, start


@pytest.fixture
def selector(monkeypatch):
    instances = []
    class Selector:
        def __init__(self, path):
            self.closed = False
            self.calls = []
            instances.append(self)
        async def distill(self, text, focus, command):
            assert not self.closed
            self.calls.append((text, focus, command))
            return "kept"
        async def close(self):
            self.closed = True
    monkeypatch.setattr("supervisor.runtime.distiller.validate_bundle", lambda path: {})
    monkeypatch.setattr("supervisor.runtime.distiller.require_dependencies", lambda: None)
    monkeypatch.setattr("supervisor.runtime.distiller.LogDistiller", Selector)
    return instances


def make_client(tmp_path, *, runtime=True, distiller=False):
    root, workspace = tmp_path / "project", tmp_path / "snapshot"
    root.mkdir()
    workspace.mkdir()
    codex, claude = FakeBackend(), FakeBackend()
    client = RuntimeClient(cwd=root, backends={"codex": codex, "claude-code": claude})
    client.configure_run(runtime_enabled=runtime, log_distiller={"enabled": distiller, "model_path": "bundle"})
    return client, workspace, codex, claude


def has_focus(record):
    return "focus" in next(tool for tool in record["tools"] if tool["name"] == "exec_command")["parameters"]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [True, False])
@pytest.mark.parametrize("distiller", [True, False])
async def test_run_switches_control_roles_not_review_dependencies(tmp_path, selector, runtime, distiller):
    client, workspace, _, _ = make_client(tmp_path, runtime=runtime, distiller=distiller)
    try:
        for role in ("coder", "completion_review", "adversary", "runtime"):
            thread = await start(client, workspace, belloRole=role, developerInstructions="role instructions")
            record = client._threads[thread]
            assert record["distillerEnabled"] == (distiller and role == "coder")
            assert has_focus(record) == (distiller and role == "coder")
            assert record["networkAccess"] == (not runtime)
            assert record["sandbox"] == "workspace-write"
            assert record["approvalPolicy"] == ("on-request" if runtime else "never")
            assert ("Add a short focus" in record["developerInstructions"]) == (distiller and role == "coder")
        assert len(selector) == int(distiller)
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-5.6-sol", "claude-code/claude-sonnet-4-6"])
async def test_coder_resume_repair_turn_and_new_revision_keep_distillation(tmp_path, selector, model):
    client, workspace, codex, claude = make_client(tmp_path, runtime=False, distiller=True)
    try:
        thread = await start(client, workspace, model, belloRole="coder")
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        await client._emit({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}})
        await client.request("thread/resume", {"threadId": thread, "belloRole": "coder", "developerInstructions": "caller cannot replace tools", "approvalPolicy": "on-request"})
        backend = claude if model.startswith("claude-code/") else codex
        sent = backend.calls[-1][1]
        assert ("Add a very short focus" if model.startswith("claude-code/") else "Add a short focus") in sent["developerInstructions"]
        assert sent["approvalPolicy"] == "never" and has_focus(sent)
        next_turn = (await client.turn_start({"threadId": thread, "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": [str(workspace)]}}))["turn"]["id"]
        scope = client._scope_for(thread, next_turn)
        assert scope.distiller_enabled and scope.network_access
        revision = await start(client, workspace, model, belloRole="coder")
        assert client._threads[revision]["distillerEnabled"]
        assert len(selector) == 1
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["coder", "completion_review", "adversary"])
async def test_cross_provider_children_inherit_distiller_only_for_coder(tmp_path, selector, role):
    client, workspace, _, _ = make_client(tmp_path, runtime=False, distiller=True)
    try:
        parent = await start(client, workspace, belloRole=role, config={"agents": {
            "enabled": True, "role": role, "max_concurrent_threads_per_session": 2,
            "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"]}}})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        reply = await client._delegate("spawn_agent", {"model": "claude-code/claude-sonnet-4-6", "effort": "high", "message": "inspect"}, parent, turn)
        child = json.loads(reply["content"][0]["text"])["agent_id"]
        record = client._threads[child]
        assert record["distillerEnabled"] == (role == "coder")
        assert has_focus(record) == (role == "coder")
        assert record["developerInstructions"].count("Add a very short focus") == int(role == "coder")
        assert record["networkAccess"] and record["approvalPolicy"] == "never"
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconfigure", [True, False])
async def test_restart_recreates_closed_distiller(tmp_path, selector, reconfigure):
    client, workspace, codex, _ = make_client(tmp_path, distiller=True)
    try:
        thread = await start(client, workspace, belloRole="coder")
        old = client._distiller
        await client.stop()
        assert old.closed
        if reconfigure:
            client.configure_run(log_distiller={"enabled": True, "model_path": "bundle"})
        await client.start()
        client._engines["codex"] = codex
        await client.request("thread/resume", {"threadId": thread, "belloRole": "coder"})
        assert client._distiller is not old
        assert await client._host.distill("original output", "focus", "cmd") == "kept"
        assert len(selector) == 2
    finally:
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["runtime", "distiller"])
async def test_saved_thread_cannot_silently_keep_outdated_tool_policy(tmp_path, selector, changed):
    client, workspace, _, _ = make_client(tmp_path)
    await start(client, workspace, belloRole="coder")
    thread = next(iter(client._threads))
    await client.stop()
    client.configure_run(runtime_enabled=changed != "runtime", log_distiller={"enabled": changed == "distiller", "model_path": "bundle"})
    try:
        await client.start()
        with pytest.raises(AppServerError, match="start a fresh run"):
            await client.request("thread/resume", {"threadId": thread, "belloRole": "coder"})
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_runtime_off_network_does_not_grant_outside_files_or_approvals(tmp_path):
    client, workspace, _, _ = make_client(tmp_path, runtime=False)
    executed = []
    class Runner:
        def __init__(self, policy):
            executed.append(policy)
        async def run(self, command, cwd, timeout, on_output=None, **kwargs):
            await on_output("download complete")
            return SandboxResult("download complete", 0, .1)
    async def unexpected(*args):
        raise AssertionError("no runtime approvals")
    try:
        thread = await start(client, workspace, belloRole="coder")
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        client._host.runner_factory = Runner
        client._host.approve = unexpected
        async def tool(call_id, name, arguments):
            return await client._host.call({"threadId": thread, "turnId": turn, "callId": call_id, "name": name, "arguments": arguments})
        assert not (await tool("download", "exec_command", {"command": "download into workspace"}))["isError"]
        assert executed[0].network_access and executed[0].mode == "workspace-write"
        assert (await tool("escape", "exec_command", {"command": "outside", "sandbox_permissions": "require_escalated"}))["isError"]
        assert (await tool("read", "read_file", {"path": str(tmp_path / "private.txt")}))["isError"]
        assert len(executed) == 1
    finally:
        await client.stop()


def test_enabled_missing_bundle_fails_before_start_without_dependencies(tmp_path):
    client = RuntimeClient(cwd=tmp_path)
    with pytest.raises(AppServerError, match="Cannot use log-distiller bundle"):
        client.configure_run(log_distiller={"enabled": True, "model_path": "absent"})
    assert not client._started


def test_enabled_missing_dependencies_fail_before_start(tmp_path, selector, monkeypatch):
    def missing():
        raise RuntimeError("install Bello[log-distiller]")
    monkeypatch.setattr("supervisor.runtime.distiller.require_dependencies", missing)
    client = RuntimeClient(cwd=tmp_path)
    with pytest.raises(RuntimeError, match="Bello"):
        client.configure_run(log_distiller={"enabled": True, "model_path": "bundle"})
    assert not client._started and not selector
