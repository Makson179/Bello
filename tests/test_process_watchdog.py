from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import weakref

import pytest

from supervisor import process_fence, watchdog
from supervisor.controller_recovery import RunOwner, recovery_root

SOURCE = Path(__file__).resolve().parents[1]
WORKER = SOURCE / "tests/support/watchdog_worker.py"


@pytest.fixture(autouse=True)
def source_imports(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(SOURCE))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def classify(root):
    return json.loads((root / "fixture-state.json").read_text())


def command(root, mode):
    return [sys.executable, str(WORKER), str(root), mode]


def fixture_scope():
    return "groups" if sys.platform == "darwin" else "tree"


def live(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    result = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def await_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"fixture did not create {path.name}"


def test_actual_crash_restarts_same_fixture_identity_and_retains_budget(tmp_path):
    messages = []
    result = watchdog.watch_command(command(tmp_path, "once"), project_root=tmp_path,
        disposition=classify, backoff=(), required_scope=fixture_scope(), report=messages.append)
    assert result == 0
    record = classify(tmp_path)
    assert record["attempt"] == 2
    assert record["thread_id"] == "same-fixture-thread"
    assert record["budget_consumed"] == 2
    assert json.loads((recovery_root(tmp_path) / "watchdog.json").read_text())["restarts"] == 1
    assert any("restoring the same run" in message for message in messages)


def test_actual_repeated_crash_stops_at_persisted_run_limit(tmp_path):
    options = dict(project_root=tmp_path, disposition=classify, backoff=(), required_scope=fixture_scope(),
                   maximum_restarts=2, report=lambda _: None)
    assert watchdog.watch_command(command(tmp_path, "always"), **options) == 1
    assert classify(tmp_path)["attempt"] == 3
    # A new monitor cannot replenish the already consumed logical-run budget.
    # The fixture's receipt check expects recovery env; reset its invocation
    # counter only, preserving its logical run identity and watchdog budget.
    state = classify(tmp_path)
    state["attempt"] = 0
    (tmp_path / "fixture-state.json").write_text(json.dumps(state))
    assert watchdog.watch_command(command(tmp_path, "always"), **options) == 1
    assert classify(tmp_path)["attempt"] == 1
    assert json.loads((recovery_root(tmp_path) / "watchdog.json").read_text())["restarts"] == 2


@pytest.mark.parametrize("mode,expected", [("success", 0), ("provider", 2), ("terminal", 1), ("blocked", 1), ("interrupt", 130)])
def test_normal_terminal_provider_and_user_stop_never_restart(tmp_path, mode, expected):
    assert watchdog.watch_command(command(tmp_path, mode), project_root=tmp_path,
        disposition=classify, backoff=(), required_scope=fixture_scope(), report=lambda _: None) == expected
    assert classify(tmp_path)["attempt"] == 1
    assert not (recovery_root(tmp_path) / "watchdog.json").exists()


def test_windows_guardian_uses_unexported_win32_suspend_flag_before_job_assignment(monkeypatch):
    """Exercise the Windows guardian branch without inventing subprocess flags."""
    import threading
    from types import SimpleNamespace
    from supervisor import appserver

    events, messages = [], []
    command = ["fixture-python", "fixture-worker"]
    environment = {
        "BELLO_GUARDIAN_ADDRESS": json.dumps(["127.0.0.1", 1234]),
        "BELLO_GUARDIAN_SECRET": "00" * 32,
        "BELLO_GUARDIAN_COMMAND": json.dumps(command),
    }
    worker = SimpleNamespace(pid=4321, returncode=0, poll=lambda: 0,
                             wait=lambda **kwargs: events.append("wait-worker"))

    def spawn(actual_command, *, env, creationflags):
        assert actual_command == command
        assert creationflags == 0x00000004
        assert "BELLO_GUARDIAN_SECRET" not in env
        events.append("spawn-suspended")
        return worker

    job = SimpleNamespace(close=lambda: events.append("close-job"))

    class Job:
        @classmethod
        def create(cls, pid):
            assert pid == worker.pid and events == ["spawn-suspended"]
            events.append("assign-job")
            return job

    def resume(pid):
        assert pid == worker.pid and events == ["spawn-suspended", "assign-job"]
        events.append("resume-worker")

    def terminate(owned):
        assert owned is job
        events.append("fence-job")
        return True

    control = SimpleNamespace(send=messages.append, close=lambda: events.append("close-control"))
    listener = SimpleNamespace(address=("127.0.0.1", 4321), close=lambda: events.append("close-listener"))
    monkeypatch.setattr(process_fence, "os", SimpleNamespace(name="nt", environ=environment))
    # CPython exposes neither CREATE_SUSPENDED nor POSIX process-group APIs on
    # this minimal Windows surface. The production fallback must supply 0x4.
    monkeypatch.setattr(process_fence, "subprocess", SimpleNamespace(Popen=spawn))
    monkeypatch.setattr(process_fence, "Client", lambda *args, **kwargs: control)
    monkeypatch.setattr(process_fence, "Listener", lambda *args, **kwargs: listener)
    monkeypatch.setattr(process_fence, "_enable_linux_subreaper", lambda: False)
    monkeypatch.setattr(process_fence, "_terminate_windows_job", terminate)
    monkeypatch.setattr(process_fence, "threading", SimpleNamespace(Event=threading.Event, Lock=threading.Lock,
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: events.append("serve-worker"))))
    monkeypatch.setattr(appserver, "_WindowsKillJob", Job)
    monkeypatch.setattr(appserver, "_resume_windows_process", resume)

    assert process_fence.guardian_main() == 0
    assert events == ["spawn-suspended", "assign-job", "resume-worker", "serve-worker",
                      "fence-job", "close-job", "wait-worker", "close-listener", "close-control"]
    assert messages == [{"started": 4321}, {"exit_code": 0, "fenced": True, "scope": "tree"}]


