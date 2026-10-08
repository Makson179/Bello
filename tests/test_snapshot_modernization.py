"""Contracts at the boundaries between snapshot components."""

from __future__ import annotations

import hashlib
import os
import pickle
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervisor.workspace_snapshot as snapshots
from supervisor.workspace_snapshot import (
    SnapshotPatchError,
    SnapshotPatchSelection,
    SnapshotPathState,
    WorkspaceSnapshotError,
    apply_snapshot_patch,
    create_workspace_snapshot,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "TASK.md").write_text("Implement the task.\n", encoding="utf-8")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    # Test temporaries may live inside the source checkout. Give each project
    # its own Git context so parent ignore rules and apply prefixes cannot leak in.
    snapshots._run_git(root, ["init", "-q"])
    snapshots._run_git(root, ["add", "TASK.md", "app.py"])
    snapshots._run_git(
        root,
        [
            "-c",
            "user.name=Snapshot Test",
            "-c",
            "user.email=snapshot@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "baseline",
        ],
    )
    return root


@pytest.fixture
def snapshot(project: Path):
    result = create_workspace_snapshot(project, project / "TASK.md")
    try:
        yield result
    finally:
        result.cleanup()


def _patch_pipeline(monkeypatch, snapshot, *, windows, fail_at=None, changed=True):
    events = []
    results = {
        "_snapshot_patch_selection": SnapshotPatchSelection(
            ("app.py",) if changed else (), ("build.pyc",)
        ),
        "_snapshot_patch": b"candidate patch",
    }
    stages = [
        "_restore_trusted_snapshot_git_config",
        "_audit_windows_snapshot_before_git",
        "_snapshot_patch_selection",
        "_validate_windows_original_root",
        "_validate_snapshot_patch_paths",
        "_validate_windows_patch_targets",
        "_validate_symlink_targets",
        "_snapshot_patch",
        "_apply_patch_to_original",
    ]
    monkeypatch.setattr(snapshots, "_is_windows_platform", lambda: windows)

    def step(name):
        def invoke(*args, **kwargs):
            events.append((name, kwargs))
            if name == fail_at:
                raise SnapshotPatchError(f"blocked at {name}")
            return results.get(name)

        return invoke

    for stage in stages:
        monkeypatch.setattr(snapshots, stage, step(stage))
    if not windows:
        stages = [stage for stage in stages if "windows" not in stage]
    return events, stages


@pytest.mark.parametrize("runtime_enabled", [False, True])
@pytest.mark.parametrize("windows", [False, True])
def test_export_checks_keep_their_order_and_runtime_scope(
    snapshot, monkeypatch, runtime_enabled, windows
):
    events, stages = _patch_pipeline(monkeypatch, snapshot, windows=windows)

    # This entry point was imported before any helper was replaced.
    result = apply_snapshot_patch(snapshot, runtime_enabled=runtime_enabled)

    assert [name for name, _kwargs in events] == stages
    assert dict(events)["_validate_snapshot_patch_paths"] == {
        "task_relative_path": "TASK.md",
        "declared_grading_roots": (),
        "check_path_heuristics": runtime_enabled,
    }
    assert result.applied
    assert result.changed_paths == ("app.py",)
    assert result.ignored_paths == ("build.pyc",)
    assert result.patch_bytes == len(b"candidate patch")


@pytest.mark.parametrize(
    "fail_at",
    [
        "_restore_trusted_snapshot_git_config",
        "_audit_windows_snapshot_before_git",
        "_snapshot_patch_selection",
        "_validate_windows_original_root",
        "_validate_snapshot_patch_paths",
        "_validate_windows_patch_targets",
        "_validate_symlink_targets",
        "_snapshot_patch",
    ],
)
def test_export_stops_at_each_failed_boundary(snapshot, monkeypatch, fail_at):
    events, stages = _patch_pipeline(
        monkeypatch, snapshot, windows=True, fail_at=fail_at
    )

    with pytest.raises(SnapshotPatchError, match=f"blocked at {fail_at}"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)

    assert [name for name, _kwargs in events] == stages[: stages.index(fail_at) + 1]
    assert (snapshot.original_root / "app.py").read_text() == "value = 1\n"


def test_empty_selection_still_restores_config_and_audits_before_git(
    snapshot, monkeypatch
):
    events, stages = _patch_pipeline(monkeypatch, snapshot, windows=True, changed=False)
    result = apply_snapshot_patch(snapshot)
    assert not result.applied
    assert result.ignored_paths == ("build.pyc",)
    assert [name for name, _kwargs in events] == stages[:3]


