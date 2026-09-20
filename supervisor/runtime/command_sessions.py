"""Managed, bounded command sessions layered over :mod:`runtime.sandbox`.

The manager never spawns a process itself.  A caller supplies a SandboxRunner,
which remains the sole owner of process creation, isolation, timeout and tree
cleanup.  This layer only keeps that coroutine alive after an early yield and
provides polling and stop semantics.

Persisted active records are marked ``lost`` on recovery and are never replayed
or reattached.  Their outcome is uncertain: the old runtime may have died after
the command changed the workspace, and a PID alone would not prove identity.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Protocol

from supervisor.filesystem_safety import is_link_or_reparse
from supervisor.runtime.sandbox import SandboxResult


OutputCallback = Callable[[str], Awaitable[None]]
ACTIVE_STATUSES = frozenset({"running"})
TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "timed_out", "cancelled", "lost"}
)
COMMAND_SESSION_MAX_OUTPUT_CHARS = 1024 * 1024
COMMAND_SESSION_MAX_RESPONSE_CHARS = 256 * 1024
COMMAND_SESSION_MAX_PERSISTED_OUTPUT_CHARS = 8 * 1024
COMMAND_SESSION_MAX_ACTIVE = 16
COMMAND_SESSION_MAX_ACTIVE_PER_TURN = 4
COMMAND_SESSION_MAX_RECORDS = 256
COMMAND_SESSION_MAX_YIELD_MS = 30_000
COMMAND_SESSION_STOP_GRACE_SECONDS = 5.0
COMMAND_SESSION_MAX_STATE_BYTES = 4 * 1024 * 1024
_STATE_VERSION = 1
_STATE_FILE = "sessions.json"


class CommandSessionError(RuntimeError):
    """A command-session request is invalid, stale, or not owned by its caller."""


class CommandRunner(Protocol):
    async def run(
        self,
        command: str,
        cwd: Path,
        timeout: float,
        on_output: OutputCallback | None = None,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> SandboxResult: ...


@dataclass(frozen=True, slots=True)
class CommandCompletion:
    """One terminal callback payload; exactly one is produced per live start."""

    session_id: str
    thread_id: str
    turn_id: str
    call_id: str
    status: str
    result: SandboxResult | None
    error: BaseException | None


FinishedCallback = Callable[[CommandCompletion], Awaitable[None]]


@dataclass(slots=True)
class _Session:
    id: str
    thread_id: str
    turn_id: str
    call_id: str
    fingerprint: str
    status: str
    started_at: float
    finished_at: float | None = None
    exit_code: int | None = None
    duration: float | None = None
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None
    callback_error: str | None = None
    buffer: str = ""
    buffer_start: int = 0
    output_end: int = 0
    cursor: int = 0
    output_truncated: bool = False
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class CommandSessionManager:
    """Own yielded SandboxRunner calls and expose poll/stop operations."""

    def __init__(
        self,
        directory: Path,
        *,
        max_output_chars: int = COMMAND_SESSION_MAX_OUTPUT_CHARS,
        max_response_chars: int = COMMAND_SESSION_MAX_RESPONSE_CHARS,
        max_active: int = COMMAND_SESSION_MAX_ACTIVE,
        max_active_per_turn: int = COMMAND_SESSION_MAX_ACTIVE_PER_TURN,
        max_records: int = COMMAND_SESSION_MAX_RECORDS,
    ):
        for name, value in {
            "max_output_chars": max_output_chars,
            "max_response_chars": max_response_chars,
            "max_active": max_active,
            "max_active_per_turn": max_active_per_turn,
            "max_records": max_records,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_response_chars > max_output_chars:
            raise ValueError("max_response_chars cannot exceed max_output_chars")
        if max_records < max_active:
            raise ValueError("max_records cannot be smaller than max_active")

        self.directory = Path(directory).absolute()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_link_or_reparse(self.directory) or not self.directory.is_dir():
            raise CommandSessionError(
                "command-session state must be a private directory, not a link"
            )
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        self._state_file = self.directory / _STATE_FILE
        self._validate_state_file()
        self.max_output_chars = max_output_chars
        self.max_response_chars = max_response_chars
        self.max_active = max_active
        self.max_active_per_turn = max_active_per_turn
        self.max_records = max_records
        self._sessions: dict[str, _Session] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._load_recovered()

    async def start(
        self,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str,
        runner: CommandRunner,
        command: str,
        cwd: Path,
        timeout: float,
        yield_time_ms: int = 10_000,
        on_output: OutputCallback | None = None,
        on_finished: FinishedCallback | None = None,
    ) -> dict[str, Any]:
        """Start exactly one managed runner call and wait at most ``yield_time_ms``."""

        thread_id = self._identifier(thread_id, "thread_id")
        turn_id = self._identifier(turn_id, "turn_id")
        call_id = self._identifier(call_id, "call_id")
        if self._closed:
            raise CommandSessionError("command-session manager is closed")
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise CommandSessionError("command must be non-empty text without NUL bytes")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > 3600
        ):
            raise CommandSessionError("timeout must be between 0 and 3600 seconds")
        wait_seconds = self._wait_seconds(yield_time_ms, "yield_time_ms")
        try:
            resolved_cwd = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CommandSessionError("command cwd must be an existing directory") from exc
        if not resolved_cwd.is_dir():
            raise CommandSessionError("command cwd must be an existing directory")

        session_id = self.session_id(thread_id, turn_id, call_id)
        fingerprint = self._fingerprint(command, resolved_cwd, float(timeout))
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                self._require_owner(existing, thread_id, turn_id)
                if existing.call_id != call_id or existing.fingerprint != fingerprint:
                    raise CommandSessionError(
                        "the command call id was already claimed with different arguments"
                    )
                return self._snapshot(existing, consume=True)
            self._enforce_limits(thread_id, turn_id)
            session = _Session(
                id=session_id,
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                fingerprint=fingerprint,
                status="running",
                started_at=time.time(),
            )
            self._sessions[session_id] = session
            self._prune()
            # Persist the claim before scheduling the runner. A crash in this
            # gap becomes lost/uncertain rather than accidentally executing it
            # during recovery.
            self._persist()
            session.task = asyncio.create_task(
                self._drive(
                    session,
                    runner,
                    command,
                    resolved_cwd,
                    float(timeout),
                    on_output,
                    on_finished,
                ),
                name=f"bello-command-{session_id[-12:]}",
            )

        try:
            if wait_seconds:
                try:
                    await asyncio.wait_for(session.done.wait(), wait_seconds)
                except asyncio.TimeoutError:
                    pass
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self._stop_sessions([session])
            raise
        async with self._lock:
            return self._snapshot(session, consume=True)

    async def poll(
        self,
        *,
        thread_id: str,
        turn_id: str,
        session_id: str,
        yield_time_ms: int = 0,
    ) -> dict[str, Any]:
        """Return new output, optionally waiting briefly for completion or output."""

        thread_id = self._identifier(thread_id, "thread_id")
        turn_id = self._identifier(turn_id, "turn_id")
        session_id = self._identifier(session_id, "session_id")
        wait_seconds = self._wait_seconds(yield_time_ms, "yield_time_ms")
        async with self._lock:
            session = self._owned_session(session_id, thread_id, turn_id)
            starting_end = session.output_end
            has_pending_output = session.cursor < session.output_end
            done = session.done
        if wait_seconds and session.status in ACTIVE_STATUSES and not has_pending_output:
            deadline = asyncio.get_running_loop().time() + wait_seconds
            while session.status in ACTIVE_STATUSES and session.output_end == starting_end:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(done.wait(), min(remaining, 0.1))
                except asyncio.TimeoutError:
                    pass
        async with self._lock:
            session = self._owned_session(session_id, thread_id, turn_id)
            return self._snapshot(session, consume=True)

    async def stop(
        self,
        *,
        thread_id: str,
        turn_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Idempotently stop an owned live session and wait for process cleanup."""

        thread_id = self._identifier(thread_id, "thread_id")
        turn_id = self._identifier(turn_id, "turn_id")
        session_id = self._identifier(session_id, "session_id")
        async with self._lock:
            session = self._owned_session(session_id, thread_id, turn_id)
        await self._stop_sessions([session])
        async with self._lock:
            return self._snapshot(session, consume=True)

    async def cancel_turn(self, thread_id: str, turn_id: str) -> None:
        thread_id = self._identifier(thread_id, "thread_id")
        turn_id = self._identifier(turn_id, "turn_id")
        async with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.thread_id == thread_id
                and session.turn_id == turn_id
                and self._requires_cleanup(session)
            ]
        await self._stop_sessions(sessions)

    async def cancel_thread(self, thread_id: str) -> None:
        thread_id = self._identifier(thread_id, "thread_id")
        async with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.thread_id == thread_id and self._requires_cleanup(session)
            ]
        await self._stop_sessions(sessions)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if self._requires_cleanup(session)
            ]
        await self._stop_sessions(sessions)

    @staticmethod
    def session_id(thread_id: str, turn_id: str, call_id: str) -> str:
        payload = json.dumps(
            [thread_id, turn_id, call_id], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return "cmd_" + hashlib.sha256(payload).hexdigest()

    async def _drive(
        self,
        session: _Session,
        runner: CommandRunner,
        command: str,
        cwd: Path,
        timeout: float,
        on_output: OutputCallback | None,
        on_finished: FinishedCallback | None,
    ) -> None:
        async def capture(chunk: str) -> None:
            if not isinstance(chunk, str):
                raise TypeError("command output callback must receive text")
            async with self._lock:
                self._append_output(session, chunk)
            if on_output is not None:
                await on_output(chunk)

        try:
            result = await runner.run(
                command,
                cwd,
                timeout,
                capture,
                cancel_event=session.cancel_event,
            )
        except asyncio.CancelledError:
            error = CommandSessionError("command session was cancelled during cleanup")
            await self._complete(
                session,
                status="cancelled",
                result=None,
                error=error,
                on_finished=on_finished,
            )
            raise
        except Exception as exc:
            await self._complete(
                session,
                status="failed",
                result=None,
                error=exc,
                on_finished=on_finished,
            )
            return

        if result.cancelled:
            status = "cancelled"
        elif result.timed_out:
            status = "timed_out"
        elif result.exit_code == 0:
            status = "completed"
        else:
            status = "failed"
        await self._complete(
            session,
            status=status,
            result=result,
            error=None,
            on_finished=on_finished,
        )

    async def _complete(
        self,
        session: _Session,
        *,
        status: str,
        result: SandboxResult | None,
        error: BaseException | None,
        on_finished: FinishedCallback | None,
    ) -> None:
        async with self._lock:
            if session.status not in ACTIVE_STATUSES:
                return
            # A compliant runner sends output through the callback. Accept a
            # result-only runner as well, without duplicating a streamed prefix.
            if result is not None and result.output and session.output_end == 0:
                self._append_output(session, result.output)
            self._finish(
                session,
                status=status,
                exit_code=result.exit_code if result is not None else (130 if status == "cancelled" else None),
                duration=result.duration if result is not None else None,
                timed_out=result.timed_out if result is not None else False,
                cancelled=result.cancelled if result is not None else status == "cancelled",
                error=f"{type(error).__name__}: {error}" if error is not None else None,
                signal_done=False,
            )
            completion = CommandCompletion(
                session_id=session.id,
                thread_id=session.thread_id,
                turn_id=session.turn_id,
                call_id=session.call_id,
                status=status,
                result=result,
                error=error,
            )
        try:
            if on_finished is not None:
                await on_finished(completion)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self._lock:
                session.callback_error = f"{type(exc).__name__}: {exc}"
                self._persist()
        finally:
            # Waiters observe terminal state only after the host has had its
            # single chance to emit item/completed. Callback failures are not
            # retried because duplicate completion events are worse than a
            # surfaced transport failure.
            session.done.set()

    def _append_output(self, session: _Session, chunk: str) -> None:
        if not chunk:
            return
        session.buffer += chunk
        session.output_end += len(chunk)
        overflow = len(session.buffer) - self.max_output_chars
        if overflow > 0:
            session.buffer = session.buffer[overflow:]
            session.buffer_start += overflow
            session.output_truncated = True

    def _finish(
        self,
        session: _Session,
        *,
        status: str,
        exit_code: int | None,
        duration: float | None = None,
        timed_out: bool = False,
        cancelled: bool = False,
        error: str | None = None,
        signal_done: bool = True,
    ) -> None:
        if session.status not in ACTIVE_STATUSES:
            return
        session.status = status
        session.exit_code = exit_code
        session.duration = duration if duration is not None else max(0.0, time.time() - session.started_at)
        session.finished_at = time.time()
        session.timed_out = timed_out
        session.cancelled = cancelled
        session.error = error
        if signal_done:
            session.done.set()
        self._prune()
        self._persist()

    async def _stop_sessions(self, sessions: list[_Session]) -> None:
        tasks: list[asyncio.Task[None]] = []
        for session in sessions:
            # The runner may already have made the record terminal while its
            # on_finished callback is still publishing item/completed.  Signal
            # cancellation only to an active runner, but always join its owner
            # task so turn/thread cleanup cannot overtake that final callback.
            if session.status in ACTIVE_STATUSES:
                session.cancel_event.set()
            if session.task is not None:
                tasks.append(session.task)
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=COMMAND_SESSION_STOP_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Consume any cancellation on already-done tasks too.
        await asyncio.gather(*done, return_exceptions=True)

    @staticmethod
    def _requires_cleanup(session: _Session) -> bool:
        return session.status in ACTIVE_STATUSES or (
            session.task is not None and not session.task.done()
        )

    def _snapshot(self, session: _Session, *, consume: bool) -> dict[str, Any]:
        start = max(session.cursor, session.buffer_start)
        relative = start - session.buffer_start
        available = session.buffer[relative:]
        response_dropped = session.cursor < session.buffer_start
        if len(available) > self.max_response_chars:
            available = available[-self.max_response_chars :]
            response_dropped = True
        if consume:
            session.cursor = session.output_end
        value: dict[str, Any] = {
            "sessionId": session.id,
            "status": session.status,
            "output": available,
            "outputTruncated": session.output_truncated or response_dropped,
        }
        if session.status in TERMINAL_STATUSES:
            value["aggregatedOutput"] = session.buffer[-self.max_response_chars :]
            value["exitCode"] = session.exit_code
            value["duration"] = session.duration
            value["timedOut"] = session.timed_out
            value["cancelled"] = session.cancelled
            if session.error:
                value["error"] = session.error
            if session.callback_error:
                value["callbackError"] = session.callback_error
        return value

    def _owned_session(self, session_id: str, thread_id: str, turn_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise CommandSessionError("unknown command session")
        self._require_owner(session, thread_id, turn_id)
        return session

    @staticmethod
    def _require_owner(session: _Session, thread_id: str, turn_id: str) -> None:
        if session.thread_id != thread_id or session.turn_id != turn_id:
            raise CommandSessionError("command session is not owned by this thread and turn")

    def _enforce_limits(self, thread_id: str, turn_id: str) -> None:
        active = [s for s in self._sessions.values() if s.status in ACTIVE_STATUSES]
        if len(active) >= self.max_active:
            raise CommandSessionError("global active command-session limit reached")
        owned = [s for s in active if s.thread_id == thread_id and s.turn_id == turn_id]
        if len(owned) >= self.max_active_per_turn:
            raise CommandSessionError("active command-session limit for this turn reached")

    @staticmethod
    def _identifier(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
            raise CommandSessionError(f"{name} must be non-empty bounded text")
        return value

    @staticmethod
    def _wait_seconds(value: Any, name: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= COMMAND_SESSION_MAX_YIELD_MS
        ):
            raise CommandSessionError(
                f"{name} must be an integer from 0 to {COMMAND_SESSION_MAX_YIELD_MS}"
            )
        return value / 1000

    @staticmethod
    def _fingerprint(command: str, cwd: Path, timeout: float) -> str:
        payload = json.dumps(
            {"command": command, "cwd": str(cwd), "timeout": timeout},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _validate_state_file(self) -> None:
        if is_link_or_reparse(self._state_file):
            raise CommandSessionError("command-session registry cannot be a link")
        try:
            info = self._state_file.stat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CommandSessionError("command-session registry must be an ordinary, unshared file")

    def _load_recovered(self) -> None:
        if not self._state_file.exists():
            return
        self._validate_state_file()
        if self._state_file.stat().st_size > COMMAND_SESSION_MAX_STATE_BYTES:
            raise CommandSessionError("command-session registry is oversized")
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandSessionError("command-session registry is invalid") from exc
        if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
            raise CommandSessionError("unsupported command-session registry version")
        entries = raw.get("sessions")
        if not isinstance(entries, list):
            raise CommandSessionError("command-session registry has no session list")
        changed = False
        for entry in entries[-self.max_records :]:
            session = self._restore(entry)
            if session is None:
                raise CommandSessionError("command-session registry contains an invalid record")
            if session.id in self._sessions:
                raise CommandSessionError("command-session registry contains duplicate ids")
            if session.status in ACTIVE_STATUSES:
                session.status = "lost"
                session.finished_at = time.time()
                session.duration = max(0.0, session.finished_at - session.started_at)
                session.error = (
                    "runtime restarted while the command was active; its outcome is uncertain "
                    "and it will not be replayed"
                )
                session.done.set()
                changed = True
            else:
                session.done.set()
            self._sessions[session.id] = session
        if changed:
            self._persist()

    def _restore(self, entry: Any) -> _Session | None:
        if not isinstance(entry, dict):
            return None
        required = ("id", "threadId", "turnId", "callId", "fingerprint", "status", "startedAt")
        if not all(isinstance(entry.get(key), str) and entry[key] for key in required[:-1]):
            return None
        started_at = entry.get("startedAt")
        status = entry.get("status")
        if (
            isinstance(started_at, bool)
            or not isinstance(started_at, int | float)
            or not math.isfinite(started_at)
            or status not in ACTIVE_STATUSES | TERMINAL_STATUSES
        ):
            return None
        tail = entry.get("outputTail", "")
        output_end = entry.get("outputChars", len(tail))
        if not isinstance(tail, str) or not isinstance(output_end, int) or output_end < len(tail):
            return None
        tail = tail[-self.max_output_chars :]
        session = _Session(
            id=entry["id"],
            thread_id=entry["threadId"],
            turn_id=entry["turnId"],
            call_id=entry["callId"],
            fingerprint=entry["fingerprint"],
            status=status,
            started_at=float(started_at),
            finished_at=self._optional_number(entry.get("finishedAt")),
            exit_code=entry.get("exitCode") if isinstance(entry.get("exitCode"), int) else None,
            duration=self._optional_number(entry.get("duration")),
            timed_out=entry.get("timedOut") is True,
            cancelled=entry.get("cancelled") is True,
            error=entry.get("error") if isinstance(entry.get("error"), str) else None,
            callback_error=(
                entry.get("callbackError")
                if isinstance(entry.get("callbackError"), str)
                else None
            ),
            buffer=tail,
            buffer_start=output_end - len(tail),
            output_end=output_end,
            cursor=output_end - len(tail),
            output_truncated=(
                entry.get("outputTruncated") is True or output_end > len(tail)
            ),
        )
        return session

    @staticmethod
    def _optional_number(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            return None
        return float(value)

    def _prune(self) -> None:
        if len(self._sessions) <= self.max_records:
            return
        terminal = sorted(
            (s for s in self._sessions.values() if s.status in TERMINAL_STATUSES),
            key=lambda session: session.finished_at or session.started_at,
        )
        for session in terminal:
            if len(self._sessions) <= self.max_records:
                break
            self._sessions.pop(session.id, None)

    def _persist(self) -> None:
        self._validate_state_file()
        entries = []
        for session in self._sessions.values():
            entries.append(
                {
                    "id": session.id,
                    "threadId": session.thread_id,
                    "turnId": session.turn_id,
                    "callId": session.call_id,
                    "fingerprint": session.fingerprint,
                    "status": session.status,
                    "startedAt": session.started_at,
                    "finishedAt": session.finished_at,
                    "exitCode": session.exit_code,
                    "duration": session.duration,
                    "timedOut": session.timed_out,
                    "cancelled": session.cancelled,
                    "error": session.error,
                    "callbackError": session.callback_error,
                    "outputTail": session.buffer[-COMMAND_SESSION_MAX_PERSISTED_OUTPUT_CHARS:],
                    "outputChars": session.output_end,
                    "outputTruncated": session.output_truncated,
                }
            )
        payload = json.dumps(
            {"version": _STATE_VERSION, "sessions": entries},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fd, temporary_name = tempfile.mkstemp(prefix=".sessions.", dir=self.directory)
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_file)
            if os.name != "nt":
                os.chmod(self._state_file, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
