from __future__ import annotations

import os
import shutil
import stat
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from supervisor.filesystem_safety import is_link_or_reparse


class ExecutableResolutionError(RuntimeError):
    """Raised when a trusted native executable cannot be resolved safely."""


def is_native_windows() -> bool:
    return sys.platform == "win32"


def resolve_trusted_executable(
    command: str,
    *,
    cwd: Path | None = None,
    excluded_roots: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> str | None:
    """Resolve an executable without Windows' current-directory fallback.

    POSIX deliberately retains ``shutil.which`` behavior.  On Windows we scan
    absolute PATH entries ourselves, reject relative/empty entries and
    reparse-backed candidates, and never return a bare command name.
    """

    use_windows = is_native_windows() if windows is None else windows
    if not use_windows:
        return shutil.which(command, path=(environ or os.environ).get("PATH"))

    environment = os.environ if environ is None else environ
    blocked = [Path(root).expanduser().absolute() for root in excluded_roots]
    if cwd is not None:
        blocked.append(Path(cwd).expanduser().absolute())

    raw_command = Path(command).expanduser()
    if raw_command.is_absolute():
        candidates = [raw_command]
    elif raw_command.parent != Path("."):
        # Relative executable paths (including .\tool.exe) are precisely the
        # CreateProcess search ambiguity this resolver exists to remove.
        return None
    else:
        names = _windows_executable_names(raw_command.name, environment)
        candidates = []
        for raw_entry in environment.get("PATH", "").split(os.pathsep):
            raw_entry = raw_entry.strip()
            if not raw_entry:
                continue
            if len(raw_entry) >= 2 and raw_entry[0] == raw_entry[-1] == '"':
                raw_entry = raw_entry[1:-1]
            elif '"' in raw_entry:
                continue
            entry = Path(raw_entry).expanduser()
            if not entry.is_absolute():
                continue
            entry = entry.absolute()
            if _path_is_blocked(entry, blocked):
                continue
            candidates.extend(entry / name for name in names)

    for candidate in candidates:
        try:
            lexical = candidate.absolute()
            metadata = lexical.lstat()
            if is_link_or_reparse(lexical, stat_result=metadata):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            resolved = lexical.resolve(strict=True)
            if _path_is_blocked(lexical, blocked) or _path_is_blocked(resolved, blocked):
                continue
            if _has_reparse_ancestor(lexical.parent):
                continue
            return str(resolved)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            # Executable trust is fail-closed on unreadable topology.
            continue
    return None


def require_trusted_executable(
    command: str,
    *,
    cwd: Path | None = None,
    excluded_roots: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> str:
    resolved = resolve_trusted_executable(
        command,
        cwd=cwd,
        excluded_roots=excluded_roots,
        environ=environ,
        windows=windows,
    )
    if resolved is None:
        raise ExecutableResolutionError(
            f"trusted executable {command!r} was not found on an absolute, non-workspace PATH entry"
        )
    return resolved


def windows_system_executable(name: str) -> str:
    """Return an absolute executable from the real Windows system directory."""

    if not is_native_windows():
        raise ExecutableResolutionError("Windows system executables are unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    length = kernel32.GetSystemDirectoryW(buffer, size)
    if length == 0 or length >= size:
        raise ExecutableResolutionError("could not resolve the Windows system directory")
    candidate = Path(buffer.value) / name
    resolved = resolve_trusted_executable(str(candidate), windows=True)
    if resolved is None:
        raise ExecutableResolutionError(f"trusted Windows system executable is unavailable: {name}")
    return resolved


def _windows_executable_names(command: str, environ: Mapping[str, str]) -> tuple[str, ...]:
    if Path(command).suffix:
        return (command,)
    raw_extensions = environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    extensions: list[str] = []
    for raw in raw_extensions.split(";"):
        extension = raw.strip()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = "." + extension
        if extension.casefold() not in {item.casefold() for item in extensions}:
            extensions.append(extension)
    return tuple([command + extension for extension in extensions] + [command])


def _path_is_blocked(path: Path, roots: Iterable[Path]) -> bool:
    try:
        candidate = os.path.normcase(str(path.resolve(strict=False)))
    except OSError:
        return True
    for root in roots:
        try:
            boundary = os.path.normcase(str(root.resolve(strict=False)))
            if os.path.commonpath([candidate, boundary]) == boundary:
                return True
        except (OSError, ValueError):
            return True
    return False


def _has_reparse_ancestor(directory: Path) -> bool:
    current = directory
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return True
        if is_link_or_reparse(current, stat_result=metadata):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent
