"""Overlapped directory I/O owns its buffers until cancellation completes."""

from __future__ import annotations

import ctypes
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import weakref

import pytest

from supervisor import snapshot_windows as windows


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


@pytest.fixture
def native_api(monkeypatch):
    state = SimpleNamespace(events=[], error=0, complete=False, inflight=True)

    def cancel(*args):
        state.events.append("cancel")
        return True

    def result(*args):
        state.events.append("result")
        state.error = 995 if state.complete else 996
        return False

    def wait(*args):
        state.events.append("wait")
        state.complete = True
        return 0

    def close(handle):
        assert state.complete or not state.inflight, (
            "kernel can still write the OVERLAPPED and buffer"
        )
        state.events.append(f"close:{handle}")

    api = SimpleNamespace(
        CancelIoEx=Function(cancel),
        GetOverlappedResult=Function(result),
        WaitForSingleObject=Function(wait),
    )
    ops = SimpleNamespace(
        _close_windows_handle=close,
        WorkspaceSnapshotError=RuntimeError,
        _WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER=31,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: state.error, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda code: OSError(code, "synthetic Win32 error"),
        raising=False,
    )

    class Watcher(windows._WindowsDirectoryChangeWatcher):
        @classmethod
        def _services(cls):
            return ops

    watcher = Watcher(
        Path("dependency"), 10, 20, ctypes.c_ulong(0), ctypes.create_string_buffer(64)
    )
    watcher.pending = True
    yield state, api, watcher
    # Drain while fake APIs are still installed. On Windows a later finalizer
    # must never pass test-only handle numbers into the real kernel functions.
    state.complete = True
    api.CancelIoEx.call = lambda *args: True
    api.GetOverlappedResult.call = lambda *args: True
    ops._close_windows_handle = close
    watcher.close()


def test_close_waits_for_cancellation_before_closing_handles(native_api):
    state, _, watcher = native_api
    watcher.close()
    assert state.events == [
        "cancel",
        "result",
        "wait",
        "result",
        "close:10",
        "close:20",
    ]
    assert not watcher.pending
    assert watcher.closed and watcher.handle == watcher.event_handle == 0


def test_discarded_watcher_drains_before_python_releases_native_storage(native_api):
    state, _, fixture_watcher = native_api
    watcher = type(fixture_watcher)(
        Path("discarded-dependency"),
        30,
        40,
        ctypes.c_ulong(0),
        ctypes.create_string_buffer(64),
        pending=True,
    )
    watcher_ref = weakref.ref(watcher)
    overlapped_ref = weakref.ref(watcher.overlapped)
    buffer_ref = weakref.ref(watcher.buffer)
    del watcher
    gc.collect()
    assert state.complete, "pending kernel writes outlived their Python storage"
    assert state.events == [
        "cancel",
        "result",
        "wait",
        "result",
        "close:30",
        "close:40",
    ]
    assert watcher_ref() is overlapped_ref() is buffer_ref() is None


@pytest.mark.parametrize("failure", ["timeout", "exception", "shutdown-global"])
def test_discarded_pending_watcher_survives_failed_finalization(
    native_api, monkeypatch, failure
):
    state, api, fixture_watcher = native_api
    watcher = type(fixture_watcher)(
        Path("discarded-dependency"),
        30,
        40,
        ctypes.c_ulong(0),
        ctypes.create_string_buffer(64),
        pending=True,
    )
    if failure == "timeout":
        api.WaitForSingleObject.call = lambda *args: 258
    elif failure == "exception":

        def interrupted(*args):
            raise KeyboardInterrupt("synthetic finalizer interruption")

        api.CancelIoEx.call = interrupted
    else:
        # Model module teardown before close() can install its quarantine.
        monkeypatch.setattr(windows, "_RETAINED_WINDOWS_WATCHERS", None)
    retained = windows._WindowsDirectoryChangeWatcher.__del__.__defaults__[0]
    reference = weakref.ref(watcher)
    overlapped_ref = weakref.ref(watcher.overlapped)
    buffer_ref = weakref.ref(watcher.buffer)
    key = id(watcher)
    del watcher
    gc.collect()
    try:
        assert retained[key] is reference()
        assert reference().pending
        assert reference()._finalizer_pinned
        assert overlapped_ref() is not None and buffer_ref() is not None
        assert not any(event.startswith("close:") for event in state.events)
    finally:
        monkeypatch.setattr(windows, "_RETAINED_WINDOWS_WATCHERS", retained)
        state.complete = True
        api.CancelIoEx.call = lambda *args: True
        api.GetOverlappedResult.call = lambda *args: True
        retained[key].close()
    gc.collect()
    # The deliberately rare lifetime pin is never manually decremented, even
    # when a later retry can safely release the operating-system handles.
    assert reference() is not None
    assert not reference().pending
    assert reference().handle == reference().event_handle == 0
    assert key not in retained
    assert overlapped_ref() is not None and buffer_ref() is not None


