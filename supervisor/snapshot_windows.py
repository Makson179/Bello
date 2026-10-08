"""Native Windows snapshot guards, watchers, and platform strategy.

Each control owns its kernel handles. The public compatibility subclasses supply
the live service interface without changing their dataclass constructor contracts."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from supervisor.snapshot_services import SnapshotServices


def _windows_api_path(ops: SnapshotServices, /, path: Path) -> str:
    """Return an absolute extended-length spelling for Win32 file APIs."""

    raw = str(path.absolute())
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _close_windows_handle(ops: SnapshotServices, /, handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _is_windows_platform(ops: SnapshotServices, /) -> bool:
    # Kept as a small seam so both filesystem strategies are testable from POSIX.
    return ops._host_is_windows_platform()


def _runtime_exposure_mode(ops: SnapshotServices, /) -> str:
    return (
        ops.RUNTIME_EXPOSURE_COPY
        if ops._is_windows_platform()
        else ops.RUNTIME_EXPOSURE_SYMLINK
    )


def _native_windows_runtime_controls_enabled(ops: SnapshotServices, /) -> bool:
    return os.name == "nt" and ops._is_windows_platform()


def _name_key(ops: SnapshotServices, /, name: str) -> str:
    return name.casefold() if ops._is_windows_platform() else name


@dataclass
class _WindowsRuntimeFileGuard:
    """A non-inheritable handle that prevents replacing or writing one file."""

    path: Path
    handle: int
    identity: tuple[int, int]

    @classmethod
    def _services(cls) -> SnapshotServices:
        raise NotImplementedError("public snapshot control supplies its services")

    @classmethod
    def open(cls, path: Path) -> Self:
        ops = cls._services()
        if os.name != "nt":
            raise ops.WorkspaceSnapshotError(
                "Windows runtime file guards require native Windows"
            )
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
            ops._windows_api_path(path),
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
            if ops.is_link_or_reparse(path, stat_result=metadata) or not stat.S_ISREG(
                metadata.st_mode
            ):
                raise ops.WorkspaceSnapshotError(
                    f"immutable Windows runtime exposure is not a regular file: {path}"
                )
            return cls(
                path=path,
                handle=int(handle),
                identity=(metadata.st_dev, metadata.st_ino),
            )
        except BaseException:
            ops._close_windows_handle(int(handle))
            raise

    def integrity_issue(self) -> str | None:
        ops = self._services()
        try:
            metadata = self.path.lstat()
        except OSError:
            return f"protected runtime file is missing or unreadable: {self.path}"
        if (
            ops.is_link_or_reparse(self.path, stat_result=metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != self.identity
        ):
            return f"protected runtime file was replaced or redirected: {self.path}"
        return None

    def close(self) -> None:
        ops = self._services()
        if self.handle:
            handle = self.handle
            self.handle = 0
            ops._close_windows_handle(handle)


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
    def _services(cls) -> SnapshotServices:
        raise NotImplementedError("public snapshot control supplies its services")

    @classmethod
    def open(cls, path: Path) -> Self:
        ops = cls._services()
        if os.name != "nt":
            raise ops.WorkspaceSnapshotError(
                "Windows directory watchers require native Windows"
            )
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
        if ops.is_link_or_reparse(path, stat_result=metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise ops.WorkspaceSnapshotError(
                f"read-only Windows dependency exposure is not a regular directory: {path}"
            )
        # FILE_LIST_DIRECTORY with no FILE_SHARE_DELETE both arms recursive
        # notifications and prevents swapping out the watched root itself.
        handle = kernel32.CreateFileW(
            ops._windows_api_path(path),
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
            ops._close_windows_handle(int(handle))
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
        ops = self._services()
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
            ops._WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER,
            None,
            ctypes.byref(self.overlapped),
            None,
        ):
            error = ctypes.get_last_error()
            if error != 997:  # ERROR_IO_PENDING is expected for overlapped I/O.
                raise ctypes.WinError(error)

    def consume_changes(self) -> bool:
        ops = self._services()
        if self.closed:
            raise ops.WorkspaceSnapshotError(
                f"Windows dependency watcher is closed: {self.path}"
            )
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
        ops = self._services()
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
            ops._close_windows_handle(self.handle)
            self.handle = 0
        if self.event_handle:
            ops._close_windows_handle(self.event_handle)
            self.event_handle = 0
