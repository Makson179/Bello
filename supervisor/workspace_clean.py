from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path


class WorkspaceCleanError(RuntimeError):
    pass


def clean_workspace_except_task(
    project_root: Path,
    task_path: Path,
    *,
    protected_paths: Iterable[str | Path] = (),
) -> list[Path]:
    root = project_root.resolve()
    task = task_path.resolve()
    if not task.is_file():
        raise WorkspaceCleanError(f"task file does not exist: {task_path}")
    try:
        task.relative_to(root)
    except ValueError as exc:
        raise WorkspaceCleanError(f"task file must be inside project root: {task_path}") from exc

    preserved = (task, *_existing_paths_in_root(root, protected_paths))
    removed: list[Path] = []
    _clean_dir(root, preserved, removed)
    return removed


def _clean_dir(directory: Path, preserved: tuple[Path, ...], removed: list[Path]) -> None:
    for child in directory.iterdir():
        if any(_same_path(child, path) for path in preserved):
            continue
        if child.is_dir() and not child.is_symlink() and any(_contains_path(child, path) for path in preserved):
            _clean_dir(child, preserved, removed)
            continue
        _remove_entry(child)
        removed.append(child)


def _existing_paths_in_root(root: Path, paths: Iterable[str | Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.abspath(candidate))
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists() or candidate.is_symlink():
            result.append(candidate)
    return tuple(dict.fromkeys(result))


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _remove_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
