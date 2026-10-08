"""No-follow snapshot file I/O, stable-entry checks, and cleanup primitives."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from supervisor.snapshot_services import SnapshotServices

if TYPE_CHECKING:
    pass


def remove_isolated_workspace_tree(ops: SnapshotServices, /, path: Path) -> None:
    ops._remove_path(path)


def _read_regular_file(ops: SnapshotServices, /, path: Path) -> tuple[bytes, int]:
    try:
        descriptor = ops._open_regular_file_no_follow(path)
    except OSError as exc:
        raise ops.WorkspaceSnapshotError(
            f"snapshot Git control file is not a regular file: {path}"
        ) from exc
    try:
        mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), mode
    finally:
        os.close(descriptor)


def _regular_file_matches(
    ops: SnapshotServices, /, path: Path, expected: bytes
) -> bool:
    descriptor: int | None = None
    try:
        descriptor = ops._open_regular_file_no_follow(path)
        content = bytearray()
        while len(content) <= len(expected):
            chunk = os.read(
                descriptor, min(1024 * 1024, len(expected) + 1 - len(content))
            )
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content) == expected
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_replace_bytes(
    ops: SnapshotServices, /, path: Path, content: bytes, mode: int
) -> None:
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_path(ops: SnapshotServices, /, path: Path) -> None:
    ops.remove_path_tree(path)


def _cleanup_path_best_effort(ops: SnapshotServices, /, path: Path) -> None:
    try:
        ops._remove_path(path)
        return
    except OSError:
        pass
    try:
        ops._make_tree_owner_writable(path)
        ops._remove_path(path)
    except OSError:
        # This helper is used only while preserving an already-active exception.  Public
        # cleanup methods use the strict path and report a remaining tree to the caller.
        return


def _make_regular_entry_owner_writable(
    ops: SnapshotServices, /, path: Path, metadata: os.stat_result
) -> None:
    metadata = ops._assert_stable_regular_entry(
        path,
        metadata,
        require_directory=stat.S_ISDIR(metadata.st_mode),
    )
    if (
        ops._is_windows_platform()
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink > 1
    ):
        raise OSError(
            f"refusing to change permissions through a Windows hardlink: {path}"
        )
    permissions = stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR
    if stat.S_ISDIR(metadata.st_mode):
        permissions |= stat.S_IXUSR
    os.chmod(path, permissions)


def _assert_stable_regular_entry(
    ops: SnapshotServices,
    /,
    path: Path,
    expected: os.stat_result,
    *,
    require_directory: bool,
) -> os.stat_result:
    current = path.lstat()
    expected_type = stat.S_IFMT(expected.st_mode)
    if (
        ops.is_link_or_reparse(path, stat_result=current)
        or stat.S_IFMT(current.st_mode) != expected_type
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or require_directory != stat.S_ISDIR(current.st_mode)
    ):
        raise OSError(
            f"filesystem entry changed or was redirected during operation: {path}"
        )
    return current


def _make_tree_owner_writable(ops: SnapshotServices, /, path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if ops.is_link_or_reparse(path, stat_result=metadata):
        return
    if stat.S_ISDIR(metadata.st_mode):
        ops._make_regular_entry_owner_writable(path, metadata)
        metadata = ops._assert_stable_regular_entry(
            path, metadata, require_directory=True
        )
        children = list(path.iterdir())
        metadata = ops._assert_stable_regular_entry(
            path, metadata, require_directory=True
        )
        for child in children:
            ops._assert_stable_regular_entry(path, metadata, require_directory=True)
            ops._make_tree_owner_writable(child)
        return
    ops._make_regular_entry_owner_writable(path, metadata)


def _sha256_file(ops: SnapshotServices, /, path: Path) -> str:
    descriptor = ops._open_regular_file_no_follow(path)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _open_regular_file_no_follow(ops: SnapshotServices, /, path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
