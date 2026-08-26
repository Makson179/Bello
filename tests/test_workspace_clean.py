from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import supervisor.filesystem_safety as filesystem_safety_module
import supervisor.workspace_clean as workspace_clean_module
from supervisor.workspace_clean import WorkspaceCleanError, clean_workspace_except_task


def test_clean_workspace_removes_everything_except_task(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("remove", encoding="utf-8")
    (tmp_path / ".supervisor").mkdir()
    (tmp_path / ".supervisor" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.txt").write_text("remove", encoding="utf-8")

    removed = clean_workspace_except_task(tmp_path, task)

    assert task.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["TASK.md"]
    assert {path.name for path in removed} == {"notes.txt", ".supervisor", "build"}


def test_clean_workspace_preserves_nested_task_parents(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = tasks_dir / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    (tasks_dir / "old.md").write_text("remove", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("remove", encoding="utf-8")

    clean_workspace_except_task(tmp_path, task)

    assert task.exists()
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == [
        "tasks",
        "tasks/TASK.md",
    ]


def test_clean_workspace_preserves_declared_protected_paths(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    protected_file = tmp_path / "grading" / "golden.json"
    protected_file.parent.mkdir()
    protected_file.write_text("{}", encoding="utf-8")
    protected_dir = tmp_path / "hidden-tests"
    protected_dir.mkdir()
    (protected_dir / "test_hidden.py").write_text("def test_hidden(): pass", encoding="utf-8")
    (tmp_path / "grading" / "remove.txt").write_text("remove", encoding="utf-8")
    (tmp_path / "src.py").write_text("remove", encoding="utf-8")

    clean_workspace_except_task(
        tmp_path,
        task,
        protected_paths=("grading/golden.json", protected_dir),
    )

    assert task.exists()
    assert protected_file.exists()
    assert (protected_dir / "test_hidden.py").exists()
    assert not (tmp_path / "grading" / "remove.txt").exists()
    assert not (tmp_path / "src.py").exists()


def test_clean_workspace_ignores_protected_paths_outside_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = workspace / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    removable = workspace / "remove.txt"
    removable.write_text("remove", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    clean_workspace_except_task(workspace, task, protected_paths=(outside,))

    assert not removable.exists()
    assert outside.exists()


def test_clean_workspace_rejects_task_outside_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    other = outside / "TASK.md"
    other.write_text("# Task", encoding="utf-8")

    with pytest.raises(WorkspaceCleanError):
        clean_workspace_except_task(workspace, other)


def test_clean_workspace_removes_simulated_junction_as_leaf_without_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    removable = tmp_path / "build"
    junction = removable / "junction"
    junction.mkdir(parents=True)
    external = tmp_path.parent / f"{tmp_path.name}-external.txt"
    external.write_text("outside\n", encoding="utf-8")
    removed_as_link: list[Path] = []
    real_reparse = workspace_clean_module.is_reparse_point
    real_link = filesystem_safety_module.is_link_or_reparse

    def simulated_reparse(path: Path, *, stat_result=None) -> bool:
        return path == junction or real_reparse(path, stat_result=stat_result)

    def remove_simulated_link(path: Path, *, stat_result=None) -> None:
        removed_as_link.append(path)
        path.rmdir()

    monkeypatch.setattr(workspace_clean_module, "is_reparse_point", simulated_reparse)
    monkeypatch.setattr(
        filesystem_safety_module,
        "is_link_or_reparse",
        lambda path, stat_result=None: path == junction
        or real_link(path, stat_result=stat_result),
    )
    monkeypatch.setattr(
        filesystem_safety_module,
        "remove_link_or_reparse",
        remove_simulated_link,
    )

    clean_workspace_except_task(tmp_path, task)

    assert removed_as_link == [junction]
    assert not removable.exists()
    assert external.read_text(encoding="utf-8") == "outside\n"
    external.unlink()


def test_clean_workspace_removes_read_only_nested_tree(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    artifact = locked / "artifact.txt"
    artifact.write_text("remove\n", encoding="utf-8")
    artifact.chmod(0o444)
    locked.chmod(0o555)

    clean_workspace_except_task(tmp_path, task)

    assert task.exists()
    assert not locked.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction behavior")
def test_clean_workspace_removes_native_junction_without_touching_target(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    external = tmp_path.parent / f"{tmp_path.name}-junction-target"
    external.mkdir()
    external_file = external / "outside.txt"
    external_file.write_text("outside\n", encoding="utf-8")
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "mklink", "/J", str(junction), str(external)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if created.returncode != 0:
        external_file.unlink()
        external.rmdir()
        pytest.skip(f"directory junction creation is unavailable: {created.stderr.strip()}")

    clean_workspace_except_task(tmp_path, task)

    assert not junction.exists()
    assert external_file.read_text(encoding="utf-8") == "outside\n"
    external_file.unlink()
    external.rmdir()
