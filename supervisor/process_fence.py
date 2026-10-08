"""Live process ownership for the crash watchdog.

The guardian retains process-group anchors as unreaped direct children. Group
numbers are therefore live capabilities, never PIDs recovered from a file.
The controller may die before or after a spawn without losing group ownership.
Windows uses an inherited, non-breakaway Job Object instead.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import weakref
from multiprocessing.connection import Client, Listener
from typing import Any

_ADDRESS = "BELLO_FENCE_ADDRESS"
_SECRET = "BELLO_FENCE_SECRET"
_connection: Any = None
_worker_pid: int | None = None
_request_lock = threading.Lock()
_groups: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()


class _GroupHandle(int):
    def __new__(cls, group: int, lease: str):
        value = super().__new__(cls, group)
        value.lease = lease
        return value


def _request(message: dict[str, Any]) -> dict[str, Any]:
    with _request_lock:
        _connection.send(message)
        if not _connection.poll(10):
            raise RuntimeError("process guardian request timed out")
        reply = _connection.recv()
    if not isinstance(reply, dict) or "error" in reply:
        raise RuntimeError("process guardian rejected an ownership request")
    return reply


def signal_process_group(group: int, sig: int) -> None:
    """Signal a live lease; retired leases can never target a reused group ID."""
    if not is_guarded_worker():
        if isinstance(group, _GroupHandle):
            raise RuntimeError("process group lease lost its guardian; refusing a numeric PID fallback")
        os.killpg(group, sig)
        return
    if not isinstance(group, _GroupHandle):
        raise RuntimeError("guarded process group has no live ownership lease")
    _request({"op": "signal", "lease": group.lease, "signal": int(sig)})


def is_guarded_worker() -> bool:
    return _connection is not None and _worker_pid == os.getpid()


def configure_worker() -> None:
    """Consume the private guardian capability before a backend inherits env."""
    global _connection, _worker_pid
    address, secret = os.environ.pop(_ADDRESS, None), os.environ.pop(_SECRET, None)
    if address is None and secret is None:
        return
    if not address or not secret:
        raise RuntimeError("incomplete process guardian capability")
    host, port = json.loads(address)
    _connection = Client((host, port), authkey=bytes.fromhex(secret))
    _connection.send({"op": "hello", "pid": os.getpid()})
    if not _connection.poll(10):
        raise RuntimeError("process guardian handshake timed out")
    if _connection.recv() != {"ok": True}:
        raise RuntimeError("process guardian rejected the worker")
    _worker_pid = os.getpid()
    if os.name == "nt":
        def listen_for_stop() -> None:
            import _thread
            try:
                if _connection.recv() == {"stop": True}:
                    _thread.interrupt_main()
            except (OSError, EOFError):
                # Job kill-on-close is the parent-death fallback.
                return
        threading.Thread(target=listen_for_stop, daemon=True).start()


def process_group_id(process: Any) -> int:
    try:
        return _groups.get(process, process.pid)
    except TypeError:
        # Some adapters/tests expose lightweight, non-weak-referenceable views.
        return process.pid


async def create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    """Spawn with an owned group, preserving ordinary behavior outside watchdog."""
    if _connection is None or os.name == "nt" or not kwargs.get("start_new_session"):
        return await asyncio.create_subprocess_exec(*args, **kwargs)
    # Only the controller possesses this connection; it is close-on-exec and
    # its authentication secret has already been removed from os.environ.
    reply = _request({"op": "group"})
    if type(reply.get("group")) is not int or not isinstance(reply.get("lease"), str):
        raise RuntimeError("process guardian could not allocate a launch group")
    group = _GroupHandle(reply["group"], reply["lease"])
    kwargs["start_new_session"] = False
    kwargs["process_group"] = group
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*args, **kwargs))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Never retire the group while process creation could still join it.
        while not spawn.done():
            try:
                await asyncio.shield(spawn)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            process = spawn.result()
        except BaseException:
            signal_process_group(group, signal.SIGKILL)
            raise
        _groups[process] = group
        signal_process_group(group, signal.SIGKILL)
        await process.wait()
        raise
    except BaseException:
        signal_process_group(group, signal.SIGKILL)
        raise
    _groups[process] = group
    return process


_ANCHOR = (
    "import signal,time; "
    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
    "signal.signal(signal.SIGINT,signal.SIG_IGN); "
    "time.sleep(8640000)"
)


def _live_groups(groups: set[int]) -> bool:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and int(parts[0]) in groups and not parts[1].startswith("Z"):
            return True
    return False


def _kill_owned_groups(anchors: list[subprocess.Popen], *, timeout: float = 10.0) -> bool:
    """Never reap an anchor until its complete group is no longer running."""
    groups = {anchor.pid for anchor in anchors}
    deadline = time.monotonic() + timeout
    failed = False
    while True:
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS reports EPERM for a process group containing zombies.
                if sys.platform != "darwin":
                    failed = True
        if not _live_groups(groups):
            if failed:
                # Keep the anchors unreaped on an unproven result. A caller
                # retaining a failed lease must never retain a reusable PID.
                return False
            for anchor in anchors:
                anchor.wait(timeout=max(0.1, deadline - time.monotonic()))
            return not failed
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _enable_linux_subreaper() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    import ctypes
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "could not contain orphan descendants")
    return True


def _linux_children(parent: int) -> set[int]:
    # /proc/<pid>/task includes children of every thread, not just the leader.
    from pathlib import Path
    children: set[int] = set()
    for task in Path(f"/proc/{parent}/task").glob("*/children"):
        try:
            children.update(int(pid) for pid in task.read_text().split())
        except FileNotFoundError:
            continue
    return children


def _linux_pid_matches(fd: int, pid: int) -> bool:
    from pathlib import Path
    try:
        for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if line.startswith("Pid:"):
                return int(line.split(":", 1)[1]) == pid
    except (FileNotFoundError, ValueError):
        return False
    return False


def _linux_parent_pid(pid: int) -> int | None:
    from pathlib import Path
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except FileNotFoundError:
        return None


def _linux_owned_identity(pid: int, fd: int, parent: int, handles: dict[int, int]) -> bool:
    """Bind the opened handle to our still-live tree, even with auto-reaping.

    SIGCHLD=SIG_IGN can reap children of a stopped process. A numeric child
    listing alone is therefore insufficient: inspect ancestry between two
    live pidfd checks, and retain the ancestor's identity as well.
    """
    if not _linux_pid_matches(fd, pid):
        return False
    observed_parent = _linux_parent_pid(pid)
    guardian = os.getpid()
    ancestor_live = parent == guardian or (
        parent in handles and _linux_pid_matches(handles[parent], parent)
    )
    owned = observed_parent == guardian or (ancestor_live and observed_parent == parent)
    return owned and _linux_pid_matches(fd, pid)


def _linux_wait_stopped(pid: int, fd: int, deadline: float) -> bool:
    from pathlib import Path
    while time.monotonic() < deadline:
        try:
            tasks = Path(f"/proc/{pid}/task")
            before = set(tasks.iterdir())
            # Confirm every thread, not just a stopped/zombie group leader.
            # A thread cannot fork or reap once actually stopped. Repeating
            # the task inventory closes creation during the first inventory.
            stopped = all(
                (task / "stat").read_text().rsplit(")", 1)[1].split()[0] in {"T", "t", "Z", "X"}
                for task in before
            )
            if stopped and set(tasks.iterdir()) == before:
                return True
        except FileNotFoundError:
            # Exiting threads can disappear while /proc is scanned. Check the
            # stable process handle below before deciding it has gone away.
            pass
        try:
            signal.pidfd_send_signal(fd, signal.SIGSTOP)
        except ProcessLookupError:
            return True
        time.sleep(0.002)
    return False


def _kill_linux_tree(*, timeout: float = 10.0) -> bool:
    """Freeze and reap all descendants, including double forks and setsid().

    Every signal uses a kernel pidfd. Open handles are checked against the
    live child hierarchy before signalling; subreaping prevents orphan loss.
    """
    handles: dict[int, int] = {}
    identity_refused = False
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            pending = [(os.getpid(), pid) for pid in _linux_children(os.getpid())]
            seen: set[int] = set()
            while pending:
                parent, pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                if pid not in handles:
                    try:
                        fd = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    if not _linux_owned_identity(pid, fd, parent, handles):
                        os.close(fd)
                        # The PID exited, was auto-reaped or changed ancestry.
                        # Never signal a handle whose ownership is unproven.
                        identity_refused = True
                        continue
                    handles[pid] = fd
                elif not _linux_pid_matches(handles[pid], pid):
                    continue
                try:
                    signal.pidfd_send_signal(handles[pid], signal.SIGSTOP)
                except OSError:
                    pass
                # Stop completion precedes child traversal. Auto-reaping can
                # still occur, so each opened child is independently checked.
                if not _linux_wait_stopped(pid, handles[pid], deadline):
                    return False
                if _linux_pid_matches(handles[pid], pid):
                    children = _linux_children(pid)
                    if _linux_pid_matches(handles[pid], pid):
                        pending.extend((pid, child) for child in children)
            # Re-scan after freezing: a concurrent exit reparents to us, and a
            # concurrent fork remains in an already frozen ancestor's children.
            all_children = _linux_children(os.getpid())
            if all_children.issubset(seen):
                for fd in handles.values():
                    try:
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                while True:
                    try:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        return not identity_refused
                    if pid == 0:
                        break
            time.sleep(0.01)
        return False
    finally:
        for fd in handles.values():
            try:
                # A failed proof still makes a best-effort cleanup attempt;
                # never leave already stopped owned processes suspended.
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                except OSError:
                    pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _terminate_windows_job(job: Any, *, timeout: float = 10.0) -> bool:
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("user", ctypes.c_longlong), ("kernel", ctypes.c_longlong),
            ("period_user", ctypes.c_longlong), ("period_kernel", ctypes.c_longlong),
            ("faults", wintypes.DWORD), ("total", wintypes.DWORD),
            ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
        ]

    api = job._kernel32
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    if not api.TerminateJobObject(job._handle, 137):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        accounting = Accounting()
        if not api.QueryInformationJobObject(job._handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            return False
        if accounting.active == 0:
            return True
        time.sleep(0.02)
    return False


def guardian_main() -> int:
    """Separate process: contains workers, allocates groups and witnesses exit."""
    address = tuple(json.loads(os.environ.pop("BELLO_GUARDIAN_ADDRESS")))
    secret = bytes.fromhex(os.environ.pop("BELLO_GUARDIAN_SECRET"))
    command = json.loads(os.environ.pop("BELLO_GUARDIAN_COMMAND"))
    control = Client(address, authkey=secret)
    linux_tree = _enable_linux_subreaper()
    listener = Listener(("127.0.0.1", 0), authkey=secret)
    worker_env = dict(os.environ, **{
        _ADDRESS: json.dumps(listener.address), _SECRET: secret.hex(),
    })
    anchors: dict[str, subprocess.Popen] = {}
    shutdown = threading.Event()
    lock = threading.Lock()
    worker: subprocess.Popen | None = None
    worker_channel: list[Any] = []
    job: Any = None

    def anchor_group() -> tuple[str, int]:
        anchor = subprocess.Popen(
            [sys.executable, "-c", _ANCHOR], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, process_group=0,
        )
        lease = secrets.token_hex(32)
        anchors[lease] = anchor
        return lease, anchor.pid

    def serve_worker() -> None:
        try:
            connection = listener.accept()
            with connection:
                hello = connection.recv()
                if worker is None or hello != {"op": "hello", "pid": worker.pid}:
                    connection.send({"ok": False})
                    return
                connection.send({"ok": True})
                worker_channel.append(connection)
                while not shutdown.is_set():
                    request = connection.recv()
                    with lock:
                        if shutdown.is_set() or not isinstance(request, dict) or os.name == "nt":
                            connection.send({"error": "guardian is stopping"})
                            return
                        if request == {"op": "group"}:
                            lease, group = anchor_group()
                            connection.send({"group": group, "lease": lease})
                        elif request.get("op") == "signal" and request.get("signal") in {signal.SIGTERM, signal.SIGKILL, signal.SIGINT}:
                            lease = request.get("lease")
                            anchor = anchors.get(lease)
                            if anchor is not None:
                                if request["signal"] == signal.SIGKILL:
                                    try:
                                        cleared = _kill_owned_groups([anchor], timeout=3)
                                    finally:
                                        if anchor.returncode is not None:
                                            # Even a failed wait may already
                                            # have reaped. Invalidate first.
                                            anchors.pop(lease, None)
                                    if not cleared:
                                        connection.send({"error": "process group cleanup failed"})
                                        continue
                                    anchors.pop(lease, None)
                                else:
                                    try:
                                        os.killpg(anchor.pid, request["signal"])
                                    except ProcessLookupError:
                                        pass
                                    except PermissionError:
                                        if sys.platform != "darwin" or _live_groups({anchor.pid}):
                                            raise
                            connection.send({"ok": True})
                        else:
                            connection.send({"error": "invalid ownership request"})
        except (EOFError, OSError):
            return

    fence_ok = False
    parent_alive = True
    requested_stop = False
    exit_code = 125
    try:
        if os.name == "nt":
            from supervisor.appserver import _WindowsKillJob, _resume_windows_process
            # CREATE_SUSPENDED is a Win32 flag, not a public subprocess export
            # on supported CPython versions. Assign the Job before any worker
            # code runs, regardless of whether Python exposes that constant.
            worker = subprocess.Popen(command, env=worker_env,
                                      creationflags=getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
            job = _WindowsKillJob.create(worker.pid)
            _resume_windows_process(worker.pid)
        else:
            _, worker_group = anchor_group()
            worker = subprocess.Popen(command, env=worker_env, process_group=worker_group)
        threading.Thread(target=serve_worker, daemon=True).start()
        control.send({"started": worker.pid})
        while worker.poll() is None:
            if control.poll(0.05):
                try:
                    request = control.recv()
                except EOFError:
                    parent_alive = False
                    break
                if request == {"stop": True}:
                    requested_stop = True
                    break
        if requested_stop and worker.poll() is None:
            # Preserve the controller's normal cancellation/checkpoint/report
            # path before applying a bounded forceful descendant fence.
            if os.name != "nt":
                worker.send_signal(signal.SIGINT)
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            else:
                # _thread.interrupt_main in the worker preserves Python's
                # normal Ctrl-C cleanup without broadcasting a console event.
                if worker_channel:
                    try:
                        worker_channel[0].send({"stop": True})
                        worker.wait(timeout=10)
                    except (OSError, EOFError, subprocess.TimeoutExpired):
                        pass
        if worker.returncode is not None:
            exit_code = worker.returncode
    finally:
        try:
            with lock:
                shutdown.set()
                if job is not None:
                    fence_ok = _terminate_windows_job(job)
                elif os.name == "nt":
                    # Job assignment can fail in a restricted outer Job. The
                    # worker is still suspended and has executed no app code.
                    fence_ok = False
                elif linux_tree:
                    fence_ok = _kill_linux_tree()
                else:
                    fence_ok = _kill_owned_groups(list(anchors.values()))
        except Exception:
            fence_ok = False
        finally:
            if job is not None:
                try:
                    job.close()
                except OSError:
                    fence_ok = False
            if worker is not None:
                try:
                    if worker.poll() is None:
                        worker.kill()  # Still our live unreaped direct child.
                    worker.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    fence_ok = False
            listener.close()
        if parent_alive:
            try:
                control.send({
                    "exit_code": exit_code, "fenced": fence_ok,
                    "scope": "tree" if (os.name == "nt" or linux_tree) else "groups",
                })
            except (OSError, EOFError):
                pass
        control.close()
    return 0 if fence_ok else 125


if __name__ == "__main__":
    raise SystemExit(guardian_main())
