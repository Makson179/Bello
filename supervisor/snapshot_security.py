"""Snapshot authority, path topology, and symlink boundary checks.

These checks consume captured authority; they never derive grants from mutable
runtime aliases. Copy sanitization operates only on an unpublished snapshot."""

from __future__ import annotations

from collections.abc import Sequence
import os
import stat
import subprocess
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        SnapshotSymlinkRewrite,
    )


def validate_plan_git_isolation(
    ops: SnapshotServices, /, project_root: Path, plan_path: Path
) -> None:
    """Require a private plan input that reviewers cannot recover from Git.

    Completion review receives a faithful clone of the submitted repository's Git
    metadata.  A plan that is tracked now, or was committed on any reachable ref,
    therefore cannot be made genuinely reviewer-blind without rewriting project
    history.  Reject that ambiguous case before constructing the coder snapshot.
    """

    original_root = project_root.resolve()
    _original_plan, plan_relative = ops._resolve_plan_input(original_root, plan_path)
    if not ops._is_top_level_git_repository(original_root):
        return
    relative = plan_relative.as_posix()
    tracked = subprocess.run(
        [
            ops._git_executable(original_root),
            "--literal-pathspecs",
            "ls-files",
            "--error-unmatch",
            "--",
            relative,
        ],
        cwd=original_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tracked.returncode == 0:
        raise ops.WorkspaceSnapshotError(
            f"private plan path is tracked by Git and cannot be hidden from reviewers: {relative}"
        )
    if tracked.returncode != 1:
        detail = tracked.stderr.decode("utf-8", errors="replace").strip()
        raise ops.WorkspaceSnapshotError(
            "failed to verify that the private plan is untracked"
            + (f": {detail}" if detail else "")
        )
    history = subprocess.run(
        [
            ops._git_executable(original_root),
            "--literal-pathspecs",
            "log",
            "--all",
            "--format=%H",
            "--",
            relative,
        ],
        cwd=original_root,
        env=ops._isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if history.returncode != 0:
        detail = history.stderr.strip()
        raise ops.WorkspaceSnapshotError(
            "failed to inspect reachable Git history for the private plan"
            + (f": {detail}" if detail else "")
        )
    if history.stdout.strip():
        raise ops.WorkspaceSnapshotError(
            "private plan path appears in reachable Git history and cannot be hidden "
            f"from reviewers: {relative}"
        )


def _resolve_plan_input(
    ops: SnapshotServices, /, project_root: Path, plan_path: Path
) -> tuple[Path, Path]:
    root = project_root.resolve()
    candidate = Path(plan_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.absolute()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"private plan input is missing or unreadable: {candidate}"
        ) from exc
    if ops.is_link_or_reparse(candidate, stat_result=metadata) or not stat.S_ISREG(
        metadata.st_mode
    ):
        raise ops.WorkspaceSnapshotError(
            f"private plan input must be a regular file, not a link or directory: {candidate}"
        )
    if metadata.st_nlink > 1:
        raise ops.WorkspaceSnapshotError(
            f"private plan input must not be a hardlink: {candidate}"
        )
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ops.WorkspaceSnapshotError(
            f"private plan input must be inside the project root: {candidate}"
        ) from exc
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to resolve private plan input: {candidate}"
        ) from exc
    reserved_names = {
        ops._name_key(name) for name in ops.SNAPSHOT_RESERVED_TASK_PATH_NAMES
    }
    reserved_part = next(
        (part for part in relative.parts if ops._name_key(part) in reserved_names),
        None,
    )
    if reserved_part is not None:
        raise ops.WorkspaceSnapshotError(
            "private plan input cannot be inside Bello runtime, cache, or dependency "
            f"directory: {reserved_part}"
        )
    if relative.name.casefold() == "agents.md":
        raise ops.WorkspaceSnapshotError(
            "private plan input cannot be named AGENTS.md because Codex loads it as "
            "workspace instructions"
        )
    if any("\n" in part or "\r" in part for part in relative.parts):
        raise ops.WorkspaceSnapshotError("private plan path cannot contain a newline")
    return resolved, relative


def _audit_windows_snapshot_before_git(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> None:
    """Fail closed on coder-created topology before invoking native Git.

    Runtime state and dependency copies are explicitly excluded from Git's
    pathspec and can be very large, so their ordinary directory contents are
    verified by the existing manifests instead of being walked here.  Their
    roots must still remain regular directories.  Every Git-visible entry,
    including `.git` itself, is inspected with lstat and stable directory IDs.
    """

    root = snapshot.snapshot_root
    skipped_roots = tuple(
        tuple(part.casefold() for part in PureWindowsPath(value).parts)
        for value in (".supervisor", *snapshot.readonly_dependency_paths)
    )

    def relative_parts(path: Path) -> tuple[str, ...]:
        return tuple(part.casefold() for part in path.relative_to(root).parts)

    def is_skipped(path: Path) -> bool:
        parts = relative_parts(path)
        return any(
            len(parts) >= len(prefix) and parts[: len(prefix)] == prefix
            for prefix in skipped_roots
        )

    try:
        root_metadata = root.lstat()
        if ops.is_link_or_reparse(root, stat_result=root_metadata) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise ops.SnapshotPatchError(
                "snapshot workspace root was replaced or redirected"
            )
        stack: list[tuple[Path, os.stat_result]] = [(root, root_metadata)]
        while stack:
            directory, expected = stack.pop()
            current = directory.lstat()
            if (
                ops.is_link_or_reparse(directory, stat_result=current)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (expected.st_dev, expected.st_ino)
            ):
                raise ops.SnapshotPatchError(
                    f"snapshot directory changed or was redirected before Git: {directory}"
                )
            children = list(directory.iterdir())
            ops._validate_windows_directory_names(
                directory, [child.name for child in children]
            )
            stable = directory.lstat()
            if (stable.st_dev, stable.st_ino) != (current.st_dev, current.st_ino):
                raise ops.SnapshotPatchError(
                    f"snapshot directory changed during pre-Git audit: {directory}"
                )
            for child in children:
                metadata = child.lstat()
                if ops.is_link_or_reparse(child, stat_result=metadata):
                    raise ops.SnapshotPatchError(
                        f"snapshot contains a Windows link/reparse entry before Git: {child}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if not is_skipped(child):
                        stack.append((child, metadata))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ops.SnapshotPatchError(
                        f"snapshot contains an unsupported filesystem entry before Git: {child}"
                    )
                if metadata.st_nlink > 1:
                    raise ops.SnapshotPatchError(
                        f"snapshot contains a hardlinked file before Git: {child}"
                    )
    except ops.SnapshotPatchError:
        raise
    except OSError as exc:
        raise ops.SnapshotPatchError(
            f"failed to audit native Windows snapshot before Git: {exc}"
        ) from exc


def _validate_windows_original_root(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> None:
    try:
        metadata = snapshot.original_root.lstat()
    except OSError as exc:
        raise ops.SnapshotPatchError(
            f"native Windows workspace root is missing or unreadable: {snapshot.original_root}"
        ) from exc
    if (
        ops.is_link_or_reparse(snapshot.original_root, stat_result=metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != snapshot.original_root_identity
    ):
        raise ops.SnapshotPatchError(
            "native Windows workspace root was replaced or redirected during the run"
        )


def _validate_snapshot_patch_paths(
    ops: SnapshotServices,
    /,
    original_root: Path,
    paths: tuple[str, ...],
    *,
    task_relative_path: str,
    declared_grading_roots: tuple[str | Path, ...],
    check_path_heuristics: bool = True,
) -> None:
    if any(ops._path_is_at_or_below(path, task_relative_path) for path in paths):
        raise ops.SnapshotPatchError(
            f"snapshot patch path rejected: task file is immutable: {task_relative_path}"
        )
    decision = ops.PolicyEngine(
        original_root,
        declared_grading_roots=declared_grading_roots,
        immutable_paths=(task_relative_path,),
    ).evaluate_patch_paths(
        list(paths),
        check_path_heuristics=check_path_heuristics,
    )
    if decision.kind != ops.PolicyDecisionKind.ALLOW:
        raise ops.SnapshotPatchError(f"snapshot patch path rejected: {decision.reason}")


def _validate_windows_patch_targets(
    ops: SnapshotServices, /, root: Path, paths: tuple[str, ...]
) -> None:
    for raw in paths:
        relative = PureWindowsPath(raw)
        if relative.is_absolute() or relative.drive or not relative.parts:
            raise ops.SnapshotPatchError(
                f"snapshot patch path is not Windows-relative: {raw}"
            )
        current = root
        for index, part in enumerate(relative.parts):
            if part in {".", ".."}:
                raise ops.SnapshotPatchError(
                    f"snapshot patch path contains traversal on Windows: {raw}"
                )
            if issue := ops.windows_path_component_issue(part):
                raise ops.SnapshotPatchError(
                    f"snapshot patch path is unsafe on Windows: {raw}: {issue}"
                )
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                # Once a component is absent, all remaining components are new and Git
                # apply will create them under the last verified regular directory.
                break
            if ops.is_link_or_reparse(current, stat_result=metadata):
                raise ops.SnapshotPatchError(
                    "snapshot patch target traverses a Windows reparse point or link: "
                    f"{raw}"
                )
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise ops.SnapshotPatchError(
                    f"snapshot patch parent is not a regular directory on Windows: {raw}"
                )
            if (
                index == len(relative.parts) - 1
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink > 1
            ):
                raise ops.SnapshotPatchError(
                    "snapshot patch refuses to modify a hardlinked Windows workspace file: "
                    f"{raw}"
                )


def _validate_symlink_targets(
    ops: SnapshotServices, /, snapshot_root: Path, paths: tuple[str, ...]
) -> None:
    root = snapshot_root.resolve()
    for raw in paths:
        path = root / raw
        if not ops.is_link_or_reparse(path):
            continue
        if ops._is_windows_platform():
            raise ops.SnapshotPatchError(
                "snapshot patch refuses Windows symlink/reparse changes because safe "
                f"creation cannot be guaranteed without elevated privileges: {raw}"
            )
        target = os.readlink(path)
        target_path = Path(target)
        if target_path.is_absolute():
            raise ops.SnapshotPatchError(
                f"snapshot patch creates or modifies absolute symlink: {raw} -> {target}"
            )
        candidate = path.parent / target_path
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ops.SnapshotPatchError(
                f"snapshot patch creates or modifies escaping symlink: {raw} -> {target}"
            ) from exc


def _verification_trusted_mounts(
    ops: SnapshotServices,
    /,
    original_root: Path,
    source_snapshot: WorkspaceSnapshot | None,
) -> dict[str, Path]:
    if source_snapshot is None:
        return {}
    if source_snapshot.snapshot_root.resolve() != original_root:
        raise ops.WorkspaceSnapshotError(
            "verification source snapshot does not match the submitted workspace"
        )
    if source_snapshot.runtime_exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
        if issue := source_snapshot.task_integrity_issue():
            raise ops.WorkspaceSnapshotError(
                f"trusted verification task exposure failed integrity validation: {issue}"
            )
        for relative in source_snapshot.readonly_dependency_paths:
            label = f"dependency:{relative}"
            try:
                manifest = ops._runtime_exposure_manifest(original_root / relative)
            except OSError as exc:
                raise ops.WorkspaceSnapshotError(
                    f"trusted verification dependency exposure is missing: {relative}"
                ) from exc
            if manifest != source_snapshot.runtime_copy_manifests.get(label, ()):
                raise ops.WorkspaceSnapshotError(
                    f"trusted verification dependency exposure was modified: {relative}"
                )
        return {}
    mounts: dict[str, Path] = {
        source_snapshot.task_relative_path: (
            source_snapshot.original_root / source_snapshot.task_relative_path
        ),
    }
    for relative in source_snapshot.readonly_dependency_paths:
        mounts[relative] = source_snapshot.original_root / relative
    for relative, target in mounts.items():
        link = original_root / relative
        if not ops._symlink_points_to(link, target):
            raise ops.WorkspaceSnapshotError(
                f"trusted verification mount is missing or redirected: {relative}"
            )
    return mounts


def _sanitize_copied_workspace_symlinks(
    ops: SnapshotServices,
    /,
    original_root: Path,
    snapshot_root: Path,
    *,
    trusted_external_symlinks: dict[str, Path] | None = None,
) -> tuple[tuple[SnapshotSymlinkRewrite, ...], tuple[str, ...]]:
    trusted_external_symlinks = trusted_external_symlinks or {}
    rewrites: list[SnapshotSymlinkRewrite] = []
    excluded: list[str] = []
    for current, dirs, files in os.walk(snapshot_root, followlinks=False):
        if Path(current) == snapshot_root:
            dirs[:] = [
                name for name in dirs if ops._name_key(name) != ops._name_key(".git")
            ]
        for name in sorted([*dirs, *files]):
            destination = Path(current) / name
            try:
                metadata = destination.lstat()
            except FileNotFoundError:
                continue
            if ops.is_reparse_point(
                destination, stat_result=metadata
            ) and not stat.S_ISLNK(metadata.st_mode):
                raise ops.WorkspaceSnapshotError(
                    "snapshot copy produced an unsupported Windows reparse entry: "
                    f"{destination}"
                )
            if not destination.is_symlink():
                continue
            relative = destination.relative_to(snapshot_root).as_posix()
            raw_target = os.readlink(destination)
            original_link = original_root / relative
            raw_target_path = Path(raw_target)
            target_candidate = (
                raw_target_path
                if raw_target_path.is_absolute()
                else original_link.parent / raw_target_path
            )
            try:
                resolved_target = target_candidate.resolve(strict=False)
                trusted_target = trusted_external_symlinks.get(relative)
                if (
                    trusted_target is not None
                    and resolved_target == trusted_target.resolve(strict=False)
                ):
                    continue
                target_relative = resolved_target.relative_to(original_root)
            except (OSError, ValueError):
                destination.unlink()
                excluded.append(relative)
                continue
            if not raw_target_path.is_absolute():
                continue
            snapshot_target = snapshot_root / target_relative
            safe_target = os.path.relpath(snapshot_target, start=destination.parent)
            destination.unlink()
            os.symlink(
                safe_target, destination, target_is_directory=resolved_target.is_dir()
            )
            rewrites.append(
                ops.SnapshotSymlinkRewrite(
                    path=relative,
                    original_target=raw_target,
                    snapshot_target=safe_target,
                )
            )
    return tuple(rewrites), tuple(excluded)


def _ensure_safe_runtime_destination_parent(
    ops: SnapshotServices, /, destination: Path, root: Path
) -> None:
    try:
        relative_parent = destination.parent.relative_to(root)
    except ValueError as exc:
        raise ops.WorkspaceSnapshotError(
            f"runtime exposure destination escapes snapshot root: {destination}"
        ) from exc
    current = root
    root_metadata = current.lstat()
    ops._assert_stable_regular_entry(current, root_metadata, require_directory=True)
    for part in relative_parent.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            metadata = current.lstat()
        ops._assert_stable_regular_entry(current, metadata, require_directory=True)


def _path_is_at_or_below(
    ops: SnapshotServices, /, raw_path: str, raw_parent: str
) -> bool:
    if ops._is_windows_platform():
        path_parts = tuple(part.casefold() for part in PureWindowsPath(raw_path).parts)
        parent_parts = tuple(
            part.casefold() for part in PureWindowsPath(raw_parent).parts
        )
    else:
        path_parts = Path(raw_path).parts
        parent_parts = Path(raw_parent).parts
    return (
        len(path_parts) >= len(parent_parts)
        and path_parts[: len(parent_parts)] == parent_parts
    )


def _validate_windows_directory_names(
    ops: SnapshotServices, /, directory: Path, names: Sequence[str]
) -> None:
    seen: dict[str, str] = {}
    for name in names:
        if issue := ops.windows_path_component_issue(name):
            raise ops.WorkspaceSnapshotError(
                f"Windows snapshot path is unsafe at {directory / name}: {issue}"
            )
        key = name.casefold()
        previous = seen.get(key)
        if previous is not None and previous != name:
            raise ops.WorkspaceSnapshotError(
                "Windows snapshot source contains case-colliding names in "
                f"{directory}: {previous!r} and {name!r}"
            )
        seen[key] = name


def _validate_windows_snapshot_source(
    ops: SnapshotServices,
    /,
    root: Path,
    *,
    original_task: Path | None,
    declared_roots: tuple[Path, ...],
) -> None:
    """Reject Windows source topology that cannot be copied and patched safely."""

    hardlinks: dict[tuple[int, int], list[tuple[str, int, bool]]] = {}
    dependency_names = {
        name.casefold() for name in ops.SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
    }

    git_entry = root / ".git"
    try:
        git_metadata = git_entry.lstat()
    except FileNotFoundError:
        git_metadata = None
    if git_metadata is not None and (
        ops.is_link_or_reparse(git_entry, stat_result=git_metadata)
        or not stat.S_ISDIR(git_metadata.st_mode)
    ):
        raise ops.WorkspaceSnapshotError(
            "native Windows workspace snapshots require .git to be a regular directory; "
            "linked worktrees and reparse-backed Git metadata are not supported"
        )

    def fail_walk(error: OSError) -> None:
        raise error

    try:
        for current, dirs, files in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=fail_walk,
        ):
            current_path = Path(current)
            relative_dir = current_path.relative_to(root)
            under_git = bool(
                relative_dir.parts and relative_dir.parts[0].casefold() == ".git"
            )
            ops._validate_windows_directory_names(current_path, [*dirs, *files])

            kept_dirs: list[str] = []
            for name in dirs:
                child = current_path / name
                metadata = child.lstat()
                child_under_git = under_git or (
                    not relative_dir.parts and name.casefold() == ".git"
                )
                if ops.is_link_or_reparse(child, stat_result=metadata):
                    raise ops.WorkspaceSnapshotError(
                        "native Windows workspace snapshots refuse symlinks, junctions, "
                        f"mount points, and other reparse entries: {child}"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ops.WorkspaceSnapshotError(
                        f"Windows snapshot source contains an unsupported entry: {child}"
                    )
                # Read-only dependency trees are materialized by a dedicated
                # copier that resolves only project-internal reparse targets,
                # breaks hardlinks, detects cycles, and emits a regular tree.
                # Do not reject common workspace/package-manager links before
                # that stricter, context-aware pass gets to inspect them.
                if name.casefold() in dependency_names and not child_under_git:
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs

            for name in files:
                child = current_path / name
                metadata = child.lstat()
                relative = child.relative_to(root).as_posix()
                child_under_git = under_git or (
                    not relative_dir.parts and name.casefold() == ".git"
                )
                if ops.is_link_or_reparse(child, stat_result=metadata):
                    raise ops.WorkspaceSnapshotError(
                        "native Windows workspace snapshots refuse symlinks, junctions, "
                        f"mount points, and other reparse entries: {child}"
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    raise ops.WorkspaceSnapshotError(
                        f"Windows snapshot source contains an unsupported entry: {child}"
                    )
                mutable = (
                    not child_under_git
                    and ops._windows_source_path_can_be_patched(
                        root,
                        child,
                        relative,
                        original_task=original_task,
                        declared_roots=declared_roots,
                    )
                )
                hardlinks.setdefault((metadata.st_dev, metadata.st_ino), []).append(
                    (relative, metadata.st_nlink, mutable)
                )
    except ops.WorkspaceSnapshotError:
        raise
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to audit native Windows workspace filesystem topology: {exc}"
        ) from exc

    for entries in hardlinks.values():
        link_count = max(entry[1] for entry in entries)
        if link_count <= 1 or not any(entry[2] for entry in entries):
            continue
        if link_count > len(entries):
            example = next(entry[0] for entry in entries if entry[2])
            raise ops.WorkspaceSnapshotError(
                "native Windows workspace file has a hardlink outside the audited project; "
                f"snapshot patching is unsafe: {example}"
            )


def _windows_source_path_can_be_patched(
    ops: SnapshotServices,
    /,
    root: Path,
    path: Path,
    relative: str,
    *,
    original_task: Path | None,
    declared_roots: tuple[Path, ...],
) -> bool:
    ignored_names = {
        name.casefold()
        for name in ops.SNAPSHOT_ALWAYS_IGNORE_NAMES
        | ops.SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
    }
    if any(part.casefold() in ignored_names for part in Path(relative).parts):
        return False
    if original_task is not None:
        try:
            if path.resolve(strict=False) == original_task:
                return False
        except OSError:
            return False
    if ops.is_protected_path(root, path) or ops.is_supervisor_runtime_path(root, path):
        return False
    return not ops._matches_declared_root(path, declared_roots)


def _resolve_declared_roots(
    ops: SnapshotServices, /, project_root: Path, roots: tuple[str | Path, ...]
) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for raw in roots:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = project_root / path
        try:
            resolved.append(path.resolve(strict=False))
        except OSError:
            continue
    return tuple(dict.fromkeys(resolved))


def _matches_declared_root(
    ops: SnapshotServices, /, path: Path, roots: tuple[Path, ...]
) -> bool:
    if not roots:
        return False
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        if resolved == root:
            return True
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False
