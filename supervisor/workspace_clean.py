from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

from supervisor.filesystem_safety import is_reparse_point, remove_path_tree


class WorkspaceCleanError(RuntimeError):
    pass


def clean_workspace_except_task(
    project_root: Path,
    task_path: Path,
    *,
    protected_paths: Iterable[str | Path] = (),
) -> list[Path]:
    lexical_root = Path(os.path.abspath(project_root.expanduser()))
    lexical_task = Path(os.path.abspath(task_path.expanduser()))
    if _path_has_reparse_component(lexical_task, lexical_root):
        raise WorkspaceCleanError(
            f"task path traverses a Windows reparse point: {task_path}"
        )
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
        try:
            metadata = child.lstat()
        except FileNotFoundError:
            continue
        if is_reparse_point(child, stat_result=metadata):
            _remove_entry(child, stat_result=metadata)
            removed.append(child)
            continue
        if any(_same_path(child, path) for path in preserved):
            continue
        filesystem_link = stat.S_ISLNK(metadata.st_mode) or is_reparse_point(
            child, stat_result=metadata
        )
        if (
            stat.S_ISDIR(metadata.st_mode)
            and not filesystem_link
            and any(_contains_path(child, path) for path in preserved)
        ):
            _clean_dir(child, preserved, removed)
            continue
        _remove_entry(child, stat_result=metadata)
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
        if _path_has_reparse_component(candidate, root):
            raise WorkspaceCleanError(
                f"protected path traverses a Windows reparse point: {candidate}"
            )
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


def _path_has_reparse_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if is_reparse_point(current, stat_result=metadata):
            return True
    return False


def _remove_entry(path: Path, *, stat_result: os.stat_result | None = None) -> None:
    remove_path_tree(path, stat_result=stat_result)
