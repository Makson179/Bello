from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from supervisor.filesystem_safety import (
    is_link_or_reparse,
    remove_path_tree,
    windows_path_component_issue,
)
from supervisor.executables import (
    ExecutableResolutionError,
    require_trusted_executable,
    windows_system_executable,
)

CODEX_NO_WEB_SEARCH_CONFIG_FLAGS = ["-c", 'web_search="disabled"']

APP_SERVER_PARENT_CONTEXT_ENV_VARS = {
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_NETWORK_ALLOW_LOCAL_BINDING",
    "CODEX_NETWORK_POLICY_VIOLATION",
    "CODEX_NETWORK_PROXY_ACTIVE",
    "CODEX_NETWORK_PROXY_ATTRIBUTION",
    "CODEX_NETWORK_PROXY_BROKERED_CREDENTIALS",
    "CODEX_NETWORK_PROXY_CREDENTIAL_BROKER_ACTIVE",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "CODEX_SNAPSHOT_OVERRIDE",
    "CODEX_THREAD_ID",
}


class AppServerError(RuntimeError):
    pass


class AppServerProtocolError(AppServerError):
    pass


class AppServerTimeoutError(AppServerError):
    pass


@dataclass(frozen=True)
class AppServerMessage:
    raw: dict[str, Any]

    @property
    def request_id(self) -> int | str | None:
        return self.raw.get("id")

    @property
    def method(self) -> str | None:
        value = self.raw.get("method")
        return value if isinstance(value, str) else None

    @property
    def params(self) -> dict[str, Any]:
        value = self.raw.get("params")
        return value if isinstance(value, dict) else {}

    @property
    def is_response(self) -> bool:
        return "id" in self.raw and ("result" in self.raw or "error" in self.raw) and "method" not in self.raw

    @property
    def is_server_request(self) -> bool:
        return "id" in self.raw and self.method is not None

    @property
    def is_notification(self) -> bool:
        return "id" not in self.raw and self.method is not None


NotificationHandler = Callable[[AppServerMessage], Awaitable[None] | None]
ServerRequestHandler = Callable[[AppServerMessage], Awaitable[None] | None]
TransportErrorHandler = Callable[[BaseException], Awaitable[None] | None]

APP_SERVER_STDOUT_LIMIT = 16 * 1024 * 1024
APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS = 30.0
APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS = 30.0
APP_SERVER_RESPOND_TIMEOUT_SECONDS = 15.0
APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS = 10.0
APP_SERVER_CODER_RPC_TIMEOUT_SECONDS = 3600.0
APP_SERVER_PROCESS_EXIT_TIMEOUT_SECONDS = 3.0

_IS_WINDOWS = os.name == "nt"
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_WINDOWS_CREATE_SUSPENDED = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
_WINDOWS_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_CLOSE = 0x00002000


def _app_server_process_kwargs() -> dict[str, Any]:
    if _IS_WINDOWS:
        return {"creationflags": _WINDOWS_CREATE_NEW_PROCESS_GROUP | _WINDOWS_CREATE_SUSPENDED}
    return {"start_new_session": True}


def _app_server_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    if not _IS_WINDOWS or not command:
        return command
    try:
        executable = require_trusted_executable(
            command[0],
            cwd=cwd or Path.cwd(),
            environ=environ,
            windows=True,
        )
    except ExecutableResolutionError as exc:
        raise AppServerError(str(exc)) from exc
    return [executable, *command[1:]]


def _resume_windows_process(pid: int) -> None:
    """Resume a process created with CREATE_SUSPENDED after job assignment."""

    if not _IS_WINDOWS:  # pragma: no cover - guarded by AppServerClient.start
        raise RuntimeError("Windows thread APIs are unavailable on this platform")

    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        _raise_windows_api_error(ctypes, "enumerating suspended app-server threads")
    resumed = False
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    _raise_windows_api_error(ctypes, "opening suspended app-server thread")
                try:
                    previous_suspend_count = kernel32.ResumeThread(thread)
                    if previous_suspend_count == 0xFFFFFFFF:
                        _raise_windows_api_error(ctypes, "resuming app-server process")
                    resumed = resumed or previous_suspend_count > 0
                finally:
                    kernel32.CloseHandle(thread)
            has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise ProcessLookupError(f"could not find a suspended primary thread for app-server process {pid}")


