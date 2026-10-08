"""Filesystem normalization and protected-root matching for policy checks.

Runtime dependencies are looked up through ``supervisor.policy`` so existing
imports and monkeypatches keep their original effect. The local imports defer
that lookup until a call; these helpers own no mutable engine state.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable


SECRET_FILE_GLOBS = {
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "service-account*.json",
}


SECRET_NAME_PARTS = {
    "secret",
    "credential",
    "password",
    "passwd",
    "token",
    "apikey",
    "api_key",
    "private",
    "vault",
}


SECRET_PATH_PARTS = {
    ".git",
    ".ssh",
    ".aws",
    ".kube",
}


CHEATING_WORKSPACE_PATH_PARTS = {"hidden", "id_private", "private", "grading"}


SECRET_PATH_SUFFIXES = {
    (".config", "gh"),
    (".config", "gcloud"),
    (".docker", "config.json"),
}


def windows_path_syntax_problem(raw: str | os.PathLike[str]) -> str | None:
    """Reject Win32 spellings whose target cannot be established safely.

    In particular, drive-relative paths, device namespaces, alternate data
    streams, reserved DOS devices, and Win32-normalized trailing dots/spaces
    must never become a second spelling that bypasses a protected root check.
    """

    from supervisor import policy as _policy

    text = _policy.os.fspath(raw)
    if not text or text.startswith(("http://", "https://")):
        return None
    if "\x00" in text:
        return "NUL in Windows path"
    normalized = text.replace("/", "\\")
    if normalized.startswith(("\\\\?\\", "\\\\.\\")):
        return "Windows device/extended path is ambiguous"
    drive, tail = _policy.ntpath.splitdrive(normalized)
    if drive and not (
        (len(drive) == 2 and drive[0].isalpha() and drive[1] == ":")
        or drive.startswith("\\\\")
    ):
        return "Windows provider or malformed drive path is ambiguous"
    if drive and len(drive) == 2 and drive[1] == ":" and (not tail or not tail.startswith("\\")):
        return "drive-relative Windows path is ambiguous"
    if ":" in tail:
        return "Windows alternate data stream path is ambiguous"
    for part in (part for part in normalized.split("\\") if part):
        if part in {".", ".."} or part == drive:
            continue
        issue = _policy.windows_path_component_issue(part)
        if issue is not None:
            return issue
    return None


def _path_for_platform(raw: str | os.PathLike[str], *, windows_paths: bool) -> Path | None:
    from supervisor import policy as _policy

    text = _policy.os.fspath(raw)
    if windows_paths:
        if _policy.windows_path_syntax_problem(text) is not None:
            return None
        # On a real Windows host pathlib already implements drive and UNC
        # semantics.  On POSIX, this conversion lets tests exercise relative
        # backslash paths while absolute drive/UNC inputs stay fail-closed.
        if _policy.os.name != "nt" and _policy.ntpath.isabs(text):
            return None
        if _policy.os.name != "nt":
            text = text.replace("\\", "/")
    try:
        return _policy.Path(text).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None


def _path_comparison_key(path: Path, *, windows_paths: bool) -> str:
    from supervisor import policy as _policy

    text = str(path)
    if windows_paths:
        return _policy.ntpath.normcase(text.replace("/", "\\")).rstrip("\\")
    return text


def _path_is_within(path: Path, root: Path, *, windows_paths: bool) -> bool:
    from supervisor import policy as _policy

    if not windows_paths:
        return _policy._is_relative_to(path, root)
    candidate = _policy._path_comparison_key(path, windows_paths=True)
    boundary = _policy._path_comparison_key(root, windows_paths=True)
    if candidate == boundary:
        return True
    return bool(boundary and candidate.startswith(boundary + "\\"))


def _path_has_root_identity(path: Path, root: Path) -> bool:
    """Match an existing authority even when resolve() retains a case alias.

    Inspect ancestors too: the requested leaf may not exist yet. Identity,
    unlike casefolding, preserves distinct names on case-sensitive filesystems.
    """
    from supervisor import policy as _policy

    try:
        boundary = root.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    identity = (boundary.st_dev, boundary.st_ino)
    for ancestor in (path, *path.parents):
        try:
            metadata = ancestor.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            if exc.errno != _policy.errno.ENAMETOOLONG and getattr(exc, "winerror", None) not in {
                123,  # ERROR_INVALID_NAME
                206,  # ERROR_FILENAME_EXCED_RANGE
            }:
                raise
            # Shell payloads/inline programs can be candidates without being
            # filesystem names. Windows may report invalid syntax rather than
            # ENAMETOOLONG; do not suppress generic EINVAL or access/I/O errors.
            # Skip the impossible leaf, but still inspect
            # its parents so an existing protected authority is not bypassed.
            continue
        if (metadata.st_dev, metadata.st_ino) == identity:
            return True
    return False


def path_root_hit(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    roots: Iterable[Path],
    windows_paths: bool = False,
) -> str | None:
    from supervisor import policy as _policy

    path = _policy._path_for_platform(raw, windows_paths=windows_paths)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    for root in roots:
        try:
            resolved_root = root.resolve(strict=False)
        except OSError:
            continue
        if (_policy._path_is_within(resolved, resolved_root, windows_paths=windows_paths)
                or _policy._path_has_root_identity(resolved, resolved_root)):
            return str(resolved_root)
    return None


def _parts_lower(path: Path) -> list[str]:
    return [part.lower() for part in path.parts]


def is_secret_path(path: Path) -> bool:
    from supervisor import policy as _policy

    parts = _policy._parts_lower(path)
    name = path.name.lower()
    if any(part in _policy.SECRET_PATH_PARTS for part in parts):
        return True
    for suffix in _policy.SECRET_PATH_SUFFIXES:
        if len(parts) >= len(suffix) and tuple(parts[-len(suffix) :]) == suffix:
            return True
    if any(fragment in name for fragment in _policy.SECRET_NAME_PARTS):
        return True
    return any(_policy.fnmatch.fnmatch(name, pattern.lower()) for pattern in _policy.SECRET_FILE_GLOBS)


def is_workspace_cheating_path(workspace: Path, path: Path) -> bool:
    from supervisor import policy as _policy

    try:
        relative_parts = path.resolve().relative_to(workspace.resolve()).parts
    except ValueError:
        return False
    return any(part.lower() in _policy.CHEATING_WORKSPACE_PATH_PARTS for part in relative_parts)


def is_supervisor_runtime_path(workspace: Path, path: Path) -> bool:
    try:
        relative_parts = path.resolve().relative_to(workspace.resolve()).parts
    except ValueError:
        return False
    return any(part.lower() == ".supervisor" for part in relative_parts)


def is_protected_path(workspace: Path, path: Path) -> bool:
    from supervisor import policy as _policy

    return _policy.is_secret_path(path) or _policy.is_workspace_cheating_path(workspace, path)


def is_workspace_control_path(workspace: Path, path: Path) -> bool:
    """Reserved controller/Git authorities, not guesses about project filenames."""
    from supervisor import policy as _policy

    root = workspace.resolve()
    candidate = path.resolve()
    for relative in (".git", ".supervisor", ".codex/bello-run"):
        control = root / relative
        if any(candidate == boundary or candidate.is_relative_to(boundary)
               for boundary in (control, control.resolve())):
            return True
        if _policy._path_has_root_identity(candidate, control):
            return True
    return False


def _resolve_outside_candidate(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    windows_paths: bool | None = None,
) -> Path | None:
    # Leading/trailing whitespace is part of a Win32 path spelling.  Trimming
    # it before validation would turn an unsafe alias such as ``"file "``
    # into the different, apparently safe path ``"file"``.
    from supervisor import policy as _policy

    text = _policy.os.fspath(raw).strip("'\"")
    if not text or text.startswith(("http://", "https://")):
        return None
    windows = (_policy.sys.platform == "win32") if windows_paths is None else windows_paths
    path = _policy._path_for_platform(text, windows_paths=windows)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _declared_grading_path_hit(
    raw: str | os.PathLike[str],
    *,
    cwd: Path,
    roots: tuple[Path, ...],
    windows_paths: bool = False,
) -> str | None:
    from supervisor import policy as _policy

    if not roots:
        return None
    return _policy.path_root_hit(raw, cwd=cwd, roots=roots, windows_paths=windows_paths)


def _declared_roots_from_env() -> tuple[Path, ...]:
    from supervisor import policy as _policy

    raw = _policy.os.environ.get("BELLO_DECLARED_GRADING_PATHS", "")
    if not raw:
        return ()
    roots: list[_policy.Path] = []
    for item in raw.split(_policy.os.pathsep):
        if not item.strip():
            continue
        resolved = _policy._resolve_outside_candidate(item, cwd=_policy.Path.cwd())
        if resolved is not None:
            roots.append(resolved)
    return tuple(dict.fromkeys(roots))


def normalize_path(
    workspace: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool | None = None,
) -> Path | None:
    from supervisor import policy as _policy

    windows = (_policy.sys.platform == "win32") if windows_paths is None else windows_paths
    return _policy._normalize_path(workspace, workspace, raw, windows_paths=windows)


def _normalize_path(
    workspace: Path,
    cwd: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool,
) -> Path | None:
    from supervisor import policy as _policy

    raw_text = _policy.os.fspath(raw)
    simulated_host_absolute = windows_paths and _policy.os.name != "nt" and _policy.Path(raw_text).is_absolute()
    if not windows_paths:
        # Preserve the established POSIX contract: command path normalization
        # treats ``~`` lexically here (the shell will expand it later), which
        # lets secret-name checks see components such as ``.ssh``.
        path = _policy.Path(raw_text)
    else:
        path = _policy.Path(raw_text).expanduser() if simulated_host_absolute else _policy._path_for_platform(raw, windows_paths=True)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        path_exists = path.exists()
    except (OSError, ValueError):
        # Win32 rejects wildcard and otherwise malformed components before a
        # filesystem lookup.  Treat those spellings as ambiguous instead of
        # letting policy evaluation crash; lexical protected-path checks still
        # get a chance to hard-deny names such as ``.super*``.
        return None
    parent = path if path_exists else path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, ValueError):
        try:
            resolved_parent = parent.resolve(strict=False)
        except (OSError, ValueError):
            return None
    resolved = resolved_parent if path_exists else resolved_parent / path.name
    if not _policy._path_is_within(resolved, workspace.resolve(), windows_paths=windows_paths):
        return None
    return resolved


def normalize_path_from_cwd(
    workspace: Path,
    cwd: Path,
    raw: str | os.PathLike[str],
    *,
    windows_paths: bool | None = None,
) -> Path | None:
    from supervisor import policy as _policy

    windows = (_policy.sys.platform == "win32") if windows_paths is None else windows_paths
    return _policy._normalize_path(workspace, cwd, raw, windows_paths=windows)


def _workspace_relative(workspace: Path, path: Path, *, windows_paths: bool = False) -> str:
    from supervisor import policy as _policy

    if not _policy._path_is_within(path, workspace.resolve(), windows_paths=windows_paths):
        return str(path)
    try:
        return path.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        # The POSIX simulation of a case-insensitive Windows path may differ
        # only by case.  It is still contained according to the comparison
        # above; retain the resolved spelling for diagnostics.
        return str(path)


def _command_working_directory(
    workspace: Path,
    cwd: str | None,
    *,
    windows_paths: bool = False,
) -> tuple[Path, set[str], str | None]:
    from supervisor import policy as _policy

    if cwd is None:
        return workspace.resolve(), set(), None
    resolved = _policy._normalize_path(workspace, workspace, cwd, windows_paths=windows_paths)
    if resolved is None:
        return workspace.resolve(), {"workspace_escape"}, "command working directory escapes workspace or is ambiguous"
    if _policy.is_protected_path(workspace, resolved):
        return resolved, {"secret_path"}, None
    return resolved, set(), None


def extract_paths(payload: dict[str, Any]) -> list[str]:
    from supervisor import policy as _policy

    candidates: list[str] = []
    for key in ("path", "file_path", "filepath", "cwd", "directory"):
        value = payload.get(key)
        if isinstance(value, str):
            candidates.append(value)
    for key in ("paths", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, str))
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        candidates.extend(_policy.extract_paths(tool_input))
    return candidates


def resolve_all_paths(
    workspace: Path,
    raw_paths: Iterable[str],
    *,
    windows_paths: bool | None = None,
) -> tuple[list[Path], str | None]:
    from supervisor import policy as _policy

    windows = (_policy.sys.platform == "win32") if windows_paths is None else windows_paths
    resolved: list[_policy.Path] = []
    for raw in raw_paths:
        path = _policy._normalize_path(workspace, workspace, raw, windows_paths=windows)
        if path is None:
            return [], f"path escapes workspace or is ambiguous: {raw}"
        resolved.append(path)
    return resolved, None


def _resolve_candidate_path(
    raw: str,
    *,
    cwd: Path,
    windows_paths: bool = False,
) -> Path | None:
    from supervisor import policy as _policy

    text = raw.strip("'\"")
    if not text:
        return None
    path = _policy._path_for_platform(text, windows_paths=windows_paths)
    if path is None:
        return None
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=False)
    except OSError:
        return None


def _looks_pathish(value: str) -> bool:
    if not value or value.startswith("-"):
        return False
    return value.startswith(("~", "/", ".")) or "/" in value


def _lower_pathish_parts(value: str) -> set[str]:
    from supervisor import policy as _policy

    return {part.lower() for part in _policy.Path(_policy._strip_pytest_selector(value)).parts}


def _looks_like_path_argument(token: str, workspace: Path, *, windows_paths: bool = False) -> bool:
    from supervisor import policy as _policy

    if token in {"-", "--"} or token.startswith("-"):
        return False
    if token.startswith(("http://", "https://")):
        return False
    path = _policy._path_for_platform(token, windows_paths=windows_paths)
    if path is None:
        return True
    return (
        path.is_absolute()
        or "/" in token
        or (windows_paths and "\\" in token)
        or "." in token
        or (workspace / path).exists()
    )
