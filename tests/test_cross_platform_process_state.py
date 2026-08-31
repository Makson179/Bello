from __future__ import annotations

import asyncio
import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervisor.appserver as appserver_module
import supervisor.filesystem_safety as filesystem_safety_module
import supervisor.state as state_module
from supervisor.appserver import AppServerClient
from supervisor.state import FileLock, StateStore


def test_file_lock_uses_posix_flock_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    class FakeFcntl:
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(fd: int, operation: int) -> None:
            calls.append((fd, operation))

    monkeypatch.setattr(state_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(state_module, "_fcntl", FakeFcntl)

    lock = FileLock(tmp_path / "state.lock")
    with lock:
        assert lock.fd is not None
        acquired_fd = lock.fd

    assert calls == [(acquired_fd, FakeFcntl.LOCK_EX), (acquired_fd, FakeFcntl.LOCK_UN)]
    assert lock.fd is None
    with pytest.raises(OSError) as exc_info:
        os.fstat(acquired_fd)
    assert exc_info.value.errno == errno.EBADF


def test_file_lock_windows_branch_waits_for_contention_and_locks_sentinel_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2
        attempts = 0
        calls: list[tuple[int, int, int]] = []

        @classmethod
        def locking(cls, fd: int, operation: int, byte_count: int) -> None:
            cls.calls.append((operation, os.lseek(fd, 0, os.SEEK_CUR), byte_count))
            if operation == cls.LK_NBLCK:
                cls.attempts += 1
                if cls.attempts < 3:
                    raise PermissionError(errno.EACCES, "lock is held")

    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module, "_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(state_module.time, "sleep", sleeps.append)

    path = tmp_path / "state.lock"
    with FileLock(path):
        assert path.read_bytes() == b"\0"

    assert sleeps == [state_module._WINDOWS_LOCK_POLL_SECONDS] * 2
    assert FakeMsvcrt.calls == [
        (FakeMsvcrt.LK_NBLCK, 0, 1),
        (FakeMsvcrt.LK_NBLCK, 0, 1),
        (FakeMsvcrt.LK_NBLCK, 0, 1),
        (FakeMsvcrt.LK_UNLCK, 0, 1),
    ]


def test_file_lock_windows_branch_retries_sentinel_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.lock"
    sleeps: list[float] = []
    write_attempts = 0
    real_write = os.write

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2
        calls: list[tuple[int, int, int]] = []

        @classmethod
        def locking(cls, fd: int, operation: int, byte_count: int) -> None:
            cls.calls.append((operation, os.lseek(fd, 0, os.SEEK_CUR), byte_count))

    def racing_write(fd: int, data: bytes) -> int:
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            # Model another creator winning the race between this process's
            # size check and sentinel write, then locking the sentinel byte.
            other_fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                assert real_write(other_fd, b"\0") == 1
            finally:
                os.close(other_fd)
            error = PermissionError(errno.EACCES, "byte is locked")
            error.winerror = 33  # type: ignore[attr-defined]
            raise error
        return real_write(fd, data)

    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module, "_msvcrt", FakeMsvcrt)
    monkeypatch.setattr(state_module.os, "write", racing_write)
    monkeypatch.setattr(state_module.time, "sleep", sleeps.append)

    with FileLock(path):
        assert path.read_bytes() == b"\0"

    assert write_attempts == 1
    assert sleeps == [state_module._WINDOWS_LOCK_POLL_SECONDS]
    assert FakeMsvcrt.calls == [
        (FakeMsvcrt.LK_NBLCK, 0, 1),
        (FakeMsvcrt.LK_UNLCK, 0, 1),
    ]


def test_file_lock_windows_branch_fails_closed_for_non_contention_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted_fd: int | None = None

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd: int, operation: int, byte_count: int) -> None:
            nonlocal attempted_fd
            attempted_fd = fd
            raise PermissionError(errno.EPERM, "locking is not permitted")

    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module, "_msvcrt", FakeMsvcrt)

    with pytest.raises(PermissionError, match="locking is not permitted"):
        with FileLock(tmp_path / "state.lock"):
            pass

    assert attempted_fd is not None
    with pytest.raises(OSError) as exc_info:
        os.fstat(attempted_fd)
    assert exc_info.value.errno == errno.EBADF


def test_windows_atomic_replace_retries_transient_sharing_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new\n", encoding="utf-8")
    destination.write_text("old\n", encoding="utf-8")
    real_replace = os.replace
    attempts = 0
    sleeps: list[float] = []

    def flaky_replace(raw_source: str, raw_destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError(errno.EACCES, "sharing violation")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        real_replace(raw_source, raw_destination)

    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module.os, "replace", flaky_replace)
    monkeypatch.setattr(state_module.time, "sleep", sleeps.append)

    state_module._atomic_replace(str(source), destination)

    assert attempts == 3
    assert sleeps == [state_module._WINDOWS_REPLACE_POLL_SECONDS] * 2
    assert destination.read_text(encoding="utf-8") == "new\n"