@pytest.mark.parametrize("failure", ["normalize", "check", "apply", "verify"])
def test_transaction_restores_bytes_modes_links_and_absence(
    project, monkeypatch, failure
):
    binary = project / "asset.bin"
    binary.write_bytes(b"\x00\xfforiginal\x00")
    removed = project / "removed.txt"
    removed.write_text("must survive rollback", encoding="utf-8")
    app = project / "app.py"
    if os.name != "nt":
        app.chmod(0o755)
        (project / "alias").symlink_to(app)

    snapshot = create_workspace_snapshot(project, project / "TASK.md")
    paths = ("app.py", "asset.bin", "removed.txt", "new.txt")
    if os.name != "nt":
        paths += ("alias",)
    before = {path: snapshots._snapshot_path_state(project / path) for path in paths}
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")
    (snapshot.snapshot_root / "app.py").chmod(0o644)
    (snapshot.snapshot_root / "asset.bin").write_bytes(b"\x00new\xff")
    (snapshot.snapshot_root / "removed.txt").unlink()
    (snapshot.snapshot_root / "new.txt").write_text("new", encoding="utf-8")
    if os.name != "nt":
        alias = snapshot.snapshot_root / "alias"
        alias.unlink()
        alias.symlink_to("asset.bin")

    normalize = snapshots._normalize_original_symlink_baselines
    git_apply = snapshots._run_git_apply
    verify = snapshots._verify_applied_paths
    restore = snapshots._restore_original_paths
    events = []

    def normalize_then_fail(*args):
        events.append("normalize")
        normalize(*args)
        if failure == "normalize":
            raise SnapshotPatchError("injected normalization failure")

    def apply_then_fail(cwd, args, patch):
        stage = "check" if "--check" in args else "apply"
        events.append(stage)
        completed = git_apply(cwd, args, patch)
        assert completed.returncode == 0, completed.stderr
        if failure == stage:
            # An apply failure may follow partial writes; exercise rollback
            # after the real Git command has changed the original workspace.
            return subprocess.CompletedProcess(args, 1, b"", b"injected failure")
        return completed

    def verify_then_fail(*args):
        events.append("verify")
        verify(*args)
        raise OSError("injected verification failure")

    def restore_and_record(*args):
        events.append("rollback")
        return restore(*args)

    monkeypatch.setattr(
        snapshots, "_normalize_original_symlink_baselines", normalize_then_fail
    )
    monkeypatch.setattr(snapshots, "_run_git_apply", apply_then_fail)
    monkeypatch.setattr(snapshots, "_verify_applied_paths", verify_then_fail)
    monkeypatch.setattr(snapshots, "_restore_original_paths", restore_and_record)
    try:
        with pytest.raises(SnapshotPatchError, match="injected"):
            apply_snapshot_patch(snapshot)
        order = ["normalize", "check", "apply", "verify"]
        assert events == order[: order.index(failure) + 1] + ["rollback"]
        assert {
            path: snapshots._snapshot_path_state(project / path) for path in paths
        } == before
        assert binary.read_bytes() == b"\x00\xfforiginal\x00"
    finally:
        snapshot.cleanup()


def test_rollback_attempts_remaining_paths_and_reports_failure(tmp_path, monkeypatch):
    original = tmp_path / "original"
    backup = tmp_path / "backup"
    original.mkdir()
    backup.mkdir()
    for name in ("first", "second"):
        (original / name).write_text("modified", encoding="utf-8")
        (backup / name).write_text("baseline", encoding="utf-8")
    remove = snapshots._remove_path
    attempted = []

    def fail_first(path):
        attempted.append(path.name)
        if path.name == "first":
            raise OSError("injected restore failure")
        remove(path)

    monkeypatch.setattr(snapshots, "_remove_path", fail_first)
    with pytest.raises(SnapshotPatchError, match="rollback failed: first: injected"):
        snapshots._restore_original_paths(
            original, backup, (("first", True), ("second", True))
        )
    assert attempted == ["first", "second"]
    assert (original / "second").read_text() == "baseline"


