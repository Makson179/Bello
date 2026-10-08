from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from supervisor import snapshot_recovery as recovery
from supervisor import workspace_snapshot as ops


@pytest.fixture
def saved_snapshot(tmp_path: Path):
    (tmp_path / ".supervisor").mkdir()
    (tmp_path / "TASK.md").write_text("Implement the task.\n")
    (tmp_path / "app.py").write_text("value = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dependency.js").write_text("export default 1;\n")
    snapshot = ops.create_workspace_snapshot(tmp_path, tmp_path / "TASK.md")
    run_id = str(uuid4())
    authority_path = tmp_path / ".supervisor" / "controller" / "snapshot.json"
    digest = recovery.persist_snapshot_authority(
        snapshot, authority_path, run_id=run_id
    )
    yield snapshot, authority_path, run_id, digest
    snapshot.cleanup()


def _restore(saved):
    snapshot, authority, run_id, digest = saved
    return recovery.restore_snapshot_authority(
        authority,
        run_id=run_id,
        expected_digest=digest,
        project_root=snapshot.original_root,
    )


def _child(saved, script: str) -> subprocess.CompletedProcess[str]:
    snapshot, authority, run_id, digest = saved
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(authority),
            run_id,
            digest,
            str(snapshot.original_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )


_RESTORE_SCRIPT = """
import sys
from pathlib import Path
from supervisor import workspace_snapshot as ops
from supervisor.snapshot_recovery import restore_snapshot_authority
snapshot = restore_snapshot_authority(Path(sys.argv[1]), run_id=sys.argv[2], expected_digest=sys.argv[3], project_root=Path(sys.argv[4]))
"""


def test_another_process_restores_same_live_snapshot_and_applies_candidate(
    saved_snapshot,
):
    snapshot, authority, _run, _digest = saved_snapshot
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n")
    completed = _child(
        saved_snapshot,
        _RESTORE_SCRIPT
        + """
import json
result = ops.apply_snapshot_patch(snapshot)
print(json.dumps({"root": str(snapshot.snapshot_root), "baseline": snapshot.baseline_commit, "dependencies": [str(p) for p in snapshot.readonly_dependency_roots], "changed": result.changed_paths}))
""",
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["root"] == str(snapshot.snapshot_root)
    assert result["baseline"] == snapshot.baseline_commit
    assert result["dependencies"] == [
        str(p) for p in snapshot.readonly_dependency_roots
    ]
    assert result["changed"] == ["app.py"]
    assert (snapshot.original_root / "app.py").read_text() == "value = 2\n"
    assert (snapshot.snapshot_root / ".git").is_dir()
    assert authority.is_file()


def test_identical_checkpoint_reuses_baseline_and_preserves_file_identity(
    saved_snapshot, monkeypatch
):
    snapshot, authority, run_id, digest = saved_snapshot
    before = authority.stat()
    monkeypatch.setattr(
        recovery, "_baseline_authority", lambda _: pytest.fail("rehashed baseline")
    )
    monkeypatch.setattr(
        recovery, "_capture", lambda *a: pytest.fail("rebuilt unchanged authority")
    )
    monkeypatch.setattr(
        recovery,
        "_json_bytes",
        lambda *a: pytest.fail("serialized unchanged authority"),
    )
    monkeypatch.setattr(
        recovery, "_read_private", lambda *a: pytest.fail("reread unchanged authority")
    )
    assert (
        recovery.persist_snapshot_authority(snapshot, authority, run_id=run_id)
        == digest
    )
    after = authority.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


@pytest.mark.parametrize("consumer", ["restore", "apply"])
def test_cached_authority_never_blesses_same_size_timestamp_tampering(
    saved_snapshot, monkeypatch, consumer
):
    snapshot, authority, run_id, digest = saved_snapshot
    before = authority.stat()
    data = authority.read_bytes()
    altered = data.replace(b'"version":1', b'"version":2', 1)
    assert altered != data and len(altered) == len(data)
    authority.write_bytes(altered)
    os.utime(authority, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert authority.stat().st_size == before.st_size
    assert authority.stat().st_mtime_ns == before.st_mtime_ns
    # Simulate even indistinguishable metadata: the optimization may reuse
    # only its existing pinned digest, never accept newly read disk values.
    monkeypatch.setattr(
        recovery, "_authority_file_stamp", lambda _: snapshot.recovery_cache["stamp"]
    )
    assert (
        recovery.persist_snapshot_authority(snapshot, authority, run_id=run_id)
        == digest
    )
    assert authority.read_bytes() == altered
    with pytest.raises(
        ops.WorkspaceSnapshotError, match="authority.*(changed|modified)"
    ):
        if consumer == "restore":
            _restore(saved_snapshot)
        else:
            ops.apply_snapshot_patch(snapshot)


def test_runtime_manifest_change_invalidates_cached_authority(saved_snapshot):
    snapshot, authority, run_id, digest = saved_snapshot
    snapshot.runtime_copy_manifests["plan"] = (
        (".", ops.SnapshotPathState(kind="file", sha256="1" * 64)),
    )
    updated = recovery.persist_snapshot_authority(snapshot, authority, run_id=run_id)
    assert updated != digest
    assert (
        json.loads(authority.read_bytes())["runtime_copy_manifests"]["plan"][0][
            "state"
        ]["sha256"]
        == "1" * 64
    )


@pytest.mark.parametrize(
    "kind",
    [
        "bytes",
        "symlink",
        "hardlink",
        "parent_link",
        "wrong_run",
        "task",
        "config",
        "dependency",
        "git_link",
        "snapshot_identity",
    ],
)
def test_rejects_mutated_authority_and_topology_without_repair(
    saved_snapshot, tmp_path: Path, kind: str
):
    snapshot, authority, run_id, digest = saved_snapshot
    restored_path = None
    if kind == "bytes":
        authority.write_bytes(authority.read_bytes() + b" ")
    elif kind in {"symlink", "hardlink"}:
        backup = authority.with_suffix(".backup")
        authority.rename(backup)
        if kind == "symlink":
            authority.symlink_to(backup)
        else:
            os.link(backup, authority)
    elif kind == "parent_link":
        parent = authority.parent
        moved = parent.with_name("moved-controller")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
    elif kind == "wrong_run":
        run_id = str(uuid4())
    elif kind == "task":
        (tmp_path / "TASK.md").write_text("Changed task\n")
    elif kind == "config":
        (snapshot.snapshot_root / ".git" / "config").write_text(
            "[core]\n hooksPath=/tmp/hooks\n"
        )
    elif kind == "dependency":
        dependency = snapshot.snapshot_root / "node_modules"
        dependency.unlink()
        dependency.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "git_link":
        (snapshot.snapshot_root / ".git" / "linked-data").symlink_to(
            tmp_path / "app.py"
        )
    elif kind == "snapshot_identity":
        restored_path = snapshot.snapshot_root.with_name("old-workspace")
        snapshot.snapshot_root.rename(restored_path)
        snapshot.snapshot_root.mkdir()
    before = (tmp_path / "app.py").read_bytes()
    try:
        with pytest.raises(
            ops.WorkspaceSnapshotError, match="snapshot recovery rejected"
        ):
            recovery.restore_snapshot_authority(
                authority, run_id=run_id, expected_digest=digest, project_root=tmp_path
            )
        assert (tmp_path / "app.py").read_bytes() == before
    finally:
        if restored_path is not None:
            snapshot.snapshot_root.rmdir()
            restored_path.rename(snapshot.snapshot_root)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_version",
        "extra_key",
        "relative_traversal",
        "bad_base64",
        "bad_baseline",
        "wrong_root",
    ],
)
def test_strict_authority_schema_rejects_invalid_protected_records(
    saved_snapshot, mutation: str
):
    snapshot, authority, run_id, _digest = saved_snapshot
    record = json.loads(authority.read_bytes())
    if mutation == "unknown_version":
        record["version"] = 42
    elif mutation == "extra_key":
        record["provider_approval"] = "allow"
    elif mutation == "relative_traversal":
        record["task_relative_path"] = "../TASK.md"
    elif mutation == "bad_base64":
        record["task_bytes"] = "not base64!"
    elif mutation == "bad_baseline":
        record["baseline_objects"][record["baseline_commit"]] = "0" * 64
    elif mutation == "wrong_root":
        record["snapshot_root"] = str(snapshot.original_root)
    data = json.dumps(record).encode()
    authority.write_bytes(data)
    with pytest.raises(ops.WorkspaceSnapshotError, match="snapshot recovery rejected"):
        recovery.restore_snapshot_authority(
            authority,
            run_id=run_id,
            expected_digest=hashlib.sha256(data).hexdigest(),
            project_root=snapshot.original_root,
        )


