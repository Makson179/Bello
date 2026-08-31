from __future__ import annotations

import os
import stat
from pathlib import Path


WINDOWS_RESERVED_PATH_STEMS = {
    "aux",
    "clock$",
    "con",
    "conin$",
    "conout$",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
WINDOWS_INVALID_PATH_CHARACTERS = frozenset('<>:"/\\|?*')


def is_windows_platform() -> bool:
    return os.name == "nt"


def is_reparse_point(path: Path, *, stat_result: os.stat_result | None = None) -> bool:
    """Return whether *path* is backed by a Windows reparse point.

    Directory junctions are not consistently reported as symlinks by supported Python
    versions.  The file-attribute bit is available through ``lstat`` without following
    the target and covers junctions, symlinks, mount points, and unknown reparse tags.
    Unknown tags are deliberately treated as links by callers.
    """

    try:
        metadata = stat_result if stat_result is not None else path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def is_link_or_reparse(path: Path, *, stat_result: os.stat_result | None = None) -> bool:
    try:
        metadata = stat_result if stat_result is not None else path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or is_reparse_point(path, stat_result=metadata)


def remove_link_or_reparse(path: Path, *, stat_result: os.stat_result | None = None) -> None:
    """Remove a link/reparse leaf without traversing its target."""

    metadata = stat_result if stat_result is not None else path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        path.unlink()
        return
    if not is_reparse_point(path, stat_result=metadata):
        raise OSError(f"not a filesystem link or reparse point: {path}")

    # Junctions and directory mount points require RemoveDirectory on Windows.  Other
    # reparse-backed leaves (including file symlinks) use unlink.  Neither operation
    # follows the target.
    directory_attribute = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if attributes & directory_attribute or stat.S_ISDIR(metadata.st_mode):
        path.rmdir()
    else:
        path.unlink()


def remove_path_tree(
    path: Path,
    *,
    stat_result: os.stat_result | None = None,
) -> None:
    """Remove *path* without ever recursively following a filesystem link.

    Every ordinary directory is revalidated around enumeration and before a
    permission change.  Reparse points (including Windows junctions) are always
    leaves.  This intentionally raises when an entry changes identity during the
    operation instead of risking traversal into a replacement target.
    """

    try:
        metadata = stat_result if stat_result is not None else path.lstat()
    except FileNotFoundError:
        return
    if is_link_or_reparse(path, stat_result=metadata):
        try:
            remove_link_or_reparse(path, stat_result=metadata)
        except PermissionError:
            if not is_windows_platform():
                raise
            _clear_windows_readonly_without_following(path, metadata)
            current = path.lstat()
            if not is_link_or_reparse(path, stat_result=current):
                raise OSError(f"filesystem link changed during cleanup: {path}")
            remove_link_or_reparse(path, stat_result=current)
        return
    if stat.S_ISDIR(metadata.st_mode):
        metadata = _assert_stable_regular_entry(path, metadata, require_directory=True)
        required = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if stat.S_IMODE(metadata.st_mode) & required != required:
            metadata = _make_regular_entry_owner_writable(path, metadata)
        children = list(path.iterdir())
        metadata = _assert_stable_regular_entry(path, metadata, require_directory=True)
        for child in children:
            _assert_stable_regular_entry(path, metadata, require_directory=True)
            remove_path_tree(child)
        metadata = _assert_stable_regular_entry(path, metadata, require_directory=True)
        try:
            path.rmdir()
        except PermissionError:
            metadata = _make_regular_entry_owner_writable(path, metadata)
            _assert_stable_regular_entry(path, metadata, require_directory=True)
            path.rmdir()
        return
    metadata = _assert_stable_regular_entry(path, metadata, require_directory=False)
    try:
        path.unlink()
    except PermissionError:
        metadata = _make_regular_entry_owner_writable(path, metadata)
        _assert_stable_regular_entry(path, metadata, require_directory=False)
        path.unlink()


def _make_regular_entry_owner_writable(
    path: Path,
    metadata: os.stat_result,
) -> os.stat_result:
    metadata = _assert_stable_regular_entry(
        path,
        metadata,
        require_directory=stat.S_ISDIR(metadata.st_mode),
    )
    if is_windows_platform() and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise OSError(
            f"refusing to change permissions through a Windows hardlink during cleanup: {path}"
        )
    permissions = stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR
    if stat.S_ISDIR(metadata.st_mode):
        permissions |= stat.S_IXUSR
    os.chmod(path, permissions)
    return _assert_stable_regular_entry(
        path,
        metadata,
        require_directory=stat.S_ISDIR(metadata.st_mode),
    )


def _assert_stable_regular_entry(
    path: Path,
    expected: os.stat_result,
    *,
    require_directory: bool,
) -> os.stat_result:
    current = path.lstat()
    if (
        is_link_or_reparse(path, stat_result=current)
        or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or require_directory != stat.S_ISDIR(current.st_mode)
    ):
        raise OSError(f"filesystem entry changed or was redirected during cleanup: {path}")
    return current


def _clear_windows_readonly_without_following(path: Path, expected: os.stat_result) -> None:
    """Clear READONLY on a Windows link itself using an OPEN_REPARSE_POINT handle."""

    import ctypes
    from ctypes import wintypes

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
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
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # FILE_WRITE_ATTRIBUTES, all sharing modes, OPEN_EXISTING,
    # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
    handle = kernel32.CreateFileW(
        str(path),
        0x0100,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # The reparse-point handle remains bound to this entry even if its path
        # is renamed after this check, so subsequent attribute writes cannot be
        # redirected through a replacement junction.
        _assert_stable_link_entry(path, expected)
        info = _FileBasicInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        readonly = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        info.FileAttributes &= ~readonly
        if not kernel32.SetFileInformationByHandle(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    _assert_stable_link_entry(path, expected)


def _assert_stable_link_entry(path: Path, expected: os.stat_result) -> None:
    current = path.lstat()
    if (
        not is_link_or_reparse(path, stat_result=current)
        or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise OSError(f"filesystem link changed during cleanup: {path}")


def windows_name_key(name: str) -> str:
    return name.casefold() if is_windows_platform() else name


def windows_path_component_issue(name: str) -> str | None:
    """Describe why a component cannot be represented safely through Win32 paths."""

    if not name or name in {".", ".."}:
        return "empty or relative path component"
    if name.endswith((" ", ".")):
        return "trailing spaces and periods are ambiguous on Windows"
    if any(ord(character) < 32 for character in name):
        return "control character is not supported on Windows"
    invalid = sorted(set(name) & WINDOWS_INVALID_PATH_CHARACTERS)
    if invalid:
        return f"invalid Windows path character {invalid[0]!r}"
    stem = name.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED_PATH_STEMS:
        return "reserved Windows device name"
    return None
