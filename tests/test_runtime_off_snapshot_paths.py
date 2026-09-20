"""Offline regressions for runtime-off export without filename heuristics."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

import supervisor.workspace_snapshot as snapshot_module
from supervisor.schemas import BelloStatus
from supervisor.workspace_snapshot import (
    SnapshotPatchError,
    SnapshotPatchSelection,
    WorkspaceSnapshot,
    apply_snapshot_patch,
    create_workspace_snapshot,
)
from tests.test_bello_state import _runtime_controller


def _init_project(root: Path) -> Path:
    root.mkdir()
    task = root / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "TASK.md"], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
            "-c", "commit.gpgsign=false", "commit", "-qm", "baseline",
        ],
        cwd=root,
        check=True,
    )
    return task


@pytest.fixture
def snapshot(tmp_path: Path):
    root = tmp_path / "project"
    task = _init_project(root)
    result = create_workspace_snapshot(root, task, declared_grading_roots=("assessment",))
    try:
        yield result
    finally:
        result.cleanup()


@pytest.mark.parametrize("runtime_enabled", [False, True])
@pytest.mark.parametrize(
    "relative", ["tokenizer.py", "t/err_unexpected_token.json", ".env", "fixtures/sample.pem"]
)
def test_snapshot_export_filename_heuristics_follow_runtime_switch(
    snapshot: WorkspaceSnapshot, relative: str, runtime_enabled: bool,
) -> None:
    fixture = snapshot.snapshot_root / relative
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("synthetic test fixture\n", encoding="utf-8")
    benign = snapshot.snapshot_root / "ordinary.txt"
    benign.write_text("ordinary source change\n", encoding="utf-8")

    if runtime_enabled:
        # The public default remains runtime-on; denial must precede all writes.
        with pytest.raises(SnapshotPatchError, match="secret-pattern"):
            apply_snapshot_patch(snapshot)
        assert not (snapshot.original_root / relative).exists()
        assert not (snapshot.original_root / "ordinary.txt").exists()
    else:
        result = apply_snapshot_patch(snapshot, runtime_enabled=False)
        assert set(result.changed_paths) == {relative, "ordinary.txt"}
        assert (snapshot.original_root / relative).read_text() == "synthetic test fixture\n"
        assert (snapshot.original_root / "ordinary.txt").read_text() == "ordinary source change\n"


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/config", ".aws/config", ".kube/config", ".docker/config.json",
        ".config/gh/hosts.yml", ".config/gcloud/configurations/default",
        "hidden/fixture.json", "grading/result.json", "private/data.json",
    ],
)
def test_runtime_off_snapshot_export_allows_synthetic_project_fixture_directories(
    snapshot: WorkspaceSnapshot, relative: str,
) -> None:
    candidate = snapshot.snapshot_root / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("synthetic project fixture\n", encoding="utf-8")

    result = apply_snapshot_patch(snapshot, runtime_enabled=False)
    assert result.changed_paths == (relative,)
    assert (snapshot.original_root / relative).read_text() == "synthetic project fixture\n"


@pytest.mark.parametrize("relative", ["assessment/answer.json", "assessment/token_fixture.json"])
def test_runtime_off_snapshot_export_preserves_grading_guards(
    snapshot: WorkspaceSnapshot, relative: str,
) -> None:
    candidate = snapshot.snapshot_root / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_text("synthetic protected-path fixture\n", encoding="utf-8")

    with pytest.raises(SnapshotPatchError, match="declared grading/hidden|secret-pattern"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)
    assert not (snapshot.original_root / relative).exists()


def test_runtime_off_snapshot_export_preserves_task_immutability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    task = _init_project(root)
    monkeypatch.setattr(snapshot_module, "_native_windows_runtime_controls_enabled", lambda: False)
    snapshot = create_workspace_snapshot(root, task)
    try:
        snapshot.task_path.unlink()
        snapshot.task_path.write_text("replacement task\n", encoding="utf-8")
        with pytest.raises(SnapshotPatchError, match="task file is immutable"):
            apply_snapshot_patch(snapshot, runtime_enabled=False)
        assert task.read_text() == "# Task"
    finally:
        snapshot.cleanup()


@pytest.mark.parametrize(
    "relative",
    ["../outside.txt", "<absolute-outside>", ".git/forged", ".supervisor/forged.json", ".codex/bello-run/forged.json"],
)
def test_runtime_off_snapshot_export_rejects_untrusted_selected_paths(
    snapshot: WorkspaceSnapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original outside bytes\n", encoding="utf-8")
    selected_path = str(outside) if relative == "<absolute-outside>" else relative
    # Git normally cannot produce traversal entries and filters runtime state.
    # Exercise the export validation boundary if the selection is ever malformed.
    monkeypatch.setattr(
        snapshot_module, "_snapshot_patch_selection",
        lambda _snapshot: SnapshotPatchSelection((selected_path,), ()),
    )
    with pytest.raises(SnapshotPatchError, match="snapshot patch path rejected"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)
    assert outside.read_text() == "original outside bytes\n"
    assert not (snapshot.original_root / ".git" / "forged").exists()
    assert not (snapshot.original_root / ".supervisor" / "forged.json").exists()
    assert not (snapshot.original_root / ".codex" / "bello-run" / "forged.json").exists()


@pytest.mark.parametrize(
    ("control_relative", "alias_relative"),
    [
        (".git", ".GIT"),
        (".git", ".GiT"),
        (".supervisor", ".SUPERVISOR"),
        (".supervisor", ".SuperVisor"),
        (".codex/bello-run", ".CODEX/BELLO-RUN"),
        (".codex/bello-run", ".CoDeX/BeLLo-RuN"),
    ],
)
@pytest.mark.parametrize("existing_target", [False, True])
def test_runtime_off_snapshot_export_rejects_actual_case_alias_of_control_authority(
    snapshot: WorkspaceSnapshot,
    monkeypatch: pytest.MonkeyPatch,
    control_relative: str,
    alias_relative: str,
    existing_target: bool,
) -> None:
    control = snapshot.original_root / control_relative
    control.mkdir(parents=True, exist_ok=True)
    alias = snapshot.original_root / alias_relative
    if not alias.exists() or not alias.samefile(control):
        pytest.skip("requires an actual case-insensitive temporary filesystem")

    sentinel = control / "authority-sentinel.txt"
    sentinel.write_bytes(b"original controller authority bytes\n")
    selected_name = sentinel.name if existing_target else "forged-new-file.json"
    selected_path = f"{alias_relative}/{selected_name}"
    # Git rejects or filters some control entries before listing a diff. Inject
    # selection to test the final exporter against the actual filesystem alias.
    monkeypatch.setattr(
        snapshot_module, "_snapshot_patch_selection",
        lambda _snapshot: SnapshotPatchSelection((selected_path,), ()),
    )
    patch_generation = Mock(side_effect=AssertionError("control alias reached patch generation"))
    monkeypatch.setattr(snapshot_module, "_snapshot_patch", patch_generation)

    with pytest.raises(SnapshotPatchError, match="snapshot patch path rejected"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)

    patch_generation.assert_not_called()
    assert sentinel.read_bytes() == b"original controller authority bytes\n"
    assert (alias / sentinel.name).read_bytes() == sentinel.read_bytes()
    assert not (control / "forged-new-file.json").exists()


@pytest.mark.parametrize(
    "relative", ["fixtures/.SuperVisor/token.json", "fixtures/.CoDeX/BeLLo-RuN/token.json"],
)
def test_runtime_off_snapshot_export_allows_nested_control_name_fixtures(
    snapshot: WorkspaceSnapshot, relative: str,
) -> None:
    fixture = snapshot.snapshot_root / relative
    fixture.parent.mkdir(parents=True)
    fixture.write_text("synthetic nested fixture\n", encoding="utf-8")

    result = apply_snapshot_patch(snapshot, runtime_enabled=False)

    assert result.changed_paths == (relative,)
    assert (snapshot.original_root / relative).read_text() == "synthetic nested fixture\n"


@pytest.mark.parametrize("alias_relative", ["ASSESSMENT", "AsSeSsMeNt"])
@pytest.mark.parametrize("existing_target", [False, True])
def test_runtime_off_snapshot_export_rejects_actual_case_alias_of_declared_grading_root(
    snapshot: WorkspaceSnapshot,
    monkeypatch: pytest.MonkeyPatch,
    alias_relative: str,
    existing_target: bool,
) -> None:
    grading = snapshot.original_root / "assessment"
    grading.mkdir()
    alias = snapshot.original_root / alias_relative
    if not alias.exists() or not alias.samefile(grading):
        pytest.skip("requires an actual case-insensitive temporary filesystem")

    sentinel = grading / "token.json"
    sentinel.write_bytes(b"original declared grading bytes\n")
    selected_name = sentinel.name if existing_target else "new_token_fixture.json"
    selected_path = f"{alias_relative}/{selected_name}"
    monkeypatch.setattr(
        snapshot_module, "_snapshot_patch_selection",
        lambda _snapshot: SnapshotPatchSelection((selected_path,), ()),
    )
    patch_generation = Mock(side_effect=AssertionError("grading alias reached patch generation"))
    monkeypatch.setattr(snapshot_module, "_snapshot_patch", patch_generation)

    with pytest.raises(SnapshotPatchError, match="declared grading/hidden path access denied"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)

    patch_generation.assert_not_called()
    assert sentinel.read_bytes() == b"original declared grading bytes\n"
    assert (alias / sentinel.name).read_bytes() == sentinel.read_bytes()
    assert not (grading / "new_token_fixture.json").exists()


@pytest.mark.parametrize("alias_relative", ["task.md", "TaSk.Md"])
def test_runtime_off_snapshot_export_rejects_actual_case_alias_of_immutable_task(
    snapshot: WorkspaceSnapshot, monkeypatch: pytest.MonkeyPatch, alias_relative: str,
) -> None:
    task = snapshot.original_root / snapshot.task_relative_path
    alias = snapshot.original_root / alias_relative
    if not alias.exists() or not alias.samefile(task):
        pytest.skip("requires an actual case-insensitive temporary filesystem")

    original_bytes = task.read_bytes()
    monkeypatch.setattr(
        snapshot_module, "_snapshot_patch_selection",
        lambda _snapshot: SnapshotPatchSelection((alias_relative,), ()),
    )
    patch_generation = Mock(side_effect=AssertionError("task alias reached patch generation"))
    monkeypatch.setattr(snapshot_module, "_snapshot_patch", patch_generation)

    with pytest.raises(SnapshotPatchError, match="immutable"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)

    patch_generation.assert_not_called()
    assert task.read_bytes() == original_bytes
    assert alias.read_bytes() == original_bytes


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink patch behavior")
@pytest.mark.parametrize("absolute", [False, True])
def test_runtime_off_snapshot_export_rejects_escaping_symlink(
    snapshot: WorkspaceSnapshot, tmp_path: Path, absolute: bool,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original outside bytes\n", encoding="utf-8")
    target = str(outside) if absolute else os.path.relpath(outside, snapshot.snapshot_root)
    (snapshot.snapshot_root / "fixture_token.json").symlink_to(target)

    with pytest.raises(SnapshotPatchError, match="absolute symlink|escaping symlink"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)
    assert outside.read_text() == "original outside bytes\n"
    assert not (snapshot.original_root / "fixture_token.json").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX original-target symlink behavior")
def test_runtime_off_snapshot_export_rejects_original_parent_redirect(
    snapshot: WorkspaceSnapshot, tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "tokenizer.py"
    sentinel.write_text("original outside bytes\n", encoding="utf-8")
    (snapshot.original_root / "src").symlink_to(outside, target_is_directory=True)
    (snapshot.snapshot_root / "src").mkdir()
    (snapshot.snapshot_root / "src" / "tokenizer.py").write_text("new source\n", encoding="utf-8")

    with pytest.raises(SnapshotPatchError, match="path escapes workspace"):
        apply_snapshot_patch(snapshot, runtime_enabled=False)
    assert sentinel.read_text() == "original outside bytes\n"
    assert (snapshot.original_root / "src").is_symlink()


@pytest.mark.parametrize("runtime_enabled", [False, True])
async def test_controller_finalization_exports_using_selected_runtime_mode(
    tmp_path: Path, runtime_enabled: bool,
) -> None:
    root = tmp_path / "project"
    _init_project(root)
    controller, store, _ = _runtime_controller(root)
    controller.runtime_enabled = runtime_enabled
    controller.use_git_diff = True
    snapshot = create_workspace_snapshot(root, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    fixture = snapshot.snapshot_root / "t" / "err_unexpected_token.json"
    fixture.parent.mkdir()
    fixture.write_text('{"valid": false}\n', encoding="utf-8")

    try:
        await controller.finalize(
            "task complete", status=BelloStatus.COMPLETE, completion_review_accepted=True,
        )
        if runtime_enabled:
            assert store.get_bello_config().status == BelloStatus.ESCALATED
            assert not (root / "t" / "err_unexpected_token.json").exists()
            assert controller._snapshot_recovery_path is not None
        else:
            assert store.get_bello_config().status == BelloStatus.COMPLETE
            assert (root / "t" / "err_unexpected_token.json").read_text() == '{"valid": false}\n'
            assert controller._snapshot_patch_applied is True
        assert not snapshot.temp_root.exists()
    finally:
        snapshot.cleanup()
