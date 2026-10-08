"""Transactional export from a snapshot to the original workspace.

This module owns the original-workspace mutation boundary and the backup lifetime:
validate, back up, normalize symlink baselines, check/apply, verify, or roll back.
Keep the pre-Git audit and target-validation order explicit in _apply_snapshot_patch."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices
from supervisor.snapshot_recovery import (
    begin_patch_transaction,
    commit_patch_transaction,
    previous_patch_result,
)

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        SnapshotPatchResult,
    )


def apply_snapshot_patch(
    ops: SnapshotServices,
    /,
    snapshot: WorkspaceSnapshot,
    *,
    runtime_enabled: bool = True,
) -> SnapshotPatchResult:
    """Export a candidate with mandatory authority checks in either runtime mode."""
    try:
        previous = previous_patch_result(snapshot)
        if previous is not None:
            return previous
        # A crash after this durable write has an explicitly uncertain outcome.
        # It must never reach Git apply for a second time on restart.
        begin_patch_transaction(snapshot)
        result = ops._apply_snapshot_patch(snapshot, runtime_enabled=runtime_enabled)
        commit_patch_transaction(snapshot, result)
        return result
    except ops.WorkspaceSnapshotError:
        raise
    except OSError as exc:
        raise ops.SnapshotPatchError(
            f"snapshot patch filesystem operation failed: {exc}"
        ) from exc


def _apply_snapshot_patch(
    ops: SnapshotServices,
    /,
    snapshot: WorkspaceSnapshot,
    *,
    runtime_enabled: bool = True,
) -> SnapshotPatchResult:
    ops._restore_trusted_snapshot_git_config(snapshot)
    if ops._is_windows_platform():
        # Git is an unsandboxed native executable.  Audit the mutable tree
        # before allowing it to enumerate or stage coder-controlled paths.
        # In particular, reject a hardlink to an external file before Git can
        # turn that alias into patch input.
        ops._audit_windows_snapshot_before_git(snapshot)
    selection = ops._snapshot_patch_selection(snapshot)
    changed_paths = selection.changed_paths
    if not changed_paths:
        return ops.SnapshotPatchResult(
            applied=False, ignored_paths=selection.ignored_paths
        )
    if ops._is_windows_platform():
        ops._validate_windows_original_root(snapshot)
    ops._validate_snapshot_patch_paths(
        snapshot.original_root,
        changed_paths,
        task_relative_path=snapshot.task_relative_path,
        declared_grading_roots=snapshot.declared_grading_roots,
        check_path_heuristics=runtime_enabled,
    )
    if ops._is_windows_platform():
        ops._validate_windows_patch_targets(snapshot.original_root, changed_paths)
    ops._validate_symlink_targets(snapshot.snapshot_root, changed_paths)
    patch = ops._snapshot_patch(snapshot, changed_paths)
    if not patch.strip():
        raise ops.SnapshotPatchError(
            "snapshot reported changed paths but produced an empty patch"
        )
    ops._apply_patch_to_original(snapshot, changed_paths, patch)
    return ops.SnapshotPatchResult(
        applied=True,
        changed_paths=changed_paths,
        patch_bytes=len(patch),
        ignored_paths=selection.ignored_paths,
    )


def _apply_patch_to_original(
    ops: SnapshotServices,
    /,
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
    patch: bytes,
) -> None:
    original_root = snapshot.original_root
    with tempfile.TemporaryDirectory(prefix="bello-patch-backup-") as raw_backup:
        backup_root = Path(raw_backup)
        normalized_symlink_paths = ops._rewritten_symlink_paths_for_changes(
            snapshot, changed_paths
        )
        backup_paths = tuple(dict.fromkeys((*changed_paths, *normalized_symlink_paths)))
        backup_entries = ops._backup_original_paths(
            original_root, backup_root, backup_paths
        )
        try:
            ops._normalize_original_symlink_baselines(snapshot, changed_paths)
            check = ops._run_git_apply(
                original_root, ["--check", "--binary", "--whitespace=nowarn"], patch
            )
            if check.returncode != 0:
                raise ops.SnapshotPatchError(
                    ops._format_apply_error(
                        "snapshot patch does not apply cleanly", check
                    )
                )
            applied = ops._run_git_apply(
                original_root, ["--binary", "--whitespace=nowarn"], patch
            )
            if applied.returncode != 0:
                raise ops.SnapshotPatchError(
                    ops._format_apply_error(
                        "snapshot patch apply failed after clean check", applied
                    )
                )
            ops._verify_applied_paths(
                original_root, snapshot.snapshot_root, changed_paths
            )
        except Exception:
            ops._restore_original_paths(original_root, backup_root, backup_entries)
            raise


def _normalize_original_symlink_baselines(
    ops: SnapshotServices,
    /,
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
) -> None:
    affected_paths = set(
        ops._rewritten_symlink_paths_for_changes(snapshot, changed_paths)
    )
    for rewrite in snapshot.rewritten_symlinks:
        if rewrite.path not in affected_paths:
            continue
        path = snapshot.original_root / rewrite.path
        if not path.is_symlink() or os.readlink(path) != rewrite.original_target:
            raise ops.SnapshotPatchError(
                f"real workspace changed at rewritten symlink path during the run: {rewrite.path}"
            )
        path.unlink()
        os.symlink(rewrite.snapshot_target, path)


def _rewritten_symlink_paths_for_changes(
    ops: SnapshotServices,
    /,
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        rewrite.path
        for rewrite in snapshot.rewritten_symlinks
        if any(
            ops._path_is_at_or_below(changed_path, rewrite.path)
            or ops._path_is_at_or_below(rewrite.path, changed_path)
            for changed_path in changed_paths
        )
    )


def _backup_original_paths(
    ops: SnapshotServices,
    /,
    original_root: Path,
    backup_root: Path,
    changed_paths: tuple[str, ...],
) -> tuple[tuple[str, bool], ...]:
    entries: list[tuple[str, bool]] = []
    for raw in ops._minimal_changed_paths(changed_paths):
        source = original_root / raw
        exists = source.exists() or source.is_symlink()
        entries.append((raw, exists))
        if not exists:
            continue
        destination = backup_root / raw
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), destination)
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        elif source.is_file():
            shutil.copy2(source, destination, follow_symlinks=False)
        else:
            raise ops.SnapshotPatchError(
                f"unsupported original path type during patch backup: {raw}"
            )
    return tuple(entries)


def _restore_original_paths(
    ops: SnapshotServices,
    /,
    original_root: Path,
    backup_root: Path,
    entries: tuple[tuple[str, bool], ...],
) -> None:
    failures: list[str] = []
    for raw, existed in entries:
        destination = original_root / raw
        try:
            ops._remove_path(destination)
            if not existed:
                continue
            source = backup_root / raw
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                os.symlink(os.readlink(source), destination)
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
        except OSError as exc:
            failures.append(f"{raw}: {exc}")
    if failures:
        raise ops.SnapshotPatchError(
            "snapshot patch rollback failed: " + "; ".join(failures)
        )


def _minimal_changed_paths(
    ops: SnapshotServices, /, paths: tuple[str, ...]
) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in sorted(paths, key=lambda value: (len(Path(value).parts), value)):
        if any(ops._path_is_at_or_below(raw, existing) for existing in selected):
            continue
        selected.append(raw)
    return tuple(selected)


def _format_apply_error(
    ops: SnapshotServices, /, prefix: str, completed: subprocess.CompletedProcess[bytes]
) -> str:
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    detail = stderr or stdout or f"exit {completed.returncode}"
    return f"{prefix}: {detail}"
