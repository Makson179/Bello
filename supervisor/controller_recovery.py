"""Durable, fail-closed restoration of a logical controller run.

The record is controller authority, outside the coder snapshot's writable root.
It is not an instruction to replay a command. Ambiguous transitions, pending
tools, reviews, and final application remain blocked for manual recovery.
"""
from __future__ import annotations

from collections import deque
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import asdict
from functools import lru_cache, wraps
import hashlib
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import sys
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from supervisor.filesystem_safety import is_link_or_reparse
from supervisor.schemas import (AdversaryReport, BelloConfig, BelloStatus,
    ChangedFile, CoderMessage, CompletionReturnRecord, CompletionReviewDecision,
    HealthState, InspectionRun, PriorIntervention, ValidationRun)
from supervisor.state import (CONFIG, HEALTH, EVENTS, DECISIONS, PROGRESS, HANDOFF,
    LAST_ACTION)


class RecoveryBlocked(RuntimeError):
    """Old state must be preserved; no automatic continuation is authorized."""


def recovery_root(project_root: Path) -> Path:
    root = project_root.resolve()
    for path in (root / ".supervisor", root / ".supervisor" / "controller"):
        if is_link_or_reparse(path):
            raise RecoveryBlocked("controller recovery directory cannot be a link or reparse point")
        if path.exists() and not path.is_dir():
            raise RecoveryBlocked("controller recovery directory is not a directory")
    return root / ".supervisor" / "controller"


_OWNERS: dict[tuple[int, str], tuple[int, int, str, bool]] = {}
_OWNER_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar("bello_run_owner", default=None)