def test_authority_cannot_live_inside_the_model_workspace(saved_snapshot):
    snapshot, _authority, run_id, _digest = saved_snapshot
    with pytest.raises(ops.WorkspaceSnapshotError, match="protected .supervisor"):
        recovery.persist_snapshot_authority(
            snapshot, snapshot.snapshot_root / "authority.json", run_id=run_id
        )


def test_detached_export_is_not_live_recovery_authority(saved_snapshot):
    snapshot, _authority, _run_id, _digest = saved_snapshot
    ops._detach_recovery_workspace(snapshot)
    with pytest.raises(ops.WorkspaceSnapshotError, match="snapshot recovery rejected"):
        _restore(saved_snapshot)


def test_detached_plan_persists_without_reexposure(tmp_path: Path):
    (tmp_path / ".supervisor").mkdir()
    (tmp_path / "TASK.md").write_text("task")
    (tmp_path / "PLAN.md").write_text("private plan")
    snapshot = ops.create_workspace_snapshot(
        tmp_path, tmp_path / "TASK.md", plan_path=tmp_path / "PLAN.md"
    )
    path = tmp_path / ".supervisor" / "controller" / "snapshot.json"
    run_id = str(uuid4())
    try:
        recovery.persist_snapshot_authority(snapshot, path, run_id=run_id)
        assert snapshot.detach_plan_exposure()
        digest = recovery.persist_snapshot_authority(snapshot, path, run_id=run_id)
        restored = recovery.restore_snapshot_authority(
            path, run_id=run_id, expected_digest=digest, project_root=tmp_path
        )
        assert not restored.plan_exposed
        assert restored.plan_bytes == b"private plan"
        assert restored.plan_path is not None and not restored.plan_path.exists()
    finally:
        snapshot.cleanup()


@pytest.mark.parametrize("boundary", ["before_apply", "after_apply"])
def test_killed_apply_is_never_replayed_in_another_process(
    saved_snapshot, boundary: str
):
    snapshot, authority, _run_id, _digest = saved_snapshot
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n")
    hook = (
        """
def crash(*args, **kwargs):
    os._exit(91)
ops._apply_patch_to_original = crash
"""
        if boundary == "before_apply"
        else """
verify = ops._verify_applied_paths
def crash(*args, **kwargs):
    verify(*args, **kwargs)
    os._exit(91)
ops._verify_applied_paths = crash
"""
    )
    first = _child(
        saved_snapshot,
        _RESTORE_SCRIPT
        + "\nimport os\n"
        + hook
        + "\nops.apply_snapshot_patch(snapshot)\n",
    )
    assert first.returncode == 91, first.stderr
    original = (snapshot.original_root / "app.py").read_bytes()
    assert original == (
        b"value = 1\n" if boundary == "before_apply" else b"value = 2\n"
    )
    transaction = json.loads(authority.with_name("snapshot-apply.json").read_bytes())
    assert transaction["disposition"] == "applying"
    second = _child(
        saved_snapshot, _RESTORE_SCRIPT + "\nops.apply_snapshot_patch(snapshot)\n"
    )
    assert second.returncode != 0
    assert "uncertain outcome" in second.stderr
    assert (snapshot.original_root / "app.py").read_bytes() == original


def test_committed_apply_is_idempotent_and_detects_later_original_edits(
    saved_snapshot, monkeypatch
):
    snapshot, _authority, _run_id, _digest = saved_snapshot
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n")
    result = ops.apply_snapshot_patch(snapshot)
    restored = _restore(saved_snapshot)
    monkeypatch.setattr(
        ops, "_apply_snapshot_patch", lambda *a, **kw: pytest.fail("replayed patch")
    )
    assert ops.apply_snapshot_patch(restored) == result
    (snapshot.original_root / "app.py").write_text("value = 3\n")
    with pytest.raises(ops.WorkspaceSnapshotError, match="refusing to reapply"):
        ops.apply_snapshot_patch(restored)


