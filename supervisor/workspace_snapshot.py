"""Backward-compatible entry points and per-snapshot records.

Construction, comparison, authority checks, Git handling, runtime exposures, and
transactional application live in snapshot_* components. Each receives the typed
live facade as a dependency port: helper replacement here still affects nested
calls and existing snapshots. No component copies or synchronizes module globals.

Snapshot records retain the public constructors and field layout. Construction
initializes them; runtime owns subsequent control/manifest mutations; comparison
reads them; transaction alone owns writes back to the original workspace.
"""

# Historical imports are also dependency/monkeypatch seams for the live facade.
# ruff: noqa: F401

from __future__ import annotations

from collections.abc import Sequence
import errno
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Iterable, cast

from supervisor.filesystem_safety import (
    is_link_or_reparse,
    is_reparse_point,
    is_windows_platform as _host_is_windows_platform,
    remove_path_tree,
    windows_path_component_issue,
)
from supervisor.executables import ExecutableResolutionError, require_trusted_executable
from supervisor.policy import (
    PolicyEngine,
    is_protected_path,
    is_supervisor_runtime_path,
)
from supervisor.schemas import PolicyDecisionKind
from supervisor.snapshot_services import SnapshotServices
from supervisor import snapshot_construction as _construction
from supervisor import snapshot_state as _state
from supervisor import snapshot_security as _security
from supervisor import snapshot_transaction as _transaction
from supervisor import snapshot_git as _git
from supervisor import snapshot_runtime as _runtime
from supervisor import snapshot_filesystem as _filesystem
from supervisor import snapshot_windows as _windows
from supervisor import snapshot_lifecycle as _lifecycle


def _services() -> SnapshotServices:
    return cast(SnapshotServices, sys.modules[__name__])


SNAPSHOT_ALWAYS_IGNORE_NAMES = {
    ".git",
    ".supervisor",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}

SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES = {
    ".venv",
    "venv",
    "node_modules",
}

SNAPSHOT_RESERVED_TASK_PATH_NAMES = (
    SNAPSHOT_ALWAYS_IGNORE_NAMES | SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
)

GENERATED_ARTIFACT_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
}

GENERATED_ARTIFACT_FILE_NAMES = {
    ".coverage",
    ".ds_store",
    "cmakecache.txt",
    "coverage.xml",
}

GENERATED_ARTIFACT_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".gcda",
    ".gcno",
    ".tsbuildinfo",
}

VERIFICATION_MUTABLE_ARTIFACT_DIR_NAMES = GENERATED_ARTIFACT_DIR_NAMES | {".cache"}

VERIFICATION_BUILD_ARTIFACT_DIR_NAMES = VERIFICATION_MUTABLE_ARTIFACT_DIR_NAMES | {
    ".gradle",
    ".next",
    "build",
    "coverage",
    "dist",
    "target",
}

VERIFICATION_BUILD_ARTIFACT_SUFFIXES = GENERATED_ARTIFACT_SUFFIXES | {
    ".a",
    ".class",
    ".dll",
    ".dylib",
    ".exe",
    ".jar",
    ".lib",
    ".log",
    ".o",
    ".obj",
    ".so",
    ".wasm",
}

VERIFICATION_SAFE_GIT_CONFIG: dict[str, set[str] | None] = {
    "core.filemode": {"true", "false"},
    "core.ignorecase": {"true", "false"},
    "core.symlinks": {"true", "false"},
    "core.precomposeunicode": {"true", "false"},
    "core.autocrlf": {"true", "false", "input"},
    "core.eol": {"lf", "crlf", "native"},
    "core.safecrlf": {"true", "false", "warn"},
    "core.sparsecheckout": {"true", "false"},
    "core.sparsecheckoutcone": {"true", "false"},
    "index.sparse": {"true", "false"},
}

