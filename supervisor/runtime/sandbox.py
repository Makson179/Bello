"""Fail-closed OS sandbox for model-controlled commands.

The trusted provider/controller processes must run outside this boundary.  On
macOS restricted modes use Seatbelt through ``/usr/bin/sandbox-exec``; on
Linux they use a private bubblewrap mount/PID/network namespace.  Merely
filtering command strings or checking paths is deliberately not treated as a
security boundary.

On Windows, restricted modes delegate to the packaged native helper, which
combines a unique less-privileged AppContainer, exact temporary ACL grants,
and a kill-on-close Job Object.  The backend fails closed if that helper or
any requested filesystem authority cannot be secured.  In particular, a
standard account usually cannot grant an AppContainer access to protected
machine-wide toolchains; those need an exact host-controlled per-user runtime
root.  ``danger-full-access`` remains an explicit, unsandboxed escape hatch on
every platform.

The sandbox bounds collected/streamed output, but does not impose CPU, memory,
process-count, or workspace-size quotas.  Linux PID namespaces provide strong
descendant cleanup.  Seatbelt is inherited by macOS descendants, while process
group cleanup cannot guarantee collection of a deliberately double-forked
``setsid`` child; such a child remains filesystem/network confined.  The
strict native regression pins this known macOS limitation to OpenAI Codex
revision ``1e66885a16161048215a3782ecdd1739aab0aabf`` so it cannot silently be
mistaken for a full process-lifetime guarantee.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Literal

from supervisor.executables import resolve_trusted_executable


SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]
OutputCallback = Callable[[str], Awaitable[None]]

_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_MAX_OUTPUT_CHARS = 8 * 1024 * 1024
_OUTPUT_TRUNCATED = "\n[bello: command output truncated at 8M characters]\n"
_TERMINATE_GRACE_SECONDS = 0.5
_BACKEND_PROBE_SECONDS = 5.0
_MAC_OTOOL = Path("/usr/bin/otool")

# These are host-selected developer tools, not model-supplied command names.
# A fixed catalog keeps PATH useful without exposing a user's generic bin
# directories (which often sit beside credentials and unrelated executables).
_TOOLCHAIN_COMMANDS = (
    "node", "npm", "npx", "corepack", "pnpm", "yarn", "deno", "bun",
    "python", "python3", "pip", "pip3", "pytest", "ruff", "mypy", "uv",
    "git", "rg", "cargo", "rustc", "go", "make", "cmake", "ninja",
    "cc", "gcc", "g++", "clang", "clang++", "java", "javac", "ruby",
    "bundle", "bundler", "php", "composer", "swift", "xcodebuild", "dotnet",
)


class SandboxUnavailableError(RuntimeError):
    """The requested restricted OS boundary cannot be established."""


class SandboxPolicyError(ValueError):
    """A caller supplied an invalid or ambiguous sandbox authority."""


def _resolve_existing(path: Path, *, directory: bool | None, label: str) -> Path:
    try:
        raw = os.fspath(path)
        if "\x00" in raw:
            raise ValueError("NUL byte")
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SandboxPolicyError(f"{label} must be an existing, resolvable path: {path}") from exc
    if directory is True and not resolved.is_dir():
        raise SandboxPolicyError(f"{label} must be a directory: {resolved}")
    if directory is False and not resolved.is_file():
        raise SandboxPolicyError(f"{label} must be a file: {resolved}")
    return resolved


def _contains(parent: Path, child: Path) -> bool:
    return child == parent or child.is_relative_to(parent)


def _real_home() -> Path:
    try:
        return Path.home().resolve(strict=True)
    except OSError:
        return Path.home().resolve()


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Filesystem and network authority assigned to one model workspace.

    ``readable_roots`` are explicit read-only dependency authorities, not
    additional writable workspaces.  In ``workspace-write`` mode overlapping
    roots are rejected because pathname carve-outs can be bypassed by renaming
    an ancestor on path-based sandboxes.
    """

    root: Path
    mode: SandboxMode = "workspace-write"
    network_access: bool = False
    readable_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in _MODES:
            raise SandboxPolicyError(f"unsupported sandbox mode: {self.mode}")
        if not isinstance(self.network_access, bool):
            raise SandboxPolicyError("network_access must be a boolean")
        root = _resolve_existing(Path(self.root), directory=True, label="sandbox root")
        if self.mode != "danger-full-access":
            filesystem_root = Path(root.anchor)
            if root == filesystem_root:
                raise SandboxPolicyError("the filesystem root cannot be a restricted workspace")
            home = _real_home()
            if _contains(root, home):
                raise SandboxPolicyError("a restricted workspace cannot contain the account home")

        readable: list[Path] = []
        for entry in tuple(self.readable_roots):
            resolved = _resolve_existing(Path(entry), directory=None, label="readable root")
            if self.mode != "danger-full-access":
                if resolved == Path(resolved.anchor):
                    raise SandboxPolicyError("the filesystem root cannot be a readable dependency")
                home = _real_home()
                if _contains(resolved, home):
                    raise SandboxPolicyError("a readable dependency cannot contain the account home")
                if self.mode == "workspace-write" and (
                    _contains(root, resolved) or _contains(resolved, root)
                ):
                    if resolved != root:
                        raise SandboxPolicyError(
                            "read-only dependencies cannot overlap a writable workspace"
                        )
                    continue
            if resolved != root and resolved not in readable:
                readable.append(resolved)

        object.__setattr__(self, "root", root)
        object.__setattr__(self, "readable_roots", tuple(readable))


