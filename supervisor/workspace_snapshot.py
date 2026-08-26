from __future__ import annotations

from collections.abc import Sequence
import errno
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Iterable

from supervisor.filesystem_safety import (
    is_link_or_reparse,
    is_reparse_point,
    is_windows_platform as _host_is_windows_platform,
    remove_path_tree,
    windows_path_component_issue,
)
from supervisor.executables import ExecutableResolutionError, require_trusted_executable
from supervisor.policy import PolicyEngine, is_protected_path, is_supervisor_runtime_path
from supervisor.schemas import PolicyDecisionKind


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

SNAPSHOT_RESERVED_TASK_PATH_NAMES = SNAPSHOT_ALWAYS_IGNORE_NAMES | SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES

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


def _windows_api_path(path: Path) -> str:
    """Return an absolute extended-length spelling for Win32 file APIs."""

    raw = str(path.absolute())
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _is_windows_platform() -> bool:
    # Kept as a small seam so both filesystem strategies are testable from POSIX.
    return _host_is_windows_platform()


def _runtime_exposure_mode() -> str:
    return RUNTIME_EXPOSURE_COPY if _is_windows_platform() else RUNTIME_EXPOSURE_SYMLINK


def _native_windows_runtime_controls_enabled() -> bool:
    return os.name == "nt" and _is_windows_platform()


def _name_key(name: str) -> str:
    return name.casefold() if _is_windows_platform() else name


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

    def cleanup(self) -> None:
        if self.temp_root.exists() or self.temp_root.is_symlink():
            try:
                _remove_path(self.temp_root)
            except OSError:
                _make_tree_owner_writable(self.temp_root)
                _remove_path(self.temp_root)
        if self.temp_root.exists() or self.temp_root.is_symlink():
            raise WorkspaceSnapshotError(
                f"failed to remove verification snapshot: {self.temp_root}"
            )

    def assert_submission_unchanged(self) -> None:
        before = dict(self.submitted_manifest)
        after = dict(_verification_worktree_manifest(self.snapshot_root))
        changed = [
            path
            for path in sorted(before)
            if before.get(path) != after.get(path)
            and path not in self.mutable_submitted_paths
        ]
        # Existing submitted paths are always immutable, even when they live under a
        # conventional build/cache directory. Only newly created, clearly generated paths
        # may remain as incidental outputs of a check.
        changed.extend(
            path
            for path in sorted(after.keys() - before.keys())
            if not _is_verification_mutable_artifact_path(path)
            and not _verification_path_is_git_ignored(self.snapshot_root, path)
        )
        if self.git_manifest:
            current_git = _verification_git_manifest(self.snapshot_root)
            if current_git != self.git_manifest:
                changed.append(".git verification metadata")
        if self.git_control_manifest:
            current_control = _verification_git_control_manifest(self.snapshot_root)
            if current_control != self.git_control_manifest:
                changed.append(".git verification control files")
        if not changed:
            return
        detail = ", ".join(changed[:12])
        if len(changed) > 12:
            detail += f", ... (+{len(changed) - 12} more)"
        raise WorkspaceSnapshotError(
            "completion verification modified submitted workspace paths: " + detail
        )


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


