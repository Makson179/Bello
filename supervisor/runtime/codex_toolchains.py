"""Host-selected read-only tool dependencies missing from native ``:minimal``.

Native Codex owns execution and its sandbox. These paths only supplement its
filesystem profile: they never make a dependency writable or expose a generic
home/PATH directory. Discovery uses the same fixed catalog as Bello's other
engines; commands supplied by a model are not an input.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys

from supervisor.executables import resolve_trusted_executable
from supervisor.runtime import sandbox


_IS_MACOS = sys.platform == "darwin"
_IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")
_XCODE_SELECT = Path("/usr/bin/xcode-select")
_COMMAND_LINE_TOOLS = Path("/Library/Developer/CommandLineTools")
_APPLICATIONS = Path("/Applications")
_MAC_CRYPTEX_ALIASES = Path("/System/Cryptexes")


def _mac_cryptex_alias_directory() -> Path | None:
    """Permit Apple's public alias directory, not its broader Preboot tree.

    macOS puts /System/Cryptexes/App/usr/bin on PATH. Native permissions
    canonicalize an exact App symlink grant, leaving its lexical metadata
    inaccessible. libuv's spawn("sh") then stops on EPERM before reaching
    /bin/sh, so npm scripts fail even though their tests work directly.
    Only the fixed, root-owned, non-writable system directory is eligible.
    """
    if not _IS_MACOS:
        return None
    try:
        metadata = _MAC_CRYPTEX_ALIASES.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or _MAC_CRYPTEX_ALIASES.resolve(strict=True) != _MAC_CRYPTEX_ALIASES):
            return None
    except (OSError, RuntimeError):
        return None
    return _MAC_CRYPTEX_ALIASES


def _mac_developer_directory() -> Path | None:
    """Resolve Apple's selected toolchain, not an arbitrary reported directory.

    /usr/bin/python3 and git are dispatchers, not their actual runtimes. The
    selected CLT/Xcode tree contains their implementations, frameworks and SDKs.
    Only conventional Apple developer installations are accepted here.
    """
    if not _IS_MACOS or not _XCODE_SELECT.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(_XCODE_SELECT), "-p"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=3, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        if completed.returncode != 0:
            return None
        lexical = Path(completed.stdout.strip())
        if not lexical.is_absolute():
            return None
        canonical = lexical.resolve(strict=True)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if not canonical.is_dir():
        return None
    clt = canonical == _COMMAND_LINE_TOOLS
    xcode = (
        canonical.name == "Developer" and canonical.parent.name == "Contents"
        and canonical.parent.parent.suffix == ".app"
        and canonical.parent.parent.parent == _APPLICATIONS
    )
    return canonical if clt or xcode else None


def _current_python_paths() -> tuple[Path, ...]:
    """Retain a venv's lexical entry point as well as its canonical base runtime."""
    executable = Path(sys.executable)
    prefix, base = Path(sys.prefix), Path(sys.base_prefix)
    paths = [executable, base]
    # A symlinked venv needs pyvenv.cfg and its site packages; a Windows venv
    # also needs its redirector. Do not infer a containing tree from bin/python.
    if prefix == base or (prefix / "pyvenv.cfg").is_file():
        paths.append(prefix)
    return tuple(paths)


def _known_windows_runtime(root: Path, python_paths: tuple[Path, ...]) -> bool:
    # The generic sandbox resolver also supports directory-local Windows
    # launchers. Native grants must not expose an arbitrary PATH parent for
    # an otherwise unrecognized executable.
    if any(root == path.resolve() for path in python_paths) or (root / "pyvenv.cfg").is_file():
        return True
    if root.parent.name == "node_modules":
        return True
    return any(sandbox._pattern_root(root, pattern, trailing) == root for pattern, trailing in (
        ((".nvm", "versions", "node"), 1), ((".pyenv", "versions"), 1),
        ((".asdf", "installs"), 2), (("mise", "installs"), 2),
        ((".rustup", "toolchains"), 1),
    ))


def native_toolchain_read_paths(workspace: Path) -> tuple[Path, ...]:
    """Discover bounded runtime roots for this host and this assigned workspace.

    Call once per workspace in a backend and reuse the result for resumed and
    reviewer threads. All inspection is local; no candidate tool is executed.
    The only subprocesses are fixed system metadata readers (otool/xcode-select).
    """
    if not workspace.is_absolute():
        return ()
    try:
        policy = sandbox.SandboxPolicy(root=workspace)
    except sandbox.SandboxPolicyError:
        # A missing/invalid workspace remains the native backend's error. It
        # must not cause us to grant paths discovered relative to another cwd.
        return ()
    toolchain = sandbox._discover_toolchain(policy)
    home = Path.home().resolve()
    # These are containers for unrelated user data, never runtime boundaries.
    broad = {Path(home.anchor), home, *(home / name for name in (
        ".cache", ".local", ".config", ".codex", ".bello",
    ))}
    private = tuple(home / name for name in (".ssh", ".aws", ".gnupg", ".codex", ".bello"))
    selected: list[Path] = []

    def unsafe(path: Path) -> bool:
        return (path in broad or home.is_relative_to(path)
                or path.is_relative_to(policy.root) or policy.root.is_relative_to(path)
                or any(path.is_relative_to(root) for root in private))

    def append(path: Path) -> None:
        if not path.is_absolute():
            return
        try:
            canonical = path.resolve(strict=True)
            lexical_parent = path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return
        lexical = path.absolute()
        if (unsafe(lexical) or unsafe(canonical)
                or lexical.is_relative_to(workspace.absolute())
                or lexical_parent.is_relative_to(policy.root)):
            return
        # Native Seatbelt may check the lexical symlink before its target.
        # Exact aliases are safe; do not grant their containing PATH directory.
        for entry in (lexical, canonical):
            if entry not in selected:
                selected.append(entry)

    python_paths = _current_python_paths()
    for root in toolchain.readable_roots:
        if _IS_WINDOWS and root.is_dir() and not _known_windows_runtime(root, python_paths):
            continue
        append(root)
    for _, executable in toolchain.shims:
        append(executable)
    for path in python_paths:
        append(path)
    environment = {"PATH": sandbox._host_tool_path(policy),
                   "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")}
    for name in sandbox._TOOLCHAIN_COMMANDS:
        try:
            raw = resolve_trusted_executable(
                name, cwd=policy.root, excluded_roots=(policy.root,),
                environ=environment,
            )
            if (raw is not None and Path(raw).is_file()
                    and (os.name != "posix" or os.access(raw, os.X_OK))):
                append(Path(raw))
        except (OSError, RuntimeError, ValueError):
            continue
    developer = _mac_developer_directory()
    if developer is not None:
        append(developer)
    if _IS_MACOS:
        for path in (*sandbox._mac_public_ssl_files(), *sandbox._mac_developer_selector_paths()):
            append(path)
        cryptex_aliases = _mac_cryptex_alias_directory()
        if cryptex_aliases is not None:
            append(cryptex_aliases)
    if _IS_LINUX and not (_IS_MACOS or _IS_WINDOWS):
        # Native bubblewrap cannot bind a venv's python symlink again after
        # mounting the containing venv directory. These descendants already
        # have the same read authority; retain canonical targets outside the
        # directory, but avoid redundant mounts inside it. Seatbelt's lexical
        # alias grants are intentionally unchanged.
        directories = tuple(path for path in selected if path.is_dir())
        selected = [path for path in selected if not any(
            path != root and path.is_relative_to(root) for root in directories
        )]
    return tuple(selected)
