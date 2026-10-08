"""Bello state regression tests."""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from supervisor.controller import BelloController
from supervisor.main import _run_async_cleanly
from supervisor.schemas import AppEvent, AppEventSource, ChangedFile, FinalReport, BelloConfig, ValidationRun
from supervisor.state import DECISIONS, EVENTS, FINAL_REPORT, LOG, PREVIOUS_RUNS, PROGRESS, RECOVERY, RUN_CHECKPOINT, RUNTIME_METRICS, RUNTIME_TRACE, SUPERVISOR_WAKES, StateStore

from tests.support.controller import (
    _FakeTUI,
    _async_noop,
)


def test_bello_state_initializes_required_files(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    assert store.path(EVENTS).exists()
    assert store.path(FINAL_REPORT).exists()
    assert store.get_bello_config().task_path == str(task)


def test_run_checkpoint_is_atomic_metadata_and_survives_resume_initialization(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    config = BelloConfig(project_root=str(tmp_path), task_path=str(task))
    store.initialize_bello(config, overwrite=True)
    checkpoint = {
        "version": 1,
        "phase": "completion_review",
        "state": "active",
        "workspace_path": str(tmp_path / "workspace"),
    }

    store.write_run_checkpoint(checkpoint)
    store.initialize_bello(config, mode="resume")

    assert store.path(RUN_CHECKPOINT).is_file()
    assert store.get_run_checkpoint() == checkpoint


def test_bello_events_are_append_only_jsonl(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.append_event(AppEvent(sequence=1, source=AppEventSource.SYSTEM, event_type="test"))

    lines = store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["event_type"] == "test"


def test_fresh_initialization_creates_empty_previous_runs_without_run_slot(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")
    previous_runs = store.path(PREVIOUS_RUNS)

    assert previous_runs.is_dir()
    assert list(previous_runs.iterdir()) == []

    store.path(EVENTS).write_text('{"sequence": 9}\n', encoding="utf-8")
    store.path(LOG).write_text("old log\n", encoding="utf-8")
    (previous_runs / "run9").mkdir()
    (previous_runs / "run9" / "FINAL_REPORT.md").write_text("old report", encoding="utf-8")
    recovery = store.path(RECOVERY)
    (recovery / "run9" / "workspace").mkdir(parents=True)
    (recovery / "run9" / "workspace" / "app.py").write_text("recovery", encoding="utf-8")
    (store.state_dir / "scratch.txt").write_text("scratch", encoding="utf-8")

    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")

    assert store.path(EVENTS).read_text(encoding="utf-8") == ""
    assert store.path(LOG).read_text(encoding="utf-8") == ""
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert store.path(PREVIOUS_RUNS).is_dir()
    assert list(store.path(PREVIOUS_RUNS).iterdir()) == []
    assert not store.path(RECOVERY).exists()
    assert not (store.state_dir / "scratch.txt").exists()


def test_resume_initialization_preserves_history_and_resets_runtime_files(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    config = BelloConfig(project_root=str(tmp_path), task_path=str(task))
    store.initialize_bello(config, mode="fresh")
    previous_runs = store.path(PREVIOUS_RUNS)
    run1 = previous_runs / "run1"
    run1.mkdir()
    (run1 / "task.md").write_text("old task", encoding="utf-8")
    (run1 / "FINAL_REPORT.md").write_text("old report", encoding="utf-8")
    store.path(EVENTS).write_text('{"sequence": 42}\n', encoding="utf-8")
    store.path(LOG).write_text("old log\n", encoding="utf-8")
    store.path(FINAL_REPORT).write_text("stale final", encoding="utf-8")
    store.path(PROGRESS).write_text("stale progress", encoding="utf-8")
    store.path(DECISIONS).write_text("stale decisions", encoding="utf-8")
    store.path(SUPERVISOR_WAKES).write_text("stale wake\n", encoding="utf-8")
    store.path(RUNTIME_TRACE).write_text("stale trace\n", encoding="utf-8")
    store.path(RUNTIME_METRICS).write_text('{"old": true}\n', encoding="utf-8")
    recovery_workspace = store.path(RECOVERY) / "run2" / "workspace"
    recovery_workspace.mkdir(parents=True)
    (recovery_workspace / "app.py").write_text("recover me", encoding="utf-8")
    (store.state_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    (store.state_dir / "scratch_dir").mkdir()

    store.initialize_bello(config, mode="resume")

    assert store.path(EVENTS).read_text(encoding="utf-8") == '{"sequence": 42}\n'
    assert store.path(LOG).read_text(encoding="utf-8") == "old log\n"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "old task"
    assert (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "old report"
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert "not started" in store.path(PROGRESS).read_text(encoding="utf-8")
    assert store.path(DECISIONS).read_text(encoding="utf-8") == "# Decisions\n\n"
    assert store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8") == ""
    assert store.path(RUNTIME_TRACE).read_text(encoding="utf-8") == ""
    assert store.path(RUNTIME_METRICS).read_text(encoding="utf-8") == "{}\n"
    assert (recovery_workspace / "app.py").read_text(encoding="utf-8") == "recover me"
    assert not (store.state_dir / "scratch.txt").exists()
    assert not (store.state_dir / "scratch_dir").exists()


def test_archive_completed_run_copies_task_and_report_after_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), mode="fresh")

    store.write_final_report("first report\n")
    run1 = store.archive_completed_run(task)
    store.write_final_report("second report\n")
    run2 = store.archive_completed_run(task)

    assert run1.name == "run1"
    assert run2.name == "run2"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "# Task"
    assert (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "first report\n"
    assert (run2 / "FINAL_REPORT.md").read_text(encoding="utf-8") == "second report\n"


def test_controller_event_sequence_starts_at_one_when_events_are_empty(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    controller = BelloController(tmp_path, task_path=task)
    controller.initialize_state()

    assert controller._sequence == 0

    controller._append_event(AppEventSource.SYSTEM, "test/new")

    lines = controller.store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["sequence"] == 1
    assert controller.store.get_bello_config().last_event_sequence == 1


def test_controller_event_sequence_continues_existing_events(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    store.append_event(AppEvent(sequence=7, source=AppEventSource.SYSTEM, event_type="old"))
    store.append_event(AppEvent(sequence=42, source=AppEventSource.SYSTEM, event_type="newer"))

    controller = BelloController(tmp_path, task_path=task)
    controller.initialize_state()

    assert controller._sequence == 42

    controller._append_event(AppEventSource.SYSTEM, "test/new")

    lines = controller.store.path(EVENTS).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["sequence"] == 43
    assert controller.store.get_bello_config().last_event_sequence == 43


def test_final_report_rendering(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.write_final_report(FinalReport(task_path=str(task), status="complete", result="done", files_changed=["a.py"]))

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "# Final Report" in text
    assert "- a.py" in text


def test_final_report_omits_completion_review_status_when_not_applicable(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    store.write_final_report(
        FinalReport(
            task_path=str(task),
            status="complete",
            result="completed normally",
            completion_review_accepted=None,
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "- Status: complete" in text
    assert "- Result: completed normally" in text
    assert "Completion review accepted" not in text


async def test_final_report_non_git_omits_git_usage_and_includes_validations(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = True
    controller.validations = [
        ValidationRun(command="pytest -q", exit_code=0, passed=True, summary="command completed: pytest -q exit=0", sequence=1)
    ]
    controller.observed_changed_files = {"cron.py": ChangedFile(path="cron.py", status="modified")}
    controller.tui = _FakeTUI()
    controller.running = True

    await controller.finalize("task complete")

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "usage: git diff" not in text
    assert "fatal: not a git repository" not in text
    assert "## Diff Summary" not in text
    assert "- cron.py" in text
    assert "- pytest -q (behavioral pass, exit=0)" in text

    run1 = store.path(PREVIOUS_RUNS) / "run1"
    assert (run1 / "task.md").read_text(encoding="utf-8") == "# Task"
    archived_report = (run1 / "FINAL_REPORT.md").read_text(encoding="utf-8")
    assert "# Final Report" in archived_report
    assert "- Result: task complete" in archived_report

    controller._archive_final_report_once()
    assert sorted(path.name for path in store.path(PREVIOUS_RUNS).iterdir()) == ["run1"]


def test_run_async_cleanly_exits_zero_after_loop_cleanup() -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_async_cleanly(_async_noop())

    assert exc_info.value.code == 0