def test_worker_crash_kills_separate_backend_group_before_restart(tmp_path):
    assert watchdog.watch_command(command(tmp_path, "child"), project_root=tmp_path,
        disposition=classify, backoff=(), required_scope=fixture_scope(), report=lambda _: None) == 0
    assert not live(int((tmp_path / "child.pid").read_text()))


@pytest.mark.skipif(sys.platform == "darwin", reason="Darwin has no proven arbitrary descendant containment")
def test_tree_fence_includes_native_grandchild_new_session(tmp_path):
    assert watchdog.watch_command(command(tmp_path, "detached"), project_root=tmp_path,
        disposition=classify, backoff=(), report=lambda _: None) == 0
    assert not live(int((tmp_path / "child.pid").read_text()))


def test_watchdog_death_leaves_guardian_to_kill_worker_and_backend(tmp_path):
    script = (
        "from pathlib import Path; from supervisor.watchdog import watch_command; "
        f"watch_command({command(tmp_path, 'wait')!r},project_root=Path({str(tmp_path)!r}))"
    )
    monitor = subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy())
    try:
        await_file(tmp_path / "ready")
        worker = classify(tmp_path)["owner_pid"]
        child = int((tmp_path / "child.pid").read_text())
        monitor.kill()
        monitor.wait(timeout=5)
        deadline = time.monotonic() + 10
        while (live(worker) or live(child)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not live(worker) and not live(child)
    finally:
        if monitor.poll() is None:
            monitor.terminate()
            monitor.wait(timeout=15)


@pytest.mark.skipif(os.name == "nt", reason="POSIX console signal test")
def test_user_interrupt_runs_controller_cleanup_before_fencing(tmp_path):
    script = (
        "from pathlib import Path; from supervisor.watchdog import watch_command; "
        f"raise SystemExit(watch_command({command(tmp_path, 'wait')!r},project_root=Path({str(tmp_path)!r})))"
    )
    monitor = subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy())
    try:
        await_file(tmp_path / "ready")
        monitor.send_signal(signal.SIGINT)
        assert monitor.wait(timeout=20) == 130
        assert (tmp_path / "graceful-stop").read_text() == "controller cleanup completed"
        assert classify(tmp_path)["attempt"] == 1
        assert not live(int((tmp_path / "child.pid").read_text()))
    finally:
        if monitor.poll() is None:
            monitor.terminate()
            monitor.wait(timeout=15)


