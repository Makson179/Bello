from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, TypeVar

from pydantic import BaseModel

if os.name == "nt":
    import msvcrt as _msvcrt

    _fcntl = None
else:
    import fcntl as _fcntl

    _msvcrt = None

from supervisor.schemas import AppEvent, FinalReport, HealthState, BelloConfig
from supervisor.filesystem_safety import remove_path_tree
from supervisor.review_limits import normalize_review_limit_payload

T = TypeVar("T")

STATE_DIR_NAME = ".supervisor"
CONFIG = "config.json"
PROGRESS = "PROGRESS.md"
DECISIONS = "DECISIONS.md"
LAST_ACTION = "LAST_ACTION.md"
ACTION_HISTORY_LIMIT = 10
HEALTH = "HEALTH.json"
HANDOFF = "HANDOFF.md"
FINAL_REPORT = "FINAL_REPORT.md"
LOG = "log.jsonl"
EVENTS = "events.jsonl"
SUPERVISOR_WAKES = "supervisor_wakes.jsonl"
RUNTIME_TRACE = "runtime_trace.jsonl"
RUNTIME_METRICS = "runtime_metrics.json"
AGENT_SETTINGS = "agent-settings.json"
PREVIOUS_RUNS = "previous_runs"
RECOVERY = "recovery"

INITIALIZATION_MODES = Literal["fresh", "resume"]

_IS_WINDOWS = os.name == "nt"
_WINDOWS_LOCK_POLL_SECONDS = 0.05
_WINDOWS_LOCK_BUSY_ERRNOS = {
    errno.EACCES,
    errno.EAGAIN,
    getattr(errno, "EDEADLK", errno.EACCES),
}
_WINDOWS_LOCK_BUSY_WINERRORS = {32, 33}
_WINDOWS_REPLACE_RETRY_SECONDS = 2.0
_WINDOWS_REPLACE_POLL_SECONDS = 0.05
_WINDOWS_REPLACE_BUSY_WINERRORS = {5, 32, 33}


