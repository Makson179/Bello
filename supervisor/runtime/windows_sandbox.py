"""Controller for Bello's packaged native Windows sandbox helper.

The helper owns the AppContainer, ACL, and Job Object boundary.  This module
only transports a strict request, drains the untrusted command's combined
output, and treats the helper's separate stderr pipe as a bounded control
channel.  Keeping stdin open after the request is intentional: EOF is the
native helper's parent-death/cancellation signal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import codecs
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import stat
import struct
import subprocess
from typing import Literal, TypeAlias


RestrictedMode: TypeAlias = Literal["read-only", "workspace-write"]
OutputCallback: TypeAlias = Callable[[str], Awaitable[None]]

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_CONTROL_BYTES = 64 * 1024
_ABORT_GRACE_SECONDS = 5.0
_PIPE_DRAIN_GRACE_SECONDS = 1.0
_RECOVERY_TIMEOUT_SECONDS = 30.0
_HELPER_NAME = "bello-windows-sandbox.exe"


class WindowsSandboxError(RuntimeError):
    """Base error for the native Windows restricted backend."""


class WindowsSandboxUnavailableError(WindowsSandboxError):
    """The packaged native helper cannot be used safely."""


class WindowsSandboxBackendError(WindowsSandboxError):
    """The helper rejected a request or violated its control protocol."""


@dataclass(frozen=True, slots=True)
class WindowsSandboxOutcome:
    output: str
    exit_code: int
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class _ExitRecord:
    exit_code: int


@dataclass(frozen=True, slots=True)
class _ErrorRecord:
    code: str
    message: str


_TerminalRecord: TypeAlias = _ExitRecord | _ErrorRecord


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _helper_path(root: Path, mode: RestrictedMode) -> Path:
    packaged = Path(__file__).resolve().parent / "bin" / _HELPER_NAME
    try:
        lexical_info = packaged.lstat()
        resolved = packaged.resolve(strict=True)
        info = resolved.stat()
    except OSError as exc:
        raise WindowsSandboxUnavailableError(
            f"the packaged Windows sandbox helper is unavailable at {packaged}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(lexical_info, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(lexical_info.st_mode)
        or file_attributes & reparse_flag
    ):
        raise WindowsSandboxUnavailableError(
            f"the Windows sandbox helper is not a non-reparse regular file: {packaged}"
        )
    if mode == "workspace-write" and _contains(root, resolved):
        raise WindowsSandboxUnavailableError(
            "the Windows sandbox helper cannot be loaded from the writable authority"
        )
    return resolved


def _helper_environment() -> dict[str, str]:
    # The helper resolves System32, LocalAppData, and the account profile from
    # Windows APIs.  An empty environment prevents tokens, proxies, loader
    # controls, and caller-controlled path authorities from reaching it.
    return {}


def _encode_request(request: dict[str, object]) -> bytes:
    try:
        body = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise WindowsSandboxBackendError("Windows sandbox request is not valid JSON") from exc
    if not body or len(body) > MAX_REQUEST_BYTES:
        raise WindowsSandboxBackendError(
            f"Windows sandbox request exceeds {MAX_REQUEST_BYTES} bytes"
        )
    return struct.pack("<I", len(body)) + body


def _run_request(
    *,
    command: str,
    cwd: Path,
    root: Path,
    mode: RestrictedMode,
    readable_roots: Iterable[Path],
    private_paths: Iterable[Path],
    network_access: bool,
) -> dict[str, object]:
    return {
        "operation": "run",
        "protocolVersion": PROTOCOL_VERSION,
        "command": command,
        "cwd": os.fspath(cwd),
        "root": os.fspath(root),
        "mode": mode,
        "readableRoots": [os.fspath(path) for path in readable_roots],
        "privatePaths": [os.fspath(path) for path in private_paths],
        "networkAccess": network_access,
    }


def _parse_terminal(raw: bytes) -> _TerminalRecord:
    if not raw or len(raw) > MAX_CONTROL_BYTES:
        raise WindowsSandboxBackendError("Windows sandbox control record is missing or oversized")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise WindowsSandboxBackendError(
            "Windows sandbox control channel did not contain exactly one terminal record"
        )
    try:
        value = json.loads(raw[:-1].decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsSandboxBackendError(
            "Windows sandbox control record is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict) or value.get("protocolVersion") != PROTOCOL_VERSION:
        raise WindowsSandboxBackendError("Windows sandbox control protocol version mismatch")
    kind = value.get("kind")
    if kind == "exit":
        if set(value) != {"protocolVersion", "kind", "exitCode"}:
            raise WindowsSandboxBackendError("Windows sandbox exit record has unexpected fields")
        exit_code = value.get("exitCode")
        if type(exit_code) is not int or not -(2**31) <= exit_code < 2**31:
            raise WindowsSandboxBackendError("Windows sandbox exitCode is not a signed 32-bit integer")
        return _ExitRecord(exit_code)
    if kind == "error":
        if set(value) != {"protocolVersion", "kind", "code", "message"}:
            raise WindowsSandboxBackendError("Windows sandbox error record has unexpected fields")
        code = value.get("code")
        message = value.get("message")
        if not isinstance(code, str) or not code or not isinstance(message, str) or not message:
            raise WindowsSandboxBackendError("Windows sandbox error record is malformed")
        return _ErrorRecord(code, message)
    raise WindowsSandboxBackendError("Windows sandbox control record has an unknown kind")


async def _read_control(stream: asyncio.StreamReader) -> _TerminalRecord:
    chunks: list[bytes] = []
    retained = 0
    while True:
        chunk = await stream.read(min(4096, MAX_CONTROL_BYTES + 1 - retained))
        if not chunk:
            break
        retained += len(chunk)
        if retained > MAX_CONTROL_BYTES:
            raise WindowsSandboxBackendError("Windows sandbox control record is oversized")
        chunks.append(chunk)
    return _parse_terminal(b"".join(chunks))


async def _close_stdin(process: asyncio.subprocess.Process) -> None:
    if process.stdin is None:
        return
    process.stdin.close()
    try:
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _await_uninterruptibly(task: asyncio.Task[None]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    task.result()


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


async def _spawn(
    helper: Path,
    environment: dict[str, str],
    *,
    cwd: Path,
) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            os.fspath(helper),
            cwd=os.fspath(cwd),
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise WindowsSandboxUnavailableError(
            f"could not start the packaged Windows sandbox helper: {exc}"
        ) from exc


async def _recover(helper: Path, environment: dict[str, str]) -> None:
    process = await _spawn(helper, environment, cwd=helper.parent)
    assert process.stdin is not None and process.stderr is not None
    control = asyncio.create_task(_read_control(process.stderr))
    waiter = asyncio.create_task(process.wait())
    try:
        process.stdin.write(
            _encode_request({"operation": "recover", "protocolVersion": PROTOCOL_VERSION})
        )
        await process.stdin.drain()
        await _close_stdin(process)
        try:
            await asyncio.wait_for(
                asyncio.gather(asyncio.shield(waiter), asyncio.shield(control)),
                _RECOVERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise WindowsSandboxBackendError("Windows sandbox recovery timed out") from exc
        record = control.result()
        if process.returncode != 0 or not isinstance(record, _ExitRecord) or record.exit_code != 0:
            if isinstance(record, _ErrorRecord):
                detail = f"{record.code}: {record.message}"
            else:
                detail = f"helper exit {process.returncode}, terminal record {record!r}"
            raise WindowsSandboxBackendError(f"Windows sandbox recovery failed: {detail}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in (control, waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(control, waiter, return_exceptions=True)


async def _abort_and_recover(
    process: asyncio.subprocess.Process,
    helper: Path,
    environment: dict[str, str],
) -> None:
    await _close_stdin(process)
    if process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), _ABORT_GRACE_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    await _recover(helper, environment)


async def _settle_aborted_streams(
    output: asyncio.Task[None], control: asyncio.Task[_TerminalRecord]
) -> None:
    _, pending = await asyncio.wait(
        {output, control}, timeout=_PIPE_DRAIN_GRACE_SECONDS
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if output.done() and not output.cancelled():
        error = output.exception()
        if error is not None:
            raise error
    if control.done() and not control.cancelled():
        # Retrieve deliberate EOF/kill protocol errors without treating them as
        # a cleanup failure; the separate recovery request is authoritative.
        control.exception()


async def run_restricted(
    *,
    command: str,
    cwd: Path,
    root: Path,
    mode: RestrictedMode,
    readable_roots: Iterable[Path],
    private_paths: Iterable[Path],
    network_access: bool,
    timeout: float,
    on_output: OutputCallback | None,
    cancel_event: asyncio.Event | None,
    max_output_chars: int,
    truncated_text: str,
) -> WindowsSandboxOutcome:
    """Run one command through the native helper without an unsafe fallback."""

    if mode not in ("read-only", "workspace-write"):
        raise WindowsSandboxBackendError(f"unsupported restricted Windows mode: {mode}")
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise WindowsSandboxBackendError("command must be non-empty and contain no NUL byte")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise WindowsSandboxBackendError("timeout must be a finite positive number")
    if type(network_access) is not bool:
        raise WindowsSandboxBackendError("network_access must be a boolean")
    if type(max_output_chars) is not int or max_output_chars < 0:
        raise WindowsSandboxBackendError("max_output_chars must be a non-negative integer")
    if on_output is not None and not callable(on_output):
        raise TypeError("on_output must be an async callable")
    if cancel_event is not None and cancel_event.is_set():
        return WindowsSandboxOutcome("", 130, cancelled=True)

    root = Path(root).resolve(strict=True)
    cwd = Path(cwd).resolve(strict=True)
    readable_roots = tuple(Path(path).resolve(strict=True) for path in readable_roots)
    private_paths = tuple(Path(path).resolve(strict=False) for path in private_paths)
    helper = _helper_path(root, mode)
    environment = _helper_environment()
    frame = _encode_request(
        _run_request(
            command=command,
            cwd=cwd,
            root=root,
            mode=mode,
            readable_roots=readable_roots,
            private_paths=private_paths,
            network_access=network_access,
        )
    )

    process: asyncio.subprocess.Process | None = None
    output_task: asyncio.Task[None] | None = None
    control_task: asyncio.Task[_TerminalRecord] | None = None
    waiter: asyncio.Task[int] | None = None
    cancellation: asyncio.Task[bool] | None = None
    chunks: list[str] = []
    retained = 0
    truncated = False

    async def collect_output(stream: asyncio.StreamReader) -> None:
        nonlocal retained, truncated
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        async def retain(text: str) -> None:
            nonlocal retained, truncated
            if not text:
                return
            remaining = max_output_chars - retained
            emitted = text[: max(remaining, 0)]
            if emitted:
                chunks.append(emitted)
                retained += len(emitted)
                if on_output is not None:
                    await on_output(emitted)
            if len(emitted) < len(text) and not truncated:
                truncated = True
                chunks.append(truncated_text)
                if on_output is not None:
                    await on_output(truncated_text)

        while True:
            raw = await stream.read(65536)
            if not raw:
                break
            await retain(decoder.decode(raw))
        await retain(decoder.decode(b"", final=True))

    try:
        process = await _spawn(helper, environment, cwd=root)
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        output_task = asyncio.create_task(collect_output(process.stdout))
        control_task = asyncio.create_task(_read_control(process.stderr))
        waiter = asyncio.create_task(process.wait())
        if cancel_event is not None:
            cancellation = asyncio.create_task(cancel_event.wait())

        process.stdin.write(frame)
        await process.stdin.drain()
        deadline = asyncio.get_running_loop().time() + float(timeout)
        timed_out = False
        cancelled = False
        while True:
            for task in (output_task, control_task):
                if task.done() and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        raise error
            if waiter.done():
                break
            watched: set[asyncio.Task] = {waiter}
            if not output_task.done():
                watched.add(output_task)
            if not control_task.done():
                watched.add(control_task)
            if cancellation is not None:
                watched.add(cancellation)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            done, _ = await asyncio.wait(
                watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                timed_out = True
                break
            if cancellation is not None and cancellation in done and cancellation.result():
                cancelled = True
                break
            if waiter in done:
                break

        if timed_out or cancelled:
            cleanup = asyncio.create_task(_abort_and_recover(process, helper, environment))
            await _await_uninterruptibly(cleanup)
            await _settle_aborted_streams(output_task, control_task)
            return WindowsSandboxOutcome(
                "".join(chunks),
                124 if timed_out else 130,
                timed_out=timed_out,
                cancelled=cancelled,
            )

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    asyncio.shield(output_task), asyncio.shield(control_task)
                ),
                _ABORT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise WindowsSandboxBackendError(
                "Windows sandbox pipes did not close after helper exit"
            ) from exc
        record = control_task.result()
        await _close_stdin(process)
        if isinstance(record, _ErrorRecord):
            if process.returncode == 0:
                raise WindowsSandboxBackendError(
                    "Windows sandbox helper returned an error record with exit status 0"
                )
            raise WindowsSandboxBackendError(f"{record.code}: {record.message}")
        if process.returncode != 0:
            raise WindowsSandboxBackendError(
                f"Windows sandbox helper exited {process.returncode} after an exit record"
            )
        return WindowsSandboxOutcome("".join(chunks), record.exit_code)
    except asyncio.CancelledError as exc:
        if process is not None:
            cleanup = asyncio.create_task(_abort_and_recover(process, helper, environment))
            try:
                await _await_uninterruptibly(cleanup)
            except BaseException as cleanup_error:
                raise WindowsSandboxBackendError(
                    f"Windows sandbox cleanup failed after task cancellation: {cleanup_error}"
                ) from exc
        raise
    except BaseException as exc:
        if process is not None:
            cleanup = asyncio.create_task(_abort_and_recover(process, helper, environment))
            try:
                await _await_uninterruptibly(cleanup)
            except BaseException as cleanup_error:
                raise WindowsSandboxBackendError(
                    f"Windows sandbox cleanup failed: {cleanup_error}"
                ) from exc
        raise
    finally:
        if cancellation is not None:
            cancellation.cancel()
        tasks = [task for task in (cancellation, output_task, control_task, waiter) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