@dataclass(frozen=True, slots=True)
class SandboxResult:
    output: str
    exit_code: int
    duration: float
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class _Invocation:
    argv: tuple[str, ...]
    env: dict[str, str] | None
    cwd: Path
    backend: str


@dataclass(frozen=True, slots=True)
class _Toolchain:
    """Exact host-selected executables and their smallest useful runtimes."""

    shims: tuple[tuple[str, Path], ...] = ()
    readable_roots: tuple[Path, ...] = ()


def _clean_environment(home: str, temp: str, path_entries: Iterable[Path]) -> dict[str, str]:
    path: list[str] = []
    for entry in path_entries:
        value = os.fspath(entry)
        if value not in path:
            path.append(value)
    return {
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.pathsep.join(path),
        "TMPDIR": temp,
        "XDG_CACHE_HOME": f"{temp.rstrip('/')}/cache",
        "XDG_CONFIG_HOME": f"{home.rstrip('/')}/.config",
        "XDG_DATA_HOME": f"{home.rstrip('/')}/.local/share",
        "XDG_RUNTIME_DIR": f"{temp.rstrip('/')}/run",
    }


def _trusted_launcher(path: Path, label: str) -> Path:
    """Accept only a fixed, root-owned executable outside user-controlled trees."""

    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise SandboxUnavailableError(f"{label} is not installed at {path}") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise SandboxUnavailableError(f"{label} is not an executable regular file: {resolved}")
    if os.name == "posix" and (info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise SandboxUnavailableError(f"{label} is not installed as a trusted system executable")
    home = _real_home()
    if _contains(home, resolved):
        raise SandboxUnavailableError(f"{label} cannot be loaded from the account home")
    return resolved


def _existing(paths: Iterable[str]) -> tuple[Path, ...]:
    found: list[Path] = []
    for value in paths:
        path = Path(value)
        if path.exists():
            resolved = path.resolve()
            if resolved not in found:
                found.append(resolved)
    return tuple(found)


def _runtime_root() -> Path | None:
    """Return this interpreter's narrow base runtime, when safe to expose RO.

    Version managers conventionally install an interpreter below the account
    home.  Rejecting every such runtime made ordinary pyenv/venv commands
    unusable.  The exact version root is safe to expose read-only; the home
    itself (or an ancestor containing it) is not.
    """

    try:
        root = Path(sys.base_prefix).resolve(strict=True)
    except OSError:
        return None
    if root == Path(root.anchor) or _contains(root, _real_home()):
        return None
    return root


def _path_is_within_authority(path: Path, authority: Path) -> bool:
    return _contains(authority, path) if authority.is_dir() else path == authority


def _pattern_root(path: Path, pattern: tuple[str, ...], trailing: int) -> Path | None:
    parts = path.parts
    limit = len(parts) - len(pattern) + 1
    for index in range(max(limit, 0)):
        if parts[index:index + len(pattern)] != pattern:
            continue
        end = index + len(pattern) + trailing
        if end <= len(parts):
            return Path(*parts[:end])
    return None


def _tool_runtime_root(path: Path) -> Path:
    """Choose a version-scoped runtime rather than a generic user prefix."""

    # Prefer a Python framework version over its enclosing Homebrew formula.
    # It contains the interpreter, stdlib, extension modules and site scripts
    # without exposing unrelated Homebrew kegs.
    patterns = (
        (("Python.framework", "Versions"), 1),
        ((".nvm", "versions", "node"), 1),
        ((".pyenv", "versions"), 1),
        ((".asdf", "installs"), 2),
        (("mise", "installs"), 2),
        ((".rustup", "toolchains"), 1),
        (("Cellar",), 2),
    )
    for pattern, trailing in patterns:
        candidate = _pattern_root(path, pattern, trailing)
        if candidate is not None and candidate.exists():
            return candidate.resolve()

    # npm-family entry points commonly resolve from bin/npm to a script below
    # lib/node_modules.  Limit authority to that one installed package when no
    # recognizable version-manager root encloses it.
    parts = path.parts
    for index, value in enumerate(parts[:-1]):
        if value == "node_modules" and index + 1 < len(parts):
            candidate = Path(*parts[:index + 2])
            if candidate.exists():
                return candidate.resolve()

    # A conventional virtual environment is another exact runtime boundary.
    current = path.parent
    for _ in range(8):
        if (current / "pyvenv.cfg").is_file():
            return current.resolve()
        if current.parent == current:
            break
        current = current.parent
    if sys.platform == "win32":
        # Windows runtimes normally keep node.exe beside npm.cmd, and Python
        # launchers either beside python.exe or in one Scripts directory.
        # Never broaden a generic bin directory such as ~/.cargo/bin.
        parent = path.parent
        if parent.name.casefold() == "scripts" and parent.parent != parent:
            return parent.parent
        return parent
    return path


def _safe_tool_root(candidate: Path, policy: SandboxPolicy) -> Path:
    selected = _tool_runtime_root(candidate)
    home = _real_home()
    invalid = (
        selected == Path(selected.anchor)
        or _contains(selected, home)
        or (selected != policy.root and _contains(selected, policy.root))
    )
    return candidate if invalid else selected


def _homebrew_opt_root(path: Path) -> Path | None:
    parts = path.parts
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] == "opt" and index + 1 < len(parts):
            candidate = Path(*parts[:index + 2])
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if _pattern_root(canonical, ("Cellar",), 2) == canonical:
                return candidate
    return None