def test_uncertain_finalizer_pins_once_but_success_never_pins(native_api):
    state, api, watcher = native_api
    pins = []
    api.WaitForSingleObject.call = lambda *args: 258
    watcher.__del__(_pin=pins.append)
    watcher.__del__(_pin=pins.append)
    assert pins == [watcher]
    assert watcher._finalizer_pinned
    state.complete = True
    watcher.close()
    watcher.__del__(_pin=pins.append)
    assert pins == [watcher]


def test_successful_finalization_never_pins(native_api):
    _, _, watcher = native_api
    watcher.__del__(_pin=lambda _: pytest.fail("successful cleanup pinned storage"))
    assert not watcher._finalizer_pinned
    assert not watcher.pending
    assert watcher.handle == watcher.event_handle == 0


@pytest.mark.parametrize(
    "native",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(os.name != "nt", reason="native Windows I/O"),
        ),
    ],
)
def test_pending_finalizer_pin_survives_all_python_registry_references(
    tmp_path, native
):
    script = r"""
import ctypes,gc,json,sys,weakref
from pathlib import Path
from supervisor import snapshot_windows,workspace_snapshot
native=sys.argv[2]=="True"
class Watcher(snapshot_windows._WindowsDirectoryChangeWatcher):
    def close(self):
        raise OSError("simulate unavailable teardown services")
if native:
    watcher=workspace_snapshot._WindowsDirectoryChangeWatcher.open(Path(sys.argv[1]))
    def cannot_close():
        raise OSError("simulate unavailable teardown services")
    watcher.close=cannot_close
else:
    watcher=Watcher(Path("synthetic"),10,20,ctypes.c_ulong(0),ctypes.create_string_buffer(64),pending=True)
reference=weakref.ref(watcher)
buffer_reference=weakref.ref(watcher.buffer)
overlapped_reference=weakref.ref(watcher.overlapped)
del watcher
snapshot_windows._RETAINED_WINDOWS_WATCHERS.clear()
for _ in range(3):gc.collect()
assert reference() is not None and reference()._finalizer_pinned
assert reference().pending and buffer_reference() is not None and overlapped_reference() is not None
if native:
    # This intentionally quarantined request still owns its native storage and
    # handles until OS process teardown; it must survive new kernel writes.
    for i in range(100):(Path(sys.argv[1])/str(i)).write_bytes(b"after GC")
print(json.dumps({"status":"PASS","registry_empty":not snapshot_windows._RETAINED_WINDOWS_WATCHERS}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "faulthandler",
            "-c",
            script,
            str(tmp_path),
            str(native),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"status": "PASS", "registry_empty": True}


def test_native_watcher_rejects_interpreter_without_lifetime_pin(
    native_api, monkeypatch, tmp_path
):
    _, _, watcher = native_api
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(windows, "_PIN_WINDOWS_WATCHER", None)
    with pytest.raises(RuntimeError, match="CPython lifetime protection"):
        type(watcher).open(tmp_path)


def test_discarded_file_guard_releases_its_sharing_lock():
    closed = []

    class Guard(windows._WindowsRuntimeFileGuard):
        @classmethod
        def _services(cls):
            return SimpleNamespace(_close_windows_handle=closed.append)

    guard = Guard(Path("task"), 30, (1, 2))
    reference = weakref.ref(guard)
    del guard
    gc.collect()
    assert reference() is None
    assert closed == [30]


@pytest.mark.parametrize(
    "cancel_error,result", [(None, True), (None, 995), (1168, True), (1168, 995)]
)
def test_completed_and_not_found_cancellation_races_are_drained(
    native_api, cancel_error, result
):
    state, api, watcher = native_api

    def cancel(*args):
        state.events.append("cancel")
        state.error = cancel_error or 0
        return cancel_error is None

    def complete(*args):
        state.events.append("result")
        state.complete = True
        state.error = 0 if result is True else result
        return result is True

    api.CancelIoEx.call = cancel
    api.GetOverlappedResult.call = complete
    watcher.close()
    assert state.events == ["cancel", "result", "close:10", "close:20"]
    assert not watcher.pending
    assert id(watcher) not in windows._RETAINED_WINDOWS_WATCHERS
    watcher.close()
    assert state.events == ["cancel", "result", "close:10", "close:20"]


def test_not_found_request_still_waits_until_completion(native_api):
    state, api, watcher = native_api

    def cancel(*args):
        state.events.append("cancel")
        state.error = 1168
        return False

    api.CancelIoEx.call = cancel
    watcher.close()
    assert state.events == [
        "cancel",
        "result",
        "wait",
        "result",
        "close:10",
        "close:20",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "cancel-error",
        "cancel-exception",
        "query-error",
        "timeout",
        "wait-error",
        "still-pending",
    ],
)
def test_unproven_completion_retains_storage_and_handles_for_retry(native_api, failure):
    state, api, watcher = native_api
    if failure == "cancel-error":

        def denied(*args):
            state.error = 5
            return False

        api.CancelIoEx.call = denied
    elif failure == "cancel-exception":

        def interrupted(*args):
            raise OSError("synthetic cancel exception")

        api.CancelIoEx.call = interrupted
    elif failure == "query-error":

        def query_error(*args):
            state.error = 6
            return False

        api.GetOverlappedResult.call = query_error
    elif failure == "timeout":
        api.WaitForSingleObject.call = lambda *args: 258
    elif failure == "wait-error":

        def wait_error(*args):
            state.error = 6
            return 0xFFFFFFFF

        api.WaitForSingleObject.call = wait_error
    else:
        api.WaitForSingleObject.call = lambda *args: 0
    with pytest.raises(OSError):
        watcher.close()
    assert watcher.pending and watcher.closed
    assert watcher.handle == 10 and watcher.event_handle == 20
    assert windows._RETAINED_WINDOWS_WATCHERS[id(watcher)] is watcher
    assert not any(event.startswith("close:") for event in state.events)
    before = watcher.overlapped, watcher.buffer
    gc.collect()
    assert (watcher.overlapped, watcher.buffer) == before
    state.complete = True
    api.CancelIoEx.call = lambda *args: True
    api.GetOverlappedResult.call = lambda *args: True
    watcher.close()
    assert not watcher.pending and watcher.handle == watcher.event_handle == 0
    assert id(watcher) not in windows._RETAINED_WINDOWS_WATCHERS


def test_caller_collection_clear_cannot_release_pending_buffer(native_api):
    _, api, watcher = native_api
    api.WaitForSingleObject.call = lambda *args: 258
    watcher_type = type(watcher)
    independent = watcher_type(
        Path("other-dependency"),
        30,
        40,
        ctypes.c_ulong(0),
        ctypes.create_string_buffer(64),
        pending=True,
    )
    reference = weakref.ref(independent)
    owned = {"dependency": independent}
    try:
        with pytest.raises(TimeoutError):
            independent.close()
        del independent
        owned.clear()
        gc.collect()
        assert reference() is not None
        assert reference().pending
        api.CancelIoEx.call = lambda *args: True
        api.GetOverlappedResult.call = lambda *args: True
        # The mock close checker must now agree that native completion occurred.
        native_api[0].complete = True
        reference().close()
        gc.collect()
        assert reference() is None
    finally:
        if reference() is not None:
            windows._RETAINED_WINDOWS_WATCHERS.pop(id(reference()), None)


def test_unarmed_watcher_closes_without_waiting_for_nonexistent_io(native_api):
    state, _, watcher = native_api
    watcher.pending = False
    state.inflight = False
    watcher.close()
    assert state.events == ["close:10", "close:20"]
    assert watcher.handle == watcher.event_handle == 0


def test_completed_io_closes_other_handle_after_close_failure(native_api):
    state, _, watcher = native_api
    regular_close = watcher._services()._close_windows_handle

    def close(handle):
        regular_close(handle)
        if handle == 10:
            raise OSError("synthetic CloseHandle failure")

    watcher._services()._close_windows_handle = close
    with pytest.raises(OSError, match="CloseHandle"):
        watcher.close()
    assert state.events[-2:] == ["close:10", "close:20"]
    assert not watcher.pending and watcher.handle == watcher.event_handle == 0
    assert id(watcher) not in windows._RETAINED_WINDOWS_WATCHERS
    watcher.close()  # Never retry a numeric handle whose close outcome was uncertain.
    assert state.events.count("close:10") == 1


@pytest.mark.parametrize(
    "kind", ["reset-failure", "read-failure", "read-pending", "read-success"]
)
def test_arm_tracks_only_potentially_issued_io(native_api, kind):
    state, api, watcher = native_api
    watcher.pending = False
    state.inflight = False

    # Real ctypes storage is required by byref, with the same named members.
    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", ctypes.c_uint32),
            ("OffsetHigh", ctypes.c_uint32),
            ("hEvent", ctypes.c_void_p),
        ]

    watcher.overlapped = Overlapped()
    watcher._services()._WINDOWS_DEPENDENCY_CONTENT_NOTIFY_FILTER = 31

    def reset(*args):
        state.error = 5
        return kind != "reset-failure"

    def read(*args):
        state.error = 997 if kind == "read-pending" else 5
        state.inflight = kind in {"read-success", "read-pending"}
        return kind == "read-success"

    api.ResetEvent = Function(reset)
    api.ReadDirectoryChangesW = Function(read)
    if kind.endswith("failure"):
        with pytest.raises(OSError):
            watcher._arm()
        assert not watcher.pending
        watcher.close()
        assert state.events == ["close:10", "close:20"]
    else:
        watcher._arm()
        assert watcher.pending
        with pytest.raises(RuntimeError, match="reuse"):
            watcher._arm()
        watcher.close()
        assert not watcher.pending


@pytest.mark.parametrize(
    "failure",
    ["before-arm", "not-issued", "issued-interruption", "issued-cancel-timeout"],
)
def test_construction_failure_preserves_native_storage_until_drained(
    native_api, tmp_path, monkeypatch, failure
):
    state, api, watcher = native_api
    ops = watcher._services()
    state.inflight = False
    ops.is_link_or_reparse = lambda *args, **kwargs: False
    ops._windows_api_path = str
    monkeypatch.setattr(windows, "os", SimpleNamespace(name="nt"))
    api.CreateFileW = Function(lambda *args: 10)
    api.CreateEventW = Function(lambda *args: 20)

    def reset(*args):
        state.error = 5
        return failure != "before-arm"

    def read(*args):
        state.error = 5
        if failure.startswith("issued"):
            state.inflight = True
            raise KeyboardInterrupt("synthetic native return interruption")
        return False

    api.ResetEvent = Function(reset)
    api.ReadDirectoryChangesW = Function(read)
    before = set(windows._RETAINED_WINDOWS_WATCHERS)
    if failure == "issued-cancel-timeout":
        api.WaitForSingleObject.call = lambda *args: 258
    expected = KeyboardInterrupt if failure == "issued-interruption" else OSError
    try:
        with pytest.raises(expected):
            type(watcher).open(tmp_path)
        retained = set(windows._RETAINED_WINDOWS_WATCHERS) - before
        if failure == "issued-cancel-timeout":
            assert len(retained) == 1
            survivor = windows._RETAINED_WINDOWS_WATCHERS[retained.pop()]
            assert (
                survivor.pending
                and survivor.handle == 10
                and survivor.event_handle == 20
            )
            assert not any(event.startswith("close:") for event in state.events)
            state.complete = True
            survivor.close()
        else:
            assert not retained
            assert state.events[-2:] == ["close:10", "close:20"]
    finally:
        for key in set(windows._RETAINED_WINDOWS_WATCHERS) - before:
            windows._RETAINED_WINDOWS_WATCHERS.pop(key)


def test_consumed_notification_is_completed_before_rearming(native_api, monkeypatch):
    _, api, watcher = native_api
    api.GetOverlappedResult.call = lambda *args: True

    def rearm():
        assert not watcher.pending
        watcher.pending = True

    monkeypatch.setattr(watcher, "_arm", rearm)
    assert watcher.consume_changes() is True
    assert watcher.pending
    watcher.close()


def test_closed_watcher_never_silently_rearms(native_api):
    _, _, watcher = native_api
    watcher.close()
    with pytest.raises(RuntimeError, match="closed"):
        watcher.consume_changes()
    with pytest.raises(RuntimeError, match="reuse"):
        watcher._arm()


@pytest.mark.skipif(
    os.name != "nt",
    reason="requires native Windows asynchronous I/O and process handle counts",
)
@pytest.mark.parametrize("ownership", ["explicit-close", "discard", "cyclic-discard"])
def test_native_windows_repeated_cancel_gc_isolated_process(tmp_path, ownership):
    script = r"""
