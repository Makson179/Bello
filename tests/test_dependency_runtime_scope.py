"""Dependency authority is supplied by the controller, never rediscovered from aliases."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from supervisor.appserver import AppServerError
from supervisor.coder import CoderSession, coder_thread_params, coder_thread_resume_params, coder_turn_params
from supervisor.controller import BelloController
from supervisor.project_config import MultiAgentConfig
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.sandbox import SandboxPolicy, SandboxRunner, SandboxUnavailableError
from supervisor.schemas import BelloConfig
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent
import supervisor.workspace_snapshot as workspace_snapshot_module
from supervisor.workspace_snapshot import WorkspaceSnapshotError, create_workspace_snapshot


def _create_inputs(tmp_path: Path):
    project = tmp_path / "original"
    project.mkdir()
    (project / "README.md").write_text("Synthetic repository\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "README.md"],
        ["-c", "user.name=Dependency Scope Test", "-c", "user.email=scope@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic baseline"],
    ):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    task = project / "TASK.md"
    task.write_text("Use the existing dependency\n", encoding="utf-8")
    dependency = project / ".venv"
    dependency.mkdir()
    (dependency / "public.txt").write_text("PUBLIC_DEPENDENCY\n", encoding="utf-8")
    (project / "private-controller.txt").write_text("PRIVATE_CONTROLLER\n", encoding="utf-8")
    private = tmp_path / "controller-private"
    private.mkdir()
    (private / "private.txt").write_text("PRIVATE_SIBLING\n", encoding="utf-8")
    return project, task, dependency, private


def _create_snapshot(tmp_path: Path):
    project, task, dependency, private = _create_inputs(tmp_path)
    snapshot = create_workspace_snapshot(project, task)
    return project, task, dependency, private, snapshot


@pytest.fixture
def dependency_snapshot(tmp_path: Path):
    values = _create_snapshot(tmp_path)
    try:
        yield values
    finally:
        values[-1].cleanup()


class _Backend:
    """Only the provider is stubbed; runtime scope and journal are real."""

    def __init__(self):
        self.calls = []

    async def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": params["turnId"], "status": "inProgress"}}
        return {}

    async def stop(self):
        pass


def _coder(client, project, task, snapshot, **kwargs):
    store = StateStore(project)
    store.initialize_bello(BelloConfig(project_root=str(project), task_path=str(task)), overwrite=True)
    return CoderSession(
        client, store, snapshot.snapshot_root, snapshot.task_path,
        model="gpt-5.6-sol", intelligence="high",
        readonly_roots=snapshot.readonly_dependency_roots, **kwargs,
    )


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX external dependency symlinks")
def test_snapshot_dependency_authority_reaches_every_coder_parameter_builder(dependency_snapshot) -> None:
    project, task, dependency, _, snapshot = dependency_snapshot
    assert snapshot.readonly_dependency_paths == (".venv",)
    assert snapshot.readonly_dependency_roots == (dependency.resolve(),)
    assert (snapshot.snapshot_root / ".venv").is_symlink()
    roots = [str(snapshot.snapshot_root), str(task), str(dependency)]
    kwargs = {"task_path": snapshot.task_path, "readonly_roots": snapshot.readonly_dependency_roots}
    assert coder_thread_params(snapshot.snapshot_root, **kwargs)["runtimeWorkspaceRoots"] == roots
    assert coder_thread_resume_params("thread", snapshot.snapshot_root, **kwargs)["runtimeWorkspaceRoots"] == roots
    turn = coder_turn_params("thread", "Use the dependency", snapshot.snapshot_root, **kwargs)
    assert turn["runtimeWorkspaceRoots"] == roots
    assert turn["sandboxPolicy"]["writableRoots"] == [str(snapshot.snapshot_root)]
    assert str(project) not in roots
    policy = SandboxPolicy(snapshot.snapshot_root, readable_roots=tuple(Path(path) for path in roots))
    assert policy.readable_roots == (task, dependency)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX external dependency symlinks")
async def test_dependency_alias_replacement_does_not_change_turn_or_journal_authority(
    dependency_snapshot, tmp_path: Path,
) -> None:
    project, task, dependency, private, snapshot = dependency_snapshot
    backend = _Backend()
    state_dir = tmp_path / "runtime-state"
    client = RuntimeClient(cwd=project, state_dir=state_dir, backends={"codex": backend})
    coder = _coder(client, project, task, snapshot)
    roots = [str(snapshot.snapshot_root), str(task), str(dependency)]
    try:
        thread = await coder.start_thread()
        alias = snapshot.snapshot_root / ".venv"
        alias.unlink()
        alias.symlink_to(private, target_is_directory=True)
        assert snapshot.readonly_dependency_roots == (dependency,)
        assert coder.readonly_roots == (dependency,)
        for start_turn in (coder.start_initial_turn, coder.start_restart_turn):
            turn = await start_turn()
            assert backend.calls[-1][1]["runtimeWorkspaceRoots"] == roots
            scope = client._scope_for(thread, turn)
            assert scope.readable_roots == tuple(Path(root) for root in roots)
            assert client._host._path(scope, str(dependency / "public.txt")) == dependency / "public.txt"
            with pytest.raises(PermissionError, match="outside"):
                client._host._path(scope, str(alias / "private.txt"))
            await coder.interrupt()
            coder.mark_turn_completed(turn)
        await client.stop()

        backend = _Backend()
        client = RuntimeClient(cwd=project, state_dir=state_dir, backends={"codex": backend})
        coder.client = client
        await coder.resume_thread()
        assert backend.calls[-1][1]["runtimeWorkspaceRoots"] == roots
        turn = await coder.start_revision_turn("Continue checking the implementation")
        assert backend.calls[-1][1]["runtimeWorkspaceRoots"] == roots
        scope = client._scope_for(thread, turn)
        assert scope.readable_roots == tuple(Path(root) for root in roots)
        with pytest.raises(PermissionError, match="outside"):
            client._host._path(scope, str(alias / "private.txt"))
        await coder.interrupt()
        coder.mark_turn_completed(turn)
        with pytest.raises(AppServerError, match="assigned filesystem scope"):
            await client.thread_resume(coder_thread_resume_params(
                thread, snapshot.snapshot_root, task_path=task, readonly_roots=(private,),
            ))
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX external dependency symlinks")
async def test_children_inherit_only_pinned_dependency_authority(dependency_snapshot, tmp_path: Path) -> None:
    project, task, dependency, private, snapshot = dependency_snapshot
    backend = _Backend()
    client = RuntimeClient(cwd=project, state_dir=tmp_path / "child-state", backends={"codex": backend})
    coder = _coder(client, project, task, snapshot, multi_agent=MultiAgentConfig(enabled=True))
    roots = [str(snapshot.snapshot_root), str(task), str(dependency)]
    try:
        thread = await coder.start_thread()
        turn = await coder.start_initial_turn()
        alias = snapshot.snapshot_root / ".venv"
        alias.unlink()
        alias.symlink_to(private, target_is_directory=True)
        result = await client._delegate("spawn_agent", {
            "model": "gpt-5.6-luna", "effort": "high", "message": "Inspect the dependency",
        }, thread, turn)
        child = json.loads(result["content"][0]["text"])["agent_id"]
        child_record = client._threads[child]
        assert child_record["runtimeWorkspaceRoots"] == roots
        scope = client._scope_for(child, child_record["activeTurnId"])
        assert scope.readable_roots == tuple(Path(root) for root in roots)
        assert client._host._path(scope, str(dependency / "public.txt")) == dependency / "public.txt"
        with pytest.raises(PermissionError, match="outside"):
            client._host._path(scope, str(alias / "private.txt"))
        assert str(project) not in child_record["runtimeWorkspaceRoots"]
    finally:
        await client.stop()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX external dependency symlinks")
async def test_transport_fallback_inherits_previous_dependency_authority(dependency_snapshot, tmp_path: Path) -> None:
    project, task, dependency, private, snapshot = dependency_snapshot
    backend = _Backend()
    client = RuntimeClient(cwd=project, state_dir=tmp_path / "fallback-state", backends={"codex": backend})
    controller = BelloController(project, task_path=task, client=client)
    controller.store.initialize_bello(
        BelloConfig(project_root=str(project), task_path=str(task)), overwrite=True,
    )
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    previous = CoderSession(
        client, controller.store, snapshot.snapshot_root, snapshot.task_path,
        model="gpt-5.6-sol", intelligence="high", readonly_roots=snapshot.readonly_dependency_roots,
    )
    try:
        await previous.start_thread()
        alias = snapshot.snapshot_root / ".venv"
        alias.unlink()
        alias.symlink_to(private, target_is_directory=True)
        await controller._start_fallback_recovery_coder(previous, start_continuation=True)
        replacement = controller.coder
        assert replacement.thread_id != previous.thread_id
        assert replacement.readonly_roots == (dependency,)
        assert backend.calls[-1][1]["runtimeWorkspaceRoots"] == [
            str(snapshot.snapshot_root), str(task), str(dependency),
        ]
        scope = client._scope_for(replacement.thread_id, replacement.active_turn_id)
        with pytest.raises(PermissionError, match="outside"):
            client._host._path(scope, str(alias / "private.txt"))
    finally:
        await client.stop()


def test_windows_copy_snapshot_does_not_grant_original_dependencies(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(workspace_snapshot_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(workspace_snapshot_module, "_native_windows_runtime_controls_enabled", lambda: False)
    project, _, dependency, _, snapshot = _create_snapshot(tmp_path)
    try:
        assert snapshot.runtime_exposure_mode == workspace_snapshot_module.RUNTIME_EXPOSURE_COPY
        assert snapshot.readonly_dependency_paths == (".venv",)
        assert snapshot.readonly_dependency_roots == ()
        assert not (snapshot.snapshot_root / ".venv").is_symlink()
        assert (snapshot.snapshot_root / ".venv" / "public.txt").read_text(encoding="utf-8") == "PUBLIC_DEPENDENCY\n"
        roots = coder_thread_params(
            snapshot.snapshot_root, task_path=snapshot.task_path,
            readonly_roots=snapshot.readonly_dependency_roots,
        )["runtimeWorkspaceRoots"]
        assert roots == [str(snapshot.snapshot_root)]
        assert str(dependency) not in roots and str(project) not in roots
    finally:
        snapshot.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX source dependency symlinks")
@pytest.mark.parametrize("target", ["original", "plan-directory"])
def test_source_dependency_cannot_grant_original_or_private_plan_directory(tmp_path: Path, target: str) -> None:
    project, task, dependency, _ = _create_inputs(tmp_path)
    plans = project / "plans"
    plans.mkdir()
    plan = plans / "PLAN.md"
    plan.write_text("PRIVATE_PLAN\n", encoding="utf-8")
    shutil.rmtree(dependency)
    dependency.symlink_to(project if target == "original" else plans, target_is_directory=True)
    with pytest.raises(WorkspaceSnapshotError, match="dependency exposure would grant private workspace inputs"):
        create_workspace_snapshot(project, task, plan_path=plan)


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX source dependency symlinks")
def test_source_dependency_alias_to_exact_private_plan_is_not_exposed(tmp_path: Path) -> None:
    project, task, dependency, _ = _create_inputs(tmp_path)
    plan = project / "PLAN.md"
    plan.write_text("PRIVATE_PLAN\n", encoding="utf-8")
    shutil.rmtree(dependency)
    dependency.symlink_to(plan)
    snapshot = create_workspace_snapshot(project, task, plan_path=plan)
    try:
        assert snapshot.readonly_dependency_roots == ()
        assert snapshot.readonly_dependency_paths == ()
        assert not (snapshot.snapshot_root / ".venv").exists()
        assert not (snapshot.snapshot_root / ".venv").is_symlink()
    finally:
        snapshot.cleanup()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX external dependency symlinks")
async def test_retained_completion_review_uses_source_dependency_roots_without_private_plan(
    tmp_path: Path,
) -> None:
    project, task, dependency, private = _create_inputs(tmp_path)
    plan = project / "PLAN.md"
    plan.write_text("PRIVATE_PLAN\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(project, task, plan_path=plan)

    class ReviewBackend(_Backend):
        async def request(self, method, params, timeout=30):
            response = await super().request(method, params, timeout=timeout)
            if method == "turn/start":
                response["turn"].update(status="completed", items=[{
                    "type": "agentMessage", "text": json.dumps({
                        "decision": "accept", "reason": "Offline scope test",
                        "validation_gaps": [], "message_to_coder": None,
                        "persistent_decision": None, "progress_update": None,
                        "clear_handoff": False, "display_message": None, "handoff": None,
                        "wake_sequence": 7, "generation": 0,
                    }),
                }])
            return response

    backend = ReviewBackend()
    client = RuntimeClient(cwd=project, state_dir=tmp_path / "review-state", backends={"codex": backend})
    store = StateStore(project)
    store.initialize_bello(BelloConfig(project_root=str(project), task_path=str(task)), overwrite=True)
    agent = StatelessSupervisorAgent(
        client, store, task, workspace_root=snapshot.snapshot_root, model="gpt-5.6-sol",
        completion_workspace_write=True, completion_source_snapshot=snapshot,
    )
    try:
        decision = await agent.decide_completion(agent.build_packet(wake_sequence=7, current_summary="Scope review"))
        assert decision.decision == "accept"
        review = agent.completion_workspace_snapshot
        assert review is not None
        assert review.snapshot_root != snapshot.snapshot_root
        roots = [str(review.snapshot_root), str(task), str(dependency)]
        for method, params in backend.calls:
            if method in {"thread/start", "turn/start"}:
                assert params["runtimeWorkspaceRoots"] == roots
        assert not (review.snapshot_root / "PLAN.md").exists()
        assert (review.snapshot_root / ".venv").resolve() == dependency
        thread = agent.completion_thread_id
        scope = client._scope_for(thread, client._threads[thread]["activeTurnId"])
        assert client._host._path(scope, str(review.snapshot_root / ".venv" / "public.txt")) == dependency / "public.txt"
        for forbidden in (plan, snapshot.plan_path, private / "private.txt", project / "private-controller.txt"):
            with pytest.raises(PermissionError, match="outside"):
                client._host._path(scope, str(forbidden))
        assert str(project) not in roots and str(snapshot.snapshot_root) not in roots
    finally:
        await agent.close_completion_review()
        await client.stop()
        snapshot.cleanup()


def _has_native_posix_backend() -> bool:
    if sys.platform == "darwin":
        return Path("/usr/bin/sandbox-exec").exists()
    return sys.platform.startswith("linux") and any(
        Path(path).exists() for path in ("/usr/bin/bwrap", "/bin/bwrap")
    )


@pytest.mark.skipif(
    not _has_native_posix_backend() and os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") != "1",
    reason="requires a native POSIX sandbox backend",
)
async def test_real_coder_runtime_executes_dependencies_without_writes_or_private_reads(
    dependency_snapshot, tmp_path: Path,
) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("execute-only file semantics require a non-root POSIX user")
    project, task, dependency, private, snapshot = dependency_snapshot
    oracle = dependency / "programbench-oracle"
    if sys.platform == "darwin":
        # Executing a copied Apple-signed binary can hang in the kernel. Use
        # a public script for execution and a never-executed permission fixture.
        script = dependency / "public-tool"
        script.write_text("#!/bin/sh\nprintf 'ORACLE_EXECUTED\\n'\n", encoding="utf-8")
        script.chmod(0o755)
        oracle.write_bytes(b"EXECUTE_ONLY_PERMISSION_FIXTURE")
        executable_target = ".venv/public-tool"
    else:
        shutil.copyfile("/bin/echo", oracle)
        executable_target = ".venv/programbench-oracle"
    oracle.chmod(0o111)
    (snapshot.snapshot_root / "executable").symlink_to(executable_target)
    client = RuntimeClient(cwd=project, state_dir=tmp_path / "native-state", backends={"codex": _Backend()})
    coder = _coder(client, project, task, snapshot)
    sequence = 0

    async def call(name, arguments):
        nonlocal sequence
        sequence += 1
        return await client._call_tool({
            "threadId": thread, "turnId": turn, "callId": str(sequence),
            "name": name, "arguments": arguments,
        })

    try:
        thread = await coder.start_thread()
        turn = await coder.start_initial_turn()
        scope = client._scope_for(thread, turn)
        try:
            probe = await SandboxRunner(SandboxPolicy(
                scope.root, mode=scope.mode, readable_roots=scope.readable_roots,
            )).run("true", scope.root, 5)
        except SandboxUnavailableError as exc:
            if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
                raise
            pytest.skip(str(exc))
        assert probe.exit_code == 0, probe.output
        for name, arguments, expected in (
            ("read_file", {"path": ".venv/public.txt"}, "PUBLIC_DEPENDENCY"),
            ("exec_command", {"command": "/bin/cat .venv/public.txt"}, "PUBLIC_DEPENDENCY"),
            ("exec_command", {"command": "./executable ORACLE_EXECUTED"}, "ORACLE_EXECUTED"),
        ):
            result = await call(name, arguments)
            assert not result["isError"], result
            assert expected in str(result), result
        for name, arguments in (
            ("read_file", {"path": ".venv/programbench-oracle"}),
            ("read_file", {"path": str(project / "private-controller.txt")}),
            ("read_file", {"path": str(private / "private.txt")}),
            ("write_file", {"path": ".venv/public.txt", "content": "CHANGED"}),
        ):
            assert (await call(name, arguments))["isError"], (name, arguments)
        for command in (
            "/bin/cat .venv/programbench-oracle",
            "printf CHANGED > .venv/public.txt",
            f"/bin/cat {shlex.quote(str(project / 'private-controller.txt'))}",
            f"/bin/cat {shlex.quote(str(private / 'private.txt'))}",
        ):
            assert (await call("exec_command", {"command": command}))["isError"], command
        assert (dependency / "public.txt").read_text(encoding="utf-8") == "PUBLIC_DEPENDENCY\n"
        assert oracle.stat().st_mode & 0o777 == 0o111
    finally:
        await client.stop()