def _mac_linked_kegs(executable: Path) -> tuple[Path, ...]:
    """Read Mach-O load commands and return only exact Homebrew dependency kegs.

    The inspected executable is never launched.  Arbitrary absolute load paths
    are ignored: otherwise a crafted binary named ``node`` could turn this
    resolver into a confused deputy for an account credential.
    """

    if (
        sys.platform != "darwin"
        or "Cellar" not in executable.parts
        or not _MAC_OTOOL.is_file()
    ):
        return ()
    try:
        completed = subprocess.run(
            (str(_MAC_OTOOL), "-L", str(executable)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    roots: list[Path] = []
    for line in completed.stdout.splitlines()[1:]:
        raw = line.strip().split(" (", 1)[0]
        dependency = Path(raw)
        if not dependency.is_absolute():
            continue
        try:
            canonical = dependency.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        keg = _pattern_root(canonical, ("Cellar",), 2)
        if keg is None or not keg.is_dir():
            continue
        lexical = _homebrew_opt_root(dependency)
        for root in (lexical, keg.resolve()):
            if root is not None and root not in roots:
                roots.append(root)
        if lexical is not None and lexical.name.startswith("openssl@"):
            # Homebrew compiles OPENSSLDIR outside the versioned keg.  Authorize
            # only the public configuration file, never the sibling private/
            # or certs/ directories where users may keep sensitive key data.
            openssl_config = lexical.parent.parent / "etc" / lexical.name / "openssl.cnf"
            if openssl_config.is_file() and openssl_config not in roots:
                roots.append(openssl_config)
    return tuple(roots)


def _host_tool_path(policy: SandboxPolicy) -> str:
    """Drop workspace-relative PATH entries before resolving trusted tools."""

    safe: list[str] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        entry = Path(raw).expanduser()
        if not entry.is_absolute():
            continue
        try:
            lexical = entry.absolute()
            canonical = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not canonical.is_dir():
            continue
        if _contains(policy.root, lexical) or _contains(policy.root, canonical):
            continue
        value = os.fspath(lexical)
        if value not in safe:
            safe.append(value)
    return os.pathsep.join(safe)


def _discover_toolchain(policy: SandboxPolicy) -> _Toolchain:
    """Resolve a fixed tool catalog without granting any PATH directory."""

    environment = {
        "PATH": _host_tool_path(policy),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
    }
    authorities = (policy.root, *policy.readable_roots)
    shims: list[tuple[str, Path]] = []
    roots: list[Path] = []

    runtime = _runtime_root()
    if runtime is not None and not any(
        _path_is_within_authority(runtime, authority) for authority in authorities
    ):
        roots.append(runtime)

    for name in _TOOLCHAIN_COMMANDS:
        try:
            raw = resolve_trusted_executable(
                name,
                cwd=policy.root,
                excluded_roots=(policy.root,),
                environ=environment,
            )
        except (OSError, RuntimeError, ValueError):
            continue
        if raw is None:
            continue
        lexical = Path(raw).expanduser()
        if not lexical.is_absolute():
            continue
        try:
            candidate = lexical.resolve(strict=True)
            info = candidate.stat()
        except (OSError, RuntimeError):
            continue
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and not os.access(candidate, os.X_OK)):
            continue
        shims.append((name, candidate))
        if any(_path_is_within_authority(candidate, authority) for authority in authorities):
            continue
        selected = _safe_tool_root(candidate, policy)
        if not any(_path_is_within_authority(selected, root) for root in roots):
            roots.append(selected)
        for dependency in _mac_linked_kegs(candidate):
            if not any(_path_is_within_authority(dependency, root) for root in roots):
                roots.append(dependency)

    return _Toolchain(tuple(shims), tuple(roots))


def _stage_tool_shims(scratch: Path, toolchain: _Toolchain) -> Path:
    directory = scratch / "bin"
    directory.mkdir(mode=0o700)
    for name, target in toolchain.shims:
        link = directory / name
        try:
            link.symlink_to(target)
        except FileExistsError:
            continue
    return directory


def _workspace_bin_dirs(root: Path) -> tuple[Path, ...]:
    candidates = (
        root / ".venv" / "bin",
        root / "venv" / "bin",
        root / "node_modules" / ".bin",
    )
    return tuple(path for path in candidates if path.is_dir())


def _mac_system_roots() -> tuple[Path, ...]:
    roots = list(_existing((
        "/System",
        "/Library/Apple",
        "/Library/Frameworks",
        "/usr/bin",
        "/usr/sbin",
        "/usr/lib",
        "/usr/libexec",
        "/usr/share",
        "/bin",
        "/sbin",
        "/dev/fd",
        "/dev/null",
        "/dev/random",
        "/dev/urandom",
        "/private/etc/group",
        "/private/etc/hosts",
        "/private/etc/localtime",
        "/private/etc/nsswitch.conf",
        "/private/etc/passwd",
        "/private/etc/protocols",
        "/private/etc/services",
    )))
    runtime = _runtime_root()
    if runtime is not None:
        # Expose only this interpreter's canonical runtime, never the complete
        # Homebrew tree: that prefix is commonly user-writable and may contain
        # unrelated formula data.
        roots.append(runtime)
    # /bin/sh's system dispatcher consults this root-owned selector before it
    # execs the selected shell.  Keep the lexical symlink path as authority;
    # resolving it to /bin/bash alone does not authorize opening the selector.
    selector = Path("/private/var/select/sh")
    if selector.exists():
        roots.append(selector)
    return tuple(dict.fromkeys(roots))


def _private_candidates(authorities: Iterable[Path]) -> tuple[Path, ...]:
    protected: list[Path] = []
    for authority in authorities:
        if not authority.is_dir():
            continue
        for relative in (Path(".supervisor"), Path(".codex") / "bello-run"):
            lexical = authority / relative
            canonical = lexical.resolve(strict=False)
            for candidate in (lexical, canonical):
                if candidate not in protected:
                    protected.append(candidate)
    return tuple(protected)


def _private_rename_anchors(authorities: Iterable[Path]) -> tuple[Path, ...]:
    """Return namespace entries whose rename would bypass a private-path rule.

    Seatbelt evaluates path filters against the current name.  Denying the
    ``.codex/bello-run`` subtree alone is therefore insufficient: a command
    could first rename ``.codex`` and then access the same inode under its new
    name.  Keep only the two reserved namespace chains anchored; writes to
    unrelated ``.codex`` children remain available.
    """

    anchors: list[Path] = []
    for authority in authorities:
        if not authority.is_dir():
            continue
        for relative in (
            Path(".supervisor"),
            Path(".codex"),
            Path(".codex") / "bello-run",
        ):
            candidate = authority / relative
            if candidate not in anchors:
                anchors.append(candidate)
    return tuple(anchors)


def _mac_profile(
    policy: SandboxPolicy,
    scratch: Path,
    toolchain: _Toolchain = _Toolchain(),
) -> tuple[str, tuple[str, ...]]:
    """Build a constant Seatbelt program plus ``-D`` path parameters."""

    read_files: list[Path] = []
    read_dirs: list[Path] = []
    system_roots = _mac_system_roots()
    trusted_lexical_roots = (*system_roots, *toolchain.readable_roots)
    for path in (
        *system_roots,
        *toolchain.readable_roots,
        policy.root,
        *policy.readable_roots,
        scratch,
    ):
        # Seatbelt can report access to a lexical symlink (not only its target),
        # notably /private/var/select/sh.  Explicit system roots are trusted, so
        # preserve both spellings without doing that for caller-supplied paths.
        spellings = (
            (path, path.resolve(strict=False))
            if path in trusted_lexical_roots
            else (path.resolve(strict=False),)
        )
        for target in spellings:
            bucket = read_dirs if target.is_dir() else read_files
            if target not in bucket:
                bucket.append(target)

    metadata_paths: list[Path] = []
    for target in (*read_dirs, *read_files):
        for parent in target.parents:
            if parent == Path("/"):
                continue
            if parent not in metadata_paths:
                metadata_paths.append(parent)

    write_dirs = [scratch]
    if policy.mode == "workspace-write":
        write_dirs.append(policy.root)
    write_files = [Path("/dev/null")]
    authorities = (policy.root, *policy.readable_roots)
    denied = _private_candidates(authorities)
    rename_anchors = _private_rename_anchors(authorities)

    parameters: list[str] = []
    rules = [
        "(version 1)",
        "(deny default)",
        "(allow network*)" if policy.network_access else "(deny network*)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        # dyld probes the volume root on current macOS releases.  This literal
        # rule exposes no descendant file contents and is intentionally not a
        # broad ``(subpath \"/\")`` authority.
        '(allow file-read* (literal "/"))',
    ]
    for index, path in enumerate(read_dirs):
        key = f"BELLO_READ_DIR_{index}"
        parameters.append(f"{key}={path}")
        rules.append(
            f'(allow file-read* file-map-executable (literal (param "{key}")) '
            f'(subpath (param "{key}")))'
        )
    for index, path in enumerate(read_files):
        key = f"BELLO_READ_FILE_{index}"
        parameters.append(f"{key}={path}")
        rules.append(f'(allow file-read* file-map-executable (literal (param "{key}")))')
    for index, path in enumerate(metadata_paths):
        key = f"BELLO_METADATA_{index}"
        parameters.append(f"{key}={path}")
        rules.append(f'(allow file-read-metadata (literal (param "{key}")))')
    for index, path in enumerate(write_dirs):
        key = f"BELLO_WRITE_DIR_{index}"
        parameters.append(f"{key}={path}")
        rules.append(
            f'(allow file-write* (literal (param "{key}")) (subpath (param "{key}")))'
        )
    for index, path in enumerate(write_files):
        if not path.exists():
            continue
        key = f"BELLO_WRITE_FILE_{index}"
        parameters.append(f"{key}={path.resolve()}")
        rules.append(f'(allow file-write* (literal (param "{key}")))')
    for index, path in enumerate(denied):
        key = f"BELLO_PRIVATE_{index}"
        parameters.append(f"{key}={path}")
        rules.append(f'(deny file-read* (literal (param "{key}")) (subpath (param "{key}")))')
        rules.append(f'(deny file-write* (literal (param "{key}")) (subpath (param "{key}")))')
    for index, path in enumerate(rename_anchors):
        key = f"BELLO_PRIVATE_ANCHOR_{index}"
        parameters.append(f"{key}={path}")
        rules.append(f'(deny file-write-unlink (literal (param "{key}")))')
    if policy.mode == "workspace-write":
        parameters.append(f"BELLO_WORKSPACE_ANCHOR={policy.root}")
        rules.append(
            '(deny file-write-unlink '
            '(require-all (literal (param "BELLO_WORKSPACE_ANCHOR")) (vnode-type DIRECTORY)))'
        )
    return "\n".join(rules), tuple(parameters)


def _mac_invocation(
    policy: SandboxPolicy,
    cwd: Path,
    command: tuple[str, ...],
    scratch: Path,
    toolchain: _Toolchain = _Toolchain(),
) -> _Invocation:
    launcher = _trusted_launcher(Path("/usr/bin/sandbox-exec"), "macOS sandbox-exec")
    profile, parameters = _mac_profile(policy, scratch, toolchain)
    argv: list[str] = [str(launcher), "-p", profile]
    for parameter in parameters:
        argv.extend(("-D", parameter))
    argv.extend(("--", *command))
    paths = (
        *_workspace_bin_dirs(policy.root),
        scratch / "bin",
        *_existing(("/usr/bin", "/bin", "/usr/sbin", "/sbin")),
    )
    env = _clean_environment(str(scratch / "home"), str(scratch / "tmp"), paths)
    return _Invocation(tuple(argv), env, cwd, "sandbox-exec")


def _linux_system_mounts() -> tuple[tuple[Path, Path], ...]:
    candidates = [
        "/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/libexec", "/usr/share",
        "/lib", "/lib64", "/bin", "/sbin", "/etc/ld.so.cache", "/etc/group",
        "/etc/localtime", "/etc/nsswitch.conf", "/etc/passwd", "/nix/store", "/gnu/store",
    ]
    runtime = _runtime_root()
    if runtime is not None:
        candidates.append(str(runtime))
    mounts: list[tuple[Path, Path]] = []
    for value in candidates:
        destination = Path(value)
        if destination.is_symlink() or not destination.exists():
            continue
        source = destination.resolve()
        if any(_contains(existing_source, source) and _contains(existing_dest, destination)
               for existing_source, existing_dest in mounts):
            continue
        mounts.append((source, destination))
    mounts.sort(key=lambda pair: len(pair[1].parts))
    return tuple(mounts)


def _linux_launcher() -> Path:
    failures: list[str] = []
    for candidate in (Path("/usr/bin/bwrap"), Path("/bin/bwrap"), Path("/usr/local/bin/bwrap")):
        if not candidate.exists():
            continue
        try:
            return _trusted_launcher(candidate, "bubblewrap")
        except SandboxUnavailableError as exc:
            failures.append(str(exc))
    detail = f" ({'; '.join(failures)})" if failures else ""
    raise SandboxUnavailableError(
        "restricted execution on Linux requires a trusted system bubblewrap (bwrap) installation" + detail
    )


def _linux_masks(policy: SandboxPolicy) -> tuple[tuple[str, Path], ...]:
    authorities = (policy.root, *policy.readable_roots)
    if policy.mode == "workspace-write":
        # A bind mount follows symlinks; it cannot pin the link's directory
        # entry. Renaming such a private namespace link would leave its old
        # target unmasked on the next command. Refuse that ambiguous layout.
        for entry in _private_rename_anchors((policy.root,)):
            if entry.is_symlink():
                raise SandboxPolicyError(
                    f"writable private sandbox namespaces cannot be symbolic links: {entry}"
                )
    masks: list[tuple[str, Path]] = []
    for candidate in _private_candidates(authorities):
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not any(_contains(authority, resolved) for authority in authorities if authority.is_dir()):
            continue
        kind = "dir" if resolved.is_dir() else "file"
        entry = (kind, resolved)
        if entry not in masks:
            masks.append(entry)
    return tuple(masks)


def _linux_mask_anchors(
    policy: SandboxPolicy, masks: Iterable[tuple[str, Path]]
) -> tuple[Path, ...]:
    """Pin private-path parents as mountpoints, without making siblings RO."""

    if policy.mode != "workspace-write":
        return ()
    anchors: set[Path] = set()
    for _, target in masks:
        parent = target.parent
        while parent != policy.root and _contains(policy.root, parent):
            anchors.add(parent)
            parent = parent.parent
    return tuple(sorted(anchors, key=lambda path: (len(path.parts), os.fspath(path))))


def _bwrap_parent_directories(destinations: Iterable[Path]) -> tuple[Path, ...]:
    existing = {Path("/home"), Path("/home/bello"), Path("/tmp")}
    parents: set[Path] = set()
    for destination in destinations:
        current = destination.parent
        while current != current.parent:
            if current not in existing:
                parents.add(current)
            current = current.parent
    return tuple(sorted(parents, key=lambda path: (len(path.parts), os.fspath(path))))


def _linux_invocation(
    policy: SandboxPolicy,
    cwd: Path,
    command: tuple[str, ...],
    scratch: Path | None = None,
    toolchain: _Toolchain = _Toolchain(),
) -> _Invocation:
    launcher = _linux_launcher()
    path_entries = (
        *_workspace_bin_dirs(policy.root),
        *((Path("/opt/bello-tools/bin"),) if scratch is not None else ()),
        *_existing(("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/local/bin")),
    )
    clean = _clean_environment("/home/bello", "/tmp", path_entries)
    argv: list[str] = [
        str(launcher),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    if not policy.network_access:
        # bwrap's private namespace has no host/external connectivity.  It does
        # bring up a sandbox-local loopback interface for sibling descendants.
        argv.append("--unshare-net")
    argv.extend((
        "--cap-drop", "ALL",
        "--clearenv",
        "--setenv", "HOME", "/home/bello",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
        "--setenv", "XDG_CONFIG_HOME", "/home/bello/.config",
        "--setenv", "XDG_DATA_HOME", "/home/bello/.local/share",
        "--setenv", "XDG_RUNTIME_DIR", "/tmp/run",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "PATH", clean["PATH"],
        "--proc", "/proc",
        "--dev", "/dev",
        "--perms", "0700", "--tmpfs", "/tmp",
        "--dir", "/home",
        "--perms", "0700", "--dir", "/home/bello",
    ))
    system_mounts = _linux_system_mounts()
    internal_roots = tuple(
        root for root in toolchain.readable_roots
        if not any(
            _path_is_within_authority(root, authority)
            for authority in (policy.root, *policy.readable_roots)
        )
        and not any(
            _path_is_within_authority(root, destination)
            for _, destination in system_mounts
        )
    )
    destinations = [
        *(destination for _, destination in system_mounts),
        *internal_roots,
        *policy.readable_roots,
        policy.root,
    ]
    if scratch is not None:
        destinations.append(Path("/opt/bello-tools"))
    for parent in _bwrap_parent_directories(destinations):
        argv.extend(("--dir", str(parent)))
    for source, destination in system_mounts:
        argv.extend(("--ro-bind", str(source), str(destination)))
    for alias in (Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64")):
        if alias.is_symlink():
            argv.extend(("--symlink", os.readlink(alias), str(alias)))
    if scratch is not None:
        argv.extend(("--ro-bind", str(scratch), "/opt/bello-tools"))
    for dependency in internal_roots:
        argv.extend(("--ro-bind", str(dependency), str(dependency)))
    for dependency in policy.readable_roots:
        argv.extend(("--ro-bind", str(dependency), str(dependency)))
    bind = "--ro-bind" if policy.mode == "read-only" else "--bind"
    argv.extend((bind, str(policy.root), str(policy.root)))
    masks = _linux_masks(policy)
    # A masked leaf is already a mountpoint, but Linux still permits renaming
    # an ordinary ancestor such as .codex. Bind the parents first so rename
    # fails with EBUSY, then hide private leaves on top of those anchors.
    for anchor in _linux_mask_anchors(policy, masks):
        argv.extend(("--bind", str(anchor), str(anchor)))
    for kind, target in masks:
        if kind == "dir":
            argv.extend(("--tmpfs", str(target), "--remount-ro", str(target)))
        else:
            argv.extend(("--ro-bind", "/dev/null", str(target)))
    argv.extend(("--chdir", str(cwd), "--", *command))

    # bwrap clears the inner environment itself.  The same scrubbed environment
    # is used for the outer launcher so loader injection and agent sockets are
    # removed before bubblewrap starts.
    return _Invocation(tuple(argv), clean, policy.root, "bubblewrap")


def _danger_invocation(cwd: Path, command: str) -> _Invocation:
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        shell = system_root / "System32" / "cmd.exe"
        return _Invocation((str(shell), "/d", "/s", "/c", command), None, cwd, "danger-full-access")
    return _Invocation(("/bin/sh", "-c", command), None, cwd, "danger-full-access")


_PROBED_BACKENDS: set[tuple[str, str, int, int]] = set()


async def _probe_backend(invocation: _Invocation) -> None:
    launcher = Path(invocation.argv[0])
    try:
        info = launcher.stat()
    except OSError as exc:
        raise SandboxUnavailableError(f"sandbox backend disappeared: {launcher}") from exc
    key = (sys.platform, str(launcher), info.st_ino, info.st_mtime_ns)
    if key in _PROBED_BACKENDS:
        return

    marker = invocation.argv.index("--")
    probe_argv = (*invocation.argv[:marker + 1], "/usr/bin/true")
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *probe_argv,
            cwd=str(invocation.cwd),
            env=invocation.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            close_fds=True,
            start_new_session=os.name == "posix",
        )
        output, _ = await asyncio.wait_for(process.communicate(), _BACKEND_PROBE_SECONDS)
    except asyncio.CancelledError:
        if process is not None:
            await _finish_cleanup(asyncio.create_task(_terminate_process_tree(process)))
        raise
    except asyncio.TimeoutError as exc:
        if process is not None:
            await _finish_cleanup(asyncio.create_task(_terminate_process_tree(process)))
        raise SandboxUnavailableError(f"{invocation.backend} preflight timed out") from exc
    except OSError as exc:
        raise SandboxUnavailableError(f"could not start {invocation.backend}: {exc}") from exc
    if process.returncode != 0:
        detail = output.decode("utf-8", "replace").strip()[:1000]
        suffix = f": {detail}" if detail else ""
        raise SandboxUnavailableError(
            f"{invocation.backend} is installed but cannot establish the required sandbox{suffix}"
        )
    _PROBED_BACKENDS.add(key)


async def _terminate_process_tree(process: asyncio.subprocess.Process, *, descendants_only: bool = False) -> None:
    if os.name == "posix":
        group = process.pid
        if not descendants_only and process.returncode is None:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), _TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                pass
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), _TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return

    if process.returncode is not None:
        return
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    taskkill = system_root / "System32" / "taskkill.exe"
    try:
        killer = await asyncio.create_subprocess_exec(
            str(taskkill), "/PID", str(process.pid), "/T", "/F",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            close_fds=True,
        )
        await killer.wait()
    except OSError:
        process.kill()
    await process.wait()