def test_process_killed_after_durable_commit_returns_result_without_reapplication(
    saved_snapshot,
):
    snapshot, _authority, _run_id, _digest = saved_snapshot
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n")
    crashed = _child(
        saved_snapshot,
        _RESTORE_SCRIPT
        + """
import os
from supervisor import snapshot_transaction
commit = snapshot_transaction.commit_patch_transaction
def crash(snapshot, result):
    commit(snapshot, result)
    os._exit(92)
snapshot_transaction.commit_patch_transaction = crash
ops.apply_snapshot_patch(snapshot)
""",
    )
    assert crashed.returncode == 92, crashed.stderr
    replay = (
        _RESTORE_SCRIPT
        + """
def forbidden(*args, **kwargs):
    raise AssertionError('Git apply must never run twice')
ops._apply_snapshot_patch = forbidden
result = ops.apply_snapshot_patch(snapshot)
assert result.applied and result.changed_paths == ('app.py',)
"""
    )
    repeated = _child(saved_snapshot, replay)
    assert repeated.returncode == 0, repeated.stderr
    (snapshot.original_root / "app.py").write_text("subsequent user edit\n")
    changed = _child(saved_snapshot, replay)
    assert changed.returncode != 0
    assert "refusing to reapply" in changed.stderr
    assert (snapshot.original_root / "app.py").read_text() == "subsequent user edit\n"


def test_committed_deletion_with_removed_parent_is_idempotent(tmp_path: Path):
    (tmp_path / ".supervisor").mkdir()
    (tmp_path / "TASK.md").write_text("task")
    (tmp_path / "obsolete").mkdir()
    (tmp_path / "obsolete" / "old.py").write_text("old source")
    snapshot = ops.create_workspace_snapshot(tmp_path, tmp_path / "TASK.md")
    path = tmp_path / ".supervisor" / "controller" / "snapshot.json"
    run_id = str(uuid4())
    try:
        recovery.persist_snapshot_authority(snapshot, path, run_id=run_id)
        (snapshot.snapshot_root / "obsolete" / "old.py").unlink()
        (snapshot.snapshot_root / "obsolete").rmdir()
        first = ops.apply_snapshot_patch(snapshot)
        assert first.changed_paths == ("obsolete/old.py",)
        assert not (tmp_path / "obsolete").exists()
        assert ops.apply_snapshot_patch(snapshot) == first
    finally:
        snapshot.cleanup()


@pytest.mark.parametrize("record", [b'{"version":true}', b'{"version":1,"version":1}'])
def test_noncanonical_json_records_are_rejected(saved_snapshot, record):
    snapshot, authority, run_id, _digest = saved_snapshot
    authority.write_bytes(record)
    with pytest.raises(ops.WorkspaceSnapshotError, match="unavailable or invalid"):
        recovery.restore_snapshot_authority(
            authority,
            run_id=run_id,
            expected_digest=hashlib.sha256(record).hexdigest(),
            project_root=snapshot.original_root,
        )


def test_windows_runtime_controls_are_recreated_after_validation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(ops, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(ops, "_runtime_exposure_mode", lambda: "copy")
    monkeypatch.setattr(ops, "_native_windows_runtime_controls_enabled", lambda: False)
    (tmp_path / ".supervisor").mkdir()
    (tmp_path / "TASK.md").write_text("task")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("dependency")
    snapshot = ops.create_workspace_snapshot(tmp_path, tmp_path / "TASK.md")
    path = tmp_path / ".supervisor" / "controller" / "snapshot.json"
    run_id = str(uuid4())
    opened = []

    class Control:
        @classmethod
        def open(cls, path):
            opened.append(path)
            return cls()

        def integrity_issue(self):
            return None

        def close(self):
            pass

    try:
        digest = recovery.persist_snapshot_authority(snapshot, path, run_id=run_id)
        # Original controller records evolve after the private state copy was
        # captured. Validate the saved copy, not the evolving source bytes.
        assert path.exists()
        assert not (snapshot.snapshot_root / ".supervisor" / "controller").exists()
        (tmp_path / ".supervisor" / "new-controller-status").write_text("running")
        monkeypatch.setattr(
            ops, "_native_windows_runtime_controls_enabled", lambda: True
        )
        monkeypatch.setattr(ops, "_WindowsRuntimeFileGuard", Control)
        monkeypatch.setattr(ops, "_WindowsDirectoryChangeWatcher", Control)
        restored = recovery.restore_snapshot_authority(
            path, run_id=run_id, expected_digest=digest, project_root=tmp_path
        )
        assert opened == [snapshot.task_path, snapshot.snapshot_root / "node_modules"]
        assert set(restored.windows_runtime_file_guards) == {"task"}
        assert set(restored.windows_dependency_watchers) == {"dependency:node_modules"}
        restored.close_windows_runtime_controls()
    finally:
        snapshot.cleanup()
