"""Bounded process crash recovery. This module never starts model work itself."""
from __future__ import annotations

import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Callable, Iterator

from supervisor.filesystem_safety import is_link_or_reparse

MAX_RESTARTS = 3
WORKER_ENV = "BELLO_WATCHDOG_WORKER"
EPOCH_ENV = "BELLO_WATCHDOG_FENCE_TOKEN"
PERMIT_ENV = "BELLO_RECOVERY_PERMIT"


def _root(project_root: Path) -> Path:
    from supervisor.controller_recovery import recovery_root
    return recovery_root(project_root)


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if is_link_or_reparse(path, stat_result=info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise RuntimeError(f"invalid watchdog authority file: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid watchdog authority object: {path.name}")
    return value


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _monitor_lock(project_root: Path) -> Iterator[Path]:
    """A second launcher fails before config creation, cleanup, or state writes."""
    from supervisor.controller_recovery import RunOwner
    with RunOwner(project_root):
        root = _root(project_root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "watchdog.lock"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if is_link_or_reparse(path, stat_result=info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("watchdog lock must be an unshared regular file")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise RuntimeError("another Bello watchdog already owns this project") from exc
    try:
        yield root
    finally:
        os.close(fd)


def validate_recovery_permit(
    project_root: Path, *, run_id: str, owner_pid: int, owner_epoch: str,
) -> None:
    """Validate a guardian receipt without changing preserved recovery state.

    The new owner's epoch checkpoint invalidates it before any provider work.
    """
    path = _root(project_root) / "fence-receipt.json"
    try:
        receipt = _read_json(path)
        expected = os.environ.pop(PERMIT_ENV, None)
        if (
            set(receipt) != {"version", "run_id", "owner_pid", "owner_epoch", "nonce", "scope"}
            or type(receipt["version"]) is not int or receipt["version"] != 1 or receipt["scope"] != "tree"
            or type(receipt["owner_pid"]) is not int
            or receipt["run_id"] != run_id or receipt["owner_pid"] != owner_pid
            or receipt["owner_epoch"] != owner_epoch
            or not isinstance(expected, str) or len(expected) != 64 or not expected.isascii()
            or not isinstance(receipt["nonce"], str) or not receipt["nonce"].isascii()
            or not secrets.compare_digest(receipt["nonce"], expected)
        ):
            raise RuntimeError("predecessor process fencing receipt does not match this run")
    except (OSError, ValueError, KeyError) as exc:
        raise RuntimeError("recovery requires a trusted predecessor process fencing receipt") from exc


def _reserve_restart(root: Path, run_id: str, maximum: int) -> int | None:
    path = root / "watchdog.json"
    record = _read_json(path) if path.exists() else {"version": 1, "run_id": run_id, "restarts": 0}
    if (
        set(record) != {"version", "run_id", "restarts"}
        or type(record["version"]) is not int or record["version"] != 1
        or not isinstance(record["run_id"], str)
        or type(record["restarts"]) is not int or record["restarts"] < 0
    ):
        raise RuntimeError("invalid persisted watchdog restart budget")
    if record["run_id"] != run_id:
        record = {"version": 1, "run_id": run_id, "restarts": 0}
    if record["restarts"] >= maximum:
        return None
    record["restarts"] += 1
    _atomic_json(path, record)
    return record["restarts"]


def _run_guarded(command: list[str], env: dict[str, str], *, cwd: Path) -> dict[str, Any]:
    secret = secrets.token_bytes(32)
    listener = Listener(("127.0.0.1", 0), authkey=secret)
    guardian_env = dict(env, **{
        "BELLO_GUARDIAN_ADDRESS": json.dumps(listener.address),
        "BELLO_GUARDIAN_SECRET": secret.hex(),
        "BELLO_GUARDIAN_COMMAND": json.dumps(command),
    })
    guardian: subprocess.Popen | None = None
    connection: Any = None
    interrupted = False
    old_handlers: dict[int, Any] = {}

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        if connection is not None:
            try:
                connection.send({"stop": True})
            except (OSError, EOFError):
                pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_stop)
        guardian = subprocess.Popen(
            [sys.executable, "-m", "supervisor.process_fence"], cwd=cwd, env=guardian_env,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        # A bounded accept prevents a broken guardian import from hanging CLI.
        listener._listener._socket.settimeout(15)
        connection = listener.accept()
        if not connection.poll(15):
            raise RuntimeError("process guardian did not start the controller")
        started = connection.recv()
        if not isinstance(started, dict) or type(started.get("started")) is not int:
            raise RuntimeError("invalid process guardian startup receipt")
        if interrupted:
            connection.send({"stop": True})
        while True:
            if connection.poll(0.1):
                result = connection.recv()
                break
            if guardian.poll() is not None:
                raise RuntimeError("process guardian exited without a fencing receipt")
        if not isinstance(result, dict):
            raise RuntimeError("invalid process guardian completion receipt")
        result["owner_pid"] = started["started"]
        result["stopped"] = interrupted
        return result
    except (EOFError, OSError) as exc:
        raise RuntimeError("process guardian connection was lost; recovery is blocked") from exc
    finally:
        if connection is not None:
            connection.close()  # EOF asks the surviving guardian to clean up.
        listener.close()
        try:
            if guardian is not None:
                guardian.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # Do not kill the cleanup authority or signal a PID loaded from disk.
            raise RuntimeError("process guardian cleanup timed out; recovery is blocked")
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


def watch_command(
    command: list[str], *, project_root: Path,
    disposition: Callable[[Path], dict[str, Any]] | None = None,
    maximum_restarts: int = MAX_RESTARTS, backoff: tuple[float, ...] = (1.0, 2.0, 4.0),
    report: Callable[[str], None] = print, explicit_recovery: bool = False,
    required_scope: str = "tree",
    recovery_command: list[str] | Callable[[dict[str, Any]], list[str]] | None = None,
) -> int:
    """Restart only a fenced, eligible crash; the budget belongs to the run UUID.

    ``required_scope='groups'`` exists for local fixtures which intentionally
    spawn no detached descendants. Production CLI always requires full trees.
    """
    from supervisor.controller_recovery import recovery_disposition
    classify = disposition or recovery_disposition
    with _monitor_lock(project_root) as root:
        permit = None
        state: dict[str, Any] = {}
        if explicit_recovery:
            state = classify(project_root)
            if not state.get("eligible"):
                raise RuntimeError("--recover requires an eligible interrupted run")
            try:
                saved = _read_json(root / "fence-receipt.json")
            except (OSError, ValueError) as exc:
                raise RuntimeError("--recover requires a trusted predecessor fencing receipt") from exc
            permit = saved.get("nonce")
            if (
                not isinstance(permit, str) or len(permit) != 64 or not permit.isascii()
                or saved.get("scope") != "tree"
                or any(saved.get(key) != state.get(key) for key in ("run_id", "owner_pid", "owner_epoch"))
            ):
                raise RuntimeError("--recover fencing receipt does not match the interrupted run")
        while True:
            epoch = secrets.token_hex(32)
            env = dict(os.environ, **{WORKER_ENV: "1", EPOCH_ENV: epoch})
            env.pop(PERMIT_ENV, None)
            if permit is not None:
                env[PERMIT_ENV] = permit
            selected_command = command
            if permit:
                selected_command = recovery_command(state) if callable(recovery_command) else (recovery_command or command)
            result = _run_guarded(selected_command, env, cwd=project_root)
            exit_code = result.get("exit_code", 125)
            if not result.get("fenced"):
                report("Bello stopped with a cleanup failure: owned processes could not all be confirmed stopped; recovery is blocked.")
                return 1
            if result.get("stopped") or exit_code in (0, 2, 130, 143, -signal.SIGINT, -signal.SIGTERM):
                return 130 if result.get("stopped") else (128 - exit_code if exit_code < 0 else exit_code)
            state = classify(project_root)
            if not state.get("eligible") or state.get("terminal"):
                report(f"Bello recovery stopped: {state.get('reason', 'the run is not recoverable')}.")
                return 1
            if (
                not result.get("fenced") or result.get("scope") != required_scope
                or state.get("owner_pid") != result.get("owner_pid")
                or state.get("owner_epoch") != epoch
            ):
                report("Bello recovery blocked: predecessor process-tree cleanup could not be proven on this host.")
                return 1
            run_id = state.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise RuntimeError("eligible recovery record has no run identity")
            retry = _reserve_restart(root, run_id, maximum_restarts)
            if retry is None:
                report(f"Bello recovery stopped: the run has used its {maximum_restarts} automatic restarts.")
                return 1
            permit = secrets.token_hex(32)
            _atomic_json(root / "fence-receipt.json", {
                "version": 1, "run_id": run_id, "owner_pid": result["owner_pid"],
                "owner_epoch": epoch, "nonce": permit, "scope": result["scope"],
            })
            delay = backoff[min(retry - 1, len(backoff) - 1)] if backoff else 0
            report(f"Bello controller crashed; restoring the same run ({retry}/{maximum_restarts}) in {delay:g}s.")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                return 130


def watch_cli(args: list[str], *, explicit_recovery: bool = False) -> int:
    command = [sys.executable, "-m", "supervisor.main", *args]
    def resume_command(state: dict[str, Any]) -> list[str]:
        result = [*command, "--start-over", "false", "--clean", "false"]
        if isinstance(state.get("task_path"), str):
            result.extend(("--task", state["task_path"]))
        if isinstance(state.get("plan_path"), str):
            result.extend(("--plan", state["plan_path"]))
        return result
    return watch_command(
        command, recovery_command=resume_command,
        project_root=Path.cwd(), explicit_recovery=explicit_recovery,
    )