def test_windows_atomic_replace_does_not_retry_non_sharing_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmp"
    destination = tmp_path / "state.json"
    source.write_text("new\n", encoding="utf-8")
    sleeps: list[float] = []

    def denied_replace(raw_source: str, raw_destination: Path) -> None:
        raise PermissionError(errno.EPERM, "policy denied")

    monkeypatch.setattr(state_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(state_module.os, "replace", denied_replace)
    monkeypatch.setattr(state_module.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError, match="policy denied"):
        state_module._atomic_replace(str(source), destination)

    assert sleeps == []


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows lock semantics")
def test_windows_file_lock_is_released_after_owner_process_dies(tmp_path: Path) -> None:
    lock_path = tmp_path / "owner-death.lock"
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(repository_root) + (
        os.pathsep + existing_python_path if existing_python_path else ""
    )
    owner_script = """
import sys
from pathlib import Path
from supervisor.state import FileLock

with FileLock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""
    contender_script = """
import sys
from pathlib import Path
from supervisor.state import FileLock

with FileLock(Path(sys.argv[1])):
    print("reacquired", flush=True)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, str(lock_path)],
        cwd=repository_root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "locked"
        owner.kill()
        owner.wait(timeout=10)

        contender = subprocess.run(
            [sys.executable, "-c", contender_script, str(lock_path)],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip() == "reacquired"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def test_state_updates_are_serialized_between_processes(store: StateStore, tmp_path: Path) -> None:
    gate = tmp_path / "start-workers"
    worker = """
import sys
import time
from pathlib import Path

from supervisor.state import StateStore

workspace = Path(sys.argv[1])
gate = Path(sys.argv[2])
iterations = int(sys.argv[3])
while not gate.exists():
    time.sleep(0.005)
state = StateStore(workspace)
for _ in range(iterations):
    state.patch_health(
        lambda health: health.model_copy(
            update={"interventions": health.interventions + 1}
        )
    )
"""
    environment = os.environ.copy()
    repository_root = Path(__file__).resolve().parents[1]
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(repository_root) + (
        os.pathsep + existing_python_path if existing_python_path else ""
    )
    worker_count = 4
    iterations = 25
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(tmp_path), str(gate), str(iterations)],
            cwd=repository_root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(worker_count)
    ]
    gate.write_text("go\n", encoding="utf-8")

    results: list[tuple[int, str, str]] = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()

    assert results == [(0, "", "")] * worker_count
    assert store.get_health().interventions == worker_count * iterations


def test_state_clear_treats_reparse_directory_as_leaf(
    store: StateStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reparse_leaf = store.state_dir / "junction"
    reparse_leaf.mkdir()
    (reparse_leaf / "target-data.txt").write_text("keep\n", encoding="utf-8")
    removed: list[Path] = []

    monkeypatch.setattr(
        filesystem_safety_module,
        "is_reparse_point",
        lambda path, *, stat_result=None: path == reparse_leaf,
    )
    monkeypatch.setattr(
        filesystem_safety_module,
        "remove_link_or_reparse",
        lambda path, *, stat_result=None: removed.append(path),
    )

    store._clear_state_dir(preserve=set())

    assert removed == [reparse_leaf]
    assert (reparse_leaf / "target-data.txt").read_text(encoding="utf-8") == "keep\n"


def test_state_clear_removes_nested_read_only_tree(store: StateStore) -> None:
    readonly_tree = store.state_dir / "read-only"
    nested = readonly_tree / "nested"
    nested.mkdir(parents=True)
    payload = nested / "state.json"
    payload.write_text('{"state": "stale"}\n', encoding="utf-8")
    payload.chmod(0o444)
    nested.chmod(0o555)
    readonly_tree.chmod(0o555)

    try:
        store._clear_state_dir(preserve=set())
    finally:
        # Keep pytest cleanup reliable if the assertion path itself fails.
        if payload.exists():
            payload.chmod(0o600)
        if nested.exists():
            nested.chmod(0o700)
        if readonly_tree.exists():
            readonly_tree.chmod(0o700)

    assert not readonly_tree.exists()


def test_state_clear_unlinks_directory_symlink_without_touching_target(
    store: StateStore,
    tmp_path: Path,
) -> None:
    target = tmp_path / "symlink-target"
    target.mkdir()
    payload = target / "keep.txt"
    payload.write_text("keep\n", encoding="utf-8")
    link = store.state_dir / "linked-directory"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"could not create test directory symlink: {exc}")

    store._clear_state_dir(preserve=set())

    assert not link.exists()
    assert payload.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(os.name != "nt", reason="requires a native Windows junction")