def test_watchdog_rejects_shared_authority_files(tmp_path):
    with RunOwner(tmp_path):
        source = recovery_root(tmp_path) / "source.json"
        source.write_text("{}")
        linked = recovery_root(tmp_path) / "watchdog.json"
        os.link(source, linked)
        with pytest.raises(RuntimeError, match="invalid watchdog authority"):
            watchdog._read_json(linked)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin-specific conservative fence scope")
def test_darwin_production_default_blocks_unproven_native_tree(tmp_path):
    messages = []
    assert watchdog.watch_command(command(tmp_path, "once"), project_root=tmp_path,
        disposition=classify, backoff=(), report=messages.append) == 1
    assert classify(tmp_path)["attempt"] == 1
    assert "process-tree cleanup could not be proven" in messages[-1]


def test_real_controller_through_native_guardian_resumes_or_fails_closed(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "TASK.md").write_text("Set VALUE to 43.\n")
    (project / "solution.py").write_text("VALUE = 0\n")
    evidence = tmp_path / "provider-events.jsonl"
    messages = []
    result = watchdog.watch_command(
        [sys.executable, "-B", "-m", "tests.support.controller_recovery_process", str(project), str(evidence)],
        project_root=project, backoff=(), report=messages.append,
    )
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    before = next(row for row in rows if row["kind"] == "crash_checkpoint")
    record = json.loads((recovery_root(project) / "run.json").read_text())
    if sys.platform == "darwin":
        assert result == 1
        assert not any(row["kind"] == "thread_resumed" for row in rows)
        assert "process-tree cleanup could not be proven" in messages[-1]
        assert (project / "solution.py").read_text() == "VALUE = 0\n"
        # This fixture starts no external tools; remove only its verified,
        # trusted disposable snapshot after the refusal has been asserted.
        from supervisor.snapshot_recovery import restore_snapshot_authority
        restore_snapshot_authority(
            recovery_root(project) / "snapshot.json", run_id=record["run_id"],
            expected_digest=record["snapshot_digest"], project_root=project,
        ).cleanup()
    else:
        assert result == 0
        after = next(row for row in rows if row["kind"] == "restored_evidence")
        for key in ("run_id", "snapshot", "generation", "restarts", "sequence"):
            assert before[key] == after[key]
        assert before["pid"] != after["pid"]
        assert sum(row["kind"] == "continuation_turn" for row in rows) == 1
        assert (project / "solution.py").read_text() == "VALUE = 43\n"
        assert record["terminal"] and not record["eligible"]


def test_group_mapping_uses_live_process_object_not_reusable_pid():
    class Process:
        pid = 42
    old, replacement = Process(), Process()
    process_fence._groups[old] = 123
    assert process_fence.process_group_id(old) == 123
    assert process_fence.process_group_id(replacement) == 42
    reference = weakref.ref(old)
    del old
    assert reference() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX group-lease lifecycle; Windows uses Job handles")
def test_completed_group_leases_are_reaped_and_late_signals_are_safe(tmp_path):
    assert watchdog.watch_command(command(tmp_path, "leases"), project_root=tmp_path,
        disposition=classify, backoff=(), report=lambda _: None) == 0
    evidence = json.loads((tmp_path / "lease-evidence.json").read_text())
    assert evidence == {"guardian_children": 2, "zombies": 0}


@pytest.mark.skipif(os.name == "nt", reason="POSIX group lease protocol")
def test_reused_numeric_group_does_not_change_old_lease(monkeypatch):
    messages = []
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: True)
    monkeypatch.setattr(process_fence, "_request", lambda message: messages.append(message) or {"ok": True})
    old = process_fence._GroupHandle(123, "old-lease")
    replacement = process_fence._GroupHandle(123, "new-lease")
    process_fence.signal_process_group(old, signal.SIGKILL)
    process_fence.signal_process_group(replacement, signal.SIGTERM)
    assert [message["lease"] for message in messages] == ["old-lease", "new-lease"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX group lease protocol")
def test_lost_guardian_cannot_fall_back_to_signalling_old_numeric_group(monkeypatch):
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: False)
    monkeypatch.setattr(process_fence.os, "killpg", lambda *_: pytest.fail("numeric PID fallback"))
    with pytest.raises(RuntimeError, match="lost its guardian"):
        process_fence.signal_process_group(process_fence._GroupHandle(123, "old"), signal.SIGKILL)