class RunOwner(AbstractContextManager):
    """Nonblocking process-lifetime lock; nested CLI/controller calls share it."""

    def __init__(self, project_root: Path, *, controller: bool = False):
        self.root = recovery_root(project_root)
        self.key = (os.getpid(), str(self.root))
        self.entered = False
        self.controller = controller
        self.context_reset = None

    def __enter__(self):
        if self.entered:
            raise RecoveryBlocked("this controller ownership context is already entered")
        existing = _OWNERS.get(self.key)
        if existing:
            if (_OWNER_CONTEXT.get() or {}).get(str(self.root)) != existing[2]:
                raise RecoveryBlocked("another execution context owns this workspace")
            if self.controller and existing[3]:
                raise RecoveryBlocked("another Bello controller owns this workspace")
            _OWNERS[self.key] = (existing[0], existing[1] + 1, existing[2], existing[3] or self.controller)
            self.entered = True
            return self
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / "owner.lock"
        _ordinary_file(path, missing_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
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
            raise RecoveryBlocked("another Bello controller owns this workspace") from exc
        try:
            _check_monitor(self.root)
        except BaseException:
            os.close(fd)
            raise
        token = secrets.token_hex(32)
        _OWNERS[self.key] = (fd, 1, token, self.controller)
        self.context_reset = _OWNER_CONTEXT.set({**(_OWNER_CONTEXT.get() or {}), str(self.root): token})
        self.entered = True
        return self

    def __exit__(self, *exc):
        if not self.entered:
            return
        self.entered = False
        fd, count, token, claimed = _OWNERS[self.key]
        if count > 1:
            _OWNERS[self.key] = (fd, count - 1, token, claimed and not self.controller)
            return
        del _OWNERS[self.key]
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            if self.context_reset is not None:
                _OWNER_CONTEXT.reset(self.context_reset)


def _check_monitor(root: Path) -> None:
    """A watchdog also owns the gaps between its worker processes."""
    path = root / "watchdog.lock"
    if not path.exists():
        return
    _ordinary_file(path)
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            from supervisor.process_fence import is_guarded_worker
            if not is_guarded_worker():
                raise RecoveryBlocked("an active watchdog owns this logical run") from exc
    finally:
        os.close(fd)


def _ordinary_file(path: Path, *, missing_ok: bool = False) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise RecoveryBlocked(f"required recovery file is missing: {path.name}")
    if is_link_or_reparse(path, stat_result=info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RecoveryBlocked(f"recovery file must be an unshared regular file: {path.name}")


def _digest(path: Path) -> str:
    _ordinary_file(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def implementation_fingerprint() -> str:
    """Bind source checkouts too, not merely the advertised package version."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256(sys.version.encode())
    for directory, children, filenames in os.walk(root):
        children[:] = sorted(name for name in children if name not in {"node_modules", "__pycache__"})
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.suffix in {".py", ".mjs", ".json", ".md", ".txt", ".toml"}:
                digest.update(str(path.relative_to(root)).replace(os.sep, "/").encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    # Importlib enumerates one distribution again for every duplicate sys.path
    # entry (pytest/plugin discovery commonly adds those). Bind installations,
    # not enumeration multiplicity; distinct roots and versions remain distinct.
    versions = sorted({(re.sub(r"[-_.]+", "-", item.metadata.get("Name", "").lower()),
                        item.version, str(Path(item.locate_file("")).resolve()))
                       for item in distributions()})
    digest.update(json.dumps(versions, separators=(",", ":")).encode())
    return digest.hexdigest()


@lru_cache(maxsize=4)
def _worker_inputs(root: Path) -> tuple[Path, ...]:
    paths = []
    for directory, children, filenames in os.walk(root):
        children[:] = sorted(name for name in children if name != "node_modules")
        paths.extend(Path(directory) / name for name in filenames if name.endswith(".mjs"))
    return tuple(paths)


def effective_prompt_identity() -> dict[str, str]:
    from importlib.resources import files
    from supervisor.prompts.supervisor import PROMPTS_ENV_VAR, PROMPTS_RESOURCE
    override = os.environ.get(PROMPTS_ENV_VAR)
    path = Path(override) if override else Path(str(files("supervisor.prompts").joinpath(PROMPTS_RESOURCE)))
    return {"source": str(path.absolute()), "resolved": str(path.resolve(strict=True)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _planned_codex_command(config: BelloConfig) -> tuple[list[str], Path | None]:
    """Mirror the installer's selection only: no install, lock, mkdir or probe."""
    from supervisor.runtime.native_codex_install import ASYNC_BUNDLES, BUNDLES, _platform_key
    binary = os.environ.get("BELLO_CODEX_BINARY")
    manifest = os.environ.get("BELLO_CODEX_SELECTION_MANIFEST")
    if config.async_tools:
        binary, manifest = (binary or "").strip(), (manifest or "").strip()
    if config.async_tools or config.log_distiller.get("enabled", False):
        if binary or manifest:
            return ([binary or "codex", "app-server", "--listen", "stdio://"],
                    Path(manifest).expanduser().absolute() if manifest else None)
        key = _platform_key()
        bundle = (ASYNC_BUNDLES if config.async_tools else BUNDLES).get(key)
        if bundle is not None:
            base = Path(os.environ.get("BELLO_RUNTIME_DIR", str(Path.home() / ".bello/runtime"))).expanduser().absolute()
            directory = base / "native-codex" / bundle.archive_sha256
            executable = directory / "bin" / ("codex.exe" if key[0] == "Windows" else "codex")
            return [str(executable), "app-server", "--listen", "stdio://"], directory / "selection-manifest.json"
        if not config.async_tools:
            raise RecoveryBlocked("native engine selection is unsupported for recovery")
    # The non-installer backend strips only the optional manifest, not binary.
    manifest = (os.environ.get("BELLO_CODEX_SELECTION_MANIFEST") or "").strip()
    return ([os.environ.get("BELLO_CODEX_BINARY", "codex"), "app-server", "--listen", "stdio://"],
            Path(manifest).expanduser().absolute() if manifest else None)


def _verify_planned_engine_selection(controller: Any, config: BelloConfig,
                                     expected: dict[str, dict[str, str]]) -> None:
    """Reject a changed selector before a provider process or recovery write."""
    from supervisor.executables import require_trusted_executable
    from supervisor.runtime.client import RuntimeClient
    client = controller.client
    if not isinstance(client, RuntimeClient):
        if _engine_identity(client, {}) != expected:
            raise RecoveryBlocked("selected execution engine changed before recovery")
        return
    try:
        for name, entry in expected.items():
            backend = client._engines.get(name)
            manifest = None
            if backend is not None:
                # Explicit injected engines must have a statically inspectable
                # command. Do not initialize one to discover its selection.
                command = getattr(backend, "_native_command", None) or getattr(backend, "_command", None) or getattr(backend, "command", None)
                manifest = getattr(backend, "_selection_manifest", None)
                backend_type = type(backend).__module__ + "." + type(backend).__qualname__
                if not command:
                    raise RecoveryBlocked("injected engine selection cannot be resolved without startup")
            elif name == "codex":
                command, manifest = _planned_codex_command(config)
                backend_type = "supervisor.runtime.codex.CodexBackend"
            elif name == "claude-code":
                from supervisor.runtime.claude import ClaudeBackend
                command = [str(ClaudeBackend._official_cli(prepare=False).path)]
                backend_type = "supervisor.runtime.claude.ClaudeBackend"
            elif name == "pi":
                from supervisor.runtime.install import worker_directory
                node = require_trusted_executable(os.environ.get("BELLO_NODE", "node"), cwd=Path.cwd())
                command = [node, str(worker_directory() / "worker.mjs")]
                backend_type = "supervisor.runtime.transport.WorkerTransport"
            else:
                raise RecoveryBlocked("execution engine has no read-only recovery selector")
            executable = str(Path(require_trusted_executable(command[0], cwd=client.cwd)).resolve(strict=True))
            selected_manifest = str(Path(manifest).resolve(strict=True)) if manifest else None
            if (entry.get("backend") != backend_type or entry.get("command") != json.dumps(command, separators=(",", ":"))
                    or entry.get("executable") != executable or entry.get("manifest") != selected_manifest):
                raise RecoveryBlocked("selected execution engine changed before recovery")
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, RecoveryBlocked):
            raise
        raise RecoveryBlocked("execution engine selection cannot be verified without startup") from exc


def _engine_identity(client: Any, cache: dict[str, str]) -> dict[str, dict[str, str]]:
    """Capture selected launchers, manifests and worker inputs once per owner.

    Restoration always hashes disk afresh. The live-owner cache deliberately
    never adopts changed bytes as new authority for the same running engine.
    """
    engines = getattr(client, "_engines", None)
    engines = engines if isinstance(engines, dict) else {"client": client}
    result = {}
    for name, backend in engines.items():
        entry = {"backend": type(backend).__module__ + "." + type(backend).__qualname__}
        command = getattr(backend, "_native_command", None) or getattr(backend, "command", None)
        cli = getattr(backend, "_cli_path", None)
        if cli:
            command = [str(cli)]
        paths = []
        if command:
            entry["command"] = json.dumps(command, separators=(",", ":"))
            executable = shutil.which(command[0])
            if executable is None:
                raise RecoveryBlocked("selected engine executable cannot be identified")
            paths.append(Path(executable).resolve(strict=True))
            entry["executable"] = str(paths[0])
            if name in {"codex", "client"} and entry["backend"].startswith("supervisor."):
                with paths[0].open("rb") as stream:
                    header = stream.read(4)
                if not (header.startswith(b"\x7fELF") or header.startswith(b"MZ") or
                        header in {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}):
                    # An arbitrary launcher can delegate to an unbound binary.
                    # Normal execution works, but it is not resumable authority.
                    entry["unverified_launcher"] = "native delegate is not bound"
            paths.extend(Path(value).resolve(strict=True) for value in command[1:]
                         if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file())
        manifest = getattr(backend, "_selection_manifest", None)
        if manifest:
            paths.append(Path(manifest).resolve(strict=True))
            entry["manifest"] = str(paths[-1])
        if name == "pi" and command and len(command) > 1:
            worker_root = Path(command[1]).resolve().parent
            paths.extend(_worker_inputs(worker_root))
            paths.extend(path for path in (worker_root / "package.json", worker_root / "package-lock.json",
                worker_root / "node_modules/@earendil-works/pi-coding-agent/package.json") if path.is_file())
        for path in sorted(set(paths)):
            key = str(path)
            if key not in cache:
                with path.open("rb") as stream:
                    cache[key] = hashlib.file_digest(stream, "sha256").hexdigest()
            entry["file:" + key] = cache[key]
        result[name] = entry
    return result


def _verify_engine_files(identity: dict[str, dict[str, str]]) -> None:
    for entry in identity.values():
        for key, expected in entry.items():
            if key.startswith("file:"):
                path = Path(key[5:])
                if not path.is_absolute() or is_link_or_reparse(path) or not path.is_file():
                    raise RecoveryBlocked("execution-engine identity is unavailable")
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != expected:
                    raise RecoveryBlocked("execution-engine files changed since the checkpoint")


class ControllerState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    completion_attempt_count: int = Field(ge=0)
    completion_restarts: int = Field(ge=0)
    no_marker_idle_nudge_count: int = Field(ge=0)
    provider_failure_recovery_counts: dict[str, int]
    validations: list[ValidationRun]
    inspections: list[InspectionRun]
    prior_interventions: list[PriorIntervention]
    completion_returns: list[CompletionReturnRecord]
    observed_changed_files: dict[str, ChangedFile]
    last_coder_message: CoderMessage | None
    completion_review_return_sequence: int | None
    validation_runtime_state: dict[str, dict[str, Any]]
    scalars: dict[str, int | str | bool | None]
    completion_knowledge: dict[str, list[Any]]
    accepted_completion: CompletionReviewDecision | None
    accepted_adversary: AdversaryReport | None
    pending_adversary: AdversaryReport | None
    readiness_journal: list[dict[str, Any]]
    handled_completions: list[list[Any]]
    subagents: list[dict[str, Any]]
    observed_items: dict[str, dict[str, Any]]


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[2] = 2
    run_id: str
    implementation: str
    prompt_identity: dict[str, str]
    engine_identity: dict[str, dict[str, str]]
    owner_pid: int = Field(gt=0)
    owner_epoch: str
    project_root: str
    task_path: str
    task_hash: str
    plan_path: str | None
    plan_hash: str | None
    sandbox: str
    runtime_state_dir: str | None
    runtime_tools_digest: str | None
    phase: str
    eligible: bool
    reason: str
    terminal: bool
    clean_shutdown: bool = False
    config: dict[str, Any]
    health: dict[str, Any]
    state_files: dict[str, str]
    snapshot_digest: str | None
    controller: ControllerState


_STATE_FILES = (CONFIG, HEALTH, EVENTS, DECISIONS, PROGRESS, HANDOFF, LAST_ACTION)
_MUTABLE_CONFIG = {
    "start_over", "clean",
    "coder_thread_id", "active_coder_turn_id", "generation", "restart_count",
    "last_event_sequence", "last_applied_supervisor_sequence", "pending_server_request_ids", "status",
    "codex_version", "appserver_schema_hash", "runtime_name", "runtime_protocol_version",
    "revision_coder_active", "adversary_run_count", "completion_return_count",
    "completion_returns_since_adversary", "last_relevant_edit_sequence", "last_validation_sequence",
    "last_trusted_behavioral_validation_sequence", "last_trusted_passing_behavioral_validation_sequence",
}
_SCALARS = (
    "_generation_has_coder_turn", "_current_turn_action_count", "_last_completion_marker_sequence",
    "_no_marker_completion_review_key", "_last_large_diff_signature", "_last_restart_budget_signature",
    "_last_suspicious_file_signature", "_runtime_apply_retry_count", "_runtime_decision_retry_count",
    "_transport_recovery_total",
)
_TERMINAL = {BelloStatus.COMPLETE, BelloStatus.ESCALATED, BelloStatus.STUCK,
             BelloStatus.PROVIDER_FAILURE, BelloStatus.EXITED}


def _read_record(project_root: Path) -> RunRecord | None:
    path = recovery_root(project_root) / "run.json"
    if not path.exists():
        return None
    _ordinary_file(path)
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("oversized recovery record")
        def unique_pairs(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate recovery key")
                value[key] = item
            return value
        raw = json.loads(path.read_bytes(), object_pairs_hook=unique_pairs)
        if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 2:
            raise ValueError("unsupported recovery schema")
        record = RunRecord.model_validate_json(json.dumps(raw))
        UUID(record.run_id)
        if not re.fullmatch(r"[0-9a-f]{64}", record.owner_epoch):
            raise ValueError("invalid ownership epoch")
        if not re.fullmatch(r"[0-9a-f]{64}", record.implementation):
            raise ValueError("invalid implementation identity")
        if (set(record.prompt_identity) != {"source", "resolved", "sha256"}
                or not re.fullmatch(r"[0-9a-f]{64}", record.prompt_identity["sha256"])):
            raise ValueError("invalid prompt identity")
        cfg = BelloConfig.model_validate(record.config)
        health = HealthState.model_validate(record.health)
        for name in ("generation", "restart_count", "last_event_sequence", "last_applied_supervisor_sequence",
                     "adversary_run_count", "completion_return_count", "completion_returns_since_adversary"):
            if type(record.config.get(name)) is not int or record.config[name] < 0:
                raise ValueError("invalid config counter")
        for name, value in record.health.items():
            if name in {"generation", "restart_count", "denied_requests", "consecutive_failed_tests", "repeated_command_count",
                        "interventions", "minutes_without_progress", "last_progress_sequence", "timeout_fallback_count",
                        "parse_failure_count", "restart_issue_interventions", "restart_issue_last_sequence"}:
                if type(value) is not int or value < 0:
                    raise ValueError("invalid health counter")
        if cfg.generation != health.generation or cfg.restart_count != health.restart_count:
            raise ValueError("health generation/counter mismatch")
        if record.terminal != (cfg.status in _TERMINAL) or (record.terminal and (record.eligible or record.phase != "terminal")):
            raise ValueError("inconsistent terminal disposition")
        if record.eligible and (record.phase not in {"coder", "coder_turn_complete"} or cfg.status != BelloStatus.RUNNING):
            raise ValueError("inconsistent continuation disposition")
        if cfg.last_applied_supervisor_sequence > cfg.last_event_sequence:
            raise ValueError("impossible decision sequence")
        evidence = [*record.controller.validations, *record.controller.inspections,
                    *record.controller.prior_interventions, *record.controller.completion_returns,
                    *record.controller.observed_changed_files.values()]
        if record.controller.last_coder_message is not None:
            evidence.append(record.controller.last_coder_message)
        if any(item.sequence is not None and (item.sequence < 0 or item.sequence > cfg.last_event_sequence) for item in evidence):
            raise ValueError("evidence sequence is outside the committed history")
        if record.task_hash != cfg.task_hash or record.project_root != cfg.project_root:
            raise ValueError("inconsistent run identity")
        if set(record.controller.completion_knowledge) != {"behavior_surface", "uncovered_edge_candidates"}:
            raise ValueError("invalid completion knowledge")
        if set(record.state_files) != set(_STATE_FILES) or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in record.state_files.values()):
            raise ValueError("invalid state bindings")
        return record
    except (ValueError, TypeError) as exc:
        raise RecoveryBlocked("invalid or unsupported controller recovery record; preserve it for manual recovery") from exc


def recovery_disposition(project_root: Path) -> dict[str, Any]:
    try:
        record = _read_record(project_root)
    except (RecoveryBlocked, OSError) as exc:
        return {"eligible": False, "reason": str(exc), "terminal": False}
    if record is None:
        return {"eligible": False, "reason": "no durable controller run", "terminal": False}
    return {name: getattr(record, name) for name in
            ("eligible", "reason", "run_id", "owner_pid", "owner_epoch", "terminal", "task_path", "plan_path")}


def _capture(controller: Any) -> ControllerState:
    plain = ("completion_attempt_count", "completion_restarts", "no_marker_idle_nudge_count",
             "provider_failure_recovery_counts", "validations", "inspections", "prior_interventions",
             "completion_returns", "observed_changed_files", "last_coder_message",
             "completion_review_return_sequence", "validation_runtime_state")
    values = {name: getattr(controller, name) for name in plain}
    values.update(
        scalars={name: getattr(controller, name, None) for name in _SCALARS},
        completion_knowledge=controller._completion_knowledge_state,
        accepted_completion=getattr(controller, "_accepted_completion_decision", None),
        accepted_adversary=getattr(controller, "_accepted_adversary_report", None),
        pending_adversary=getattr(controller, "_pending_adversary_report", None),
        readiness_journal=[asdict(item) for item in controller._readiness_event_journal],
        handled_completions=[list(item) for item in getattr(controller, "_handled_coder_completions", ())],
        subagents=[asdict(item) for item in controller._subagents.values()],
        observed_items=getattr(controller, "_recovery_observed_items", {}),
    )
    return ControllerState(**values)


def _restored_values(controller: Any, state: ControllerState) -> dict[str, Any]:
    from supervisor.controller import _ReadinessJournalEvent, SubagentRuntimeState
    special = {"scalars", "completion_knowledge", "accepted_completion", "accepted_adversary",
               "pending_adversary", "readiness_journal", "handled_completions", "subagents", "observed_items"}
    values = {name: getattr(state, name) for name in ControllerState.model_fields.keys() - special}
    if set(state.scalars) != set(_SCALARS):
        raise RecoveryBlocked("controller scalar recovery schema does not match")
    for name in ("_current_turn_action_count", "_runtime_apply_retry_count", "_runtime_decision_retry_count", "_transport_recovery_total"):
        value = state.scalars[name]
        if type(value) is not int or value < 0:
            raise RecoveryBlocked("invalid durable controller counter")
    if type(state.scalars["_generation_has_coder_turn"]) is not bool:
        raise RecoveryBlocked("invalid durable coder generation state")
    if any(type(value) is not int or value < 0 for value in state.provider_failure_recovery_counts.values()):
        raise RecoveryBlocked("invalid durable provider recovery counter")
    for key, item in state.observed_items.items():
        parts = json.loads(key)
        if (not isinstance(parts, list) or len(parts) != 3 or any(not isinstance(part, str) or not part for part in parts)
                or set(item) != {"type", "status", "exitCode"}):
            raise RecoveryBlocked("invalid durable provider item identity")
    values.update(state.scalars)
    values["_completion_knowledge_state"] = state.completion_knowledge
    values["_accepted_completion_decision"] = state.accepted_completion
    values["_accepted_adversary_report"] = state.accepted_adversary
    values["_pending_adversary_report"] = state.pending_adversary
    values["_readiness_event_journal"] = deque(
        (_ReadinessJournalEvent(**item) for item in state.readiness_journal),
        maxlen=controller._readiness_event_journal.maxlen)
    values["_handled_coder_completions"] = deque((tuple(item) for item in state.handled_completions), maxlen=128)
    values["_subagents"] = {item["thread_id"]: SubagentRuntimeState(**item) for item in state.subagents}
    values["_recovery_observed_items"] = state.observed_items
    return values


class DurableRun:
    def __init__(self, controller: Any):
        self.controller = controller
        self.root = recovery_root(controller.project_root)
        self.run_id = str(uuid4())
        self.owner_epoch = os.environ.pop("BELLO_WATCHDOG_FENCE_TOKEN", None) or secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-f]{64}", self.owner_epoch):
            raise RecoveryBlocked("invalid controller ownership epoch")
        self.transition_depth = 0
        self.restored = False
        self.record: RunRecord | None = None
        self.engine_file_cache: dict[str, str] = {}
        self.expected_engines: dict[str, dict[str, str]] | None = None
        self.prompt_identity = effective_prompt_identity()
        self.prompt_identity_changed = False

    def reset_new_run(self) -> None:
        for name in ("run.json", "snapshot.json", "snapshot-apply.json"):
            path = self.root / name
            _ordinary_file(path, missing_ok=True)
            path.unlink(missing_ok=True)

    def initialize(self, expected: BelloConfig, *, fresh: bool) -> bool:
        c = self.controller
        if fresh:
            return False
        record = _read_record(c.project_root)
        if record is None:
            previous = c.store.get_bello_config() if c.store.path(CONFIG).exists() else None
            old_checkpoint = c.store.get_run_checkpoint()
            if previous and previous.status not in _TERMINAL and (
                previous.coder_thread_id or old_checkpoint or previous.status != BelloStatus.STARTING
            ):
                raise RecoveryBlocked("legacy interrupted run has no trusted recovery authority; use its recovery export or explicit --start-over")
            return False
        if record.terminal:
            # Completed state can start a new run, but a corrupted terminal
            # marker must never be used as authorization to erase old state.
            if _digest(c.store.path(CONFIG)) != record.state_files[CONFIG]:
                raise RecoveryBlocked("terminal config no longer matches its recovery record; use explicit --start-over")
            return False
        if not c.recovery_enabled:
            raise RecoveryBlocked("unfinished run requires recovery; use explicit --start-over for a new run")
        from supervisor.coder import coder_sandbox_mode
        if coder_sandbox_mode() == "danger-full-access":
            raise RecoveryBlocked("full-access coder can modify recovery authority; automatic recovery is not trusted")
        if record.implementation != implementation_fingerprint():
            raise RecoveryBlocked("Bello code or dependency environment changed; the old run requires manual recovery")
        if record.prompt_identity != self.prompt_identity:
            raise RecoveryBlocked("effective prompt source or contents changed; the old run requires manual recovery")
        _verify_engine_files(record.engine_identity)
        _verify_planned_engine_selection(c, expected, record.engine_identity)
        if not record.eligible:
            raise RecoveryBlocked(f"automatic run recovery is blocked: {record.reason}; preserved state requires manual recovery")
        if c.clean_workspace:
            raise RecoveryBlocked("--clean cannot be combined with recovery of an unfinished run; use explicit --start-over for a new run")
        if (self.root / "snapshot-apply.json").exists():
            raise RecoveryBlocked("final workspace application has started; its result/report must be inspected before recovery")
        from supervisor.coder import coder_sandbox_mode
        checks = (
            record.project_root == str(c.project_root), record.task_path == str(c.task_path),
            record.task_hash == c._canonical_task_hash, record.sandbox == coder_sandbox_mode(),
            record.plan_path == (str(c.plan_path) if c.plan_path else None),
            record.plan_hash == (_digest(c.plan_path) if c.plan_path else None),
            record.runtime_state_dir == (str(c.client.state_dir.resolve()) if hasattr(c.client, "state_dir") else None),
        )
        if not all(checks):
            raise RecoveryBlocked("run project/task/plan/sandbox/runtime identity changed")
        desired = expected.model_dump(mode="json")
        if any(record.config.get(key) != value for key, value in desired.items() if key not in _MUTABLE_CONFIG):
            raise RecoveryBlocked("effective run configuration changed; cannot continue the old run")
        if set(record.state_files) != set(_STATE_FILES):
            raise RecoveryBlocked("incomplete recovery state-file binding")
        if any(_digest(c.store.path(name)) != digest for name, digest in record.state_files.items()):
            raise RecoveryBlocked("run state changed after its last complete checkpoint")
        cfg = c.store.get_bello_config()
        if cfg.model_dump(mode="json") != record.config or c.store.get_health().model_dump(mode="json") != record.health:
            raise RecoveryBlocked("config or health do not match the durable checkpoint")
        if cfg.last_event_sequence != c.store.max_event_sequence() or cfg.last_applied_supervisor_sequence > cfg.last_event_sequence:
            raise RecoveryBlocked("event/decision sequence is inconsistent")
        try:
            restored_values = _restored_values(c, record.controller)
        except (TypeError, KeyError, ValueError) as exc:
            raise RecoveryBlocked("invalid durable controller state") from exc
        # A successful backend stop is not proof that detached descendants are
        # dead. Both orderly and abrupt exits require the guardian's tree fence.
        from supervisor.watchdog import validate_recovery_permit
        validate_recovery_permit(c.project_root, run_id=record.run_id,
                                 owner_pid=record.owner_pid, owner_epoch=record.owner_epoch)
        _reject_uncertain_tools(record.runtime_state_dir, expected_digest=record.runtime_tools_digest)
        if record.snapshot_digest is None:
            raise RecoveryBlocked("run has no restorable trusted workspace snapshot")
        from supervisor.snapshot_recovery import restore_snapshot_authority
        snapshot = restore_snapshot_authority(self.root / "snapshot.json", run_id=record.run_id,
                                             expected_digest=record.snapshot_digest, project_root=c.project_root)
        # All persistent validation completes before replacing any live state.
        for name, value in restored_values.items():
            setattr(c, name, value)
        c._coder_snapshot = snapshot
        c.workspace_root = snapshot.snapshot_root
        c.workspace_task_path = snapshot.task_path
        c.workspace_plan_path = snapshot.plan_path
        c._coder_started = True
        c._sequence = cfg.last_event_sequence
        self.run_id, self.record, self.restored = record.run_id, record, True
        self.expected_engines = record.engine_identity
        return True

    def verify_engine_selection(self) -> None:
        if self.expected_engines is None:
            return
        actual = _engine_identity(self.controller.client, self.engine_file_cache)
        if actual != self.expected_engines:
            raise RecoveryBlocked("selected execution engine changed; old work will not be replayed")

    def _unsafe_reason(self, phase: str) -> str | None:
        c = self.controller
        from supervisor.coder import coder_sandbox_mode
        if coder_sandbox_mode() == "danger-full-access":
            return "full-access coder can modify recovery authority"
        if not c.recovery_enabled:
            return "automatic controller recovery is disabled"
        if self.transition_depth:
            return "controller transition was interrupted"
        if phase not in {"coder", "coder_turn_complete", "paused"}:
            return f"phase {phase} has no automatic recovery boundary"
        if getattr(c, "_finalizing", False) or getattr(c, "_snapshot_patch_applied", False):
            return "final application or report outcome requires inspection"
        if c.pending_approvals:
            return "an approval request was pending; grants cannot be replayed"
        if c.store.get_bello_config().status == BelloStatus.PAUSED:
            return "run was intentionally paused; automatic work must not resume it"
        if not c.event_queue.empty():
            return "provider notifications remain unprocessed"
        for name in ("_active_supervisor_check", "_supervisor_next_runtime_check", "_supervisor_next_completion_check",
                     "_deferred_completion_check", "_active_adversary_thread_id", "_pending_adversary_report",
                     "_revision_switch_in_progress", "_restart_transition_token"):
            if getattr(c, name, None):
                return f"unfinished controller work: {name}"
        if c._pending_runtime_trigger_signatures or c._pending_runtime_trigger_actions:
            return "runtime review triggers have not been acknowledged"
        if c._active_coder_subagents():
            return "child work was active at the checkpoint"
        watch = getattr(c, "_coder_watch", None)
        if watch is not None and watch.running_tools:
            return "a dispatched tool had no observed terminal outcome"
        if c._coder_snapshot is None or not c.store.get_bello_config().coder_thread_id:
            return "coder snapshot/thread is not fully initialized"
        return None

    def checkpoint(self, phase: str, *, clean_shutdown: bool = False) -> None:
        c = self.controller
        from supervisor.coder import coder_sandbox_mode
        cfg = c.store.get_bello_config()
        terminal = cfg.status in _TERMINAL
        reason = "terminal run" if terminal else self._unsafe_reason(phase)
        self.prompt_identity_changed |= effective_prompt_identity() != self.prompt_identity
        if self.prompt_identity_changed and not terminal:
            reason = "effective prompt source changed during the run"
        journal = getattr(c.client, "_journal", None)
        tools_digest, uncertain_tools = journal.recovery_tool_state() if journal is not None else (None, False)
        if uncertain_tools and not terminal:
            reason = "a dispatched runtime tool has an uncertain outcome"
        if reason is None and hasattr(c.client, "state_dir") and tools_digest is None:
            reason = "runtime command journal is unavailable"
        engines = _engine_identity(c.client, self.engine_file_cache)
        if reason is None and any("unverified_launcher" in entry for entry in engines.values()):
            reason = "execution-engine launcher delegates to an unbound native binary"
        snapshot_digest = self.record.snapshot_digest if self.record else None
        if c._coder_snapshot is not None:
            from supervisor.snapshot_recovery import persist_snapshot_authority
            snapshot_digest = persist_snapshot_authority(c._coder_snapshot, self.root / "snapshot.json", run_id=self.run_id)
        record = RunRecord(
            run_id=self.run_id, owner_pid=os.getpid(), owner_epoch=self.owner_epoch,
            implementation=implementation_fingerprint(),
            prompt_identity=self.prompt_identity,
            engine_identity=engines,
            project_root=str(c.project_root), task_path=str(c.task_path), task_hash=c._canonical_task_hash,
            plan_path=str(c.plan_path) if c.plan_path else None,
            plan_hash=_digest(c.plan_path) if c.plan_path else None, sandbox=coder_sandbox_mode(),
            runtime_state_dir=str(c.client.state_dir.resolve()) if hasattr(c.client, "state_dir") else None,
            runtime_tools_digest=tools_digest,
            phase=phase, eligible=reason is None, reason=reason or "safe coder continuation boundary",
            terminal=terminal, clean_shutdown=clean_shutdown,
            config=cfg.model_dump(mode="json"), health=c.store.get_health().model_dump(mode="json"),
            state_files={name: c.store.content_digest(name) for name in _STATE_FILES},
            snapshot_digest=snapshot_digest, controller=_capture(c),
        )
        c.store.atomic_write_json(self.root / "run.json", record)
        self.record = record

    def begin(self) -> None:
        self.transition_depth += 1
        if self.transition_depth == 1 and self.record is not None:
            # A write-ahead fence needs no fresh snapshot/hash scan: the old
            # complete state remains useful evidence, but is no longer safe to
            # continue until the whole transition commits.
            self.record = self.record.model_copy(update={
                "eligible": False, "reason": "controller transition was interrupted", "clean_shutdown": False})
            self.controller.store.atomic_write_json(self.root / "run.json", self.record)

    def end(self, *, succeeded: bool) -> None:
        self.transition_depth -= 1
        if succeeded:
            phase = "terminal" if self.controller.store.get_bello_config().status in _TERMINAL else "coder"
            self.checkpoint(phase)


def durable_transition(method):
    """Fence async controller mutations before execution; seal only on success."""
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        owner = getattr(self, "_durable_run", None)
        if owner is None:
            return await method(self, *args, **kwargs)
        if method.__name__ == "handle_controller_event" and args:
            event = args[0]
            message = getattr(event, "message", None)
            if getattr(event, "kind", None) == "notification" and message is not None:
                from supervisor.controller import _is_stream_delta_method
                if _is_stream_delta_method(message.method or ""):
                    # Streaming output changes transient buffers only. Tool
                    # start/completion and all decision boundaries remain fenced.
                    return await method(self, *args, **kwargs)
        owner.begin()
        success = False
        try:
            result = await method(self, *args, **kwargs)
            success = True
            return result
        finally:
            owner.end(succeeded=success)
    return wrapped


def _reject_uncertain_tools(runtime_state_dir: str | None, *, expected_digest: str | None) -> None:
    if runtime_state_dir is None:
        return
    directory = Path(runtime_state_dir)
    path = directory / "runtime.sqlite3"
    _ordinary_file(path)
    for suffix in ("-wal", "-shm"):
        _ordinary_file(path.with_name(path.name + suffix), missing_ok=True)
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            rows = db.execute("SELECT thread_id,call_id,fingerprint,status,result FROM tools ORDER BY thread_id,call_id").fetchall()
    except sqlite3.Error as exc:
        raise RecoveryBlocked("runtime command journal cannot be verified") from exc
    if any(row[3] != "completed" for row in rows):
        raise RecoveryBlocked("a dispatched runtime tool has an uncertain outcome; it will not be replayed")
    digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    if expected_digest is None or digest != expected_digest:
        raise RecoveryBlocked("runtime tool outcomes changed after the checkpoint; unprocessed results require manual recovery")


def observed_item_key(thread_id: str, turn_id: str, item_id: str) -> str:
    return json.dumps([thread_id, turn_id, item_id], separators=(",", ":"))


def observed_item_value(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("type", "status", "exitCode")}


async def resume_coder(controller: Any) -> None:
    """Restore a known thread; never fall back to a new thread or replay tools."""
    cfg = controller.store.get_bello_config()
    coder = controller.coder
    coder.thread_id = cfg.coder_thread_id
    coder.active_turn_id = None
    thread = await coder.resume_thread()
    turns = thread.get("turns", [])
    if cfg.active_coder_turn_id:
        old = next((item for item in turns if isinstance(item, dict) and item.get("id") == cfg.active_coder_turn_id), None)
        if old is None:
            raise RecoveryBlocked("provider cannot verify the interrupted coder turn")
        if old.get("status") == "completed":
            raise RecoveryBlocked("coder completed after the checkpoint; unprocessed evidence requires manual recovery")
        observed = getattr(controller, "_recovery_observed_items", {})
        seen = set()
        for item in old.get("items", []):
            if isinstance(item, dict) and item.get("type") not in {"agentMessage", "reasoning", "userMessage", "plan"}:
                if item.get("status") not in {"completed", "failed", "declined"}:
                    raise RecoveryBlocked("provider history contains a tool with an uncertain outcome")
                key = observed_item_key(cfg.coder_thread_id, cfg.active_coder_turn_id, item.get("id"))
                if key in seen or observed.get(key) != observed_item_value(item):
                    raise RecoveryBlocked("provider history contains an action not accounted for by the checkpoint")
                seen.add(key)
        expected = {key for key, value in observed.items()
                    if json.loads(key)[:2] == [cfg.coder_thread_id, cfg.active_coder_turn_id]
                    and value.get("type") not in {"agentMessage", "reasoning", "userMessage", "plan"}}
        if seen != expected:
            raise RecoveryBlocked("provider history omits a previously observed tool outcome")
    controller.pending_approvals.clear()
    controller.store.update_bello_config(lambda current: current.model_copy(update={
        "active_coder_turn_id": None, "pending_server_request_ids": [], "status": BelloStatus.RUNNING}))
    await coder.start_turn(
        "The Bello controller restarted the same logical run after stopping its prior processes. "
        "The existing workspace and history are preserved. Inspect current files and continue the task. "
        "Do not replay prior tool calls or assume an interrupted command did nothing. "
        "All original task, approval and sandbox restrictions still apply.")