RUNTIME_EXPOSURE_SYMLINK = "symlink"
RUNTIME_EXPOSURE_COPY = "copy"
PRIVATE_PLAN_EXCLUDE_BEGIN = "# bello-private-plan-input: begin"
PRIVATE_PLAN_EXCLUDE_END = "# bello-private-plan-input: end"
BELLO_LAUNCHER_RUNTIME_PARTS = (".codex", "bello-run")

# ReadDirectoryChangesW filters used for materialized dependency trees.  Do not
# include FILE_NOTIFY_CHANGE_SECURITY (0x00000100): Codex's native Windows
# sandbox installs inheritable capability ACEs on the snapshot root before a
# command starts, and Windows propagates those controller-owned ACL changes to
# descendants.  Treating that propagation as a coder write would make the
# first read-only command fail.  Name, attribute, size, and last-write events
# still cover mutations that can affect dependency contents or resolution.
_WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER = (
    0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
    | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
    | 0x00000004  # FILE_NOTIFY_CHANGE_ATTRIBUTES
    | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
    | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
)


class WorkspaceSnapshotError(RuntimeError):
    pass


class SnapshotPatchError(WorkspaceSnapshotError):
    pass


@dataclass(frozen=True)
class VerificationWorkspaceSnapshot:
    """Disposable writable copy used only for review-time command execution."""

    original_root: Path
    snapshot_root: Path
    temp_root: Path
    submitted_manifest: tuple[tuple[str, SnapshotPathState], ...] = ()
    git_manifest: tuple[tuple[str, str], ...] = ()
    git_control_manifest: tuple[tuple[str, SnapshotPathState], ...] = ()
    mutable_submitted_paths: tuple[str, ...] = ()
    scratch_root: Path | None = None

    def cleanup(self) -> None:
        return _lifecycle._cleanup_verification_snapshot(_services(), self)

    def assert_submission_unchanged(self) -> None:
        return _state._assert_submission_unchanged(_services(), self)


@dataclass(frozen=True)
class SnapshotPatchResult:
    applied: bool
    changed_paths: tuple[str, ...] = ()
    patch_bytes: int = 0
    ignored_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotPatchSelection:
    changed_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotPathState:
    kind: str
    sha256: str | None = None
    executable: bool = False
    symlink_target: str | None = None


@dataclass(frozen=True)
class SnapshotSymlinkRewrite:
    path: str
    original_target: str
    snapshot_target: str


class _WindowsRuntimeFileGuard(_windows._WindowsRuntimeFileGuard):
    @classmethod
    def open(cls, path: Path) -> "_WindowsRuntimeFileGuard":
        return super().open(path)

    @classmethod
    def _services(cls) -> SnapshotServices:
        return _services()


class _WindowsDirectoryChangeWatcher(_windows._WindowsDirectoryChangeWatcher):
    @classmethod
    def open(cls, path: Path) -> "_WindowsDirectoryChangeWatcher":
        return super().open(path)

    @classmethod
    def _services(cls) -> SnapshotServices:
        return _services()


