"""Read-only dependency port for the snapshot components.

The compatibility facade implements this interface. Components receive it explicitly
and keep operation state in local variables or the supplied snapshot. Resolving
helpers through the live facade preserves legacy monkeypatch/instrumentation seams,
including calls made later by callbacks and previously constructed snapshots.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Protocol

if TYPE_CHECKING:
    from supervisor.executables import ExecutableResolutionError
    from supervisor.policy import PolicyEngine
    from supervisor.schemas import PolicyDecisionKind
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshotError,
        SnapshotPatchError,
        WorkspaceSnapshot,
        VerificationWorkspaceSnapshot,
        SnapshotPatchResult,
        SnapshotPatchSelection,
        SnapshotPathState,
        SnapshotSymlinkRewrite,
        _WindowsRuntimeFileGuard,
        _WindowsDirectoryChangeWatcher,
    )


class SnapshotServices(Protocol):
    """Dependencies only; this interface owns no mutable snapshot state."""

    def _windows_api_path(self, path: Path) -> str: ...

    def _close_windows_handle(self, handle: int) -> None: ...

    def _is_windows_platform(self) -> bool: ...

    def _runtime_exposure_mode(self) -> str: ...

    def _native_windows_runtime_controls_enabled(self) -> bool: ...

    def _name_key(self, name: str) -> str: ...

    def _close_windows_runtime_controls(
        self,
        file_guards: dict[str, _WindowsRuntimeFileGuard],
        dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher],
    ) -> None: ...

    def _scrub_private_plan_from_snapshot_git(
        self, snapshot: WorkspaceSnapshot
    ) -> None: ...

    def validate_plan_git_isolation(
        self, project_root: Path, plan_path: Path
    ) -> None: ...

    def _resolve_plan_input(
        self, project_root: Path, plan_path: Path
    ) -> tuple[Path, Path]: ...

    def _gitignore_literal_path(self, raw_path: str) -> str: ...

    def create_workspace_snapshot(
        self,
        project_root: Path,
        task_path: Path,
        *,
        plan_path: Path | None = None,
        declared_grading_roots: Iterable[str | Path] = (),
        prefix: str = "bello-coder-",
    ) -> WorkspaceSnapshot: ...

    def create_verification_workspace_snapshot(
        self,
        project_root: Path,
        *,
        source_snapshot: WorkspaceSnapshot | None = None,
        prefix: str = "bello-completion-",
    ) -> VerificationWorkspaceSnapshot: ...

    def _create_verification_scratch(self, snapshot_root: Path) -> Path: ...

    def copy_isolated_workspace_tree(
        self, source_root: Path, destination_root: Path, *, ignore=None
    ) -> None: ...

    def remove_isolated_workspace_tree(self, path: Path) -> None: ...

    def apply_snapshot_patch(
        self, snapshot: WorkspaceSnapshot, *, runtime_enabled: bool = True
    ) -> SnapshotPatchResult: ...

    def _apply_snapshot_patch(
        self, snapshot: WorkspaceSnapshot, *, runtime_enabled: bool = True
    ) -> SnapshotPatchResult: ...

    def _audit_windows_snapshot_before_git(
        self, snapshot: WorkspaceSnapshot
    ) -> None: ...

    def _validate_windows_original_root(self, snapshot: WorkspaceSnapshot) -> None: ...

    def _restore_runtime_links(
        self, snapshot: WorkspaceSnapshot
    ) -> tuple[str, ...]: ...

    def _runtime_task_integrity_issue(
        self, snapshot: WorkspaceSnapshot
    ) -> str | None: ...

    def _runtime_plan_integrity_issue(
        self, snapshot: WorkspaceSnapshot
    ) -> str | None: ...

    def _snapshot_patch_selection(
        self, snapshot: WorkspaceSnapshot
    ) -> SnapshotPatchSelection: ...

    def _filter_snapshot_patch_paths(
        self,
        snapshot_root: Path,
        changed_paths: tuple[str, ...],
        *,
        readonly_dependency_paths: tuple[str, ...],
        private_runtime_paths: tuple[str, ...] = (),
    ) -> SnapshotPatchSelection: ...

    def _snapshot_patch(
        self, snapshot: WorkspaceSnapshot, changed_paths: Sequence[str]
    ) -> bytes: ...

    def _validate_snapshot_patch_paths(
        self,
        original_root: Path,
        paths: tuple[str, ...],
        *,
        task_relative_path: str,
        declared_grading_roots: tuple[str | Path, ...],
        check_path_heuristics: bool = True,
    ) -> None: ...

    def _validate_windows_patch_targets(
        self, root: Path, paths: tuple[str, ...]
    ) -> None: ...

    def _validate_symlink_targets(
        self, snapshot_root: Path, paths: tuple[str, ...]
    ) -> None: ...

    def _apply_patch_to_original(
        self, snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...], patch: bytes
    ) -> None: ...

    def _normalize_original_symlink_baselines(
        self, snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...]
    ) -> None: ...

    def _rewritten_symlink_paths_for_changes(
        self, snapshot: WorkspaceSnapshot, changed_paths: tuple[str, ...]
    ) -> tuple[str, ...]: ...

    def _init_snapshot_git(self, snapshot_root: Path) -> str: ...

    def _restore_trusted_snapshot_git_config(
        self, snapshot: WorkspaceSnapshot
    ) -> None: ...

    def _detach_recovery_workspace(self, snapshot: WorkspaceSnapshot) -> None: ...

    def _read_regular_file(self, path: Path) -> tuple[bytes, int]: ...

    def _regular_file_matches(self, path: Path, expected: bytes) -> bool: ...

    def _atomic_replace_bytes(self, path: Path, content: bytes, mode: int) -> None: ...

    def _clone_git_metadata(
        self,
        original_root: Path,
        snapshot_root: Path,
        *,
        fail_on_clone_error: bool = False,
    ) -> bool: ...

    def _is_top_level_git_repository(self, root: Path) -> bool: ...

    def _verification_snapshot_ignore(
        self, original_root: Path, *, private_runtime_paths: tuple[str, ...] = ()
    ): ...

    def _verification_trusted_mounts(
        self, original_root: Path, source_snapshot: WorkspaceSnapshot | None
    ) -> dict[str, Path]: ...

    def _verification_gitlink_paths(self, root: Path) -> tuple[str, ...]: ...

    def _git_metadata_path(
        self, root: Path, relative: str, *, required: bool
    ) -> Path | None: ...

    def _copy_verification_git_file(
        self,
        original_root: Path,
        snapshot_root: Path,
        relative: str,
        *,
        required: bool = False,
    ) -> None: ...

    def _git_config_file_values(self, path: Path, key: str) -> list[str]: ...

    def _git_config_has_include(self, path: Path) -> bool: ...

    def _copy_verification_safe_git_config(
        self, original_root: Path, snapshot_root: Path
    ) -> None: ...

    def _git_config_bool(self, root: Path, key: str) -> bool: ...

    def _reject_verification_git_alternates(self, root: Path) -> None: ...

    def _copy_snapshot_git_index(
        self, original_root: Path, snapshot_root: Path
    ) -> None: ...

    def _hide_verification_runtime_state(self, snapshot_root: Path) -> None: ...

    def _hide_verification_private_inputs(
        self, snapshot_root: Path, private_runtime_paths: tuple[str, ...]
    ) -> None: ...

    def _remove_private_plan_git_exclude(self, snapshot_root: Path) -> None: ...

    def _verification_git_manifest(
        self, snapshot_root: Path
    ) -> tuple[tuple[str, str], ...]: ...

    def _verification_git_control_manifest(
        self, snapshot_root: Path
    ) -> tuple[tuple[str, SnapshotPathState], ...]: ...

    def _sanitize_verification_snapshot_git(self, snapshot_root: Path) -> None: ...

    def _sync_snapshot_remotes(
        self, original_root: Path, snapshot_root: Path
    ) -> None: ...

    def _optional_git_lines(self, cwd: Path, args: list[str]) -> list[str]: ...

    def _clear_snapshot_worktree(self, snapshot_root: Path) -> None: ...

    def _sanitize_copied_workspace_symlinks(
        self,
        original_root: Path,
        snapshot_root: Path,
        *,
        trusted_external_symlinks: dict[str, Path] | None = None,
    ) -> tuple[tuple[SnapshotSymlinkRewrite, ...], tuple[str, ...]]: ...

    def _verification_worktree_manifest(
        self, snapshot_root: Path
    ) -> tuple[tuple[str, SnapshotPathState], ...]: ...

    def _is_verification_mutable_artifact_path(self, raw_path: str) -> bool: ...

    def _verification_path_is_git_ignored(
        self, snapshot_root: Path, raw_path: str
    ) -> bool: ...

    def _verification_mutable_submitted_paths(
        self, snapshot_root: Path, manifest: tuple[tuple[str, SnapshotPathState], ...]
    ) -> tuple[str, ...]: ...

    def _looks_like_build_artifact(self, path: Path, raw_path: str) -> bool: ...

    def _create_windows_dependency_exposure(
        self,
        destination: Path,
        source: Path,
        *,
        project_root: Path,
        safe_destination_root: Path,
    ) -> None: ...

    def _materialize_windows_dependency_directory(
        self,
        source: Path,
        destination: Path,
        *,
        project_root: Path,
        active_directory_ids: set[tuple[int, int]],
    ) -> None: ...

    def _create_runtime_exposure(
        self,
        destination: Path,
        source: Path,
        *,
        mode: str,
        safe_destination_root: Path | None = None,
        excluded_root_names: tuple[str, ...] = (),
    ) -> None: ...

    def _ensure_safe_runtime_destination_parent(
        self, destination: Path, root: Path
    ) -> None: ...

    def _runtime_exposure_manifest(
        self, path: Path, *, excluded_root_names: tuple[str, ...] = (),
    ) -> tuple[tuple[str, SnapshotPathState], ...]: ...

    def _runtime_directory_manifest(
        self,
        root: Path,
        directory: Path,
        expected: os.stat_result,
        entries: list[tuple[str, SnapshotPathState]],
        *,
        excluded_root_names: tuple[str, ...] = (),
    ) -> None: ...

    def _create_readonly_link(self, destination: Path, source: Path) -> None: ...

    def _symlink_points_to(self, path: Path, target: Path) -> bool: ...

    def _backup_original_paths(
        self, original_root: Path, backup_root: Path, changed_paths: tuple[str, ...]
    ) -> tuple[tuple[str, bool], ...]: ...

    def _restore_original_paths(
        self,
        original_root: Path,
        backup_root: Path,
        entries: tuple[tuple[str, bool], ...],
    ) -> None: ...

    def _verify_applied_paths(
        self, original_root: Path, snapshot_root: Path, changed_paths: tuple[str, ...]
    ) -> None: ...

    def _snapshot_path_state(self, path: Path) -> SnapshotPathState: ...

    def _minimal_changed_paths(self, paths: tuple[str, ...]) -> tuple[str, ...]: ...

    def _remove_path(self, path: Path) -> None: ...

    def _cleanup_path_best_effort(self, path: Path) -> None: ...

    def _make_regular_entry_owner_writable(
        self, path: Path, metadata: os.stat_result
    ) -> None: ...

    def _assert_stable_regular_entry(
        self, path: Path, expected: os.stat_result, *, require_directory: bool
    ) -> os.stat_result: ...

    def _make_tree_owner_writable(self, path: Path) -> None: ...

    def _sha256_file(self, path: Path) -> str: ...

    def _open_regular_file_no_follow(self, path: Path) -> int: ...

    def _path_is_at_or_below(self, raw_path: str, raw_parent: str) -> bool: ...

    def _git_executable(self, cwd: Path) -> str: ...

    def _run_git(
        self, cwd: Path, args: list[str], *, capture_bytes: bool = False
    ) -> str | bytes: ...

    def _run_git_apply(
        self, cwd: Path, args: list[str], patch: bytes
    ) -> subprocess.CompletedProcess[bytes]: ...

    def _isolated_git_env(self) -> dict[str, str]: ...

    def snapshot_git_environment(self) -> dict[str, str]: ...

    def _format_apply_error(
        self, prefix: str, completed: subprocess.CompletedProcess[bytes]
    ) -> str: ...

    def _is_generated_artifact_path(
        self, snapshot_root: Path, raw_path: str
    ) -> bool: ...

    def _validate_windows_directory_names(
        self, directory: Path, names: Sequence[str]
    ) -> None: ...

    def _validate_windows_snapshot_source(
        self,
        root: Path,
        *,
        original_task: Path | None,
        declared_roots: tuple[Path, ...],
    ) -> None: ...

    def _windows_source_path_can_be_patched(
        self,
        root: Path,
        path: Path,
        relative: str,
        *,
        original_task: Path | None,
        declared_roots: tuple[Path, ...],
    ) -> bool: ...

    def _snapshot_ignore(
        self,
        original_root: Path,
        declared_roots: tuple[Path, ...],
        *,
        original_task: Path,
        original_plan: Path | None,
        readonly_dependencies: list[tuple[Path, str]],
    ): ...

    def _resolve_declared_roots(
        self, project_root: Path, roots: tuple[str | Path, ...]
    ) -> tuple[Path, ...]: ...

    def _matches_declared_root(self, path: Path, roots: tuple[Path, ...]) -> bool: ...

    @property
    def WorkspaceSnapshotError(self) -> type[WorkspaceSnapshotError]: ...

    @property
    def SnapshotPatchError(self) -> type[SnapshotPatchError]: ...

    @property
    def VerificationWorkspaceSnapshot(self) -> type[VerificationWorkspaceSnapshot]: ...

    @property
    def SnapshotPatchResult(self) -> type[SnapshotPatchResult]: ...

    @property
    def SnapshotPatchSelection(self) -> type[SnapshotPatchSelection]: ...

    @property
    def SnapshotPathState(self) -> type[SnapshotPathState]: ...

    @property
    def SnapshotSymlinkRewrite(self) -> type[SnapshotSymlinkRewrite]: ...

    @property
    def _WindowsRuntimeFileGuard(self) -> type[_WindowsRuntimeFileGuard]: ...

    @property
    def _WindowsDirectoryChangeWatcher(
        self,
    ) -> type[_WindowsDirectoryChangeWatcher]: ...

    @property
    def WorkspaceSnapshot(self) -> type[WorkspaceSnapshot]: ...

    @property
    def SNAPSHOT_ALWAYS_IGNORE_NAMES(self) -> set[str]: ...

    @property
    def SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES(self) -> set[str]: ...

    @property
    def SNAPSHOT_RESERVED_TASK_PATH_NAMES(self) -> set[str]: ...

    @property
    def GENERATED_ARTIFACT_DIR_NAMES(self) -> set[str]: ...

    @property
    def GENERATED_ARTIFACT_FILE_NAMES(self) -> set[str]: ...

    @property
    def GENERATED_ARTIFACT_SUFFIXES(self) -> set[str]: ...

    @property
    def VERIFICATION_MUTABLE_ARTIFACT_DIR_NAMES(self) -> set[str]: ...

    @property
    def VERIFICATION_BUILD_ARTIFACT_DIR_NAMES(self) -> set[str]: ...

    @property
    def VERIFICATION_BUILD_ARTIFACT_SUFFIXES(self) -> set[str]: ...

    @property
    def RUNTIME_EXPOSURE_SYMLINK(self) -> str: ...

    @property
    def RUNTIME_EXPOSURE_COPY(self) -> str: ...

    @property
    def PRIVATE_PLAN_EXCLUDE_BEGIN(self) -> str: ...

    @property
    def PRIVATE_PLAN_EXCLUDE_END(self) -> str: ...

    @property
    def BELLO_LAUNCHER_RUNTIME_PARTS(self) -> tuple[str, ...]: ...

    @property
    def _WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER(self) -> int: ...

    @property
    def VERIFICATION_SAFE_GIT_CONFIG(self) -> dict[str, set[str] | None]: ...

    @property
    def is_link_or_reparse(self) -> Callable[..., bool]: ...

    @property
    def is_reparse_point(self) -> Callable[..., bool]: ...

    @property
    def _host_is_windows_platform(self) -> Callable[[], bool]: ...

    @property
    def remove_path_tree(self) -> Callable[[Path], None]: ...

    @property
    def windows_path_component_issue(self) -> Callable[[str], str | None]: ...

    @property
    def ExecutableResolutionError(self) -> type[ExecutableResolutionError]: ...

    @property
    def require_trusted_executable(self) -> Callable[..., str]: ...

    @property
    def PolicyEngine(self) -> type[PolicyEngine]: ...

    @property
    def is_protected_path(self) -> Callable[..., bool]: ...

    @property
    def is_supervisor_runtime_path(self) -> Callable[..., bool]: ...

    @property
    def PolicyDecisionKind(self) -> type[PolicyDecisionKind]: ...