def test_state_clear_removes_windows_junction_without_touching_target(
    store: StateStore,
    tmp_path: Path,
) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    payload = target / "keep.txt"
    payload.write_text("keep\n", encoding="utf-8")
    junction = store.state_dir / "junction"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create test junction: {result.stderr or result.stdout}")

    store._clear_state_dir(preserve=set())

    assert not junction.exists()
    assert payload.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    ("is_windows", "expected"),
    [
        (False, {"start_new_session": True}),
        (
            True,
            {
                "creationflags": appserver_module._WINDOWS_CREATE_NEW_PROCESS_GROUP
                | appserver_module._WINDOWS_CREATE_SUSPENDED
            },
        ),
    ],
)
def test_app_server_process_creation_uses_platform_process_group(
    is_windows: bool,
    expected: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", is_windows)

    assert appserver_module._app_server_process_kwargs() == expected


def test_windows_app_server_resolves_cmd_launcher_with_pathext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launcher = tmp_path / "trusted-bin" / "codex.CMD"
    launcher.parent.mkdir()
    launcher.write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    assert appserver_module._app_server_command(
        ["codex", "app-server"],
        cwd=workspace,
        environ={"PATH": str(launcher.parent), "PATHEXT": ".CMD;.EXE"},
    ) == [
        str(launcher.resolve()),
        "app-server",
    ]


def test_posix_app_server_preserves_command_without_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", False)
    monkeypatch.setattr(
        appserver_module.shutil,
        "which",
        lambda executable: pytest.fail("POSIX command resolution must remain unchanged"),
    )

    assert appserver_module._app_server_command(["codex", "app-server"]) == [
        "codex",
        "app-server",
    ]


class _BlockingReader:
    async def readline(self) -> bytes:
        await asyncio.Event().wait()
        return b""


async def test_windows_app_server_start_and_graceful_stop_use_group_and_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4100
        returncode: int | None = None
        stdin = object()
        stdout = _BlockingReader()
        stderr = _BlockingReader()

        def __init__(self) -> None:
            self.signals: list[int] = []

        def send_signal(self, sent_signal: int) -> None:
            self.signals.append(sent_signal)

        def terminate(self) -> None:
            raise AssertionError("CTRL_BREAK_EVENT should be the graceful path")

        def kill(self) -> None:
            raise AssertionError("the Job Object should own forced cleanup")

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    class FakeJob:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    process = FakeProcess()
    job = FakeJob()
    creation: dict[str, object] = {}
    resumed: list[int] = []

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        creation["command"] = command
        creation["kwargs"] = kwargs
        return process

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "require_trusted_executable",
        lambda *args, **kwargs: r"C:\trusted\codex.CMD",
    )
    monkeypatch.setattr(appserver_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        appserver_module,
        "_WindowsKillJob",
        SimpleNamespace(create=lambda pid: job),
    )
    monkeypatch.setattr(appserver_module, "_resume_windows_process", resumed.append)
    monkeypatch.setattr(
        appserver_module,
        "_app_server_environment",
        lambda: {"CODEX_HOME": str(tmp_path / "missing-codex-home")},
    )
    client = AppServerClient(command=["codex", "app-server"])

    await client.start()
    await client.stop()

    kwargs = creation["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == (
        appserver_module._WINDOWS_CREATE_NEW_PROCESS_GROUP
        | appserver_module._WINDOWS_CREATE_SUSPENDED
    )
    assert "start_new_session" not in kwargs
    assert resumed == [process.pid]
    assert process.signals == [appserver_module._WINDOWS_CTRL_BREAK_EVENT]
    assert job.closed is True
    assert client.process is None


async def test_windows_app_server_stop_forces_job_after_grace_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_exited = asyncio.Event()

    class HangingProcess:
        pid = 4101
        returncode: int | None = None

        def __init__(self) -> None:
            self.signals: list[int] = []

        def send_signal(self, sent_signal: int) -> None:
            self.signals.append(sent_signal)

        def terminate(self) -> None:
            raise AssertionError("CTRL_BREAK_EVENT should be attempted first")

        def kill(self) -> None:
            raise AssertionError("the Job Object should kill the process tree")

        async def wait(self) -> int:
            await process_exited.wait()
            assert self.returncode is not None
            return self.returncode

    process = HangingProcess()

    class FakeJob:
        closed = False

        def close(self) -> None:
            self.closed = True
            process.returncode = -1
            process_exited.set()

    job = FakeJob()
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(appserver_module, "APP_SERVER_PROCESS_EXIT_TIMEOUT_SECONDS", 0.01)
    client = AppServerClient()
    client.process = process  # type: ignore[assignment]
    client._windows_job = job  # type: ignore[assignment]

    await client.stop()

    assert process.signals == [appserver_module._WINDOWS_CTRL_BREAK_EVENT]
    assert job.closed is True
    assert client.process is None