@dataclass(frozen=True)
class WorkspaceSnapshot:
    original_root: Path
    original_root_identity: tuple[int, int]
    snapshot_root: Path
    temp_root: Path
    task_path: Path
    task_relative_path: str
    task_bytes: bytes
    task_sha256: str
    baseline_commit: str
    git_config_bytes: bytes
    git_config_mode: int
    git_worktree_config_bytes: bytes | None
    git_worktree_config_mode: int | None
    plan_source_path: Path | None = None
    plan_path: Path | None = None
    plan_relative_path: str | None = None
    plan_bytes: bytes | None = field(default=None, repr=False, compare=False)
    plan_sha256: str | None = None
    plan_exposed: bool = False
    readonly_dependency_paths: tuple[str, ...] = ()
    # Canonical external authorities captured from controller-owned sources at
    # snapshot creation. Never derive grants from coder-writable aliases later.
    readonly_dependency_roots: tuple[Path, ...] = ()
    declared_grading_roots: tuple[str | Path, ...] = ()
    rewritten_symlinks: tuple[SnapshotSymlinkRewrite, ...] = ()
    excluded_external_symlink_paths: tuple[str, ...] = ()
    runtime_exposure_mode: str = RUNTIME_EXPOSURE_SYMLINK
    runtime_copy_manifests: dict[str, tuple[tuple[str, SnapshotPathState], ...]] = (
        field(default_factory=dict, repr=False, compare=False)
    )
    windows_runtime_file_guards: dict[str, _WindowsRuntimeFileGuard] = field(
        default_factory=dict, repr=False, compare=False
    )
    windows_dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher] = field(
        default_factory=dict, repr=False, compare=False
    )
    runtime_integrity_issues: list[str] = field(
        default_factory=list, repr=False, compare=False
    )
    # Controller-private durable authority; never populated from model input.
    recovery_authority_path: Path | None = field(default=None, repr=False, compare=False)
    recovery_run_id: str | None = field(default=None, repr=False, compare=False)
    recovery_authority_digest: str | None = field(default=None, repr=False, compare=False)
    recovery_identity: dict = field(default_factory=dict, repr=False, compare=False)
    recovery_cache: dict = field(default_factory=dict, repr=False, compare=False)

    def cleanup(self) -> None:
        return _lifecycle._cleanup_workspace_snapshot(_services(), self)

    def restore_runtime_links(self) -> tuple[str, ...]:
        return _runtime._restore_snapshot_runtime_links(_services(), self)

    def task_integrity_issue(self) -> str | None:
        return _runtime._snapshot_task_integrity_issue(_services(), self)

    def plan_integrity_issue(self) -> str | None:
        return _runtime._snapshot_plan_integrity_issue(_services(), self)

    def detach_plan_exposure(self) -> bool:
        """Remove the initial-coder-only plan mount without deleting its source."""

        return _runtime._detach_plan_exposure(_services(), self)

    def runtime_integrity_issue(self) -> str | None:
        return _runtime._snapshot_runtime_integrity_issue(_services(), self)

    def close_windows_runtime_controls(self) -> None:
        return _runtime._release_windows_runtime_controls(_services(), self)

    def git_control_is_trusted(self) -> bool:
        return _git._git_control_is_trusted(_services(), self)

    def restore_git_control(self) -> bool:
        return _git._restore_git_control(_services(), self)

    def preserve(self, destination: Path) -> Path:
        return _lifecycle._preserve_workspace_snapshot(_services(), self, destination)


def _windows_api_path(path: Path) -> str:
    """Return an absolute extended-length spelling for Win32 file APIs."""
    return _windows._windows_api_path(_services(), path)


def _close_windows_handle(handle: int) -> None:
    return _windows._close_windows_handle(_services(), handle)


def _is_windows_platform() -> bool:
    return _windows._is_windows_platform(_services())


def _runtime_exposure_mode() -> str:
    return _windows._runtime_exposure_mode(_services())


def _native_windows_runtime_controls_enabled() -> bool:
    return _windows._native_windows_runtime_controls_enabled(_services())


def _name_key(name: str) -> str:
    return _windows._name_key(_services(), name)


def _close_windows_runtime_controls(
    file_guards: dict[str, _WindowsRuntimeFileGuard],
    dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher],
) -> None:
    """Best-effort unwind used while snapshot construction already has an error."""
    return _runtime._close_windows_runtime_controls(
        _services(), file_guards, dependency_watchers
    )


def _scrub_private_plan_from_snapshot_git(snapshot: WorkspaceSnapshot) -> None:
    """Remove any coder-created Git reference to the private plan before revision.

    The ordinary ignore rule prevents routine staging, but a coder can explicitly use
    ``git add -f``.  Completion receives a fresh reachable-object clone and is already
    protected; a revision coder reuses this snapshot, so its index, reflogs, and loose
    objects must be scrubbed before that fresh thread starts.
    """
    return _git._scrub_private_plan_from_snapshot_git(_services(), snapshot)


