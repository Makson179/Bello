"""Controller workspace regression tests."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import pytest
import supervisor.controller as controller_module
import supervisor.workspace_snapshot as workspace_snapshot_module
from supervisor.controller import BelloController
from supervisor.schemas import AppEvent, AppEventSource, ChangedFile, InspectionRun, BelloStatus, ValidationRun
from supervisor.state import DECISIONS, FINAL_REPORT, PROGRESS
from supervisor.supervisor_agent import StatelessSupervisorAgent
from supervisor.workspace_snapshot import WorkspaceSnapshotError, create_workspace_snapshot

from tests.support.controller import (
    _runtime_controller,
)


async def test_controller_stages_plan_only_in_disposable_coder_workspace(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_bytes(b"PRIVATE PLAN CONTENT\r\npytest -q\r\n")
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    try:
        assert controller.plan_path == plan.resolve()
        assert controller.workspace_plan_path == snapshot.plan_path
        assert controller._active_coder_plan_path() == snapshot.plan_path
        assert snapshot.plan_path is not None
        assert snapshot.plan_path.read_text(encoding="utf-8") == (
            "PRIVATE PLAN CONTENT\npytest -q\n"
        )
        assert "plan_path" not in type(controller.store.get_bello_config()).model_fields
        for path in controller.store.state_dir.rglob("*"):
            if path.is_file():
                assert b"PRIVATE PLAN CONTENT" not in path.read_bytes()

        (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", "--", "PLAN.md"],
            cwd=snapshot.snapshot_root,
            check=True,
        )
        changed_files = await controller.changed_files()
        diff_summary = await controller.diff_summary()
        patch_summary = await controller.patch_summary()
        controller.validations = [
            ValidationRun(
                command="pytest -q",
                exit_code=0,
                passed=True,
                summary="pytest -q passed",
                captured_output="1 passed\n",
                sequence=1,
            ),
            ValidationRun(
                command="pytest tests/test_PLAN.md -q",
                exit_code=0,
                passed=True,
                summary="test_PLAN.md passed",
                captured_output="1 passed\n",
                sequence=2,
            ),
            ValidationRun(
                command="python -m pytest ./PLAN.md",
                exit_code=0,
                passed=True,
                summary="PRIVATE PLAN CONTENT",
                captured_output="PRIVATE PLAN CONTENT\n",
                sequence=3,
            )
        ]
        controller.inspections = [
            InspectionRun(
                command=f"cat {snapshot.plan_path}",
                exit_code=0,
                passed=True,
                summary="PRIVATE PLAN CONTENT",
                captured_output="PRIVATE PLAN CONTENT\n",
                sequence=4,
                inspected_paths=[str(snapshot.plan_path)],
            )
        ]
        packet_details = await controller.completion_packet_details(
            [*changed_files, ChangedFile(path="PLAN.md", status="added")]
        )
        review_payload = "\n".join(
            (
                diff_summary,
                patch_summary or "",
                repr(changed_files),
                repr(packet_details),
            )
        )
        assert "app.py" in review_payload
        assert str(snapshot.plan_path) not in review_payload
        assert "ChangedFile(path='PLAN.md'" not in review_payload
        assert "PRIVATE PLAN CONTENT" not in review_payload
        assert len(packet_details["validation_outputs"]) == 2
        assert packet_details["validation_outputs"][0].command == "pytest -q"
        assert packet_details["validation_outputs"][0].captured_output == "1 passed\n"
        assert packet_details["validation_outputs"][1].command == (
            "pytest tests/test_PLAN.md -q"
        )
        assert packet_details["inspection_outputs"] == []

        controller.store.append_text_locked(
            PROGRESS,
            f"- runtime quoted {snapshot.plan_path}\n",
        )
        controller.store.append_text_locked(
            DECISIONS,
            "- PRIVATE PLAN CONTENT\npytest -q\n",
        )
        controller.store.append_recent_action(
            f"runtime inspected {snapshot.plan_path}"
        )
        controller.store.append_recent_action(
            f"runtime resolved {snapshot.plan_source_path} from the private input"
        )
        controller.store.append_event(
            AppEvent(
                sequence=99,
                source=AppEventSource.SUPERVISOR,
                event_type="runtime/private_echo",
                payload={"text": "PRIVATE PLAN CONTENT\npytest -q\n"},
            )
        )
        review_agent = StatelessSupervisorAgent(  # type: ignore[arg-type]
            None,
            controller.store,
            task,
        )
        raw_packet = review_agent.build_packet(
            wake_sequence=100,
            current_summary=f"review after {snapshot.plan_path}",
        )
        safe_packet = controller._review_safe_packet_state(raw_packet)
        raw_state = json.dumps(raw_packet.model_dump(mode="json"))
        safe_state = json.dumps(safe_packet.model_dump(mode="json"))
        assert "PRIVATE PLAN CONTENT" in raw_state
        assert "PLAN.md" in raw_state
        assert "PRIVATE PLAN CONTENT" not in safe_state
        assert "PLAN.md" not in safe_state
        encoded_plan_source_path = json.dumps(str(snapshot.plan_source_path))[1:-1]
        assert encoded_plan_source_path in raw_state
        assert encoded_plan_source_path not in safe_state
    finally:
        snapshot.cleanup()


def test_nested_plan_read_is_private_when_action_uses_its_local_cwd(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "notes" / "PLAN.md"
    plan.parent.mkdir()
    plan_text = "PRIVATE NESTED PLAN\n" + ("private-detail-" * 2000)
    plan.write_text(plan_text, encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    try:
        inspection = InspectionRun(
            command="cat PLAN.md",
            cwd=str(snapshot.snapshot_root / "notes"),
            exit_code=0,
            passed=True,
            summary="cat PLAN.md",
            captured_output=plan_text[:20_000],
            sequence=1,
            inspected_paths=["PLAN.md"],
        )

        assert controller._exposes_review_private_input(inspection) is True
        aggregate_read = InspectionRun(
            command='for f in notes/*.md; do cat "$f"; done',
            cwd=str(snapshot.snapshot_root),
            exit_code=0,
            passed=True,
            summary="read Markdown files",
            captured_output=plan_text[:20_000],
            sequence=2,
            inspected_paths=["notes/*.md"],
        )
        assert controller._exposes_review_private_input(aggregate_read) is True
        assert controller._review_safe_values([aggregate_read]) == []
    finally:
        snapshot.cleanup()


def test_private_plan_path_matching_respects_platform_case_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "ПЛАН.md"
    plan.write_text("PRIVATE PLAN\n", encoding="utf-8")
    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    controller.initialize_state()
    controller._prepare_coder_workspace()
    snapshot = controller._coder_snapshot
    assert snapshot is not None
    validation = ValidationRun(
        command="pytest план.md -q",
        exit_code=0,
        passed=True,
        summary="lowercase план.md passed",
        captured_output="1 passed\n",
        sequence=1,
    )
    try:
        monkeypatch.setattr(controller_module, "is_windows_platform", lambda: False)
        assert controller._exposes_review_private_input(validation) is False
        monkeypatch.setattr(controller_module, "is_windows_platform", lambda: True)
        assert controller._exposes_review_private_input(validation) is True
        unrelated = validation.model_copy(
            update={
                "command": "pytest мегаплан.md -q",
                "raw_command": "pytest мегаплан.md -q",
                "normalized_command": "pytest мегаплан.md -q",
                "summary": "unrelated Cyrillic filename passed",
            }
        )
        assert controller._exposes_review_private_input(unrelated) is False
    finally:
        snapshot.cleanup()


def test_controller_clean_preserves_explicit_plan(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("private plan\n", encoding="utf-8")
    disposable = tmp_path / "remove-me.txt"
    disposable.write_text("remove\n", encoding="utf-8")

    controller = BelloController(
        tmp_path,
        task_path=task,
        plan_path=plan,
        clean_workspace=True,
    )

    # Construction must not destroy an existing run before ownership and
    # recovery validation. Cleaning happens only during owned initialization.
    assert disposable.exists()
    from supervisor.controller_recovery import RunOwner
    with RunOwner(tmp_path, controller=True):
        controller.initialize_state()

    assert task.read_text(encoding="utf-8") == "# Task\n"
    assert plan.read_text(encoding="utf-8") == "private plan\n"
    assert not disposable.exists()


def test_controller_rejects_plan_without_disposable_coder_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("private plan\n", encoding="utf-8")
    controller = BelloController(tmp_path, task_path=task, plan_path=plan)
    monkeypatch.setattr(
        controller_module,
        "coder_sandbox_mode",
        lambda: "danger-full-access",
    )

    with pytest.raises(WorkspaceSnapshotError, match="workspace-write coder snapshot"):
        controller._prepare_coder_workspace()


async def test_finalize_applies_accepted_snapshot_patch_to_real_workspace(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.use_git_diff = True
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")

    await controller.finalize("task complete", status=BelloStatus.COMPLETE, completion_review_accepted=True)

    assert source.read_text(encoding="utf-8") == "value = 2\n"
    assert not snapshot.temp_root.exists()
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert "- app.py" in store.path(FINAL_REPORT).read_text(encoding="utf-8")


async def test_finalize_preserves_snapshot_and_escalates_when_patch_back_is_rejected(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.use_git_diff = True
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    await controller.finalize("task complete", status=BelloStatus.COMPLETE, completion_review_accepted=True)

    assert not (tmp_path / ".env").exists()
    assert not snapshot.temp_root.exists()
    recovery_workspace = tmp_path / ".supervisor" / "recovery" / "run1" / "workspace"
    assert recovery_workspace.is_dir()
    assert (recovery_workspace / ".env").read_text(encoding="utf-8") == "TOKEN=secret\n"
    assert not (recovery_workspace / ".git").exists()
    assert not (recovery_workspace / ".supervisor").exists()
    assert not (recovery_workspace / "TASK.md").is_symlink()
    assert (recovery_workspace / "TASK.md").read_text(encoding="utf-8") == "# Task"
    assert store.get_bello_config().status == BelloStatus.ESCALATED
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted snapshot could not be applied" in report
    assert "snapshot preserved" in report


async def test_noncomplete_run_preserves_workspace_without_applying_it(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    source = tmp_path / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller._coder_started = True
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    (snapshot.snapshot_root / "app.py").write_text("value = 2\n", encoding="utf-8")

    await controller.finalize("exited by user", status=BelloStatus.EXITED)

    recovery_workspace = tmp_path / ".supervisor" / "recovery" / "run1" / "workspace"
    assert source.read_text(encoding="utf-8") == "value = 1\n"
    assert (recovery_workspace / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (recovery_workspace / ".git").exists()
    assert not (recovery_workspace / ".supervisor").exists()
    assert store.get_bello_config().status == BelloStatus.EXITED
    assert str(recovery_workspace) in store.path(FINAL_REPORT).read_text(encoding="utf-8")


async def test_preflight_failure_cleans_unused_snapshot_without_recovery(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller._snapshot_patch_applied = False
    controller._coder_started = False
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path

    await controller.finalize("preflight failed", status=BelloStatus.PROVIDER_FAILURE)

    assert not snapshot.temp_root.exists()
    assert not (store.state_dir / "recovery").exists()
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


@pytest.mark.skipif(os.name == "nt", reason="POSIX task-link integrity path")
def test_task_integrity_detects_replaced_snapshot_link(tmp_path: Path) -> None:
    controller, _store, _ = _runtime_controller(tmp_path)
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    try:
        assert controller._task_integrity_issue() is None
        snapshot.task_path.unlink()
        snapshot.task_path.write_text("weakened\n", encoding="utf-8")

        assert controller._task_integrity_issue() == "the coder workspace replaced or removed the read-only task link"
    finally:
        snapshot.cleanup()


def test_task_integrity_detects_replaced_snapshot_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _store, _ = _runtime_controller(tmp_path)
    monkeypatch.setattr(
        workspace_snapshot_module,
        "_runtime_exposure_mode",
        lambda: workspace_snapshot_module.RUNTIME_EXPOSURE_COPY,
    )
    monkeypatch.setattr(
        workspace_snapshot_module,
        "_native_windows_runtime_controls_enabled",
        lambda: False,
    )
    snapshot = create_workspace_snapshot(tmp_path, controller.task_path)
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    try:
        assert not snapshot.task_path.is_symlink()
        assert controller._task_integrity_issue() is None
        snapshot.task_path.write_text("weakened\n", encoding="utf-8")

        assert controller._task_integrity_issue() == (
            "the coder workspace replaced or modified the isolated task copy"
        )
    finally:
        snapshot.cleanup()


def test_adversary_snapshot_gets_functional_git_repo(tmp_path: Path) -> None:
    import shutil as _shutil
    import subprocess as _subprocess

    from supervisor.controller import _create_adversary_snapshot

    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("print('x')\n", encoding="utf-8")
    notes = project / "notes"
    notes.mkdir()
    (notes / "PLAN.md").write_text("PRIVATE PLAN\n", encoding="utf-8")
    (notes / "keep.md").write_text("public project note\n", encoding="utf-8")

    snapshot = _create_adversary_snapshot(
        project,
        excluded_relative_paths=("notes/PLAN.md",),
    )
    try:
        assert (snapshot / "app.py").exists()
        assert not (snapshot / "notes" / "PLAN.md").exists()
        assert (snapshot / "notes" / "keep.md").read_text(encoding="utf-8") == (
            "public project note\n"
        )
        assert (snapshot / ".git").is_dir()
        head = _subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=snapshot, capture_output=True, text=True
        )
        assert head.returncode == 0
        status = _subprocess.run(
            ["git", "status", "--short"], cwd=snapshot, capture_output=True, text=True
        )
        assert status.returncode == 0
        # Files stay untracked on purpose: recursive deletes inside the snapshot must
        # remain approvable for the adversary (tracked paths would be policy-denied).
        assert "?? app.py" in status.stdout
    finally:
        _shutil.rmtree(snapshot.parent, ignore_errors=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink sanitization regression")
def test_adversary_snapshot_drops_link_that_escapes_workspace(tmp_path: Path) -> None:
    from supervisor.controller import _create_adversary_snapshot
    from supervisor.workspace_snapshot import remove_isolated_workspace_tree

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("host data\n", encoding="utf-8")
    link = project / "escape"
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    snapshot = _create_adversary_snapshot(project)
    try:
        assert not (snapshot / "escape").exists()
        assert not (snapshot / "escape").is_symlink()
        assert external.read_text(encoding="utf-8") == "host data\n"
    finally:
        remove_isolated_workspace_tree(snapshot.parent)


def test_adversary_snapshot_git_ignores_global_template_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from supervisor.controller import _create_adversary_snapshot

    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("print('x')\n", encoding="utf-8")
    marker = tmp_path / "global-hook-ran"
    template = tmp_path / "git-template"
    hooks = template / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    global_config = tmp_path / "global-gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(global_config), "init.templateDir", str(template)],
        check=True,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    snapshot = _create_adversary_snapshot(project)
    try:
        assert not marker.exists()
        assert not (snapshot / ".git" / "hooks" / "post-commit").exists()
        hooks_path = subprocess.check_output(
            ["git", "config", "--local", "--get", "core.hooksPath"],
            cwd=snapshot,
            text=True,
        ).strip()
        assert hooks_path == os.devnull
    finally:
        import shutil as _shutil

        _shutil.rmtree(snapshot.parent, ignore_errors=True)
