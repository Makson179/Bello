"""Snapshot construction. Owns unpublished temporary trees until handoff.

Captured baselines and runtime controls transfer to the returned snapshot only on
success; construction failures unwind controls before removing the temporary tree."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        VerificationWorkspaceSnapshot,
        SnapshotPathState,
        _WindowsRuntimeFileGuard,
        _WindowsDirectoryChangeWatcher,
    )


def create_workspace_snapshot(
    ops: SnapshotServices,
    /,
    project_root: Path,
    task_path: Path,
    *,
    plan_path: Path | None = None,
    declared_grading_roots: Iterable[str | Path] = (),
    prefix: str = "bello-coder-",
) -> WorkspaceSnapshot:
    ops._git_executable(project_root)
    try:
        original_root = project_root.resolve()
        original_task = task_path.resolve()
        if not original_root.is_dir():
            raise ops.WorkspaceSnapshotError(
                f"workspace snapshot source is not a directory: {original_root}"
            )
        original_root_metadata = original_root.lstat()
        task_bytes = original_task.read_bytes()
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to read project or task path for workspace snapshot: {exc}"
        ) from exc
    try:
        task_relative = original_task.relative_to(original_root)
    except ValueError as exc:
        raise ops.WorkspaceSnapshotError(
            f"task path is outside project root: {original_task}"
        ) from exc
    reserved_names = {
        ops._name_key(name) for name in ops.SNAPSHOT_RESERVED_TASK_PATH_NAMES
    }
    reserved_task_part = next(
        (part for part in task_relative.parts if ops._name_key(part) in reserved_names),
        None,
    )
    if reserved_task_part is not None:
        raise ops.WorkspaceSnapshotError(
            f"task path cannot be inside Bello runtime, cache, or dependency directory: {reserved_task_part}"
        )

    original_plan: Path | None = None
    plan_relative: Path | None = None
    plan_bytes: bytes | None = None
    plan_sha256: str | None = None
    if plan_path is not None:
        original_plan, plan_relative = ops._resolve_plan_input(original_root, plan_path)
        if original_plan == original_task:
            raise ops.WorkspaceSnapshotError(
                "plan path must be different from the task path"
            )
        ops.validate_plan_git_isolation(original_root, plan_path)
        try:
            plan_bytes, _plan_mode = ops._read_regular_file(original_plan)
        except (OSError, ops.WorkspaceSnapshotError) as exc:
            raise ops.WorkspaceSnapshotError(
                f"failed to read private plan input: {exc}"
            ) from exc
        plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()

    declared_roots = tuple(declared_grading_roots)
    resolved_declared_roots = ops._resolve_declared_roots(original_root, declared_roots)
    exposure_mode = ops._runtime_exposure_mode()
    if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
        if ops.is_link_or_reparse(original_root, stat_result=original_root_metadata):
            raise ops.WorkspaceSnapshotError(
                f"native Windows workspace root is a reparse point after resolution: {original_root}"
            )
        if original_root_metadata.st_ino == 0:
            raise ops.WorkspaceSnapshotError(
                "native Windows workspace filesystem does not expose a stable root file ID; "
                "snapshot patch safety cannot be established"
            )
        ops._validate_windows_snapshot_source(
            original_root,
            original_task=original_task,
            declared_roots=resolved_declared_roots,
        )
    try:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to create temporary workspace snapshot directory: {exc}"
        ) from exc
    snapshot_root = temp_root / "workspace"
    readonly_dependencies: list[tuple[Path, str]] = []
    runtime_copy_manifests: dict[str, tuple[tuple[str, SnapshotPathState], ...]] = {}
    windows_runtime_file_guards: dict[str, _WindowsRuntimeFileGuard] = {}
    windows_dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher] = {}
    try:
        history_preserved = ops._clone_git_metadata(original_root, snapshot_root)
        if history_preserved:
            ops._sync_snapshot_remotes(original_root, snapshot_root)
            ops._clear_snapshot_worktree(snapshot_root)
        shutil.copytree(
            original_root,
            snapshot_root,
            dirs_exist_ok=history_preserved,
            symlinks=True,
            ignore=ops._snapshot_ignore(
                original_root,
                resolved_declared_roots,
                original_task=original_task,
                original_plan=original_plan,
                readonly_dependencies=readonly_dependencies,
            ),
        )
        if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
            ops._validate_windows_snapshot_source(
                original_root,
                original_task=original_task,
                declared_roots=resolved_declared_roots,
            )
        rewritten_symlinks, excluded_external_symlinks = (
            ops._sanitize_copied_workspace_symlinks(
                original_root,
                snapshot_root,
            )
        )
        snapshot_task = snapshot_root / task_relative
        ops._create_runtime_exposure(
            snapshot_task,
            original_task,
            mode=exposure_mode,
            safe_destination_root=snapshot_root,
        )
        if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
            runtime_copy_manifests["task"] = ops._runtime_exposure_manifest(
                snapshot_task
            )
            if ops._native_windows_runtime_controls_enabled():
                windows_runtime_file_guards["task"] = ops._WindowsRuntimeFileGuard.open(
                    snapshot_task
                )
        state_source = original_root / ".supervisor"
        readonly_dependency_paths: list[str] = []
        readonly_dependency_roots: list[Path] = []
        for source, relative in readonly_dependencies:
            if exposure_mode == ops.RUNTIME_EXPOSURE_SYMLINK:
                dependency_root = source.resolve(strict=True)
                if (
                    original_root.is_relative_to(dependency_root)
                    or ops.is_protected_path(original_root, dependency_root)
                    or ops.is_supervisor_runtime_path(original_root, dependency_root)
                    or (
                        original_plan is not None
                        and original_plan.is_relative_to(dependency_root)
                    )
                ):
                    raise ops.WorkspaceSnapshotError(
                        f"dependency exposure would grant private workspace inputs: {relative}"
                    )
                readonly_dependency_roots.append(dependency_root)
                ops._create_runtime_exposure(
                    snapshot_root / relative,
                    source,
                    mode=exposure_mode,
                    safe_destination_root=snapshot_root,
                )
            readonly_dependency_paths.append(relative)
        baseline_commit = ops._init_snapshot_git(snapshot_root)
        info_exclude = snapshot_root / ".git" / "info" / "exclude"
        info_exclude.parent.mkdir(parents=True, exist_ok=True)
        with info_exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n/.supervisor\n")
            if plan_relative is not None:
                handle.write(f"{ops.PRIVATE_PLAN_EXCLUDE_BEGIN}\n")
                handle.write(
                    f"/{ops._gitignore_literal_path(plan_relative.as_posix())}\n"
                )
                handle.write(f"{ops.PRIVATE_PLAN_EXCLUDE_END}\n")
            if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
                for name in sorted(ops.SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES):
                    handle.write(f"{name}/\n")
        snapshot_plan: Path | None = None
        if original_plan is not None and plan_relative is not None:
            if plan_bytes is None:
                raise ops.WorkspaceSnapshotError(
                    "private plan input bytes are missing during isolated exposure"
                )
            snapshot_plan = snapshot_root / plan_relative
            ops._ensure_safe_runtime_destination_parent(snapshot_plan, snapshot_root)
            ops._atomic_replace_bytes(
                snapshot_plan,
                plan_bytes,
                0o644 if ops._is_windows_platform() else 0o444,
            )
            if ops._sha256_file(original_plan) != plan_sha256:
                raise ops.WorkspaceSnapshotError(
                    "private plan input changed while the coder workspace was being prepared"
                )
            runtime_copy_manifests["plan"] = ops._runtime_exposure_manifest(
                snapshot_plan
            )
            if ops._native_windows_runtime_controls_enabled():
                windows_runtime_file_guards["plan"] = ops._WindowsRuntimeFileGuard.open(
                    snapshot_plan
                )
        if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
            for source, relative in readonly_dependencies:
                destination = snapshot_root / relative
                ops._create_windows_dependency_exposure(
                    destination,
                    source,
                    project_root=original_root,
                    safe_destination_root=snapshot_root,
                )
                runtime_copy_manifests[f"dependency:{relative}"] = (
                    ops._runtime_exposure_manifest(destination)
                )
                if ops._native_windows_runtime_controls_enabled():
                    windows_dependency_watchers[f"dependency:{relative}"] = (
                        ops._WindowsDirectoryChangeWatcher.open(destination)
                    )
        if state_source.is_dir():
            # Runtime state must be mounted for the controller but must never enter the
            # coder snapshot's Git history/index: Completion is intentionally blind to the
            # checklist and could otherwise recover the absolute mount target via git show.
            state_destination = snapshot_root / ".supervisor"
            ops._create_runtime_exposure(
                state_destination,
                state_source,
                mode=exposure_mode,
                safe_destination_root=snapshot_root,
            )
            if exposure_mode == ops.RUNTIME_EXPOSURE_COPY:
                runtime_copy_manifests["supervisor_state"] = (
                    ops._runtime_exposure_manifest(state_destination)
                )
        git_config_bytes, git_config_mode = ops._read_regular_file(
            snapshot_root / ".git" / "config"
        )
        worktree_config = snapshot_root / ".git" / "config.worktree"
        if worktree_config.exists() or worktree_config.is_symlink():
            git_worktree_config_bytes, git_worktree_config_mode = (
                ops._read_regular_file(worktree_config)
            )
        else:
            git_worktree_config_bytes, git_worktree_config_mode = None, None
        return ops.WorkspaceSnapshot(
            original_root=original_root,
            original_root_identity=(
                original_root_metadata.st_dev,
                original_root_metadata.st_ino,
            ),
            snapshot_root=snapshot_root.resolve(),
            temp_root=temp_root,
            task_path=snapshot_task.absolute(),
            task_relative_path=task_relative.as_posix(),
            task_bytes=task_bytes,
            task_sha256=hashlib.sha256(task_bytes).hexdigest(),
            baseline_commit=baseline_commit,
            git_config_bytes=git_config_bytes,
            git_config_mode=git_config_mode,
            git_worktree_config_bytes=git_worktree_config_bytes,
            git_worktree_config_mode=git_worktree_config_mode,
            plan_source_path=original_plan,
            plan_path=snapshot_plan,
            plan_relative_path=(
                plan_relative.as_posix() if plan_relative is not None else None
            ),
            plan_bytes=plan_bytes,
            plan_sha256=plan_sha256,
            plan_exposed=snapshot_plan is not None,
            readonly_dependency_paths=tuple(
                sorted(dict.fromkeys(readonly_dependency_paths))
            ),
            readonly_dependency_roots=tuple(dict.fromkeys(readonly_dependency_roots)),
            declared_grading_roots=declared_roots,
            rewritten_symlinks=rewritten_symlinks,
            excluded_external_symlink_paths=excluded_external_symlinks,
            runtime_exposure_mode=exposure_mode,
            runtime_copy_manifests=runtime_copy_manifests,
            windows_runtime_file_guards=windows_runtime_file_guards,
            windows_dependency_watchers=windows_dependency_watchers,
        )
    except ops.WorkspaceSnapshotError:
        ops._close_windows_runtime_controls(
            windows_runtime_file_guards,
            windows_dependency_watchers,
        )
        ops._cleanup_path_best_effort(temp_root)
        raise
    except Exception as exc:
        ops._close_windows_runtime_controls(
            windows_runtime_file_guards,
            windows_dependency_watchers,
        )
        ops._cleanup_path_best_effort(temp_root)
        raise ops.WorkspaceSnapshotError(
            f"failed to create coder workspace snapshot: {exc}"
        ) from exc


def create_verification_workspace_snapshot(
    ops: SnapshotServices,
    /,
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

    ops._git_executable(project_root)
    try:
        original_root = project_root.resolve()
        if not original_root.is_dir():
            raise ops.WorkspaceSnapshotError(
                f"verification snapshot source is not a directory: {original_root}"
            )
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to resolve verification snapshot source: {exc}"
        ) from exc

    if ops._is_windows_platform():
        ops._validate_windows_snapshot_source(
            original_root,
            original_task=None,
            declared_roots=(),
        )

    trusted_mounts = ops._verification_trusted_mounts(original_root, source_snapshot)
    private_runtime_paths = (
        (source_snapshot.plan_relative_path,)
        if source_snapshot is not None
        and source_snapshot.plan_relative_path is not None
        else ()
    )
    source_is_git = ops._is_top_level_git_repository(original_root)
    if source_is_git:
        ops._reject_verification_git_alternates(original_root)
    gitlink_paths = (
        ops._verification_gitlink_paths(original_root) if source_is_git else ()
    )
    if gitlink_paths:
        joined = ", ".join(gitlink_paths[:8])
        if len(gitlink_paths) > 8:
            joined += f", ... (+{len(gitlink_paths) - 8} more)"
        raise ops.WorkspaceSnapshotError(
            "verification snapshots do not yet support Git submodules; "
            f"gitlink paths: {joined}"
        )

    try:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to create temporary verification snapshot directory: {exc}"
        ) from exc
    snapshot_root = temp_root / "workspace"
    try:
        history_preserved = ops._clone_git_metadata(
            original_root,
            snapshot_root,
            fail_on_clone_error=True,
        )
        if history_preserved:
            ops._clear_snapshot_worktree(snapshot_root)
        shutil.copytree(
            original_root,
            snapshot_root,
            dirs_exist_ok=history_preserved,
            symlinks=True,
            ignore=ops._verification_snapshot_ignore(
                original_root,
                private_runtime_paths=private_runtime_paths,
            ),
        )
        if ops._is_windows_platform():
            ops._validate_windows_snapshot_source(
                original_root,
                original_task=None,
                declared_roots=(),
            )
        # The reviewer may write inside this copy while running existing checks. Rewrite
        # links that point back into the submitted workspace and remove links that escape it,
        # so a check cannot read or mutate host paths through a copied symlink.
        ops._sanitize_copied_workspace_symlinks(
            original_root,
            snapshot_root,
            trusted_external_symlinks=trusted_mounts,
        )
        if history_preserved:
            ops._sanitize_verification_snapshot_git(snapshot_root)
            ops._copy_verification_safe_git_config(original_root, snapshot_root)
            ops._copy_verification_git_file(
                original_root, snapshot_root, "info/exclude"
            )
            if ops._git_config_bool(snapshot_root, "core.sparsecheckout"):
                ops._copy_verification_git_file(
                    original_root,
                    snapshot_root,
                    "info/sparse-checkout",
                    required=True,
                )
            ops._copy_snapshot_git_index(original_root, snapshot_root)
            ops._hide_verification_runtime_state(snapshot_root)
            ops._hide_verification_private_inputs(snapshot_root, private_runtime_paths)
        verification = ops.VerificationWorkspaceSnapshot(
            original_root=original_root,
            snapshot_root=snapshot_root.resolve(),
            temp_root=temp_root,
        )
        object.__setattr__(
            verification,
            "submitted_manifest",
            ops._verification_worktree_manifest(verification.snapshot_root),
        )
        object.__setattr__(
            verification,
            "mutable_submitted_paths",
            ops._verification_mutable_submitted_paths(
                verification.snapshot_root,
                verification.submitted_manifest,
            ),
        )
        if history_preserved:
            object.__setattr__(
                verification,
                "git_manifest",
                ops._verification_git_manifest(verification.snapshot_root),
            )
            object.__setattr__(
                verification,
                "git_control_manifest",
                ops._verification_git_control_manifest(verification.snapshot_root),
            )
        # Allocate review inputs only after capturing the submitted state. Existing
        # files, including submitted files under .cache, retain their usual protection.
        object.__setattr__(
            verification,
            "scratch_root",
            ops._create_verification_scratch(verification.snapshot_root),
        )
        return verification
    except ops.WorkspaceSnapshotError:
        ops._cleanup_path_best_effort(temp_root)
        raise
    except shutil.Error as exc:
        ops._cleanup_path_best_effort(temp_root)
        raise ops.WorkspaceSnapshotError(
            _verification_copy_failure(exc, original_root)
        ) from exc
    except Exception as exc:
        ops._cleanup_path_best_effort(temp_root)
        raise ops.WorkspaceSnapshotError(
            f"failed to create verification workspace snapshot: {exc}"
        ) from exc


def _verification_copy_failure(error: shutil.Error, source_root: Path) -> str:
    """Bound copy diagnostics without exposing contents or changing source access."""
    entries = error.args[0] if error.args and isinstance(error.args[0], list) else []
    details = []
    for entry in entries[:5]:
        if not isinstance(entry, tuple) or len(entry) != 3:
            continue
        try:
            relative = str(Path(entry[0]).relative_to(source_root))
        except (TypeError, ValueError):
            relative = "<entry outside workspace>"
        # repr escapes control characters in names; neither full OS errors nor
        # file contents belong in a potentially model-visible failure message.
        relative = repr(relative[:160])
        if len(relative) > 180:
            relative = relative[:177] + "..."
        reason = str(entry[2]).lower()
        kind = "named pipe" if reason.endswith(" is a named pipe") else (
            "permission denied" if reason.startswith("[errno 13] permission denied") else "copy failed"
        )
        details.append(f"{relative} ({kind})")
    summary = "; ".join(details) or "uncopyable submitted entries"
    if len(entries) > 5:
        summary += f"; +{len(entries) - 5} more"
    return (
        f"failed to create verification workspace snapshot: {summary}. "
        "Review includes ignored and untracked files. Keep temporary test artifacts "
        "outside the submitted workspace. No entries were silently skipped and "
        "source permissions were not changed."
    )


def _create_verification_scratch(ops: SnapshotServices, /, snapshot_root: Path) -> Path:
    """Allocate a private persistent scratch directory before exposing the snapshot."""

    cache_root = snapshot_root / ".cache"
    # Never replace a submitted path or follow a copied link into another location.
    # No reviewer has access to this newly created snapshot yet.
    if ops.is_link_or_reparse(cache_root):
        raise ops.WorkspaceSnapshotError(
            "verification scratch .cache must not be a link"
        )
    if cache_root.exists() and not cache_root.is_dir():
        raise ops.WorkspaceSnapshotError(
            "verification scratch .cache must be a real directory"
        )
    cache_root.mkdir(mode=0o700, exist_ok=True)
    if not cache_root.is_dir() or cache_root.resolve() != cache_root:
        raise ops.WorkspaceSnapshotError(
            "verification scratch .cache must be a real directory"
        )
    # mkdtemp uses exclusive creation and retries name collisions without modifying them.
    return Path(tempfile.mkdtemp(prefix="bello-review-", dir=cache_root)).resolve()


def copy_isolated_workspace_tree(
    ops: SnapshotServices, /, source_root: Path, destination_root: Path, *, ignore=None
) -> None:
    """Copy a disposable workspace without retaining links to its source or host."""

    source = source_root.resolve(strict=True)
    if ops._is_windows_platform():
        ops._validate_windows_snapshot_source(
            source,
            original_task=None,
            declared_roots=(),
        )
    shutil.copytree(
        source,
        destination_root,
        symlinks=True,
        ignore=ignore,
    )
    if ops._is_windows_platform():
        ops._validate_windows_snapshot_source(
            source,
            original_task=None,
            declared_roots=(),
        )
    ops._sanitize_copied_workspace_symlinks(source, destination_root)


def _verification_snapshot_ignore(
    ops: SnapshotServices,
    /,
    original_root: Path,
    *,
    private_runtime_paths: tuple[str, ...] = (),
):
    root = original_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        # Runtime state and initial-coder-only inputs are not part of the submitted
        # artifact and must not become writable review input. Keep every other path,
        # including caches and untracked files, so Git status retains the candidate.
        ignored_names = {ops._name_key(".git"), ops._name_key(".supervisor")}
        ignored = {name for name in names if ops._name_key(name) in ignored_names}
        try:
            relative_dir = Path(directory).relative_to(root)
        except ValueError:
            relative_dir = Path()
        for name in names:
            relative = (relative_dir / name).as_posix()
            if any(
                ops._path_is_at_or_below(relative, private_path)
                and ops._path_is_at_or_below(private_path, relative)
                for private_path in private_runtime_paths
            ):
                ignored.add(name)
        return ignored

    return ignore


def _clear_snapshot_worktree(ops: SnapshotServices, /, snapshot_root: Path) -> None:
    for child in snapshot_root.iterdir():
        if ops._name_key(child.name) == ops._name_key(".git"):
            continue
        ops._remove_path(child)


def _snapshot_ignore(
    ops: SnapshotServices,
    /,
    original_root: Path,
    declared_roots: tuple[Path, ...],
    *,
    original_task: Path,
    original_plan: Path | None,
    readonly_dependencies: list[tuple[Path, str]],
):
    root = original_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        current = Path(directory)
        dependency_names = {
            ops._name_key(value) for value in ops.SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
        }
        always_ignore_names = {
            ops._name_key(value) for value in ops.SNAPSHOT_ALWAYS_IGNORE_NAMES
        }
        for name in names:
            candidate = current / name
            try:
                candidate_relative = candidate.relative_to(root).as_posix()
            except ValueError:
                candidate_relative = name
            try:
                resolved_candidate = candidate.resolve(strict=False)
            except OSError:
                resolved_candidate = None
            if resolved_candidate == original_task:
                ignored.add(name)
                continue
            if original_plan is not None and resolved_candidate == original_plan:
                ignored.add(name)
                continue
            relative_parts = tuple(
                ops._name_key(part) for part in Path(candidate_relative).parts
            )
            if relative_parts[: len(ops.BELLO_LAUNCHER_RUNTIME_PARTS)] == tuple(
                ops._name_key(part) for part in ops.BELLO_LAUNCHER_RUNTIME_PARTS
            ):
                # The Bello delegation skill writes its launcher command and
                # parameters here.  They are controller-side runtime records,
                # not submitted project input; in particular, an optional
                # private plan path must not become reviewer-visible metadata.
                ignored.add(name)
                continue
            if ops._name_key(name) in dependency_names:
                readonly_dependencies.append((candidate, candidate_relative))
                ignored.add(name)
                continue
            if ops._name_key(name) in always_ignore_names:
                ignored.add(name)
                continue
            if ops.is_protected_path(root, candidate) or ops.is_supervisor_runtime_path(
                root, candidate
            ):
                ignored.add(name)
                continue
            if ops._matches_declared_root(candidate, declared_roots):
                ignored.add(name)
        return ignored

    return ignore
