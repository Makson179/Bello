from __future__ import annotations

import os
from pathlib import Path

import pytest

import supervisor.task_select as task_select_module
from supervisor.task_select import TaskSelectionError, resolve_task, scan_markdown_tasks, validate_task_path


def test_task_selection_ranking_and_exclusions(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("notes", encoding="utf-8")
    (tmp_path / "PLAN.md").write_text("plan", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "TASK.md").write_text("ignore", encoding="utf-8")

    candidates = scan_markdown_tasks(tmp_path)

    assert candidates[0] == (tmp_path / "PLAN.md").resolve()
    assert all("node_modules" not in path.parts for path in candidates)


def test_validate_task_requires_markdown_inside_project(tmp_path: Path) -> None:
    task = tmp_path / "TASK.txt"
    task.write_text("no", encoding="utf-8")

    with pytest.raises(TaskSelectionError):
        validate_task_path(task, tmp_path)


def test_resolve_task_uses_selector_for_multiple_candidates(tmp_path: Path) -> None:
    (tmp_path / "TASK.md").write_text("task", encoding="utf-8")
    (tmp_path / "notes.md").write_text("notes", encoding="utf-8")

    selected = resolve_task(tmp_path, None, input_func=lambda _: "2", output_func=lambda _: None)

    assert selected.name == "notes.md"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_task_scan_preserves_posix_file_symlink_behavior(tmp_path: Path) -> None:
    target = tmp_path / "task-source.md"
    target.write_text("task\n", encoding="utf-8")
    link = tmp_path / "TASK.md"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    assert validate_task_path(link, tmp_path) == target.resolve()
    assert target.resolve() in scan_markdown_tasks(tmp_path)


def test_task_scan_prunes_simulated_windows_reparse_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible = tmp_path / "TASK.md"
    visible.write_text("visible\n", encoding="utf-8")
    junction = tmp_path / "junction"
    junction.mkdir()
    hidden = junction / "PLAN.md"
    hidden.write_text("do not traverse\n", encoding="utf-8")
    real_reparse = task_select_module.is_reparse_point
    monkeypatch.setattr(
        task_select_module,
        "is_reparse_point",
        lambda path, stat_result=None: path == junction
        or real_reparse(path, stat_result=stat_result),
    )

    candidates = scan_markdown_tasks(tmp_path)

    assert visible.resolve() in candidates
    assert hidden.resolve() not in candidates


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create the reserved fixture name")
def test_windows_task_validation_rejects_reserved_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "CON.md"
    task.write_text("unsafe alias\n", encoding="utf-8")
    monkeypatch.setattr(task_select_module, "is_windows_platform", lambda: True)

    with pytest.raises(TaskSelectionError, match="reserved Windows device name"):
        validate_task_path(task, tmp_path)
