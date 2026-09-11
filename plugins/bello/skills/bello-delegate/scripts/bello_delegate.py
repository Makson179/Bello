#!/usr/bin/env python3
"""Small, dependency-free background launcher and status reader for Bello."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping
from typing import Any


RUN_DIRECTORY = Path(".codex") / "bello-run"
ACTIVE_STATES = frozenset({"launching", "running"})
TERMINAL_STATES = frozenset({"exited", "launch_failed", "stale"})
STATE_NAME = "state.json"
LAUNCH_NAME = "launch.json"
LOCK_NAME = "active.lock"
STDOUT_NAME = "bello.log"
STDERR_NAME = "bello.err.log"
MAX_JSON_BYTES = 256 * 1024
TAIL_BYTES = 12 * 1024
TAIL_LINES = 80


class LauncherError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _ordinary_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise LauncherError(f"{label} cannot be a symbolic link: {path}")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        raise LauncherError(f"{label} must be a directory: {path}")


def _project_path(value: str | os.PathLike[str]) -> Path:
    try:
        project = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherError(f"project does not exist: {value}") from exc
    if not project.is_dir():
        raise LauncherError(f"project must be a directory: {project}")
    return project


def _run_directory(project: Path, *, create: bool) -> Path:
    codex_dir = project / ".codex"
    run_dir = project / RUN_DIRECTORY
    _ordinary_directory(codex_dir, "project .codex directory")
    _ordinary_directory(run_dir, "Bello launcher directory")
    if create:
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(run_dir, 0o700)
    return run_dir


def _safe_regular_file(path: Path, label: str) -> None:
    unlinked_identities: set[tuple[int, int]] = set()
    for _ in range(3):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise LauncherError(f"{label} cannot be a symbolic link: {path}")
        if not stat.S_ISREG(info.st_mode) or info.st_nlink not in (0, 1):
            break
        identity = (info.st_dev, info.st_ino)
        if info.st_nlink == 1:
            if identity not in unlinked_identities:
                return
            break
        # An atomic state update can unlink the inode after pathname lookup but
        # before lstat reads its metadata. Recheck the replacement, never accept
        # that unlinked inode itself or relax the hard-link/symlink checks.
        unlinked_identities.add(identity)
    raise LauncherError(f"{label} must be an ordinary, unshared file: {path}")


def _read_json(path: Path) -> dict[str, Any] | None:
    _safe_regular_file(path, path.name)
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise LauncherError(f"refusing oversized launcher file: {path}")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LauncherError(f"cannot read launcher file: {path}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LauncherError(f"invalid JSON in launcher file: {path}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"launcher JSON must be an object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _safe_regular_file(path, path.name)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _resolve_input_file(project: Path, value: str, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    if candidate.is_symlink():
        raise LauncherError(f"{label} cannot be a symbolic link: {value}")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherError(f"{label} file does not exist: {value}") from exc
    if not _is_within(resolved, project) or not resolved.is_file():
        raise LauncherError(f"{label} must be an ordinary file inside the project: {value}")
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


def _is_link_or_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _has_reparse_ancestor(directory: Path) -> bool:
    current = directory
    while True:
        try:
            metadata = current.lstat()
        except (FileNotFoundError, OSError):
            return True
        if _is_link_or_reparse(current, metadata):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _resolve_scoped_executable(
    command: str,
    project: Path,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    windows: bool | None = None,
) -> Path | None:
    """Resolve a command without consulting the project or current directory."""

    environment = os.environ if environ is None else environ
    use_windows = os.name == "nt" if windows is None else windows
    blocked = [project.expanduser().absolute(), (cwd or Path.cwd()).expanduser().absolute()]
    raw_command = Path(command).expanduser()
    if raw_command.is_absolute():
        candidates = [raw_command]
    elif raw_command.parent != Path("."):
        # Relative executable paths are ambiguous and can escape the PATH policy.
        return None
    else:
        names = (
            _windows_executable_names(raw_command.name, environment)
            if use_windows
            else (raw_command.name,)
        )
        candidates: list[Path] = []
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
            if not stat.S_ISREG(metadata.st_mode):
                continue
            if use_windows and _is_link_or_reparse(lexical, metadata):
                continue
            if not use_windows and not os.access(lexical, os.X_OK):
                continue
            resolved = lexical.resolve(strict=True)
            if _path_is_blocked(lexical, blocked) or _path_is_blocked(resolved, blocked):
                continue
            if use_windows and _has_reparse_ancestor(resolved.parent):
                continue
            return resolved
        except (FileNotFoundError, NotADirectoryError, OSError, RuntimeError):
            continue
    return None


def _find_bello(project: Path) -> Path:
    path = _resolve_scoped_executable("bello", project)
    if path is None:
        raise LauncherError("bello is not installed or is not available on PATH")
    return path


def _build_bello_command(
    project: Path,
    *,
    task: str | None = None,
    plan: str | None = None,
    executable: Path | None = None,
) -> list[str]:
    command = [str(executable or _find_bello(project))]
    if task is not None:
        command.extend(("--task", str(_resolve_input_file(project, task, "task"))))
    if plan is not None:
        command.extend(("--plan", str(_resolve_input_file(project, plan, "plan"))))
    return command


def _validate_saved_command(project: Path, launch: dict[str, Any]) -> list[str]:
    command = launch.get("command")
    executable = launch.get("executable")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
        or not isinstance(executable, str)
        or command[0] != executable
    ):
        raise LauncherError("saved Bello launch command is invalid")
    try:
        resolved_executable = Path(executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherError("saved Bello executable no longer exists") from exc
    if (
        not resolved_executable.is_file()
        or str(resolved_executable) != executable
        or _path_is_blocked(resolved_executable, (project, Path.cwd()))
    ):
        raise LauncherError("saved Bello executable identity changed")

    index = 1
    seen: set[str] = set()
    while index < len(command):
        option = command[index]
        if option not in {"--task", "--plan"} or option in seen or index + 1 >= len(command):
            raise LauncherError("saved Bello command contains unsupported options")
        seen.add(option)
        label = option.removeprefix("--")
        expected = _resolve_input_file(project, command[index + 1], label)
        if str(expected) != command[index + 1]:
            raise LauncherError(f"saved Bello {label} path changed")
        index += 2
    return command


def _create_lock(path: Path, token: str) -> None:
    _safe_regular_file(path, "Bello launcher lock")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise LauncherError(
            "another Bello launch owns .codex/bello-run/active.lock; inspect status before recovery"
        ) from exc
    try:
        os.write(fd, token.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _release_lock(path: Path, token: str) -> None:
    try:
        _safe_regular_file(path, "Bello launcher lock")
        current = path.read_text(encoding="ascii")
        if hmac.compare_digest(current, token):
            path.unlink()
    except (FileNotFoundError, OSError, UnicodeDecodeError, LauncherError):
        return


def _open_log(path: Path):
    _safe_regular_file(path, path.name)
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    return os.fdopen(fd, "wb")


def _pid_alive(value: Any) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except (OSError, ValueError):
        return False
    return True


def _tail(path: Path) -> str:
    _safe_regular_file(path, path.name)
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - TAIL_BYTES))
            raw = stream.read(TAIL_BYTES)
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-TAIL_LINES:])


def _supervisor_summary(project: Path) -> dict[str, Any]:
    config = _read_json(project / ".supervisor" / "config.json")
    if config is None:
        return {}
    allowed = (
        "status",
        "task_path",
        "generation",
        "restart_count",
        "coder_model",
        "runtime_model",
        "completion_model",
        "adversary_model",
        "coder_intelligence",
        "runtime_intelligence",
        "completion_intelligence",
        "adversary_intelligence",
    )
    return {key: config[key] for key in allowed if key in config}


def _supervisor_directory(project: Path) -> Path:
    directory = project / ".supervisor"
    _ordinary_directory(directory, "Bello supervisor state directory")
    return directory


def _git_status(project: Path) -> str:
    git = _resolve_scoped_executable("git", project)
    if git is None:
        return ""
    try:
        result = subprocess.run(
            [str(git), "-C", str(project), "status", "--short", "--untracked-files=normal"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout[-TAIL_BYTES:].decode("utf-8", errors="replace")


def status(project_value: str | os.PathLike[str] = ".") -> dict[str, Any]:
    project = _project_path(project_value)
    run_dir = _run_directory(project, create=False)
    supervisor_dir = _supervisor_directory(project)
    state = _read_json(run_dir / STATE_NAME) if run_dir.exists() else None
    raw_status = state.get("status") if state is not None else "not_launched"
    launcher_alive = _pid_alive(state.get("launcherPid")) if state else False
    child_alive = _pid_alive(state.get("pid")) if state else False
    effective = raw_status if isinstance(raw_status, str) else "invalid"
    if effective == "launching" and not launcher_alive:
        effective = "stale"
    elif effective == "running" and not (launcher_alive or child_alive):
        effective = "stale"

    public_state = {}
    if state is not None:
        for key in (
            "command",
            "executable",
            "launcherPid",
            "pid",
            "startedAt",
            "finishedAt",
            "exitCode",
            "error",
        ):
            if key in state:
                public_state[key] = state[key]
    public_state.update(
        {
            "status": effective,
            # A PID can be reused. These are liveness hints, never durable run identity.
            "launcherProcessAliveUnverified": launcher_alive,
            "belloProcessAliveUnverified": child_alive,
        }
    )
    artifacts = {
        "progress": _tail(supervisor_dir / "PROGRESS.md"),
        "decisions": _tail(supervisor_dir / "DECISIONS.md"),
        "finalReport": _tail(supervisor_dir / "FINAL_REPORT.md"),
        "stdout": _tail(run_dir / STDOUT_NAME) if run_dir.exists() else "",
        "stderr": _tail(run_dir / STDERR_NAME) if run_dir.exists() else "",
    }
    return {
        "project": str(project),
        "runDirectory": str(run_dir),
        "launcher": public_state,
        "supervisor": _supervisor_summary(project),
        "artifacts": artifacts,
        "gitStatus": _git_status(project),
    }


def _background_flags() -> dict[str, Any]:
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        return {"creationflags": flags}
    return {"start_new_session": True}


def start(
    project_value: str | os.PathLike[str] = ".",
    *,
    task: str | None = None,
    plan: str | None = None,
) -> dict[str, Any]:
    project = _project_path(project_value)
    run_dir = _run_directory(project, create=True)
    lock_path = run_dir / LOCK_NAME
    if lock_path.exists():
        current = status(project)
        current["duplicateRejected"] = True
        if current["launcher"]["status"] in ACTIVE_STATES:
            return current
        raise LauncherError(
            "a stale Bello launcher lock exists; inspect the recorded state before removing "
            ".codex/bello-run/active.lock"
        )

    command = _build_bello_command(project, task=task, plan=plan)
    token = secrets.token_hex(32)
    _create_lock(lock_path, token)
    launch = {
        "version": 1,
        "project": str(project),
        "command": command,
        "executable": command[0],
        "token": token,
        "requestedAt": _now(),
    }
    try:
        _atomic_json(run_dir / LAUNCH_NAME, launch)
        _atomic_json(
            run_dir / STATE_NAME,
            {
                "version": 1,
                "status": "launching",
                "project": str(project),
                "command": command,
                "executable": command[0],
                "startedAt": launch["requestedAt"],
            },
        )
        script = Path(__file__).resolve(strict=True)
        runner_command = [
            sys.executable,
            str(script),
            "_run",
            "--project",
            str(project),
            "--token",
            token,
        ]
        runner = subprocess.Popen(
            runner_command,
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_background_flags(),
        )
        launching = _read_json(run_dir / STATE_NAME) or {}
        launching["launcherPid"] = runner.pid
        _atomic_json(run_dir / STATE_NAME, launching)
    except BaseException:
        _release_lock(lock_path, token)
        raise

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        observed = _read_json(run_dir / STATE_NAME) or {}
        if observed.get("status") != "launching" or not _pid_alive(runner.pid):
            break
        time.sleep(0.05)
    return status(project)


def _run_worker(project_value: str, token: str) -> int:
    project = _project_path(project_value)
    run_dir = _run_directory(project, create=False)
    lock_path = run_dir / LOCK_NAME
    launch = _read_json(run_dir / LAUNCH_NAME)
    if launch is None or not isinstance(launch.get("token"), str):
        raise LauncherError("missing Bello launch record")
    if not hmac.compare_digest(launch["token"], token):
        raise LauncherError("Bello launch token does not match the active record")
    if launch.get("project") != str(project):
        raise LauncherError("Bello launch project does not match the active record")
    state = {
        "version": 1,
        "status": "launching",
        "project": str(project),
        "launcherPid": os.getpid(),
        "startedAt": launch.get("requestedAt", _now()),
    }
    try:
        command = _validate_saved_command(project, launch)
        state.update(command=command, executable=command[0])
        with _open_log(run_dir / STDOUT_NAME) as stdout, _open_log(run_dir / STDERR_NAME) as stderr:
            process = subprocess.Popen(
                command,
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
            )
            state.update(status="running", pid=process.pid)
            _atomic_json(run_dir / STATE_NAME, state)
            return_code = process.wait()
        state.update(status="exited", exitCode=return_code, finishedAt=_now())
        _atomic_json(run_dir / STATE_NAME, state)
        return return_code
    except BaseException as exc:
        state.update(
            status="launch_failed",
            error=f"{type(exc).__name__}: {exc}",
            finishedAt=_now(),
        )
        try:
            _atomic_json(run_dir / STATE_NAME, state)
        except Exception:
            pass
        return 1
    finally:
        _release_lock(lock_path, token)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--project", default=".")
        if name == "start":
            command.add_argument("--task")
            command.add_argument("--plan")
    worker = subparsers.add_parser("_run", help=argparse.SUPPRESS)
    worker.add_argument("--project", required=True)
    worker.add_argument("--token", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "_run":
            return _run_worker(args.project, args.token)
        result = (
            start(args.project, task=args.task, plan=args.plan)
            if args.command == "start"
            else status(args.project)
        )
    except LauncherError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
