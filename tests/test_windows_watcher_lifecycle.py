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
    # A failed mock cleanup must not keep test-only state alive between cases.
    windows._RETAINED_WINDOWS_WATCHERS.pop(id(watcher), None)


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
def test_native_windows_repeated_cancel_gc_isolated_process(tmp_path):
    script = r"""
import ctypes,gc,json,sys,time
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
    watcher.close()
    assert not watcher.pending and not watcher.handle and not watcher.event_handle
    del watcher
    gc.collect()
    assert not snapshot_windows._RETAINED_WINDOWS_WATCHERS
    if (folder/"entry").exists():
        (folder/"entry").unlink()
    folder.rmdir()
for number in range(5):cycle(number)
before=handles()
for number in range(5,205):cycle(number)
gc.collect()
after=handles()
assert before==after,(before,after)
print(json.dumps({"status":"PASS","iterations":200,"handles_before":before,"handles_after":after}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["iterations"] == 200
    assert receipt["handles_before"] == receipt["handles_after"]