def validate_plan_git_isolation(project_root: Path, plan_path: Path) -> None:
    """Require a private plan input that reviewers cannot recover from Git.

    Completion review receives a faithful clone of the submitted repository's Git
    metadata.  A plan that is tracked now, or was committed on any reachable ref,
    therefore cannot be made genuinely reviewer-blind without rewriting project
    history.  Reject that ambiguous case before constructing the coder snapshot.
    """
    return _security.validate_plan_git_isolation(_services(), project_root, plan_path)


def _resolve_plan_input(project_root: Path, plan_path: Path) -> tuple[Path, Path]:
    return _security._resolve_plan_input(_services(), project_root, plan_path)


def _gitignore_literal_path(raw_path: str) -> str:
    return _git._gitignore_literal_path(_services(), raw_path)


def create_workspace_snapshot(
    project_root: Path,
    task_path: Path,
    *,
    plan_path: Path | None = None,
    declared_grading_roots: Iterable[str | Path] = (),
    prefix: str = "bello-coder-",
) -> WorkspaceSnapshot:
    return _construction.create_workspace_snapshot(
        _services(),
        project_root,
        task_path,
        plan_path=plan_path,
        declared_grading_roots=declared_grading_roots,
        prefix=prefix,
    )


def create_verification_workspace_snapshot(
    project_root: Path,
    *,
    source_snapshot: WorkspaceSnapshot | None = None,
    prefix: str = "bello-completion-",
) -> VerificationWorkspaceSnapshot:
    """Copy the submitted state without making review artifacts part of the submission.

    Unlike the coder snapshot, this snapshot preserves the submitted repository's HEAD,
    index, worktree changes, and untracked files.  Completion Review can therefore run
    existing checks and inspect the real diff while any caches, temporary files, or other
    incidental writes remain disposable.
    """
    return _construction.create_verification_workspace_snapshot(
        _services(), project_root, source_snapshot=source_snapshot, prefix=prefix
    )


def _create_verification_scratch(snapshot_root: Path) -> Path:
    """Allocate a private persistent scratch directory before exposing the snapshot."""
    return _construction._create_verification_scratch(_services(), snapshot_root)


def copy_isolated_workspace_tree(
    source_root: Path, destination_root: Path, *, ignore=None
) -> None:
    """Copy a disposable workspace without retaining links to its source or host."""
    return _construction.copy_isolated_workspace_tree(
        _services(), source_root, destination_root, ignore=ignore
    )


def remove_isolated_workspace_tree(path: Path) -> None:
    return _filesystem.remove_isolated_workspace_tree(_services(), path)


def apply_snapshot_patch(
    snapshot: WorkspaceSnapshot, *, runtime_enabled: bool = True
) -> SnapshotPatchResult:
    """Export a candidate with mandatory authority checks in either runtime mode."""
    return _transaction.apply_snapshot_patch(
        _services(), snapshot, runtime_enabled=runtime_enabled
    )


def _apply_snapshot_patch(
    snapshot: WorkspaceSnapshot, *, runtime_enabled: bool = True
) -> SnapshotPatchResult:
    return _transaction._apply_snapshot_patch(
        _services(), snapshot, runtime_enabled=runtime_enabled
    )


def _audit_windows_snapshot_before_git(snapshot: WorkspaceSnapshot) -> None:
    """Fail closed on coder-created topology before invoking native Git.

    Runtime state and dependency copies are explicitly excluded from Git's
    pathspec and can be very large, so their ordinary directory contents are
    verified by the existing manifests instead of being walked here.  Their
    roots must still remain regular directories.  Every Git-visible entry,
    including `.git` itself, is inspected with lstat and stable directory IDs.
    """
    return _security._audit_windows_snapshot_before_git(_services(), snapshot)


def _validate_windows_original_root(snapshot: WorkspaceSnapshot) -> None:
    return _security._validate_windows_original_root(_services(), snapshot)


def _restore_runtime_links(snapshot: WorkspaceSnapshot) -> tuple[str, ...]:
    return _runtime._restore_runtime_links(_services(), snapshot)