def test_linux_reused_child_pid_is_rejected_before_any_signal(monkeypatch):
    from types import SimpleNamespace

    closed = []
    monkeypatch.setattr(process_fence, "_linux_children", lambda parent: {77})
    # Model the complete Linux syscall surface, including constants absent on
    # Windows. This identity-safety regression must run on every test host.
    monkeypatch.setattr(process_fence, "os", SimpleNamespace(
        getpid=lambda: 1000, pidfd_open=lambda pid: 707, close=closed.append,
        WNOHANG=1, waitpid=lambda *_: (_ for _ in ()).throw(ChildProcessError()),
    ))
    monkeypatch.setattr(process_fence, "_linux_pid_matches", lambda fd, pid: True)
    # This is the process occupying an auto-reaped child's old numeric PID.
    monkeypatch.setattr(process_fence, "_linux_parent_pid", lambda pid: -999)
    monkeypatch.setattr(process_fence, "signal", SimpleNamespace(
        SIGSTOP=19, SIGKILL=9,
        pidfd_send_signal=lambda *_: pytest.fail("unowned process signalled"),
    ))
    assert not process_fence._kill_linux_tree(timeout=0.1)
    assert closed == [707]


def test_linux_child_of_exited_reused_ancestor_is_not_owned(monkeypatch):
    monkeypatch.setattr(process_fence, "_linux_pid_matches", lambda fd, pid: fd != 100)
    monkeypatch.setattr(process_fence, "_linux_parent_pid", lambda pid: 10)
    assert not process_fence._linux_owned_identity(11, 101, 10, {10: 100})


def test_normal_exit_with_failed_fence_is_not_reported_as_success(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, "_run_guarded", lambda *args, **kwargs: {"exit_code": 0, "fenced": False})
    messages = []
    assert watchdog.watch_command(command(tmp_path, "success"), project_root=tmp_path,
        disposition=classify, report=messages.append) == 1
    assert "cleanup failure" in messages[-1]


def test_recovery_permit_requires_exact_receipt_without_mutating_old_state(monkeypatch, tmp_path):
    with RunOwner(tmp_path):
        receipt = recovery_root(tmp_path) / "fence-receipt.json"
        data = {"version": 1, "run_id": "same-run", "owner_pid": 123, "owner_epoch": "e" * 64,
                "nonce": "a" * 64, "scope": "tree"}
        watchdog._atomic_json(receipt, data)
        monkeypatch.setenv(watchdog.PERMIT_ENV, "b" * 64)
        with pytest.raises(RuntimeError, match="does not match"):
            watchdog.validate_recovery_permit(tmp_path, run_id="same-run", owner_pid=123, owner_epoch="e" * 64)
        assert receipt.exists()
        monkeypatch.setenv(watchdog.PERMIT_ENV, "a" * 64)
        watchdog.validate_recovery_permit(tmp_path, run_id="same-run", owner_pid=123, owner_epoch="e" * 64)
        assert json.loads(receipt.read_text()) == data
        with pytest.raises(RuntimeError):
            watchdog.validate_recovery_permit(tmp_path, run_id="same-run", owner_pid=123, owner_epoch="e" * 64)


def test_group_only_receipt_cannot_authorize_controller_recovery(monkeypatch, tmp_path):
    with RunOwner(tmp_path):
        watchdog._atomic_json(recovery_root(tmp_path) / "fence-receipt.json", {
            "version": 1, "run_id": "same-run", "owner_pid": 123, "owner_epoch": "e" * 64,
            "nonce": "a" * 64, "scope": "groups",
        })
        monkeypatch.setenv(watchdog.PERMIT_ENV, "a" * 64)
        with pytest.raises(RuntimeError, match="does not match"):
            watchdog.validate_recovery_permit(tmp_path, run_id="same-run", owner_pid=123, owner_epoch="e" * 64)