async def _finish_cleanup(cleanup: asyncio.Task[None]) -> None:
    """Finish child cleanup even if the caller receives repeated cancellations."""

    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    cleanup.result()


class SandboxRunner:
    """Run one shell command with the authority in :class:`SandboxPolicy`."""

    def __init__(self, policy: SandboxPolicy):
        if not isinstance(policy, SandboxPolicy):
            raise TypeError("policy must be a SandboxPolicy")
        self.policy = policy

    def _cwd(self, cwd: Path) -> Path:
        resolved = _resolve_existing(Path(cwd), directory=True, label="command cwd")
        if self.policy.mode == "danger-full-access":
            return resolved
        directory_roots = (self.policy.root, *(p for p in self.policy.readable_roots if p.is_dir()))
        if not any(_contains(root, resolved) for root in directory_roots):
            raise SandboxPolicyError("command cwd is outside the assigned filesystem scope")
        return resolved

    async def run(
        self,
        command: str,
        cwd: Path,
        timeout: float,
        on_output: OutputCallback | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult:
        if not isinstance(command, str) or not command.strip():
            raise SandboxPolicyError("command must be a non-empty string")
        if "\x00" in command:
            raise SandboxPolicyError("command cannot contain a NUL byte")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise SandboxPolicyError("timeout must be a finite positive number")
        if on_output is not None and not callable(on_output):
            raise TypeError("on_output must be an async callable")
        resolved_cwd = self._cwd(Path(cwd))
        started = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return SandboxResult("", 130, time.monotonic() - started, cancelled=True)

        if sys.platform == "win32" and self.policy.mode != "danger-full-access":
            from supervisor.runtime.windows_sandbox import (
                WindowsSandboxError,
                run_restricted,
            )

            toolchain = _discover_toolchain(self.policy)
            readable_roots = tuple(dict.fromkeys((
                *self.policy.readable_roots,
                *toolchain.readable_roots,
            )))
            private_paths = _private_candidates((self.policy.root, *readable_roots))
            try:
                outcome = await run_restricted(
                    command=command,
                    cwd=resolved_cwd,
                    root=self.policy.root,
                    mode=self.policy.mode,
                    readable_roots=readable_roots,
                    private_paths=private_paths,
                    network_access=self.policy.network_access,
                    timeout=timeout,
                    on_output=on_output,
                    cancel_event=cancel_event,
                    max_output_chars=_MAX_OUTPUT_CHARS,
                    truncated_text=_OUTPUT_TRUNCATED,
                )
            except WindowsSandboxError as exc:
                raise SandboxUnavailableError(
                    f"Windows restricted sandbox failed closed: {exc}"
                ) from exc
            return SandboxResult(
                outcome.output,
                outcome.exit_code,
                time.monotonic() - started,
                timed_out=outcome.timed_out,
                cancelled=outcome.cancelled,
            )

        scratch: Path | None = None
        process: asyncio.subprocess.Process | None = None
        reader: asyncio.Task[None] | None = None
        waiter: asyncio.Task[int] | None = None
        cancellation: asyncio.Task[bool] | None = None
        chunks: list[str] = []
        retained = 0
        truncated = False

        async def collect() -> None:
            nonlocal retained, truncated
            assert process is not None and process.stdout is not None
            decoder = codecs.getincrementaldecoder("utf-8")("replace")

            async def retain(text: str) -> None:
                nonlocal retained, truncated
                if not text:
                    return
                remaining = _MAX_OUTPUT_CHARS - retained
                emitted = text[:max(remaining, 0)]
                if emitted:
                    chunks.append(emitted)
                    retained += len(emitted)
                    if on_output is not None:
                        await on_output(emitted)
                if len(emitted) < len(text) and not truncated:
                    truncated = True
                    chunks.append(_OUTPUT_TRUNCATED)
                    if on_output is not None:
                        await on_output(_OUTPUT_TRUNCATED)

            while True:
                raw = await process.stdout.read(65536)
                if not raw:
                    break
                await retain(decoder.decode(raw))
            await retain(decoder.decode(b"", final=True))

        try:
            if self.policy.mode == "danger-full-access":
                invocation = _danger_invocation(resolved_cwd, command)
            elif sys.platform == "darwin":
                scratch = Path(tempfile.mkdtemp(prefix="bello-sandbox-")).resolve()
                os.chmod(scratch, 0o700)
                for relative in ("home", "tmp"):
                    path = scratch / relative
                    path.mkdir(mode=0o700)
                toolchain = _discover_toolchain(self.policy)
                _stage_tool_shims(scratch, toolchain)
                invocation = _mac_invocation(
                    self.policy,
                    resolved_cwd,
                    ("/bin/sh", "-c", command),
                    scratch,
                    toolchain,
                )
                await _probe_backend(invocation)
            elif sys.platform.startswith("linux"):
                scratch = Path(tempfile.mkdtemp(prefix="bello-sandbox-")).resolve()
                os.chmod(scratch, 0o700)
                toolchain = _discover_toolchain(self.policy)
                _stage_tool_shims(scratch, toolchain)
                invocation = _linux_invocation(
                    self.policy,
                    resolved_cwd,
                    ("/bin/sh", "-c", command),
                    scratch,
                    toolchain,
                )
                await _probe_backend(invocation)
            else:
                raise SandboxUnavailableError(
                    f"restricted execution is not implemented safely on {sys.platform}; "
                    "select danger-full-access explicitly or use a supported sandbox host"
                )

            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
            process = await asyncio.create_subprocess_exec(
                *invocation.argv,
                cwd=str(invocation.cwd),
                env=invocation.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                close_fds=True,
                start_new_session=os.name == "posix",
                creationflags=creation_flags,
            )
            reader = asyncio.create_task(collect())
            waiter = asyncio.create_task(process.wait())
            if cancel_event is not None:
                cancellation = asyncio.create_task(cancel_event.wait())

            deadline = asyncio.get_running_loop().time() + float(timeout)
            timed_out = False
            cancelled = False
            while True:
                watched: set[asyncio.Task] = {waiter}
                if not reader.done():
                    watched.add(reader)
                if cancellation is not None:
                    watched.add(cancellation)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    break
                done, _ = await asyncio.wait(watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    timed_out = True
                    break
                if cancellation is not None and cancellation in done and cancellation.result():
                    cancelled = True
                    break
                if reader in done:
                    error = reader.exception()
                    if error is not None:
                        raise error
                if waiter in done:
                    break

            if timed_out or cancelled:
                await _terminate_process_tree(process)
            else:
                # A shell may exit after backgrounding a descendant.  Clear the
                # process group before waiting for EOF on the shared pipe.
                await _terminate_process_tree(process, descendants_only=True)
            if not reader.done():
                try:
                    await asyncio.wait_for(asyncio.shield(reader), _TERMINATE_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    reader.cancel()
                    await asyncio.gather(reader, return_exceptions=True)
            if not reader.cancelled():
                reader.result()
            code = 124 if timed_out else 130 if cancelled else int(process.returncode or 0)
            return SandboxResult(
                "".join(chunks), code, time.monotonic() - started,
                timed_out=timed_out, cancelled=cancelled,
            )
        except asyncio.CancelledError:
            if process is not None:
                await _finish_cleanup(asyncio.create_task(_terminate_process_tree(process)))
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            raise
        except OSError as exc:
            if process is not None:
                await _terminate_process_tree(process)
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if self.policy.mode == "danger-full-access" or process is not None:
                raise
            raise SandboxUnavailableError(f"sandbox process could not be started: {exc}") from exc
        except BaseException:
            if process is not None:
                await _terminate_process_tree(process)
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            raise
        finally:
            if cancellation is not None:
                cancellation.cancel()
                await asyncio.gather(cancellation, return_exceptions=True)
            if waiter is not None and not waiter.done():
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
