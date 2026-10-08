"""Disposable snapshot cleanup and recovery preservation.

Coordinates resource release before removing or detaching a snapshot tree."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    from supervisor.workspace_snapshot import (
        WorkspaceSnapshot,
        VerificationWorkspaceSnapshot,
    )


def _cleanup_verification_snapshot(
    ops: SnapshotServices, /, self: VerificationWorkspaceSnapshot
) -> None:
    if self.temp_root.exists() or self.temp_root.is_symlink():
        try:
            ops._remove_path(self.temp_root)
        except OSError:
            ops._make_tree_owner_writable(self.temp_root)
            ops._remove_path(self.temp_root)
    if self.temp_root.exists() or self.temp_root.is_symlink():
        raise ops.WorkspaceSnapshotError(
            f"failed to remove verification snapshot: {self.temp_root}"
        )


def _cleanup_workspace_snapshot(
    ops: SnapshotServices, /, self: WorkspaceSnapshot
) -> None:
    self.close_windows_runtime_controls()
    if self.temp_root.exists() or self.temp_root.is_symlink():
        try:
            ops._remove_path(self.temp_root)
        except OSError:
            ops._make_tree_owner_writable(self.temp_root)
            ops._remove_path(self.temp_root)
    if self.temp_root.exists() or self.temp_root.is_symlink():
        raise ops.WorkspaceSnapshotError(
            f"failed to remove coder snapshot: {self.temp_root}"
        )


def _preserve_workspace_snapshot(
    ops: SnapshotServices, /, self: WorkspaceSnapshot, destination: Path
) -> Path:
    try:
        self.close_windows_runtime_controls()
        ops._detach_recovery_workspace(self)
        destination = destination.resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise ops.WorkspaceSnapshotError(
                f"snapshot recovery destination already exists: {destination}"
            )
        relative_workspace = self.snapshot_root.relative_to(self.temp_root)
        if os.name == "nt":
            try:
                # A same-volume directory rename is atomic and never walks
                # coder-controlled descendants.  shutil.move falls back to
                # copytree across volumes, which could traverse a reparse
                # point added just before preservation.
                os.replace(self.temp_root, destination)
            except OSError as exc:
                if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
                    raise
                # Keep the detached recovery workspace on its existing
                # volume instead of performing an unsafe recursive copy.
                return self.snapshot_root
        else:
            shutil.move(str(self.temp_root), str(destination))
        return destination / relative_workspace
    except ops.WorkspaceSnapshotError:
        raise
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"failed to preserve coder workspace for recovery: {exc}"
        ) from exc