def _runtime_task_integrity_issue(snapshot: WorkspaceSnapshot) -> str | None:
    return _runtime._runtime_task_integrity_issue(_services(), snapshot)


def _runtime_plan_integrity_issue(snapshot: WorkspaceSnapshot) -> str | None:
    return _runtime._runtime_plan_integrity_issue(_services(), snapshot)


def _snapshot_patch_selection(snapshot: WorkspaceSnapshot) -> SnapshotPatchSelection:
    return _state._snapshot_patch_selection(_services(), snapshot)


def _filter_snapshot_patch_paths(
    snapshot_root: Path,
    changed_paths: tuple[str, ...],
    *,
    readonly_dependency_paths: tuple[str, ...],
    private_runtime_paths: tuple[str, ...] = (),
) -> SnapshotPatchSelection:
    return _state._filter_snapshot_patch_paths(
        _services(),
        snapshot_root,
        changed_paths,
        readonly_dependency_paths=readonly_dependency_paths,
        private_runtime_paths=private_runtime_paths,
    )


def _snapshot_patch(snapshot: WorkspaceSnapshot, changed_paths: Sequence[str]) -> bytes:
    return _state._snapshot_patch(_services(), snapshot, changed_paths)


def _validate_snapshot_patch_paths(
    original_root: Path,
    paths: tuple[str, ...],
    *,
    task_relative_path: str,
    declared_grading_roots: tuple[str | Path, ...],
    check_path_heuristics: bool = True,
) -> None:
    return _security._validate_snapshot_patch_paths(
        _services(),
        original_root,
        paths,
        task_relative_path=task_relative_path,
        declared_grading_roots=declared_grading_roots,
        check_path_heuristics=check_path_heuristics,
    )


def _validate_windows_patch_targets(root: Path, paths: tuple[str, ...]) -> None:
    return _security._validate_windows_patch_targets(_services(), root, paths)


def _validate_symlink_targets(snapshot_root: Path, paths: tuple[str, ...]) -> None:
    return _security._validate_symlink_targets(_services(), snapshot_root, paths)


def _apply_patch_to_original(
    snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...], patch: bytes
) -> None:
    return _transaction._apply_patch_to_original(
        _services(), snapshot, changed_paths, patch
    )


def _normalize_original_symlink_baselines(
    snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...]
) -> None:
    return _transaction._normalize_original_symlink_baselines(
        _services(), snapshot, changed_paths
    )


def _rewritten_symlink_paths_for_changes(
    snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...]
) -> tuple[str, ...]:
    return _transaction._rewritten_symlink_paths_for_changes(
        _services(), snapshot, changed_paths
    )


def _init_snapshot_git(snapshot_root: Path) -> str:
    return _git._init_snapshot_git(_services(), snapshot_root)


def _restore_trusted_snapshot_git_config(snapshot: WorkspaceSnapshot) -> None:
    return _git._restore_trusted_snapshot_git_config(_services(), snapshot)


def _detach_recovery_workspace(snapshot: WorkspaceSnapshot) -> None:
    return _runtime._detach_recovery_workspace(_services(), snapshot)


def _read_regular_file(path: Path) -> tuple[bytes, int]:
    return _filesystem._read_regular_file(_services(), path)


def _regular_file_matches(path: Path, expected: bytes) -> bool:
    return _filesystem._regular_file_matches(_services(), path, expected)


def _atomic_replace_bytes(path: Path, content: bytes, mode: int) -> None:
    return _filesystem._atomic_replace_bytes(_services(), path, content, mode)


def _clone_git_metadata(
    original_root: Path, snapshot_root: Path, *, fail_on_clone_error: bool = False
) -> bool:
    return _git._clone_git_metadata(
        _services(),
        original_root,
        snapshot_root,
        fail_on_clone_error=fail_on_clone_error,
    )


def _is_top_level_git_repository(root: Path) -> bool:
    return _git._is_top_level_git_repository(_services(), root)


