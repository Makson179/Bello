"""Controller-owned runtime exposures and their integrity lifecycle.

After construction this is the sole writer of a snapshot's exposure manifests,
integrity issues, plan visibility, and native control registries. The records live
on the snapshot, never on a shared service or module singleton."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        _WindowsRuntimeFileGuard,
        _WindowsDirectoryChangeWatcher,
    )


def _close_windows_runtime_controls(
    ops: SnapshotServices,
    /,
    file_guards: dict[str, _WindowsRuntimeFileGuard],
    dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher],
) -> None:
    """Best-effort unwind used while snapshot construction already has an error."""

    for watcher in list(dependency_watchers.values()):
        try:
            watcher.close()
        except OSError:
            pass
    dependency_watchers.clear()
    for guard in list(file_guards.values()):
        try:
            guard.close()
        except OSError:
            pass
    file_guards.clear()


def _restore_runtime_links(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> tuple[str, ...]:
    repaired: list[str] = []
    mounts: list[tuple[Path, Path, str]] = [
        (
            snapshot.snapshot_root / snapshot.task_relative_path,
            snapshot.original_root / snapshot.task_relative_path,
            "task",
        ),
    ]
    state_source = snapshot.original_root / ".supervisor"
    if state_source.is_dir():
        mounts.append(
            (snapshot.snapshot_root / ".supervisor", state_source, "supervisor_state")
        )
    for relative in snapshot.readonly_dependency_paths:
        source = snapshot.original_root / relative
        if source.exists() or source.is_symlink():
            mounts.append(
                (snapshot.snapshot_root / relative, source, f"dependency:{relative}")
            )
    for destination, source, label in mounts:
        if snapshot.runtime_exposure_mode == ops.RUNTIME_EXPOSURE_SYMLINK:
            if ops._symlink_points_to(destination, source):
                continue
            ops._create_runtime_exposure(
                destination,
                source,
                mode=ops.RUNTIME_EXPOSURE_SYMLINK,
                safe_destination_root=snapshot.snapshot_root,
            )
            repaired.append(label)
            continue

        watcher = snapshot.windows_dependency_watchers.get(label)
        if watcher is not None and watcher.consume_changes():
            issue = (
                "the coder modified the read-only Windows dependency exposure "
                f"during an action: {label.removeprefix('dependency:')}"
            )
            if issue not in snapshot.runtime_integrity_issues:
                snapshot.runtime_integrity_issues.append(issue)
            # The watcher intentionally keeps the dependency root open without
            # FILE_SHARE_DELETE.  Do not fight that kernel guard by attempting
            # an in-place repair: the controller will escalate immediately and
            # recovery detaches this exposure after closing the watcher.
            continue
        expected_manifest = snapshot.runtime_copy_manifests.get(label, ())
        try:
            destination_manifest = ops._runtime_exposure_manifest(destination)
        except (OSError, ops.WorkspaceSnapshotError):
            destination_manifest = ()
        was_replaced = destination_manifest != expected_manifest
        if label.startswith("dependency:"):
            if was_replaced:
                ops._create_windows_dependency_exposure(
                    destination,
                    source,
                    project_root=snapshot.original_root,
                    safe_destination_root=snapshot.snapshot_root,
                )
                destination_manifest = ops._runtime_exposure_manifest(destination)
        else:
            source_manifest = ops._runtime_exposure_manifest(source)
            if destination_manifest != source_manifest:
                ops._create_runtime_exposure(
                    destination,
                    source,
                    mode=ops.RUNTIME_EXPOSURE_COPY,
                    safe_destination_root=snapshot.snapshot_root,
                )
                destination_manifest = ops._runtime_exposure_manifest(destination)
        snapshot.runtime_copy_manifests[label] = destination_manifest
        if was_replaced:
            repaired.append(label)

    if snapshot.plan_exposed and snapshot.plan_path is not None:
        expected = snapshot.runtime_copy_manifests.get("plan", ())
        try:
            current = ops._runtime_exposure_manifest(snapshot.plan_path)
        except (OSError, ops.WorkspaceSnapshotError):
            current = ()
        if current != expected:
            issue = "the coder modified the isolated plan copy during an action"
            if issue not in snapshot.runtime_integrity_issues:
                snapshot.runtime_integrity_issues.append(issue)
            if snapshot.plan_bytes is None:
                raise ops.WorkspaceSnapshotError(
                    "private plan input bytes are missing during restoration"
                )
            ops._ensure_safe_runtime_destination_parent(
                snapshot.plan_path,
                snapshot.snapshot_root,
            )
            ops._atomic_replace_bytes(
                snapshot.plan_path,
                snapshot.plan_bytes,
                0o644 if ops._is_windows_platform() else 0o444,
            )
            snapshot.runtime_copy_manifests["plan"] = ops._runtime_exposure_manifest(
                snapshot.plan_path
            )
            repaired.append("plan")
    return tuple(repaired)


def _runtime_task_integrity_issue(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> str | None:
    task = snapshot.snapshot_root / snapshot.task_relative_path
    if snapshot.runtime_exposure_mode == ops.RUNTIME_EXPOSURE_SYMLINK:
        if not task.is_symlink():
            return "the coder workspace replaced or removed the read-only task link"
        try:
            if task.resolve(strict=True) != (
                snapshot.original_root / snapshot.task_relative_path
            ).resolve(strict=True):
                return "the coder workspace redirected the read-only task link"
        except OSError:
            return "the coder workspace task link is broken"
        return None

    guard = snapshot.windows_runtime_file_guards.get("task")
    if guard is not None:
        guard_issue = guard.integrity_issue()
        if guard_issue is not None:
            return guard_issue
    try:
        current = ops._runtime_exposure_manifest(task)
    except (OSError, ops.WorkspaceSnapshotError):
        current = ()
    if current != snapshot.runtime_copy_manifests.get("task", ()):
        return "the coder workspace replaced or modified the isolated task copy"
    return None


def _runtime_plan_integrity_issue(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> str | None:
    if not snapshot.plan_exposed:
        return None
    plan = snapshot.plan_path
    source = snapshot.plan_source_path
    expected_hash = snapshot.plan_sha256
    if plan is None or source is None or expected_hash is None:
        return "the private plan exposure metadata is incomplete"
    try:
        source_metadata = source.lstat()
        if ops.is_link_or_reparse(
            source, stat_result=source_metadata
        ) or not stat.S_ISREG(source_metadata.st_mode):
            return "the original private plan is no longer a regular file"
        if ops._sha256_file(source) != expected_hash:
            return "the original private plan changed after the run started"
    except OSError:
        return "the original private plan is missing or unreadable"
    guard = snapshot.windows_runtime_file_guards.get("plan")
    if guard is not None:
        guard_issue = guard.integrity_issue()
        if guard_issue is not None:
            return guard_issue
    try:
        current = ops._runtime_exposure_manifest(plan)
    except (OSError, ops.WorkspaceSnapshotError):
        current = ()
    if current != snapshot.runtime_copy_manifests.get("plan", ()):
        return "the coder workspace replaced or modified the isolated plan copy"
    return None


def _detach_recovery_workspace(
    ops: SnapshotServices, /, snapshot: WorkspaceSnapshot
) -> None:
    ops._remove_path(snapshot.snapshot_root / ".git")
    ops._remove_path(snapshot.snapshot_root / ".supervisor")
    if snapshot.plan_path is not None:
        ops._remove_path(snapshot.plan_path)
        object.__setattr__(snapshot, "plan_exposed", False)
        snapshot.runtime_copy_manifests.pop("plan", None)
    for relative in snapshot.readonly_dependency_paths:
        ops._remove_path(snapshot.snapshot_root / relative)
    task = snapshot.snapshot_root / snapshot.task_relative_path
    ops._remove_path(task)
    if ops._is_windows_platform():
        ops._ensure_safe_runtime_destination_parent(task, snapshot.snapshot_root)
    else:
        task.parent.mkdir(parents=True, exist_ok=True)
    ops._atomic_replace_bytes(task, snapshot.task_bytes, 0o644)


def _create_windows_dependency_exposure(
    ops: SnapshotServices,
    /,
    destination: Path,
    source: Path,
    *,
    project_root: Path,
    safe_destination_root: Path,
) -> None:
    """Materialize a dependency tree without retaining Windows reparse links."""

    ops._ensure_safe_runtime_destination_parent(destination, safe_destination_root)
    source_root = source.resolve(strict=False)
    project = project_root.resolve(strict=True)
    try:
        source_root.relative_to(project)
    except ValueError as exc:
        raise ops.WorkspaceSnapshotError(
            f"read-only dependency root escapes the project: {source}"
        ) from exc
    root_metadata = source.lstat()
    if ops.is_link_or_reparse(source, stat_result=root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise ops.WorkspaceSnapshotError(
            f"read-only dependency root must be a regular directory: {source}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        before = source.lstat()
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.bello-copy-", dir=destination.parent
            )
        )
        try:
            ops._materialize_windows_dependency_directory(
                source,
                temporary,
                project_root=project,
                active_directory_ids=set(),
            )
            after = source.lstat()
            if (
                not ops.is_link_or_reparse(source, stat_result=after)
                and stat.S_ISDIR(after.st_mode)
                and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            ):
                ops._runtime_exposure_manifest(temporary)
                ops._remove_path(destination)
                os.replace(temporary, destination)
                return
        finally:
            ops._remove_path(temporary)
        if attempt == 1:
            break
    raise ops.WorkspaceSnapshotError(
        f"read-only dependency changed while it was being materialized: {source}"
    )


def _materialize_windows_dependency_directory(
    ops: SnapshotServices,
    /,
    source: Path,
    destination: Path,
    *,
    project_root: Path,
    active_directory_ids: set[tuple[int, int]],
) -> None:
    metadata = source.lstat()
    if ops.is_link_or_reparse(source, stat_result=metadata):
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project_root)
        except (OSError, ValueError) as exc:
            raise ops.WorkspaceSnapshotError(
                f"dependency link/reparse target escapes the project: {source}"
            ) from exc
        resolved_metadata = resolved.lstat()
        if stat.S_ISDIR(resolved_metadata.st_mode):
            ops._materialize_windows_dependency_directory(
                resolved,
                destination,
                project_root=project_root,
                active_directory_ids=active_directory_ids,
            )
            return
        if not stat.S_ISREG(resolved_metadata.st_mode):
            raise ops.WorkspaceSnapshotError(
                f"dependency link/reparse target is unsupported: {source}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination, follow_symlinks=False)
        stable = resolved.lstat()
        if (stable.st_dev, stable.st_ino) != (
            resolved_metadata.st_dev,
            resolved_metadata.st_ino,
        ):
            raise ops.WorkspaceSnapshotError(
                f"dependency link target changed while copying: {source}"
            )
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise ops.WorkspaceSnapshotError(
            f"dependency directory entry is unsupported: {source}"
        )

    identity = (metadata.st_dev, metadata.st_ino)
    if identity in active_directory_ids:
        raise ops.WorkspaceSnapshotError(
            f"dependency link/reparse cycle detected: {source}"
        )
    active_directory_ids.add(identity)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        children = sorted(source.iterdir(), key=lambda child: child.name.casefold())
        ops._validate_windows_directory_names(
            source, [child.name for child in children]
        )
        stable = source.lstat()
        if (stable.st_dev, stable.st_ino) != identity:
            raise ops.WorkspaceSnapshotError(
                f"dependency directory changed while enumerating: {source}"
            )
        for child in children:
            child_destination = destination / child.name
            child_metadata = child.lstat()
            if ops.is_link_or_reparse(child, stat_result=child_metadata):
                ops._materialize_windows_dependency_directory(
                    child,
                    child_destination,
                    project_root=project_root,
                    active_directory_ids=active_directory_ids,
                )
            elif stat.S_ISDIR(child_metadata.st_mode):
                ops._materialize_windows_dependency_directory(
                    child,
                    child_destination,
                    project_root=project_root,
                    active_directory_ids=active_directory_ids,
                )
            elif stat.S_ISREG(child_metadata.st_mode):
                shutil.copy2(child, child_destination, follow_symlinks=False)
                child_stable = child.lstat()
                if (child_stable.st_dev, child_stable.st_ino) != (
                    child_metadata.st_dev,
                    child_metadata.st_ino,
                ):
                    raise ops.WorkspaceSnapshotError(
                        f"dependency file changed while copying: {child}"
                    )
            else:
                raise ops.WorkspaceSnapshotError(
                    f"dependency tree contains an unsupported entry: {child}"
                )
            current = source.lstat()
            if (current.st_dev, current.st_ino) != identity:
                raise ops.WorkspaceSnapshotError(
                    f"dependency directory changed while copying: {source}"
                )
    finally:
        active_directory_ids.remove(identity)


def _create_runtime_exposure(
    ops: SnapshotServices,
    /,
    destination: Path,
    source: Path,
    *,
    mode: str,
    safe_destination_root: Path | None = None,
) -> None:
    if safe_destination_root is not None:
        ops._ensure_safe_runtime_destination_parent(destination, safe_destination_root)
    if mode == ops.RUNTIME_EXPOSURE_SYMLINK:
        ops._create_readonly_link(destination, source)
        return
    if mode != ops.RUNTIME_EXPOSURE_COPY:
        raise ops.WorkspaceSnapshotError(f"unknown runtime exposure mode: {mode}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        before = ops._runtime_exposure_manifest(source)
        source_state = before[0][1] if before else ops.SnapshotPathState(kind="absent")
        temporary: Path
        if source_state.kind == "directory":
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.bello-copy-", dir=destination.parent
                )
            )
            try:
                shutil.copytree(
                    source,
                    temporary,
                    dirs_exist_ok=True,
                    symlinks=False,
                    copy_function=shutil.copy2,
                )
            except BaseException:
                ops._remove_path(temporary)
                raise
        elif source_state.kind == "file":
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.bello-copy-",
                dir=destination.parent,
            )
            os.close(descriptor)
            temporary = Path(raw_temporary)
            try:
                shutil.copy2(source, temporary, follow_symlinks=False)
            except BaseException:
                ops._remove_path(temporary)
                raise
        else:
            raise ops.WorkspaceSnapshotError(
                f"runtime exposure source is not a regular file or directory: {source}"
            )

        try:
            copied = ops._runtime_exposure_manifest(temporary)
            after = ops._runtime_exposure_manifest(source)
            if before == after and copied == before:
                ops._remove_path(destination)
                os.replace(temporary, destination)
                return
        finally:
            ops._remove_path(temporary)
        if attempt == 1:
            break
    raise ops.WorkspaceSnapshotError(
        f"runtime exposure source changed while it was being copied: {source}"
    )


def _create_readonly_link(
    ops: SnapshotServices, /, destination: Path, source: Path
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        ops._remove_path(destination)
    os.symlink(str(source.resolve()), destination, target_is_directory=source.is_dir())


def _symlink_points_to(ops: SnapshotServices, /, path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return False


def _restore_snapshot_runtime_links(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> tuple[str, ...]:
    try:
        return ops._restore_runtime_links(self)
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to restore coder workspace runtime links: {exc}"
        ) from exc


def _snapshot_task_integrity_issue(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> str | None:
    return ops._runtime_task_integrity_issue(self)


def _snapshot_plan_integrity_issue(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> str | None:
    return ops._runtime_plan_integrity_issue(self)


def _detach_plan_exposure(ops: SnapshotServices, /, self: WorkspaceSnapshot) -> bool:
    """Remove the initial-coder-only plan mount without deleting its source."""

    if not self.plan_exposed or self.plan_path is None:
        return False
    guard = self.windows_runtime_file_guards.pop("plan", None)
    if guard is not None:
        try:
            guard.close()
        except OSError as exc:
            raise ops.WorkspaceSnapshotError(
                f"failed to close Windows plan integrity control: {exc}"
            ) from exc
    try:
        ops._scrub_private_plan_from_snapshot_git(self)
        ops._remove_private_plan_git_exclude(self.snapshot_root)
        ops._remove_path(self.plan_path)
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to detach private plan exposure: {exc}"
        ) from exc
    self.runtime_copy_manifests.pop("plan", None)
    object.__setattr__(self, "plan_exposed", False)
    return True


def _snapshot_runtime_integrity_issue(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> str | None:
    return self.runtime_integrity_issues[0] if self.runtime_integrity_issues else None


def _release_windows_runtime_controls(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> None:
    failures: list[str] = []
    for label, watcher in list(self.windows_dependency_watchers.items()):
        try:
            watcher.close()
        except OSError as exc:
            failures.append(f"{label}: {exc}")
    self.windows_dependency_watchers.clear()
    for label, guard in list(self.windows_runtime_file_guards.items()):
        try:
            guard.close()
        except OSError as exc:
            failures.append(f"{label}: {exc}")
    self.windows_runtime_file_guards.clear()
    if failures:
        raise ops.WorkspaceSnapshotError(
            "failed to close Windows runtime integrity controls: " + "; ".join(failures)
        )
