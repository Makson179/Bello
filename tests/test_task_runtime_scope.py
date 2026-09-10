from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from supervisor.appserver import AppServerError
from supervisor.coder import (
    CoderSession,
    coder_thread_params,
    coder_thread_resume_params,
    coder_turn_params,
    task_runtime_workspace_roots,
)
from supervisor.controller import BelloController
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.sandbox import SandboxPolicy, SandboxRunner, SandboxUnavailableError
from supervisor.runtime.tools import ToolHost, ToolScope
from supervisor.schemas import BelloConfig
from supervisor.state import StateStore
from supervisor.workspace_snapshot import create_verification_workspace_snapshot, create_workspace_snapshot


@pytest.fixture
def task_snapshot(tmp_path: Path):
    project = tmp_path / "original"
    project.mkdir()
    (project / "README.md").write_text("Initial repository\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "README.md"],
        ["-c", "user.name=Scope Test", "-c", "user.email=scope@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "Before task creation"],
    ):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    task = project / "TASK.md"
    task.write_text("The fresh task exists only in the working tree.\n", encoding="utf-8")
    sibling = project / "OTHER.md"
    sibling.write_text("Unrelated original file\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(project, task)
    try:
        yield project, task, sibling, snapshot
    finally:
        snapshot.cleanup()


def test_internal_task_needs_no_overlapping_read_authority(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("Task\n", encoding="utf-8")
    roots = task_runtime_workspace_roots(tmp_path, task)
    assert roots == [tmp_path.resolve()]
    assert SandboxPolicy(tmp_path, readable_roots=tuple(roots)).readable_roots == ()

    coder = CoderSession(object(), StateStore(tmp_path), tmp_path, Path("TASK.md"))
    assert coder._task_read_path == task.resolve()


def test_external_task_authority_rejects_directories_and_missing_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for invalid in (tmp_path, tmp_path / "missing.md"):
        with pytest.raises(ValueError, match="existing file"):
            coder_thread_params(workspace, task_path=invalid)


@pytest.mark.skipif(os.name == "nt", reason="uses the POSIX snapshot task-link strategy")
def test_coder_parameter_builders_grant_only_the_canonical_task_file(task_snapshot) -> None:
    project, task, _, snapshot = task_snapshot
    roots = [str(snapshot.snapshot_root), str(task)]
    assert snapshot.task_path.is_symlink()
    assert snapshot.task_path.resolve() == task
    assert coder_thread_params(snapshot.snapshot_root, task_path=snapshot.task_path)["runtimeWorkspaceRoots"] == roots
    assert coder_thread_resume_params("thread", snapshot.snapshot_root, task_path=task)["runtimeWorkspaceRoots"] == roots
    turn = coder_turn_params("thread", "Read the task", snapshot.snapshot_root, task_path=task)
    assert turn["runtimeWorkspaceRoots"] == roots
    assert turn["sandboxPolicy"]["writableRoots"] == [str(snapshot.snapshot_root)]
    assert str(project) not in roots


class _Backend:
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


@pytest.mark.skipif(os.name == "nt", reason="uses the POSIX snapshot task-link strategy")
async def test_coder_pins_task_authority_across_turns_and_journal_resume(task_snapshot, tmp_path: Path) -> None:
    project, task, sibling, snapshot = task_snapshot
    store = StateStore(project)
    store.initialize_bello(BelloConfig(project_root=str(project), task_path=str(task)), overwrite=True)
    state_dir = tmp_path / "runtime-state"
    backend = _Backend()
    client = RuntimeClient(cwd=project, state_dir=state_dir, backends={"pi": backend})
    coder = CoderSession(client, store, snapshot.snapshot_root, snapshot.task_path,
                         model="gpt-5.6-sol", intelligence="high")
    roots = [str(snapshot.snapshot_root), str(task)]
    try:
        thread = await coder.start_thread()
        # The model may replace this writable alias, but the pinned source
        # authority and subsequent path-only instructions must stay unchanged.
        snapshot.task_path.unlink()
        snapshot.task_path.symlink_to(sibling)
        for start_turn in (coder.start_initial_turn, coder.start_restart_turn):
            turn = await start_turn()
            params = backend.calls[-1][1]
            assert params["runtimeWorkspaceRoots"] == roots
            assert str(task) in params["input"][0]["text"]
            assert str(sibling) not in params["input"][0]["text"]
            assert client._scope_for(thread, turn).readable_roots == tuple(Path(root) for root in roots)
            with pytest.raises(PermissionError, match="outside"):
                client._host._path(client._scope_for(thread, turn), str(snapshot.task_path))
            await coder.interrupt()
            coder.mark_turn_completed(turn)
        await client.stop()

        backend = _Backend()
        client = RuntimeClient(cwd=project, state_dir=state_dir, backends={"pi": backend})
        coder.client = client
        await coder.resume_thread()
        assert backend.calls[-1][1]["runtimeWorkspaceRoots"] == roots
        turn = await coder.start_revision_turn("Check the implementation")
        params = backend.calls[-1][1]
        assert params["runtimeWorkspaceRoots"] == roots
        assert str(task) in params["input"][0]["text"]
        assert str(sibling) not in params["input"][0]["text"]
        assert client._scope_for(thread, turn).readable_roots == tuple(Path(root) for root in roots)
        await coder.interrupt()
        coder.mark_turn_completed(turn)
        with pytest.raises(AppServerError, match="assigned filesystem scope"):
            await client.thread_resume(coder_thread_resume_params(
                thread, snapshot.snapshot_root, task_path=sibling,
            ))
    finally:
        await client.stop()


def _has_native_posix_backend() -> bool:
    if sys.platform == "darwin":
        return Path("/usr/bin/sandbox-exec").exists()
    return sys.platform.startswith("linux") and any(
        Path(path).exists() for path in ("/usr/bin/bwrap", "/bin/bwrap")
    )


@pytest.mark.skipif(os.name == "nt", reason="uses the POSIX snapshot task-link strategy")
async def test_transport_fallback_keeps_previous_task_authority(task_snapshot, tmp_path: Path) -> None:
    project, task, sibling, snapshot = task_snapshot
    backend = _Backend()
    client = RuntimeClient(cwd=project, state_dir=tmp_path / "fallback-state", backends={"pi": backend})
    controller = BelloController(project, task_path=task, client=client)
    controller.store.initialize_bello(
        BelloConfig(project_root=str(project), task_path=str(task)), overwrite=True,
    )
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    previous = CoderSession(client, controller.store, snapshot.snapshot_root, snapshot.task_path,
                            model="gpt-5.6-sol", intelligence="high")
    try:
        await previous.start_thread()
        snapshot.task_path.unlink()
        snapshot.task_path.symlink_to(sibling)
        await controller._start_fallback_recovery_coder(previous, start_continuation=True)
        replacement = controller.coder
        assert replacement.task_read_path == task
        assert replacement.thread_id != previous.thread_id
        params = backend.calls[-1][1]
        assert params["runtimeWorkspaceRoots"] == [str(snapshot.snapshot_root), str(task)]
        scope = client._scope_for(replacement.thread_id, replacement.active_turn_id)
        with pytest.raises(PermissionError, match="outside"):
            client._host._path(scope, str(sibling))
    finally:
        await client.stop()


@pytest.mark.skipif(not _has_native_posix_backend(), reason="requires a native POSIX sandbox backend")
@pytest.mark.parametrize("review_copy", [False, True], ids=["coder", "retained-review-alias"])
async def test_native_task_reads_work_without_original_directory_authority(task_snapshot, tmp_path: Path, review_copy: bool) -> None:
    project, task, sibling, snapshot = task_snapshot
    review = create_verification_workspace_snapshot(
        snapshot.snapshot_root, source_snapshot=snapshot,
    ) if review_copy else None
    workspace = review.snapshot_root if review is not None else snapshot.snapshot_root
    roots = tuple(task_runtime_workspace_roots(workspace, task))
    scope = ToolScope(workspace, "workspace-write", readable_roots=roots)
    journal = RuntimeJournal(tmp_path / "tool-state")

    async def reject_unexpected(*args):
        raise AssertionError("this task read must not need approval or delegation")

    async def emit(_message):
        pass

    host = ToolHost(journal, lambda *_: scope, reject_unexpected, emit, reject_unexpected)
    sequence = 0

    async def call(name, arguments):
        nonlocal sequence
        sequence += 1
        return await host.call({"threadId": "thread", "turnId": "turn", "callId": str(sequence),
                                "name": name, "arguments": arguments})

    try:
        try:
            probe = await SandboxRunner(SandboxPolicy(workspace, readable_roots=roots)).run("true", workspace, 5)
        except SandboxUnavailableError as exc:
            if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
                raise
            pytest.skip(str(exc))
        assert probe.exit_code == 0, probe.output
        assert (workspace / "TASK.md").is_symlink()
        for path in ("TASK.md", str(task)):
            result = await call("read_file", {"path": path})
            assert not result["isError"], result
            assert task.read_text(encoding="utf-8") in result["content"][0]["text"]
            result = await call("exec_command", {"command": f"/bin/cat {shlex.quote(path)}"})
            packet = json.loads(result["content"][0]["text"])
            assert not result["isError"], result
            assert task.read_text(encoding="utf-8") in packet["output"]

        for name, arguments in (
            ("read_file", {"path": str(sibling)}),
            ("list_directory", {"path": str(project)}),
            ("write_file", {"path": str(task), "content": "changed"}),
            ("write_file", {"path": "TASK.md", "content": "changed"}),
        ):
            assert (await call(name, arguments))["isError"]
        quoted_task = shlex.quote(str(task))
        for command in (
            f"/bin/cat {shlex.quote(str(sibling))}",
            f"printf changed > {quoted_task}",
            f"/bin/mv {quoted_task} moved-task.md",
            f"/bin/rm {quoted_task}",
            f"/bin/ln {quoted_task} task-hardlink && printf changed > task-hardlink",
        ):
            result = await call("exec_command", {"command": command})
            assert result["isError"], (command, result)
        parent_listing = await call("exec_command", {"command": f"/bin/ls {shlex.quote(str(project))}"})
        if not parent_listing["isError"]:
            # Linux exposes synthetic parents for the exact file mount. Their
            # existence must not disclose the original directory's siblings.
            assert sibling.name not in json.loads(parent_listing["content"][0]["text"])["output"]
        result = await call("write_file", {"path": "allowed.txt", "content": "workspace write"})
        assert not result["isError"], result
        assert (workspace / "allowed.txt").read_text(encoding="utf-8") == "workspace write"
        assert task.read_text(encoding="utf-8") == "The fresh task exists only in the working tree.\n"
    finally:
        await host.close()
        journal.close()
        if review is not None:
            review.cleanup()