def _verification_snapshot_ignore(
    original_root: Path, *, private_runtime_paths: tuple[str, ...] = ()
):
    return _construction._verification_snapshot_ignore(
        _services(), original_root, private_runtime_paths=private_runtime_paths
    )


def _verification_trusted_mounts(
    original_root: Path, source_snapshot: WorkspaceSnapshot | None
) -> dict[str, Path]:
    return _security._verification_trusted_mounts(
        _services(), original_root, source_snapshot
    )


def _verification_gitlink_paths(root: Path) -> tuple[str, ...]:
    return _git._verification_gitlink_paths(_services(), root)


def _git_metadata_path(root: Path, relative: str, *, required: bool) -> Path | None:
    return _git._git_metadata_path(_services(), root, relative, required=required)


def _copy_verification_git_file(
    original_root: Path, snapshot_root: Path, relative: str, *, required: bool = False
) -> None:
    return _git._copy_verification_git_file(
        _services(), original_root, snapshot_root, relative, required=required
    )


def _git_config_file_values(path: Path, key: str) -> list[str]:
    return _git._git_config_file_values(_services(), path, key)


def _git_config_has_include(path: Path) -> bool:
    return _git._git_config_has_include(_services(), path)


def _copy_verification_safe_git_config(
    original_root: Path, snapshot_root: Path
) -> None:
    return _git._copy_verification_safe_git_config(
        _services(), original_root, snapshot_root
    )


def _git_config_bool(root: Path, key: str) -> bool:
    return _git._git_config_bool(_services(), root, key)


def _reject_verification_git_alternates(root: Path) -> None:
    return _git._reject_verification_git_alternates(_services(), root)


def _copy_snapshot_git_index(original_root: Path, snapshot_root: Path) -> None:
    return _git._copy_snapshot_git_index(_services(), original_root, snapshot_root)


def _hide_verification_runtime_state(snapshot_root: Path) -> None:
    return _git._hide_verification_runtime_state(_services(), snapshot_root)


def _hide_verification_private_inputs(
    snapshot_root: Path, private_runtime_paths: tuple[str, ...]
) -> None:
    return _git._hide_verification_private_inputs(
        _services(), snapshot_root, private_runtime_paths
    )


def _remove_private_plan_git_exclude(snapshot_root: Path) -> None:
    return _git._remove_private_plan_git_exclude(_services(), snapshot_root)


def _verification_git_manifest(snapshot_root: Path) -> tuple[tuple[str, str], ...]:
    """Capture review-relevant Git semantics without hashing mutable object storage.

    Commands run during review may legitimately populate object/cache files, but they must
    not change which submitted revision/index/config the reviewer is judging.
    """
    return _state._verification_git_manifest(_services(), snapshot_root)


def _verification_git_control_manifest(
    snapshot_root: Path,
) -> tuple[tuple[str, SnapshotPathState], ...]:
    return _state._verification_git_control_manifest(_services(), snapshot_root)


def _sanitize_verification_snapshot_git(snapshot_root: Path) -> None:
    return _git._sanitize_verification_snapshot_git(_services(), snapshot_root)


def _sync_snapshot_remotes(original_root: Path, snapshot_root: Path) -> None:
    return _git._sync_snapshot_remotes(_services(), original_root, snapshot_root)


def _optional_git_lines(cwd: Path, args: list[str]) -> list[str]:
    return _git._optional_git_lines(_services(), cwd, args)


def _clear_snapshot_worktree(snapshot_root: Path) -> None:
    return _construction._clear_snapshot_worktree(_services(), snapshot_root)


def _sanitize_copied_workspace_symlinks(
    original_root: Path,
    snapshot_root: Path,
    *,
    trusted_external_symlinks: dict[str, Path] | None = None,
) -> tuple[tuple[SnapshotSymlinkRewrite, ...], tuple[str, ...]]:
    return _security._sanitize_copied_workspace_symlinks(
        _services(),
        original_root,
        snapshot_root,
        trusted_external_symlinks=trusted_external_symlinks,
    )