import ctypes,gc,json,sys,time,weakref
from ctypes import wintypes
from pathlib import Path
from supervisor import snapshot_windows, workspace_snapshot
api=ctypes.WinDLL("kernel32",use_last_error=True)
api.GetCurrentProcess.argtypes=[]
api.GetCurrentProcess.restype=wintypes.HANDLE
api.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
api.GetProcessHandleCount.restype=wintypes.BOOL
def handles():
    count=wintypes.DWORD()
    if not api.GetProcessHandleCount(api.GetCurrentProcess(),ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    return count.value
root=Path(sys.argv[1])
ownership=sys.argv[2]
def cycle(number):
    folder=root/str(number)
    folder.mkdir()
    watcher=workspace_snapshot._WindowsDirectoryChangeWatcher.open(folder)
    if number%3:
        (folder/"entry").write_text("notification")
    if number%3==1:
        deadline=time.monotonic()+3
        while not watcher.consume_changes():
            assert time.monotonic()<deadline,"native notification did not complete"
            time.sleep(.001)
    if ownership=="explicit-close":
        watcher.close()
        assert not watcher.pending and not watcher.handle and not watcher.event_handle
    elif ownership=="cyclic-discard":
        watcher.test_cycle=watcher
    reference=weakref.ref(watcher)
    overlapped_reference=weakref.ref(watcher.overlapped)
    buffer_reference=weakref.ref(watcher.buffer)
    del watcher
    gc.collect()
    assert reference() is overlapped_reference() is buffer_reference() is None
    assert not snapshot_windows._RETAINED_WINDOWS_WATCHERS
    # Trigger kernel notifications only AFTER discarded storage was collected.
    # An unclosed native request could otherwise silently corrupt a later test.
    (folder/"after-gc").write_bytes(b"notification after collection")
    (folder/"after-gc").unlink()
    if (folder/"entry").exists():
        (folder/"entry").unlink()
    folder.rmdir()
for number in range(5):cycle(number)
before=handles()
for number in range(5,205):cycle(number)
gc.collect()
after=handles()
assert before==after,(before,after)
print(json.dumps({"status":"PASS","iterations":200,"ownership":ownership,"handles_before":before,"handles_after":after}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "faulthandler",
            "-c",
            script,
            str(tmp_path),
            ownership,
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["iterations"] == 200
    assert receipt["ownership"] == ownership
    assert receipt["handles_before"] == receipt["handles_after"]