async def test_windows_app_server_start_failure_force_cleans_spawned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4102
        returncode: int | None = None
        stdin = object()
        stdout = _BlockingReader()
        stderr = _BlockingReader()

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()
    cleaned: list[int] = []

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        return process

    def fail_job_assignment(pid: int) -> None:
        raise OSError(errno.EACCES, "job assignment denied")

    def fake_force_cleanup(spawned: FakeProcess) -> None:
        cleaned.append(spawned.pid)
        spawned.returncode = -1

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "require_trusted_executable",
        lambda *args, **kwargs: r"C:\trusted\codex.CMD",
    )
    monkeypatch.setattr(appserver_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        appserver_module,
        "_WindowsKillJob",
        SimpleNamespace(create=fail_job_assignment),
    )
    monkeypatch.setattr(
        appserver_module,
        "_app_server_environment",
        lambda: {"CODEX_HOME": str(tmp_path / "missing-codex-home")},
    )
    client = AppServerClient(command=["codex", "app-server"])
    monkeypatch.setattr(client, "_force_windows_process_tree_without_job", fake_force_cleanup)

    with pytest.raises(OSError, match="job assignment denied"):
        await client.start()

    assert cleaned == [process.pid]
    assert client.process is None


async def test_windows_app_server_resume_failure_closes_assigned_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4103
        returncode: int | None = None
        stdin = object()
        stdout = _BlockingReader()
        stderr = _BlockingReader()

        async def wait(self) -> int:
            assert self.returncode is not None
            return self.returncode

    process = FakeProcess()

    class FakeJob:
        closed = False

        def close(self) -> None:
            self.closed = True
            process.returncode = -1

    job = FakeJob()

    async def fake_create_subprocess_exec(*command: str, **kwargs: object) -> FakeProcess:
        return process

    def fail_resume(pid: int) -> None:
        raise OSError(errno.EACCES, "thread resume denied")

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "require_trusted_executable",
        lambda *args, **kwargs: r"C:\trusted\codex.CMD",
    )
    monkeypatch.setattr(appserver_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        appserver_module,
        "_WindowsKillJob",
        SimpleNamespace(create=lambda pid: job),
    )
    monkeypatch.setattr(appserver_module, "_resume_windows_process", fail_resume)
    monkeypatch.setattr(
        appserver_module,
        "_app_server_environment",
        lambda: {"CODEX_HOME": str(tmp_path / "missing-codex-home")},
    )
    client = AppServerClient(command=["codex", "app-server"])

    with pytest.raises(OSError, match="thread resume denied"):
        await client.start()

    assert job.closed is True
    assert client.process is None


async def test_app_server_stop_kills_descendant_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_marker = tmp_path / "child.pid"
    child_script = tmp_path / "appserver-child.py"
    child_script.write_text(
        """
import os
import signal
import sys
import time
from pathlib import Path

if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    parent_script = tmp_path / "appserver-parent.py"
    parent_script.write_text(
        """
import subprocess
import sys
import time

subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2]],
    close_fds=True,
)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        appserver_module,
        "_app_server_environment",
        lambda: {"CODEX_HOME": str(tmp_path / "missing-codex-home")},
    )
    # This test targets descendant cleanup, not executable trust.  Hosted
    # Windows runners may expose sys.executable through runner-managed reparse
    # points that the production Codex resolver intentionally rejects.
    monkeypatch.setattr(
        appserver_module,
        "_app_server_command",
        lambda command, **kwargs: command,
    )
    client = AppServerClient(
        command=[sys.executable, str(parent_script), str(child_script), str(child_marker)]
    )
    child_pid: int | None = None

    await client.start()
    try:
        await _wait_for_path(child_marker)
        child_pid = int(child_marker.read_text(encoding="utf-8"))
        assert _process_is_running(child_pid)
    finally:
        await client.stop()

    assert child_pid is not None
    await _wait_for_process_exit(child_pid)


async def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        await asyncio.sleep(0.02)


async def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while _process_is_running(pid):
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"descendant process {pid} survived AppServerClient.stop()")
        await asyncio.sleep(0.02)


def _process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.exists():
            try:
                fields = proc_stat.read_text(encoding="utf-8").split()
            except OSError:
                return False
            if len(fields) > 2 and fields[2] == "Z":
                return False
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)