def _verification_worktree_manifest(
    snapshot_root: Path,
) -> tuple[tuple[str, SnapshotPathState], ...]:
    return _state._verification_worktree_manifest(_services(), snapshot_root)


def _is_verification_mutable_artifact_path(raw_path: str) -> bool:
    return _state._is_verification_mutable_artifact_path(_services(), raw_path)


def _verification_path_is_git_ignored(snapshot_root: Path, raw_path: str) -> bool:
    return _state._verification_path_is_git_ignored(
        _services(), snapshot_root, raw_path
    )


def _verification_mutable_submitted_paths(
    snapshot_root: Path, manifest: tuple[tuple[str, SnapshotPathState], ...]
) -> tuple[str, ...]:
    return _state._verification_mutable_submitted_paths(
        _services(), snapshot_root, manifest
    )


def _looks_like_build_artifact(path: Path, raw_path: str) -> bool:
    return _state._looks_like_build_artifact(_services(), path, raw_path)


def _create_windows_dependency_exposure(
    destination: Path, source: Path, *, project_root: Path, safe_destination_root: Path
) -> None:
    """Materialize a dependency tree without retaining Windows reparse links."""
    return _runtime._create_windows_dependency_exposure(
        _services(),
        destination,
        source,
        project_root=project_root,
        safe_destination_root=safe_destination_root,
    )


def _materialize_windows_dependency_directory(
    source: Path,
    destination: Path,
    *,
    project_root: Path,
    active_directory_ids: set[tuple[int, int]],
) -> None:
    return _runtime._materialize_windows_dependency_directory(
        _services(),
        source,
        destination,
        project_root=project_root,
        active_directory_ids=active_directory_ids,
    )


def _create_runtime_exposure(
    destination: Path,
    source: Path,
    *,
    mode: str,
    safe_destination_root: Path | None = None,
    excluded_root_names: tuple[str, ...] = (),
) -> None:
    return _runtime._create_runtime_exposure(
        _services(),
        destination,
        source,
        mode=mode,
        safe_destination_root=safe_destination_root,
        excluded_root_names=excluded_root_names,
    )


def _ensure_safe_runtime_destination_parent(destination: Path, root: Path) -> None:
    return _security._ensure_safe_runtime_destination_parent(
        _services(), destination, root
    )


def _runtime_exposure_manifest(
    path: Path, *, excluded_root_names: tuple[str, ...] = (),
) -> tuple[tuple[str, SnapshotPathState], ...]:
    return _state._runtime_exposure_manifest(
        _services(), path, excluded_root_names=excluded_root_names,
    )


def _runtime_directory_manifest(
    root: Path,
    directory: Path,
    expected: os.stat_result,
    entries: list[tuple[str, SnapshotPathState]],
    *,
    excluded_root_names: tuple[str, ...] = (),
) -> None:
    return _state._runtime_directory_manifest(
        _services(), root, directory, expected, entries,
        excluded_root_names=excluded_root_names,
    )


def _create_readonly_link(destination: Path, source: Path) -> None:
    return _runtime._create_readonly_link(_services(), destination, source)


def _symlink_points_to(path: Path, target: Path) -> bool:
    return _runtime._symlink_points_to(_services(), path, target)


def _backup_original_paths(
    original_root: Path, backup_root: Path, changed_paths: tuple[str, ...]
) -> tuple[tuple[str, bool], ...]:
    return _transaction._backup_original_paths(
        _services(), original_root, backup_root, changed_paths
    )


def _restore_original_paths(
    original_root: Path, backup_root: Path, entries: tuple[tuple[str, bool], ...]
) -> None:
    return _transaction._restore_original_paths(
        _services(), original_root, backup_root, entries
    )


def _verify_applied_paths(
    original_root: Path, snapshot_root: Path, changed_paths: tuple[str, ...]
) -> None:
    return _state._verify_applied_paths(
        _services(), original_root, snapshot_root, changed_paths
    )


