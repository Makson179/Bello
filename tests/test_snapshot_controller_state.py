"""Private recovery controls must never enter the Windows state copy."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from supervisor import snapshot_recovery
from supervisor import workspace_snapshot as snapshots
from supervisor.controller_recovery import RunOwner


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "TASK.md").write_text("Implement a local synthetic task.\n", encoding="utf-8")
    state = root / ".supervisor"
    state.mkdir()
    (state / "HANDOFF.md").write_text("public handoff\n", encoding="utf-8")
    return root


@pytest.fixture
def windows_copy(monkeypatch):
    monkeypatch.setattr(snapshots, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(snapshots, "_native_windows_runtime_controls_enabled", lambda: False)


def test_windows_copy_never_reads_private_controller_authority(
    project: Path, windows_copy, monkeypatch,
):
    """Model mandatory Windows byte-lock denial without claiming native coverage."""
    private = project / ".supervisor" / "controller"
    original_hash = snapshots._sha256_file

    def hash_public_only(path):
        if path == private or private in path.parents:
            raise PermissionError("synthetic Windows mandatory ownership-lock denial")
        return original_hash(path)

    monkeypatch.setattr(snapshots, "_sha256_file", hash_public_only)
    with RunOwner(project):
        snapshot = snapshots.create_workspace_snapshot(project, project / "TASK.md")
        try:
            exposed = snapshot.snapshot_root / ".supervisor"
            assert (exposed / "HANDOFF.md").read_text(encoding="utf-8") == "public handoff\n"
            assert not (exposed / "controller").exists()
            assert all(not name.startswith("controller") for name, _ in snapshot.runtime_copy_manifests["supervisor_state"])
            (private / "run.json").write_text("{}", encoding="utf-8")
            assert snapshot.restore_runtime_links() == ()
            (project / ".supervisor" / "HANDOFF.md").write_text("updated handoff\n", encoding="utf-8")
            snapshot.restore_runtime_links()
            assert (exposed / "HANDOFF.md").read_text(encoding="utf-8") == "updated handoff\n"
            assert not (exposed / "controller").exists()
        finally:
            snapshot.cleanup()


@pytest.mark.parametrize("private_name", ["controller", "CONTROLLER"])
def test_private_projection_is_case_insensitive_and_root_only(
    project: Path, windows_copy, private_name: str,
):
    state = project / ".supervisor"
    (state / private_name).mkdir()
    (state / private_name / "private.json").write_text("{}", encoding="utf-8")
    nested = state / "notes" / "controller"
    nested.mkdir(parents=True)
    (nested / "visible.txt").write_text("ordinary nested file", encoding="utf-8")
    snapshot = snapshots.create_workspace_snapshot(project, project / "TASK.md")
    try:
        exposed = snapshot.snapshot_root / ".supervisor"
        assert not (exposed / private_name).exists()
        assert (exposed / "notes" / "controller" / "visible.txt").read_text(encoding="utf-8") == "ordinary nested file"
        assert any(name == "notes/controller/visible.txt" for name, _ in snapshot.runtime_copy_manifests["supervisor_state"])
    finally:
        snapshot.cleanup()


def test_windows_copy_refresh_removes_injected_private_directory(
    project: Path, windows_copy,
):
    with RunOwner(project):
        snapshot = snapshots.create_workspace_snapshot(project, project / "TASK.md")
        try:
            private_copy = snapshot.snapshot_root / ".supervisor" / "controller"
            assert not private_copy.exists()
            private_copy.mkdir()
            (private_copy / "forged.json").write_text("{}", encoding="utf-8")
            assert "supervisor_state" in snapshot.restore_runtime_links()
            assert not private_copy.exists()
        finally:
            snapshot.cleanup()


def test_windows_copy_authority_roundtrip_retains_only_public_state(
    project: Path, windows_copy,
):
    with RunOwner(project):
        snapshot = snapshots.create_workspace_snapshot(project, project / "TASK.md")
        try:
            run_id = str(uuid4())
            authority = project / ".supervisor" / "controller" / "snapshot.json"
            digest = snapshot_recovery.persist_snapshot_authority(snapshot, authority, run_id=run_id)
            restored = snapshot_recovery.restore_snapshot_authority(
                authority, run_id=run_id, expected_digest=digest, project_root=project,
            )
            try:
                restored.restore_runtime_links()
                assert not (restored.snapshot_root / ".supervisor" / "controller").exists()
                assert (restored.snapshot_root / ".supervisor" / "HANDOFF.md").is_file()
                assert (project / ".supervisor" / "controller" / "owner.lock").exists()
            finally:
                restored.close_windows_runtime_controls()
            private_copy = snapshot.snapshot_root / ".supervisor" / "controller"
            private_copy.mkdir()
            (private_copy / "forged.json").write_text("{}", encoding="utf-8")
            with pytest.raises(snapshots.WorkspaceSnapshotError, match="runtime copy changed"):
                snapshot_recovery.restore_snapshot_authority(
                    authority, run_id=run_id, expected_digest=digest, project_root=project,
                )
        finally:
            snapshot.cleanup()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows mandatory byte-range locks")
def test_native_windows_snapshot_preserves_held_run_owner(project: Path):
    with RunOwner(project):
        # A distinct handle cannot read the held byte. This verifies the actual
        # kernel condition which the portable regression above only simulates.
        with pytest.raises(OSError):
            (project / ".supervisor" / "controller" / "owner.lock").read_bytes()
        snapshot = snapshots.create_workspace_snapshot(project, project / "TASK.md")
        try:
            assert snapshot.runtime_exposure_mode == snapshots.RUNTIME_EXPOSURE_COPY
            assert not (snapshot.snapshot_root / ".supervisor" / "controller").exists()
            snapshot.restore_runtime_links()
            child = subprocess.run(
                [sys.executable, "-B", "-c", """
import sys
from pathlib import Path
from supervisor.controller_recovery import RecoveryBlocked, RunOwner
try:
    with RunOwner(Path(sys.argv[1])):
        raise SystemExit(7)
except RecoveryBlocked:
    print('ownership remains exclusive')
""", str(project)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                timeout=30, check=False,
            )
            assert child.returncode == 0, child.stderr
            assert child.stdout.strip() == "ownership remains exclusive"
        finally:
            snapshot.cleanup()
