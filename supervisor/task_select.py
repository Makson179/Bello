from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from supervisor.filesystem_safety import (
    is_reparse_point,
    is_windows_platform,
    windows_path_component_issue,
)


EXCLUDED_DIRS = {".git", ".supervisor", "node_modules", "vendor", "dist", "build", "target", ".venv", "venv"}
PREFERRED_NAMES = ["TASK.md", "task.md", "PLAN.md", "plan.md", "TODO.md"]


class TaskSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class TaskCandidate:
    path: Path
    rank: int


def validate_task_path(path: Path, project_root: Path) -> Path:
    lexical_root = Path(os.path.abspath(project_root.expanduser()))
    lexical_path = Path(os.path.abspath(path.expanduser()))
    if _path_has_reparse_component(lexical_path, lexical_root):
        raise TaskSelectionError(f"task path traverses a Windows reparse point: {path}")
    resolved = path.expanduser().resolve()
    root = project_root.resolve()
    if not resolved.exists():
        raise TaskSelectionError(f"task file does not exist: {path}")
    if not resolved.is_file():
        raise TaskSelectionError(f"task path is not a file: {path}")
    if resolved.suffix.lower() != ".md":
        raise TaskSelectionError("task file must end in .md")
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise TaskSelectionError(f"task file must be inside project root: {path}") from exc
    if is_windows_platform():
        for part in relative.parts:
            if issue := windows_path_component_issue(part):
                raise TaskSelectionError(
                    f"task path contains an unsafe Windows name {part!r}: {issue}"
                )
    return resolved


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    if is_windows_platform():
        excluded = {name.casefold() for name in EXCLUDED_DIRS}
        return any(part.casefold() in excluded for part in rel.parts)
    return any(part in EXCLUDED_DIRS for part in rel.parts)


def _path_has_reparse_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
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


def scan_markdown_tasks(project_root: Path) -> list[Path]:
    root = project_root.resolve()
    candidates: list[TaskCandidate] = []
    preferred_names = {
        (name.casefold() if is_windows_platform() else name): rank
        for rank, name in reversed(tuple(enumerate(PREFERRED_NAMES)))
    }
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in dirs:
            directory = current_path / name
            if _is_excluded(directory, root) or is_reparse_point(directory):
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            path = current_path / name
            if path.suffix.lower() != ".md" or is_reparse_point(path):
                continue
            name_key = name.casefold() if is_windows_platform() else name
            preferred_rank = preferred_names.get(name_key, len(PREFERRED_NAMES))
            depth = len(path.relative_to(root).parts)
            candidates.append(
                TaskCandidate(path=path.resolve(), rank=preferred_rank * 1000 + depth)
            )
    return [candidate.path for candidate in sorted(candidates, key=lambda item: (item.rank, str(item.path)))]


def resolve_task(project_root: Path, task: Path | None, *, input_func=input, output_func=print) -> Path:
    root = project_root.resolve()
    if task is not None:
        return validate_task_path(task if task.is_absolute() else root / task, root)

    candidates = scan_markdown_tasks(root)
    if not candidates:
        raise TaskSelectionError("no markdown task file found")
    if len(candidates) == 1:
        return candidates[0]

    output_func("Select task file:")
    for index, candidate in enumerate(candidates, start=1):
        output_func(f"{index}. {candidate.relative_to(root)}")
    while True:
        raw = input_func("Task number: ").strip()
        try:
            selected = int(raw)
        except ValueError:
            output_func("Enter a number from the list.")
            continue
        if 1 <= selected <= len(candidates):
            return candidates[selected - 1]
        output_func("Enter a number from the list.")