def _raise_windows_api_error(ctypes_module: Any, action: str) -> None:
    error = ctypes_module.get_last_error()
    raise OSError(error, f"Windows error while {action}: {ctypes_module.FormatError(error).strip()}")


class _WindowsKillJob:
    """A Windows Job Object that kills the entire child tree when closed."""

    def __init__(self, handle: Any, kernel32: Any):
        self._handle = handle
        self._kernel32 = kernel32

    @classmethod
    def create(cls, pid: int) -> "_WindowsKillJob":
        if not _IS_WINDOWS:  # pragma: no cover - guarded by AppServerClient.start
            raise RuntimeError("Windows Job Objects are unavailable on this platform")

        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            cls._raise_last_windows_error(ctypes, "creating app-server cleanup job")
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_CLOSE
            if not kernel32.SetInformationJobObject(job_handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                cls._raise_last_windows_error(ctypes, "configuring app-server cleanup job")

            # AssignProcessToJobObject requires PROCESS_TERMINATE and
            # PROCESS_SET_QUOTA.  Opening a separate handle avoids depending
            # on asyncio/subprocess implementation details.
            process_handle = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
            if not process_handle:
                cls._raise_last_windows_error(ctypes, "opening app-server process")
            try:
                if not kernel32.AssignProcessToJobObject(job_handle, process_handle):
                    cls._raise_last_windows_error(ctypes, "assigning app-server cleanup job")
            finally:
                kernel32.CloseHandle(process_handle)
        except BaseException:
            kernel32.CloseHandle(job_handle)
            raise
        return cls(job_handle, kernel32)

    @staticmethod
    def _raise_last_windows_error(ctypes_module: Any, action: str) -> None:
        _raise_windows_api_error(ctypes_module, action)

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        if not self._kernel32.CloseHandle(handle):
            import ctypes

            self._raise_last_windows_error(ctypes, "closing app-server cleanup job")


class AppServerClient:
    def __init__(
        self,
        *,
        command: list[str] | None = None,
        cwd: Path | None = None,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        transport_error_handler: TransportErrorHandler | None = None,
        stdout_limit: int = APP_SERVER_STDOUT_LIMIT,
    ):
        self.command = command or ["codex", "app-server", *CODEX_NO_WEB_SEARCH_CONFIG_FLAGS, "--listen", "stdio://"]
        self.cwd = cwd
        self.notification_handler = notification_handler
        self.server_request_handler = server_request_handler
        self.transport_error_handler = transport_error_handler
        self.stdout_limit = stdout_limit
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._waiters: list[tuple[Callable[[AppServerMessage], bool], asyncio.Future[AppServerMessage]]] = []
        self.incoming: asyncio.Queue[AppServerMessage] = asyncio.Queue()
        self.reader_error: BaseException | None = None
        self._isolated_codex_home: Path | None = None
        self._process_group_id: int | None = None
        self._windows_job: _WindowsKillJob | None = None

    async def start(self) -> None:
        if self.process is not None:
            return
        env = _app_server_environment()
        resolved_command = _app_server_command(self.command, cwd=self.cwd, environ=env)
        source_codex_home = _codex_home_from_environment(env)
        if source_codex_home.is_dir():
            self._isolated_codex_home = _create_isolated_codex_home(source_codex_home)
            env["CODEX_HOME"] = str(self._isolated_codex_home)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *resolved_command,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.stdout_limit,
                **_app_server_process_kwargs(),
            )
            if _IS_WINDOWS:
                self._windows_job = _WindowsKillJob.create(self.process.pid)
                _resume_windows_process(self.process.pid)
            else:
                # start_new_session makes the app-server PID its process-group
                # ID.  Save it now so descendants can still be killed after
                # the direct child has already exited.
                self._process_group_id = self.process.pid
        except BaseException:
            await self._abort_failed_start()
            self._cleanup_isolated_codex_home()
            raise
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        try:
            if self._reader_task:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except asyncio.CancelledError:
                    pass
                self._reader_task = None
            if self._stderr_task:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except asyncio.CancelledError:
                    pass
                self._stderr_task = None
            if self.process:
                if self.process.returncode is None:
                    self._request_process_tree_shutdown()
                    try:
                        await asyncio.wait_for(
                            self.process.wait(),
                            timeout=APP_SERVER_PROCESS_EXIT_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        self._force_process_tree_shutdown()
                        await self.process.wait()
        finally:
            # A graceful app-server exit does not prove its descendants also
            # exited.  Force-clean the saved POSIX group or close the Windows
            # kill-on-close Job Object before dropping our process reference.
            self._force_process_tree_shutdown()
            self.process = None
            self._process_group_id = None
            self._cleanup_isolated_codex_home()

    def _cleanup_isolated_codex_home(self) -> None:
        if self._isolated_codex_home is None:
            return
        isolated = self._isolated_codex_home
        _remove_codex_home_tree(isolated)
        if isolated.exists() or isolated.is_symlink():
            raise AppServerError(f"failed to remove isolated CODEX_HOME: {isolated}")
        self._isolated_codex_home = None

    async def _abort_failed_start(self) -> None:
        process = self.process
        if process is None:
            return
        self._force_process_tree_shutdown()
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=APP_SERVER_PROCESS_EXIT_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        self.process = None
        self._process_group_id = None

    def _request_process_tree_shutdown(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        if _IS_WINDOWS:
            try:
                process.send_signal(_WINDOWS_CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            return
        self._signal_posix_process_group(signal.SIGTERM)

    def _force_process_tree_shutdown(self) -> None:
        process = self.process
        if _IS_WINDOWS:
            job = self._windows_job
            self._windows_job = None
            if job is not None:
                try:
                    job.close()
                    return
                except OSError:
                    # Fall through to taskkill if closing the kernel handle
                    # unexpectedly fails.
                    pass
            if process is not None and process.returncode is None:
                self._force_windows_process_tree_without_job(process)
            return
        self._signal_posix_process_group(signal.SIGKILL)

    def _signal_posix_process_group(self, sig: int) -> None:
        process = self.process
        if process is None:
            return
        group_id = self._process_group_id
        if group_id is None and process.returncode is None:
            group_id = process.pid
        try:
            if group_id is None:
                return
            os.killpg(group_id, sig)
        except ProcessLookupError:
            return
        except OSError:
            if process.returncode is not None:
                return
            try:
                if sig == signal.SIGTERM:
                    process.terminate()
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    @staticmethod
    def _force_windows_process_tree_without_job(process: asyncio.subprocess.Process) -> None:
        # This is a fail-safe for the narrow window where process creation
        # succeeded but Job Object assignment did not.  Normal Windows cleanup
        # closes the Job Object and never launches taskkill.
        try:
            taskkill = windows_system_executable("taskkill.exe")
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=_WINDOWS_CREATE_NO_WINDOW,
            )
        except (ExecutableResolutionError, OSError, ValueError, subprocess.TimeoutExpired):
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    async def initialize(self, *, timeout: float = APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "bello", "title": "Bello", "version": "0.4.1"},
                "capabilities": {"experimentalApi": True, "requestAttestation": False},
            },
            timeout=timeout,
        )
        await self.notify("initialized", timeout=APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS)
        return result

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        await self._ensure_started()
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_with_timeout(payload, timeout, stage=f"app-server RPC {method} send")
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AppServerTimeoutError(f"app-server RPC {method} response timed out after {timeout:g}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> None:
        await self._ensure_started()
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._send_with_timeout(payload, timeout, stage=f"app-server notification {method} send")

    async def respond(
        self,
        request_id: int | str,
        result: Any = None,
        error: Any = None,
        *,
        timeout: float = APP_SERVER_RESPOND_TIMEOUT_SECONDS,
    ) -> None:
        await self._ensure_started()
        payload: dict[str, Any] = {"id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        await self._send_with_timeout(payload, timeout, stage=f"app-server respond {request_id} send")

    async def wait_for_notification(
        self,
        predicate: Callable[[AppServerMessage], bool],
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> AppServerMessage:
        future: asyncio.Future[AppServerMessage] = asyncio.get_running_loop().create_future()
        self._waiters.append((predicate, future))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AppServerTimeoutError(f"app-server notification wait timed out after {timeout:g}s") from exc
        finally:
            self._waiters = [(pred, fut) for pred, fut in self._waiters if fut is not future]

    async def config_requirements_read(self, *, timeout: float = APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        return await self.request("configRequirements/read", timeout=timeout)

    async def account_read(self, *, timeout: float = APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        return await self.request("account/read", {"refreshToken": False}, timeout=timeout)

    async def account_rate_limits_read(self, *, timeout: float = APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        return await self.request("account/rateLimits/read", timeout=timeout)

    async def model_list(self, *, timeout: float = APP_SERVER_PREFLIGHT_RPC_TIMEOUT_SECONDS) -> dict[str, Any]:
        return await self.request("model/list", {}, timeout=timeout)

    async def thread_start(
        self,
        params: dict[str, Any],
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("thread/start", params, timeout=timeout)

    async def thread_resume(
        self,
        params: dict[str, Any],
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("thread/resume", params, timeout=timeout)

    async def thread_read(
        self,
        thread_id: str,
        *,
        include_turns: bool = True,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("thread/read", {"threadId": thread_id, "includeTurns": include_turns}, timeout=timeout)

    async def thread_turns_list(
        self,
        thread_id: str,
        *,
        limit: int = 10,
        items_view: str = "full",
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request(
            "thread/turns/list",
            {"threadId": thread_id, "limit": limit, "itemsView": items_view},
            timeout=timeout,
        )

    async def thread_archive(
        self,
        thread_id: str,
        *,
        timeout: float = APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("thread/archive", {"threadId": thread_id}, timeout=timeout)

    async def thread_unsubscribe(
        self,
        thread_id: str,
        *,
        timeout: float = APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=timeout)

    async def turn_start(
        self,
        params: dict[str, Any],
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("turn/start", params, timeout=timeout)

    async def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        text: str,
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request(
            "turn/steer",
            {"threadId": thread_id, "expectedTurnId": expected_turn_id, "input": [text_input(text)]},
            timeout=timeout,
        )

    async def turn_interrupt(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout: float = APP_SERVER_CONTROL_RPC_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        return await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout)

    async def _ensure_started(self) -> None:
        if self.process is None:
            await self.start()
        if self.process is None or self.process.stdin is None:
            raise AppServerError("app-server process is not writable")

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("app-server process is not writable")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def _send_with_timeout(self, payload: dict[str, Any], timeout: float, *, stage: str) -> None:
        try:
            await asyncio.wait_for(self._send(payload), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AppServerTimeoutError(f"{stage} timed out after {timeout:g}s") from exc

    async def _read_loop(self) -> None:
        assert self.process is not None
        error: BaseException | None = None
        try:
            if self.process.stdout is None:
                raise AppServerError("app-server process has no stdout")
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    error = AppServerError("app-server stream closed")
                    self.reader_error = error
                    await self._notify_transport_error(error)
                    break
                try:
                    raw = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise AppServerProtocolError(f"invalid JSON from app-server: {exc}") from exc
                if not isinstance(raw, dict):
                    continue
                message = AppServerMessage(raw)
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self._normalize_reader_error(exc)
            self.reader_error = error
            await self._notify_transport_error(error)
        finally:
            pending_error = error or AppServerError("app-server stream closed")
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(pending_error)

    def _normalize_reader_error(self, exc: Exception) -> AppServerError:
        if isinstance(exc, AppServerError):
            return exc
        if isinstance(exc, ValueError) and "chunk is longer than limit" in str(exc):
            return AppServerProtocolError(
                f"app-server stdout line exceeded stream limit ({self.stdout_limit} bytes): {exc}"
            )
        return AppServerError(f"app-server stream reader failed: {exc}")

    async def _notify_transport_error(self, error: BaseException) -> None:
        if self.transport_error_handler is None:
            return
        try:
            result = self.transport_error_handler(error)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    async def _drain_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return

    async def _dispatch(self, message: AppServerMessage) -> None:
        if message.is_response:
            future = self._pending.get(message.request_id)
            if future and not future.done():
                if "error" in message.raw:
                    future.set_exception(AppServerError(str(message.raw["error"])))
                else:
                    result = message.raw.get("result")
                    future.set_result(result if isinstance(result, dict) else {"value": result})
            return

        await self.incoming.put(message)
        for predicate, future in list(self._waiters):
            if not future.done() and predicate(message):
                future.set_result(message)
        if message.is_server_request and self.server_request_handler:
            result = self.server_request_handler(message)
            if asyncio.iscoroutine(result):
                await result
        elif message.is_notification and self.notification_handler:
            result = self.notification_handler(message)
            if asyncio.iscoroutine(result):
                await result


def text_input(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "text_elements": []}


def _app_server_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for name in APP_SERVER_PARENT_CONTEXT_ENV_VARS:
        env.pop(name, None)
    return env


def _codex_home_from_environment(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CODEX_HOME")
    if configured:
        # Keep the lexical entry until `_create_isolated_codex_home` has
        # inspected it.  Resolving here would erase a root junction/symlink
        # before the native-Windows reparse check can reject it.
        return Path(configured).expanduser().absolute()
    if _IS_WINDOWS:
        windows_profile = environ.get("USERPROFILE")
        if not windows_profile and environ.get("HOMEDRIVE") and environ.get("HOMEPATH"):
            windows_profile = environ["HOMEDRIVE"] + environ["HOMEPATH"]
        home = Path(windows_profile or Path.home()).expanduser()
    else:
        home = Path(environ.get("HOME") or Path.home()).expanduser()
    return (home / ".codex").absolute()


def _create_isolated_codex_home(source: Path) -> Path:
    lexical_source = source.expanduser().absolute()
    if _IS_WINDOWS and is_link_or_reparse(lexical_source):
        raise AppServerError(
            f"native Windows CODEX_HOME cannot be a symlink, junction, or reparse point: {source}"
        )
    source = lexical_source.resolve(strict=True)
    if not source.is_dir():
        raise AppServerError(f"CODEX_HOME is not a directory: {source}")
    isolated = Path(tempfile.mkdtemp(prefix="bello-codex-home-")).resolve()
    try:
        children = list(source.iterdir())
        if _IS_WINDOWS:
            _validate_windows_codex_home_names(source, [child.name for child in children])
        for child in children:
            if child.name == "rules" or (_IS_WINDOWS and child.name.casefold() == "rules"):
                continue
            destination = isolated / child.name
            if _IS_WINDOWS:
                _copy_windows_codex_home_entry(child, destination)
            else:
                os.symlink(str(child), destination, target_is_directory=child.is_dir())
        (isolated / "rules").mkdir(mode=0o700)
        return isolated
    except BaseException:
        try:
            _remove_codex_home_tree(isolated)
        except OSError:
            pass
        raise


def _validate_windows_codex_home_names(directory: Path, names: list[str]) -> None:
    seen: dict[str, str] = {}
    for name in names:
        if issue := windows_path_component_issue(name):
            raise AppServerError(f"unsafe Windows CODEX_HOME path {directory / name}: {issue}")
        key = name.casefold()
        previous = seen.get(key)
        if previous is not None and previous != name:
            raise AppServerError(
                f"Windows CODEX_HOME contains case-colliding names: {previous!r} and {name!r}"
            )
        seen[key] = name


def _copy_windows_codex_home_entry(source: Path, destination: Path) -> None:
    _validate_windows_codex_home_entry(source)
    metadata = source.lstat()
    metadata = _assert_stable_codex_entry(
        source, metadata, require_directory=stat.S_ISDIR(metadata.st_mode)
    )
    if stat.S_ISDIR(metadata.st_mode):
        shutil.copytree(source, destination, symlinks=False, copy_function=shutil.copy2)
        _assert_stable_codex_entry(source, metadata, require_directory=True)
        _validate_windows_codex_home_entry(destination)
        _make_codex_home_copy_writable(destination)
        return
    if stat.S_ISREG(metadata.st_mode):
        shutil.copy2(source, destination, follow_symlinks=False)
        _assert_stable_codex_entry(source, metadata, require_directory=False)
        _validate_windows_codex_home_entry(destination)
        _make_codex_home_copy_writable(destination)
        return
    raise AppServerError(f"unsupported Windows CODEX_HOME entry: {source}")


def _validate_windows_codex_home_entry(source: Path) -> None:
    metadata = source.lstat()
    if is_link_or_reparse(source, stat_result=metadata):
        raise AppServerError(
            "native Windows isolated CODEX_HOME refuses symlinks, junctions, mount points, "
            f"and other reparse entries: {source}"
        )
    if stat.S_ISREG(metadata.st_mode):
        _assert_stable_codex_entry(source, metadata, require_directory=False)
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise AppServerError(f"unsupported Windows CODEX_HOME entry: {source}")
    metadata = _assert_stable_codex_entry(source, metadata, require_directory=True)
    children = list(source.iterdir())
    metadata = _assert_stable_codex_entry(source, metadata, require_directory=True)
    _validate_windows_codex_home_names(source, [child.name for child in children])
    for child in children:
        _assert_stable_codex_entry(source, metadata, require_directory=True)
        _validate_windows_codex_home_entry(child)
    _assert_stable_codex_entry(source, metadata, require_directory=True)


def _make_codex_home_copy_writable(path: Path) -> None:
    metadata = path.lstat()
    if is_link_or_reparse(path, stat_result=metadata):
        raise AppServerError(f"isolated CODEX_HOME copy unexpectedly contains a link: {path}")
    if _IS_WINDOWS and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
        raise AppServerError(
            f"isolated CODEX_HOME copy unexpectedly contains a hardlinked file: {path}"
        )
    permissions = stat.S_IMODE(metadata.st_mode) | stat.S_IRUSR | stat.S_IWUSR
    if stat.S_ISDIR(metadata.st_mode):
        permissions |= stat.S_IXUSR
        metadata = _assert_stable_codex_entry(path, metadata, require_directory=True)
        os.chmod(path, permissions)
        metadata = _assert_stable_codex_entry(path, metadata, require_directory=True)
        children = list(path.iterdir())
        metadata = _assert_stable_codex_entry(path, metadata, require_directory=True)
        for child in children:
            _assert_stable_codex_entry(path, metadata, require_directory=True)
            _make_codex_home_copy_writable(child)
        return
    _assert_stable_codex_entry(path, metadata, require_directory=False)
    os.chmod(path, permissions)


def _remove_codex_home_tree(path: Path) -> None:
    remove_path_tree(path)


def _assert_stable_codex_entry(
    path: Path,
    expected: os.stat_result,
    *,
    require_directory: bool,
) -> os.stat_result:
    current = path.lstat()
    if (
        is_link_or_reparse(path, stat_result=current)
        or stat.S_IFMT(current.st_mode) != stat.S_IFMT(expected.st_mode)
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or require_directory != stat.S_ISDIR(current.st_mode)
    ):
        raise AppServerError(
            f"CODEX_HOME entry changed or was redirected during isolated copy/cleanup: {path}"
        )
    return current


def last_agent_message_text(turn: dict[str, Any]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        text = _agent_message_text_from_item(item)
        if text is not None:
            return text
    return None


def _agent_message_text_from_item(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type == "agentMessage" and isinstance(item.get("text"), str):
        return item["text"]
    if item_type in {"message", "assistantMessage"} or item.get("role") in {"assistant", "agent"}:
        text = _message_content_text(item.get("content"))
        if text is not None:
            return text
        if isinstance(item.get("text"), str):
            return item["text"]
        if isinstance(item.get("message"), str):
            return item["message"]
    payload = item.get("payload")
    if isinstance(payload, dict):
        return _agent_message_text_from_item(payload)
    return None


def _message_content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    text = "".join(parts).strip()
    return text or None
