from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from supervisor.filesystem_safety import (
    is_link_or_reparse,
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
    return _validate_markdown_path(path, project_root, label="task")


def validate_plan_path(path: Path, project_root: Path) -> Path:
    lexical_path = Path(os.path.abspath(path.expanduser()))
    try:
        metadata = lexical_path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and is_link_or_reparse(
        lexical_path,
        stat_result=metadata,
    ):
        raise TaskSelectionError(f"plan file must be a regular file, not a link: {path}")
    resolved = _validate_markdown_path(path, project_root, label="plan")
    if resolved.name.casefold() == "agents.md":
        raise TaskSelectionError(
            "plan file cannot be named AGENTS.md because Codex loads it as workspace instructions"
        )
    return resolved


def _validate_markdown_path(path: Path, project_root: Path, *, label: str) -> Path:
    lexical_root = Path(os.path.abspath(project_root.expanduser()))
    lexical_path = Path(os.path.abspath(path.expanduser()))
    if _path_has_reparse_component(lexical_path, lexical_root):
        raise TaskSelectionError(f"{label} path traverses a Windows reparse point: {path}")
    resolved = path.expanduser().resolve()
    root = project_root.resolve()
    if not resolved.exists():
        raise TaskSelectionError(f"{label} file does not exist: {path}")
    if not resolved.is_file():
        raise TaskSelectionError(f"{label} path is not a file: {path}")
    if resolved.suffix.lower() != ".md":
        raise TaskSelectionError(f"{label} file must end in .md")
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise TaskSelectionError(f"{label} file must be inside project root: {path}") from exc
    if is_windows_platform():
        for part in relative.parts:
            if issue := windows_path_component_issue(part):
                raise TaskSelectionError(
                    f"{label} path contains an unsafe Windows name {part!r}: {issue}"
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


def scan_markdown_tasks(
    project_root: Path,
    *,
    excluded_paths: tuple[Path, ...] = (),
) -> list[Path]:
    root = project_root.resolve()
    excluded = {path.expanduser().resolve() for path in excluded_paths}
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
            resolved_path = path.resolve()
            if resolved_path in excluded:
                continue
            name_key = name.casefold() if is_windows_platform() else name
            preferred_rank = preferred_names.get(name_key, len(PREFERRED_NAMES))
            depth = len(path.relative_to(root).parts)
            candidates.append(
                TaskCandidate(path=resolved_path, rank=preferred_rank * 1000 + depth)
            )
    return [candidate.path for candidate in sorted(candidates, key=lambda item: (item.rank, str(item.path)))]


def resolve_task(
    project_root: Path,
    task: Path | None,
    *,
    plan_path: Path | None = None,
    input_func=input,
    output_func=print,
) -> Path:
    root = project_root.resolve()
    if task is not None:
        resolved_task = validate_task_path(task if task.is_absolute() else root / task, root)
        if plan_path is not None and resolved_task == plan_path.resolve():
            raise TaskSelectionError("task and plan must be different files")
        return resolved_task

    excluded_paths = (plan_path,) if plan_path is not None else ()
    candidates = scan_markdown_tasks(root, excluded_paths=excluded_paths)
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


def resolve_plan(project_root: Path, plan: Path | None) -> Path | None:
    if plan is None:
        return None
    root = project_root.resolve()
    return validate_plan_path(plan if plan.is_absolute() else root / plan, root)