@dataclass
class _WindowsRuntimeFileGuard:
    """A non-inheritable handle that prevents replacing or writing one file."""

    path: Path
    handle: int
    identity: tuple[int, int]

    @classmethod
    def open(cls, path: Path) -> "_WindowsRuntimeFileGuard":
        if os.name != "nt":
            raise WorkspaceSnapshotError("Windows runtime file guards require native Windows")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        # FILE_READ_DATA | FILE_READ_ATTRIBUTES, FILE_SHARE_READ, OPEN_EXISTING.
        # A metadata-only access mask does not participate in the Windows I/O
        # manager's write-share accounting, so FILE_READ_DATA is required for
        # omitting FILE_SHARE_WRITE to reject in-place content writes.  Omitting
        # FILE_SHARE_DELETE also prevents replacement while the coder is active,
        # without changing the file's ACL or mode.
        handle = kernel32.CreateFileW(
            _windows_api_path(path),
            0x0081,
            0x00000001,
            None,
            3,
            0x00000080,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            metadata = path.lstat()
            if is_link_or_reparse(path, stat_result=metadata) or not stat.S_ISREG(
                metadata.st_mode
            ):
                raise WorkspaceSnapshotError(
                    f"immutable Windows runtime exposure is not a regular file: {path}"
                )
            return cls(
                path=path,
                handle=int(handle),
                identity=(metadata.st_dev, metadata.st_ino),
            )
        except BaseException:
            _close_windows_handle(int(handle))
            raise

    def integrity_issue(self) -> str | None:
        try:
            metadata = self.path.lstat()
        except OSError:
            return f"protected runtime file is missing or unreadable: {self.path}"
        if (
            is_link_or_reparse(self.path, stat_result=metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self.identity
        ):
            return f"protected runtime file was replaced or redirected: {self.path}"
        return None

    def close(self) -> None:
        if self.handle:
            handle = self.handle
            self.handle = 0
            _close_windows_handle(handle)


@dataclass
class _WindowsDirectoryChangeWatcher:
    """Kernel-backed detector for transient writes inside one dependency tree."""

    path: Path
    handle: int
    event_handle: int
    overlapped: object
    buffer: object
    closed: bool = False

    @classmethod
    def open(cls, path: Path) -> "_WindowsDirectoryChangeWatcher":
        if os.name != "nt":
            raise WorkspaceSnapshotError("Windows directory watchers require native Windows")
        import ctypes
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE

        metadata = path.lstat()
        if is_link_or_reparse(path, stat_result=metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise WorkspaceSnapshotError(
                f"read-only Windows dependency exposure is not a regular directory: {path}"
            )
        # FILE_LIST_DIRECTORY with no FILE_SHARE_DELETE both arms recursive
        # notifications and prevents swapping out the watched root itself.
        handle = kernel32.CreateFileW(
            _windows_api_path(path),
            0x0001,
            0x00000001 | 0x00000002,
            None,
            3,
            0x02000000 | 0x40000000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        event_handle = kernel32.CreateEventW(None, True, False, None)
        if not event_handle:
            error = ctypes.get_last_error()
            _close_windows_handle(int(handle))
            raise ctypes.WinError(error)
        overlapped = _Overlapped()
        overlapped.hEvent = event_handle
        watcher = cls(
            path=path,
            handle=int(handle),
            event_handle=int(event_handle),
            overlapped=overlapped,
            buffer=ctypes.create_string_buffer(64 * 1024),
        )
        try:
            watcher._arm()
        except BaseException:
            watcher.close()
            raise
        return watcher

    def _arm(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel32.ResetEvent.restype = wintypes.BOOL
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
        if not kernel32.ResetEvent(self.event_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.overlapped.Internal = None
        self.overlapped.InternalHigh = None
        self.overlapped.Offset = 0
        self.overlapped.OffsetHigh = 0
        self.overlapped.hEvent = self.event_handle
        if not kernel32.ReadDirectoryChangesW(
            self.handle,
            self.buffer,
            ctypes.sizeof(self.buffer),
            True,
            _WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER,
            None,
            ctypes.byref(self.overlapped),
            None,
        ):
            error = ctypes.get_last_error()
            if error != 997:  # ERROR_IO_PENDING is expected for overlapped I/O.
                raise ctypes.WinError(error)

    def consume_changes(self) -> bool:
        if self.closed:
            raise WorkspaceSnapshotError(f"Windows dependency watcher is closed: {self.path}")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        status = kernel32.WaitForSingleObject(self.event_handle, 0)
        if status == 258:  # WAIT_TIMEOUT
            return False
        if status != 0:  # WAIT_OBJECT_0
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = wintypes.DWORD()
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL
        if not kernel32.GetOverlappedResult(
            self.handle,
            ctypes.byref(self.overlapped),
            ctypes.byref(transferred),
            False,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # A zero-byte completion means the change buffer overflowed.  That is
        # still a definite integrity event, so it fails closed like any write.
        self._arm()
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.CancelIoEx.restype = wintypes.BOOL
        if self.handle:
            kernel32.CancelIoEx(self.handle, ctypes.byref(self.overlapped))
        if self.handle:
            _close_windows_handle(self.handle)
            self.handle = 0
        if self.event_handle:
            _close_windows_handle(self.event_handle)
            self.event_handle = 0


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
    readonly_dependency_paths: tuple[str, ...] = ()
    declared_grading_roots: tuple[str | Path, ...] = ()
    rewritten_symlinks: tuple[SnapshotSymlinkRewrite, ...] = ()
    excluded_external_symlink_paths: tuple[str, ...] = ()
    runtime_exposure_mode: str = RUNTIME_EXPOSURE_SYMLINK
    runtime_copy_manifests: dict[
        str, tuple[tuple[str, SnapshotPathState], ...]
    ] = field(default_factory=dict, repr=False, compare=False)
    windows_runtime_file_guards: dict[
        str, _WindowsRuntimeFileGuard
    ] = field(default_factory=dict, repr=False, compare=False)
    windows_dependency_watchers: dict[
        str, _WindowsDirectoryChangeWatcher
    ] = field(default_factory=dict, repr=False, compare=False)
    runtime_integrity_issues: list[str] = field(default_factory=list, repr=False, compare=False)

    def cleanup(self) -> None:
        self.close_windows_runtime_controls()
        if self.temp_root.exists() or self.temp_root.is_symlink():
            try:
                _remove_path(self.temp_root)
            except OSError:
                _make_tree_owner_writable(self.temp_root)
                _remove_path(self.temp_root)
        if self.temp_root.exists() or self.temp_root.is_symlink():
            raise WorkspaceSnapshotError(f"failed to remove coder snapshot: {self.temp_root}")

    def restore_runtime_links(self) -> tuple[str, ...]:
        try:
            return _restore_runtime_links(self)
        except OSError as exc:
            raise WorkspaceSnapshotError(f"failed to restore coder workspace runtime links: {exc}") from exc

    def task_integrity_issue(self) -> str | None:
        return _runtime_task_integrity_issue(self)

    def runtime_integrity_issue(self) -> str | None:
        return self.runtime_integrity_issues[0] if self.runtime_integrity_issues else None

    def close_windows_runtime_controls(self) -> None:
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
            raise WorkspaceSnapshotError(
                "failed to close Windows runtime integrity controls: " + "; ".join(failures)
            )

    def git_control_is_trusted(self) -> bool:
        git_dir = self.snapshot_root / ".git"
        if is_link_or_reparse(git_dir) or not git_dir.is_dir():
            return False
        if not _regular_file_matches(git_dir / "config", self.git_config_bytes):
            return False
        worktree_config = git_dir / "config.worktree"
        if self.git_worktree_config_bytes is None:
            return not (worktree_config.exists() or worktree_config.is_symlink())
        return _regular_file_matches(worktree_config, self.git_worktree_config_bytes)

    def restore_git_control(self) -> bool:
        if self.git_control_is_trusted():
            return False
        try:
            _restore_trusted_snapshot_git_config(self)
        except OSError as exc:
            raise WorkspaceSnapshotError(f"failed to restore trusted snapshot Git config: {exc}") from exc
        return True

    def preserve(self, destination: Path) -> Path:
        try:
            self.close_windows_runtime_controls()
            _detach_recovery_workspace(self)
            destination = destination.resolve(strict=False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise WorkspaceSnapshotError(f"snapshot recovery destination already exists: {destination}")
            relative_workspace = self.snapshot_root.relative_to(self.temp_root)
            if os.name == "nt":
                try:
                    # A same-volume directory rename is atomic and never walks
                    # coder-controlled descendants.  shutil.move falls back to
                    # copytree across volumes, which could traverse a reparse
                    # point added just before preservation.
                    os.replace(self.temp_root, destination)
                except OSError as exc:
                    if exc.errno != errno.EXDEV and getattr(
                        exc, "winerror", None
                    ) != 17:
                        raise
                    # Keep the detached recovery workspace on its existing
                    # volume instead of performing an unsafe recursive copy.
                    return self.snapshot_root
            else:
                shutil.move(str(self.temp_root), str(destination))
            return destination / relative_workspace
        except WorkspaceSnapshotError:
            raise
        except OSError as exc:
            raise WorkspaceSnapshotError(f"failed to preserve coder workspace for recovery: {exc}") from exc


def _close_windows_runtime_controls(
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


def create_workspace_snapshot(
    project_root: Path,
    task_path: Path,
    *,
    declared_grading_roots: Iterable[str | Path] = (),
    prefix: str = "bello-coder-",
) -> WorkspaceSnapshot:
    _git_executable(project_root)
    try:
        original_root = project_root.resolve()
        original_task = task_path.resolve()
        if not original_root.is_dir():
            raise WorkspaceSnapshotError(f"workspace snapshot source is not a directory: {original_root}")
        original_root_metadata = original_root.lstat()
        task_bytes = original_task.read_bytes()
    except OSError as exc:
        raise WorkspaceSnapshotError(f"failed to read project or task path for workspace snapshot: {exc}") from exc
    try:
        task_relative = original_task.relative_to(original_root)
    except ValueError as exc:
        raise WorkspaceSnapshotError(f"task path is outside project root: {original_task}") from exc
    reserved_names = {_name_key(name) for name in SNAPSHOT_RESERVED_TASK_PATH_NAMES}
    reserved_task_part = next(
        (part for part in task_relative.parts if _name_key(part) in reserved_names), None
    )
    if reserved_task_part is not None:
        raise WorkspaceSnapshotError(
            f"task path cannot be inside Bello runtime, cache, or dependency directory: {reserved_task_part}"
        )

    declared_roots = tuple(declared_grading_roots)
    resolved_declared_roots = _resolve_declared_roots(original_root, declared_roots)
    exposure_mode = _runtime_exposure_mode()
    if exposure_mode == RUNTIME_EXPOSURE_COPY:
        if is_link_or_reparse(original_root, stat_result=original_root_metadata):
            raise WorkspaceSnapshotError(
                f"native Windows workspace root is a reparse point after resolution: {original_root}"
            )
        if original_root_metadata.st_ino == 0:
            raise WorkspaceSnapshotError(
                "native Windows workspace filesystem does not expose a stable root file ID; "
                "snapshot patch safety cannot be established"
            )
        _validate_windows_snapshot_source(
            original_root,
            original_task=original_task,
            declared_roots=resolved_declared_roots,
        )
    try:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    except OSError as exc:
        raise WorkspaceSnapshotError(f"failed to create temporary workspace snapshot directory: {exc}") from exc
    snapshot_root = temp_root / "workspace"
    readonly_dependencies: list[tuple[Path, str]] = []
    runtime_copy_manifests: dict[str, tuple[tuple[str, SnapshotPathState], ...]] = {}
    windows_runtime_file_guards: dict[str, _WindowsRuntimeFileGuard] = {}
    windows_dependency_watchers: dict[str, _WindowsDirectoryChangeWatcher] = {}
    try:
        history_preserved = _clone_git_metadata(original_root, snapshot_root)
        if history_preserved:
            _sync_snapshot_remotes(original_root, snapshot_root)
            _clear_snapshot_worktree(snapshot_root)
        shutil.copytree(
            original_root,
            snapshot_root,
            dirs_exist_ok=history_preserved,
            symlinks=True,
            ignore=_snapshot_ignore(
                original_root,
                resolved_declared_roots,
                original_task=original_task,
                readonly_dependencies=readonly_dependencies,
            ),
        )
        if exposure_mode == RUNTIME_EXPOSURE_COPY:
            _validate_windows_snapshot_source(
                original_root,
                original_task=original_task,
                declared_roots=resolved_declared_roots,
            )
        rewritten_symlinks, excluded_external_symlinks = _sanitize_copied_workspace_symlinks(
            original_root,
            snapshot_root,
        )
        snapshot_task = snapshot_root / task_relative
        _create_runtime_exposure(
            snapshot_task,
            original_task,
            mode=exposure_mode,
            safe_destination_root=snapshot_root,
        )
        if exposure_mode == RUNTIME_EXPOSURE_COPY:
            runtime_copy_manifests["task"] = _runtime_exposure_manifest(snapshot_task)
            if _native_windows_runtime_controls_enabled():
                windows_runtime_file_guards["task"] = _WindowsRuntimeFileGuard.open(
                    snapshot_task
                )
        state_source = original_root / ".supervisor"
        readonly_dependency_paths: list[str] = []
        for source, relative in readonly_dependencies:
            if exposure_mode == RUNTIME_EXPOSURE_SYMLINK:
                _create_runtime_exposure(
                    snapshot_root / relative,
                    source,
                    mode=exposure_mode,
                    safe_destination_root=snapshot_root,
                )
            readonly_dependency_paths.append(relative)
        baseline_commit = _init_snapshot_git(snapshot_root)
        info_exclude = snapshot_root / ".git" / "info" / "exclude"
        info_exclude.parent.mkdir(parents=True, exist_ok=True)
        with info_exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n/.supervisor\n")
            if exposure_mode == RUNTIME_EXPOSURE_COPY:
                for name in sorted(SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES):
                    handle.write(f"{name}/\n")
        if exposure_mode == RUNTIME_EXPOSURE_COPY:
            for source, relative in readonly_dependencies:
                destination = snapshot_root / relative
                _create_windows_dependency_exposure(
                    destination,
                    source,
                    project_root=original_root,
                    safe_destination_root=snapshot_root,
                )
                runtime_copy_manifests[f"dependency:{relative}"] = (
                    _runtime_exposure_manifest(destination)
                )
                if _native_windows_runtime_controls_enabled():
                    windows_dependency_watchers[f"dependency:{relative}"] = (
                        _WindowsDirectoryChangeWatcher.open(destination)
                    )
        if state_source.is_dir():
            # Runtime state must be mounted for the controller but must never enter the
            # coder snapshot's Git history/index: Completion is intentionally blind to the
            # checklist and could otherwise recover the absolute mount target via git show.
            state_destination = snapshot_root / ".supervisor"
            _create_runtime_exposure(
                state_destination,
                state_source,
                mode=exposure_mode,
                safe_destination_root=snapshot_root,
            )
            if exposure_mode == RUNTIME_EXPOSURE_COPY:
                runtime_copy_manifests["supervisor_state"] = _runtime_exposure_manifest(
                    state_destination
                )
        git_config_bytes, git_config_mode = _read_regular_file(snapshot_root / ".git" / "config")
        worktree_config = snapshot_root / ".git" / "config.worktree"
        if worktree_config.exists() or worktree_config.is_symlink():
            git_worktree_config_bytes, git_worktree_config_mode = _read_regular_file(worktree_config)
        else:
            git_worktree_config_bytes, git_worktree_config_mode = None, None
        return WorkspaceSnapshot(
            original_root=original_root,
            original_root_identity=(original_root_metadata.st_dev, original_root_metadata.st_ino),
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
            readonly_dependency_paths=tuple(sorted(dict.fromkeys(readonly_dependency_paths))),
            declared_grading_roots=declared_roots,
            rewritten_symlinks=rewritten_symlinks,
            excluded_external_symlink_paths=excluded_external_symlinks,
            runtime_exposure_mode=exposure_mode,
            runtime_copy_manifests=runtime_copy_manifests,
            windows_runtime_file_guards=windows_runtime_file_guards,
            windows_dependency_watchers=windows_dependency_watchers,
        )
    except WorkspaceSnapshotError:
        _close_windows_runtime_controls(
            windows_runtime_file_guards,
            windows_dependency_watchers,
        )
        _cleanup_path_best_effort(temp_root)
        raise
    except Exception as exc:
        _close_windows_runtime_controls(
            windows_runtime_file_guards,
            windows_dependency_watchers,
        )
        _cleanup_path_best_effort(temp_root)
        raise WorkspaceSnapshotError(f"failed to create coder workspace snapshot: {exc}") from exc


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

    _git_executable(project_root)
    try:
        original_root = project_root.resolve()
        if not original_root.is_dir():
            raise WorkspaceSnapshotError(
                f"verification snapshot source is not a directory: {original_root}"
            )
    except OSError as exc:
        raise WorkspaceSnapshotError(f"failed to resolve verification snapshot source: {exc}") from exc

    if _is_windows_platform():
        _validate_windows_snapshot_source(
            original_root,
            original_task=None,
            declared_roots=(),
        )

    trusted_mounts = _verification_trusted_mounts(original_root, source_snapshot)
    source_is_git = _is_top_level_git_repository(original_root)
    if source_is_git:
        _reject_verification_git_alternates(original_root)
    gitlink_paths = _verification_gitlink_paths(original_root) if source_is_git else ()
    if gitlink_paths:
        joined = ", ".join(gitlink_paths[:8])
        if len(gitlink_paths) > 8:
            joined += f", ... (+{len(gitlink_paths) - 8} more)"
        raise WorkspaceSnapshotError(
            "verification snapshots do not yet support Git submodules; "
            f"gitlink paths: {joined}"
        )

    try:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    except OSError as exc:
        raise WorkspaceSnapshotError(
            f"failed to create temporary verification snapshot directory: {exc}"
        ) from exc
    snapshot_root = temp_root / "workspace"
    try:
        history_preserved = _clone_git_metadata(
            original_root,
            snapshot_root,
            fail_on_clone_error=True,
        )
        if history_preserved:
            _clear_snapshot_worktree(snapshot_root)
        shutil.copytree(
            original_root,
            snapshot_root,
            dirs_exist_ok=history_preserved,
            symlinks=True,
            ignore=_verification_snapshot_ignore,
        )
        if _is_windows_platform():
            _validate_windows_snapshot_source(
                original_root,
                original_task=None,
                declared_roots=(),
            )
        # The reviewer may write inside this copy while running existing checks. Rewrite
        # links that point back into the submitted workspace and remove links that escape it,
        # so a check cannot read or mutate host paths through a copied symlink.
        _sanitize_copied_workspace_symlinks(
            original_root,
            snapshot_root,
            trusted_external_symlinks=trusted_mounts,
        )
        if history_preserved:
            _sanitize_verification_snapshot_git(snapshot_root)
            _copy_verification_safe_git_config(original_root, snapshot_root)
            _copy_verification_git_file(original_root, snapshot_root, "info/exclude")
            if _git_config_bool(snapshot_root, "core.sparsecheckout"):
                _copy_verification_git_file(
                    original_root,
                    snapshot_root,
                    "info/sparse-checkout",
                    required=True,
                )
            _copy_snapshot_git_index(original_root, snapshot_root)
            _hide_verification_runtime_state(snapshot_root)
        verification = VerificationWorkspaceSnapshot(
            original_root=original_root,
            snapshot_root=snapshot_root.resolve(),
            temp_root=temp_root,
        )
        object.__setattr__(
            verification,
            "submitted_manifest",
            _verification_worktree_manifest(verification.snapshot_root),
        )
        object.__setattr__(
            verification,
            "mutable_submitted_paths",
            _verification_mutable_submitted_paths(
                verification.snapshot_root,
                verification.submitted_manifest,
            ),
        )
        if history_preserved:
            object.__setattr__(
                verification,
                "git_manifest",
                _verification_git_manifest(verification.snapshot_root),
            )
            object.__setattr__(
                verification,
                "git_control_manifest",
                _verification_git_control_manifest(verification.snapshot_root),
            )
        return verification
    except WorkspaceSnapshotError:
        _cleanup_path_best_effort(temp_root)
        raise
    except Exception as exc:
        _cleanup_path_best_effort(temp_root)
        raise WorkspaceSnapshotError(
            f"failed to create verification workspace snapshot: {exc}"
        ) from exc


def copy_isolated_workspace_tree(
    source_root: Path,
    destination_root: Path,
    *,
    ignore=None,
) -> None:
    """Copy a disposable workspace without retaining links to its source or host."""

    source = source_root.resolve(strict=True)
    if _is_windows_platform():
        _validate_windows_snapshot_source(
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
    if _is_windows_platform():
        _validate_windows_snapshot_source(
            source,
            original_task=None,
            declared_roots=(),
        )
    _sanitize_copied_workspace_symlinks(source, destination_root)


def remove_isolated_workspace_tree(path: Path) -> None:
    _remove_path(path)


def apply_snapshot_patch(snapshot: WorkspaceSnapshot) -> SnapshotPatchResult:
    try:
        return _apply_snapshot_patch(snapshot)
    except WorkspaceSnapshotError:
        raise
    except OSError as exc:
        raise SnapshotPatchError(f"snapshot patch filesystem operation failed: {exc}") from exc


def _apply_snapshot_patch(snapshot: WorkspaceSnapshot) -> SnapshotPatchResult:
    _restore_trusted_snapshot_git_config(snapshot)
    if _is_windows_platform():
        # Git is an unsandboxed native executable.  Audit the mutable tree
        # before allowing it to enumerate or stage coder-controlled paths.
        # In particular, reject a hardlink to an external file before Git can
        # turn that alias into patch input.
        _audit_windows_snapshot_before_git(snapshot)
    selection = _snapshot_patch_selection(snapshot)
    changed_paths = selection.changed_paths
    if not changed_paths:
        return SnapshotPatchResult(applied=False, ignored_paths=selection.ignored_paths)
    if _is_windows_platform():
        _validate_windows_original_root(snapshot)
    _validate_snapshot_patch_paths(
        snapshot.original_root,
        changed_paths,
        task_relative_path=snapshot.task_relative_path,
        declared_grading_roots=snapshot.declared_grading_roots,
    )
    if _is_windows_platform():
        _validate_windows_patch_targets(snapshot.original_root, changed_paths)
    _validate_symlink_targets(snapshot.snapshot_root, changed_paths)
    patch = _snapshot_patch(snapshot, changed_paths)
    if not patch.strip():
        raise SnapshotPatchError("snapshot reported changed paths but produced an empty patch")
    _apply_patch_to_original(snapshot, changed_paths, patch)
    return SnapshotPatchResult(
        applied=True,
        changed_paths=changed_paths,
        patch_bytes=len(patch),
        ignored_paths=selection.ignored_paths,
    )


def _audit_windows_snapshot_before_git(snapshot: WorkspaceSnapshot) -> None:
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
        if is_link_or_reparse(root, stat_result=root_metadata) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise SnapshotPatchError("snapshot workspace root was replaced or redirected")
        stack: list[tuple[Path, os.stat_result]] = [(root, root_metadata)]
        while stack:
            directory, expected = stack.pop()
            current = directory.lstat()
            if (
                is_link_or_reparse(directory, stat_result=current)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise SnapshotPatchError(
                    f"snapshot directory changed or was redirected before Git: {directory}"
                )
            children = list(directory.iterdir())
            _validate_windows_directory_names(directory, [child.name for child in children])
            stable = directory.lstat()
            if (stable.st_dev, stable.st_ino) != (current.st_dev, current.st_ino):
                raise SnapshotPatchError(
                    f"snapshot directory changed during pre-Git audit: {directory}"
                )
            for child in children:
                metadata = child.lstat()
                if is_link_or_reparse(child, stat_result=metadata):
                    raise SnapshotPatchError(
                        f"snapshot contains a Windows link/reparse entry before Git: {child}"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    if not is_skipped(child):
                        stack.append((child, metadata))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise SnapshotPatchError(
                        f"snapshot contains an unsupported filesystem entry before Git: {child}"
                    )
                if metadata.st_nlink > 1:
                    raise SnapshotPatchError(
                        f"snapshot contains a hardlinked file before Git: {child}"
                    )
    except SnapshotPatchError:
        raise
    except OSError as exc:
        raise SnapshotPatchError(
            f"failed to audit native Windows snapshot before Git: {exc}"
        ) from exc


def _validate_windows_original_root(snapshot: WorkspaceSnapshot) -> None:
    try:
        metadata = snapshot.original_root.lstat()
    except OSError as exc:
        raise SnapshotPatchError(
            f"native Windows workspace root is missing or unreadable: {snapshot.original_root}"
        ) from exc
    if (
        is_link_or_reparse(snapshot.original_root, stat_result=metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != snapshot.original_root_identity
    ):
        raise SnapshotPatchError(
            "native Windows workspace root was replaced or redirected during the run"
        )


def _restore_runtime_links(snapshot: WorkspaceSnapshot) -> tuple[str, ...]:
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
        mounts.append((snapshot.snapshot_root / ".supervisor", state_source, "supervisor_state"))
    for relative in snapshot.readonly_dependency_paths:
        source = snapshot.original_root / relative
        if source.exists() or source.is_symlink():
            mounts.append((snapshot.snapshot_root / relative, source, f"dependency:{relative}"))
    for destination, source, label in mounts:
        if snapshot.runtime_exposure_mode == RUNTIME_EXPOSURE_SYMLINK:
            if _symlink_points_to(destination, source):
                continue
            _create_runtime_exposure(
                destination,
                source,
                mode=RUNTIME_EXPOSURE_SYMLINK,
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
            destination_manifest = _runtime_exposure_manifest(destination)
        except (OSError, WorkspaceSnapshotError):
            destination_manifest = ()
        was_replaced = destination_manifest != expected_manifest
        if label.startswith("dependency:"):
            if was_replaced:
                _create_windows_dependency_exposure(
                    destination,
                    source,
                    project_root=snapshot.original_root,
                    safe_destination_root=snapshot.snapshot_root,
                )
                destination_manifest = _runtime_exposure_manifest(destination)
        else:
            source_manifest = _runtime_exposure_manifest(source)
            if destination_manifest != source_manifest:
                _create_runtime_exposure(
                    destination,
                    source,
                    mode=RUNTIME_EXPOSURE_COPY,
                    safe_destination_root=snapshot.snapshot_root,
                )
                destination_manifest = _runtime_exposure_manifest(destination)
        snapshot.runtime_copy_manifests[label] = destination_manifest
        if was_replaced:
            repaired.append(label)
    return tuple(repaired)


def _runtime_task_integrity_issue(snapshot: WorkspaceSnapshot) -> str | None:
    task = snapshot.snapshot_root / snapshot.task_relative_path
    if snapshot.runtime_exposure_mode == RUNTIME_EXPOSURE_SYMLINK:
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
        current = _runtime_exposure_manifest(task)
    except (OSError, WorkspaceSnapshotError):
        current = ()
    if current != snapshot.runtime_copy_manifests.get("task", ()):
        return "the coder workspace replaced or modified the isolated task copy"
    return None


def _snapshot_patch_selection(snapshot: WorkspaceSnapshot) -> SnapshotPatchSelection:
    snapshot_root = snapshot.snapshot_root
    excluded = [".supervisor", *snapshot.readonly_dependency_paths]
    pathspecs = [".", *(f":(exclude,top,literal){path}" for path in excluded)]
    # Exclude controller-owned runtime copies before Git walks the tree.  The
    # previous add-then-filter flow unnecessarily exposed large dependency
    # trees (and any transient corruption in them) to an unsandboxed Git.
    _run_git(snapshot_root, ["add", "-f", "-A", "--", *pathspecs])
    raw = _run_git(
        snapshot_root,
        [
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--name-only",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            snapshot.baseline_commit,
            "--",
        ],
        capture_bytes=True,
    )
    assert isinstance(raw, bytes)
    changed_paths = tuple(part.decode("utf-8", errors="surrogateescape") for part in raw.split(b"\0") if part)
    return _filter_snapshot_patch_paths(
        snapshot_root,
        changed_paths,
        readonly_dependency_paths=snapshot.readonly_dependency_paths,
    )


def _filter_snapshot_patch_paths(
    snapshot_root: Path,
    changed_paths: tuple[str, ...],
    *,
    readonly_dependency_paths: tuple[str, ...],
) -> SnapshotPatchSelection:
    kept: list[str] = []
    ignored: list[str] = []
    for path in changed_paths:
        if _is_generated_artifact_path(snapshot_root, path) or any(
            _path_is_at_or_below(path, dependency) for dependency in readonly_dependency_paths
        ):
            ignored.append(path)
        else:
            kept.append(path)
    return SnapshotPatchSelection(tuple(kept), tuple(ignored))


def _snapshot_patch(snapshot: WorkspaceSnapshot, changed_paths: Sequence[str]) -> bytes:
    raw = _run_git(
        snapshot.snapshot_root,
        [
            "--literal-pathspecs",
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            snapshot.baseline_commit,
            "--",
            *changed_paths,
        ],
        capture_bytes=True,
    )
    assert isinstance(raw, bytes)
    return raw


def _validate_snapshot_patch_paths(
    original_root: Path,
    paths: tuple[str, ...],
    *,
    task_relative_path: str,
    declared_grading_roots: tuple[str | Path, ...],
) -> None:
    if any(_path_is_at_or_below(path, task_relative_path) for path in paths):
        raise SnapshotPatchError(f"snapshot patch path rejected: task file is immutable: {task_relative_path}")
    decision = PolicyEngine(original_root, declared_grading_roots=declared_grading_roots).evaluate_patch_paths(list(paths))
    if decision.kind != PolicyDecisionKind.ALLOW:
        raise SnapshotPatchError(f"snapshot patch path rejected: {decision.reason}")


def _validate_windows_patch_targets(root: Path, paths: tuple[str, ...]) -> None:
    for raw in paths:
        relative = PureWindowsPath(raw)
        if relative.is_absolute() or relative.drive or not relative.parts:
            raise SnapshotPatchError(f"snapshot patch path is not Windows-relative: {raw}")
        current = root
        for index, part in enumerate(relative.parts):
            if part in {".", ".."}:
                raise SnapshotPatchError(
                    f"snapshot patch path contains traversal on Windows: {raw}"
                )
            if issue := windows_path_component_issue(part):
                raise SnapshotPatchError(
                    f"snapshot patch path is unsafe on Windows: {raw}: {issue}"
                )
            current /= part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                # Once a component is absent, all remaining components are new and Git
                # apply will create them under the last verified regular directory.
                break
            if is_link_or_reparse(current, stat_result=metadata):
                raise SnapshotPatchError(
                    "snapshot patch target traverses a Windows reparse point or link: "
                    f"{raw}"
                )
            if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise SnapshotPatchError(
                    f"snapshot patch parent is not a regular directory on Windows: {raw}"
                )
            if (
                index == len(relative.parts) - 1
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink > 1
            ):
                raise SnapshotPatchError(
                    "snapshot patch refuses to modify a hardlinked Windows workspace file: "
                    f"{raw}"
                )


def _validate_symlink_targets(snapshot_root: Path, paths: tuple[str, ...]) -> None:
    root = snapshot_root.resolve()
    for raw in paths:
        path = root / raw
        if not is_link_or_reparse(path):
            continue
        if _is_windows_platform():
            raise SnapshotPatchError(
                "snapshot patch refuses Windows symlink/reparse changes because safe "
                f"creation cannot be guaranteed without elevated privileges: {raw}"
            )
        target = os.readlink(path)
        target_path = Path(target)
        if target_path.is_absolute():
            raise SnapshotPatchError(f"snapshot patch creates or modifies absolute symlink: {raw} -> {target}")
        candidate = path.parent / target_path
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SnapshotPatchError(f"snapshot patch creates or modifies escaping symlink: {raw} -> {target}") from exc


def _apply_patch_to_original(
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
    patch: bytes,
) -> None:
    original_root = snapshot.original_root
    with tempfile.TemporaryDirectory(prefix="bello-patch-backup-") as raw_backup:
        backup_root = Path(raw_backup)
        normalized_symlink_paths = _rewritten_symlink_paths_for_changes(snapshot, changed_paths)
        backup_paths = tuple(dict.fromkeys((*changed_paths, *normalized_symlink_paths)))
        backup_entries = _backup_original_paths(original_root, backup_root, backup_paths)
        try:
            _normalize_original_symlink_baselines(snapshot, changed_paths)
            check = _run_git_apply(original_root, ["--check", "--binary", "--whitespace=nowarn"], patch)
            if check.returncode != 0:
                raise SnapshotPatchError(_format_apply_error("snapshot patch does not apply cleanly", check))
            applied = _run_git_apply(original_root, ["--binary", "--whitespace=nowarn"], patch)
            if applied.returncode != 0:
                raise SnapshotPatchError(_format_apply_error("snapshot patch apply failed after clean check", applied))
            _verify_applied_paths(original_root, snapshot.snapshot_root, changed_paths)
        except Exception:
            _restore_original_paths(original_root, backup_root, backup_entries)
            raise


def _normalize_original_symlink_baselines(
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
) -> None:
    affected_paths = set(_rewritten_symlink_paths_for_changes(snapshot, changed_paths))
    for rewrite in snapshot.rewritten_symlinks:
        if rewrite.path not in affected_paths:
            continue
        path = snapshot.original_root / rewrite.path
        if not path.is_symlink() or os.readlink(path) != rewrite.original_target:
            raise SnapshotPatchError(
                f"real workspace changed at rewritten symlink path during the run: {rewrite.path}"
            )
        path.unlink()
        os.symlink(rewrite.snapshot_target, path)


def _rewritten_symlink_paths_for_changes(
    snapshot: WorkspaceSnapshot,
    changed_paths: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        rewrite.path
        for rewrite in snapshot.rewritten_symlinks
        if any(
            _path_is_at_or_below(changed_path, rewrite.path)
            or _path_is_at_or_below(rewrite.path, changed_path)
            for changed_path in changed_paths
        )
    )


def _init_snapshot_git(snapshot_root: Path) -> str:
    identity = [
        "-c",
        "user.email=bello@localhost",
        "-c",
        "user.name=Bello Snapshot",
        "-c",
        "commit.gpgsign=false",
    ]
    if not (snapshot_root / ".git").exists():
        _run_git(snapshot_root, ["init", "-q"])
    _run_git(snapshot_root, ["config", "--local", "core.hooksPath", os.devnull])
    _run_git(snapshot_root, ["config", "--local", "commit.gpgsign", "false"])
    _run_git(snapshot_root, ["config", "--local", "tag.gpgsign", "false"])
    _run_git(snapshot_root, ["config", "--local", "user.email", "bello@localhost"])
    _run_git(snapshot_root, ["config", "--local", "user.name", "Bello Snapshot"])
    _run_git(snapshot_root, ["add", "-f", "-A", "--"])
    _run_git(
        snapshot_root,
        [*identity, "commit", "-q", "--no-verify", "--allow-empty", "-m", "bello coder snapshot baseline"],
    )
    baseline_commit = str(_run_git(snapshot_root, ["rev-parse", "HEAD"])).strip()
    _run_git(snapshot_root, ["update-ref", "refs/bello/baseline", baseline_commit])
    return baseline_commit


def _restore_trusted_snapshot_git_config(snapshot: WorkspaceSnapshot) -> None:
    git_dir = snapshot.snapshot_root / ".git"
    if is_link_or_reparse(git_dir) or not git_dir.is_dir():
        raise SnapshotPatchError("snapshot Git directory was replaced or removed")
    _atomic_replace_bytes(git_dir / "config", snapshot.git_config_bytes, snapshot.git_config_mode)
    worktree_config = git_dir / "config.worktree"
    if snapshot.git_worktree_config_bytes is None:
        _remove_path(worktree_config)
    else:
        _atomic_replace_bytes(
            worktree_config,
            snapshot.git_worktree_config_bytes,
            snapshot.git_worktree_config_mode or 0o644,
        )


def _detach_recovery_workspace(snapshot: WorkspaceSnapshot) -> None:
    _remove_path(snapshot.snapshot_root / ".git")
    _remove_path(snapshot.snapshot_root / ".supervisor")
    for relative in snapshot.readonly_dependency_paths:
        _remove_path(snapshot.snapshot_root / relative)
    task = snapshot.snapshot_root / snapshot.task_relative_path
    _remove_path(task)
    if _is_windows_platform():
        _ensure_safe_runtime_destination_parent(task, snapshot.snapshot_root)
    else:
        task.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_bytes(task, snapshot.task_bytes, 0o644)


def _read_regular_file(path: Path) -> tuple[bytes, int]:
    try:
        descriptor = _open_regular_file_no_follow(path)
    except OSError as exc:
        raise WorkspaceSnapshotError(f"snapshot Git control file is not a regular file: {path}") from exc
    try:
        mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), mode
    finally:
        os.close(descriptor)


def _regular_file_matches(path: Path, expected: bytes) -> bool:
    descriptor: int | None = None
    try:
        descriptor = _open_regular_file_no_follow(path)
        content = bytearray()
        while len(content) <= len(expected):
            chunk = os.read(descriptor, min(1024 * 1024, len(expected) + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        return bytes(content) == expected
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_replace_bytes(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def _clone_git_metadata(
    original_root: Path,
    snapshot_root: Path,
    *,
    fail_on_clone_error: bool = False,
) -> bool:
    probe = subprocess.run(
        [_git_executable(original_root), "rev-parse", "--show-toplevel"],
        cwd=original_root,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        return False
    try:
        top_level = Path(probe.stdout.strip()).resolve()
    except OSError:
        return False
    if top_level != original_root:
        return False
    cloned = subprocess.run(
        [
            _git_executable(original_root),
            "clone",
            "--quiet",
            "--no-hardlinks",
            "--no-checkout",
            str(original_root),
            str(snapshot_root),
        ],
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if cloned.returncode == 0:
        return True
    _cleanup_path_best_effort(snapshot_root)
    if fail_on_clone_error:
        detail = cloned.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceSnapshotError(
            "failed to clone Git metadata for verification snapshot"
            + (f": {detail}" if detail else "")
        )
    return False


def _is_top_level_git_repository(root: Path) -> bool:
    probe = subprocess.run(
        [_git_executable(root), "rev-parse", "--show-toplevel"],
        cwd=root,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if probe.returncode != 0:
        return False
    try:
        return Path(probe.stdout.strip()).resolve() == root.resolve()
    except OSError:
        return False


def _verification_snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    # Runtime state is not part of the submitted artifact and must not become writable
    # review input.  Keep every other path, including caches and untracked files, so the
    # copied worktree and its Git status retain the candidate state exactly.
    ignored = {_name_key(".git"), _name_key(".supervisor")}
    return {name for name in names if _name_key(name) in ignored}


def _verification_trusted_mounts(
    original_root: Path,
    source_snapshot: WorkspaceSnapshot | None,
) -> dict[str, Path]:
    if source_snapshot is None:
        return {}
    if source_snapshot.snapshot_root.resolve() != original_root:
        raise WorkspaceSnapshotError(
            "verification source snapshot does not match the submitted workspace"
        )
    if source_snapshot.runtime_exposure_mode == RUNTIME_EXPOSURE_COPY:
        if issue := source_snapshot.task_integrity_issue():
            raise WorkspaceSnapshotError(
                f"trusted verification task exposure failed integrity validation: {issue}"
            )
        for relative in source_snapshot.readonly_dependency_paths:
            label = f"dependency:{relative}"
            try:
                manifest = _runtime_exposure_manifest(original_root / relative)
            except OSError as exc:
                raise WorkspaceSnapshotError(
                    f"trusted verification dependency exposure is missing: {relative}"
                ) from exc
            if manifest != source_snapshot.runtime_copy_manifests.get(label, ()):
                raise WorkspaceSnapshotError(
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
        if not _symlink_points_to(link, target):
            raise WorkspaceSnapshotError(
                f"trusted verification mount is missing or redirected: {relative}"
            )
    return mounts


def _verification_gitlink_paths(root: Path) -> tuple[str, ...]:
    probe = subprocess.run(
        [_git_executable(root), "ls-files", "--stage", "-z"],
        cwd=root,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if probe.returncode != 0:
        return ()
    paths: list[str] = []
    for record in probe.stdout.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if separator and metadata.startswith(b"160000 "):
            paths.append(raw_path.decode("utf-8", errors="surrogateescape"))
    return tuple(paths)


def _git_metadata_path(root: Path, relative: str, *, required: bool) -> Path | None:
    raw = str(_run_git(root, ["rev-parse", "--git-path", relative])).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    common_raw = str(_run_git(root, ["rev-parse", "--git-common-dir"])).strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = root / common
    if not path.exists() and not path.is_symlink():
        if required:
            raise WorkspaceSnapshotError(f"required Git metadata file is missing: {relative}")
        return None
    if is_link_or_reparse(path) or not path.is_file():
        raise WorkspaceSnapshotError(f"Git metadata file is not a regular file: {relative}")
    try:
        common_resolved = common.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(common_resolved)
    except (OSError, ValueError) as exc:
        raise WorkspaceSnapshotError(
            f"Git metadata path escapes the repository common directory: {relative}"
        ) from exc
    return resolved


def _copy_verification_git_file(
    original_root: Path,
    snapshot_root: Path,
    relative: str,
    *,
    required: bool = False,
) -> None:
    source = _git_metadata_path(original_root, relative, required=required)
    target_raw = str(_run_git(snapshot_root, ["rev-parse", "--git-path", relative])).strip()
    target = Path(target_raw)
    if not target.is_absolute():
        target = snapshot_root / target
    if source is None:
        _remove_path(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _remove_path(target)
    shutil.copy2(source, target, follow_symlinks=False)


def _git_config_file_values(path: Path, key: str) -> list[str]:
    completed = subprocess.run(
        [_git_executable(path.parent), "config", "--file", str(path), "--get-all", key],
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise WorkspaceSnapshotError(
            f"failed to read safe Git config key {key}: {completed.stderr.strip()}"
        )
    return completed.stdout.splitlines() if completed.returncode == 0 else []


def _git_config_has_include(path: Path) -> bool:
    completed = subprocess.run(
        [
            _git_executable(path.parent),
            "config",
            "--file",
            str(path),
            "--name-only",
            "--get-regexp",
            r"^include(if)?\..*",
        ],
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode not in {0, 1}:
        raise WorkspaceSnapshotError(
            f"failed to inspect Git config includes: {completed.stderr.strip()}"
        )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _copy_verification_safe_git_config(original_root: Path, snapshot_root: Path) -> None:
    config_files: list[Path] = []
    for relative in ("config", "config.worktree"):
        path = _git_metadata_path(original_root, relative, required=relative == "config")
        if path is not None:
            if _git_config_has_include(path):
                raise WorkspaceSnapshotError(
                    "verification snapshot refuses repository-local Git config includes"
                )
            config_files.append(path)
    for key, allowed in VERIFICATION_SAFE_GIT_CONFIG.items():
        values: list[str] = []
        for config_file in config_files:
            values.extend(_git_config_file_values(config_file, key))
        if not values:
            continue
        value = values[-1].strip().lower()
        if allowed is not None and value not in allowed:
            raise WorkspaceSnapshotError(
                f"unsupported value for safe Git config key {key}: {value}"
            )
        _run_git(snapshot_root, ["config", "--local", key, value])


def _git_config_bool(root: Path, key: str) -> bool:
    values = _optional_git_lines(root, ["config", "--local", "--bool", "--get", key])
    return bool(values and values[-1].strip().lower() == "true")


def _reject_verification_git_alternates(root: Path) -> None:
    alternates = _git_metadata_path(root, "objects/info/alternates", required=False)
    if alternates is None:
        return
    if alternates.stat().st_size > 0:
        raise WorkspaceSnapshotError(
            "verification snapshot refuses external Git object alternates"
        )


def _copy_snapshot_git_index(original_root: Path, snapshot_root: Path) -> None:
    source = _git_metadata_path(original_root, "index", required=False)
    target_raw = str(_run_git(snapshot_root, ["rev-parse", "--git-path", "index"])).strip()
    target = Path(target_raw)
    if not target.is_absolute():
        target = snapshot_root / target
    if source is None:
        # An unborn or empty repository may legitimately have no index yet.  The cloned
        # repository is still useful for inspection, and copied files remain visible as
        # untracked state.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)
    # A split index contains only a delta and refers to a sibling sharedindex.<hash> file.
    # Local clone does not reliably carry that unreferenced file, so copy exactly the
    # referenced shared index before asking Git to materialize a standalone target index.
    shared_raw = str(_run_git(original_root, ["rev-parse", "--shared-index-path"])).strip()
    if shared_raw:
        shared_index = Path(shared_raw)
        if not shared_index.is_absolute():
            shared_index = original_root / shared_index
        if (
            shared_index.is_symlink()
            or not shared_index.is_file()
            or shared_index.parent.resolve() != source.parent.resolve()
            or re.fullmatch(r"sharedindex\.[0-9a-fA-F]{40,64}", shared_index.name) is None
        ):
            raise WorkspaceSnapshotError(
                "verification snapshot source shared index is not a regular file"
            )
        shutil.copy2(
            shared_index,
            target.parent / shared_index.name,
            follow_symlinks=False,
        )
    _run_git(snapshot_root, ["update-index", "--no-split-index"])
    _run_git(snapshot_root, ["ls-files", "--stage", "-z"])


def _hide_verification_runtime_state(snapshot_root: Path) -> None:
    tracked = subprocess.run(
        [_git_executable(snapshot_root), "ls-files", "--error-unmatch", "--", ".supervisor"],
        cwd=snapshot_root,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tracked.returncode == 0:
        raise WorkspaceSnapshotError(
            "verification source unexpectedly tracks private .supervisor runtime state"
        )
    history = subprocess.run(
        [_git_executable(snapshot_root), "log", "--all", "--format=%H", "--", ".supervisor"],
        cwd=snapshot_root,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if history.returncode != 0 or history.stdout.strip():
        raise WorkspaceSnapshotError(
            "verification source Git history exposes private .supervisor runtime state"
        )


def _verification_git_manifest(snapshot_root: Path) -> tuple[tuple[str, str], ...]:
    """Capture review-relevant Git semantics without hashing mutable object storage.

    Commands run during review may legitimately populate object/cache files, but they must
    not change which submitted revision/index/config the reviewer is judging.
    """

    entries: list[tuple[str, str]] = []
    for label, args in (
        ("head", ["rev-parse", "--verify", "-q", "HEAD"]),
        ("symbolic_head", ["symbolic-ref", "-q", "HEAD"]),
        ("refs", ["for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)"]),
        ("index", ["ls-files", "--stage", "-v", "-z"]),
        ("safe_config", ["config", "--local", "--list", "--null"]),
    ):
        completed = subprocess.run(
            [_git_executable(snapshot_root), *args],
            cwd=snapshot_root,
            env=_isolated_git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if label in {"head", "symbolic_head"} and completed.returncode in {1, 128}:
            value = ""
        elif completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceSnapshotError(
                f"failed to capture verification Git {label}: {detail or completed.returncode}"
            )
        else:
            value = hashlib.sha256(completed.stdout).hexdigest()
        entries.append((label, value))
    for relative in ("info/exclude", "info/sparse-checkout"):
        path = _git_metadata_path(snapshot_root, relative, required=False)
        entries.append((relative, _sha256_file(path) if path is not None else ""))
    return tuple(entries)


def _verification_git_control_manifest(
    snapshot_root: Path,
) -> tuple[tuple[str, SnapshotPathState], ...]:
    git_dir = snapshot_root / ".git"
    entries: list[tuple[str, SnapshotPathState]] = []
    for current, dirs, files in os.walk(git_dir, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(git_dir)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if relative == "logs" or relative.startswith("logs/"):
                continue
            if is_link_or_reparse(path):
                entries.append((relative, _snapshot_path_state(path)))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if relative in {
                "HEAD",
                "config",
                "config.worktree",
                "index",
                "index.lock",
                "ORIG_HEAD",
                "FETCH_HEAD",
                "COMMIT_EDITMSG",
                "info/exclude",
                "info/sparse-checkout",
            }:
                continue
            state = _snapshot_path_state(path)
            if state.kind in {"file", "symlink", "reparse"}:
                entries.append((relative, state))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _sanitize_verification_snapshot_git(snapshot_root: Path) -> None:
    git_dir = snapshot_root / ".git"
    if is_link_or_reparse(git_dir) or not git_dir.is_dir():
        raise WorkspaceSnapshotError("verification snapshot Git directory is not a regular directory")
    hooks = git_dir / "hooks"
    _remove_path(hooks)
    hooks.mkdir(mode=0o700)
    _remove_path(git_dir / "objects" / "info" / "alternates")
    for key, value in (
        ("core.hooksPath", os.devnull),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _run_git(snapshot_root, ["config", "--local", key, value])
    # A local clone adds an origin pointing at the submitted workspace.  Completion has
    # no need for it, and removing it prevents a review command from addressing the
    # candidate through a Git remote even though network access is disabled.
    for name in _optional_git_lines(snapshot_root, ["remote"]):
        _run_git(snapshot_root, ["remote", "remove", name])


def _sync_snapshot_remotes(original_root: Path, snapshot_root: Path) -> None:
    for name in _optional_git_lines(snapshot_root, ["remote"]):
        _run_git(snapshot_root, ["remote", "remove", name])
    for name in _optional_git_lines(original_root, ["remote"]):
        fetch_urls = _optional_git_lines(original_root, ["remote", "get-url", "--all", name])
        if not fetch_urls:
            continue
        _run_git(snapshot_root, ["remote", "add", name, fetch_urls[0]])
        for url in fetch_urls[1:]:
            _run_git(snapshot_root, ["remote", "set-url", "--add", name, url])
        push_urls = _optional_git_lines(original_root, ["remote", "get-url", "--push", "--all", name])
        if push_urls and push_urls != fetch_urls:
            _run_git(snapshot_root, ["remote", "set-url", "--push", name, push_urls[0]])
            for url in push_urls[1:]:
                _run_git(snapshot_root, ["remote", "set-url", "--add", "--push", name, url])


def _optional_git_lines(cwd: Path, args: list[str]) -> list[str]:
    completed = subprocess.run(
        [_git_executable(cwd), *args],
        cwd=cwd,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line]


def _clear_snapshot_worktree(snapshot_root: Path) -> None:
    for child in snapshot_root.iterdir():
        if _name_key(child.name) == _name_key(".git"):
            continue
        _remove_path(child)


def _sanitize_copied_workspace_symlinks(
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
                name for name in dirs if _name_key(name) != _name_key(".git")
            ]
        for name in sorted([*dirs, *files]):
            destination = Path(current) / name
            try:
                metadata = destination.lstat()
            except FileNotFoundError:
                continue
            if is_reparse_point(destination, stat_result=metadata) and not stat.S_ISLNK(
                metadata.st_mode
            ):
                raise WorkspaceSnapshotError(
                    "snapshot copy produced an unsupported Windows reparse entry: "
                    f"{destination}"
                )
            if not destination.is_symlink():
                continue
            relative = destination.relative_to(snapshot_root).as_posix()
            raw_target = os.readlink(destination)
            original_link = original_root / relative
            raw_target_path = Path(raw_target)
            target_candidate = raw_target_path if raw_target_path.is_absolute() else original_link.parent / raw_target_path
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
            os.symlink(safe_target, destination, target_is_directory=resolved_target.is_dir())
            rewrites.append(
                SnapshotSymlinkRewrite(
                    path=relative,
                    original_target=raw_target,
                    snapshot_target=safe_target,
                )
            )
    return tuple(rewrites), tuple(excluded)


def _verification_worktree_manifest(
    snapshot_root: Path,
) -> tuple[tuple[str, SnapshotPathState], ...]:
    entries: list[tuple[str, SnapshotPathState]] = []
    for current, dirs, files in os.walk(snapshot_root, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(snapshot_root)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            relative = (relative_dir / name).as_posix()
            if _name_key(name) == _name_key(".git"):
                continue
            if is_link_or_reparse(path):
                entries.append((relative, _snapshot_path_state(path)))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            relative = (relative_dir / name).as_posix()
            path = current_path / name
            state = _snapshot_path_state(path)
            if state.kind in {"file", "symlink", "reparse"}:
                entries.append((relative, state))
    return tuple(sorted(entries, key=lambda item: item[0]))


def _is_verification_mutable_artifact_path(raw_path: str) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in VERIFICATION_MUTABLE_ARTIFACT_DIR_NAMES for part in parts):
        return True
    lowered_name = relative.name.lower()
    if lowered_name in GENERATED_ARTIFACT_FILE_NAMES:
        return True
    return any(lowered_name.endswith(suffix) for suffix in GENERATED_ARTIFACT_SUFFIXES)


def _verification_path_is_git_ignored(snapshot_root: Path, raw_path: str) -> bool:
    completed = subprocess.run(
        [_git_executable(snapshot_root), "check-ignore", "-q", "--", raw_path],
        cwd=snapshot_root,
        env=_isolated_git_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _verification_mutable_submitted_paths(
    snapshot_root: Path,
    manifest: tuple[tuple[str, SnapshotPathState], ...],
) -> tuple[str, ...]:
    tracked: set[str] = set()
    if _is_top_level_git_repository(snapshot_root):
        raw = _run_git(snapshot_root, ["ls-files", "-z"], capture_bytes=True)
        assert isinstance(raw, bytes)
        tracked = {
            part.decode("utf-8", errors="surrogateescape")
            for part in raw.split(b"\0")
            if part
        }
    mutable: list[str] = []
    for raw_path, state in manifest:
        if raw_path in tracked or state.kind != "file":
            continue
        if _verification_path_is_git_ignored(snapshot_root, raw_path) or _looks_like_build_artifact(
            snapshot_root / raw_path,
            raw_path,
        ):
            mutable.append(raw_path)
    return tuple(sorted(mutable))


def _looks_like_build_artifact(path: Path, raw_path: str) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in VERIFICATION_BUILD_ARTIFACT_DIR_NAMES for part in parts):
        return True
    lowered_name = relative.name.lower()
    if lowered_name in GENERATED_ARTIFACT_FILE_NAMES or any(
        lowered_name.endswith(suffix) for suffix in VERIFICATION_BUILD_ARTIFACT_SUFFIXES
    ):
        return True
    try:
        descriptor = _open_regular_file_no_follow(path)
    except OSError:
        return False
    try:
        prefix = os.read(descriptor, 8)
    finally:
        os.close(descriptor)
    return (
        prefix.startswith(b"\x7fELF")
        or prefix.startswith(b"MZ")
        or prefix.startswith(b"\x00asm")
        or prefix.startswith(b"!<arch>\n")
        or prefix[:4] in {
            b"\xca\xfe\xba\xbe",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
        }
    )


def _create_windows_dependency_exposure(
    destination: Path,
    source: Path,
    *,
    project_root: Path,
    safe_destination_root: Path,
) -> None:
    """Materialize a dependency tree without retaining Windows reparse links."""

    _ensure_safe_runtime_destination_parent(destination, safe_destination_root)
    source_root = source.resolve(strict=False)
    project = project_root.resolve(strict=True)
    try:
        source_root.relative_to(project)
    except ValueError as exc:
        raise WorkspaceSnapshotError(
            f"read-only dependency root escapes the project: {source}"
        ) from exc
    root_metadata = source.lstat()
    if is_link_or_reparse(source, stat_result=root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise WorkspaceSnapshotError(
            f"read-only dependency root must be a regular directory: {source}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        before = source.lstat()
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}.bello-copy-", dir=destination.parent)
        )
        try:
            _materialize_windows_dependency_directory(
                source,
                temporary,
                project_root=project,
                active_directory_ids=set(),
            )
            after = source.lstat()
            if (
                not is_link_or_reparse(source, stat_result=after)
                and stat.S_ISDIR(after.st_mode)
                and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            ):
                _runtime_exposure_manifest(temporary)
                _remove_path(destination)
                os.replace(temporary, destination)
                return
        finally:
            _remove_path(temporary)
        if attempt == 1:
            break
    raise WorkspaceSnapshotError(
        f"read-only dependency changed while it was being materialized: {source}"
    )


def _materialize_windows_dependency_directory(
    source: Path,
    destination: Path,
    *,
    project_root: Path,
    active_directory_ids: set[tuple[int, int]],
) -> None:
    metadata = source.lstat()
    if is_link_or_reparse(source, stat_result=metadata):
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(project_root)
        except (OSError, ValueError) as exc:
            raise WorkspaceSnapshotError(
                f"dependency link/reparse target escapes the project: {source}"
            ) from exc
        resolved_metadata = resolved.lstat()
        if stat.S_ISDIR(resolved_metadata.st_mode):
            _materialize_windows_dependency_directory(
                resolved,
                destination,
                project_root=project_root,
                active_directory_ids=active_directory_ids,
            )
            return
        if not stat.S_ISREG(resolved_metadata.st_mode):
            raise WorkspaceSnapshotError(
                f"dependency link/reparse target is unsupported: {source}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination, follow_symlinks=False)
        stable = resolved.lstat()
        if (stable.st_dev, stable.st_ino) != (
            resolved_metadata.st_dev,
            resolved_metadata.st_ino,
        ):
            raise WorkspaceSnapshotError(
                f"dependency link target changed while copying: {source}"
            )
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceSnapshotError(
            f"dependency directory entry is unsupported: {source}"
        )

    identity = (metadata.st_dev, metadata.st_ino)
    if identity in active_directory_ids:
        raise WorkspaceSnapshotError(f"dependency link/reparse cycle detected: {source}")
    active_directory_ids.add(identity)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        children = sorted(source.iterdir(), key=lambda child: child.name.casefold())
        _validate_windows_directory_names(source, [child.name for child in children])
        stable = source.lstat()
        if (stable.st_dev, stable.st_ino) != identity:
            raise WorkspaceSnapshotError(
                f"dependency directory changed while enumerating: {source}"
            )
        for child in children:
            child_destination = destination / child.name
            child_metadata = child.lstat()
            if is_link_or_reparse(child, stat_result=child_metadata):
                _materialize_windows_dependency_directory(
                    child,
                    child_destination,
                    project_root=project_root,
                    active_directory_ids=active_directory_ids,
                )
            elif stat.S_ISDIR(child_metadata.st_mode):
                _materialize_windows_dependency_directory(
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
                    raise WorkspaceSnapshotError(
                        f"dependency file changed while copying: {child}"
                    )
            else:
                raise WorkspaceSnapshotError(
                    f"dependency tree contains an unsupported entry: {child}"
                )
            current = source.lstat()
            if (current.st_dev, current.st_ino) != identity:
                raise WorkspaceSnapshotError(
                    f"dependency directory changed while copying: {source}"
                )
    finally:
        active_directory_ids.remove(identity)


def _create_runtime_exposure(
    destination: Path,
    source: Path,
    *,
    mode: str,
    safe_destination_root: Path | None = None,
) -> None:
    if safe_destination_root is not None:
        _ensure_safe_runtime_destination_parent(destination, safe_destination_root)
    if mode == RUNTIME_EXPOSURE_SYMLINK:
        _create_readonly_link(destination, source)
        return
    if mode != RUNTIME_EXPOSURE_COPY:
        raise WorkspaceSnapshotError(f"unknown runtime exposure mode: {mode}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        before = _runtime_exposure_manifest(source)
        source_state = before[0][1] if before else SnapshotPathState(kind="absent")
        temporary: Path
        if source_state.kind == "directory":
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.bello-copy-", dir=destination.parent)
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
                _remove_path(temporary)
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
                _remove_path(temporary)
                raise
        else:
            raise WorkspaceSnapshotError(
                f"runtime exposure source is not a regular file or directory: {source}"
            )

        try:
            copied = _runtime_exposure_manifest(temporary)
            after = _runtime_exposure_manifest(source)
            if before == after and copied == before:
                _remove_path(destination)
                os.replace(temporary, destination)
                return
        finally:
            _remove_path(temporary)
        if attempt == 1:
            break
    raise WorkspaceSnapshotError(
        f"runtime exposure source changed while it was being copied: {source}"
    )


def _ensure_safe_runtime_destination_parent(destination: Path, root: Path) -> None:
    try:
        relative_parent = destination.parent.relative_to(root)
    except ValueError as exc:
        raise WorkspaceSnapshotError(
            f"runtime exposure destination escapes snapshot root: {destination}"
        ) from exc
    current = root
    root_metadata = current.lstat()
    _assert_stable_regular_entry(current, root_metadata, require_directory=True)
    for part in relative_parent.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            metadata = current.lstat()
        _assert_stable_regular_entry(current, metadata, require_directory=True)


def _runtime_exposure_manifest(path: Path) -> tuple[tuple[str, SnapshotPathState], ...]:
    try:
        root_metadata = path.lstat()
    except FileNotFoundError:
        return ()
    if is_link_or_reparse(path, stat_result=root_metadata):
        raise WorkspaceSnapshotError(
            f"runtime exposure refuses filesystem links or reparse points: {path}"
        )
    root_state = _snapshot_path_state(path)
    if root_state.kind == "file":
        _assert_stable_regular_entry(path, root_metadata, require_directory=False)
        return ((".", root_state),)
    if root_state.kind != "directory":
        raise WorkspaceSnapshotError(
            f"runtime exposure source contains an unsupported filesystem entry: {path}"
        )

    entries: list[tuple[str, SnapshotPathState]] = [(".", root_state)]
    _runtime_directory_manifest(path, path, root_metadata, entries)
    return tuple(sorted(entries, key=lambda item: item[0]))


def _runtime_directory_manifest(
    root: Path,
    directory: Path,
    expected: os.stat_result,
    entries: list[tuple[str, SnapshotPathState]],
) -> None:
    expected = _assert_stable_regular_entry(directory, expected, require_directory=True)
    children = sorted(directory.iterdir(), key=lambda child: child.name)
    expected = _assert_stable_regular_entry(directory, expected, require_directory=True)
    if _is_windows_platform():
        _validate_windows_directory_names(directory, [child.name for child in children])
    for child in children:
        _assert_stable_regular_entry(directory, expected, require_directory=True)
        metadata = child.lstat()
        if is_link_or_reparse(child, stat_result=metadata):
            raise WorkspaceSnapshotError(
                f"runtime exposure refuses filesystem links or reparse points: {child}"
            )
        relative = child.relative_to(root).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            entries.append((relative, SnapshotPathState(kind="directory")))
            _runtime_directory_manifest(root, child, metadata, entries)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkspaceSnapshotError(
                f"runtime exposure source contains an unsupported entry: {child}"
            )
        state = _snapshot_path_state(child)
        _assert_stable_regular_entry(child, metadata, require_directory=False)
        if state.kind != "file":
            raise WorkspaceSnapshotError(
                f"runtime exposure source contains an unsupported file entry: {child}"
            )
        entries.append((relative, state))
    _assert_stable_regular_entry(directory, expected, require_directory=True)


def _create_readonly_link(destination: Path, source: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    os.symlink(str(source.resolve()), destination, target_is_directory=source.is_dir())


def _symlink_points_to(path: Path, target: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return False


def _backup_original_paths(
    original_root: Path,
    backup_root: Path,
    changed_paths: tuple[str, ...],
) -> tuple[tuple[str, bool], ...]:
    entries: list[tuple[str, bool]] = []
    for raw in _minimal_changed_paths(changed_paths):
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
            raise SnapshotPatchError(f"unsupported original path type during patch backup: {raw}")
    return tuple(entries)


def _restore_original_paths(
    original_root: Path,
    backup_root: Path,
    entries: tuple[tuple[str, bool], ...],
) -> None:
    failures: list[str] = []
    for raw, existed in entries:
        destination = original_root / raw
        try:
            _remove_path(destination)
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
        raise SnapshotPatchError("snapshot patch rollback failed: " + "; ".join(failures))


def _verify_applied_paths(original_root: Path, snapshot_root: Path, changed_paths: tuple[str, ...]) -> None:
    mismatches: list[str] = []
    for raw in changed_paths:
        expected = _snapshot_path_state(snapshot_root / raw)
        actual = _snapshot_path_state(original_root / raw)
        if expected != actual:
            mismatches.append(raw)
    if mismatches:
        joined = ", ".join(mismatches[:20])
        raise SnapshotPatchError(f"snapshot patch verification failed for: {joined}")


def _snapshot_path_state(path: Path) -> SnapshotPathState:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return SnapshotPathState(kind="absent")
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        return SnapshotPathState(kind="symlink", symlink_target=os.readlink(path))
    if is_reparse_point(path, stat_result=metadata):
        try:
            target = os.readlink(path)
        except OSError:
            target = "<opaque-reparse-point>"
        return SnapshotPathState(kind="reparse", symlink_target=target)
    if stat.S_ISDIR(mode):
        return SnapshotPathState(kind="directory")
    if not stat.S_ISREG(mode):
        return SnapshotPathState(kind="unsupported")
    executable = False if _is_windows_platform() else bool(
        mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    )
    return SnapshotPathState(kind="file", sha256=_sha256_file(path), executable=executable)


def _minimal_changed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    selected: list[str] = []
    for raw in sorted(paths, key=lambda value: (len(Path(value).parts), value)):
        if any(_path_is_at_or_below(raw, existing) for existing in selected):
            continue
        selected.append(raw)
    return tuple(selected)


def _remove_path(path: Path) -> None:
    remove_path_tree(path)


def _cleanup_path_best_effort(path: Path) -> None:
    try:
        _remove_path(path)
        return
    except OSError:
        pass
    try:
        _make_tree_owner_writable(path)
        _remove_path(path)
    except OSError:
        # This helper is used only while preserving an already-active exception.  Public
        # cleanup methods use the strict path and report a remaining tree to the caller.
        return


def _make_regular_entry_owner_writable(path: Path, metadata: os.stat_result) -> None:
    metadata = _assert_stable_regular_entry(
        path,
        metadata,
        require_directory=stat.S_ISDIR(metadata.st_mode),
    )
    if _is_windows_platform() and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise OSError(
            f"refusing to change permissions through a Windows hardlink: {path}"
        )
    permissions = stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR
    if stat.S_ISDIR(metadata.st_mode):
        permissions |= stat.S_IXUSR
    os.chmod(path, permissions)


def _assert_stable_regular_entry(
    path: Path,
    expected: os.stat_result,
    *,
    require_directory: bool,
) -> os.stat_result:
    current = path.lstat()
    expected_type = stat.S_IFMT(expected.st_mode)
    if (
        is_link_or_reparse(path, stat_result=current)
        or stat.S_IFMT(current.st_mode) != expected_type
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or require_directory != stat.S_ISDIR(current.st_mode)
    ):
        raise OSError(f"filesystem entry changed or was redirected during operation: {path}")
    return current


def _make_tree_owner_writable(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if is_link_or_reparse(path, stat_result=metadata):
        return
    if stat.S_ISDIR(metadata.st_mode):
        _make_regular_entry_owner_writable(path, metadata)
        metadata = _assert_stable_regular_entry(path, metadata, require_directory=True)
        children = list(path.iterdir())
        metadata = _assert_stable_regular_entry(path, metadata, require_directory=True)
        for child in children:
            _assert_stable_regular_entry(path, metadata, require_directory=True)
            _make_tree_owner_writable(child)
        return
    _make_regular_entry_owner_writable(path, metadata)


def _sha256_file(path: Path) -> str:
    descriptor = _open_regular_file_no_follow(path)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _open_regular_file_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _path_is_at_or_below(raw_path: str, raw_parent: str) -> bool:
    if _is_windows_platform():
        path_parts = tuple(part.casefold() for part in PureWindowsPath(raw_path).parts)
        parent_parts = tuple(part.casefold() for part in PureWindowsPath(raw_parent).parts)
    else:
        path_parts = Path(raw_path).parts
        parent_parts = Path(raw_parent).parts
    return len(path_parts) >= len(parent_parts) and path_parts[: len(parent_parts)] == parent_parts


def _git_executable(cwd: Path) -> str:
    if not _is_windows_platform():
        executable = shutil.which("git")
        if executable is None:
            raise WorkspaceSnapshotError("git executable is required for workspace snapshots")
        # Preserve the existing POSIX invocation/search contract.
        return "git"
    try:
        return require_trusted_executable("git", cwd=cwd, windows=True)
    except ExecutableResolutionError as exc:
        raise WorkspaceSnapshotError(str(exc)) from exc


def _run_git(cwd: Path, args: list[str], *, capture_bytes: bool = False) -> str | bytes:
    completed = subprocess.run(
        [_git_executable(cwd), *args],
        cwd=cwd,
        env=_isolated_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"exit {completed.returncode}"
        raise WorkspaceSnapshotError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    if capture_bytes:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace")


def _run_git_apply(cwd: Path, args: list[str], patch: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [_git_executable(cwd), "apply", *args],
        cwd=cwd,
        env=_isolated_git_env(),
        input=patch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _isolated_git_env() -> dict[str, str]:
    env = os.environ.copy()
    blocked = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    for key in list(env):
        if key in blocked or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def snapshot_git_environment() -> dict[str, str]:
    return _isolated_git_env()


def _format_apply_error(prefix: str, completed: subprocess.CompletedProcess[bytes]) -> str:
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    detail = stderr or stdout or f"exit {completed.returncode}"
    return f"{prefix}: {detail}"


def _is_generated_artifact_path(snapshot_root: Path, raw_path: str) -> bool:
    relative = Path(raw_path)
    parts = tuple(part.lower() for part in relative.parts)
    if any(part in GENERATED_ARTIFACT_DIR_NAMES for part in parts):
        return True
    name = relative.name
    lowered_name = name.lower()
    if lowered_name in GENERATED_ARTIFACT_FILE_NAMES:
        return True
    if any(lowered_name.endswith(suffix) for suffix in GENERATED_ARTIFACT_SUFFIXES):
        return True
    return False


def _validate_windows_directory_names(directory: Path, names: Sequence[str]) -> None:
    seen: dict[str, str] = {}
    for name in names:
        if issue := windows_path_component_issue(name):
            raise WorkspaceSnapshotError(
                f"Windows snapshot path is unsafe at {directory / name}: {issue}"
            )
        key = name.casefold()
        previous = seen.get(key)
        if previous is not None and previous != name:
            raise WorkspaceSnapshotError(
                "Windows snapshot source contains case-colliding names in "
                f"{directory}: {previous!r} and {name!r}"
            )
        seen[key] = name


def _validate_windows_snapshot_source(
    root: Path,
    *,
    original_task: Path | None,
    declared_roots: tuple[Path, ...],
) -> None:
    """Reject Windows source topology that cannot be copied and patched safely."""

    hardlinks: dict[tuple[int, int], list[tuple[str, int, bool]]] = {}
    dependency_names = {
        name.casefold() for name in SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
    }

    git_entry = root / ".git"
    try:
        git_metadata = git_entry.lstat()
    except FileNotFoundError:
        git_metadata = None
    if git_metadata is not None and (
        is_link_or_reparse(git_entry, stat_result=git_metadata)
        or not stat.S_ISDIR(git_metadata.st_mode)
    ):
        raise WorkspaceSnapshotError(
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
            under_git = bool(relative_dir.parts and relative_dir.parts[0].casefold() == ".git")
            _validate_windows_directory_names(current_path, [*dirs, *files])

            kept_dirs: list[str] = []
            for name in dirs:
                child = current_path / name
                metadata = child.lstat()
                child_under_git = under_git or (
                    not relative_dir.parts and name.casefold() == ".git"
                )
                if is_link_or_reparse(child, stat_result=metadata):
                    raise WorkspaceSnapshotError(
                        "native Windows workspace snapshots refuse symlinks, junctions, "
                        f"mount points, and other reparse entries: {child}"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise WorkspaceSnapshotError(
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
                if is_link_or_reparse(child, stat_result=metadata):
                    raise WorkspaceSnapshotError(
                        "native Windows workspace snapshots refuse symlinks, junctions, "
                        f"mount points, and other reparse entries: {child}"
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceSnapshotError(
                        f"Windows snapshot source contains an unsupported entry: {child}"
                    )
                mutable = not child_under_git and _windows_source_path_can_be_patched(
                    root,
                    child,
                    relative,
                    original_task=original_task,
                    declared_roots=declared_roots,
                )
                hardlinks.setdefault((metadata.st_dev, metadata.st_ino), []).append(
                    (relative, metadata.st_nlink, mutable)
                )
    except WorkspaceSnapshotError:
        raise
    except OSError as exc:
        raise WorkspaceSnapshotError(
            f"failed to audit native Windows workspace filesystem topology: {exc}"
        ) from exc

    for entries in hardlinks.values():
        link_count = max(entry[1] for entry in entries)
        if link_count <= 1 or not any(entry[2] for entry in entries):
            continue
        if link_count > len(entries):
            example = next(entry[0] for entry in entries if entry[2])
            raise WorkspaceSnapshotError(
                "native Windows workspace file has a hardlink outside the audited project; "
                f"snapshot patching is unsafe: {example}"
            )


def _windows_source_path_can_be_patched(
    root: Path,
    path: Path,
    relative: str,
    *,
    original_task: Path | None,
    declared_roots: tuple[Path, ...],
) -> bool:
    ignored_names = {
        name.casefold()
        for name in SNAPSHOT_ALWAYS_IGNORE_NAMES | SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES
    }
    if any(part.casefold() in ignored_names for part in Path(relative).parts):
        return False
    if original_task is not None:
        try:
            if path.resolve(strict=False) == original_task:
                return False
        except OSError:
            return False
    if is_protected_path(root, path) or is_supervisor_runtime_path(root, path):
        return False
    return not _matches_declared_root(path, declared_roots)


def _snapshot_ignore(
    original_root: Path,
    declared_roots: tuple[Path, ...],
    *,
    original_task: Path,
    readonly_dependencies: list[tuple[Path, str]],
):
    root = original_root.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        current = Path(directory)
        dependency_names = {_name_key(value) for value in SNAPSHOT_READ_ONLY_DEPENDENCY_NAMES}
        always_ignore_names = {_name_key(value) for value in SNAPSHOT_ALWAYS_IGNORE_NAMES}
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
            if _name_key(name) in dependency_names:
                readonly_dependencies.append((candidate, candidate_relative))
                ignored.add(name)
                continue
            if _name_key(name) in always_ignore_names:
                ignored.add(name)
                continue
            if is_protected_path(root, candidate) or is_supervisor_runtime_path(root, candidate):
                ignored.add(name)
                continue
            if _matches_declared_root(candidate, declared_roots):
                ignored.add(name)
        return ignored

    return ignore


def _resolve_declared_roots(project_root: Path, roots: tuple[str | Path, ...]) -> tuple[Path, ...]:
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


def _matches_declared_root(path: Path, roots: tuple[Path, ...]) -> bool:
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