def require_inside_workspace(workspace: Path, path: Path) -> Path:
    workspace = workspace.resolve()
    resolved = path.resolve() if path.exists() else path.absolute().parent.resolve() / path.name
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc
    return resolved


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "FileLock":
        if self.fd is not None:
            raise RuntimeError(f"file lock is already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _acquire_file_lock(fd)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            fd = self.fd
            try:
                _release_file_lock(fd)
            finally:
                os.close(fd)
                self.fd = None


def _acquire_file_lock(fd: int) -> None:
    if not _IS_WINDOWS:
        if _fcntl is None:  # pragma: no cover - protects an invalid platform import state
            raise RuntimeError("POSIX file locking is unavailable")
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        return

    if _msvcrt is None:  # pragma: no cover - msvcrt is part of supported Windows Python
        raise RuntimeError("Windows file locking is unavailable")

    # msvcrt locks a byte range starting at the current file position.  A
    # persistent sentinel byte makes the range valid without truncating an
    # existing lock file.  Another creator can populate and lock the empty
    # file between fstat() and write(); Windows then reports the write as a
    # sharing violation.  Treat that narrow race exactly like lock
    # contention and re-check the file after the holder makes progress.
    while os.fstat(fd).st_size == 0:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            written = os.write(fd, b"\0")
        except OSError as exc:
            if not _is_windows_lock_contention(exc):
                raise
            time.sleep(_WINDOWS_LOCK_POLL_SECONDS)
            continue
        if written != 1:  # pragma: no cover - regular files must accept one byte or fail
            raise OSError(errno.EIO, "failed to initialize Windows lock file")

    while True:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if not _is_windows_lock_contention(exc):
                raise
            # LK_LOCK gives up after a fixed number of retries.  Polling the
            # non-blocking operation preserves flock's indefinite wait
            # semantics for long-running Bello processes.
            time.sleep(_WINDOWS_LOCK_POLL_SECONDS)


def _release_file_lock(fd: int) -> None:
    if not _IS_WINDOWS:
        if _fcntl is None:  # pragma: no cover - protects an invalid platform import state
            raise RuntimeError("POSIX file locking is unavailable")
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return

    if _msvcrt is None:  # pragma: no cover - msvcrt is part of supported Windows Python
        raise RuntimeError("Windows file locking is unavailable")
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def _is_windows_lock_contention(exc: OSError) -> bool:
    return exc.errno in _WINDOWS_LOCK_BUSY_ERRNOS or getattr(exc, "winerror", None) in _WINDOWS_LOCK_BUSY_WINERRORS


def _atomic_replace(source: str, destination: Path) -> None:
    """Replace a state file, tolerating only transient Windows sharing locks."""

    deadline = time.monotonic() + _WINDOWS_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            retryable = (
                _IS_WINDOWS
                and (
                    exc.errno in {errno.EACCES, errno.EAGAIN}
                    or getattr(exc, "winerror", None) in _WINDOWS_REPLACE_BUSY_WINERRORS
                )
            )
            if not retryable or time.monotonic() >= deadline:
                raise
            time.sleep(_WINDOWS_REPLACE_POLL_SECONDS)


class StateStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.state_dir = require_inside_workspace(self.workspace, self.workspace / STATE_DIR_NAME)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return require_inside_workspace(self.workspace, self.state_dir / name)

    def lock_path(self, name: str) -> Path:
        return self.path(f"{name}.lock")

    @contextmanager
    def locked(self, name: str) -> Iterator[None]:
        with FileLock(self.lock_path(name)):
            yield

    def atomic_write_text(self, path: Path, text: str) -> None:
        require_inside_workspace(self.workspace, path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def atomic_write_json(self, path: Path, data: Any) -> None:
        if isinstance(data, BaseModel):
            text = data.model_dump_json(indent=2)
        else:
            text = json.dumps(data, indent=2, sort_keys=True)
        self.atomic_write_text(path, text + "\n")

    def read_text(self, name: str, default: str = "") -> str:
        path = self.path(name)
        if not path.exists():
            return default
        return path.read_text(encoding="utf-8")

    def write_text_locked(self, name: str, text: str) -> None:
        with self.locked(name):
            self.atomic_write_text(self.path(name), text)

    def append_text_locked(self, name: str, text: str) -> None:
        with self.locked(name):
            current = self.read_text(name, "")
            self.atomic_write_text(self.path(name), current + text)

    def read_recent_actions(self, limit: int = ACTION_HISTORY_LIMIT) -> list[str]:
        return _recent_action_lines(self.read_text(LAST_ACTION, ""), limit=limit)

    def append_recent_action(self, summary: str, limit: int = ACTION_HISTORY_LIMIT) -> None:
        if limit <= 0:
            return
        summary = " ".join(summary.strip().split())[:500]
        if not summary:
            return
        with self.locked(LAST_ACTION):
            actions = _recent_action_lines(self.read_text(LAST_ACTION, ""), limit=limit - 1)
            actions.append(summary)
            self.atomic_write_text(self.path(LAST_ACTION), "\n".join(actions[-limit:]) + "\n")

    def read_json(self, name: str, default: Any) -> Any:
        path = self.path(name)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def write_json_locked(self, name: str, data: Any) -> None:
        with self.locked(name):
            self.atomic_write_json(self.path(name), data)

    def get_health(self) -> HealthState:
        return HealthState.model_validate(self.read_json(HEALTH, HealthState().model_dump()))

    def patch_health(self, patcher: Callable[[HealthState], HealthState]) -> HealthState:
        with self.locked(HEALTH):
            health = self.get_health()
            updated = patcher(health)
            self.atomic_write_json(self.path(HEALTH), updated)
            return updated

    def write_handoff(self, content: str) -> None:
        self.write_text_locked(HANDOFF, content)

    def initialize_bello(
        self,
        config: BelloConfig,
        overwrite: bool = False,
        *,
        mode: INITIALIZATION_MODES | None = None,
    ) -> None:
        mode = mode or ("fresh" if overwrite else "resume")
        if mode == "fresh":
            self._clear_state_dir(preserve=set())
        elif mode == "resume":
            self._clear_state_dir(preserve={EVENTS, LOG, PREVIOUS_RUNS, RECOVERY})
        else:
            raise ValueError(f"unknown bello initialization mode: {mode}")

        files = self._initial_state_files(config)
        for name, value in files.items():
            path = self.path(name)
            if name in {EVENTS, LOG} and mode == "resume" and path.exists():
                continue
            if isinstance(value, BaseModel):
                self.atomic_write_json(path, value)
            else:
                self.atomic_write_text(path, value)
        self.ensure_previous_runs_dir()

    def _initial_state_files(self, config: BelloConfig) -> dict[str, Any]:
        return {
            CONFIG: config,
            HEALTH: HealthState(generation=config.generation, restart_count=config.restart_count),
            PROGRESS: "# Progress\n\n- Current step: not started\n- Completed steps: none\n- Known issues: none\n",
            DECISIONS: "# Decisions\n\n",
            LAST_ACTION: "",
            HANDOFF: "",
            FINAL_REPORT: "",
            LOG: "",
            EVENTS: "",
            SUPERVISOR_WAKES: "",
            RUNTIME_TRACE: "",
            RUNTIME_METRICS: "{}\n",
        }

    def _clear_state_dir(self, *, preserve: set[str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for child in self.state_dir.iterdir():
            if child.name in preserve:
                continue
            try:
                metadata = child.lstat()
            except FileNotFoundError:
                continue
            remove_path_tree(child, stat_result=metadata)

    def ensure_previous_runs_dir(self) -> Path:
        path = self.path(PREVIOUS_RUNS)
        if path.exists() and not path.is_dir():
            path.unlink()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def next_recovery_dir(self) -> Path:
        recovery = self.path(RECOVERY)
        if recovery.exists() and not recovery.is_dir():
            recovery.unlink()
        recovery.mkdir(parents=True, exist_ok=True)
        max_run = 0
        for child in recovery.iterdir():
            if not child.is_dir() or not child.name.startswith("run"):
                continue
            suffix = child.name[3:]
            if suffix.isdigit():
                max_run = max(max_run, int(suffix))
        return recovery / f"run{max_run + 1}"

    def archive_completed_run(self, task_path: Path) -> Path:
        previous_runs = self.ensure_previous_runs_dir()
        run_dir = self._next_previous_run_dir(previous_runs)
        run_dir.mkdir()
        task_source = require_inside_workspace(self.workspace, task_path)
        final_report_source = self.path(FINAL_REPORT)
        shutil.copyfile(task_source, run_dir / "task.md")
        shutil.copyfile(final_report_source, run_dir / FINAL_REPORT)
        return run_dir

    def _next_previous_run_dir(self, previous_runs: Path) -> Path:
        max_run = 0
        for child in previous_runs.iterdir():
            if not child.is_dir() or not child.name.startswith("run"):
                continue
            suffix = child.name[3:]
            if suffix.isdigit():
                max_run = max(max_run, int(suffix))
        return previous_runs / f"run{max_run + 1}"

    def get_bello_config(self) -> BelloConfig:
        return BelloConfig.model_validate(normalize_review_limit_payload(self.read_json(CONFIG, {})))

    def update_bello_config(self, patcher: Callable[[BelloConfig], BelloConfig]) -> BelloConfig:
        with self.locked(CONFIG):
            config = self.get_bello_config()
            updated = patcher(config)
            self.atomic_write_json(self.path(CONFIG), updated)
            return updated

    def append_event(self, event: AppEvent) -> None:
        self.append_text_locked(EVENTS, event.model_dump_json() + "\n")

    def max_event_sequence(self) -> int:
        max_sequence = 0
        for line in self.read_text(EVENTS, "").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            sequence = payload.get("sequence")
            if isinstance(sequence, int) and sequence > max_sequence:
                max_sequence = sequence
        return max_sequence

    def append_raw_log(self, entry: dict[str, Any]) -> None:
        self.append_text_locked(LOG, json.dumps(entry, default=str, sort_keys=True) + "\n")

    def append_supervisor_wake(self, entry: dict[str, Any]) -> None:
        self.append_text_locked(SUPERVISOR_WAKES, json.dumps(entry, default=str, sort_keys=True) + "\n")

    def append_runtime_trace(self, entry: dict[str, Any]) -> None:
        self.append_text_locked(RUNTIME_TRACE, json.dumps(entry, default=str, sort_keys=True) + "\n")

    def update_runtime_metrics(self, patcher: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        with self.locked(RUNTIME_METRICS):
            current = self.read_json(RUNTIME_METRICS, {})
            if not isinstance(current, dict):
                current = {}
            updated = patcher(dict(current))
            self.atomic_write_json(self.path(RUNTIME_METRICS), updated)
            return updated

    def read_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        raw = self.read_text(EVENTS, "")
        lines = [line for line in raw.splitlines() if line.strip()]
        selected = lines[-limit:]
        events: list[dict[str, Any]] = []
        for line in selected:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def write_final_report(self, report: FinalReport | str) -> None:
        if isinstance(report, FinalReport):
            status = report.status.value if hasattr(report.status, "value") else str(report.status)
            lines = [
                "# Final Report",
                "",
                f"- Task: {report.task_path}",
                f"- Status: {status}",
                f"- Result: {report.result}",
                f"- Restarts: {report.restarts}",
                f"- Interventions: {report.interventions}",
            ]
            if report.completion_review_accepted is not None:
                lines.append(
                    f"- Completion review accepted: {str(report.completion_review_accepted).lower()}"
                )
            lines.extend(
                [
                    f"- Completion returns: {report.completion_returns}",
                    f"- Completion restarts: {report.completion_restarts}",
                    f"- No-marker idle nudges: {report.no_marker_idle_nudges}",
                ]
            )
            if report.files_changed:
                lines.extend(["", "## Files Changed", *[f"- {path}" for path in report.files_changed]])
            if report.validations:
                lines.extend(["", "## Validations", *[f"- {item}" for item in report.validations]])
            if report.behavior_evidence_summary:
                lines.extend(["", "## Completion Behavior Evidence", *[f"- {item}" for item in report.behavior_evidence_summary]])
            if report.files_reviewed_summary:
                lines.extend(["", "## Completion Files Reviewed", *[f"- {item}" for item in report.files_reviewed_summary]])
            if report.packet_or_access_limitations:
                lines.extend(["", "## Packet Or Access Limitations", *[f"- {item}" for item in report.packet_or_access_limitations]])
            if report.adversary_reports:
                lines.extend(["", "## Adversary Reports", *[f"- {item}" for item in report.adversary_reports]])
            if report.denied_actions:
                lines.extend(["", "## Denied Actions", *[f"- {item}" for item in report.denied_actions]])
            if report.remaining_risks:
                lines.extend(["", "## Remaining Risks", *[f"- {item}" for item in report.remaining_risks]])
            if report.diff_summary:
                lines.extend(["", "## Diff Summary", "", "```text", report.diff_summary.strip(), "```"])
            self.write_text_locked(FINAL_REPORT, "\n".join(lines).rstrip() + "\n")
        else:
            self.write_text_locked(FINAL_REPORT, report)


def _recent_action_lines(text: str, *, limit: int) -> list[str]:
    if limit <= 0:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()][-limit:]