def test_existing_verification_snapshot_uses_live_hash_helper_without_rebaselining(
    project, monkeypatch
):
    verification = snapshots.create_verification_workspace_snapshot(project)
    baseline = verification.submitted_manifest
    try:
        with monkeypatch.context() as patch:
            patch.setattr(snapshots, "_sha256_file", lambda path: "changed hash")
            with pytest.raises(WorkspaceSnapshotError, match="app.py"):
                verification.assert_submission_unchanged()
        assert verification.submitted_manifest == baseline
        verification.assert_submission_unchanged()
        state = dict(baseline)["app.py"]
        assert (
            state.sha256
            == hashlib.sha256((project / "app.py").read_bytes()).hexdigest()
        )
        assert type(state) is SnapshotPathState
        assert pickle.loads(pickle.dumps(state)) == state
    finally:
        verification.cleanup()


def test_copy_ignore_callback_resolves_helpers_after_creation(project, monkeypatch):
    ignore = snapshots._snapshot_ignore(
        project,
        (),
        original_task=project / "TASK.md",
        original_plan=None,
        readonly_dependencies=[],
    )
    assert ignore(str(project), ["app.py"]) == set()
    monkeypatch.setattr(snapshots, "is_protected_path", lambda *args: True)
    assert ignore(str(project), ["app.py"]) == {"app.py"}


def test_native_guard_instance_uses_live_close_helper(tmp_path, monkeypatch):
    # No native API is needed to prove that an already-created control uses
    # the facade's replacement when it eventually releases its handle.
    guard = snapshots._WindowsRuntimeFileGuard(tmp_path / "task", 42, (1, 2))
    closed = []
    monkeypatch.setattr(snapshots, "_close_windows_handle", closed.append)
    guard.close()
    guard.close()
    assert closed == [42]
    assert guard.handle == 0
    assert type(pickle.loads(pickle.dumps(guard))) is snapshots._WindowsRuntimeFileGuard


def test_construction_unwinds_native_controls_before_removing_tree(
    project, monkeypatch
):
    dependency = project / "node_modules"
    dependency.mkdir()
    (dependency / "package.js").write_text("package", encoding="utf-8")
    events = []
    snapshot_roots = []

    def guard(path):
        snapshot_roots.append(path.parent)
        return SimpleNamespace(close=lambda: events.append("guard closed"))

    def watcher(path):
        return SimpleNamespace(close=lambda: events.append("watcher closed"))

    def fail_control_capture(path):
        raise OSError("injected control capture failure")

    cleanup = snapshots._cleanup_path_best_effort

    def cleanup_and_record(path):
        events.append("tree removed")
        cleanup(path)

    monkeypatch.setattr(
        snapshots, "_runtime_exposure_mode", lambda: snapshots.RUNTIME_EXPOSURE_COPY
    )
    monkeypatch.setattr(
        snapshots, "_native_windows_runtime_controls_enabled", lambda: True
    )
    monkeypatch.setattr(snapshots._WindowsRuntimeFileGuard, "open", guard)
    monkeypatch.setattr(snapshots._WindowsDirectoryChangeWatcher, "open", watcher)
    monkeypatch.setattr(snapshots, "_read_regular_file", fail_control_capture)
    monkeypatch.setattr(snapshots, "_cleanup_path_best_effort", cleanup_and_record)

    with pytest.raises(
        WorkspaceSnapshotError, match="injected control capture failure"
    ):
        create_workspace_snapshot(project, project / "TASK.md")
    assert events == ["watcher closed", "guard closed", "tree removed"]
    assert len(snapshot_roots) == 1
    assert not snapshot_roots[0].parent.exists()


def test_snapshot_runtime_registries_are_owned_per_instance(project):
    first = create_workspace_snapshot(project, project / "TASK.md")
    second = create_workspace_snapshot(project, project / "TASK.md")
    closed = []
    try:
        first.runtime_integrity_issues.append("first only")
        first.runtime_copy_manifests["probe"] = ((".", SnapshotPathState("file")),)
        first.windows_runtime_file_guards["probe"] = SimpleNamespace(
            close=lambda: closed.append("first")
        )
        second.windows_runtime_file_guards["probe"] = SimpleNamespace(
            close=lambda: closed.append("second")
        )
        first.close_windows_runtime_controls()
        assert closed == ["first"]
        assert first.runtime_integrity_issue() == "first only"
        assert second.runtime_integrity_issue() is None
        assert "probe" not in second.runtime_copy_manifests
        assert "probe" in second.windows_runtime_file_guards
    finally:
        first.cleanup()
        second.cleanup()
    assert closed == ["first", "second"]