def _snapshot_path_state(path: Path) -> SnapshotPathState:
    return _state._snapshot_path_state(_services(), path)


def _minimal_changed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    return _transaction._minimal_changed_paths(_services(), paths)


def _remove_path(path: Path) -> None:
    return _filesystem._remove_path(_services(), path)


def _cleanup_path_best_effort(path: Path) -> None:
    return _filesystem._cleanup_path_best_effort(_services(), path)


def _make_regular_entry_owner_writable(path: Path, metadata: os.stat_result) -> None:
    return _filesystem._make_regular_entry_owner_writable(_services(), path, metadata)


def _assert_stable_regular_entry(
    path: Path, expected: os.stat_result, *, require_directory: bool
) -> os.stat_result:
    return _filesystem._assert_stable_regular_entry(
        _services(), path, expected, require_directory=require_directory
    )


def _make_tree_owner_writable(path: Path) -> None:
    return _filesystem._make_tree_owner_writable(_services(), path)


def _sha256_file(path: Path) -> str:
    return _filesystem._sha256_file(_services(), path)


def _open_regular_file_no_follow(path: Path) -> int:
    return _filesystem._open_regular_file_no_follow(_services(), path)


def _path_is_at_or_below(raw_path: str, raw_parent: str) -> bool:
    return _security._path_is_at_or_below(_services(), raw_path, raw_parent)


def _git_executable(cwd: Path) -> str:
    return _git._git_executable(_services(), cwd)


def _run_git(cwd: Path, args: list[str], *, capture_bytes: bool = False) -> str | bytes:
    return _git._run_git(_services(), cwd, args, capture_bytes=capture_bytes)


def _run_git_apply(
    cwd: Path, args: list[str], patch: bytes
) -> subprocess.CompletedProcess[bytes]:
    return _git._run_git_apply(_services(), cwd, args, patch)


def _isolated_git_env() -> dict[str, str]:
    return _git._isolated_git_env(_services())


def snapshot_git_environment() -> dict[str, str]:
    return _git.snapshot_git_environment(_services())


def _format_apply_error(
    prefix: str, completed: subprocess.CompletedProcess[bytes]
) -> str:
    return _transaction._format_apply_error(_services(), prefix, completed)


def _is_generated_artifact_path(snapshot_root: Path, raw_path: str) -> bool:
    return _state._is_generated_artifact_path(_services(), snapshot_root, raw_path)


def _validate_windows_directory_names(directory: Path, names: Sequence[str]) -> None:
    return _security._validate_windows_directory_names(_services(), directory, names)


def _validate_windows_snapshot_source(
    root: Path, *, original_task: Path | None, declared_roots: tuple[Path, ...]
) -> None:
    """Reject Windows source topology that cannot be copied and patched safely."""
    return _security._validate_windows_snapshot_source(
        _services(), root, original_task=original_task, declared_roots=declared_roots
    )


def _windows_source_path_can_be_patched(
    root: Path,
    path: Path,
    relative: str,
    *,
    original_task: Path | None,
    declared_roots: tuple[Path, ...],
) -> bool:
    return _security._windows_source_path_can_be_patched(
        _services(),
        root,
        path,
        relative,
        original_task=original_task,
        declared_roots=declared_roots,
    )


def _snapshot_ignore(
    original_root: Path,
    declared_roots: tuple[Path, ...],
    *,
    original_task: Path,
    original_plan: Path | None,
    readonly_dependencies: list[tuple[Path, str]],
):
    return _construction._snapshot_ignore(
        _services(),
        original_root,
        declared_roots,
        original_task=original_task,
        original_plan=original_plan,
        readonly_dependencies=readonly_dependencies,
    )


def _resolve_declared_roots(
    project_root: Path, roots: tuple[str | Path, ...]
) -> tuple[Path, ...]:
    return _security._resolve_declared_roots(_services(), project_root, roots)


def _matches_declared_root(path: Path, roots: tuple[Path, ...]) -> bool:
    return _security._matches_declared_root(_services(), path, roots)
