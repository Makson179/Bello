from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from supervisor.approval_triage import (
    CheapRuntimeReviewer,
    CheapRuntimeReviewerError,
    CheapRuntimeTriageConfig,
    runtime_triage_config_from_env,
)
from supervisor.adversary_agent import AdversaryAgent, AdversaryAgentError
from supervisor.appserver import (
    AppServerClient,
    AppServerError,
    AppServerMessage,
    last_agent_message_text,
)
from supervisor.approvals import ApprovalManager, normalize_approval_request
from supervisor.coder import (
    CODER_SANDBOX_DANGER_FULL_ACCESS,
    CODER_SANDBOX_WORKSPACE_WRITE,
    DEFAULT_INTELLIGENCE,
    CoderSession,
    coder_sandbox_mode,
    coder_thread_params,
)
from supervisor.health import (
    clear_restart_issue_for_validation,
    kill_restart_candidate,
    patch_health,
    record_restart_issue_intervention,
)
from supervisor.filesystem_safety import is_link_or_reparse, is_windows_platform
from supervisor.executables import ExecutableResolutionError, require_trusted_executable
from supervisor.project_config import DEFAULT_MODEL, MultiAgentConfig, ProjectConfig
from supervisor.policy import (
    _executable_basename,
    command_is_windows_shell_wrapper,
    lex_windows_command,
    native_shell_kind,
    windows_shell_wrapper_payload,
)
from supervisor.review_limits import review_limit_reached
from supervisor.schemas import (
    AppEvent,
    AppEventSource,
    ApprovalContext,
    AdversaryReport,
    ApprovalWakeContext,
    BehaviorSurfaceItem,
    BreadthRiskSummary,
    CheapRuntimeDecision,
    ChangedFile,
    ChangedFileContext,
    ChangedFileDiff,
    ChangedTestsSummary,
    CoderMessage,
    CompletionReturnRecord,
    CompletionReviewDecision,
    CompletionReviewDecisionKind,
    DiffPacketLimits,
    EvidenceProvenanceSummary,
    FinalReport,
    HealthDelta,
    HealthState,
    HumanMessage,
    InspectionOutput,
    InspectionRun,
    PriorIntervention,
    RestartHandoff,
    BelloConfig,
    BelloStatus,
    SubagentActivity,
    SubagentSummary,
    SupervisorDecision,
    SupervisorDecisionKind,
    SupervisorWakePacket,
    TriggeringAction,
    ValidationOutput,
    ValidationProvenance,
    ValidationRun,
)
from supervisor.schemas.models import ensure_relative_to
from supervisor.state import DECISIONS, HANDOFF, PROGRESS, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError
from supervisor.task_select import resolve_plan, resolve_task
from supervisor.tui import TerminalTUI, UserCommand
from supervisor.workspace_snapshot import (
    SnapshotPatchError,
    WorkspaceSnapshot,
    WorkspaceSnapshotError,
    apply_snapshot_patch,
    copy_isolated_workspace_tree,
    create_workspace_snapshot,
    remove_isolated_workspace_tree,
    snapshot_git_environment,
    validate_plan_git_isolation,
)
from supervisor.workspace_clean import clean_workspace_except_task


VALIDATION_LEDGER_LIMIT = 50
INSPECTION_LEDGER_LIMIT = 50
SUBAGENT_SUMMARY_LIMIT = 12
SUBAGENT_ACTION_LIMIT = 5
SUBAGENT_TEXT_LIMIT = 800
READINESS_EVENT_JOURNAL_LIMIT = 4096
READINESS_REVIEWER_THREAD_LIMIT = 8192
APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS = 3
APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS = (0.0, 1.0, 5.0)
TRANSPORT_RECOVERY_CODER_PROMPT = """The Codex app-server transport restarted during your previous turn.
Continue the task from the current workspace state. Preserve useful existing work, inspect what remains,
run the appropriate validation, and when complete output BELLO_READY_FOR_REVIEW on its own line."""
SUBAGENT_SOURCE_KINDS = (
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
)
READINESS_MARKER = "BELLO_READY_FOR_REVIEW"
READINESS_MARKER_RE = re.compile(r"^\s*BELLO_READY_FOR_REVIEW\s*$", re.MULTILINE)
NO_MARKER_IDLE_NUDGE = (
    "Continue working. If you believe the task is ready, provide Summary, Validation evidence, "
    "and the exact readiness marker on its own line: BELLO_READY_FOR_REVIEW."
)
POST_RESTART_CONTINUE_NUDGE = (
    "You are a fresh generation after a restart. Read HANDOFF.md and continue the task from there. "
    "Do not declare readiness until you have done new work and validated it."
)
LARGE_DIFF_CHANGED_LINES_THRESHOLD = 500
LARGE_DIFF_CHANGED_FILES_THRESHOLD = 10
PROTECTED_RUNTIME_WAKE_REASONS = {
    "done_without_fresh_validation",
    "repeated_same_failing_validation",
    "restart_budget",
    "suspicious_file_touched",
    "validation_regression",
}
MANDATORY_FULL_RUNTIME_WAKE_REASONS = {
    "done_without_fresh_validation",
    "restart_budget",
    "runtime_apply_retry",
    "runtime_control_replacement",
    "runtime_decision_retry",
}
CONTROLLER_IDLE_GUARD_INTERVAL_SECONDS = 60.0
CONTROLLER_IDLE_GUARD_STALL_SECONDS = 300.0
# Provider no_message (empty-completion) recovery for the completion review. A transient
# backend blip can return empty "completed" turns for a couple of minutes; ride it out with
# backed-off retries before declaring the run infra-invalid. The budget is CONSECUTIVE
# (reset on any successful supervisor decision), so a recovered provider keeps working.
COMPLETION_NO_MESSAGE_MAX_RETRIES = 6
NO_MESSAGE_RETRY_BACKOFF_SECONDS = (15.0, 30.0, 60.0, 120.0, 120.0, 120.0)
# A completion-review turn that times out must not kill the whole run: retry once on a fresh
# review thread (the timed-out turn is abandoned with the closed session) before the existing
# fatal provider_failure path. Consecutive semantics: reset on any successful decision.
COMPLETION_TIMEOUT_MAX_RETRIES = 1
# Observation-only breadth-risk hints for reviewer context. These terms must
# never force a completion decision, mandatory demo, or code change; required
# behavior is derived from task_contents and repository contract instead.
BREADTH_FEATURE_TERMS = (
    "api",
    "abi",
    "array",
    "auth",
    "cache",
    "case",
    "cli",
    "compatibility",
    "concurrency",
    "config",
    "constraint",
    "database",
    "delete",
    "enum",
    "error",
    "expression",
    "fallback",
    "function",
    "group",
    "index",
    "insert",
    "join",
    "limit",
    "migration",
    "null",
    "parser",
    "permission",
    "persistence",
    "pointer",
    "preprocessor",
    "query",
    "routing",
    "select",
    "snapshot",
    "sort",
    "storage",
    "struct",
    "transaction",
    "type",
    "update",
    "validation",
)
ADVERSARY_MODEL = DEFAULT_MODEL


class _RevisionCoderDeliveryError(RuntimeError):
    def __init__(
        self,
        stage: Literal["prepare", "thread/start", "turn/start"],
        error: Exception,
        *,
        generation: int,
        thread_id: str | None,
        coder: Any,
    ) -> None:
        super().__init__(f"{stage} failed: {error.__class__.__name__}: {error}")
        self.stage = stage
        self.original_error = error
        self.generation = generation
        self.thread_id = thread_id
        self.coder = coder


@dataclass(frozen=True)
class ControllerEvent:
    kind: str
    message: AppServerMessage | None = None
    user_command: UserCommand | None = None
    error: BaseException | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class _ReadinessJournalEvent:
    sequence: int
    source: AppEventSource
    event_type: str
    thread_id: str | None


@dataclass(frozen=True)
class QueuedSupervisorCheck:
    summary: str
    triggering_item_id: str | None = None
    triggering_action: TriggeringAction | None = None
    human_message: HumanMessage | None = None
    patch_summary: str | None = None
    completion_review: bool = False


@dataclass(frozen=True)
class RuntimeTriggerDecision:
    should_wake: bool
    reasons: tuple[str, ...] = ()
    restart_reason: str | None = None
    deterministic_action: str | None = None


@dataclass(frozen=True)
class RuntimeRestartIssue:
    key: str
    sequence: int
    validation_id: str | None = None


@dataclass(frozen=True)
class ModelAvailabilityResult:
    missing_roles: tuple[str, ...]
    available_models: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_roles


@dataclass
class SubagentRuntimeState:
    """Bounded, controller-owned state for one coder descendant thread."""

    thread_id: str
    parent_thread_id: str | None = None
    generation: int = 0
    status: str = "unknown"
    active_turn_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    prompt: str | None = None
    nickname: str | None = None
    role: str | None = None
    last_message: str | None = None
    recent_actions: list[tuple[int, str, str, str | None]] = field(default_factory=list)
    validation_ids: list[str] = field(default_factory=list)
    last_sequence: int = 0
    profile_allowed: bool | None = None

    def record_action(
        self,
        summary: str,
        *,
        sequence: int,
        kind: str,
        item_id: str | None,
    ) -> None:
        self.recent_actions.append(
            (sequence, kind, _bounded_subagent_text(summary), item_id)
        )
        self.recent_actions = self.recent_actions[-SUBAGENT_ACTION_LIMIT:]
        self.last_sequence = sequence


class BelloController:
    def __init__(
        self,
        project_root: Path,
        *,
        task_path: Path | None = None,
        plan_path: Path | None = None,
        client: AppServerClient | None = None,
        tui: TerminalTUI | None = None,
        model: str | None = None,
        coder_model: str | None = None,
        supervisor_model: str | None = None,
        runtime_model: str | None = None,
        completion_model: str | None = None,
        adversary_model: str | None = None,
        coder_intelligence: str | None = DEFAULT_INTELLIGENCE,
        supervisor_intelligence: str | None = None,
        runtime_intelligence: str | None = None,
        completion_intelligence: str | None = None,
        adversary_intelligence: str | None = DEFAULT_INTELLIGENCE,
        fast: bool = False,
        overwrite_state: bool = False,
        clean_workspace: bool = False,
        use_git_diff: bool = True,
        adversary_enabled: bool | None = None,
        adversary_runs: int | None = None,
        completion_review: bool | None = None,
        declared_grading_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
        project_config: ProjectConfig | None = None,
    ):
        self.project_root = project_root.resolve()
        self.plan_path = resolve_plan(self.project_root, plan_path)
        self.task_path = resolve_task(
            self.project_root,
            task_path,
            plan_path=self.plan_path,
        )
        self._canonical_task_contents = self.task_path.read_text(encoding="utf-8")
        self._canonical_task_hash = _hash_file(self.task_path)
        self.workspace_root = self.project_root
        self.workspace_task_path = self.task_path
        self.workspace_plan_path = self.plan_path
        self._coder_snapshot: WorkspaceSnapshot | None = None
        self._snapshot_patch_applied = False
        self._coder_started = False
        self.declared_grading_roots = tuple(str(Path(root).expanduser()) for root in declared_grading_roots or ())
        if self.plan_path is not None:
            validate_plan_git_isolation(self.project_root, self.plan_path)
        if clean_workspace:
            clean_preserved_paths: tuple[str | Path, ...] = self.declared_grading_roots
            if self.plan_path is not None:
                clean_preserved_paths = (*clean_preserved_paths, self.plan_path)
            clean_workspace_except_task(
                self.project_root,
                self.task_path,
                protected_paths=clean_preserved_paths,
            )
        self.store = StateStore(self.project_root)
        self.coder_model, self.runtime_model, self.completion_model, self.adversary_model = _resolve_controller_models(
            model=model,
            coder_model=coder_model,
            supervisor_model=supervisor_model,
            runtime_model=runtime_model,
            completion_model=completion_model,
            adversary_model=adversary_model,
        )
        self.supervisor_model = self.runtime_model
        shared_models = {self.coder_model, self.runtime_model, self.completion_model}
        self.model = self.coder_model if len(shared_models) == 1 else None
        self.coder_intelligence = coder_intelligence
        legacy_supervisor_intelligence = supervisor_intelligence or DEFAULT_INTELLIGENCE
        self.runtime_intelligence = runtime_intelligence or legacy_supervisor_intelligence
        self.completion_intelligence = completion_intelligence or legacy_supervisor_intelligence
        self.adversary_intelligence = adversary_intelligence or DEFAULT_INTELLIGENCE
        self.supervisor_intelligence = self.runtime_intelligence
        self.fast = fast
        self.overwrite_state = overwrite_state
        self.clean_workspace = clean_workspace
        self.use_git_diff = use_git_diff
        self.adversary_enabled = _adversary_enabled_from_env() if adversary_enabled is None else adversary_enabled
        self.adversary_runs = adversary_runs
        # CLI override for the completion-review toggle; stays runtime-scoped and never
        # rewrites the persisted project config, matching the other run settings.
        self.completion_review = completion_review
        self.project_config = project_config
        self.event_queue: asyncio.Queue[ControllerEvent] = asyncio.Queue()
        self.client = client or AppServerClient(
            cwd=self.project_root,
            notification_handler=self._on_notification,
            server_request_handler=self._on_server_request,
            transport_error_handler=self._on_transport_error,
        )
        self.tui = tui or TerminalTUI()
        self.supervisor: StatelessSupervisorAgent | None = None
        self.completion_supervisor: StatelessSupervisorAgent | None = None
        self.adv_report_controller: StatelessSupervisorAgent | None = None
        self.approvals: ApprovalManager | None = None
        self.runtime_triage_config: CheapRuntimeTriageConfig = runtime_triage_config_from_env(
            enabled=project_config.cheap_runtime if project_config is not None else True
        )
        self.runtime_triage_reviewer: CheapRuntimeReviewer | None = None
        self.coder: CoderSession | None = None
        self.pending_approvals: dict[int | str, ApprovalContext] = {}
        self.last_coder_message: CoderMessage | None = None
        self.validations: list[ValidationRun] = []
        self.inspections: list[InspectionRun] = []
        self.observed_changed_files: dict[str, ChangedFile] = {}
        self._command_output_chunks: dict[str, list[str]] = {}
        self.prior_interventions: list[PriorIntervention] = []
        self.running = False
        self.paused = False
        self._sequence = 0
        self._supervisor_task: asyncio.Task[None] | None = None
        self._supervisor_dirty = False
        self._supervisor_next_summary: str | None = None
        self._supervisor_next_completion_review = False
        self._supervisor_next_runtime_summary: str | None = None
        self._supervisor_next_completion_summary: str | None = None
        self._supervisor_next_runtime_check: QueuedSupervisorCheck | None = None
        self._supervisor_next_completion_check: QueuedSupervisorCheck | None = None
        self._current_turn_action_count = 0
        # True at run start (the initial generation begins working immediately); reset to False by
        # restart() until the new generation's first coder turn starts.
        self._generation_has_coder_turn = True
        self._last_completion_marker_sequence: int | None = None
        self.completion_returns: list[CompletionReturnRecord] = []
        self.completion_attempt_count = 0
        self.completion_restarts = 0
        self.provider_failure_recovery_counts: dict[str, int] = {}
        self._runtime_apply_retry_count = 0
        self._runtime_decision_retry_count = 0
        self.no_marker_idle_nudge_count = 0
        self.validation_runtime_state: dict[str, dict[str, Any]] = {}
        # Cross-review knowledge: the behavior surface accumulated by completion reviews of
        # this run plus the previous reviewer's unverified suspicions. Kept in memory on the
        # controller (not on disk in the coder-writable workspace): an in-run restart keeps this
        # same object, which is the only survival we need, and an in-memory store cannot be
        # forged by coder-authored test code or corrupted into a parse/type crash.
        self._completion_knowledge_state: dict[str, list[Any]] = {
            "behavior_surface": [],
            "uncovered_edge_candidates": [],
        }
        self.completion_review_return_sequence: int | None = None
        self._terminal_cleanup_started = False
        self._finalizing = False
        self._last_controller_activity_monotonic = time.monotonic()
        self._idle_guard_fired_for_sequence: int | None = None
        self._no_marker_completion_review_key: str | None = None
        self._last_large_diff_signature: str | None = None
        self._last_restart_budget_signature: str | None = None
        self._last_suspicious_file_signature: str | None = None
        self._pending_runtime_trigger_signatures: dict[str, tuple[str | None, str | None]] = {}
        self._pending_runtime_trigger_actions: dict[str, TriggeringAction] = {}
        self._suspicious_file_hash_cache: dict[str, tuple[tuple[Any, ...], str]] = {}
        self._pending_adversary_report: AdversaryReport | None = None
        self._active_adversary_thread_id: str | None = None
        self._active_adversary_workspace_root: Path | None = None
        self._final_report_archived = False
        self._subagents: dict[str, SubagentRuntimeState] = {}
        self._subagent_policy_notified: set[str] = set()
        self._deferred_completion_check: QueuedSupervisorCheck | None = None
        self._quiescing_coder_tree = False
        self._coder_quiesce_mutex: asyncio.Lock | None = None
        self._coder_activity_mutex: asyncio.Lock | None = None
        self._restart_transition_token: object | None = None
        self._revision_switch_in_progress = False
        self._revision_switch_done: asyncio.Future[None] | None = None
        self._revision_switch_owner: asyncio.Task[Any] | None = None
        self._readiness_event_journal: deque[_ReadinessJournalEvent] = deque(
            maxlen=READINESS_EVENT_JOURNAL_LIMIT
        )
        self._reviewer_thread_ids: OrderedDict[str, None] = OrderedDict()
        self._reviewer_thread_roles: dict[str, str] = {}
        self._transport_error_pending = False
        self._transport_recovery_lock: asyncio.Lock | None = None
        self._transport_recovery_total = 0
        self._active_provider_phase = "startup"
        self._active_supervisor_check: QueuedSupervisorCheck | None = None
        self._adversary_reservation_recovery_pending = False

    async def run(self) -> None:
        self.initialize_state()
        self._write_run_checkpoint("startup", state="active")
        try:
            await self.client.start()
            await self.client.initialize()
            await self.tui.start()
            self.running = True
            self.tui.render("SYSTEM", self._runtime_settings_summary())
            self._prepare_coder_workspace()
            self._write_run_checkpoint("coder_workspace", state="stable")
            if self._adversary_enabled_for_config() and not self._effective_completion_review():
                self.tui.render(
                    "SYSTEM",
                    "adversary requires completion review; disabled for this run",
                )
            await self.preflight()
            if not self.running:
                return
            self.supervisor = StatelessSupervisorAgent(
                self.client,
                self.store,
                self.task_path,
                workspace_root=self._active_workspace_root(),
                task_contents=self._canonical_task_contents,
                model=self._runtime_model(),
                fast=self._fast_mode(),
                intelligence=self._runtime_intelligence(),
                on_thread_start=lambda thread_id: self._register_reviewer_thread(
                    thread_id,
                    role="runtime",
                ),
            )
            self.completion_supervisor = StatelessSupervisorAgent(
                self.client,
                self.store,
                self.task_path,
                workspace_root=self._active_workspace_root(),
                task_contents=self._canonical_task_contents,
                model=self._completion_model(),
                fast=self._fast_mode(),
                intelligence=self._completion_intelligence(),
                completion_workspace_write=True,
                completion_source_snapshot=getattr(self, "_coder_snapshot", None),
                completion_multi_agent=self._completion_multi_agent_config(),
                before_completion_thread_cleanup=self._cleanup_completion_reviewer_descendants,
                on_thread_start=lambda thread_id: self._register_reviewer_thread(
                    thread_id,
                    role="completion_review",
                ),
            )
            self.adv_report_controller = StatelessSupervisorAgent(
                self.client,
                self.store,
                self.task_path,
                workspace_root=self._active_workspace_root(),
                task_contents=self._canonical_task_contents,
                model=self._completion_model(),
                fast=self._fast_mode(),
                intelligence=self._completion_intelligence(),
                completion_source_snapshot=getattr(self, "_coder_snapshot", None),
                on_thread_start=lambda thread_id: self._register_reviewer_thread(
                    thread_id,
                    role="adv_report_controller",
                ),
            )
            self.approvals = ApprovalManager(
                self._active_workspace_root(),
                supervisor=self,
                declared_grading_roots=self.declared_grading_roots,
                immutable_paths=self._immutable_approval_paths(),
            )
            self.coder = CoderSession(
                self.client,
                self.store,
                self._active_workspace_root(),
                self._active_task_path(),
                model=self._active_coder_model(),
                fast=self._fast_mode(),
                intelligence=self._active_coder_intelligence(),
                multi_agent=self._multi_agent_config(),
                plan_path=self._active_coder_plan_path(),
            )
            await self.coder.start_thread()
            self._coder_started = True
            await self.coder.start_initial_turn()
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.RUNNING}))
            self._active_provider_phase = "coder"
            self._write_run_checkpoint("coder", state="active")
            self.tui.status("supervised coder started")
            await self.event_loop()
        except (AppServerError, SupervisorAgentError) as exc:
            await self.fail_provider(f"app-server RPC failed: {exc}")
        except WorkspaceSnapshotError as exc:
            await self.fail_provider(f"run infrastructure failed: {exc}")
        finally:
            self.running = False
            await self._stop_supervisor_task()
            await self._close_completion_review_session()
            snapshot = getattr(self, "_coder_snapshot", None)
            if snapshot is not None and not getattr(self, "_snapshot_patch_applied", False):
                if getattr(self, "_coder_started", False):
                    await self._preserve_snapshot_for_recovery(snapshot, reason="unhandled_shutdown")
                else:
                    snapshot.cleanup()
                    self._coder_snapshot = None
            await self.tui.stop()
            await self.client.stop()

    def initialize_state(self) -> None:
        project_config = self._project_config_for_persistence()
        config = BelloConfig(
            project_root=str(self.project_root),
            task=project_config.task,
            task_path=project_config.task or "",
            task_hash=_hash_file(self.task_path),
            coder_mod=project_config.coder_mod,
            revision_coder_enabled=project_config.revision_coder_enabled,
            revision_coder_mod=project_config.revision_coder_mod,
            revision_coder_intelligence=project_config.revision_coder_intelligence,
            revision_coder_active=False,
            super_mod=project_config.runtime_mod,
            runtime_mod=project_config.runtime_mod,
            completion_mod=project_config.completion_mod,
            adversary_mod=project_config.adversary_mod,
            coder_intelligence=project_config.coder_intelligence,
            super_intelligence=project_config.runtime_intelligence,
            runtime_intelligence=project_config.runtime_intelligence,
            completion_intelligence=project_config.completion_intelligence,
            adversary_intelligence=project_config.adversary_intelligence,
            speed=project_config.speed,
            start_over=project_config.start_over,
            adversary=project_config.adversary,
            clean=project_config.clean,
            protected_path=list(project_config.protected_path),
            model=_shared_primary_model(project_config),
            coder_model=project_config.coder_mod,
            supervisor_model=project_config.runtime_mod,
            runtime_model=project_config.runtime_mod,
            completion_model=project_config.completion_mod,
            adversary_model=project_config.adversary_mod,
            supervisor_intelligence=project_config.runtime_intelligence,
            fast=project_config.fast,
            protected_paths=list(project_config.protected_path),
            max_adversary_runs=self._configured_adversary_runs(project_config),
            max_completion_returns_before_adversary=project_config.completion_returns_before_adversary,
            max_completion_returns_after_adversary=project_config.completion_returns_after_adversary,
            completion_review_enabled=project_config.completion_review,
            cheap_runtime=project_config.cheap_runtime,
            multi_agent=project_config.multi_agent.to_json_data(),
            completion_multi_agent=project_config.completion_multi_agent.to_json_data(),
            adversary_multi_agent=project_config.adversary_multi_agent.to_json_data(),
        )
        mode = "fresh" if self.overwrite_state else "resume"
        self.store.initialize_bello(config, mode=mode)
        self._sequence = self.store.max_event_sequence()
        _ensure_internal_runtime_git_excluded(self.project_root)

    def _write_run_checkpoint(
        self,
        phase: str,
        *,
        state: Literal["active", "stable", "recovering", "terminal"] = "stable",
        detail: str | None = None,
    ) -> None:
        """Persist orchestration metadata without copying the coder workspace."""

        self._active_provider_phase = phase
        try:
            cfg = self.store.get_bello_config()
        except Exception:
            return
        snapshot = getattr(self, "_coder_snapshot", None)
        active_check = getattr(self, "_active_supervisor_check", None)
        checkpoint: dict[str, Any] = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "state": state,
            "detail": detail,
            "status": cfg.status.value,
            "generation": cfg.generation,
            "coder_thread_id": cfg.coder_thread_id,
            "active_coder_turn_id": cfg.active_coder_turn_id,
            "revision_coder_active": cfg.revision_coder_active,
            "completion_return_count": cfg.completion_return_count,
            "adversary_run_count": cfg.adversary_run_count,
            "last_event_sequence": cfg.last_event_sequence,
            "last_applied_supervisor_sequence": cfg.last_applied_supervisor_sequence,
            "transport_recovery_total": int(
                getattr(self, "_transport_recovery_total", 0) or 0
            ),
            "workspace_path": str(
                snapshot.snapshot_root
                if snapshot is not None
                else self._active_workspace_root()
            ),
            "active_review": (
                {
                    "completion_review": active_check.completion_review,
                    "summary": active_check.summary,
                    "triggering_item_id": active_check.triggering_item_id,
                }
                if active_check is not None
                else None
            ),
        }
        accepted = getattr(self, "_accepted_completion_decision", None)
        if isinstance(accepted, CompletionReviewDecision):
            checkpoint["accepted_completion_decision"] = accepted.model_dump(
                mode="json"
            )
        report = getattr(self, "_pending_adversary_report", None)
        if isinstance(report, AdversaryReport):
            checkpoint["pending_adversary_report"] = report.model_dump(mode="json")
        try:
            self.store.write_run_checkpoint(checkpoint)
        except OSError as exc:
            # Checkpointing must never corrupt or stop the live run. The normal
            # final recovery workspace remains the fallback for a broken disk.
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "run_checkpoint_error",
                    "phase": phase,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )

    def _persist_model_config(self) -> None:
        project_config = self._project_config_for_persistence()
        self.store.update_bello_config(
            lambda cfg: cfg.model_copy(
                update={
                    "model": _shared_primary_model(project_config),
                    "coder_model": project_config.coder_mod,
                    "revision_coder_enabled": project_config.revision_coder_enabled,
                    "revision_coder_mod": project_config.revision_coder_mod,
                    "revision_coder_intelligence": project_config.revision_coder_intelligence,
                    "supervisor_model": project_config.runtime_mod,
                    "runtime_model": project_config.runtime_mod,
                    "completion_model": project_config.completion_mod,
                    "adversary_model": project_config.adversary_mod,
                    "runtime_intelligence": project_config.runtime_intelligence,
                    "completion_intelligence": project_config.completion_intelligence,
                    "adversary_intelligence": project_config.adversary_intelligence,
                    "supervisor_intelligence": project_config.runtime_intelligence,
                    "fast": project_config.fast,
                    "protected_paths": list(project_config.protected_path),
                    "max_adversary_runs": self._configured_adversary_runs(project_config),
                    "max_completion_returns_before_adversary": project_config.completion_returns_before_adversary,
                    "max_completion_returns_after_adversary": project_config.completion_returns_after_adversary,
                    "completion_review_enabled": project_config.completion_review,
                    "cheap_runtime": project_config.cheap_runtime,
                    "multi_agent": project_config.multi_agent.to_json_data(),
                    "completion_multi_agent": project_config.completion_multi_agent.to_json_data(),
                    "adversary_multi_agent": project_config.adversary_multi_agent.to_json_data(),
                }
            )
        )

    def _active_workspace_root(self) -> Path:
        return Path(getattr(self, "workspace_root", self.project_root)).resolve()

    def _active_task_path(self) -> Path:
        return Path(getattr(self, "workspace_task_path", self.task_path)).resolve()

    def _active_coder_plan_path(self) -> Path | None:
        if self._revision_coder_active():
            return None
        plan_path = getattr(self, "workspace_plan_path", None)
        if plan_path is None:
            return None
        return Path(plan_path).absolute()

    def _review_private_relative_paths(self) -> tuple[str, ...]:
        snapshot = getattr(self, "_coder_snapshot", None)
        plan_relative_path = getattr(snapshot, "plan_relative_path", None)
        if not plan_relative_path:
            return ()
        return (str(plan_relative_path),)

    def _is_review_private_path(self, path: str) -> bool:
        normalized = _normalize_internal_workspace_path(path)
        private_paths = {
            _normalize_internal_workspace_path(private_path)
            for private_path in self._review_private_relative_paths()
        }
        if is_windows_platform():
            normalized = normalized.casefold()
            private_paths = {private_path.casefold() for private_path in private_paths}
        return normalized in private_paths

    def _exposes_review_private_input(self, value: Any) -> bool:
        """Return whether structured evidence names a coder-only input path.

        Direct provenance is path-based.  Output/message fields additionally reject
        the complete plan payload so an aggregate or glob read cannot forward it.  A
        plan may contain an ordinary command such as ``pytest -q``; individual plan
        lines are never matched against commands, so genuine validation using the same
        words remains independent evidence.
        """

        snapshot = getattr(self, "_coder_snapshot", None)
        if snapshot is None or not getattr(snapshot, "plan_relative_path", None):
            return False
        structured_value = value
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        text_parts: list[str] = []

        def collect_strings(candidate: Any, *, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(candidate, str):
                text_parts.append(candidate)
                return
            if isinstance(candidate, bytes):
                text_parts.append(candidate.decode("utf-8", errors="replace"))
                return
            if isinstance(candidate, dict):
                for key, nested in candidate.items():
                    collect_strings(key, depth=depth + 1)
                    collect_strings(nested, depth=depth + 1)
                return
            if isinstance(candidate, (list, tuple, set)):
                for nested in candidate:
                    collect_strings(nested, depth=depth + 1)
                return
            if isinstance(candidate, Path):
                text_parts.append(str(candidate))

        collect_strings(value)
        case_insensitive_paths = is_windows_platform()

        def comparable_path_text(value: str) -> str:
            return value.casefold() if case_insensitive_paths else value

        comparable = comparable_path_text("\n".join(text_parts))

        path_markers: set[str] = set()
        for candidate in (
            getattr(snapshot, "plan_relative_path", None),
            getattr(snapshot, "plan_path", None),
            getattr(snapshot, "plan_source_path", None),
        ):
            if candidate is None:
                continue
            marker = str(candidate)
            path_markers.add(marker)
            path_markers.add(marker.replace("/", "\\"))
            path_markers.add(marker.replace("\\", "/"))
        relative_marker = str(snapshot.plan_relative_path)
        path_markers.add(f"./{relative_marker}")
        windows_relative_marker = relative_marker.replace("/", "\\")
        path_markers.add(f".\\{windows_relative_marker}")
        def contains_path_token(marker: str) -> bool:
            candidate = comparable_path_text(marker)
            if not candidate:
                return False
            return re.search(
                rf"(?<![\w./\\-]){re.escape(candidate)}(?![\w./\\-])",
                comparable,
            ) is not None

        def contains_path_component(component: str) -> bool:
            candidate = comparable_path_text(component)
            if not candidate:
                return False
            return re.search(
                rf"(?<![\w.-]){re.escape(candidate)}(?![\w.-])",
                comparable,
            ) is not None

        for marker in path_markers:
            if contains_path_token(marker):
                return True

        # A shell action can name a nested plan relative to its own cwd, for
        # example ``cwd=.../docs`` with ``cat PLAN.md``.  Do not match the
        # basename globally: require the relative parent components to be present
        # in the same structured action as well.
        plan_relative = Path(str(snapshot.plan_relative_path))
        parent_parts = tuple(
            comparable_path_text(part)
            for part in plan_relative.parent.parts
            if part not in {"", "."}
        )
        basename = comparable_path_text(plan_relative.name)
        if (
            parent_parts
            and basename
            and contains_path_token(basename)
            and all(contains_path_component(part) for part in parent_parts)
        ):
            return True

        # Indirect reads (for example a Markdown glob) need not spell the plan
        # path.  Inspect only output/message-bearing fields for the complete plan
        # payload; never compare plan lines against command fields.
        private_texts: list[str] = []
        if isinstance(structured_value, (ValidationRun, InspectionRun)):
            private_texts.append(structured_value.captured_output)
        elif isinstance(structured_value, CoderMessage):
            private_texts.append(structured_value.text)
        elif isinstance(structured_value, SubagentSummary):
            private_texts.extend(
                text
                for text in (
                    structured_value.prompt,
                    structured_value.last_message,
                    *(activity.summary for activity in structured_value.recent_actions),
                )
                if text
            )
        elif isinstance(structured_value, PriorIntervention):
            private_texts.extend(
                (structured_value.reason, structured_value.message_to_coder)
            )
        elif isinstance(value, dict):
            for key in (
                "captured_output",
                "output",
                "text",
                "prompt",
                "last_message",
                "message_to_coder",
            ):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    private_texts.append(candidate)
        plan_bytes = getattr(snapshot, "plan_bytes", None)
        if isinstance(plan_bytes, bytes) and plan_bytes and private_texts:
            plan_text = plan_bytes.decode("utf-8", errors="replace")
            markers = {plan_text, plan_text.strip()}
            if len(plan_text) > 512:
                markers.update(
                    {
                        plan_text[:256],
                        plan_text[len(plan_text) // 2 - 128 : len(plan_text) // 2 + 128],
                        plan_text[-256:],
                    }
                )
            markers = {marker for marker in markers if marker.strip()}
            if any(
                marker in candidate
                for marker in markers
                for candidate in private_texts
            ):
                return True
        return False

    def _review_safe_values(self, values: list[Any]) -> list[Any]:
        return [
            value
            for value in values
            if not self._exposes_review_private_input(value)
        ]

    def _review_safe_packet_state(
        self,
        packet: SupervisorWakePacket,
    ) -> SupervisorWakePacket:
        """Remove runtime-authored state that would disclose coder-only plan input."""

        def exposes_freeform(value: Any) -> bool:
            if value is None:
                return False
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            text_parts: list[str] = []

            def collect(candidate: Any) -> None:
                if isinstance(candidate, str):
                    text_parts.append(candidate)
                elif isinstance(candidate, bytes):
                    text_parts.append(candidate.decode("utf-8", errors="replace"))
                elif isinstance(candidate, dict):
                    for key, nested in candidate.items():
                        collect(key)
                        collect(nested)
                elif isinstance(candidate, (list, tuple, set)):
                    for nested in candidate:
                        collect(nested)
                elif isinstance(candidate, Path):
                    text_parts.append(str(candidate))

            collect(value)
            return self._exposes_review_private_input(
                {"text": "\n".join(text_parts)}
            )

        updates: dict[str, Any] = {}
        for field_name in ("progress", "decisions", "current_summary"):
            value = getattr(packet, field_name)
            if exposes_freeform(value):
                updates[field_name] = (
                    "Coder work is ready for independent review."
                    if field_name == "current_summary"
                    else ""
                )
        if exposes_freeform(packet.handoff):
            updates["handoff"] = None
        if exposes_freeform(packet.health):
            updates["health"] = {}
        updates["last_actions"] = [
            value for value in packet.last_actions if not exposes_freeform(value)
        ]
        updates["recent_events"] = [
            value for value in packet.recent_events if not exposes_freeform(value)
        ]
        return packet.model_copy(update=updates)

    def _git_command_excluding_review_private_inputs(
        self,
        command: list[str],
    ) -> list[str]:
        private_paths = self._review_private_relative_paths()
        if not private_paths or tuple(command[:2]) not in {
            ("git", "status"),
            ("git", "diff"),
        }:
            return list(command)
        filtered = list(command)
        if "--" not in filtered:
            filtered.append("--")
        filtered.append(".")
        filtered.extend(
            f":(exclude,top,literal){private_path}"
            for private_path in private_paths
        )
        return filtered

    def _canonical_task_text(self) -> str:
        return getattr(self, "_canonical_task_contents", _read_task_text(self.task_path))

    def _immutable_approval_paths(self) -> tuple[Path, ...]:
        snapshot = getattr(self, "_coder_snapshot", None)
        plan_path = getattr(self, "plan_path", None)
        if snapshot is not None:
            paths = [snapshot.original_root, self.task_path]
            if plan_path is not None:
                paths.append(Path(plan_path))
            return tuple(paths)
        task_path = getattr(self, "task_path", None)
        paths = [Path(task_path)] if task_path is not None else []
        if plan_path is not None:
            paths.append(Path(plan_path))
        return tuple(paths)

    def _task_integrity_issue(self) -> str | None:
        expected_hash = getattr(self, "_canonical_task_hash", None)
        if expected_hash:
            try:
                current_hash = _hash_file(self.task_path)
            except OSError:
                return "the original task file is missing or unreadable"
            if current_hash != expected_hash:
                return "the original task file changed after the run started"
        snapshot = getattr(self, "_coder_snapshot", None)
        if snapshot is None:
            return None
        return snapshot.task_integrity_issue()

    def _runtime_integrity_issue(self) -> str | None:
        snapshot = getattr(self, "_coder_snapshot", None)
        if snapshot is None:
            return None
        return snapshot.plan_integrity_issue() or snapshot.runtime_integrity_issue()

    async def _escalate_runtime_integrity_issue(self, *, source: str) -> bool:
        issue = self._runtime_integrity_issue()
        if issue is None:
            return False
        message = f"coder workspace runtime integrity failure ({source}): {issue}"
        self.tui.render("INTEGRITY", message)
        self.store.append_text_locked(PROGRESS, f"- Integrity failure: {message}\n")
        self._append_event(
            AppEventSource.SUPERVISOR,
            "integrity/runtime_control_mutation",
            reason=message,
        )
        await self.finalize(f"escalated: {message}", status=BelloStatus.ESCALATED)
        return True

    def _repair_snapshot_runtime_controls(self, *, source: str) -> tuple[str, ...]:
        snapshot = getattr(self, "_coder_snapshot", None)
        if snapshot is None:
            return ()
        repaired = list(snapshot.restore_runtime_links())
        if snapshot.restore_git_control():
            repaired.append("git_config")
        if not repaired:
            return ()
        detail = ", ".join(repaired)
        message = f"restored replaced coder workspace runtime control(s): {detail} ({source})"
        self.store.append_text_locked(PROGRESS, f"- Integrity guard: {message}.\n")
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "coder_workspace_runtime_controls_restored",
                "source": source,
                "repaired": list(repaired),
            }
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "integrity/runtime_controls_restored",
            reason=message,
        )
        cfg = self.store.get_bello_config()
        patch_health(
            self.store,
            HealthDelta(generation=cfg.generation, add_risk_signals=["runtime_control_replacement"]),
        )
        self.tui.render("INTEGRITY", message)
        return tuple(repaired)

    def _uses_coder_snapshot(self) -> bool:
        return coder_sandbox_mode() == CODER_SANDBOX_WORKSPACE_WRITE

    def _prepare_coder_workspace(self) -> None:
        if not self._uses_coder_snapshot():
            if self.plan_path is not None:
                raise WorkspaceSnapshotError(
                    "--plan requires the default workspace-write coder snapshot so independent reviewers can remain plan-blind"
                )
            self.workspace_root = self.project_root
            self.workspace_task_path = self.task_path
            self.workspace_plan_path = None
            self._coder_snapshot = None
            return
        snapshot = create_workspace_snapshot(
            self.project_root,
            self.task_path,
            plan_path=self.plan_path,
            declared_grading_roots=getattr(self, "declared_grading_roots", ()),
        )
        self._coder_snapshot = snapshot
        self.workspace_root = snapshot.snapshot_root
        self.workspace_task_path = snapshot.task_path
        self.workspace_plan_path = snapshot.plan_path
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "coder_workspace_snapshot_created",
                "snapshot_root": str(snapshot.snapshot_root),
                "original_root": str(snapshot.original_root),
                "rewritten_symlinks": [rewrite.path for rewrite in snapshot.rewritten_symlinks],
                "excluded_external_symlinks": list(snapshot.excluded_external_symlink_paths),
            }
        )

    def _coder_model(self) -> str | None:
        return getattr(self, "coder_model", getattr(self, "model", DEFAULT_MODEL))

    def _revision_coder_enabled(self) -> bool:
        project_config = getattr(self, "project_config", None)
        if project_config is not None:
            return bool(project_config.revision_coder_enabled)
        try:
            return bool(self.store.get_bello_config().revision_coder_enabled)
        except Exception:
            return False

    def _revision_coder_model(self) -> str | None:
        project_config = getattr(self, "project_config", None)
        if project_config is not None:
            return project_config.revision_coder_mod
        try:
            return self.store.get_bello_config().revision_coder_mod or self._coder_model()
        except Exception:
            return self._coder_model()

    def _revision_coder_active(self) -> bool:
        try:
            return bool(self.store.get_bello_config().revision_coder_active)
        except Exception:
            return False

    def _active_coder_model(self) -> str | None:
        if self._revision_coder_active():
            return self._revision_coder_model()
        return self._coder_model()

    def _runtime_model(self) -> str | None:
        return getattr(self, "runtime_model", getattr(self, "supervisor_model", getattr(self, "model", DEFAULT_MODEL)))

    def _supervisor_model(self) -> str | None:
        return self._runtime_model()

    def _completion_model(self) -> str | None:
        return getattr(self, "completion_model", getattr(self, "supervisor_model", getattr(self, "model", DEFAULT_MODEL)))

    def _adversary_model(self) -> str:
        return getattr(self, "adversary_model", ADVERSARY_MODEL)

    def _fast_mode(self) -> bool:
        return bool(getattr(self, "fast", False))

    def _cheap_runtime_enabled(self) -> bool:
        try:
            return bool(self.store.get_bello_config().cheap_runtime)
        except Exception:
            project_config = getattr(self, "project_config", None)
            return bool(project_config.cheap_runtime) if project_config is not None else True

    def _effective_completion_review(self) -> bool:
        """Whether the completion review gate is active for this run.

        CLI override wins; otherwise the persisted project-config mirror. With the gate
        off, the coder's readiness marker finalizes the run directly and the adversary
        (which runs inside the review-accept path) is inactive.
        """
        override = getattr(self, "completion_review", None)
        if override is not None:
            return bool(override)
        try:
            return bool(self.store.get_bello_config().completion_review_enabled)
        except Exception:
            project_config = getattr(self, "project_config", None)
            if project_config is not None:
                return bool(project_config.completion_review)
            return True

    def _adversary_enabled_for_config(self) -> bool:
        enabled = getattr(self, "adversary_enabled", None)
        if enabled is False:
            return False
        return True

    def _configured_adversary_runs(self, project_config: ProjectConfig) -> int:
        """Adversary pass budget persisted to the run config. Mirrors the project file only —
        CLI overrides (adversary_enabled / adversary_runs) stay runtime-scoped and are applied
        in _effective_max_adversary_runs, matching how the other run settings behave."""
        return max(0, project_config.adversary_runs) if project_config.adversary else 0

    def _project_config_for_persistence(self) -> ProjectConfig:
        config = getattr(self, "project_config", None)
        if config is not None:
            return config
        return ProjectConfig(
            task=_workspace_display_path(self.project_root, str(self.task_path)),
            coder_mod=self._coder_model() or DEFAULT_MODEL,
            runtime_mod=self._runtime_model() or DEFAULT_MODEL,
            completion_mod=self._completion_model() or DEFAULT_MODEL,
            adversary_mod=self._adversary_model(),
            coder_intelligence=self._coder_intelligence() or DEFAULT_INTELLIGENCE,
            runtime_intelligence=self._runtime_intelligence() or DEFAULT_INTELLIGENCE,
            completion_intelligence=self._completion_intelligence() or DEFAULT_INTELLIGENCE,
            adversary_intelligence=self._adversary_intelligence() or DEFAULT_INTELLIGENCE,
            speed="fast" if self._fast_mode() else "usual",
            start_over=self.overwrite_state,
            adversary=self._adversary_enabled_for_config(),
            clean=self.clean_workspace,
            protected_path=tuple(_workspace_display_path(self.project_root, path) for path in self.declared_grading_roots),
        )

    def _runtime_settings_summary(self) -> str:
        protected_paths = (
            ", ".join(_workspace_display_path(self.project_root, path) for path in self.declared_grading_roots)
            if self.declared_grading_roots
            else "absent"
        )
        speed = "fast" if self._fast_mode() else "usual"
        multi_agent_summary = _format_multi_agent_summary(self._multi_agent_config())
        completion_multi_agent_summary = _format_multi_agent_summary(
            self._completion_multi_agent_config()
        )
        adversary_multi_agent_summary = _format_multi_agent_summary(
            self._adversary_multi_agent_config()
        )
        revision_coder_summary = "off"
        if self._revision_coder_enabled():
            revision_coder_summary = (
                f"on({self._revision_coder_model()}/{self._revision_coder_intelligence()})"
            )
        return (
            "settings: "
            f"task={_workspace_display_path(self.project_root, str(self.task_path))} "
            f"coder-mod={self._coder_model()} "
            f"runtime-mod={self._runtime_model()} "
            f"completion-mod={self._completion_model()} "
            f"adversary-mod={self._adversary_model()} "
            f"coder-intelligence={self._coder_intelligence()} "
            f"revision-coder={revision_coder_summary} "
            f"runtime-intelligence={self._runtime_intelligence()} "
            f"completion-intelligence={self._completion_intelligence()} "
            f"adversary-intelligence={self._adversary_intelligence()} "
            f"speed={speed} "
            f"cheap-runtime={_format_bool(self._cheap_runtime_enabled())} "
            f"multi-agent={multi_agent_summary} "
            f"completion-multi-agent={completion_multi_agent_summary} "
            f"adversary-multi-agent={adversary_multi_agent_summary} "
            f"start-over={_format_bool(self.overwrite_state)} "
            f"clean={_format_bool(self.clean_workspace)} "
            f"completion-review={_format_bool(self._effective_completion_review())} "
            f"adversary={_format_bool(self._adversary_enabled_for_config() and self._effective_completion_review())} "
            f"protected-path={protected_paths}"
        )

    def _coder_intelligence(self) -> str | None:
        return getattr(self, "coder_intelligence", DEFAULT_INTELLIGENCE)

    def _revision_coder_intelligence(self) -> str | None:
        project_config = getattr(self, "project_config", None)
        if project_config is not None:
            return project_config.revision_coder_intelligence
        try:
            return self.store.get_bello_config().revision_coder_intelligence or self._coder_intelligence()
        except Exception:
            return self._coder_intelligence()

    def _active_coder_intelligence(self) -> str | None:
        if self._revision_coder_active():
            return self._revision_coder_intelligence()
        return self._coder_intelligence()

    def _coder_lifecycle_accepts_activity(
        self,
        cfg: BelloConfig | None = None,
        *,
        require_running: bool = True,
    ) -> bool:
        cfg = cfg or self.store.get_bello_config()
        return bool(
            (not require_running or getattr(self, "running", True))
            and not getattr(self, "paused", False)
            and not getattr(self, "_finalizing", False)
            and not getattr(self, "_terminal_cleanup_started", False)
            and cfg.status in {BelloStatus.STARTING, BelloStatus.RUNNING}
        )

    def _coder_activity_lock(self) -> asyncio.Lock:
        mutex = getattr(self, "_coder_activity_mutex", None)
        if mutex is None:
            mutex = asyncio.Lock()
            self._coder_activity_mutex = mutex
        return mutex

    async def _wait_for_coder_activity(self) -> None:
        async with self._coder_activity_lock():
            return

    async def _deliver_coder_message(
        self,
        message: str,
        *,
        coder: Any | None = None,
        force_new_turn: bool = False,
    ) -> tuple[bool, str | None]:
        async with self._coder_activity_lock():
            cfg = self.store.get_bello_config()
            target = coder if coder is not None else getattr(self, "coder", None)
            if (
                not self._coder_lifecycle_accepts_activity(cfg)
                or target is None
                or target is not getattr(self, "coder", None)
                or getattr(target, "thread_id", cfg.coder_thread_id) != cfg.coder_thread_id
            ):
                return False, None
            if force_new_turn:
                result = await target.start_turn(message)
            else:
                result = await target.steer_or_start(message)
            current = self.store.get_bello_config()
            delivered_to_current_lifecycle = bool(
                self._coder_lifecycle_accepts_activity(current)
                and target is getattr(self, "coder", None)
                and getattr(target, "thread_id", current.coder_thread_id) == current.coder_thread_id
            )
            return delivered_to_current_lifecycle, result

    def _runtime_intelligence(self) -> str | None:
        return getattr(self, "runtime_intelligence", getattr(self, "supervisor_intelligence", DEFAULT_INTELLIGENCE))

    def _supervisor_intelligence(self) -> str | None:
        return self._runtime_intelligence()

    def _completion_intelligence(self) -> str | None:
        return getattr(
            self,
            "completion_intelligence",
            getattr(self, "supervisor_intelligence", DEFAULT_INTELLIGENCE),
        )

    def _adversary_intelligence(self) -> str | None:
        return getattr(self, "adversary_intelligence", DEFAULT_INTELLIGENCE)

    def _completion_supervisor_agent(self) -> StatelessSupervisorAgent | None:
        return getattr(self, "completion_supervisor", None) or getattr(self, "supervisor", None)

    def _adv_report_controller_agent(self) -> StatelessSupervisorAgent | None:
        return getattr(self, "adv_report_controller", None)

    async def preflight(self) -> None:
        self.tui.status("checking Codex version")
        codex = _controller_executable("codex", self.project_root)
        if codex is None:
            raise RuntimeError("trusted codex executable not found")
        version = _run_probe([codex, "--version"])[1]
        self.tui.status("checking Codex app-server schema")
        schema_hash = await self._generate_schema_hash_async()
        self.store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"codex_version": version, "appserver_schema_hash": schema_hash})
        )
        self.tui.status("checking Codex account")
        account = await self.client.account_read()
        if account.get("requiresOpenaiAuth") and account.get("account") is None:
            raise RuntimeError("Codex auth missing. Run `codex login` before starting Bello.")
        self.tui.status("checking Codex rate limits")
        try:
            await self.client.account_rate_limits_read()
        except Exception as exc:
            warning = f"Codex rate limit check unavailable; continuing: {exc}"
            self.tui.render("SYSTEM", warning)
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "preflight_warning",
                    "check": "codex_rate_limits",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
        self.tui.status("checking available models")
        models_response = await self.client.model_list()
        self._persist_model_config()
        await self._ensure_selected_models_available(models_response)
        if self.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE:
            return
        self.tui.status("checking supervisor structured output")
        await self._structured_output_self_test()
        await self._configure_runtime_triage()
        self.tui.status("checking config requirements")
        await self.client.config_requirements_read()
        self.tui.status("checking coder sandbox and approval settings")
        thread = await self.client.thread_start(
            coder_thread_params(
                self._active_workspace_root(),
                model=self._coder_model(),
                fast=self._fast_mode(),
                multi_agent=self._multi_agent_config(),
            )
        )
        approval_policy = thread.get("approvalPolicy")
        sandbox = thread.get("sandbox")
        thread_id = thread.get("thread", {}).get("id") if isinstance(thread.get("thread"), dict) else None
        if approval_policy != "on-request":
            raise RuntimeError("app-server did not accept on-request coder approval policy")
        expected_sandbox = coder_sandbox_mode()
        if not _sandbox_matches_mode(
            sandbox,
            expected_sandbox,
            workspace_root=self._active_workspace_root(),
        ):
            raise RuntimeError(f"app-server did not accept {expected_sandbox} coder sandbox")
        if isinstance(thread_id, str):
            await self._cleanup_preflight_probe_thread(thread_id)

    async def _ensure_selected_models_available(self, models_response: dict[str, Any]) -> None:
        result = _selected_model_availability(
            models_response,
            coder_model=self._coder_model(),
            revision_coder_model=(
                self._revision_coder_model()
                if self._revision_coder_enabled() and self._effective_completion_review()
                else None
            ),
            runtime_model=self._runtime_model(),
            completion_model=self._completion_model() if self._effective_completion_review() else None,
            adversary_model=self._adversary_model() if self._adversary_model_required_for_preflight() else None,
            subagent_models=self._enabled_subagent_models_for_preflight(),
        )
        if result.ok:
            return
        available = ", ".join(result.available_models) if result.available_models else "none reported"
        missing = ", ".join(result.missing_roles)
        message = (
            "model availability preflight failed before coder start: "
            f"selected model(s) are not available from Codex app-server model/list: {missing}. "
            f"Available models: {available}. "
            "The interruption is recorded in .supervisor/FINAL_REPORT.md."
        )
        self.store.append_text_locked(PROGRESS, f"- {message}\n")
        await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)

    def _enabled_subagent_models_for_preflight(self) -> tuple[str, ...]:
        policies = [self._multi_agent_config()]
        if self._effective_completion_review():
            policies.append(self._completion_multi_agent_config())
        if self._adversary_model_required_for_preflight():
            policies.append(self._adversary_multi_agent_config())
        models: list[str] = []
        for policy in policies:
            if not getattr(policy, "enabled", False):
                continue
            for model in getattr(policy, "allowed", {}):
                if model not in models:
                    models.append(model)
        return tuple(models)

    def _adversary_model_required_for_preflight(self) -> bool:
        if not self._effective_completion_review():
            return False
        enabled = getattr(self, "adversary_enabled", None)
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.store.get_bello_config().max_adversary_runs > 0

    async def event_loop(self) -> None:
        assert self.tui is not None
        while self.running:
            event_task = asyncio.create_task(self.event_queue.get())
            input_task = asyncio.create_task(self.tui.input_queue.get())
            done, pending = await asyncio.wait(
                {event_task, input_task},
                timeout=CONTROLLER_IDLE_GUARD_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                await self._handle_controller_idle_guard()
                continue
            for done_task in done:
                completed = done_task.result()
                self._mark_controller_activity()
                if isinstance(completed, ControllerEvent):
                    await self.handle_controller_event(completed)
                elif isinstance(completed, UserCommand):
                    await self.handle_user_command(completed)

    def _mark_controller_activity(self) -> None:
        self._last_controller_activity_monotonic = time.monotonic()
        self._idle_guard_fired_for_sequence = None

    async def _handle_controller_idle_guard(self, *, now: float | None = None, force: bool = False) -> None:
        if not self.running or getattr(self, "paused", False) or getattr(self, "_terminal_cleanup_started", False):
            return
        cfg = self.store.get_bello_config()
        if cfg.active_coder_turn_id:
            return
        coder = getattr(self, "coder", None)
        if coder is None:
            await self.finalize(
                "controller idle guard: no active coder session, no pending approvals, and no supervisor check",
                status=BelloStatus.PROVIDER_FAILURE,
            )
            return
        if getattr(coder, "active_turn_id", None):
            return
        if getattr(self, "pending_approvals", None):
            return
        task = getattr(self, "_supervisor_task", None)
        if task is not None and not task.done():
            return
        current_time = time.monotonic() if now is None else now
        last_activity = getattr(self, "_last_controller_activity_monotonic", current_time)
        if not force and current_time - last_activity < CONTROLLER_IDLE_GUARD_STALL_SECONDS:
            return
        sequence = cfg.last_event_sequence
        if getattr(self, "_idle_guard_fired_for_sequence", None) == sequence:
            return
        self._idle_guard_fired_for_sequence = sequence
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "controller_idle_guard",
                "sequence": sequence,
                "reason": "running with no active coder turn, pending approval, or supervisor check",
            }
        )
        await self._handle_no_marker_idle()

    async def handle_controller_event(self, event: ControllerEvent) -> None:
        try:
            if event.kind == "shutdown":
                self.running = False
                return
            if event.kind == "transport_error":
                await self.handle_transport_error(event)
                return
            if event.message is None:
                return
            message = event.message
            if event.kind == "server_request":
                await self.handle_server_request(message)
            elif event.kind == "notification":
                await self.handle_notification(message)
        except AppServerError as exc:
            if getattr(self, "_transport_error_pending", False):
                return
            await self.fail_provider(f"app-server RPC failed while handling {event.kind}: {exc}")

    async def handle_transport_error(self, event: ControllerEvent) -> None:
        message = event.error_message or str(event.error) or "app-server transport error"
        self._append_event(AppEventSource.APP_SERVER, "appServer/transportError", reason=message)
        if _is_recoverable_app_server_transport_error(message):
            recovered = await self._recover_app_server_transport(message)
            if recovered:
                return
        self._transport_error_pending = False
        await self.finalize(f"app-server transport error: {message}", status=BelloStatus.PROVIDER_FAILURE)

    def _transport_recovery_mutex(self) -> asyncio.Lock:
        lock = getattr(self, "_transport_recovery_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._transport_recovery_lock = lock
        return lock

    async def _recover_app_server_transport(self, message: str) -> bool:
        """Restart app-server and resume the current logical run in place."""

        async with self._transport_recovery_mutex():
            if not self._coder_lifecycle_accepts_activity(require_running=False):
                return False
            active_check = getattr(self, "_active_supervisor_check", None)
            failed_phase = getattr(self, "_active_provider_phase", "unknown")
            self._write_run_checkpoint(
                failed_phase,
                state="recovering",
                detail=message,
            )
            self.tui.render(
                "SYSTEM",
                f"app-server transport lost during {failed_phase}; recovering",
            )
            self.store.append_text_locked(
                PROGRESS,
                f"- Provider recovery: app-server transport was lost during {failed_phase}; "
                "restarting the transport and preserving the current workspace.\n",
            )
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "app_server_transport_recovery_started",
                    "phase": failed_phase,
                    "message": message,
                    "coder_thread_id": self.store.get_bello_config().coder_thread_id,
                    "active_coder_turn_id": self.store.get_bello_config().active_coder_turn_id,
                }
            )

            supervisor_task = getattr(self, "_supervisor_task", None)
            if supervisor_task is not None and supervisor_task is not asyncio.current_task():
                await self._stop_supervisor_task()
            self._supervisor_task = None
            await self._abandon_dead_completion_review()
            self.pending_approvals.clear()
            self.store.update_bello_config(
                lambda cfg: cfg.model_copy(update={"pending_server_request_ids": []})
            )
            self._subagents = {}
            self._subagent_policy_notified = set()
            self._reviewer_thread_ids = OrderedDict()
            self._reviewer_thread_roles = {}
            self._active_adversary_thread_id = None

            if failed_phase == "adversary" and not getattr(
                self, "_adversary_reservation_recovery_pending", False
            ):
                self._rollback_interrupted_adversary_reservation()
                self._adversary_reservation_recovery_pending = True

            errors: list[str] = []
            for attempt in range(1, APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS + 1):
                delay = APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS[
                    min(attempt - 1, len(APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS) - 1)
                ]
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await self._restart_app_server_client()
                    await self._recover_coder_thread_after_transport(
                        start_continuation=active_check is None,
                    )
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                    errors.append(error)
                    self.store.append_raw_log(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "type": "app_server_transport_recovery_attempt_failed",
                            "attempt": attempt,
                            "phase": failed_phase,
                            "error": error,
                        }
                    )
                    continue

                self._transport_recovery_total = int(
                    getattr(self, "_transport_recovery_total", 0) or 0
                ) + 1
                self._transport_error_pending = False
                self._adversary_reservation_recovery_pending = False
                self._write_run_checkpoint(
                    "coder" if active_check is None else failed_phase,
                    state="stable",
                    detail=f"transport recovered on attempt {attempt}",
                )
                self.store.append_text_locked(
                    PROGRESS,
                    f"- Provider recovery complete: app-server resumed on attempt "
                    f"{attempt}/{APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS}.\n",
                )
                self.store.append_raw_log(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "type": "app_server_transport_recovered",
                        "attempt": attempt,
                        "phase": failed_phase,
                        "coder_thread_id": self.store.get_bello_config().coder_thread_id,
                        "active_coder_turn_id": self.store.get_bello_config().active_coder_turn_id,
                    }
                )
                self.tui.render("SYSTEM", "app-server transport recovered")
                if active_check is not None and self._coder_lifecycle_accepts_activity():
                    self._schedule_supervisor_check(
                        active_check.summary,
                        triggering_item_id=active_check.triggering_item_id,
                        triggering_action=active_check.triggering_action,
                        human_message=active_check.human_message,
                        patch_summary=active_check.patch_summary,
                        completion_review=active_check.completion_review,
                    )
                return True

            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "app_server_transport_recovery_exhausted",
                    "phase": failed_phase,
                    "errors": errors,
                }
            )
            return False

    async def _restart_app_server_client(self) -> None:
        client = self.client
        restart = getattr(client, "restart", None)
        if callable(restart):
            await restart()
        else:
            await client.stop()
            await client.start()
        await client.initialize()

    async def _abandon_dead_completion_review(self) -> None:
        supervisor = self._completion_supervisor_agent()
        if supervisor is None:
            return
        abandon = getattr(
            supervisor,
            "abandon_completion_review_after_transport_loss",
            None,
        )
        if callable(abandon):
            await abandon()
            return
        if hasattr(supervisor, "completion_thread_id"):
            supervisor.completion_thread_id = None

    def _rollback_interrupted_adversary_reservation(self) -> None:
        self.store.update_bello_config(
            lambda cfg: cfg.model_copy(
                update={
                    "adversary_run_count": max(0, cfg.adversary_run_count - 1),
                }
            )
        )
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "interrupted_adversary_reservation_rolled_back",
            }
        )

    async def _recover_coder_thread_after_transport(
        self,
        *,
        start_continuation: bool,
    ) -> None:
        coder = getattr(self, "coder", None)
        cfg = self.store.get_bello_config()
        if coder is None or not cfg.coder_thread_id:
            raise RuntimeError("no persisted coder thread is available for recovery")
        coder.thread_id = cfg.coder_thread_id
        coder.active_turn_id = cfg.active_coder_turn_id
        try:
            thread = await coder.resume_thread()
        except Exception as exc:
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "coder_thread_resume_failed",
                    "thread_id": cfg.coder_thread_id,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
            await self._start_fallback_recovery_coder(
                coder,
                start_continuation=start_continuation,
            )
            return

        active_turn_id = cfg.active_coder_turn_id
        active_turn = _thread_turn_by_id(thread, active_turn_id)
        status = active_turn.get("status") if active_turn is not None else None
        if active_turn_id and active_turn is not None and status == "completed":
            text = last_agent_message_text(active_turn)
            if text:
                self._append_event(
                    AppEventSource.APP_SERVER,
                    "transport/replayedCoderMessage",
                    thread_id=cfg.coder_thread_id,
                    turn_id=active_turn_id,
                    reason="replayed from thread/resume after transport loss",
                )
                self.last_coder_message = CoderMessage(
                    text=text.strip(),
                    sequence=self._sequence,
                )
                self.tui.render("CODER", text.strip())
            coder.mark_turn_completed(active_turn_id)
            self._write_run_checkpoint("coder_turn_complete", state="stable")
            await self._handle_coder_turn_completed(item_id=None)
            return
        if active_turn_id and status == "inProgress":
            coder.active_turn_id = active_turn_id
            self.store.update_bello_config(
                lambda current: current.model_copy(
                    update={"active_coder_turn_id": active_turn_id}
                )
            )
            return
        if active_turn_id:
            self._clear_persisted_coder_turn(cfg.coder_thread_id, active_turn_id)
        if start_continuation:
            await coder.start_turn(TRANSPORT_RECOVERY_CODER_PROMPT)

    async def _start_fallback_recovery_coder(
        self,
        previous: CoderSession,
        *,
        start_continuation: bool,
    ) -> None:
        cfg = self.store.get_bello_config()
        replacement = CoderSession(
            self.client,
            self.store,
            self._active_workspace_root(),
            self._active_task_path(),
            model=self._active_coder_model(),
            fast=self._fast_mode(),
            intelligence=self._active_coder_intelligence(),
            multi_agent=self._multi_agent_config(),
            plan_path=self._active_coder_plan_path(),
        )
        self.coder = replacement
        new_thread_id = await replacement.start_thread()
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "coder_transport_recovery_fallback_thread",
                "previous_thread_id": cfg.coder_thread_id or previous.thread_id,
                "new_thread_id": new_thread_id,
            }
        )
        if start_continuation:
            await replacement.start_turn(TRANSPORT_RECOVERY_CODER_PROMPT)

    async def fail_provider(self, message: str) -> None:
        if not self.running and self.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE:
            return
        await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)

    async def _cleanup_preflight_probe_thread(self, thread_id: str) -> None:
        try:
            await self.client.thread_unsubscribe(thread_id)
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind="preflight_probe_thread",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    async def handle_user_command(self, command: UserCommand) -> None:
        text = command.text.strip()
        if not text:
            return
        self._append_event(AppEventSource.USER, "user/input", reason=text)
        if text == "/quit":
            await self.finalize("exited by user", status=BelloStatus.EXITED)
            return
        if text in {"/pause", "\x03"}:
            await self.pause()
            return
        if text == "/resume":
            current_status = self.store.get_bello_config().status
            if (
                getattr(self, "_finalizing", False)
                or getattr(self, "_terminal_cleanup_started", False)
                or current_status != BelloStatus.PAUSED
            ):
                return
            self.paused = False
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.RUNNING}))
            self.tui.status("resumed")
            return
        if text == "/restart":
            await self.restart("user requested supervised restart")
            return
        if text == "/status":
            cfg = self.store.get_bello_config()
            health = self.store.get_health()
            self.tui.render("SYSTEM", f"task={Path(cfg.task_path).name} generation={cfg.generation} active_turn={cfg.active_coder_turn_id} pending_approvals={len(self.pending_approvals)} restarts={health.restart_count}")
            return
        self._schedule_supervisor_check(
            f"Human message to supervisor: {text}",
            human_message=HumanMessage(text=command.text, sequence=self._sequence),
        )

    async def handle_server_request(self, message: AppServerMessage) -> None:
        context = normalize_approval_request(message)
        if getattr(self, "_terminal_cleanup_started", False):
            manager = getattr(self, "approvals", None) or ApprovalManager(
                self._active_workspace_root(),
                declared_grading_roots=getattr(self, "declared_grading_roots", ()),
                immutable_paths=self._immutable_approval_paths(),
            )
            resolution = manager._deny(context, "terminal state reached")
            await self.client.respond(context.server_request_id, manager.response_payload(context, resolution))
            self.tui.render("DENIED", f"{resolution.decision}: {resolution.reason}")
            return
        self.pending_approvals[context.server_request_id] = context
        self.store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"pending_server_request_ids": list(self.pending_approvals)})
        )
        self._append_event(
            AppEventSource.APPROVAL,
            context.server_request_method,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            item_id=context.item_id,
            reason=context.command or context.grant_root or context.request_type.value,
        )
        is_adversary_request = self._is_adversary_approval_context(context)
        if is_adversary_request:
            adversary_workspace_root = getattr(self, "_active_adversary_workspace_root", None)
            fallback_manager = ApprovalManager(
                adversary_workspace_root or self._active_workspace_root(),
                supervisor=self,
                declared_grading_roots=getattr(self, "declared_grading_roots", ()),
                immutable_paths=self._immutable_approval_paths(),
                adversary_mode=adversary_workspace_root is not None,
            )
            if adversary_workspace_root is None:
                resolution = fallback_manager._deny(context, "adversary snapshot workspace is not active")
            else:
                resolution = await fallback_manager.decide(context)
            response = fallback_manager.response_payload(context, resolution)
        elif self.approvals is None:
            fallback_manager = ApprovalManager(
                self._active_workspace_root(),
                declared_grading_roots=getattr(self, "declared_grading_roots", ()),
                immutable_paths=self._immutable_approval_paths(),
            )
            resolution = fallback_manager._deny(context, "approval manager not ready")
            response = fallback_manager.response_payload(context, resolution)
        else:
            resolution = await self.approvals.decide(context)
            response = self.approvals.response_payload(context, resolution)
        await self.client.respond(context.server_request_id, response)
        is_denial = _approval_resolution_is_denial(resolution.decision)
        decision_key = _approval_resolution_metric_key(resolution.decision)
        self._record_approval_metric(decision=decision_key, from_supervisor=resolution.from_supervisor)
        self.tui.render("DENIED" if is_denial else "APPROVAL", f"{resolution.decision}: {resolution.reason}")
        if resolution.persistent_decision:
            self.store.append_text_locked(DECISIONS, f"- {resolution.persistent_decision}\n")
        if is_denial:
            if is_adversary_request:
                denied_command = (context.command or context.grant_root or context.request_type.value or "").strip()
                if len(denied_command) > 200:
                    denied_command = denied_command[:197] + "..."
                denied_list = getattr(self, "_adversary_denied_commands", None)
                if denied_list is None:
                    denied_list = self._adversary_denied_commands = []
                denied_list.append(f"{denied_command} (denied: {resolution.reason})")
                self.store.append_text_locked(
                    PROGRESS,
                    f"- Adversary approval denied without steering coder: {resolution.reason}\n",
                )
            elif self.coder is not None:
                delivery_reason = resolution.reason
                if self._is_coder_descendant(context.thread_id):
                    delivery_reason = (
                        f"Approval for subagent {context.thread_id} was denied: {resolution.reason}. "
                        "As the parent coder, steer or stop that child and continue with a compliant approach."
                    )
                try:
                    delivered, _ = await self._deliver_coder_message(delivery_reason)
                    if not delivered:
                        return
                except AppServerError as exc:
                    if not _is_no_active_turn_to_steer_error(exc):
                        raise
                    self.tui.render("SUPERVISOR", f"denial delivered as approval response; starting a new coder turn: {exc}")
                    if hasattr(self.coder, "active_turn_id"):
                        self.coder.active_turn_id = None
                    self.store.update_bello_config(
                        lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None})
                    )
                    delivered, turn_id = await self._deliver_coder_message(
                        delivery_reason,
                        force_new_turn=True,
                    )
                    if not delivered:
                        return
                    if isinstance(turn_id, str):
                        self.store.update_bello_config(
                            lambda cfg: cfg.model_copy(update={"active_coder_turn_id": turn_id})
                        )
                    self.store.append_text_locked(
                        PROGRESS,
                        "- Approval denial was returned to app-server after the original turn ended; "
                        "started a new coder turn with the denial reason.\n",
                    )
            patch_health(self.store, HealthDelta(generation=self.store.get_health().generation, denied_requests=1, last_denial=resolution.reason))

    def _is_adversary_approval_context(self, context: ApprovalContext) -> bool:
        active_adversary_thread_id = getattr(self, "_active_adversary_thread_id", None)
        if active_adversary_thread_id and context.thread_id == active_adversary_thread_id:
            return True
        reviewer_role = self._reviewer_role_for_thread(context.thread_id)
        if reviewer_role is not None:
            return reviewer_role == "adversary"
        if not active_adversary_thread_id or not isinstance(context.thread_id, str):
            return False
        cfg = self.store.get_bello_config()
        if context.thread_id == cfg.coder_thread_id or self._is_coder_descendant(
            context.thread_id,
            cfg=cfg,
        ):
            return False
        # Child notifications normally establish ancestry before a request arrives. If
        # app-server delivers an adversary-child approval first, keep the unknown request
        # inside the active disposable-snapshot policy instead of risking canonical routing.
        return True

    async def decide_approval(self, context: ApprovalContext, reason: str) -> SupervisorDecision:
        if self.supervisor is None:
            raise SupervisorAgentError("supervisor not ready")
        self._reconcile_intervention_accounting()
        cfg = self.store.get_bello_config()
        wake_sequence = cfg.last_event_sequence + 1
        origin = "adversary_snapshot" if self._is_adversary_approval_context(context) else "coder"
        approval_context = _approval_wake_context(context, reason, origin=origin)
        packet = self.supervisor.build_packet(
            wake_sequence=wake_sequence,
            current_summary=f"Approval request needs judgment: {reason}",
            diff_summary=await self.diff_summary(),
            triggering_server_request_id=context.server_request_id,
            approval_context=approval_context,
            pending_approvals=[
                _approval_wake_context(
                    pending,
                    reason if pending.server_request_id == context.server_request_id else None,
                    origin="adversary_snapshot" if self._is_adversary_approval_context(pending) else "coder",
                )
                for pending in self.pending_approvals.values()
            ],
            subagents=self._subagent_summaries(),
            last_coder_message=self.last_coder_message,
            validations=list(self.validations),
            inspections=list(getattr(self, "inspections", [])),
            prior_interventions=list(self.prior_interventions),
            changed_files=await self.changed_files(),
            patch_summary=_patch_summary_from_approval_context(context) or await self.patch_summary(),
        )
        return await self.supervisor.decide(packet)

    async def handle_notification(self, message: AppServerMessage) -> None:
        params = message.params
        method = message.method or "notification"
        thread_id = _notification_thread_id(method, params)
        turn_id = _turn_id_from_params(params)
        item_id = _item_id_from_params(params)
        cfg = self.store.get_bello_config()
        if _is_stream_delta_method(method):
            # Completion/adversary commands are deliberately outside the coder evidence
            # ledger. Do not retain their potentially large output chunks waiting for a
            # coder item/completed event that can never consume them.
            if thread_id == cfg.coder_thread_id or self._is_coder_descendant(thread_id, cfg=cfg):
                self._record_command_output_delta(method, params, item_id=item_id)
            return
        event_payload = _bounded_subagent_event_payload(method, params)
        if self._exposes_review_private_input(event_payload):
            event_payload.pop("prompt", None)
        self._append_event(
            AppEventSource.APP_SERVER,
            method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            payload=event_payload,
        )
        if getattr(self, "_terminal_cleanup_started", False) and method != "serverRequest/resolved":
            return

        lifecycle_accepts_activity = self._coder_lifecycle_accepts_activity(cfg)
        await self._track_subagent_notification(
            method,
            params,
            thread_id=thread_id,
            turn_id=turn_id,
            enforce_policy=lifecycle_accepts_activity,
        )
        cfg = self.store.get_bello_config()
        lifecycle_accepts_activity = self._coder_lifecycle_accepts_activity(cfg)

        if method == "serverRequest/resolved":
            request_id = params.get("requestId")
            self.pending_approvals.pop(request_id, None)
            self.store.update_bello_config(
                lambda current: current.model_copy(update={"pending_server_request_ids": list(self.pending_approvals)})
            )
            return
        root_coder_notification = thread_id == cfg.coder_thread_id
        descendant_notification = self._is_coder_descendant(thread_id, cfg=cfg)
        if not lifecycle_accepts_activity and (root_coder_notification or descendant_notification):
            if method == "turn/started" and isinstance(thread_id, str) and isinstance(turn_id, str):
                await self._reject_late_coder_turn(
                    thread_id,
                    turn_id,
                    root=root_coder_notification,
                )
            elif method == "turn/completed" and root_coder_notification and isinstance(turn_id, str):
                if self.coder:
                    self.coder.mark_turn_completed(turn_id)
                self._clear_persisted_coder_turn(thread_id, turn_id)
            return
        if method == "turn/started" and thread_id == cfg.coder_thread_id and isinstance(turn_id, str):
            if self.coder:
                self.coder.active_turn_id = turn_id
            self._deferred_completion_check = None
            self._current_turn_action_count = 0
            self._generation_has_coder_turn = True
            self.store.update_bello_config(lambda current: current.model_copy(update={"active_coder_turn_id": turn_id}))
            self._write_run_checkpoint("coder", state="active")
            self.tui.render("CODER", f"turn started {turn_id}")
            return
        if method == "item/completed" and thread_id == cfg.coder_thread_id:
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    self.last_coder_message = CoderMessage(text=text, sequence=self._sequence)
                self.tui.render("CODER", text)
                return
            if _is_completed_action(item):
                await self._handle_completed_coder_action(
                    item,
                    item_id=item_id,
                    method=method,
                    thread_id=thread_id,
                    is_subagent=False,
                )
            return
        if method == "item/completed" and self._is_coder_descendant(thread_id, cfg=cfg):
            await self._handle_subagent_item_completed(
                params.get("item"),
                item_id=item_id,
                method=method,
                thread_id=thread_id,
            )
            return
        if method == "turn/completed" and thread_id == cfg.coder_thread_id:
            if self.coder and isinstance(turn_id, str):
                self.coder.mark_turn_completed(turn_id)
            self._write_run_checkpoint("coder_turn_complete", state="stable")
            await self._handle_coder_turn_completed(item_id=item_id)
            return
        if method in {"turn/completed", "thread/status/changed", "thread/closed"}:
            await self._resume_deferred_completion_if_quiescent()

    def _clear_persisted_coder_turn(self, thread_id: str | None, turn_id: str | None) -> None:
        if not thread_id or not turn_id:
            return
        coder = getattr(self, "coder", None)
        if (
            coder is not None
            and getattr(coder, "thread_id", None) == thread_id
            and getattr(coder, "active_turn_id", None) == turn_id
        ):
            coder.active_turn_id = None
        self.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "active_coder_turn_id": (
                        None
                        if current.coder_thread_id == thread_id
                        and current.active_coder_turn_id == turn_id
                        else current.active_coder_turn_id
                    )
                }
            )
        )

    async def _reject_late_coder_turn(self, thread_id: str, turn_id: str, *, root: bool) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "late_coder_turn_rejected",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "root": root,
                "status": self.store.get_bello_config().status.value,
            }
        )
        try:
            await self.client.turn_interrupt(thread_id, turn_id)
        except Exception as exc:
            if _is_turn_already_inactive_error(exc):
                if root:
                    self._clear_persisted_coder_turn(thread_id, turn_id)
                else:
                    state = self._subagent_registry().get(thread_id)
                    if state is not None and state.active_turn_id == turn_id:
                        state.active_turn_id = None
                        state.status = "interrupted"
                return
            self._append_cleanup_error(
                cleanup_kind="late_coder_turn_interrupt",
                thread_id=thread_id,
                turn_id=turn_id,
                error=exc,
            )
            if root:
                coder = getattr(self, "coder", None)
                if coder is not None and getattr(coder, "thread_id", None) == thread_id:
                    coder.active_turn_id = turn_id
                self.store.update_bello_config(
                    lambda current: current.model_copy(
                        update={
                            "active_coder_turn_id": (
                                turn_id if current.coder_thread_id == thread_id else current.active_coder_turn_id
                            )
                        }
                    )
                )
            quiesced = await self._quiesce_coder_tree("late_turn_notification", strict=False)
            if quiesced:
                if root:
                    self._clear_persisted_coder_turn(thread_id, turn_id)
                return
            cfg = self.store.get_bello_config()
            message = (
                f"late coder turn {turn_id} on {thread_id} could not be interrupted during "
                f"{cfg.status.value} lifecycle cleanup"
            )
            if cfg.status == BelloStatus.PAUSED and not getattr(self, "_finalizing", False):
                await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)
                return
            try:
                await self.client.stop()
            except Exception as stop_error:
                self._append_cleanup_error(
                    cleanup_kind="late_coder_turn_process_tree_stop",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    error=stop_error,
                )
            return
        if root:
            self._clear_persisted_coder_turn(thread_id, turn_id)
        else:
            state = self._subagent_registry().get(thread_id)
            if state is not None and state.active_turn_id == turn_id:
                state.active_turn_id = None
                state.status = "interrupted"

    def _record_command_output_delta(self, method: str, params: dict[str, Any], *, item_id: str | None) -> None:
        if not _is_command_output_delta_method(method):
            return
        if not item_id:
            return
        text = _output_delta_text(params)
        if not text:
            return
        chunks = getattr(self, "_command_output_chunks", None)
        if chunks is None:
            chunks = {}
            self._command_output_chunks = chunks
        chunks.setdefault(item_id, []).append(text)

    def _pop_command_output(self, item_id: str | None) -> str:
        if not item_id:
            return ""
        chunks = getattr(self, "_command_output_chunks", None)
        if not chunks:
            return ""
        return "".join(chunks.pop(item_id, []))

    async def _handle_subagent_item_completed(
        self,
        item: Any,
        *,
        item_id: str | None,
        method: str,
        thread_id: str | None,
    ) -> None:
        if not isinstance(thread_id, str):
            return
        state = self._subagent_registry().get(thread_id)
        if state is None:
            return
        if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                state.last_message = _bounded_subagent_text(text)
                state.last_sequence = self._sequence
                self.tui.render("SUBAGENT", f"{_short_thread_id(thread_id)}: {state.last_message}")
            return
        if _is_completed_action(item):
            await self._handle_completed_coder_action(
                item,
                item_id=item_id,
                method=method,
                thread_id=thread_id,
                is_subagent=True,
            )

    async def _handle_completed_coder_action(
        self,
        item: dict[str, Any],
        *,
        item_id: str | None,
        method: str,
        thread_id: str | None,
        is_subagent: bool,
    ) -> None:
        summary = _item_summary(item)
        display_summary = (
            f"subagent {_short_thread_id(thread_id)}: {summary}" if is_subagent else summary
        )
        if not is_subagent:
            self._current_turn_action_count = getattr(self, "_current_turn_action_count", 0) + 1
        persisted_summary = display_summary
        if self._exposes_review_private_input((item, display_summary)):
            persisted_summary = (
                "subagent workspace action completed"
                if is_subagent
                else "workspace action completed"
            )
        self.store.append_recent_action(persisted_summary)
        triggering_action = _triggering_action_from_item(item, item_id=item_id, summary=display_summary)
        repaired_runtime_controls = self._repair_snapshot_runtime_controls(
            source="subagent_action" if is_subagent else "coder_action"
        )
        if await self._escalate_runtime_integrity_issue(
            source="subagent_action" if is_subagent else "coder_action"
        ):
            return
        self._record_changed_files(triggering_action)
        declared_grading_issue = self._declared_grading_access_issue(triggering_action)
        if declared_grading_issue is not None:
            if is_subagent:
                declared_grading_issue = (
                    f"subagent {_short_thread_id(thread_id)}: {declared_grading_issue}"
                )
            self.tui.render("INTEGRITY", declared_grading_issue)
            self.store.append_text_locked(PROGRESS, f"- Integrity failure: {declared_grading_issue}\n")
            self._append_event(
                AppEventSource.SUPERVISOR,
                "integrity/declared_grading_path_access",
                thread_id=thread_id,
                reason=declared_grading_issue,
            )
            await self.finalize(
                f"escalated: {declared_grading_issue}",
                status=BelloStatus.ESCALATED,
            )
            return
        validation_item = _item_with_recorded_output(item, self._pop_command_output(item_id))
        validation = _validation_from_action(
            triggering_action,
            sequence=self._sequence,
            item=validation_item,
            changed_paths=list(getattr(self, "observed_changed_files", {}) or {}),
        )
        inspection = _inspection_from_action(
            triggering_action,
            sequence=self._sequence,
            item=validation_item,
        )
        if validation is not None and self._exposes_review_private_input(validation):
            validation = None
        if inspection is not None and self._exposes_review_private_input(inspection):
            inspection = None
        validation_trigger_reasons: tuple[str, ...] = ()
        if validation is not None:
            self.validations.append(validation)
            self.validations = self.validations[-VALIDATION_LEDGER_LIMIT:]
            self._record_validation_progress(validation)
            validation_trigger_reasons = self._record_validation_runtime_state(validation)
        if inspection is not None:
            self.inspections.append(inspection)
            self.inspections = self.inspections[-INSPECTION_LEDGER_LIMIT:]
        changed_files = await self.changed_files()
        self._update_relevant_edit_state(changed_files)
        if is_subagent:
            state = self._subagent_registry().get(thread_id or "")
            if state is not None:
                state.record_action(
                    display_summary,
                    sequence=self._sequence,
                    kind=str(item.get("type") or "action"),
                    item_id=item_id,
                )
                if validation is not None:
                    state.validation_ids.append(validation.validation_id)
                    state.validation_ids = state.validation_ids[-SUBAGENT_ACTION_LIMIT:]
            self.tui.render("SUBAGENT", display_summary)
            return
        runtime_decision = self.should_wake_runtime_supervisor(
            action=triggering_action,
            validation=validation,
            changed_files=changed_files,
            validation_trigger_reasons=validation_trigger_reasons,
        )
        if repaired_runtime_controls:
            runtime_decision = RuntimeTriggerDecision(
                should_wake=True,
                reasons=tuple(dict.fromkeys((*runtime_decision.reasons, "runtime_control_replacement"))),
                restart_reason=runtime_decision.restart_reason,
            )
        self.tui.render("TOOL", summary)
        self._record_runtime_trigger_trace(
            event_type=method,
            action=triggering_action,
            validation=validation,
            changed_files=changed_files,
            decision=runtime_decision,
        )
        if runtime_decision.should_wake:
            runtime_summary = f"Runtime trigger ({', '.join(runtime_decision.reasons)}): {summary}"
            if runtime_decision.restart_reason is not None:
                runtime_summary = (
                    f"Runtime trigger ({', '.join(runtime_decision.reasons)}): "
                    f"restart candidate because {runtime_decision.restart_reason}; {summary}"
                )
            self._schedule_supervisor_check(
                runtime_summary,
                triggering_item_id=item_id,
                triggering_action=triggering_action,
                patch_summary=_patch_summary_from_item(item),
            )

    def _subagent_registry(self) -> dict[str, SubagentRuntimeState]:
        registry = getattr(self, "_subagents", None)
        if registry is None:
            registry = {}
            self._subagents = registry
        return registry

    def _subagent_policy_notifications(self) -> set[str]:
        notified = getattr(self, "_subagent_policy_notified", None)
        if notified is None:
            notified = set()
            self._subagent_policy_notified = notified
        return notified

    async def _track_subagent_notification(
        self,
        method: str,
        params: dict[str, Any],
        *,
        thread_id: str | None,
        turn_id: str | None,
        enforce_policy: bool = True,
    ) -> None:
        cfg = self.store.get_bello_config()
        if method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, dict):
                self._upsert_subagent_thread(thread, generation=cfg.generation)
        elif method == "thread/status/changed" and isinstance(thread_id, str):
            state = self._subagent_registry().get(thread_id)
            if state is not None:
                state.status = _thread_status_type(params.get("status"))
                if state.status != "active":
                    state.active_turn_id = None
                state.last_sequence = self._sequence
        elif method == "thread/closed" and isinstance(thread_id, str):
            state = self._subagent_registry().get(thread_id)
            if state is not None:
                state.status = "shutdown"
                state.active_turn_id = None
                state.last_sequence = self._sequence
        elif method == "turn/started" and isinstance(thread_id, str):
            state = self._subagent_registry().get(thread_id)
            if state is not None:
                state.status = "active"
                state.active_turn_id = turn_id
                state.last_sequence = self._sequence
        elif method == "turn/completed" and isinstance(thread_id, str):
            state = self._subagent_registry().get(thread_id)
            if state is not None:
                state.status = _turn_terminal_status(params.get("turn"))
                state.active_turn_id = None
                state.last_sequence = self._sequence

        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "collabAgentToolCall":
                await self._track_collab_agent_tool_call(item, event_thread_id=thread_id)

        # A nested child can arrive before its parent notification. Re-evaluate ancestry
        # whenever the registry changes so the relevant role policy is enforced as soon as
        # the chain to a coder or reviewer root becomes known.
        if enforce_policy:
            for state in tuple(self._subagent_registry().values()):
                if self._subagent_multi_agent_policy(state.thread_id, cfg=cfg) is not None:
                    await self._enforce_subagent_profile(state)

    def _upsert_subagent_thread(
        self,
        thread: dict[str, Any],
        *,
        generation: int,
    ) -> SubagentRuntimeState | None:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            return None
        parent_thread_id = thread.get("parentThreadId")
        registry = self._subagent_registry()
        state = registry.get(thread_id)
        if state is None:
            state = SubagentRuntimeState(
                thread_id=thread_id,
                parent_thread_id=parent_thread_id if isinstance(parent_thread_id, str) else None,
                generation=generation,
            )
            registry[thread_id] = state
        elif isinstance(parent_thread_id, str):
            state.parent_thread_id = parent_thread_id
        state.status = _thread_status_type(thread.get("status"))
        if state.status != "active":
            state.active_turn_id = None
        state.nickname = _optional_bounded_text(thread.get("agentNickname"), 120) or state.nickname
        state.role = _optional_bounded_text(thread.get("agentRole"), 120) or state.role
        state.last_sequence = max(state.last_sequence, self._sequence)
        return state

    async def _track_collab_agent_tool_call(
        self,
        item: dict[str, Any],
        *,
        event_thread_id: str | None,
    ) -> None:
        sender = item.get("senderThreadId")
        parent_thread_id = sender if isinstance(sender, str) else event_thread_id
        receivers = [value for value in item.get("receiverThreadIds") or [] if isinstance(value, str)]
        agents_states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
        cfg = self.store.get_bello_config()
        for receiver in receivers:
            registry = self._subagent_registry()
            state = registry.get(receiver)
            if state is None:
                state = SubagentRuntimeState(
                    thread_id=receiver,
                    parent_thread_id=parent_thread_id,
                    generation=cfg.generation,
                )
                registry[receiver] = state
            elif state.parent_thread_id is None and isinstance(parent_thread_id, str):
                state.parent_thread_id = parent_thread_id
            if item.get("tool") == "spawnAgent":
                if isinstance(item.get("model"), str):
                    state.model = item["model"]
                if isinstance(item.get("reasoningEffort"), str):
                    state.reasoning_effort = item["reasoningEffort"]
                if isinstance(item.get("prompt"), str):
                    state.prompt = _bounded_subagent_text(item["prompt"], limit=600)
            agent_state = agents_states.get(receiver)
            if isinstance(agent_state, dict):
                state.status = str(agent_state.get("status") or state.status)
                message = agent_state.get("message")
                if isinstance(message, str) and message.strip():
                    state.last_message = _bounded_subagent_text(message, limit=600)
            elif isinstance(agent_state, str):
                state.status = agent_state
            if state.status in {"interrupted", "completed", "errored", "shutdown", "notFound"}:
                state.active_turn_id = None
            state.last_sequence = self._sequence

    def _is_coder_descendant(
        self,
        thread_id: Any,
        *,
        cfg: BelloConfig | None = None,
    ) -> bool:
        if not isinstance(thread_id, str):
            return False
        cfg = cfg or self.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        if not isinstance(root_thread_id, str) or thread_id == root_thread_id:
            return False
        seen: set[str] = set()
        current = thread_id
        for _ in range(32):
            if current in seen:
                return False
            seen.add(current)
            state = self._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return False
            if state.parent_thread_id == root_thread_id:
                return state.generation == cfg.generation
            current = state.parent_thread_id
        return False

    def _subagent_depth_from_root(self, thread_id: Any, root_thread_id: Any) -> int | None:
        if not isinstance(thread_id, str) or not isinstance(root_thread_id, str):
            return None
        if thread_id == root_thread_id:
            return 0
        seen: set[str] = set()
        current = thread_id
        depth = 0
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            depth += 1
            if state.parent_thread_id == root_thread_id:
                return depth
            current = state.parent_thread_id
        return None

    def _subagent_depth(self, thread_id: str, *, cfg: BelloConfig | None = None) -> int:
        cfg = cfg or self.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        depth = 0
        current = thread_id
        seen: set[str] = set()
        while current not in seen and depth < 32:
            seen.add(current)
            state = self._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                break
            depth += 1
            if state.parent_thread_id == root_thread_id:
                return max(1, depth)
            current = state.parent_thread_id
        return max(1, depth)

    def _active_coder_subagents(self) -> list[SubagentRuntimeState]:
        cfg = self.store.get_bello_config()
        active: list[SubagentRuntimeState] = []
        for state in self._subagent_registry().values():
            if not self._is_coder_descendant(state.thread_id, cfg=cfg):
                continue
            if state.active_turn_id or state.status in {"active", "running", "pendingInit", "inProgress"}:
                active.append(state)
        return active

    def _subagent_summaries(self) -> list[SubagentSummary]:
        cfg = self.store.get_bello_config()
        states = [
            state
            for state in self._subagent_registry().values()
            if self._is_coder_descendant(state.thread_id, cfg=cfg)
        ]
        states.sort(
            key=lambda state: (
                not bool(state.active_turn_id or state.status in {"active", "running", "pendingInit", "inProgress"}),
                -state.last_sequence,
            )
        )
        summaries: list[SubagentSummary] = []
        for state in states[:SUBAGENT_SUMMARY_LIMIT]:
            parent = state.parent_thread_id
            if not isinstance(parent, str):
                continue
            actions = [
                SubagentActivity(
                    sequence=sequence,
                    kind=kind,
                    summary=_bounded_subagent_text(summary, limit=400),
                    item_id=item_id,
                )
                for sequence, kind, summary, item_id in state.recent_actions[-SUBAGENT_ACTION_LIMIT:]
            ]
            summaries.append(
                SubagentSummary(
                    thread_id=state.thread_id,
                    parent_thread_id=parent,
                    depth=self._subagent_depth(state.thread_id, cfg=cfg),
                    status=state.status,
                    active_turn_id=state.active_turn_id,
                    model=state.model,
                    reasoning_effort=state.reasoning_effort,
                    nickname=state.nickname,
                    role=state.role,
                    prompt=_optional_bounded_text(state.prompt, 600),
                    last_message=_optional_bounded_text(state.last_message, 600),
                    recent_actions=actions,
                    validation_ids=state.validation_ids[-8:],
                    last_event_sequence=state.last_sequence or None,
                    profile_allowed=state.profile_allowed,
                )
            )
        return summaries

    async def _enforce_subagent_profile(self, state: SubagentRuntimeState) -> None:
        policy = self._subagent_multi_agent_policy(state.thread_id)
        if policy is None:
            return
        role, multi_agent = policy
        default = getattr(multi_agent, "default", None)
        model = state.model or getattr(default, "model", None)
        intelligence = state.reasoning_effort or getattr(default, "intelligence", None)
        if not isinstance(model, str) or not isinstance(intelligence, str):
            return
        state.model = model
        state.reasoning_effort = intelligence
        allowed = bool(
            getattr(multi_agent, "enabled", False)
            and getattr(multi_agent, "is_allowed", lambda *_: False)(model, intelligence)
        )
        reviewer_depth = self._reviewer_descendant_depth(state.thread_id)
        depth_allowed = role == "coder" or reviewer_depth == 1
        allowed = allowed and depth_allowed
        state.profile_allowed = allowed
        if allowed:
            return
        if state.active_turn_id:
            await self._interrupt_subagent(
                state,
                cleanup_kind=f"{role}_subagent_policy",
            )
        notified = self._subagent_policy_notifications()
        if state.thread_id in notified:
            return
        notified.add(state.thread_id)
        allowed_text = _format_allowed_subagent_profiles(multi_agent)
        if not depth_allowed:
            reason = (
                f"Reviewer subagent {state.thread_id} attempted nested delegation at depth "
                f"{reviewer_depth}; reviewer delegation is limited to one child level."
            )
        else:
            reason = (
                f"{role.replace('_', ' ').title()} subagent {state.thread_id} used forbidden profile "
                f"{model}/{intelligence}. Allowed profiles: {allowed_text}. Stop that child and, if "
                "delegation is still useful, spawn a replacement using an allowed profile."
            )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "subagent/depth_denied" if not depth_allowed else "subagent/profile_denied",
            thread_id=state.thread_id,
            reason=reason,
        )
        self.store.append_text_locked(PROGRESS, f"- {reason}\n")
        self.tui.render("SUPERVISOR", reason)
        if role == "coder" and self.coder is not None:
            await self._deliver_coder_message(reason)

    def _subagent_multi_agent_policy(
        self,
        thread_id: Any,
        *,
        cfg: BelloConfig | None = None,
    ) -> tuple[str, Any] | None:
        if self._is_coder_descendant(thread_id, cfg=cfg):
            return "coder", self._multi_agent_config()
        reviewer_role = self._reviewer_role_for_thread(thread_id)
        reviewer_depth = self._reviewer_descendant_depth(thread_id)
        if reviewer_depth is None or reviewer_depth < 1:
            return None
        if reviewer_role == "completion_review":
            return reviewer_role, self._completion_multi_agent_config()
        if reviewer_role == "adversary":
            return reviewer_role, self._adversary_multi_agent_config()
        return None

    def _multi_agent_config(self) -> Any:
        project_config = getattr(self, "project_config", None)
        if project_config is None:
            return MultiAgentConfig()
        return getattr(project_config, "multi_agent", None)

    def _completion_multi_agent_config(self) -> Any:
        project_config = getattr(self, "project_config", None)
        if project_config is None:
            return MultiAgentConfig()
        return getattr(project_config, "completion_multi_agent", MultiAgentConfig())

    def _adversary_multi_agent_config(self) -> Any:
        project_config = getattr(self, "project_config", None)
        if project_config is None:
            return MultiAgentConfig()
        return getattr(project_config, "adversary_multi_agent", MultiAgentConfig())

    async def _refresh_coder_subagents(self) -> None:
        cfg = self.store.get_bello_config()
        root_thread_id = cfg.coder_thread_id
        client = getattr(self, "client", None)
        if not isinstance(root_thread_id, str) or client is None or not hasattr(client, "thread_list"):
            return
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {
                "archived": False,
                "cwd": str(self._active_workspace_root()),
                "limit": 100,
                "sourceKinds": list(SUBAGENT_SOURCE_KINDS),
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.thread_list(params)
            threads = response.get("data")
            if not isinstance(threads, list):
                break
            for thread in threads:
                if isinstance(thread, dict):
                    self._upsert_subagent_thread(thread, generation=cfg.generation)
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        for state in tuple(self._subagent_registry().values()):
            if not self._is_coder_descendant(state.thread_id, cfg=cfg):
                continue
            if state.status != "active" or state.active_turn_id is not None:
                continue
            if not hasattr(client, "thread_turns_list"):
                continue
            response = await client.thread_turns_list(
                state.thread_id,
                limit=1,
                items_view="summary",
                sort_direction="desc",
            )
            turns = response.get("data")
            if isinstance(turns, list) and turns:
                turn = turns[0]
                if isinstance(turn, dict) and turn.get("status") == "inProgress" and isinstance(turn.get("id"), str):
                    state.active_turn_id = turn["id"]

    async def _refresh_reviewer_subagents(
        self,
        root_thread_id: str,
        workspace_root: Path,
    ) -> None:
        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "thread_list"):
            return
        cfg = self.store.get_bello_config()
        cursor: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {
                "archived": False,
                "cwd": str(workspace_root.resolve()),
                "limit": 100,
                "sourceKinds": list(SUBAGENT_SOURCE_KINDS),
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = await client.thread_list(params)
            threads = response.get("data")
            if not isinstance(threads, list):
                break
            for thread in threads:
                if isinstance(thread, dict):
                    self._upsert_subagent_thread(thread, generation=cfg.generation)
            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        if not hasattr(client, "thread_turns_list"):
            return
        for state in tuple(self._subagent_registry().values()):
            if self._subagent_depth_from_root(state.thread_id, root_thread_id) is None:
                continue
            if state.status != "active" or state.active_turn_id is not None:
                continue
            response = await client.thread_turns_list(
                state.thread_id,
                limit=1,
                items_view="summary",
                sort_direction="desc",
            )
            turns = response.get("data")
            if isinstance(turns, list) and turns:
                turn = turns[0]
                if (
                    isinstance(turn, dict)
                    and turn.get("status") == "inProgress"
                    and isinstance(turn.get("id"), str)
                ):
                    state.active_turn_id = turn["id"]

    async def _cleanup_completion_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: Path,
    ) -> None:
        await self._cleanup_reviewer_descendants(
            root_thread_id,
            workspace_root,
            cleanup_kind="completion_review_subagent",
        )

    async def _cleanup_adversary_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: Path,
    ) -> None:
        await self._cleanup_reviewer_descendants(
            root_thread_id,
            workspace_root,
            cleanup_kind="adversary_subagent",
        )

    async def _cleanup_reviewer_descendants(
        self,
        root_thread_id: str,
        workspace_root: Path,
        *,
        cleanup_kind: str,
    ) -> None:
        try:
            await self._refresh_reviewer_subagents(root_thread_id, workspace_root)
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind=f"{cleanup_kind}_refresh",
                thread_id=root_thread_id,
                turn_id=None,
                error=exc,
            )
        descendants = [
            (depth, state)
            for state in self._subagent_registry().values()
            if (depth := self._subagent_depth_from_root(state.thread_id, root_thread_id))
            is not None
            and depth > 0
        ]
        descendants.sort(key=lambda item: item[0], reverse=True)
        for _, state in descendants:
            if state.active_turn_id:
                try:
                    await self._interrupt_subagent(
                        state,
                        cleanup_kind=f"{cleanup_kind}_interrupt",
                    )
                except Exception:
                    pass
            try:
                if hasattr(self.client, "thread_archive"):
                    await self.client.thread_archive(state.thread_id)
                elif hasattr(self.client, "thread_unsubscribe"):
                    await self.client.thread_unsubscribe(state.thread_id)
            except Exception as exc:
                self._append_cleanup_error(
                    cleanup_kind=f"{cleanup_kind}_archive",
                    thread_id=state.thread_id,
                    turn_id=state.active_turn_id,
                    error=exc,
                )
                try:
                    if hasattr(self.client, "thread_unsubscribe"):
                        await self.client.thread_unsubscribe(state.thread_id)
                except Exception as unsubscribe_exc:
                    self._append_cleanup_error(
                        cleanup_kind=f"{cleanup_kind}_unsubscribe",
                        thread_id=state.thread_id,
                        turn_id=state.active_turn_id,
                        error=unsubscribe_exc,
                    )
            state.status = "shutdown"
            state.active_turn_id = None

    async def _interrupt_subagent(
        self,
        state: SubagentRuntimeState,
        *,
        cleanup_kind: str,
    ) -> None:
        if not state.active_turn_id:
            return
        try:
            await self.client.turn_interrupt(state.thread_id, state.active_turn_id)
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind=cleanup_kind,
                thread_id=state.thread_id,
                turn_id=state.active_turn_id,
                error=exc,
            )
            raise
        state.status = "interrupted"
        state.active_turn_id = None

    async def _quiesce_coder_tree(self, reason: str, *, strict: bool = True) -> bool:
        mutex = getattr(self, "_coder_quiesce_mutex", None)
        if mutex is None:
            mutex = asyncio.Lock()
            self._coder_quiesce_mutex = mutex
        async with mutex:
            self._quiescing_coder_tree = True
            try:
                coder = getattr(self, "coder", None)
                if coder is not None:
                    try:
                        await coder.interrupt()
                    except Exception as exc:
                        self._append_cleanup_error(
                            cleanup_kind=f"{reason}_coder_interrupt",
                            thread_id=getattr(coder, "thread_id", None) or "unknown",
                            turn_id=getattr(coder, "active_turn_id", None),
                            error=exc,
                        )
                        if strict:
                            raise
                        return False
                try:
                    for _ in range(3):
                        await self._refresh_coder_subagents()
                        active = sorted(
                            self._active_coder_subagents(),
                            key=lambda state: self._subagent_depth(state.thread_id),
                            reverse=True,
                        )
                        if not active:
                            break
                        for state in active:
                            await self._interrupt_subagent(
                                state,
                                cleanup_kind=f"{reason}_subagent_interrupt",
                            )
                    await self._refresh_coder_subagents()
                    remaining = self._active_coder_subagents()
                    if remaining:
                        ids = ", ".join(state.thread_id for state in remaining)
                        raise RuntimeError(f"coder descendants did not quiesce: {ids}")
                except Exception:
                    if strict:
                        raise
                    return False
                return True
            finally:
                self._quiescing_coder_tree = False

    async def _resume_deferred_completion_if_quiescent(self) -> None:
        queued = getattr(self, "_deferred_completion_check", None)
        if queued is None or self._active_coder_subagents():
            return
        cfg = self.store.get_bello_config()
        if cfg.active_coder_turn_id:
            return
        self._deferred_completion_check = None
        if not queued.completion_review:
            await self._finalize_completion_review_disabled()
            return
        self._schedule_supervisor_check(
            queued.summary,
            triggering_item_id=queued.triggering_item_id,
            triggering_action=queued.triggering_action,
            human_message=queued.human_message,
            patch_summary=queued.patch_summary,
            completion_review=queued.completion_review,
        )

    async def _handle_coder_turn_completed(self, *, item_id: str | None) -> None:
        repaired_runtime_controls = self._repair_snapshot_runtime_controls(source="coder_turn_completed")
        if await self._escalate_runtime_integrity_issue(source="coder_turn_completed"):
            return
        if repaired_runtime_controls:
            self._schedule_supervisor_check(
                "Runtime integrity trigger: coder workspace runtime links were replaced and restored.",
                triggering_item_id=item_id,
            )
            return
        message = self.last_coder_message
        if message is not None and _has_readiness_marker(message.text):
            if self._last_completion_marker_sequence != message.sequence:
                await self._refresh_coder_subagents()
                active_subagents = self._active_coder_subagents()
                if active_subagents:
                    child_ids = ", ".join(_short_thread_id(state.thread_id) for state in active_subagents)
                    self._deferred_completion_check = QueuedSupervisorCheck(
                        summary="Coder provided exact readiness marker; waiting for active subagents before completion.",
                        triggering_item_id=item_id,
                        completion_review=self._effective_completion_review(),
                    )
                    reason = (
                        "Coder declared readiness while relevant subagents are still active: "
                        f"{child_ids}. Wait for their results, review and integrate them, then emit the readiness marker again."
                    )
                    self._append_event(
                        AppEventSource.SUPERVISOR,
                        "completion/deferred_for_subagents",
                        reason=reason,
                    )
                    await self._steer_for_marker(reason, sequence=message.sequence, message=reason)
                    return
                self._last_completion_marker_sequence = message.sequence
                self.no_marker_idle_nudge_count = 0
                done_gap = await self._done_without_fresh_behavioral_validation()
                if done_gap is not None:
                    self._record_runtime_trigger_trace(
                        event_type="turn/completed",
                        action=TriggeringAction(
                            item_id=item_id,
                            kind="done",
                            status="completed",
                            summary=done_gap,
                        ),
                        validation=None,
                        changed_files=await self.changed_files(),
                        decision=RuntimeTriggerDecision(
                            should_wake=True,
                            reasons=("done_without_fresh_validation",),
                        ),
                    )
                    self._schedule_supervisor_check(
                        f"Runtime trigger (done_without_fresh_validation): {done_gap}",
                        triggering_item_id=item_id,
                    )
                    return
                await self._continue_after_readiness_marker(triggering_item_id=item_id)
            return
        if message is not None and _has_malformed_readiness_marker(message.text):
            await self._steer_for_marker(
                "Coder used a malformed readiness marker; require exact marker only after validation.",
                sequence=message.sequence,
            )
            return
        if message is not None and _appears_to_claim_readiness(message.text):
            await self._steer_for_marker(
                "Coder appears to be claiming readiness but did not provide exact readiness marker.",
                sequence=message.sequence,
            )
            return
        if self.pending_approvals:
            self._schedule_supervisor_check("Coder turn completed", triggering_item_id=item_id)
            return
        if getattr(self, "_current_turn_action_count", 0) == 0:
            await self._handle_no_marker_idle()
            return
        self._schedule_supervisor_check("Coder turn completed", triggering_item_id=item_id)

    async def _continue_after_readiness_marker(
        self,
        *,
        triggering_item_id: str | None,
        subagents_refreshed: bool = False,
    ) -> None:
        if not subagents_refreshed:
            await self._refresh_coder_subagents()
        active_subagents = self._active_coder_subagents()
        if active_subagents:
            self._deferred_completion_check = QueuedSupervisorCheck(
                summary="Coder provided exact readiness marker; continuing completion after active subagents finish.",
                triggering_item_id=triggering_item_id,
                completion_review=self._effective_completion_review(),
            )
            reason = "Wait for active subagents, review and integrate their results, then emit the readiness marker again."
            self._append_event(
                AppEventSource.SUPERVISOR,
                "completion/deferred_for_subagents",
                reason=reason,
            )
            await self._steer_for_marker(reason, message=reason)
            return
        if not self._effective_completion_review():
            await self._finalize_completion_review_disabled()
            return
        self._schedule_supervisor_check(
            "Coder provided exact readiness marker; running completion_review.",
            triggering_item_id=triggering_item_id,
            completion_review=True,
        )

    async def _done_without_fresh_behavioral_validation(self) -> str | None:
        changed_files = await self.changed_files()
        self._update_relevant_edit_state(changed_files)
        cfg = self.store.get_bello_config()
        latest_relevant_edit = cfg.last_relevant_edit_sequence
        if latest_relevant_edit is None:
            return None
        if any(_validation_is_fresh_behavioral_pass(validation, latest_relevant_edit) for validation in self.validations):
            return None
        return (
            "coder marked done without a trusted fresh behavioral validation after "
            f"relevant edit sequence {latest_relevant_edit}"
        )

    async def _steer_for_marker(
        self,
        reason: str,
        *,
        sequence: int | None = None,
        message: str = NO_MARKER_IDLE_NUDGE,
    ) -> None:
        cfg = self.store.get_bello_config()
        self.prior_interventions.append(
            PriorIntervention(reason=reason, message_to_coder=message, sequence=sequence or cfg.last_event_sequence)
        )
        self.prior_interventions = self.prior_interventions[-20:]
        patch_health(self.store, HealthDelta(generation=cfg.generation, interventions=1))
        self.tui.render("SUPERVISOR", reason)
        if self.coder:
            await self._deliver_coder_message(message)

    async def _handle_no_marker_idle(self) -> None:
        cfg = self.store.get_bello_config()
        if cfg.active_coder_turn_id:
            return
        await self._refresh_coder_subagents()
        if self._active_coder_subagents():
            return
        if not getattr(self, "_generation_has_coder_turn", True):
            # A freshly restarted generation has produced no coder work yet: forcing a completion
            # review here would judge the previous generation's leftover state (observed killing a
            # run via restart-with-exhausted-budget). Kick the coder instead; steer_or_start starts
            # a turn if the restart kickoff died.
            if self.coder:
                await self._deliver_coder_message(POST_RESTART_CONTINUE_NUDGE)
            return
        latest_validation_sequence = max((validation.sequence for validation in self.validations), default=None)
        last_message_sequence = self.last_coder_message.sequence if self.last_coder_message is not None else None
        review_key = f"{cfg.generation}:{last_message_sequence}:{latest_validation_sequence}"
        if getattr(self, "_no_marker_completion_review_key", None) == review_key:
            return
        self._no_marker_completion_review_key = review_key
        if not self._effective_completion_review():
            # No review gate to force: nudge the coder to finish and emit the marker,
            # which is the only terminal signal in this mode.
            await self._steer_for_marker(
                "Coder is idle with no active turn and no readiness marker; completion review is disabled, nudging coder to finish.",
            )
            return
        self.store.append_text_locked(
            PROGRESS,
            "- Controller forcing completion_review: coder is idle with no active turn and no readiness marker.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "completion/no_marker_idle_review",
            reason="coder idle with no active turn and no readiness marker",
        )
        self._schedule_supervisor_check(
            "Coder is idle with no active turn and no readiness marker. Run completion_review on the current state.",
            completion_review=True,
        )

    async def pause(self) -> None:
        if getattr(self, "_finalizing", False):
            return
        self._restart_transition_token = None
        self.paused = True
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.PAUSED}))
        self._supervisor_next_runtime_check = None
        self._supervisor_next_completion_check = None
        self._supervisor_next_runtime_summary = None
        self._supervisor_next_completion_summary = None
        self._pending_runtime_trigger_signatures = {}
        self._pending_runtime_trigger_actions = {}
        self._sync_legacy_supervisor_queue_fields()
        await self._wait_for_revision_switch()
        await self._wait_for_coder_activity()
        if getattr(self, "_finalizing", False):
            return
        supervisor_task = getattr(self, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not asyncio.current_task():
            await self._stop_supervisor_task()
        await self._close_completion_review_session()
        await self._quiesce_coder_tree("pause")
        await self._resolve_pending_approvals("paused")
        self.tui.status("paused")

    async def restart(self, reason: str, *, handoff: RestartHandoff | None = None) -> None:
        if getattr(self, "_finalizing", False):
            return
        cfg = self.store.get_bello_config()
        if getattr(self, "paused", False) or cfg.status == BelloStatus.PAUSED:
            self.tui.status("paused; resume before restarting")
            return
        if cfg.restart_count >= cfg.max_restarts:
            await self.finalize("restart cap reached", status=BelloStatus.STUCK)
            return
        transition_token = object()
        self._restart_transition_token = transition_token
        self._append_event(AppEventSource.SUPERVISOR, "controller/restart", reason=reason)
        self.store.update_bello_config(lambda current: current.model_copy(update={"status": BelloStatus.RESTARTING}))
        await self._wait_for_revision_switch()
        restart_cap_reached = False
        try:
            async with self._coder_activity_lock():
                current = self.store.get_bello_config()
                if not self._restart_transition_is_current(
                    transition_token,
                    expected_generation=current.generation,
                    expected_thread_id=current.coder_thread_id,
                ):
                    return
                if current.restart_count >= current.max_restarts:
                    restart_cap_reached = True
                else:
                    await self._restart_after_activity_barrier(
                        reason,
                        handoff=handoff,
                        previous_config=current,
                        transition_token=transition_token,
                    )
        finally:
            if self._restart_transition_token is transition_token:
                self._restart_transition_token = None
        if restart_cap_reached:
            await self.finalize("restart cap reached", status=BelloStatus.STUCK)

    async def _restart_after_activity_barrier(
        self,
        reason: str,
        *,
        handoff: RestartHandoff | None,
        previous_config: BelloConfig,
        transition_token: object,
    ) -> None:
        supervisor_task = getattr(self, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not asyncio.current_task():
            await self._stop_supervisor_task()
        if not self._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self._close_completion_review_session()
        if not self._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self._quiesce_coder_tree("restart")
        if not self._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self._resolve_pending_approvals("restart")
        handoff = handoff or _fallback_restart_handoff(
            task_contents=self._canonical_task_text(),
            reason=reason,
            last_actions=self.store.read_recent_actions(10),
        )
        self.store.write_handoff(handoff.model_dump_json(indent=2) + "\n")
        self._repair_snapshot_runtime_controls(source="restart")
        self.prior_interventions = []
        self.no_marker_idle_nudge_count = 0
        self._last_completion_marker_sequence = None
        # The new generation has produced no coder work yet; until its first turn starts,
        # completion machinery must not judge (or restart over) the previous generation's state.
        self._generation_has_coder_turn = False
        self.completion_review_return_sequence = None
        self._pending_adversary_report = None
        self._active_adversary_thread_id = None
        self._active_adversary_workspace_root = None
        self._adversary_denied_commands = []
        self.validation_runtime_state = {}
        self._last_restart_budget_signature = None
        self._pending_runtime_trigger_signatures = {}
        self._pending_runtime_trigger_actions = {}
        self._deferred_completion_check = None
        self._subagent_policy_notified = set()
        self._runtime_apply_retry_count = 0
        self._runtime_decision_retry_count = 0
        self._supervisor_next_runtime_check = None
        self._supervisor_next_completion_check = None
        self._supervisor_next_runtime_summary = None
        self._supervisor_next_completion_summary = None
        self._sync_legacy_supervisor_queue_fields()
        if not self._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        patch_health(
            self.store,
            HealthDelta(
                generation=previous_config.generation,
                restart_count=1,
                reset_generation_scoped=True,
                new_generation=previous_config.generation + 1,
            ),
        )
        self.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "generation": current.generation + 1,
                    "restart_count": current.restart_count + 1,
                    "active_coder_turn_id": None,
                    "coder_thread_id": None,
                    "status": BelloStatus.RUNNING,
                }
            )
        )
        self.coder = CoderSession(
            self.client,
            self.store,
            self._active_workspace_root(),
            self._active_task_path(),
            model=self._active_coder_model(),
            fast=self._fast_mode(),
            intelligence=self._active_coder_intelligence(),
            multi_agent=self._multi_agent_config(),
            plan_path=self._active_coder_plan_path(),
        )
        await self.coder.start_thread()
        if (
            self._restart_transition_token is not transition_token
            or not self._coder_lifecycle_accepts_activity()
        ):
            return
        await self.coder.start_restart_turn()
        if (
            self._restart_transition_token is not transition_token
            or not self._coder_lifecycle_accepts_activity()
        ):
            return
        self.tui.render("SYSTEM", "restart complete")

    def _restart_transition_is_current(
        self,
        transition_token: object,
        *,
        expected_generation: int,
        expected_thread_id: str | None,
    ) -> bool:
        current = self.store.get_bello_config()
        return bool(
            self._restart_transition_token is transition_token
            and not getattr(self, "_finalizing", False)
            and current.status == BelloStatus.RESTARTING
            and current.generation == expected_generation
            and current.coder_thread_id == expected_thread_id
        )

    async def finalize(
        self,
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        if getattr(self, "_finalizing", False):
            return
        self._finalizing = True
        self._restart_transition_token = None
        self._reconcile_intervention_accounting()
        await self._wait_for_revision_switch()
        await self._wait_for_coder_activity()
        supervisor_task = getattr(self, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not asyncio.current_task():
            await self._stop_supervisor_task()
        quiesced = await self._quiesce_coder_tree("terminal", strict=False)
        self._terminal_coder_tree_quiesced = quiesced
        if not quiesced:
            # A process-tree stop is the deterministic fallback: no agent may keep
            # mutating the snapshot while Bello computes or applies the final patch.
            try:
                await self.client.stop()
                self._terminal_coder_tree_quiesced = True
            except Exception as exc:
                self._append_cleanup_error(
                    cleanup_kind="terminal_process_tree_stop",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )
        diff = await self.diff_summary()
        changed_files = await self.changed_files()
        patch_error, recovery_path = await self._apply_final_snapshot_patch_if_needed(status)
        if patch_error is not None:
            status = BelloStatus.ESCALATED
            completion_review_accepted = False
            result = patch_error
        elif recovery_path is not None:
            result = f"{result}; unaccepted coder workspace preserved at {recovery_path}"
        health = self.store.get_health()
        accepted_completion = getattr(self, "_accepted_completion_decision", None)
        report = FinalReport(
            task_path=str(self.task_path),
            status=status,
            result=result,
            files_changed=[file.path for file in changed_files]
            or _changed_files_from_diff_summary(
                diff,
                project_root=self._active_workspace_root(),
                task_path=self._active_task_path(),
            ),
            validations=[_format_validation(validation) for validation in self.validations],
            denied_actions=[],
            interventions=health.interventions,
            restarts=health.restart_count,
            completion_review_accepted=completion_review_accepted,
            completion_returns=self.store.get_bello_config().completion_return_count,
            completion_restarts=getattr(self, "completion_restarts", 0),
            no_marker_idle_nudges=getattr(self, "no_marker_idle_nudge_count", 0),
            behavior_evidence_summary=_behavior_evidence_summary(accepted_completion),
            files_reviewed_summary=_files_reviewed_summary(accepted_completion),
            packet_or_access_limitations=list(accepted_completion.packet_or_access_limitations)
            if isinstance(accepted_completion, CompletionReviewDecision)
            else [],
            adversary_reports=_final_adversary_report_summary(
                getattr(self, "_accepted_adversary_report", None)
                or getattr(self, "_pending_adversary_report", None)
            ),
            remaining_risks=list(accepted_completion.changed_test_risks)
            if isinstance(accepted_completion, CompletionReviewDecision)
            else [],
            diff_summary=diff,
        )
        self.store.write_final_report(report)
        self._archive_final_report_once()
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": status}))
        self._write_run_checkpoint("terminal", state="terminal", detail=result)
        self.tui.render("SUPERVISOR", result)
        self.tui.status("final report written: .supervisor/FINAL_REPORT.md")
        await self._prepare_terminal_shutdown(result)
        self.running = False
        self._wake_event_loop_for_shutdown()

    async def _apply_final_snapshot_patch_if_needed(
        self,
        status: BelloStatus,
    ) -> tuple[str | None, str | None]:
        snapshot = getattr(self, "_coder_snapshot", None)
        snapshot_patch_applied = getattr(self, "_snapshot_patch_applied", False)
        recovery_path = getattr(self, "_snapshot_recovery_path", None)
        if status == BelloStatus.COMPLETE and (snapshot is None or snapshot_patch_applied):
            task_integrity_issue = self._task_integrity_issue()
            if task_integrity_issue is not None:
                return (
                    "escalated: accepted workspace failed task integrity validation: "
                    f"{task_integrity_issue}",
                    recovery_path,
                )
        if snapshot is None or snapshot_patch_applied:
            return None, recovery_path
        if status != BelloStatus.COMPLETE:
            if not getattr(self, "_coder_started", False):
                snapshot.cleanup()
                self._coder_snapshot = None
                return None, None
            recovery_path = await self._preserve_snapshot_for_recovery(snapshot, reason=status.value)
            return None, recovery_path
        runtime_integrity_issue = self._runtime_integrity_issue()
        if runtime_integrity_issue is not None:
            recovery_path = await self._preserve_snapshot_for_recovery(
                snapshot,
                reason="runtime_integrity",
            )
            return (
                "escalated: accepted snapshot failed runtime integrity validation; "
                f"workspace preserved at {recovery_path}: {runtime_integrity_issue}",
                recovery_path,
            )
        task_integrity_issue = self._task_integrity_issue()
        if task_integrity_issue is not None:
            recovery_path = await self._preserve_snapshot_for_recovery(snapshot, reason="task_integrity")
            return (
                "escalated: accepted snapshot failed task integrity validation; "
                f"workspace preserved at {recovery_path}: {task_integrity_issue}",
                recovery_path,
            )
        try:
            result = await asyncio.to_thread(apply_snapshot_patch, snapshot)
        except (SnapshotPatchError, WorkspaceSnapshotError) as exc:
            recovery_path = await self._preserve_snapshot_for_recovery(snapshot, reason="patch_failed")
            message = (
                "escalated: accepted snapshot could not be applied to the real workspace; "
                f"snapshot preserved at {recovery_path}: {exc}"
            )
            self.tui.render("PATCH", message)
            self.store.append_text_locked(PROGRESS, f"- {message}\n")
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "coder_snapshot_patch_failed",
                    "snapshot_root": recovery_path,
                    "original_root": str(snapshot.original_root),
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
            return message, recovery_path
        self._snapshot_patch_applied = True
        reportable_ignored_paths = [
            path
            for path in result.ignored_paths
            if not self._is_review_private_path(path)
        ]
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "coder_snapshot_patch_applied",
                "snapshot_root": str(snapshot.snapshot_root),
                "original_root": str(snapshot.original_root),
                "applied": result.applied,
                "changed_paths": list(result.changed_paths),
                "ignored_paths": reportable_ignored_paths,
                "patch_bytes": result.patch_bytes,
            }
        )
        if result.applied:
            ignored_suffix = (
                f"; ignored {len(reportable_ignored_paths)} generated artifact paths"
                if reportable_ignored_paths
                else ""
            )
            self.store.append_text_locked(
                PROGRESS,
                f"- Applied accepted coder snapshot patch to real workspace ({len(result.changed_paths)} paths{ignored_suffix}).\n",
            )
        else:
            if reportable_ignored_paths:
                self.store.append_text_locked(
                    PROGRESS,
                    "- Accepted coder snapshot produced no workspace patch after generated artifacts were ignored.\n",
                )
            else:
                self.store.append_text_locked(PROGRESS, "- Accepted coder snapshot produced no workspace patch.\n")
        snapshot.cleanup()
        self._coder_snapshot = None
        return None, None

    async def _preserve_snapshot_for_recovery(self, snapshot: WorkspaceSnapshot, *, reason: str) -> str:
        existing = getattr(self, "_snapshot_recovery_path", None)
        if existing:
            return str(existing)
        destination = self.store.next_recovery_dir()
        try:
            workspace = await asyncio.to_thread(snapshot.preserve, destination)
            recovery_path = str(workspace)
        except WorkspaceSnapshotError:
            recovery_path = str(snapshot.snapshot_root)
        self._snapshot_recovery_path = recovery_path
        self._coder_snapshot = None
        self.store.append_text_locked(
            PROGRESS,
            f"- Preserved coder workspace for recovery at {recovery_path} ({reason}).\n",
        )
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "coder_workspace_preserved",
                "reason": reason,
                "workspace": recovery_path,
            }
        )
        return recovery_path

    def _archive_final_report_once(self) -> None:
        if getattr(self, "_final_report_archived", False):
            return
        self.store.archive_completed_run(self.task_path)
        self._final_report_archived = True

    async def _prepare_terminal_shutdown(self, reason: str) -> None:
        if getattr(self, "_terminal_cleanup_started", False):
            return
        self._terminal_cleanup_started = True
        self.running = False
        await self._close_completion_review_session()
        coder = None if getattr(self, "_terminal_coder_tree_quiesced", False) else getattr(self, "coder", None)
        if coder:
            try:
                await coder.interrupt()
            except Exception as exc:
                self._append_cleanup_error(
                    cleanup_kind="terminal_coder_interrupt",
                    thread_id=getattr(coder, "thread_id", None) or "unknown",
                    turn_id=getattr(coder, "active_turn_id", None),
                    error=exc,
                )
        if getattr(self, "pending_approvals", None) and getattr(self, "client", None) is not None:
            try:
                await self._resolve_pending_approvals(f"terminal state reached: {reason}")
            except Exception as exc:
                self._append_cleanup_error(
                    cleanup_kind="terminal_pending_approvals",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )
        task = getattr(self, "_supervisor_task", None)
        if task is not None and task is not asyncio.current_task():
            await self._stop_supervisor_task()
        client = getattr(self, "client", None)
        if client is not None and hasattr(client, "stop"):
            try:
                await client.stop()
            except Exception as exc:
                self._append_cleanup_error(
                    cleanup_kind="terminal_appserver_stop",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )

    async def _close_completion_review_session(self) -> None:
        supervisor = self._completion_supervisor_agent()
        if supervisor is None or not hasattr(supervisor, "close_completion_review"):
            return
        thread_id = getattr(supervisor, "completion_thread_id", None) or "unknown"
        try:
            await supervisor.close_completion_review()
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind="completion_review_session",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    def _wake_event_loop_for_shutdown(self) -> None:
        queue = getattr(self, "event_queue", None)
        if queue is None:
            return
        try:
            queue.put_nowait(ControllerEvent(kind="shutdown"))
        except Exception:
            pass

    def _reconcile_intervention_accounting(self) -> None:
        prior = getattr(self, "prior_interventions", None)
        if not prior:
            return
        target = sum(1 for record in prior if _prior_record_counts_as_health_intervention(record))

        def patch(current):
            if current.interventions >= target:
                return current
            return current.model_copy(update={"interventions": target})

        self.store.patch_health(patch)

    def _schedule_supervisor_check(
        self,
        summary: str,
        *,
        triggering_item_id: str | None = None,
        triggering_action: TriggeringAction | None = None,
        human_message: HumanMessage | None = None,
        patch_summary: str | None = None,
        completion_review: bool = False,
    ) -> None:
        if (
            not self.running
            or getattr(self, "paused", False)
            or getattr(self, "_finalizing", False)
            or getattr(self, "_terminal_cleanup_started", False)
            or getattr(self, "supervisor", None) is None
        ):
            return
        if not completion_review:
            self._retain_runtime_trigger_summary(summary, triggering_action=triggering_action)
        if self._supervisor_task and not self._supervisor_task.done():
            self._queue_supervisor_check(
                summary,
                triggering_item_id=triggering_item_id,
                triggering_action=triggering_action,
                human_message=human_message,
                patch_summary=patch_summary,
                completion_review=completion_review,
            )
            return
        self._supervisor_task = asyncio.create_task(
            self._supervisor_check_loop(
                summary,
                triggering_item_id,
                triggering_action,
                human_message,
                patch_summary,
                completion_review,
            )
        )

    def _queue_supervisor_check(
        self,
        summary: str,
        *,
        triggering_item_id: str | None = None,
        triggering_action: TriggeringAction | None = None,
        human_message: HumanMessage | None = None,
        patch_summary: str | None = None,
        completion_review: bool,
    ) -> None:
        self._supervisor_dirty = True
        queued = QueuedSupervisorCheck(
            summary=summary,
            triggering_item_id=triggering_item_id,
            triggering_action=triggering_action,
            human_message=human_message,
            patch_summary=patch_summary,
            completion_review=completion_review,
        )
        if completion_review:
            self._supervisor_next_completion_check = queued
            self._supervisor_next_completion_summary = summary
        else:
            self._retain_runtime_trigger_summary(summary, triggering_action=triggering_action)
            existing = getattr(self, "_supervisor_next_runtime_check", None)
            # Human input is mandatory full-supervisor context. Do not let a later routine
            # runtime event overwrite it in the coalesced slot; its trigger reasons remain
            # retained and will be merged into the human-message runtime pass.
            if existing is not None:
                existing_has_context = bool(
                    existing.human_message is not None
                    or existing.triggering_action is not None
                    or existing.triggering_item_id is not None
                    or existing.patch_summary is not None
                )
                new_has_context = bool(
                    human_message is not None
                    or triggering_action is not None
                    or triggering_item_id is not None
                    or patch_summary is not None
                )
                if existing.human_message is not None and human_message is None:
                    queued = existing
                elif existing_has_context and not new_has_context:
                    queued = existing
            self._supervisor_next_runtime_check = queued
            self._supervisor_next_runtime_summary = queued.summary
        self._sync_legacy_supervisor_queue_fields()

    def _sync_legacy_supervisor_queue_fields(self) -> None:
        """Keep the old single-slot fields coherent for diagnostics and old state tests."""
        runtime_summary = getattr(self, "_supervisor_next_runtime_summary", None)
        completion_summary = getattr(self, "_supervisor_next_completion_summary", None)
        self._supervisor_next_summary = runtime_summary or completion_summary
        self._supervisor_next_completion_review = bool(
            completion_summary is not None and runtime_summary is None
        )

    def _cancel_queued_completion_review(self) -> None:
        self._supervisor_next_completion_check = None
        self._supervisor_next_completion_summary = None
        self._sync_legacy_supervisor_queue_fields()

    def _record_validation_progress(self, validation: ValidationRun) -> None:
        def patch(current: BelloConfig) -> BelloConfig:
            updates: dict[str, Any] = {"last_validation_sequence": validation.sequence}
            if _is_behavior_proving_validation(validation) and validation.trusted_validation_outcome != "masked_or_unknown":
                updates["last_trusted_behavioral_validation_sequence"] = validation.sequence
            if _validation_is_usable_behavioral_pass(validation):
                updates["last_trusted_passing_behavioral_validation_sequence"] = validation.sequence
            return current.model_copy(update=updates)

        self.store.update_bello_config(patch)

    def _record_validation_runtime_state(self, validation: ValidationRun) -> tuple[str, ...]:
        key = validation.validation_id
        state = getattr(self, "validation_runtime_state", None)
        if state is None:
            state = {}
            self.validation_runtime_state = state
        previous = state.get(key, {})
        previous_outcome = previous.get("trusted_validation_outcome")
        previous_failed_count = int(previous.get("consecutive_failed_count") or 0)
        current_outcome = validation.trusted_validation_outcome
        reasons: list[str] = []
        if current_outcome == "failed":
            if previous_outcome == "passed":
                reasons.append("validation_regression")
            failed_count = previous_failed_count + 1 if previous_outcome == "failed" else 1
            if failed_count >= 2:
                reasons.append("repeated_same_failing_validation")
            previous_failed_count = failed_count
        else:
            previous_failed_count = 0
        state[key] = {
            "trusted_validation_outcome": current_outcome,
            "consecutive_failed_count": previous_failed_count,
            "sequence": validation.sequence,
            "normalized_command": validation.normalized_command,
            "type": validation.type,
        }
        if current_outcome == "passed":
            clear_restart_issue_for_validation(
                self.store,
                generation=self.store.get_bello_config().generation,
                validation_id=validation.validation_id,
                sequence=validation.sequence,
                matching_issue_keys=(
                    _runtime_unresolved_execution_key(validation.command, validation.cwd),
                ),
            )
        return tuple(dict.fromkeys(reasons))

    def _deterministic_runtime_noop_reason(
        self,
        *,
        reasons: list[str],
    ) -> str | None:
        if not reasons:
            return None
        reason_set = set(reasons)
        if reason_set & PROTECTED_RUNTIME_WAKE_REASONS:
            return None
        if reason_set == {"nonzero_exit"}:
            return "first isolated nonzero exit"
        return None

    def _runtime_pending_trigger_signatures(self) -> dict[str, tuple[str | None, str | None]]:
        pending = getattr(self, "_pending_runtime_trigger_signatures", None)
        if pending is None:
            pending = {}
            self._pending_runtime_trigger_signatures = pending
        return pending

    def _runtime_pending_trigger_actions(self) -> dict[str, TriggeringAction]:
        pending = getattr(self, "_pending_runtime_trigger_actions", None)
        if pending is None:
            pending = {}
            self._pending_runtime_trigger_actions = pending
        return pending

    def _retain_runtime_trigger_summary(
        self,
        summary: str,
        *,
        triggering_action: TriggeringAction | None = None,
        replace_existing: bool = True,
    ) -> None:
        """Keep every routed runtime reason alive until a runtime pass consumes it."""
        reasons = list(_runtime_trigger_reasons_from_summary(summary))
        if summary.lstrip().startswith("Runtime integrity trigger:"):
            reasons.append("runtime_control_replacement")
        if not reasons:
            return
        pending = self._runtime_pending_trigger_signatures()
        pending_actions = self._runtime_pending_trigger_actions()
        action_signature = (
            _runtime_action_signature(triggering_action)
            if triggering_action is not None
            else None
        )
        for reason in dict.fromkeys(reasons):
            if reason == "restart_budget":
                candidate, restart_reason = kill_restart_candidate(self.store.get_health())
                signature = (
                    _restart_budget_signature(self.store.get_health(), restart_reason)
                    if candidate and restart_reason
                    else None
                )
                entry = (signature, restart_reason)
            elif reason in {"large_diff", "suspicious_file_touched"} and reason in pending:
                entry = pending[reason]
            else:
                entry = (action_signature, None)
            is_new_reason = reason not in pending
            if is_new_reason or (
                replace_existing
                and entry[0] is not None
                and pending[reason] != entry
            ):
                pending[reason] = entry
            if triggering_action is not None and (is_new_reason or replace_existing):
                pending_actions[reason] = triggering_action

    def _prepare_runtime_trigger_summary(
        self,
        summary: str,
        *,
        pending: dict[str, tuple[str | None, str | None]],
    ) -> str:
        """Describe the exact retained trigger batch included in this runtime pass."""
        existing_reasons = list(_runtime_trigger_reasons_from_summary(summary))
        reasons = list(dict.fromkeys((*existing_reasons, *pending.keys())))
        if not reasons:
            return summary

        prepared_summary = summary
        if pending:
            match = re.match(r"\s*Runtime trigger \([^)]*\):\s*", summary)
            detail = summary[match.end() :] if match else summary
            restart_entry = pending.get("restart_budget")
            if restart_entry is not None and restart_entry[1]:
                restart_prefix = f"restart candidate because {restart_entry[1]}"
                if restart_prefix not in detail:
                    detail = f"{restart_prefix}; {detail}"
            prepared_summary = f"Runtime trigger ({', '.join(reasons)}): {detail}"
        return prepared_summary

    def _ack_runtime_trigger_batch(
        self,
        pending_batch: dict[str, tuple[str | None, str | None]],
    ) -> None:
        """Mark only the trigger batch covered by a successful routing decision as handled."""
        pending = self._runtime_pending_trigger_signatures()
        for reason, entry in pending_batch.items():
            current_entry = pending.get(reason)
            if current_entry is None:
                # The condition cleared while this review was in flight. Do not stamp its
                # old signature as handled; a later recurrence must be treated as new state.
                continue
            signature = entry[0]
            if reason == "large_diff" and signature is not None:
                self._last_large_diff_signature = signature
            elif reason == "suspicious_file_touched" and signature is not None:
                self._last_suspicious_file_signature = signature
            elif reason == "restart_budget" and signature is not None:
                self._last_restart_budget_signature = signature
            if current_entry == entry:
                pending.pop(reason, None)
                self._runtime_pending_trigger_actions().pop(reason, None)
        queued = getattr(self, "_supervisor_next_runtime_check", None)
        if queued is not None:
            queued_reasons = _runtime_trigger_reasons_from_summary(queued.summary)
            remaining_reasons = list(pending)
            if queued_reasons and not remaining_reasons:
                self._supervisor_next_runtime_check = None
                self._supervisor_next_runtime_summary = None
                self._sync_legacy_supervisor_queue_fields()
            elif queued_reasons and tuple(remaining_reasons) != queued_reasons:
                match = re.match(r"\s*Runtime trigger \([^)]*\):\s*", queued.summary)
                detail = queued.summary[match.end() :] if match else queued.summary
                updated_summary = f"Runtime trigger ({', '.join(remaining_reasons)}): {detail}"
                carrier_action = next(
                    (
                        self._runtime_pending_trigger_actions().get(reason)
                        for reason in remaining_reasons
                        if self._runtime_pending_trigger_actions().get(reason) is not None
                    ),
                    queued.triggering_action,
                )
                self._supervisor_next_runtime_check = QueuedSupervisorCheck(
                    summary=updated_summary,
                    triggering_item_id=queued.triggering_item_id,
                    triggering_action=carrier_action,
                    human_message=queued.human_message,
                    patch_summary=queued.patch_summary,
                    completion_review=False,
                )
                self._supervisor_next_runtime_summary = updated_summary
                self._sync_legacy_supervisor_queue_fields()

    def _update_relevant_edit_state(self, changed_files: list[ChangedFile]) -> None:
        task_contents = self._canonical_task_text()
        relevant_sequences = [
            changed.sequence
            for changed in changed_files
            if changed.sequence is not None and _is_relevant_changed_path(changed.path, task_contents=task_contents)
        ]
        if not relevant_sequences:
            return
        latest = max(relevant_sequences)

        def patch(current: BelloConfig) -> BelloConfig:
            existing = current.last_relevant_edit_sequence
            if existing is not None and existing >= latest:
                return current
            return current.model_copy(update={"last_relevant_edit_sequence": latest})

        self.store.update_bello_config(patch)

    def should_wake_runtime_supervisor(
        self,
        *,
        action: TriggeringAction,
        validation: ValidationRun | None,
        changed_files: list[ChangedFile],
        validation_trigger_reasons: tuple[str, ...] = (),
    ) -> RuntimeTriggerDecision:
        reasons: list[str] = list(validation_trigger_reasons)
        read_only_action = bool(action.command and _is_read_only_inspection_command(action.command))
        if (
            action.exit_code is not None
            and action.exit_code != 0
            and not (
                action.command
                and _is_read_only_inspection_command(action.command)
                and _inspection_exit_is_usable(action.command, action.exit_code)
            )
        ):
            reasons.append("nonzero_exit")
        if _action_timed_out(action):
            reasons.append("timeout")
        large_diff_signature = _large_diff_signature(changed_files) if _has_large_diff(changed_files) else None
        large_diff_is_new = bool(
            large_diff_signature is not None
            and large_diff_signature != getattr(self, "_last_large_diff_signature", None)
            and large_diff_signature
            != self._runtime_pending_trigger_signatures().get("large_diff", (None, None))[0]
        )
        if large_diff_is_new and not read_only_action:
            reasons.append("large_diff")
            self._runtime_pending_trigger_signatures()["large_diff"] = (large_diff_signature, None)
        suspicious_file_hash_cache = getattr(self, "_suspicious_file_hash_cache", None)
        if suspicious_file_hash_cache is None:
            suspicious_file_hash_cache = self._suspicious_file_hash_cache = {}
        suspicious_file_signature = _suspicious_changed_file_signature(
            self._active_workspace_root(),
            changed_files,
            cache=suspicious_file_hash_cache,
        )
        if suspicious_file_signature is None:
            self._last_suspicious_file_signature = None
            self._runtime_pending_trigger_signatures().pop("suspicious_file_touched", None)
            self._runtime_pending_trigger_actions().pop("suspicious_file_touched", None)
        elif suspicious_file_signature != getattr(self, "_last_suspicious_file_signature", None):
            pending_suspicious = self._runtime_pending_trigger_signatures().get(
                "suspicious_file_touched", (None, None)
            )[0]
            if suspicious_file_signature != pending_suspicious:
                reasons.append("suspicious_file_touched")
                self._runtime_pending_trigger_signatures()["suspicious_file_touched"] = (
                    suspicious_file_signature,
                    None,
                )
        restart_candidate, restart_reason = kill_restart_candidate(self.store.get_health())
        restart_signature = (
            _restart_budget_signature(self.store.get_health(), restart_reason)
            if restart_candidate and restart_reason
            else None
        )
        if restart_signature is None:
            self._last_restart_budget_signature = None
            self._runtime_pending_trigger_signatures().pop("restart_budget", None)
            self._runtime_pending_trigger_actions().pop("restart_budget", None)
        if (
            restart_signature is not None
            and restart_signature != getattr(self, "_last_restart_budget_signature", None)
            and restart_signature
            != self._runtime_pending_trigger_signatures().get("restart_budget", (None, None))[0]
        ):
            reasons.append("restart_budget")
            self._runtime_pending_trigger_signatures()["restart_budget"] = (
                restart_signature,
                restart_reason,
            )
        reasons = list(dict.fromkeys(reasons))
        if self._deterministic_runtime_noop_reason(
            reasons=reasons,
        ):
            return RuntimeTriggerDecision(should_wake=False, reasons=())
        return RuntimeTriggerDecision(
            should_wake=bool(reasons),
            reasons=tuple(reasons),
            restart_reason=restart_reason if "restart_budget" in reasons else None,
        )

    def _record_runtime_trigger_trace(
        self,
        *,
        event_type: str,
        action: TriggeringAction | None,
        validation: ValidationRun | None,
        changed_files: list[ChangedFile],
        decision: RuntimeTriggerDecision,
    ) -> None:
        additions, deletions = _diff_line_counts(changed_files)
        suspicious_paths = [changed.path for changed in changed_files if _is_suspicious_changed_path(changed.path)]
        private_input_action = (
            action is not None and self._exposes_review_private_input(action)
        )
        trace = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_sequence": getattr(self, "_sequence", None),
            "generation": self.store.get_bello_config().generation,
            "event_type": event_type,
            "action_kind": action.kind if action is not None else None,
            "tool_name": action.kind if action is not None else None,
            "command": (
                action.command
                if action is not None and not private_input_action
                else None
            ),
            "cwd": (
                action.cwd
                if action is not None and not private_input_action
                else None
            ),
            "exit_code": action.exit_code if action is not None else None,
            "status": action.status if action is not None else None,
            "timed_out": action.timed_out if action is not None else False,
            "changed_files_count": len(changed_files),
            "changed_files": [changed.path for changed in changed_files[:20]],
            "changed_lines": additions + deletions,
            "diff_additions": additions,
            "diff_deletions": deletions,
            "suspicious_paths": suspicious_paths[:20],
            "validation_id": validation.validation_id if validation is not None else None,
            "validation_type": validation.type if validation is not None else None,
            "trusted_validation_outcome": validation.trusted_validation_outcome if validation is not None else None,
            "masking_reason": validation.masking_reason if validation is not None else None,
            "should_wake_runtime_supervisor": decision.should_wake,
            "trigger_reasons": list(decision.reasons),
            "restart_reason": decision.restart_reason,
            "deterministic_action": decision.deterministic_action,
            "skipped_noop": not decision.should_wake and decision.deterministic_action is None,
        }
        self.store.append_runtime_trace(trace)
        self._update_runtime_metrics(trace)

    def _declared_grading_access_issue(self, action: TriggeringAction) -> str | None:
        roots = getattr(self, "declared_grading_roots", ())
        if not roots:
            return None
        payload: dict[str, Any] = {}
        if action.command:
            payload["command"] = action.command
        if action.cwd:
            payload["cwd"] = action.cwd
        if action.paths:
            payload["paths"] = action.paths
        if not payload:
            return None
        manager = getattr(self, "approvals", None)
        if manager is None:
            manager = ApprovalManager(
                self._active_workspace_root(),
                declared_grading_roots=roots,
                immutable_paths=self._immutable_approval_paths(),
            )
        decision = manager.policy.evaluate(payload)
        if decision.kind.value != "deny" or "declared grading/hidden path access denied" not in decision.reason:
            return None
        command = f" command `{action.command}`" if action.command else ""
        return f"coder accessed declared grading/hidden path via{command}: {decision.reason}"

    def _update_runtime_metrics(self, trace: dict[str, Any]) -> None:
        reasons = trace.get("trigger_reasons")
        if not isinstance(reasons, list):
            reasons = []

        def patch(current: dict[str, Any]) -> dict[str, Any]:
            current["runtime_events_total"] = int(current.get("runtime_events_total") or 0) + 1
            if trace.get("should_wake_runtime_supervisor"):
                current["runtime_wakes_total"] = int(current.get("runtime_wakes_total") or 0) + 1
            if trace.get("skipped_noop"):
                current["runtime_skipped_noop_total"] = int(current.get("runtime_skipped_noop_total") or 0) + 1
            if trace.get("deterministic_action"):
                current["runtime_deterministic_action_total"] = int(
                    current.get("runtime_deterministic_action_total") or 0
                ) + 1
            counts = current.get("runtime_trigger_reason_counts")
            if not isinstance(counts, dict):
                counts = {}
            for reason in reasons:
                counts[str(reason)] = int(counts.get(str(reason)) or 0) + 1
                metric_name = f"runtime_trigger_{reason}_total"
                current[metric_name] = int(current.get(metric_name) or 0) + 1
            current["runtime_trigger_reason_counts"] = counts
            return current

        self.store.update_runtime_metrics(patch)

    def _record_supervisor_decision_metric(self, *, use_case: str, decision: str) -> None:
        def patch(current: dict[str, Any]) -> dict[str, Any]:
            counts = current.get("supervisor_decision_counts")
            if not isinstance(counts, dict):
                counts = {}
            scope_counts = counts.get(use_case)
            if not isinstance(scope_counts, dict):
                scope_counts = {}
            scope_counts[decision] = int(scope_counts.get(decision) or 0) + 1
            counts[use_case] = scope_counts
            current["supervisor_decision_counts"] = counts
            current[f"{use_case}_{decision}_total"] = int(current.get(f"{use_case}_{decision}_total") or 0) + 1
            return current

        self.store.update_runtime_metrics(patch)

    def _record_approval_metric(self, *, decision: str, from_supervisor: bool) -> None:
        def patch(current: dict[str, Any]) -> dict[str, Any]:
            counts = current.get("approval_decision_counts")
            if not isinstance(counts, dict):
                counts = {}
            counts[decision] = int(counts.get(decision) or 0) + 1
            current["approval_decision_counts"] = counts
            current["approval_requests_total"] = int(current.get("approval_requests_total") or 0) + 1
            current[f"approval_{decision}_total"] = int(current.get(f"approval_{decision}_total") or 0) + 1
            if from_supervisor:
                current["approval_from_supervisor_total"] = int(current.get("approval_from_supervisor_total") or 0) + 1
            return current

        self.store.update_runtime_metrics(patch)

    async def _supervisor_check_loop(
        self,
        summary: str,
        triggering_item_id: str | None,
        triggering_action: TriggeringAction | None,
        human_message: HumanMessage | None,
        patch_summary: str | None,
        completion_review: bool,
    ) -> None:
        while True:
            self._supervisor_dirty = False
            self._runtime_decision_invalidated_completion = False
            active_check = QueuedSupervisorCheck(
                summary=summary,
                triggering_item_id=triggering_item_id,
                triggering_action=triggering_action,
                human_message=human_message,
                patch_summary=patch_summary,
                completion_review=completion_review,
            )
            self._active_supervisor_check = active_check
            phase = "completion_review" if completion_review else "runtime_review"
            self._write_run_checkpoint(phase, state="active")
            try:
                await self._run_supervisor_check(
                    summary,
                    triggering_item_id,
                    triggering_action,
                    human_message,
                    patch_summary,
                    completion_review,
                )
            finally:
                if self._active_supervisor_check is active_check:
                    self._active_supervisor_check = None
            self._mark_controller_activity()
            if (
                not self.running
                or getattr(self, "paused", False)
                or getattr(self, "_finalizing", False)
            ):
                return
            if not completion_review and getattr(
                self,
                "_runtime_decision_invalidated_completion",
                False,
            ):
                self._cancel_queued_completion_review()
            runtime_summary = getattr(self, "_supervisor_next_runtime_summary", None)
            completion_summary = getattr(self, "_supervisor_next_completion_summary", None)
            if runtime_summary is not None:
                queued = getattr(self, "_supervisor_next_runtime_check", None) or QueuedSupervisorCheck(
                    summary=runtime_summary,
                    completion_review=False,
                )
                self._supervisor_next_runtime_check = None
                self._supervisor_next_runtime_summary = None
            elif completion_summary is not None:
                queued = getattr(
                    self,
                    "_supervisor_next_completion_check",
                    None,
                ) or QueuedSupervisorCheck(
                    summary=completion_summary,
                    completion_review=True,
                )
                self._supervisor_next_completion_check = None
                self._supervisor_next_completion_summary = None
            else:
                self._sync_legacy_supervisor_queue_fields()
                return
            self._sync_legacy_supervisor_queue_fields()
            summary = queued.summary
            triggering_item_id = queued.triggering_item_id
            triggering_action = queued.triggering_action
            human_message = queued.human_message
            patch_summary = queued.patch_summary
            completion_review = queued.completion_review

    async def _run_supervisor_check(
        self,
        summary: str,
        triggering_item_id: str | None,
        triggering_action: TriggeringAction | None,
        human_message: HumanMessage | None,
        patch_summary: str | None,
        completion_review: bool = False,
    ) -> None:
        if not self._coder_lifecycle_accepts_activity():
            if completion_review:
                await self._close_completion_review_session()
            return
        if completion_review:
            await self._refresh_coder_subagents()
            active_subagents = self._active_coder_subagents()
            if active_subagents:
                self._deferred_completion_check = QueuedSupervisorCheck(
                    summary=summary,
                    triggering_item_id=triggering_item_id,
                    triggering_action=triggering_action,
                    human_message=human_message,
                    patch_summary=patch_summary,
                    completion_review=True,
                )
                self._append_event(
                    AppEventSource.SUPERVISOR,
                    "completion/deferred_for_subagents",
                    reason="completion snapshot deferred until coder descendants are quiescent",
                )
                self.tui.render(
                    "SUPERVISOR",
                    f"completion deferred: {len(active_subagents)} subagent(s) still active",
                )
                return
        runtime_trigger_batch: dict[str, tuple[str | None, str | None]] = {}
        if not completion_review:
            self._retain_runtime_trigger_summary(
                summary,
                triggering_action=triggering_action,
                replace_existing=False,
            )
            runtime_trigger_batch = dict(self._runtime_pending_trigger_signatures())
            runtime_trigger_actions = _unique_runtime_trigger_actions(
                self._runtime_pending_trigger_actions().get(reason)
                for reason in runtime_trigger_batch
            )
        else:
            runtime_trigger_actions = []
        agent = self._completion_supervisor_agent() if completion_review else self.supervisor
        if agent is None:
            return
        self._reconcile_intervention_accounting()
        cfg = self.store.get_bello_config()
        wake_sequence = cfg.last_event_sequence + 1
        changed_files = await self.changed_files()
        packet_validations = list(self.validations)
        packet_inspections = list(getattr(self, "inspections", []))
        packet_subagents = self._subagent_summaries()
        packet_last_coder_message = self.last_coder_message
        packet_triggering_action = triggering_action
        packet_human_message = human_message
        packet_prior_interventions = list(self.prior_interventions)
        packet_patch_summary = patch_summary
        if completion_review:
            packet_validations = self._review_safe_values(packet_validations)
            packet_inspections = self._review_safe_values(packet_inspections)
            packet_subagents = self._review_safe_values(packet_subagents)
            packet_prior_interventions = self._review_safe_values(
                packet_prior_interventions
            )
            if self._exposes_review_private_input(packet_last_coder_message):
                packet_last_coder_message = None
            if self._exposes_review_private_input(packet_triggering_action):
                packet_triggering_action = None
            if self._exposes_review_private_input(packet_human_message):
                packet_human_message = None
            if self._exposes_review_private_input(packet_patch_summary):
                packet_patch_summary = None
            if self._exposes_review_private_input(summary):
                summary = "Coder work is ready for independent review."
        if not completion_review:
            summary = self._prepare_runtime_trigger_summary(
                summary,
                pending=runtime_trigger_batch,
            )
        latest_change_sequence = _latest_relevant_change_sequence(changed_files)
        freshness_summary = _validation_freshness_summary(
            validations=packet_validations,
            changed_files=changed_files,
        )
        completion_payload_mode: Literal["full", "delta", "full_fallback"] | None = None
        completion_payload_since_sequence: int | None = None
        completion_details: dict[str, Any] = {}
        if completion_review:
            completion_payload_mode, completion_payload_since_sequence = self._completion_payload_window(changed_files)
            completion_details = await self.completion_packet_details(
                changed_files,
                since_sequence=completion_payload_since_sequence,
            )
            completion_details["evidence_provenance_summary"] = _evidence_provenance_summary(
                validations=packet_validations,
                changed_files=changed_files,
                latest_change_sequence=latest_change_sequence,
            )
            completion_details["behavior_surface"] = self._behavior_surface_items()
            completion_details["prior_uncovered_edge_candidates"] = list(
                self._completion_knowledge()["uncovered_edge_candidates"]
            )
        packet = agent.build_packet(
            wake_sequence=wake_sequence,
            current_summary=summary,
            diff_summary=await self.diff_summary(),
            triggering_item_id=triggering_item_id,
            pending_approvals=[_approval_wake_context(pending) for pending in self.pending_approvals.values()],
            triggering_action=packet_triggering_action,
            runtime_triggering_actions=runtime_trigger_actions,
            subagents=packet_subagents,
            last_coder_message=packet_last_coder_message,
            validations=packet_validations,
            inspections=packet_inspections,
            human_message=packet_human_message,
            prior_interventions=packet_prior_interventions,
            changed_files=changed_files,
            patch_summary=packet_patch_summary or await self.patch_summary(),
            completion_attempt_count=getattr(self, "completion_attempt_count", 0),
            completion_returns_this_generation=_completion_returns_this_generation(self, cfg.generation),
            previous_completion_returns=list(getattr(self, "completion_returns", []))[-10:],
            last_readiness_marker_sequence=getattr(self, "_last_completion_marker_sequence", None),
            no_marker_idle_nudge_count=getattr(self, "no_marker_idle_nudge_count", 0),
            latest_relevant_change_sequence=latest_change_sequence,
            validation_freshness_summary=freshness_summary,
            completion_payload_mode=completion_payload_mode,
            completion_payload_since_sequence=completion_payload_since_sequence,
            completion_review_thread_id=getattr(agent, "completion_thread_id", None),
            adversary_report=(
                self._fresh_adversary_report(
                    generation=cfg.generation,
                    latest_relevant_change_sequence=latest_change_sequence,
                )
                if completion_review
                else None
            ),
            **completion_details,
        )
        if completion_review:
            packet = self._review_safe_packet_state(packet)
        if not self._coder_lifecycle_accepts_activity():
            if completion_review:
                await self._close_completion_review_session()
            return
        if completion_review:
            budget_action = self._completion_review_budget_action(packet=packet)
            if budget_action == "adversary":
                await self._run_adversary_before_complete(None, packet=packet)
                return
            if budget_action == "complete":
                reason = (
                    "completion review budget exhausted"
                    if self._effective_max_adversary_runs() <= 0
                    else "post-adversary completion review budget exhausted"
                )
                await self._finalize_bounded_completion(
                    reason=reason,
                )
                return
        try:
            if completion_review:
                self.completion_attempt_count = getattr(self, "completion_attempt_count", 0) + 1
                decision = await agent.decide_completion(packet)
            else:
                # Cheap-model triage: let a lightweight model route clear non-events to noop
                # before paying for the full supervisor. Never short-circuit human messages or
                # pending approvals (those always need the full supervisor); on any cheap-side
                # error or escalate, fall through to the full supervisor.
                if (
                    getattr(self, "runtime_triage_reviewer", None) is not None
                    and human_message is None
                    and not packet.pending_approvals
                    and not _runtime_packet_requires_full_supervisor(packet)
                ):
                    cheap = await self._cheap_runtime_route(packet)
                    if cheap is not None and cheap.decision == "noop":
                        self._ack_runtime_trigger_batch(runtime_trigger_batch)
                        return
                decision = await agent.decide(packet)
        except SupervisorAgentError as exc:
            if getattr(self, "_transport_error_pending", False):
                # The controller event loop owns transport recovery. Do not let
                # the same broken stream race that recovery and terminalize the
                # run from this reviewer task.
                return
            if not self._coder_lifecycle_accepts_activity():
                if completion_review:
                    await self._close_completion_review_session()
                return
            failure_kind = _classify_supervisor_agent_error(exc)
            message = f"supervisor check failed ({failure_kind}): {exc}"
            self.tui.render("SUPERVISOR", message)
            if failure_kind == "no_message":
                recovered = await self._handle_supervisor_no_message_failure(
                    message=message,
                    summary=summary,
                    completion_review=completion_review,
                )
                if recovered:
                    if (
                        not completion_review
                        and getattr(self, "_supervisor_next_runtime_summary", None) is None
                    ):
                        # Runtime no_message has exhausted its bounded retry and the existing
                        # policy explicitly skips this runtime-only review. Acknowledge that
                        # terminal skip so a retained trigger cannot strand the queue forever.
                        self._ack_runtime_trigger_batch(runtime_trigger_batch)
                    return
            if completion_review and failure_kind == "tool_timeout":
                recovered = await self._handle_completion_review_timeout_failure(
                    message=message,
                    summary=summary,
                )
                if recovered:
                    return
            if not completion_review:
                queued_runtime = getattr(self, "_supervisor_next_runtime_summary", None)
                queued_completion = getattr(self, "_supervisor_next_completion_summary", None)
                legacy_queued_completion = bool(
                    getattr(self, "_supervisor_next_completion_review", False)
                )
                if (
                    queued_runtime is None
                    and queued_completion is None
                    and not legacy_queued_completion
                ):
                    await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)
                    return
                counts = getattr(self, "provider_failure_recovery_counts", None)
                if counts is None:
                    counts = {}
                    self.provider_failure_recovery_counts = counts
                retry_key = f"runtime_monitor_{failure_kind}"
                attempts = int(counts.get(retry_key) or 0)
                if attempts < 1:
                    counts[retry_key] = attempts + 1
                    if queued_runtime is None:
                        self._queue_supervisor_check(
                            f"Retry runtime review after {failure_kind}: {summary}",
                            triggering_item_id=triggering_item_id,
                            triggering_action=triggering_action,
                            human_message=human_message,
                            patch_summary=patch_summary,
                            completion_review=False,
                        )
                    self.store.append_text_locked(
                        PROGRESS,
                        f"- Runtime supervisor failed with {failure_kind}; retrying the retained runtime trigger before completion.\n",
                    )
                    return
                self._cancel_queued_completion_review()
                patch_health(
                    self.store,
                    HealthDelta(
                        generation=cfg.generation,
                        timeout_fallback_count=1,
                        add_risk_signals=["stale_runtime_supervisor_timeout"],
                    ),
                )
                self.store.append_text_locked(
                    PROGRESS,
                    f"- Runtime supervisor failed repeatedly with {failure_kind}; refusing to run a stale completion review.\n",
                )
                await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)
                return
            await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)
            return
        if not self._coder_lifecycle_accepts_activity():
            # A lifecycle transition may race an in-flight model call. Its result belongs
            # to the previous live state and must not steer, restart, or complete the run.
            if completion_review:
                await self._close_completion_review_session()
            return
        # Successful supervisor decision: reset the transient provider no_message budget so it
        # counts CONSECUTIVE empty-completion failures, not lifetime ones (a recovered provider
        # should not inherit earlier blips toward an infra-invalid).
        if getattr(self, "provider_failure_recovery_counts", None):
            self.provider_failure_recovery_counts = {}
        if completion_review:
            if getattr(self, "_supervisor_next_runtime_summary", None) is not None:
                # Runtime evidence arrived while this completion snapshot was in flight.
                # Do not apply a now-stale accept/return/restart decision; requeue completion
                # behind the pending runtime pass, which may steer or restart the coder. Close
                # the review session now so a later readiness review cannot reuse this frozen
                # verification copy after the coder has acted on runtime steering.
                await self._close_completion_review_session()
                self._queue_supervisor_check(
                    summary,
                    triggering_item_id=triggering_item_id,
                    triggering_action=triggering_action,
                    human_message=human_message,
                    patch_summary=patch_summary,
                    completion_review=True,
                )
                return
            await self.apply_completion_decision(decision, packet_thread_id=packet.coder_thread_id, packet=packet)
        else:
            try:
                applied = await self.apply_supervisor_decision(
                    decision,
                    packet_thread_id=packet.coder_thread_id,
                    packet=packet,
                )
            except Exception as exc:
                if not self._coder_lifecycle_accepts_activity():
                    return
                self._cancel_queued_completion_review()
                attempts = int(getattr(self, "_runtime_apply_retry_count", 0) or 0)
                if attempts < 1:
                    self._runtime_apply_retry_count = attempts + 1
                    self._queue_supervisor_check(
                        f"Runtime trigger (runtime_apply_retry): retry decision after apply failure; {summary}",
                        triggering_item_id=triggering_item_id,
                        triggering_action=triggering_action,
                        human_message=human_message,
                        patch_summary=patch_summary,
                        completion_review=False,
                    )
                    self.store.append_text_locked(
                        PROGRESS,
                        f"- Runtime decision application failed ({exc.__class__.__name__}); retrying once before completion.\n",
                    )
                    return
                await self.finalize(
                    f"runtime decision application failed after retry: {exc}",
                    status=BelloStatus.PROVIDER_FAILURE,
                )
                return
            self._runtime_apply_retry_count = 0
            if not self._coder_lifecycle_accepts_activity():
                return
            if applied and self.store.get_bello_config().generation == packet.generation:
                self._runtime_decision_retry_count = 0
                self._ack_runtime_trigger_batch(runtime_trigger_batch)
                await self._resume_readiness_after_runtime_noop(decision, packet)
            elif not applied:
                attempts = int(getattr(self, "_runtime_decision_retry_count", 0) or 0)
                if attempts < 1:
                    self._runtime_decision_retry_count = attempts + 1
                    self._queue_supervisor_check(
                        f"Runtime trigger (runtime_decision_retry): refresh stale runtime decision; {summary}",
                        triggering_item_id=triggering_item_id,
                        triggering_action=triggering_action,
                        human_message=human_message,
                        patch_summary=patch_summary,
                        completion_review=False,
                    )
                    return
                self._cancel_queued_completion_review()
                await self.finalize(
                    "runtime supervisor returned a stale or mismatched decision after retry",
                    status=BelloStatus.PROVIDER_FAILURE,
                )

    async def _resume_readiness_after_runtime_noop(
        self,
        decision: SupervisorDecision,
        packet: SupervisorWakePacket,
    ) -> bool:
        if decision.decision != SupervisorDecisionKind.NOOP:
            return False
        if "done_without_fresh_validation" not in _runtime_trigger_reasons_from_summary(packet.current_summary):
            return False
        marker_sequence = packet.last_readiness_marker_sequence
        if marker_sequence is None or not self._runtime_noop_readiness_context_is_current(
            decision,
            packet,
            marker_sequence=marker_sequence,
        ):
            return False

        await self._refresh_coder_subagents()
        if not self._runtime_noop_readiness_context_is_current(
            decision,
            packet,
            marker_sequence=marker_sequence,
        ):
            return False
        self._append_event(
            AppEventSource.SUPERVISOR,
            "completion/readiness_validation_waived",
            decision="noop",
            reason=decision.reason,
        )
        await self._continue_after_readiness_marker(
            triggering_item_id=packet.triggering_item_id,
            subagents_refreshed=True,
        )
        return True

    def _runtime_noop_readiness_context_is_current(
        self,
        decision: SupervisorDecision,
        packet: SupervisorWakePacket,
        *,
        marker_sequence: int,
    ) -> bool:
        message = self.last_coder_message
        cfg = self.store.get_bello_config()
        coder = getattr(self, "coder", None)
        return bool(
            decision.wake_sequence == packet.wake_sequence
            and decision.generation == packet.generation
            and cfg.generation == packet.generation
            and cfg.coder_thread_id == packet.coder_thread_id
            and not self._readiness_snapshot_has_new_invalidating_event(packet, cfg=cfg)
            and cfg.active_coder_turn_id is None
            and self._last_completion_marker_sequence == marker_sequence
            and message is not None
            and message.sequence == marker_sequence
            and _has_readiness_marker(message.text)
            and not bool(getattr(self, "pending_approvals", None))
            and getattr(self, "_supervisor_next_runtime_summary", None) is None
            and getattr(self, "_supervisor_next_runtime_check", None) is None
            and getattr(self, "running", False)
            and not getattr(self, "paused", False)
            and not getattr(self, "_finalizing", False)
            and not getattr(self, "_terminal_cleanup_started", False)
            and (coder is None or not getattr(coder, "active_turn_id", None))
        )

    def _readiness_snapshot_has_new_invalidating_event(
        self,
        packet: SupervisorWakePacket,
        *,
        cfg: BelloConfig,
    ) -> bool:
        """Accept only a complete bounded suffix containing known reviewer traffic."""

        if cfg.last_event_sequence == packet.latest_event_sequence:
            return False
        if cfg.last_event_sequence < packet.latest_event_sequence:
            return True

        expected_sequence = packet.latest_event_sequence + 1
        for event in self._readiness_journal():
            if event.sequence < expected_sequence:
                continue
            if event.sequence > cfg.last_event_sequence:
                break
            if event.sequence != expected_sequence:
                return True
            expected_sequence += 1
            if event.source != AppEventSource.APP_SERVER:
                return True
            if event.event_type == "account/rateLimits/updated" and event.thread_id is None:
                continue
            if event.thread_id is None:
                return True
            if event.thread_id == cfg.coder_thread_id or self._is_coder_descendant(
                event.thread_id,
                cfg=cfg,
            ):
                return True
            if self._reviewer_role_for_thread(event.thread_id) is None:
                return True

        # Missing/evicted/non-contiguous entries make provenance unknowable.
        return expected_sequence <= cfg.last_event_sequence

    async def _handle_completion_review_timeout_failure(self, *, message: str, summary: str) -> bool:
        """One fresh-thread retry when a completion-review turn times out.

        A timed-out review turn used to finalize the whole run as provider_failure, discarding
        hours of coder work over a single slow review. Close the review session (abandoning the
        hung turn) and re-enter the review loop once; on a consecutive second timeout, fall
        through to the existing fatal path. The counter resets on any successful decision.
        """
        counts = getattr(self, "provider_failure_recovery_counts", None)
        if counts is None:
            counts = {}
            self.provider_failure_recovery_counts = counts
        key = "completion_review_tool_timeout"
        attempts = int(counts.get(key) or 0)
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "provider_failure_recovery",
                "kind": "tool_timeout",
                "scope": "completion_review",
                "attempts_before": attempts,
                "message": message,
            }
        )
        budget = getattr(self, "_completion_timeout_max_retries", COMPLETION_TIMEOUT_MAX_RETRIES)
        if attempts >= budget:
            return False
        counts[key] = attempts + 1
        completion_supervisor = self._completion_supervisor_agent()
        if completion_supervisor is not None and hasattr(completion_supervisor, "close_completion_review"):
            await completion_supervisor.close_completion_review()
        self.store.append_text_locked(
            PROGRESS,
            f"- Provider recovery: completion review turn timed out; retrying once on a fresh review "
            f"thread (attempt {attempts + 1}/{budget}).\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "provider/completion_timeout_retry",
            decision="retry",
            reason=message,
        )
        retry_summary = (
            "Retry completion review on a fresh thread after the previous review turn timed out. "
            f"Previous review summary: {summary}"
        )
        self._queue_supervisor_check(retry_summary, completion_review=True)
        return True

    async def _handle_supervisor_no_message_failure(
        self,
        *,
        message: str,
        summary: str,
        completion_review: bool,
    ) -> bool:
        counts = getattr(self, "provider_failure_recovery_counts", None)
        if counts is None:
            counts = {}
            self.provider_failure_recovery_counts = counts
        scope = "completion_review" if completion_review else "runtime_monitor"
        count_key = f"{scope}_no_message"
        attempts = int(counts.get(count_key) or 0)
        counts["no_message"] = int(counts.get("no_message") or 0) + 1
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "provider_failure_recovery",
                "kind": "no_message",
                "scope": scope,
                "attempts_before": attempts,
                "completion_review": completion_review,
                "message": message,
            }
        )
        budget = (
            getattr(self, "_completion_no_message_max_retries", COMPLETION_NO_MESSAGE_MAX_RETRIES)
            if completion_review
            else 1
        )
        if attempts < budget:
            counts[count_key] = attempts + 1
            completion_supervisor = self._completion_supervisor_agent()
            if completion_review and completion_supervisor is not None and hasattr(
                completion_supervisor,
                "close_completion_review",
            ):
                await completion_supervisor.close_completion_review()
            backoff = 0.0
            if completion_review:
                schedule = getattr(self, "_no_message_backoff_seconds", NO_MESSAGE_RETRY_BACKOFF_SECONDS)
                if schedule:
                    backoff = float(schedule[min(attempts, len(schedule) - 1)])
            self.store.append_text_locked(
                PROGRESS,
                f"- Provider recovery: supervisor produced no agent message; retrying review from latest "
                f"stable state (attempt {attempts + 1}/{budget}, backoff {backoff:.0f}s).\n",
            )
            self._append_event(
                AppEventSource.SUPERVISOR,
                "provider/no_message_retry",
                decision="retry",
                reason=message,
            )
            if backoff > 0:
                await asyncio.sleep(backoff)
            retry_summary = (
                "Retry supervisor review from the latest stable controller state after provider no_message. "
                f"Previous review summary: {summary}"
            )
            self._queue_supervisor_check(
                retry_summary,
                completion_review=completion_review,
            )
            return True
        counts[count_key] = attempts + 1
        if not completion_review:
            self.store.append_text_locked(
                PROGRESS,
                "- Provider recovery: runtime supervisor produced no agent message after retry; skipping this runtime-only review.\n",
            )
            self._append_event(
                AppEventSource.SUPERVISOR,
                "provider/runtime_no_message_skipped",
                decision="continue",
                reason=message,
            )
            return True
        self.store.append_text_locked(
            PROGRESS,
            "- Provider recovery failed: repeated supervisor no_message; marking run infra-invalid before scoring.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "provider/no_message_infra_invalid",
            decision="infra-invalid",
            reason=message,
        )
        await self.finalize(
            f"infra-invalid: supervisor no_message provider failure after retry/resume: {message}",
            status=BelloStatus.PROVIDER_FAILURE,
        )
        return True

    def _completion_payload_window(
        self,
        changed_files: list[ChangedFile],
    ) -> tuple[Literal["full", "delta", "full_fallback"], int | None]:
        since_sequence = getattr(self, "completion_review_return_sequence", None)
        if since_sequence is None:
            return "full", None
        task_contents = self._canonical_task_text()
        has_unknown_relevant_sequence = any(
            changed.sequence is None and _is_relevant_changed_path(changed.path, task_contents=task_contents)
            for changed in changed_files
        )
        if has_unknown_relevant_sequence:
            return "full_fallback", None
        return "delta", since_sequence

    def _record_runtime_intervention(
        self,
        *,
        reason: str,
        message: str,
        sequence: int,
        generation: int,
        issue: RuntimeRestartIssue | None,
    ) -> None:
        self.prior_interventions.append(
            PriorIntervention(reason=reason, message_to_coder=message, sequence=sequence)
        )
        self.prior_interventions = self.prior_interventions[-20:]
        patch_health(self.store, HealthDelta(generation=generation, interventions=1))
        if issue is not None:
            record_restart_issue_intervention(
                self.store,
                generation=generation,
                issue_key=issue.key,
                sequence=issue.sequence,
                validation_id=issue.validation_id,
            )

    async def apply_supervisor_decision(
        self,
        decision: SupervisorDecision,
        *,
        packet_thread_id: str | None,
        packet: SupervisorWakePacket | None = None,
    ) -> bool:
        cfg = self.store.get_bello_config()
        if not self._coder_lifecycle_accepts_activity(cfg, require_running=False):
            return False
        if decision.generation is not None and decision.generation != cfg.generation:
            return False
        if packet_thread_id != cfg.coder_thread_id:
            return False
        if decision.wake_sequence is not None and decision.wake_sequence <= cfg.last_applied_supervisor_sequence:
            return False
        health = self.store.get_health()
        issue = (
            _runtime_restart_issue(
                packet,
                active_issue_key=health.restart_issue_key,
                active_issue_last_sequence=health.restart_issue_last_sequence,
            )
            if packet is not None
            else None
        )
        restart_candidate = False
        restart_candidate_reason: str | None = None
        if decision.decision == SupervisorDecisionKind.RESTART:
            restart_candidate, restart_candidate_reason = kill_restart_candidate(
                health,
                issue_key=issue.key if issue is not None else None,
                issue_sequence=issue.sequence if issue is not None else None,
            )
        apply_restart_metadata = decision.decision != SupervisorDecisionKind.RESTART or restart_candidate

        intervention: tuple[str, str] | None = None
        if decision.decision == SupervisorDecisionKind.INTERVENE and decision.message_to_coder and self.coder:
            self.tui.render("SUPERVISOR", f"steering coder: {decision.reason}")
            delivered, _ = await self._deliver_coder_message(decision.message_to_coder)
            if not delivered:
                return False
            intervention = (decision.reason, decision.message_to_coder)
        elif decision.decision == SupervisorDecisionKind.RESTART:
            if not restart_candidate:
                message = decision.message_to_coder or _restart_rejection_steering(decision.handoff)
                self.tui.render("SUPERVISOR", f"restart rejected without health evidence: {decision.reason}")
                if self.coder:
                    delivered, _ = await self._deliver_coder_message(message)
                    if not delivered:
                        return False
                intervention = (decision.reason, message)
            else:
                if restart_candidate_reason:
                    self.tui.render("SUPERVISOR", f"restart candidate: {restart_candidate_reason}")
                await self.restart(decision.reason or "supervisor requested restart", handoff=decision.handoff)
        elif decision.decision == SupervisorDecisionKind.PAUSE:
            await self.pause()

        # Commit the decision only after its externally visible action succeeds. In
        # particular, a failed steer must remain retryable with the same wake sequence.
        if intervention is not None:
            intervention_reason, intervention_message = intervention
            self._record_runtime_intervention(
                reason=intervention_reason,
                message=intervention_message,
                sequence=decision.wake_sequence or cfg.last_event_sequence,
                generation=cfg.generation,
                issue=issue,
            )
        if decision.persistent_decision:
            self.store.append_text_locked(DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update and apply_restart_metadata:
            self.store.append_text_locked(PROGRESS, f"- {decision.progress_update}\n")
            if decision.decision != SupervisorDecisionKind.RESTART:
                patch_health(
                    self.store,
                    HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence),
                )
        if decision.clear_handoff and apply_restart_metadata:
            self.store.write_text_locked(HANDOFF, "")
        if decision.display_message and apply_restart_metadata:
            self.tui.render("SUPERVISOR", decision.display_message)
        if decision.decision != SupervisorDecisionKind.NOOP:
            self._runtime_decision_invalidated_completion = True
        self._record_supervisor_decision_metric(use_case="runtime", decision=decision.decision.value)
        self.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "last_applied_supervisor_sequence": (
                        decision.wake_sequence
                        if decision.wake_sequence is not None
                        else current.last_applied_supervisor_sequence
                    )
                }
            )
        )
        return True

    async def apply_completion_decision(
        self,
        decision: CompletionReviewDecision,
        *,
        packet_thread_id: str | None,
        packet: SupervisorWakePacket | None = None,
    ) -> None:
        cfg = self.store.get_bello_config()
        if not self._coder_lifecycle_accepts_activity(cfg, require_running=False):
            return
        if decision.generation != cfg.generation:
            return
        if packet_thread_id != cfg.coder_thread_id:
            return
        if decision.wake_sequence <= cfg.last_applied_supervisor_sequence:
            return
        self.store.update_bello_config(
            lambda current: current.model_copy(update={"last_applied_supervisor_sequence": decision.wake_sequence})
        )
        self._append_completion_anchor_log(decision, packet=packet)
        self._record_supervisor_decision_metric(use_case="completion", decision=decision.decision.value)
        self._record_completion_knowledge(decision)
        if decision.decision == CompletionReviewDecisionKind.ACCEPT:
            if self._should_run_adversary_before_complete(packet):
                if packet is None or self._adversary_runs_remaining():
                    await self._run_adversary_before_complete(decision, packet=packet)
                    return
                self._record_adversary_limit_reached(packet)
        if decision.persistent_decision:
            self.store.append_text_locked(DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update:
            self.store.append_text_locked(PROGRESS, f"- {decision.progress_update}\n")
            patch_health(self.store, HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence))
        if decision.clear_handoff:
            self.store.write_text_locked(HANDOFF, "")
        if decision.display_message:
            self.tui.render("SUPERVISOR", decision.display_message)
        self._append_event(
            AppEventSource.SUPERVISOR,
            f"completion/{decision.decision.value}",
            decision=decision.decision.value,
            reason=decision.reason,
        )
        if decision.decision == CompletionReviewDecisionKind.ACCEPT:
            self._accepted_completion_decision = decision
            self._accepted_adversary_report = packet.adversary_report if packet is not None else None
            await self.finalize(
                f"accepted by completion_review: {decision.reason or 'task complete'}",
                status=BelloStatus.COMPLETE,
                completion_review_accepted=True,
            )
            return
        if decision.decision == CompletionReviewDecisionKind.RETURN:
            await self._return_completion_to_coder(decision)
            return
        if decision.decision == CompletionReviewDecisionKind.RESTART:
            if not getattr(self, "_generation_has_coder_turn", True):
                # Nothing to restart: the current generation has not run a single coder turn, so
                # this verdict can only be judging the previous generation's leftover state. With
                # the restart budget exhausted it would finalize the run as STUCK for no reason.
                self.store.append_text_locked(
                    PROGRESS,
                    "- Discarded completion restart issued before any coder work in the current generation.\n",
                )
                self._append_event(
                    AppEventSource.SUPERVISOR,
                    "completion/restart_discarded_virgin_generation",
                    reason=decision.reason,
                )
                if self.coder:
                    await self._deliver_coder_message(POST_RESTART_CONTINUE_NUDGE)
                return
            self.completion_restarts = getattr(self, "completion_restarts", 0) + 1
            await self.restart(decision.reason or "completion review requested restart", handoff=decision.handoff)
            return

    async def _run_adversary_before_complete(
        self,
        decision: CompletionReviewDecision | None,
        *,
        packet: SupervisorWakePacket | None,
    ) -> None:
        if packet is None:
            error_summary = "completion packet missing for the adversary run"
            if decision is None:
                await self._fail_required_adversary(packet=None, error_summary=error_summary)
            else:
                await self._complete_after_adversary_unavailable(
                    decision,
                    packet=None,
                    error_summary=error_summary,
                )
            return
        adversary_run_count, max_adversary_runs = self._reserve_adversary_run()
        self._adversary_reservation_recovery_pending = False
        self._write_run_checkpoint("adversary", state="active")
        forced_by_budget = decision is None
        run_reason = "completion review budget" if forced_by_budget else "completion accept"
        self.tui.render(
            "ADVERSARY",
            f"running pre-complete adversarial tester ({adversary_run_count}/{max_adversary_runs}; {run_reason})",
        )
        self.store.append_text_locked(
            PROGRESS,
            f"- Adversarial tester starting before final complete ({adversary_run_count}/{max_adversary_runs}; "
            f"trigger: {run_reason}).\n",
        )
        workspace_state_id = _workspace_state_id(self._active_workspace_root())
        snapshot_root: Path | None = None
        previous_report = getattr(self, "_pending_adversary_report", None)
        previous_report_payload = previous_report.model_dump(mode="json") if previous_report is not None else None
        try:
            snapshot_root = _create_adversary_snapshot(
                self._active_workspace_root(),
                excluded_relative_paths=self._review_private_relative_paths(),
            )
        except Exception as exc:
            error_summary = f"snapshot setup failed: {exc.__class__.__name__}: {exc}"
            if decision is None:
                await self._fail_required_adversary(packet=packet, error_summary=error_summary)
            else:
                await self._complete_after_adversary_unavailable(
                    decision,
                    packet=packet,
                    error_summary=error_summary,
                )
            return
        self._active_adversary_workspace_root = snapshot_root
        self._adversary_denied_commands = []
        agent = AdversaryAgent(
            self.client,
            snapshot_root,
            model=self._adversary_model(),
            intelligence=self._adversary_intelligence(),
            on_thread_start=self._mark_adversary_thread_started,
            on_thread_done=self._mark_adversary_thread_done,
            denied_probes=lambda: list(getattr(self, "_adversary_denied_commands", [])),
            multi_agent=self._adversary_multi_agent_config(),
            before_thread_cleanup=self._cleanup_adversary_reviewer_descendants,
        )
        try:
            result = await agent.run(packet, previous_adversary_report=previous_report_payload)
        except AdversaryAgentError as exc:
            if getattr(self, "_transport_error_pending", False):
                return
            if not self._completion_packet_lifecycle_is_current(packet):
                self._record_stale_adversary_discard(
                    packet,
                    reason=f"adversary failed after lifecycle changed: {exc}",
                )
                return
            self.store.append_raw_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "adversary_error",
                    "generation": packet.generation,
                    "completion_wake_sequence": (
                        decision.wake_sequence if decision is not None else packet.wake_sequence
                    ),
                    "adversary_run_count": adversary_run_count,
                    "max_adversary_runs": max_adversary_runs,
                    "error": str(exc),
                }
            )
            if decision is None:
                await self._fail_required_adversary(packet=packet, error_summary=str(exc))
            else:
                await self._complete_after_adversary_unavailable(
                    decision,
                    packet=packet,
                    error_summary=str(exc),
                )
            return
        finally:
            self._active_adversary_workspace_root = None
            if snapshot_root is not None:
                remove_isolated_workspace_tree(snapshot_root.parent)

        if not self._completion_packet_lifecycle_is_current(packet):
            self._record_stale_adversary_discard(
                packet,
                reason="adversary completed after lifecycle changed",
            )
            return

        report = AdversaryReport(
            candidate_finding=result.candidate_finding,
            report_text=result.report_text,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            generation=packet.generation,
            completion_wake_sequence=decision.wake_sequence if decision is not None else packet.wake_sequence,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence,
            validation_sequence=_latest_validation_sequence(packet.validations),
            workspace_state_id=workspace_state_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending_adversary_report = report
        self._write_run_checkpoint("adversary_report", state="stable")
        self.store.append_raw_log(
            {
                "timestamp": report.created_at,
                "type": "adversary_report",
                "generation": report.generation,
                "completion_wake_sequence": report.completion_wake_sequence,
                "latest_relevant_change_sequence": report.latest_relevant_change_sequence,
                "validation_sequence": report.validation_sequence,
                "workspace_state_id": report.workspace_state_id,
                "candidate_finding": report.candidate_finding,
                "thread_id": report.thread_id,
                "turn_id": report.turn_id,
                "adversary_run_count": adversary_run_count,
                "max_adversary_runs": max_adversary_runs,
                "report_chars": len(report.report_text),
                "report_sha256": hashlib.sha256(
                    report.report_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        self.store.append_text_locked(
            PROGRESS,
            "- Adversarial tester completed; adv_report_controller is normalizing findings and observations.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/report_ready",
            decision="normalize",
            reason="pre-complete adversarial report is ready for normalization",
        )
        await self._run_adv_report_controller(
            report,
            packet=packet,
            accepted_completion_decision=decision,
        )

    async def _run_adv_report_controller(
        self,
        report: AdversaryReport,
        *,
        packet: SupervisorWakePacket,
        accepted_completion_decision: CompletionReviewDecision | None,
    ) -> None:
        if not self._completion_packet_lifecycle_is_current(packet):
            self._record_stale_adversary_discard(
                packet,
                reason="adversary report controller skipped after lifecycle changed",
            )
            return
        agent = self._adv_report_controller_agent()
        if agent is None:
            await self._fail_adv_report_controller(
                "adv_report_controller agent is unavailable"
            )
            return
        review_packet = packet.model_copy(
            update={
                "current_summary": "Normalize the completed adversary report for the coder.",
                "adversary_report": report,
            }
        )
        self.tui.render(
            "ADVERSARY", "normalizing adversary findings and observations"
        )
        self._write_run_checkpoint("adversary_report_review", state="active")
        try:
            normalized = await agent.decide_adv_report(review_packet)
        except SupervisorAgentError:
            if getattr(self, "_transport_error_pending", False):
                return
            if not self._completion_packet_lifecycle_is_current(packet):
                self._record_stale_adversary_discard(
                    packet,
                    reason="adversary report controller failed after lifecycle changed",
                )
                return
            await self._fail_adv_report_controller(
                "agent or structured-output failure"
            )
            return

        if not self._completion_packet_lifecycle_is_current(packet):
            self._record_stale_adversary_discard(
                packet,
                reason="adversary report controller completed after lifecycle changed",
            )
            return

        stale_reason = self._adv_report_controller_staleness_reason(report)
        if stale_reason is not None:
            if not self._completion_packet_lifecycle_is_current(packet):
                self._record_stale_adversary_discard(packet, reason=stale_reason)
                return
            await self._fail_adv_report_controller(stale_reason)
            return

        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "adv_report_controller_decision",
                "generation": packet.generation,
                "completion_wake_sequence": report.completion_wake_sequence,
                "forward_to_coder": normalized.forward_to_coder,
                "reason": normalized.reason,
                "report_to_coder": normalized.report_to_coder,
            }
        )
        if normalized.forward_to_coder:
            report_to_coder = _adversary_report_with_definitions(
                normalized.report_to_coder or ""
            )
            self._append_event(
                AppEventSource.SUPERVISOR,
                "adversary/report_normalized",
                decision="return",
                reason=normalized.reason,
            )
            return_decision = CompletionReviewDecision(
                decision=CompletionReviewDecisionKind.RETURN,
                reason=normalized.reason,
                uncovered_behaviors=[
                    "Normalized adversary findings or observations require coder follow-up."
                ],
                message_to_coder=report_to_coder,
                persistent_decision=None,
                progress_update=None,
                clear_handoff=False,
                display_message=None,
                handoff=None,
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )
            await self._return_completion_to_coder(
                return_decision,
                source="adversary_report_controller",
            )
            return

        self.store.append_text_locked(
            PROGRESS,
            "- adv_report_controller found no findings or observations to send to the coder; finalizing.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/report_normalized",
            decision="complete",
            reason=normalized.reason,
        )
        if accepted_completion_decision is None:
            self._accepted_adversary_report = report
            await self._finalize_bounded_completion(
                reason=(
                    "completion review budget reached and the normalized adversary "
                    "report had nothing for the coder"
                ),
            )
            return
        await self._finalize_accepted_completion(
            accepted_completion_decision,
            adversary_report=report,
            result=(
                "accepted by completion_review after adversary report normalization: "
                f"{accepted_completion_decision.reason or 'task complete'}"
            ),
        )

    def _adv_report_controller_staleness_reason(
        self,
        report: AdversaryReport,
    ) -> str | None:
        cfg = self.store.get_bello_config()
        if report.generation != cfg.generation:
            return "adversary normalization became stale because the generation changed"
        task_integrity_issue = self._task_integrity_issue()
        if task_integrity_issue is not None:
            return f"adversary normalization detected task integrity failure: {task_integrity_issue}"
        if not report.workspace_state_id:
            return "adversary report is not bound to a workspace state"
        if report.workspace_state_id != _workspace_state_id(
            self._active_workspace_root()
        ):
            return "adversary normalization became stale because the workspace changed"
        return None

    def _completion_packet_lifecycle_is_current(self, packet: SupervisorWakePacket) -> bool:
        cfg = self.store.get_bello_config()
        return bool(
            self._coder_lifecycle_accepts_activity(cfg, require_running=False)
            and cfg.generation == packet.generation
            and cfg.coder_thread_id == packet.coder_thread_id
        )

    def _record_stale_adversary_discard(
        self,
        packet: SupervisorWakePacket,
        *,
        reason: str,
    ) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "stale_adversary_result_discarded",
                "packet_generation": packet.generation,
                "current_generation": self.store.get_bello_config().generation,
                "packet_thread_id": packet.coder_thread_id,
                "current_thread_id": self.store.get_bello_config().coder_thread_id,
                "reason": reason,
            }
        )

    async def _fail_adv_report_controller(self, error_summary: str) -> None:
        self.tui.render(
            "ADVERSARY", f"adv_report_controller failed: {error_summary}"
        )
        self.store.append_text_locked(
            PROGRESS,
            f"- adv_report_controller failed ({error_summary}); the raw adversary report was not sent to the coder.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/report_controller_failed",
            reason=error_summary,
        )
        await self.finalize(
            f"adv_report_controller failed: {error_summary}",
            status=BelloStatus.PROVIDER_FAILURE,
            completion_review_accepted=False,
        )

    def _completion_review_budget_action(
        self,
        *,
        packet: SupervisorWakePacket | None = None,
    ) -> Literal["adversary", "complete"] | None:
        cfg = self.store.get_bello_config()
        if self._effective_max_adversary_runs() <= 0:
            limit = cfg.max_completion_returns_before_adversary
            if review_limit_reached(limit, cfg.completion_return_count):
                return "complete"
            return None
        if cfg.adversary_run_count == 0:
            limit = cfg.max_completion_returns_before_adversary
            if review_limit_reached(limit, cfg.completion_return_count):
                return "adversary"
            return None
        limit = cfg.max_completion_returns_after_adversary
        if not review_limit_reached(limit, cfg.completion_returns_since_adversary):
            return None
        if self._adversary_runs_remaining():
            return "adversary"
        return "complete"

    async def _finalize_bounded_completion(self, *, reason: str) -> None:
        cfg = self.store.get_bello_config()
        self.store.append_text_locked(
            PROGRESS,
            "- Bounded completion policy reached its final review budget after the coder applied the last return; "
            "finalizing without fabricating a completion-review accept.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "completion/budget_finalize",
            decision={
                "kind": "complete",
                "completion_return_count": cfg.completion_return_count,
                "completion_returns_since_adversary": cfg.completion_returns_since_adversary,
                "adversary_run_count": cfg.adversary_run_count,
                "max_adversary_runs": self._effective_max_adversary_runs(),
            },
            reason=reason,
        )
        self._accepted_completion_decision = None
        await self.finalize(
            "completed normally",
            status=BelloStatus.COMPLETE,
            completion_review_accepted=None,
        )

    async def _fail_required_adversary(
        self,
        *,
        packet: SupervisorWakePacket | None,
        error_summary: str,
    ) -> None:
        cfg = self.store.get_bello_config()
        report = AdversaryReport(
            status="error",
            candidate_finding=False,
            report_text=f"required adversary did not run: {error_summary}",
            generation=packet.generation if packet is not None else cfg.generation,
            completion_wake_sequence=packet.wake_sequence if packet is not None else cfg.last_event_sequence + 1,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence if packet is not None else None,
            validation_sequence=_latest_validation_sequence(packet.validations) if packet is not None else None,
            workspace_state_id=_workspace_state_id(self._active_workspace_root()),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending_adversary_report = report
        self.tui.render("ADVERSARY", f"required adversarial tester could not run: {error_summary}")
        self.store.append_text_locked(
            PROGRESS,
            f"- Required adversarial tester could not run ({error_summary}); failing the run instead of treating it as accepted.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/required_unavailable",
            reason=error_summary,
        )
        await self.finalize(
            f"required adversary failed under bounded review policy: {error_summary}",
            status=BelloStatus.PROVIDER_FAILURE,
            completion_review_accepted=False,
        )

    async def _finalize_completion_review_disabled(self) -> None:
        """Completion review is disabled: the coder's readiness marker is the finish line.

        Runtime supervision (approvals, steering, restarts) already ran its course; the
        final report carries the validation ledger and states plainly that no completion
        review or adversary pass certified the result.
        """
        self.store.append_text_locked(
            PROGRESS,
            "- Coder declared readiness; completion review is disabled by config, finalizing without review.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "completion/review_disabled_finalize",
            reason="coder readiness marker with completion review disabled",
        )
        await self.finalize(
            "coder declared readiness; completion review disabled by config (no review or adversary certification)",
            status=BelloStatus.COMPLETE,
            completion_review_accepted=False,
        )

    async def _finalize_accepted_completion(
        self,
        decision: CompletionReviewDecision,
        *,
        adversary_report: AdversaryReport | None,
        result: str,
    ) -> None:
        cfg = self.store.get_bello_config()
        if decision.persistent_decision:
            self.store.append_text_locked(DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update:
            self.store.append_text_locked(PROGRESS, f"- {decision.progress_update}\n")
            patch_health(
                self.store,
                HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence),
            )
        if decision.clear_handoff:
            self.store.write_text_locked(HANDOFF, "")
        if decision.display_message:
            self.tui.render("SUPERVISOR", decision.display_message)
        self._append_event(
            AppEventSource.SUPERVISOR,
            "completion/accept",
            decision="accept",
            reason=decision.reason,
        )
        self._accepted_completion_decision = decision
        self._accepted_adversary_report = adversary_report
        await self.finalize(
            result,
            status=BelloStatus.COMPLETE,
            completion_review_accepted=True,
        )

    async def _complete_after_adversary_unavailable(
        self,
        decision: CompletionReviewDecision,
        *,
        packet: SupervisorWakePacket | None,
        error_summary: str,
    ) -> None:
        """The completion review accepted and only the adversary could not run.

        That is a tester-availability problem, not evidence against the accepted work:
        finalize the accept with the missing adversary coverage recorded loudly (same
        terminal shape as adversary-disabled or limit-reached) instead of declaring the
        whole run infrastructure-invalid and discarding a reviewed, accepted solution.
        """
        cfg = self.store.get_bello_config()
        report = AdversaryReport(
            status="error",
            candidate_finding=False,
            report_text=f"adversary did not run: {error_summary}",
            generation=packet.generation if packet is not None else cfg.generation,
            completion_wake_sequence=decision.wake_sequence,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence if packet is not None else None,
            validation_sequence=_latest_validation_sequence(packet.validations) if packet is not None else None,
            workspace_state_id=_workspace_state_id(self._active_workspace_root()),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.tui.render(
            "ADVERSARY",
            f"adversarial tester could not run ({error_summary}); finalizing completion accept",
        )
        self.store.append_text_locked(
            PROGRESS,
            f"- Adversarial tester could not run ({error_summary}); finalizing prior completion accept "
            "with adversary coverage recorded as missing.\n",
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/unavailable",
            reason=error_summary,
        )
        await self._finalize_accepted_completion(
            decision,
            adversary_report=report,
            result=f"accepted by completion_review; adversary tester could not run: {error_summary}",
        )

    def _adversary_runs_remaining(self) -> bool:
        cfg = self.store.get_bello_config()
        return cfg.adversary_run_count < self._effective_max_adversary_runs()

    def _should_run_adversary_before_complete(self, packet: SupervisorWakePacket | None) -> bool:
        if self._packet_has_fresh_adversary_report(packet):
            return False
        enabled = getattr(self, "adversary_enabled", None)
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.store.get_bello_config().max_adversary_runs > 0

    def _reserve_adversary_run(self) -> tuple[int, int]:
        max_adversary_runs = self._effective_max_adversary_runs()
        updated = self.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "adversary_run_count": current.adversary_run_count + 1,
                    "completion_returns_since_adversary": 0,
                }
            )
        )
        return updated.adversary_run_count, max_adversary_runs

    def _record_adversary_limit_reached(self, packet: SupervisorWakePacket | None) -> None:
        cfg = self.store.get_bello_config()
        max_adversary_runs = self._effective_max_adversary_runs()
        reason = f"adversary run limit reached ({cfg.adversary_run_count}/{max_adversary_runs})"
        self.tui.render("ADVERSARY", f"{reason}; finalizing completion accept")
        self.store.append_text_locked(
            PROGRESS,
            f"- Skipping adversarial tester before complete: {reason}.\n",
        )
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "adversary_limit_reached",
                "generation": packet.generation if packet is not None else None,
                "wake_sequence": packet.wake_sequence if packet is not None else None,
                "adversary_run_count": cfg.adversary_run_count,
                "max_adversary_runs": max_adversary_runs,
            }
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "adversary/limit_reached",
            reason=reason,
        )

    def _effective_max_adversary_runs(self) -> int:
        if not self._effective_completion_review():
            # The adversary runs inside the completion-review accept path; without the
            # review gate there is no point where it could fire.
            return 0
        enabled = getattr(self, "adversary_enabled", None)
        if enabled is False:
            return 0
        override = getattr(self, "adversary_runs", None)
        configured_runs = self.store.get_bello_config().max_adversary_runs if override is None else override
        if enabled is True:
            return max(1, configured_runs)
        return configured_runs

    def _fresh_adversary_report(
        self,
        *,
        generation: int,
        latest_relevant_change_sequence: int | None,
    ) -> AdversaryReport | None:
        report = getattr(self, "_pending_adversary_report", None)
        if report is None:
            return None
        if report.status != "completed" or report.generation != generation:
            return None
        if report.latest_relevant_change_sequence != latest_relevant_change_sequence:
            return None
        if report.workspace_state_id and report.workspace_state_id != _workspace_state_id(self._active_workspace_root()):
            return None
        return report

    def _packet_has_fresh_adversary_report(self, packet: SupervisorWakePacket | None) -> bool:
        if packet is None:
            return False
        report = packet.adversary_report
        if report is None:
            return False
        if report.status != "completed" or report.generation != packet.generation:
            return False
        if report.latest_relevant_change_sequence != packet.latest_relevant_change_sequence:
            return False
        if report.workspace_state_id and report.workspace_state_id != _workspace_state_id(self._active_workspace_root()):
            return False
        return True

    def _mark_adversary_thread_started(self, thread_id: str) -> None:
        self._active_adversary_thread_id = thread_id
        self._register_reviewer_thread(thread_id, role="adversary")

    def _mark_adversary_thread_done(self, thread_id: str) -> None:
        if getattr(self, "_active_adversary_thread_id", None) == thread_id:
            self._active_adversary_thread_id = None

    def _append_completion_anchor_log(
        self,
        decision: CompletionReviewDecision,
        *,
        packet: SupervisorWakePacket | None,
    ) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "completion_review_anchor",
                "decision": decision.decision.value,
                "wake_sequence": decision.wake_sequence,
                "generation": decision.generation,
                "reason": decision.reason,
                "packet_mode": packet.completion_payload_mode if packet is not None else None,
                "packet_since_sequence": packet.completion_payload_since_sequence if packet is not None else None,
                "validation_ids": [validation.validation_id for validation in (packet.validations if packet else [])],
                "changed_files": [changed.path for changed in (packet.changed_files if packet else [])],
            }
        )

    async def _return_completion_to_coder(
        self,
        decision: CompletionReviewDecision,
        *,
        source: Literal["completion_review", "adversary_report_controller"] = "completion_review",
    ) -> None:
        record = CompletionReturnRecord(
            source=source,
            reason=decision.reason,
            uncovered_behaviors=decision.uncovered_behaviors,
            validation_gaps=decision.validation_gaps,
            claim_evidence_mismatches=decision.claim_evidence_mismatches,
            packet_or_access_limitations=decision.packet_or_access_limitations,
            message_to_coder=decision.message_to_coder,
            sequence=decision.wake_sequence,
            generation=decision.generation,
        )
        self.completion_returns = [*getattr(self, "completion_returns", []), record][-50:]
        self.completion_review_return_sequence = decision.wake_sequence
        if not decision.progress_update:
            details = _completion_return_summary(decision)
            source_label = (
                "Adversary report controller"
                if source == "adversary_report_controller"
                else "Completion review"
            )
            self.store.append_text_locked(
                PROGRESS, f"- {source_label} returned: {details}\n"
            )
        self.prior_interventions.append(
            PriorIntervention(
                reason=(
                    "Adversary report controller returned: "
                    if source == "adversary_report_controller"
                    else "Completion review returned: "
                )
                + decision.reason,
                message_to_coder=decision.message_to_coder or "",
                sequence=decision.wake_sequence,
            )
        )
        self.prior_interventions = self.prior_interventions[-20:]
        if source != "adversary_report_controller":
            self.store.update_bello_config(
                lambda current: current.model_copy(
                    update={
                        "completion_return_count": current.completion_return_count + 1,
                        "completion_returns_since_adversary": (
                            current.completion_returns_since_adversary + 1
                            if current.adversary_run_count > 0
                            else 0
                        ),
                    }
                )
            )
        if self.coder and decision.message_to_coder:
            if self._revision_coder_enabled() and not self._revision_coder_active():
                try:
                    await self._switch_to_revision_coder(decision.message_to_coder, source=source)
                except _RevisionCoderDeliveryError as exc:
                    if not self._revision_switch_context_is_current(
                        generation=exc.generation,
                        thread_id=exc.thread_id,
                        coder=exc.coder,
                    ):
                        self._record_cancelled_revision_switch(
                            f"lifecycle changed while revision coder {exc.stage} failed"
                        )
                        return
                    await self._fail_revision_coder_switch(exc, source=source)
                    return
            else:
                await self._deliver_coder_message(decision.message_to_coder)
        # Fresh completion-review thread per review: close the session after each return so
        # the next readiness review starts a new thread instead of accumulating prior turns.
        # The persistent thread otherwise grows ~55-85k tokens per return and crossed the
        # model context window within a generation, forcing lossy auto-compaction. Prior
        # returns are still carried into the next review via previous_completion_returns,
        # and the reviewer re-reads the workspace live, so no context is lost.
        supervisor = self._completion_supervisor_agent()
        if supervisor is not None and hasattr(supervisor, "close_completion_review"):
            await supervisor.close_completion_review()

    async def _switch_to_revision_coder(
        self,
        reviewer_feedback: str,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        done = asyncio.get_running_loop().create_future()
        owner = asyncio.current_task()
        self._revision_switch_in_progress = True
        self._revision_switch_done = done
        self._revision_switch_owner = owner
        try:
            async with self._coder_activity_lock():
                if not self._coder_lifecycle_accepts_activity():
                    self._record_cancelled_revision_switch(
                        "lifecycle changed before the revision coder switch began"
                    )
                    return
                await self._perform_revision_coder_switch(
                    reviewer_feedback,
                    source=source,
                )
        finally:
            self._revision_switch_in_progress = False
            if not done.done():
                done.set_result(None)
            if getattr(self, "_revision_switch_done", None) is done:
                self._revision_switch_done = None
            if getattr(self, "_revision_switch_owner", None) is owner:
                self._revision_switch_owner = None

    async def _fail_revision_coder_switch(
        self,
        error: Exception,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        message = (
            f"revision coder profile switch failed while delivering {source} feedback: "
            f"{error.__class__.__name__}: {error}"
        )
        self.store.append_text_locked(PROGRESS, f"- {message}\n")
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "revision_coder_switch_failed",
                "source": source,
                "error_type": error.__class__.__name__,
                "error": str(error),
            }
        )
        self._append_event(
            AppEventSource.SUPERVISOR,
            "coder/profile_switch_failed",
            reason=message,
            payload={"source": source, "error_type": error.__class__.__name__},
        )
        # The finding has already been recorded, but it was not delivered. Treat an
        # app-server failure here like an initial coder startup failure: preserve the
        # workspace and stop explicitly instead of leaving a dead supervisor task.
        await self.finalize(message, status=BelloStatus.PROVIDER_FAILURE)

    async def _wait_for_revision_switch(self) -> None:
        if getattr(self, "_revision_switch_owner", None) is asyncio.current_task():
            return
        done = getattr(self, "_revision_switch_done", None)
        if done is not None and not done.done():
            await asyncio.shield(done)

    async def _perform_revision_coder_switch(
        self,
        reviewer_feedback: str,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        """Move review-driven revisions to one fresh coder thread without a health restart."""
        previous_coder = self.coder
        previous_thread_id = getattr(previous_coder, "thread_id", None)
        expected_config = self.store.get_bello_config()
        expected_generation = expected_config.generation
        if previous_thread_id != expected_config.coder_thread_id:
            return
        try:
            await self._quiesce_coder_tree("revision_profile_switch")
        except Exception as exc:
            raise _RevisionCoderDeliveryError(
                "prepare",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            self._record_cancelled_revision_switch("lifecycle changed while quiescing the initial coder")
            return
        try:
            await self._resolve_pending_approvals("revision coder profile switch")
            self._repair_snapshot_runtime_controls(source="revision_profile_switch")
        except Exception as exc:
            raise _RevisionCoderDeliveryError(
                "prepare",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            self._record_cancelled_revision_switch("lifecycle changed while resolving pending approvals")
            return

        revision_coder = CoderSession(
            self.client,
            self.store,
            self._active_workspace_root(),
            self._active_task_path(),
            model=self._revision_coder_model(),
            fast=self._fast_mode(),
            intelligence=self._revision_coder_intelligence(),
            multi_agent=self._multi_agent_config(),
            plan_path=None,
        )
        try:
            new_thread_id = await revision_coder.start_thread(persist_state=False)
        except Exception as exc:
            raise _RevisionCoderDeliveryError(
                "thread/start",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            await self._discard_uncommitted_revision_thread(
                revision_coder,
                reason="lifecycle changed while starting the revision thread",
            )
            return
        snapshot = getattr(self, "_coder_snapshot", None)
        if snapshot is not None:
            try:
                snapshot.detach_plan_exposure()
            except Exception as exc:
                await self._discard_uncommitted_revision_thread(
                    revision_coder,
                    reason="private plan could not be detached before revision",
                )
                raise _RevisionCoderDeliveryError(
                    "prepare",
                    exc,
                    generation=expected_generation,
                    thread_id=previous_thread_id,
                    coder=previous_coder,
                ) from exc
        self.workspace_plan_path = None
        self.coder = revision_coder
        self.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "revision_coder_active": True,
                    "coder_thread_id": new_thread_id,
                    "active_coder_turn_id": None,
                }
            )
        )
        self.last_coder_message = None
        self._last_completion_marker_sequence = None
        self._no_marker_completion_review_key = None
        self._deferred_completion_check = None
        self._subagent_policy_notified = set()
        self._generation_has_coder_turn = False
        self._append_event(
            AppEventSource.SUPERVISOR,
            "coder/profile_switch",
            thread_id=new_thread_id,
            reason=f"first {source} return moved revisions to the configured revision coder",
            payload={
                "source": source,
                "previous_thread_id": previous_thread_id,
                "revision_thread_id": new_thread_id,
                "model": self._revision_coder_model(),
                "intelligence": self._revision_coder_intelligence(),
            },
        )
        self.store.append_text_locked(
            PROGRESS,
            "- Switched once to the configured revision coder after reviewer feedback; "
            f"thread {new_thread_id}, profile {self._revision_coder_model()}/"
            f"{self._revision_coder_intelligence()}.\n",
        )
        self.tui.render(
            "SYSTEM",
            f"revision coder started ({self._revision_coder_model()}/"
            f"{self._revision_coder_intelligence()})",
        )
        try:
            turn_id = await revision_coder.start_revision_turn(
                reviewer_feedback,
                persist_state=False,
            )
        except Exception as exc:
            raise _RevisionCoderDeliveryError(
                "turn/start",
                exc,
                generation=expected_generation,
                thread_id=new_thread_id,
                coder=revision_coder,
            ) from exc
        if not self._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=new_thread_id,
            coder=revision_coder,
        ):
            try:
                await self._interrupt_stale_revision_turn(
                    revision_coder,
                    reason="lifecycle changed while starting the first revision turn",
                )
            except Exception:
                # pause/restart/finalize is waiting for this switch and will retry the
                # preserved turn id through its normal serialized quiesce path.
                pass
            return
        self.store.update_bello_config(
            lambda current: current.model_copy(update={"active_coder_turn_id": turn_id})
        )

    def _revision_switch_context_is_current(
        self,
        *,
        generation: int,
        thread_id: str | None,
        coder: Any,
    ) -> bool:
        config = self.store.get_bello_config()
        blocked_statuses = {
            BelloStatus.PAUSED,
            BelloStatus.RESTARTING,
            BelloStatus.COMPLETE,
            BelloStatus.ESCALATED,
            BelloStatus.STUCK,
            BelloStatus.PROVIDER_FAILURE,
            BelloStatus.EXITED,
        }
        return bool(
            self.coder is coder
            and config.generation == generation
            and config.coder_thread_id == thread_id
            and config.status not in blocked_statuses
            and not getattr(self, "paused", False)
            and not getattr(self, "_finalizing", False)
            and getattr(self, "running", True)
        )

    def _record_cancelled_revision_switch(self, reason: str) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "revision_coder_switch_cancelled",
                "reason": reason,
            }
        )

    async def _discard_uncommitted_revision_thread(
        self,
        coder: CoderSession,
        *,
        reason: str,
    ) -> None:
        self._record_cancelled_revision_switch(reason)
        thread_id = coder.thread_id
        if not isinstance(thread_id, str) or not hasattr(self.client, "thread_unsubscribe"):
            return
        try:
            await self.client.thread_unsubscribe(thread_id)
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind="cancelled_revision_thread",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    async def _interrupt_stale_revision_turn(
        self,
        coder: CoderSession,
        *,
        reason: str,
    ) -> None:
        self._record_cancelled_revision_switch(reason)
        try:
            await coder.interrupt()
        except Exception as exc:
            self._append_cleanup_error(
                cleanup_kind="stale_revision_turn",
                thread_id=coder.thread_id or "unknown",
                turn_id=coder.active_turn_id,
                error=exc,
            )
            raise
        interrupted_thread_id = coder.thread_id
        interrupted_turn_id = coder.active_turn_id
        coder.active_turn_id = None
        if interrupted_thread_id and interrupted_turn_id:
            self.store.update_bello_config(
                lambda current: current.model_copy(
                    update={
                        "active_coder_turn_id": (
                            None
                            if current.coder_thread_id == interrupted_thread_id
                            and current.active_coder_turn_id == interrupted_turn_id
                            else current.active_coder_turn_id
                        )
                    }
                )
            )

    async def _resolve_pending_approvals(self, reason: str) -> None:
        approvals = getattr(self, "approvals", None)
        if approvals is None:
            manager = ApprovalManager(
                self._active_workspace_root(),
                declared_grading_roots=getattr(self, "declared_grading_roots", ()),
                immutable_paths=self._immutable_approval_paths(),
            )
        else:
            manager = approvals
        for request_id, context in list(self.pending_approvals.items()):
            resolution = manager._deny(context, reason)
            await self.client.respond(request_id, manager.response_payload(context, resolution))
            self.pending_approvals.pop(request_id, None)
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": []}))

    async def _stop_supervisor_task(self) -> None:
        task = self._supervisor_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def diff_summary(self) -> str:
        if not self.use_git_diff:
            return ""
        if not await self._is_git_work_tree():
            return ""
        commands = [["git", "status", "--short"], ["git", "diff", "--stat"], ["git", "diff", "--name-only"]]
        parts: list[str] = []
        for command in commands:
            output = await self._git_output(
                self._git_command_excluding_review_private_inputs(command)
            )
            if output is not None:
                output = _filter_internal_git_output(
                    output,
                    command=command,
                    project_root=self._active_workspace_root(),
                    task_path=self._active_task_path(),
                )
                parts.append(f"$ {' '.join(command)}\n{output}")
        return "\n\n".join(parts)

    async def changed_files(self) -> list[ChangedFile]:
        if not self.use_git_diff:
            return [
                changed
                for changed in _observed_changed_files(self)
                if not self._is_review_private_path(changed.path)
            ]
        if not await self._is_git_work_tree():
            return [
                changed
                for changed in _observed_changed_files(self)
                if not self._is_review_private_path(changed.path)
            ]
        status_text = await self._git_output(
            self._git_command_excluding_review_private_inputs(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
            )
        )
        numstat_text = await self._git_output(
            self._git_command_excluding_review_private_inputs(
                ["git", "diff", "--numstat", "HEAD", "--"]
            )
        )
        if status_text is None and numstat_text is None:
            return []
        files: dict[str, ChangedFile] = {}
        for path, status in _git_status_entries_from_porcelain_v1_z(status_text or ""):
            if (
                path
                and not self._is_review_private_path(path)
                and not _is_ignored_changed_path(
                    path,
                    project_root=self._active_workspace_root(),
                    task_path=self._active_task_path(),
                )
            ):
                files[path] = ChangedFile(path=path, status=status)
        for line in (numstat_text or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            additions = _parse_numstat(parts[0])
            deletions = _parse_numstat(parts[1])
            path = parts[2].strip()
            if " => " in path:
                path = path.rsplit(" => ", 1)[1].strip("{}")
            if (
                not path
                or self._is_review_private_path(path)
                or _is_ignored_changed_path(
                    path,
                    project_root=self._active_workspace_root(),
                    task_path=self._active_task_path(),
                )
            ):
                continue
            existing = files.get(path)
            status = existing.status if existing else "modified"
            files[path] = ChangedFile(path=path, status=status, additions=additions, deletions=deletions)
        observed = getattr(self, "observed_changed_files", None)
        if isinstance(observed, dict):
            for path, observed_file in observed.items():
                if path in files:
                    files[path].sequence = observed_file.sequence
        return list(files.values())[:200]

    def _record_changed_files(self, action: TriggeringAction) -> None:
        if not action.paths:
            return
        observed = getattr(self, "observed_changed_files", None)
        if observed is None:
            observed = {}
            self.observed_changed_files = observed
        for raw_path in action.paths:
            path = _workspace_display_path(self._active_workspace_root(), raw_path)
            if (
                path
                and not self._is_review_private_path(path)
                and not _is_ignored_changed_path(
                    path,
                    project_root=self._active_workspace_root(),
                    task_path=self._active_task_path(),
                )
            ):
                observed[path] = ChangedFile(path=path, status="modified", sequence=getattr(self, "_sequence", None))

    async def _is_git_work_tree(self) -> bool:
        output = await self._git_output(["git", "rev-parse", "--is-inside-work-tree"])
        return output == "true"

    async def _git_output(self, command: list[str]) -> str | None:
        try:
            exec_command = list(command)
            env = None
            snapshot = getattr(self, "_coder_snapshot", None)
            if snapshot is not None and command and command[0] == "git":
                if not snapshot.git_control_is_trusted():
                    return None
                exec_command = ["git", "-c", "core.fsmonitor=false", *command[1:]]
                if len(command) > 1 and command[1] == "diff":
                    exec_command = [*exec_command[:4], "--no-ext-diff", "--no-textconv", *exec_command[4:]]
                env = snapshot_git_environment()
            if exec_command and exec_command[0] == "git":
                git = _controller_executable(
                    "git",
                    self._active_workspace_root(),
                    environ=env,
                )
                if git is None:
                    return None
                exec_command[0] = git
            proc = await asyncio.create_subprocess_exec(
                *exec_command,
                cwd=str(self._active_workspace_root()),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                return None
            return stdout.decode("utf-8", errors="replace").strip()
        except Exception:
            return None

    async def patch_summary(self, limit: int = 4000) -> str | None:
        if not self.use_git_diff:
            return None
        parts: list[str] = []
        for command in (["git", "diff", "--unified=2", "--"], ["git", "diff", "--cached", "--unified=2", "--"]):
            output = await self._git_output(
                self._git_command_excluding_review_private_inputs(command)
            )
            if output:
                parts.append(f"$ {' '.join(command)}\n{output}")
        if not parts:
            return None
        return _bounded_text("\n\n".join(parts), limit=limit)

    # --- cross-review knowledge: accumulated behavior surface + carried suspicions ---

    def _completion_knowledge(self) -> dict[str, list[Any]]:
        # In-memory only (see __init__): survives in-run restarts because the controller object
        # is reused, and is never sourced from the coder-writable workspace. Lazily initialized
        # so controllers built via __new__ in tests still work.
        state = getattr(self, "_completion_knowledge_state", None)
        if state is None:
            state = {"behavior_surface": [], "uncovered_edge_candidates": []}
            self._completion_knowledge_state = state
        return state

    def _behavior_surface_items(self) -> list[BehaviorSurfaceItem]:
        items: list[BehaviorSurfaceItem] = []
        for entry in self._completion_knowledge()["behavior_surface"]:
            try:
                items.append(BehaviorSurfaceItem.model_validate(entry))
            except Exception:
                continue
        return items

    def _record_completion_knowledge(self, decision: CompletionReviewDecision) -> None:
        """Merge the reviewer-returned surface (merge-only: entries are never removed) into the
        in-memory knowledge and carry its unverified suspicions to the next review."""
        knowledge = self._completion_knowledge()
        merged, changed = _merge_behavior_surface_items(knowledge["behavior_surface"], decision.behavior_surface)
        if changed:
            knowledge["behavior_surface"] = merged
        artifact = decision.decision_artifact
        if artifact is not None:
            candidates = [item for item in artifact.uncovered_edge_candidates if isinstance(item, str) and item.strip()]
            if candidates != knowledge["uncovered_edge_candidates"]:
                knowledge["uncovered_edge_candidates"] = candidates

    async def completion_packet_details(
        self,
        changed_files: list[ChangedFile],
        *,
        since_sequence: int | None = None,
    ) -> dict[str, Any]:
        diff_limit = 12000
        context_limit = 8000
        changed_file_diffs: list[ChangedFileDiff] = []
        changed_file_contexts: list[ChangedFileContext] = []
        changed_tests_summary: list[ChangedTestsSummary] = []
        omitted: list[str] = []
        total_diff_chars = 0
        total_context_chars = 0
        materially_truncated = False
        truncation_reasons: list[str] = []
        is_git = self.use_git_diff and await self._is_git_work_tree()
        review_changed_files = [
            changed
            for changed in changed_files
            if not self._is_review_private_path(changed.path)
        ]
        detail_changed_files = [
            changed
            for changed in review_changed_files
            if since_sequence is None or changed.sequence is None or changed.sequence > since_sequence
        ]
        review_validations = self._review_safe_values(list(self.validations))
        review_inspections = self._review_safe_values(
            list(getattr(self, "inspections", []))
        )
        detail_validations = [
            validation
            for validation in review_validations
            if since_sequence is None or validation.sequence > since_sequence
        ]
        detail_inspections = [
            inspection
            for inspection in review_inspections
            if since_sequence is None or inspection.sequence > since_sequence
        ]

        for changed in detail_changed_files[:200]:
            file_kind = _file_kind(changed.path)
            change_kind = _change_kind(changed.status)
            diff_text = ""
            omitted_reason: str | None = None
            if is_git:
                diff_text = await self._changed_file_diff(changed.path)
            if not diff_text and change_kind == "added":
                file_text = _read_workspace_file(self._active_workspace_root(), changed.path, limit=diff_limit)
                if file_text is not None:
                    diff_text = f"<new file snapshot>\n{file_text.text}"
            if not diff_text:
                omitted_reason = "No git diff or readable file snapshot was available for this changed file."
                omitted.append(changed.path)
                materially_truncated = True
            bounded_diff = _bounded_text(diff_text, limit=diff_limit) if diff_text else ""
            diff_truncated = bool(diff_text) and len(diff_text) > len(bounded_diff)
            if diff_truncated:
                materially_truncated = True
                truncation_reasons.append(f"{changed.path}: diff exceeded {diff_limit} characters")
            total_diff_chars += len(bounded_diff)
            changed_file_diffs.append(
                ChangedFileDiff(
                    path=changed.path,
                    file_kind=file_kind,
                    change_kind=change_kind,
                    diff=bounded_diff,
                    diff_truncated=diff_truncated,
                    omitted_reason=omitted_reason,
                )
            )

            if change_kind == "deleted":
                continue
            context = _read_workspace_file(self._active_workspace_root(), changed.path, limit=context_limit)
            if context is None:
                continue
            total_context_chars += len(context.text)
            if context.truncated:
                materially_truncated = True
                truncation_reasons.append(f"{changed.path}: final file context exceeded {context_limit} characters")
            changed_file_contexts.append(
                ChangedFileContext(
                    path=changed.path,
                    final_snippets_around_changed_hunks=context.text,
                    context_truncated=context.truncated,
                )
            )
            if file_kind == "test":
                changed_tests_summary.append(_changed_tests_summary(changed.path, context.text, detail_validations))

        return {
            "changed_file_diffs": changed_file_diffs,
            "changed_file_contexts": changed_file_contexts,
            "changed_tests_summary": changed_tests_summary,
            "validation_outputs": [_validation_output(validation) for validation in detail_validations],
            "inspection_outputs": [_inspection_output(inspection) for inspection in detail_inspections],
            "completion_delta_evidence_summary": _completion_delta_evidence_summary(
                detail_validations,
                detail_inspections,
                since_sequence=since_sequence,
            ),
            "breadth_risk_summary": _breadth_risk_summary(
                task_contents=self._canonical_task_text(),
                changed_files=review_changed_files,
            ),
            "diff_packet_limits": DiffPacketLimits(
                total_diff_chars=total_diff_chars,
                total_context_chars=total_context_chars,
                omitted_changed_files=omitted,
                materially_truncated=materially_truncated,
                truncation_reason="; ".join(truncation_reasons) if truncation_reasons else None,
            ),
        }

    async def _changed_file_diff(self, path: str) -> str:
        if self._is_review_private_path(path):
            return ""
        parts: list[str] = []
        for command in (
            ["git", "diff", "--unified=80", "--", path],
            ["git", "diff", "--cached", "--unified=80", "--", path],
        ):
            output = await self._git_output(command)
            if output:
                parts.append(f"$ {' '.join(command)}\n{output}")
        return "\n\n".join(parts)

    async def _on_notification(self, message: AppServerMessage) -> None:
        await self.event_queue.put(ControllerEvent(kind="notification", message=message))

    async def _on_server_request(self, message: AppServerMessage) -> None:
        await self.event_queue.put(ControllerEvent(kind="server_request", message=message))

    async def _on_transport_error(self, error: BaseException) -> None:
        if getattr(self, "_transport_error_pending", False):
            return
        self._transport_error_pending = True
        await self.event_queue.put(ControllerEvent(kind="transport_error", error=error, error_message=str(error)))

    def _append_cleanup_error(
        self,
        *,
        cleanup_kind: str,
        thread_id: str,
        turn_id: str | None,
        error: BaseException,
    ) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "cleanup_error",
                "cleanup_kind": cleanup_kind,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "error_type": error.__class__.__name__,
                "error": str(error),
            }
        )

    def _readiness_journal(self) -> deque[_ReadinessJournalEvent]:
        limit = max(
            1,
            int(
                getattr(
                    self,
                    "_readiness_event_journal_limit",
                    READINESS_EVENT_JOURNAL_LIMIT,
                )
            ),
        )
        journal = getattr(self, "_readiness_event_journal", None)
        if not isinstance(journal, deque) or journal.maxlen != limit:
            journal = deque(journal or (), maxlen=limit)
            self._readiness_event_journal = journal
        return journal

    def _register_reviewer_thread(self, thread_id: str, *, role: str = "reviewer") -> None:
        if not isinstance(thread_id, str) or not thread_id:
            return
        registry = getattr(self, "_reviewer_thread_ids", None)
        if not isinstance(registry, OrderedDict):
            registry = OrderedDict()
            self._reviewer_thread_ids = registry
        roles = getattr(self, "_reviewer_thread_roles", None)
        if not isinstance(roles, dict):
            roles = {}
            self._reviewer_thread_roles = roles
        registry[thread_id] = None
        roles[thread_id] = role
        registry.move_to_end(thread_id)
        limit = max(
            1,
            int(
                getattr(
                    self,
                    "_readiness_reviewer_thread_limit",
                    READINESS_REVIEWER_THREAD_LIMIT,
                )
            ),
        )
        while len(registry) > limit:
            evicted_thread_id, _ = registry.popitem(last=False)
            roles.pop(evicted_thread_id, None)

    def _reviewer_role_for_thread(self, thread_id: Any) -> str | None:
        if not isinstance(thread_id, str):
            return None
        roles = getattr(self, "_reviewer_thread_roles", {})
        if not isinstance(roles, dict):
            roles = {}
        registry = getattr(self, "_reviewer_thread_ids", {})
        reviewer_roots = registry if isinstance(registry, dict) else {}
        direct = roles.get(thread_id)
        if isinstance(direct, str):
            return direct
        if thread_id in reviewer_roots:
            return "reviewer"
        current = thread_id
        seen: set[str] = set()
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            parent = state.parent_thread_id
            role = roles.get(parent)
            if isinstance(role, str):
                return role
            if parent in reviewer_roots:
                return "reviewer"
            current = parent
        return None

    def _reviewer_descendant_depth(self, thread_id: Any) -> int | None:
        if not isinstance(thread_id, str):
            return None
        roles = getattr(self, "_reviewer_thread_roles", {})
        if not isinstance(roles, dict):
            roles = {}
        registry = getattr(self, "_reviewer_thread_ids", {})
        reviewer_roots = registry if isinstance(registry, dict) else {}
        roots = set(roles) | set(reviewer_roots)
        if thread_id in roots:
            return 0
        if not roots:
            return None
        current = thread_id
        seen: set[str] = set()
        depth = 0
        for _ in range(32):
            if current in seen:
                return None
            seen.add(current)
            state = self._subagent_registry().get(current)
            if state is None or not isinstance(state.parent_thread_id, str):
                return None
            depth += 1
            parent = state.parent_thread_id
            if parent in roots:
                return depth
            current = parent
        return None

    def _append_event(
        self,
        source: AppEventSource,
        event_type: str,
        *,
        thread_id: Any = None,
        turn_id: Any = None,
        item_id: Any = None,
        decision: Any = None,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._sequence += 1
        cfg = self.store.get_bello_config()
        event = AppEvent(
            sequence=self._sequence,
            generation=cfg.generation,
            source=source,
            event_type=event_type,
            thread_id=thread_id if isinstance(thread_id, str) else None,
            turn_id=turn_id if isinstance(turn_id, str) else None,
            item_id=item_id if isinstance(item_id, str) else None,
            decision=decision,
            reason=reason,
            payload=payload or {},
        )
        self.store.append_event(event)
        self._readiness_journal().append(
            _ReadinessJournalEvent(
                sequence=event.sequence,
                source=source,
                event_type=event_type,
                thread_id=event.thread_id,
            )
        )
        self.store.update_bello_config(lambda current: current.model_copy(update={"last_event_sequence": self._sequence}))

    def _generate_schema_hash(self) -> str:
        codex = _controller_executable("codex", self.project_root)
        if codex is None:
            raise RuntimeError("codex executable not found")
        with tempfile.TemporaryDirectory(prefix="bello-appserver-schema-") as tmp_dir:
            out_dir = Path(tmp_dir)
            completed = subprocess.run(
                [codex, "app-server", "generate-json-schema", "--experimental", "--out", str(out_dir)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError((completed.stdout + completed.stderr).strip() or "app-server schema generation failed")
            required = ["ClientRequest.json", "ServerRequest.json", "TurnStartParams.json", "CommandExecutionRequestApprovalParams.json"]
            for rel in required:
                if not _schema_file_exists(out_dir, rel):
                    raise RuntimeError(f"app-server schema missing required file: {rel}")
            if not _turn_start_schema_supports_effort(out_dir):
                raise RuntimeError("app-server schema missing required turn effort field for Bello intelligence settings")
            digest = hashlib.sha256()
            for path in sorted(out_dir.rglob("*.json")):
                digest.update(str(path.relative_to(out_dir)).encode("utf-8"))
                digest.update(path.read_bytes())
            return digest.hexdigest()

    async def _generate_schema_hash_async(self) -> str:
        return await asyncio.to_thread(self._generate_schema_hash)

    async def _structured_output_self_test(self) -> None:
        agent = StatelessSupervisorAgent(
            self.client,
            self.store,
            self.task_path,
            workspace_root=self._active_workspace_root(),
            task_contents=self._canonical_task_text(),
            model=self._runtime_model(),
            fast=self._fast_mode(),
            intelligence=self._runtime_intelligence(),
        )
        cfg = self.store.get_bello_config()
        packet = SupervisorWakePacket(
            wake_sequence=1,
            latest_event_sequence=cfg.last_event_sequence,
            generation=cfg.generation,
            restart_count=cfg.restart_count,
            task_path=str(self.task_path),
            task_contents="Structured output self-test. Return noop.",
            progress="",
            decisions="",
            last_actions=[],
            health=self.store.get_health().model_dump(mode="json"),
            recent_events=[],
            current_summary="Startup structured-output self-test. Return decision noop.",
            coder_thread_id=None,
            active_coder_turn_id=None,
        )
        decision = await asyncio.wait_for(agent.decide(packet), timeout=240)
        if decision.decision not in {SupervisorDecisionKind.NOOP, SupervisorDecisionKind.PAUSE}:
            raise RuntimeError("structured-output supervisor self-test returned an unexpected decision")

    async def _configure_runtime_triage(self) -> None:
        config = runtime_triage_config_from_env(enabled=self._cheap_runtime_enabled())
        self.runtime_triage_config = config
        self.runtime_triage_reviewer = None
        if not config.enabled:
            self.tui.render("SYSTEM", "cheap runtime triage disabled by configuration")
            return
        if config.model is None:
            self.tui.render("SYSTEM", "cheap runtime triage disabled: no model configured")
            self.runtime_triage_config = CheapRuntimeTriageConfig(
                enabled=False, model=None, timeout_seconds=config.timeout_seconds
            )
            return
        reviewer = CheapRuntimeReviewer(
            self.client,
            self._active_workspace_root(),
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        try:
            await self._cheap_runtime_structured_output_self_test(reviewer)
        except Exception as exc:
            self.tui.render(
                "SYSTEM",
                f"cheap runtime triage unavailable; full supervisor on every wake ({exc.__class__.__name__})",
            )
            self.runtime_triage_config = CheapRuntimeTriageConfig(
                enabled=False, model=config.model, timeout_seconds=config.timeout_seconds
            )
            return
        self.runtime_triage_reviewer = reviewer
        self.tui.render("SYSTEM", f"cheap runtime triage enabled with model {config.model}")

    async def _cheap_runtime_structured_output_self_test(self, reviewer: CheapRuntimeReviewer) -> None:
        packet = SupervisorWakePacket(
            wake_sequence=1,
            latest_event_sequence=0,
            generation=0,
            restart_count=0,
            task_path=str(self.task_path),
            task_contents="",
            current_summary="Startup runtime-triage self-test: routine read-only progress, no failing checks.",
        )
        decision = await asyncio.wait_for(reviewer.review(packet), timeout=reviewer.timeout_seconds)
        if decision.decision not in {"noop", "escalate"}:
            raise RuntimeError("cheap runtime structured-output self-test returned an unexpected decision")

    async def _cheap_runtime_route(self, packet: SupervisorWakePacket) -> CheapRuntimeDecision | None:
        reviewer = self.runtime_triage_reviewer
        if reviewer is None:
            return None
        started = time.monotonic()
        try:
            decision = await reviewer.review(packet)
        except CheapRuntimeReviewerError as exc:
            self._record_cheap_runtime_attempt(
                packet, decision=None, outcome=f"error:{exc.__class__.__name__}", started=started, fallback=True
            )
            return None
        self._record_cheap_runtime_attempt(
            packet,
            decision=decision,
            outcome=decision.decision,
            started=started,
            fallback=(decision.decision == "escalate"),
        )
        if decision.decision == "noop":
            self.tui.render("SUPERVISOR", f"cheap runtime triage: noop ({decision.reason_code})")
        return decision

    def _record_cheap_runtime_attempt(
        self,
        packet: SupervisorWakePacket,
        *,
        decision: CheapRuntimeDecision | None,
        outcome: str,
        started: float,
        fallback: bool,
    ) -> None:
        self.store.append_raw_log(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "cheap_runtime_review",
                "wake_sequence": packet.wake_sequence,
                "generation": packet.generation,
                "current_summary": (packet.current_summary or "")[:160],
                "trigger_reasons": list(_runtime_trigger_reasons_from_summary(packet.current_summary)),
                "decision": decision.decision if decision is not None else None,
                "reason_code": decision.reason_code if decision is not None else None,
                "outcome": outcome,
                "latency_seconds": time.monotonic() - started,
                "model": self.runtime_triage_config.model,
                "full_supervisor_fallback": fallback,
            }
        )


def _approval_wake_context(
    context: ApprovalContext,
    reason: str | None = None,
    *,
    origin: str = "coder",
) -> ApprovalWakeContext:
    return ApprovalWakeContext(
        request_type=context.request_type.value,
        server_request_id=context.server_request_id,
        method=context.server_request_method,
        available_decisions=context.available_decisions,
        command=context.command,
        file_changes=context.file_changes,
        paths=context.paths,
        cwd=context.cwd,
        grant_root=context.grant_root,
        network_approval_context=context.network_approval_context,
        proposed_execpolicy_amendment=context.proposed_execpolicy_amendment,
        proposed_network_policy_amendments=context.proposed_network_policy_amendments,
        reason=reason,
        origin=origin,
    )


def _runtime_packet_requires_full_supervisor(packet: SupervisorWakePacket) -> bool:
    reasons = set(_runtime_trigger_reasons_from_summary(packet.current_summary))
    if reasons & MANDATORY_FULL_RUNTIME_WAKE_REASONS:
        return True
    return (packet.current_summary or "").lstrip().startswith("Runtime integrity trigger:")


def _runtime_trigger_reasons_from_summary(summary: str | None) -> tuple[str, ...]:
    if not summary:
        return ()
    match = re.match(r"\s*Runtime trigger \(([^)]*)\):", summary)
    if not match:
        return ()
    return tuple(
        reason
        for reason in (part.strip() for part in match.group(1).split(","))
        if reason and reason != "masked_validation"
    )


_RESTART_SHELL_NAMES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})

_LITERAL_POWERSHELL_PYTHONPATH_PREFIX = re.compile(
    r"\A\s*\$env:PYTHONPATH\s*=\s*'(?:(?:'')|[^'\r\n])*'\s*;\s*(?P<command>[^\r\n]+?)\s*\Z",
    re.IGNORECASE,
)
_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER = re.compile(
    r'\A(?P<prefix>.*?)"(?P<payload>\$env:PYTHONPATH[^"\r\n]*)"\s*\Z',
    re.IGNORECASE,
)
_SPLICE_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER = re.compile(
    r'\A(?P<prefix>.*?)\'\$env:PYTHONPATH=\'"(?P<tail>\'[^"\r\n]*)"\s*\Z',
    re.IGNORECASE,
)


def _literal_powershell_file_invocation(command: str) -> tuple[list[str], str] | None:
    """Extract a simple literal ``powershell -File script.ps1`` invocation.

    The approval parser deliberately leaves every ``-File`` invocation for
    supervisor judgment.  Runtime evidence classification has a narrower job:
    after Codex has already run a command, recognize a literal script execution
    without treating PowerShell expansion or composition as evidence.  Keep the
    two paths separate so this recognition cannot widen auto-approval policy.
    """

    tokens, problem = lex_windows_command(command, "powershell")
    if tokens is None or problem or _executable_basename(tokens[0]) not in {"powershell", "pwsh"}:
        return None
    lowered = [token.casefold() for token in tokens]
    file_indexes = [index for index, token in enumerate(lowered[1:], start=1) if token == "-file"]
    if len(file_indexes) != 1:
        return None
    file_index = file_indexes[0]
    script_index = file_index + 1
    if script_index >= len(tokens):
        return None

    # Accept only the small host-option subset needed for deterministic,
    # non-interactive script execution.  Everything after -File belongs to the
    # script and remains literal because lex_windows_command already rejected
    # expansion, escaping, redirection, and composition.
    switches = {"-nologo", "-noprofile", "-noninteractive", "-mta", "-sta"}
    value_options = {"-executionpolicy", "-inputformat", "-outputformat", "-version", "-windowstyle"}
    index = 1
    while index < file_index:
        option = lowered[index]
        if option in switches:
            index += 1
            continue
        if option in value_options and index + 1 < file_index:
            index += 2
            continue
        return None

    script = tokens[script_index]
    if not script or script.startswith("-") or not script.casefold().endswith(".ps1"):
        return None
    return [_executable_basename(script), *tokens[script_index + 1 :]], script


def _literal_powershell_pythonpath_payload(payload: str) -> tuple[list[str], str] | None:
    """Recognize one literal PYTHONPATH assignment followed by one command.

    This is runtime-evidence parsing, not approval parsing.  PowerShell env
    assignments require ``;`` composition, so the approval lexer correctly
    leaves them for supervisor judgment.  Once the command has executed, we
    can safely classify this one narrow form by removing only a single-quoted
    literal PYTHONPATH prefix and passing the entire remainder back through the
    existing fail-closed Windows lexer.
    """

    match = _LITERAL_POWERSHELL_PYTHONPATH_PREFIX.fullmatch(payload)
    if match is None:
        return None
    command = match.group("command")
    tokens, problem = lex_windows_command(command, "powershell", cross_shell_safe=True)
    if tokens is None or problem:
        return None
    normalized = list(tokens)
    normalized[0] = _executable_basename(normalized[0])
    # The production failure was specifically ``python -m pytest``.  Keeping
    # this exception on that exact action avoids exposing unrelated Python or
    # tool classifiers through a new env-prefix surface.
    if normalized[0] != "py" and re.fullmatch(r"python(?:3(?:\.\d+)?)?", normalized[0]) is None:
        return None
    python_action = _windows_python_action(normalized)
    if python_action is None or python_action[:2] != ("module", "pytest"):
        return None
    return normalized, command


def _literal_powershell_pythonpath_invocation(command: str) -> tuple[list[str], str] | None:
    """Extract the narrow PYTHONPATH form from a PowerShell ``-Command`` wrapper."""

    if "\n" in command or "\r" in command:
        return None

    match = _QUOTED_POWERSHELL_PYTHONPATH_WRAPPER.fullmatch(command)
    if match is not None:
        payload = match.group("payload")
    else:
        # Codex's Windows command renderer can represent a single quote inside
        # the payload with a POSIX-style quote splice, for example:
        #   -Command '$env:PYTHONPATH='"'C:\deps;src'; python -m pytest -q"
        # Recognize that exact boundary without asking POSIX shlex to interpret
        # arbitrary PowerShell syntax; the two grammars disagree on quote
        # termination and can otherwise hide outer-shell composition.
        match = _SPLICE_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER.fullmatch(command)
        if match is None:
            return None
        payload = f"$env:PYTHONPATH={match.group('tail')}"

    marker = "__bello_literal_pythonpath_payload__"
    sanitized_wrapper = f'{match.group("prefix")}\"{marker}\"'
    tokens, problem = lex_windows_command(sanitized_wrapper, "powershell", cross_shell_safe=True)
    if tokens is None or problem:
        return None
    if not tokens or _executable_basename(tokens[0]) not in {"powershell", "pwsh"}:
        return None

    lowered = [token.casefold() for token in tokens]
    command_indexes = [
        index for index, token in enumerate(lowered[1:], start=1) if token in {"-c", "-command"}
    ]
    if len(command_indexes) != 1:
        return None
    command_index = command_indexes[0]
    payload_index = command_index + 1
    if payload_index != len(tokens) - 1 or tokens[payload_index] != marker:
        return None

    switches = {"-nologo", "-noprofile", "-noninteractive", "-mta", "-sta"}
    value_options = {"-executionpolicy", "-inputformat", "-outputformat", "-version", "-windowstyle"}
    index = 1
    while index < command_index:
        option = lowered[index]
        if option in switches:
            index += 1
            continue
        if option in value_options and index + 1 < command_index:
            index += 2
            continue
        return None

    return _literal_powershell_pythonpath_payload(payload)


def _windows_classification_tokens(command: str) -> tuple[bool, list[str] | None, str | None]:
    """Return normalized tokens for a native/wrapped Windows command.

    The boolean distinguishes "not a Windows command surface" from "a Windows
    surface whose syntax is ambiguous".  Callers must treat the latter as
    unclassified, never fall through to POSIX ``shlex`` or regex matching.
    """

    current = command
    for _ in range(6):
        wrapper = windows_shell_wrapper_payload(current)
        if wrapper is None:
            break
        shell_kind, payload, _problem = wrapper
        if payload is None:
            if shell_kind == "powershell":
                pythonpath_invocation = _literal_powershell_pythonpath_invocation(current)
                if pythonpath_invocation is not None:
                    tokens, command_payload = pythonpath_invocation
                    return True, tokens, command_payload
                file_invocation = _literal_powershell_file_invocation(current)
                if file_invocation is not None:
                    tokens, script = file_invocation
                    return True, tokens, script
            return True, None, None
        if command_is_windows_shell_wrapper(payload):
            current = payload
            continue
        tokens, problem = lex_windows_command(payload, shell_kind)
        if tokens is None or problem:
            return True, None, payload
        normalized = list(tokens)
        normalized[0] = _executable_basename(normalized[0])
        return True, normalized, payload
    else:
        return True, None, None
    if command_is_windows_shell_wrapper(command):
        return True, None, None
    shell_kind = native_shell_kind()
    if shell_kind == "posix":
        return False, None, None
    tokens, problem = lex_windows_command(command, shell_kind, cross_shell_safe=True)
    if tokens is None or problem:
        if shell_kind == "powershell":
            pythonpath_invocation = _literal_powershell_pythonpath_payload(command)
            if pythonpath_invocation is not None:
                normalized, command_payload = pythonpath_invocation
                return True, normalized, command_payload
        return True, None, command
    normalized = list(tokens)
    normalized[0] = _executable_basename(normalized[0])
    return True, normalized, command


def _windows_tokens_are_git_inspection(tokens: list[str]) -> bool:
    if not tokens or tokens[0] != "git" or len(tokens) < 2:
        return False
    subcommand = tokens[1].casefold()
    args = [token.casefold() for token in tokens[2:]]
    if subcommand == "branch":
        # Creating, copying, renaming, or deleting a branch is mutation.  The
        # no-argument/options-only forms are the subset we can prove to be an
        # inspection without implementing Git's full option grammar.
        return not any(not arg.startswith("-") for arg in args)
    if subcommand == "remote":
        return not args or args == ["-v"] or (args[0] == "get-url" and len(args) == 2)
    return subcommand in {
        "diff",
        "for-each-ref",
        "log",
        "rev-parse",
        "show",
        "status",
    }


def _windows_effective_tool_tokens(tokens: list[str]) -> list[str]:
    if len(tokens) > 1 and tokens[0] == "npx" and not tokens[1].startswith("-"):
        return [_executable_basename(tokens[1]), *tokens[2:]]
    return tokens


def _windows_python_args(tokens: list[str]) -> list[str] | None:
    if not tokens:
        return None
    executable = tokens[0]
    if executable == "py":
        args = list(tokens[1:])
        if args and re.fullmatch(r"-3(?:\.\d+)?", args[0]):
            args = args[1:]
        elif args and (
            re.match(r"^-\d", args[0])
            or args[0].casefold().startswith(("-v:", "--list", "--company", "--tag"))
        ):
            return None
        return args
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        return list(tokens[1:])
    return None


def _windows_python_action(tokens: list[str]) -> tuple[str, str, list[str]] | None:
    """Return Python's first executable action without scanning later argv.

    ``-c code -m pytest`` runs ``code`` and merely passes ``-m pytest`` to that
    code.  Looking for ``-m`` anywhere therefore turns harmless output from a
    different action into false test evidence.  Parse only the small, explicit
    interpreter-option subset that may precede Python's mutually exclusive
    ``-c``/``-m``/script action.
    """

    args = _windows_python_args(tokens)
    if args is None:
        return None
    no_value_options = {
        "-b",
        "-bb",
        "-B",
        "-d",
        "-E",
        "-i",
        "-I",
        "-O",
        "-OO",
        "-P",
        "-q",
        "-R",
        "-s",
        "-S",
        "-u",
        "-v",
        "-x",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in no_value_options:
            index += 1
            continue
        if arg in {"-W", "-X", "--check-hash-based-pycs"}:
            if index + 1 >= len(args):
                return None
            index += 2
            continue
        if (arg.startswith("-W") or arg.startswith("-X")) and len(arg) > 2:
            index += 1
            continue
        if arg in {"-c", "-m"}:
            if index + 1 >= len(args) or not args[index + 1]:
                return None
            return ("command" if arg == "-c" else "module"), args[index + 1], args[index + 2 :]
        if arg == "--":
            if index + 1 >= len(args) or not args[index + 1]:
                return None
            return "script", args[index + 1], args[index + 2 :]
        if arg == "-":
            return "script", arg, args[index + 1 :]
        if arg.startswith("-"):
            return None
        return "script", arg, args[index + 1 :]
    return None


_PYTEST_NO_RUN_OPTIONS = frozenset(
    {
        "--cache-show",
        "--co",
        "--collect-only",
        "--fixtures",
        "--fixtures-per-test",
        "--funcargs",
        "--help",
        "--markers",
        "--setup-only",
        "--setup-plan",
        "--version",
    }
)


def _pytest_args_request_no_test_execution(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            break
        if (
            arg.startswith("-h")
            or re.fullmatch(r"-[qvxslf]+h.*", arg)
            or re.fullmatch(r"-(?:h|V)+", arg)
        ):
            return True
        if not arg.startswith("--"):
            continue
        option = arg.casefold().partition("=")[0]
        if option in _PYTEST_NO_RUN_OPTIONS:
            return True
    return False


def _windows_tokens_are_static_validation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tokens = _windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable == "git":
        return bool(args and args[0] == "diff" and "--check" in args)
    if executable in {"node", "nodejs"}:
        return bool(args and args[0] in {"-c", "--check"})
    if executable in {"eslint"}:
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        command_args = args[1:] if args[:1] == ["run"] else args
        return bool(
            command_args
            and (
                command_args[0] == "lint"
                or command_args[0].startswith("lint:")
                or command_args[0].startswith(("type-check", "typecheck"))
            )
        )
    if executable == "prettier":
        return "--check" in args
    if executable == "tsc":
        return "--noemit" in args
    python_action = _windows_python_action(tokens)
    if python_action is not None and python_action[0] == "module":
        return python_action[1].casefold() in {"compileall", "json.tool", "py_compile"}
    return False


def _windows_tokens_are_behavioral_validation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    tokens = _windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable == "pytest":
        return not _pytest_args_request_no_test_execution(tokens[1:])
    if executable in {"ava", "cypress", "jest", "mocha", "playwright", "rspec", "tap", "tox", "vitest"}:
        return True
    if executable in {"npm", "pnpm", "yarn"}:
        command_args = args[1:] if args[:1] == ["run"] else args
        return bool(command_args and (command_args[0] == "test" or command_args[0].startswith("test:")))
    if executable in {"node", "nodejs"}:
        return "--test" in args
    python_action = _windows_python_action(tokens)
    if python_action is not None and python_action[0] == "module":
        module = python_action[1].casefold()
        if module == "pytest":
            return not _pytest_args_request_no_test_execution(python_action[2])
        return module in {"nose", "nose2", "tox", "unittest"}
    if executable in {"cargo", "dotnet", "go", "gradle", "make", "mvn", "swift"}:
        return bool(args and args[0] == "test")
    return _windows_tokens_execute_script(tokens, require_test_name=True)


def _windows_tokens_execute_script(tokens: list[str], *, require_test_name: bool = False) -> bool:
    if not tokens:
        return False
    tokens = _windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    script: str | None = None
    python_action = _windows_python_action(tokens)
    if python_action is not None and python_action[0] == "script":
        script = python_action[1]
    elif executable in {"node", "nodejs", "ruby"} and len(tokens) > 1:
        non_options = [token for token in tokens[1:] if not token.startswith("-")]
        if non_options:
            script = non_options[0]
    elif executable.endswith((".js", ".mjs", ".cjs", ".py", ".ps1", ".rb")):
        script = executable
    if not script:
        return False
    normalized = script.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if not require_test_name:
        return True
    stem = normalized.rsplit(".", 1)[0]
    return bool(re.search(r"(^|[._-])tests?([._-]|$)", stem))


def _windows_tokens_are_read_only_inspection(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if _windows_tokens_are_git_inspection(tokens):
        return True
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    if executable in {"cat", "get-content", "head", "tail", "type", "wc"}:
        return bool(args)
    if executable in {"dir", "get-childitem", "ls", "pwd", "get-location"}:
        return not any(arg in {"-recurse", "/s"} for arg in args)
    if executable in {"grep", "rg", "select-string"}:
        return len(args) >= 2
    if executable == "find":
        return not any(arg in {"-delete", "-exec", "-execdir"} for arg in args)
    return False


def _windows_tokens_are_behavior_demo(tokens: list[str], *, payload: str, changed_paths: list[str]) -> bool:
    if not tokens:
        return False
    tokens = _windows_effective_tool_tokens(tokens)
    executable = tokens[0]
    args = [token.casefold() for token in tokens[1:]]
    python_action = _windows_python_action(tokens)
    if python_action is not None and python_action[0] == "command":
        return True
    if executable in {"node", "nodejs", "ruby"} and any(flag in args for flag in {"-c", "-e"}):
        return True
    if _windows_tokens_execute_script(tokens):
        return True
    lowered = payload.casefold()
    if re.search(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)", lowered):
        return True
    normalized_payload = lowered.replace("\\", "/")
    return any(
        path.replace("\\", "/").lstrip("./").casefold() in normalized_payload
        for path in changed_paths
        if path and not _is_internal_runtime_path(path, project_root=None, task_path=None)
    )


def _canonical_restart_command(command: str) -> str:
    current = _normalize_command(command)
    for _ in range(6):
        wrapper = windows_shell_wrapper_payload(current)
        if wrapper is not None:
            _shell_kind, payload, _problem = wrapper
            if payload:
                nested = _normalize_command(payload)
                if nested and nested != current:
                    current = nested
                    continue
            break
        try:
            parts = shlex.split(current)
        except ValueError:
            break
        if len(parts) < 3 or Path(parts[0]).name not in _RESTART_SHELL_NAMES:
            break
        command_index = next(
            (
                index + 1
                for index, token in enumerate(parts[1:-1], start=1)
                if token.startswith("-")
                and not token.startswith("--")
                and "c" in token[1:]
            ),
            None,
        )
        if command_index is None or command_index != len(parts) - 1:
            break
        nested = _normalize_command(parts[command_index])
        if not nested or nested == current:
            break
        current = nested
    return current


def _runtime_unresolved_execution_key(command: str, cwd: str | None) -> str:
    payload = {
        "command": _canonical_restart_command(command),
        "cwd": cwd or "",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"unresolved-execution:{digest[:16]}"


def _runtime_validation_restart_issue(validation: ValidationRun) -> RuntimeRestartIssue | None:
    if validation.trusted_validation_outcome == "passed":
        return None
    if validation.exit_code is None or validation.shell_exit_code is None:
        return RuntimeRestartIssue(
            key=_runtime_unresolved_execution_key(validation.command, validation.cwd),
            sequence=validation.sequence,
            validation_id=validation.validation_id,
        )
    if validation.trusted_validation_outcome != "failed":
        return None
    evidence = validation.captured_output or validation.summary
    normalized_evidence = " ".join(evidence.split())
    payload = {
        "validation_id": validation.validation_id,
        "exit_code": validation.exit_code,
        "shell_exit_code": validation.shell_exit_code,
        "executed_test_names": sorted(validation.executed_test_names),
        "executed_test_files": sorted(validation.executed_test_files),
        "failed_count": validation.failed_count,
        "evidence_sha256": hashlib.sha256(normalized_evidence.encode("utf-8")).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return RuntimeRestartIssue(
        key=f"failed-validation:{validation.validation_id}:{digest}",
        sequence=validation.sequence,
        validation_id=validation.validation_id,
    )


def _matching_active_validation_issue(
    packet: SupervisorWakePacket,
    *,
    active_issue_key: str | None,
    active_issue_last_sequence: int,
) -> RuntimeRestartIssue | None:
    if active_issue_key is None:
        return None
    issues = [
        issue
        for validation in packet.validations
        if validation.sequence > active_issue_last_sequence
        if (issue := _runtime_validation_restart_issue(validation)) is not None
    ]
    if not issues:
        return None
    latest = max(issues, key=lambda issue: issue.sequence)
    return latest if latest.key == active_issue_key else None


def _runtime_event_issue_payload(
    packet: SupervisorWakePacket,
    *,
    reasons: tuple[str, ...],
) -> dict[str, Any]:
    action = packet.triggering_action
    approval = packet.approval_context
    payload: dict[str, Any] = {"reasons": reasons}
    if approval is not None:
        payload["approval"] = {
            "request_type": str(approval.request_type),
            "command": (
                _canonical_restart_command(approval.command) if approval.command else None
            ),
            "cwd": approval.cwd,
            "paths": sorted(approval.paths),
        }
        return payload
    if action is not None and action.command:
        payload["command"] = {
            "kind": action.kind,
            "command": _canonical_restart_command(action.command),
            "cwd": action.cwd,
            "exit_code": action.exit_code,
        }
        return payload
    changed_paths = sorted({changed.path for changed in packet.changed_files})
    if not changed_paths and action is not None:
        changed_paths = sorted(action.paths)
    payload["paths"] = changed_paths
    if not reasons and action is not None:
        payload["kind"] = action.kind
    return payload


def _runtime_restart_issue(
    packet: SupervisorWakePacket,
    *,
    active_issue_key: str | None = None,
    active_issue_last_sequence: int = 0,
) -> RuntimeRestartIssue | None:
    validation = _runtime_triggering_validation(packet)
    if validation is not None:
        if validation.trusted_validation_outcome == "masked_or_unknown":
            # Backward-compatible schema values from older runs must not recreate the
            # retired masked-validation restart gate.
            return None
        issue = _runtime_validation_restart_issue(validation)
        if issue is not None:
            return issue

    reasons = tuple(sorted(_runtime_trigger_reasons_from_summary(packet.current_summary)))
    action = packet.triggering_action
    approval = packet.approval_context
    if action is None and approval is None and not reasons:
        if packet.current_summary.strip() != "Coder turn completed":
            return None
        return _matching_active_validation_issue(
            packet,
            active_issue_key=active_issue_key,
            active_issue_last_sequence=active_issue_last_sequence,
        )
    payload = _runtime_event_issue_payload(packet, reasons=reasons)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return RuntimeRestartIssue(key=f"runtime-event:{digest}", sequence=packet.latest_event_sequence)


def _runtime_triggering_validation(packet: SupervisorWakePacket) -> ValidationRun | None:
    if not packet.validations:
        return None
    action = packet.triggering_action
    if action is not None and action.command:
        normalized_command = _normalize_command(action.command)
        matches = [
            validation
            for validation in packet.validations
            if validation.normalized_command == normalized_command
        ]
        if matches:
            return max(matches, key=lambda validation: validation.sequence)
    validation_reasons = {
        "repeated_same_failing_validation",
        "validation_regression",
    }
    if validation_reasons & set(_runtime_trigger_reasons_from_summary(packet.current_summary)):
        return max(packet.validations, key=lambda validation: validation.sequence)
    return None


def _is_no_active_turn_to_steer_error(exc: AppServerError) -> bool:
    return "no active turn to steer" in str(exc).lower()


def _is_turn_already_inactive_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "already completed",
            "already interrupted",
            "no active turn",
            "turn is not active",
            "turn not found",
        )
    )


def _fallback_restart_handoff(*, task_contents: str, reason: str, last_actions: list[str]) -> RestartHandoff:
    objective = " ".join(task_contents.strip().split())[:1000] or "Continue the selected task."
    known_evidence = "; ".join(last_actions[-5:]) or "No completed coder actions are recorded."
    return RestartHandoff(
        objective=objective,
        restart_reason=reason,
        bad_pattern="The previous generation was interrupted or judged unreliable before completing the task.",
        known_evidence=known_evidence,
        next_step="Read the task, progress, decisions, and this handoff, then take the next concrete task step.",
        recovery_signal="The new generation makes task-relevant progress without repeating the prior failure mode.",
    )


def _restart_rejection_steering(handoff: RestartHandoff | None) -> str:
    if handoff is None:
        return "Continue the current task. Use the latest observation to make the next concrete progress step."
    return (
        f"Correct the current non-converging pattern before continuing. Avoid: {handoff.bad_pattern} "
        f"Next step: {handoff.next_step} Recovery signal: {handoff.recovery_signal}"
    )


def _triggering_action_from_item(item: Any, *, item_id: str | None, summary: str) -> TriggeringAction:
    if not isinstance(item, dict):
        return TriggeringAction(item_id=item_id, kind="item", status="completed", summary=summary)
    kind = str(item.get("type") or "item")
    exit_code = item.get("exitCode")
    return TriggeringAction(
        item_id=item_id,
        kind=kind,
        command=item.get("command") if isinstance(item.get("command"), str) else None,
        cwd=item.get("cwd") if isinstance(item.get("cwd"), str) else None,
        paths=_paths_from_item(item),
        exit_code=exit_code if isinstance(exit_code, int) else None,
        status=item.get("status") if isinstance(item.get("status"), str) else "completed",
        timed_out=_item_explicitly_timed_out(item),
        summary=summary,
    )


def _item_explicitly_timed_out(item: dict[str, Any]) -> bool:
    if item.get("timedOut") is True or item.get("timed_out") is True:
        return True
    explicit_statuses = {
        str(item.get(key) or "").strip().lower().replace("_", "")
        for key in ("status", "terminationReason", "termination_reason", "errorType", "error_type")
    }
    return bool({"timeout", "timedout"} & explicit_statuses)


def _validation_from_action(
    action: TriggeringAction,
    *,
    sequence: int,
    item: Any = None,
    changed_paths: list[str] | None = None,
) -> ValidationRun | None:
    if action.kind != "commandExecution" or not action.command:
        return None
    validation_type = _classify_validation_command(action.command, changed_paths=changed_paths or [])
    if validation_type is None:
        return None
    output = _command_output_from_item(item)
    normalized_command = _normalize_command(action.command)
    raw_selector = _raw_validation_selector(action.command)
    executed_test_names = _executed_test_names(action.command, output)
    executed_test_files = _test_files_from_output(output)
    outcome = "pass" if action.exit_code == 0 else "fail"
    if validation_type == "behavioral" and outcome == "pass" and not _tests_executed(action.command, output):
        outcome = "fail"
    trusted_outcome = "passed" if outcome == "pass" else "failed"
    passed = outcome == "pass"
    passed_count, failed_count = _test_count_summary(output)
    # Trust the test runner's factual result over the enclosing shell status. This preserves
    # real failures without reviving the removed shell-shape/masking classifier.
    if validation_type == "behavioral" and failed_count is not None and failed_count > 0:
        outcome = "fail"
        trusted_outcome = "failed"
        passed = False
    summary = _validation_summary(action.summary, output)
    return ValidationRun(
        validation_id=_stable_validation_id(
            normalized_command=normalized_command,
            cwd=action.cwd,
            validation_type=validation_type,
            raw_selector=raw_selector,
            executed_test_names=executed_test_names,
        ),
        command=action.command,
        raw_command=action.command,
        normalized_command=normalized_command,
        cwd=action.cwd,
        exit_code=action.exit_code,
        shell_exit_code=action.exit_code,
        type=validation_type,
        outcome=outcome,
        passed=passed,
        trusted_validation_outcome=trusted_outcome,
        masking_reason=None,
        summary=summary,
        captured_output=output,
        captured_output_truncated=output.endswith("...<truncated>"),
        sequence=sequence,
        was_filtered=_command_was_filtered(action.command),
        raw_selector=raw_selector,
        executed_test_names=executed_test_names,
        executed_test_files=executed_test_files,
        passed_count=passed_count,
        failed_count=failed_count,
        target_files_or_test_files=_target_files_or_test_files(action.command),
    )


def _inspection_from_action(
    action: TriggeringAction,
    *,
    sequence: int,
    item: Any = None,
) -> InspectionRun | None:
    if action.kind != "commandExecution" or not action.command:
        return None
    if not _is_read_only_inspection_command(action.command):
        return None
    output = _command_output_from_item(item)
    normalized_command = _normalize_command(action.command)
    inspected_paths = _inspected_paths_from_command(action.command)
    outcome = "pass" if _inspection_exit_is_usable(action.command, action.exit_code) else "fail"
    summary = _validation_summary(action.summary, output)
    return InspectionRun(
        inspection_id=_stable_inspection_id(
            normalized_command=normalized_command,
            cwd=action.cwd,
            inspected_paths=inspected_paths,
        ),
        command=action.command,
        raw_command=action.command,
        normalized_command=normalized_command,
        cwd=action.cwd,
        exit_code=action.exit_code,
        shell_exit_code=action.exit_code,
        outcome=outcome,
        passed=outcome == "pass",
        summary=summary,
        captured_output=output,
        captured_output_truncated=output.endswith("...<truncated>"),
        sequence=sequence,
        inspected_paths=inspected_paths,
    )


def _classify_validation_command(command: str, *, changed_paths: list[str]) -> str | None:
    if _is_git_inspection_command(command):
        return "static" if _is_git_diff_check_command(command) else None
    if _is_read_only_inspection_command(command):
        return None
    if _is_static_validation_command(command):
        return "static"
    if _is_behavioral_validation_command(command):
        return "behavioral"
    if _is_behavior_demo_command(command, changed_paths=changed_paths):
        return "behavior_demo"
    return None


def _is_static_validation_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and _windows_tokens_are_static_validation(windows_tokens))
    inner = _shell_command_payload(command)
    if inner is not None and inner != command:
        return _is_static_validation_command(inner)
    lowered = command.lower()
    executable_prefix = r"(^|[\s;&|()'\"])(?:npx\s+|(?:\.{0,2}/|/)?(?:[\w.-]+/)*)"
    node_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*node(?:js)?"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    patterns = (
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+-c(\s|$)",
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+--check(\s|$)",
        r"(^|[\s;&|()'\"])git\s+diff\s+--check(\s|$)",
        executable_prefix + r"eslint(\s|$)",
        r"(^|[\s;&|()'\"])(npm|pnpm|yarn)\s+(run\s+)?lint(\s|$|:)",
        r"(^|[\s;&|()'\"])(npm|pnpm|yarn)\s+(run\s+)?type-?check(\s|$|:)",
        executable_prefix + r"prettier\s+--check(\s|$)",
        executable_prefix + r"tsc(?:\s+[^;&|()]*)?\s+--noemit(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + r"\s+-m\s+(py_compile|compileall)(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + r"\s+-m\s+json\.tool(\s|$)",
        r"(^|[\s;&|()'\"])jq\s+['\"]?\.['\"]?(\s|$)",
        r"json\.parse\s*\(",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _is_git_inspection_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and _windows_tokens_are_git_inspection(windows_tokens))
    inner = _shell_command_payload(command)
    if inner is not None and inner != command:
        return _is_git_inspection_command(inner)
    lowered = command.lower()
    pattern = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*git\s+(diff|status|log|show|branch|remote|rev-parse|for-each-ref)\b"
    return bool(re.search(pattern, lowered))


def _is_git_diff_check_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(
            windows_tokens
            and _windows_tokens_are_git_inspection(windows_tokens)
            and len(windows_tokens) > 1
            and windows_tokens[1].casefold() == "diff"
            and "--check" in (token.casefold() for token in windows_tokens[2:])
        )
    inner = _shell_command_payload(command)
    if inner is not None and inner != command:
        return _is_git_diff_check_command(inner)
    lowered = command.lower()
    pattern = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*git\s+diff(?:\s+[^;&|()'\"]+)*\s+--check(\s|$)"
    return bool(re.search(pattern, lowered))


def _is_read_only_inspection_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and _windows_tokens_are_read_only_inspection(windows_tokens))
    inner = _shell_command_payload(command)
    if inner is not None and inner != command:
        return _is_read_only_inspection_command(inner)
    lowered = command.lower()
    if any(marker in lowered for marker in ("<<", "$(", "`")):
        return False
    if re.search(r"(?<![12])>(?!&)", command) or re.search(r"(^|[^<])<(?!<)", command):
        return False
    segments = _inspection_command_segments(command)
    if segments is None:
        return False
    if not segments:
        return False
    return all(_is_read_only_inspection_tokens(segment) for segment in segments)


def _inspection_command_segments(command: str) -> list[list[str]] | None:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return [windows_tokens] if windows_tokens else None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = [token for token in lexer if token]
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in {"&&", ";", "|"}:
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        if token in {"&"} or any(char in token for char in "<>"):
            return None
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _shell_command_payload(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    tokens = _strip_env_command_prefix(tokens)
    if len(tokens) < 3:
        return None
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    if executable not in {"bash", "sh", "zsh"}:
        return None
    for index, token in enumerate(tokens[1:], start=1):
        if not token.startswith("-"):
            continue
        if "c" not in token[1:]:
            continue
        if index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _strip_env_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
        remaining = remaining[1:]
    if not remaining:
        return remaining
    executable = remaining[0].rsplit("/", 1)[-1].lower()
    if executable != "env":
        return remaining
    remaining = remaining[1:]
    while remaining:
        token = remaining[0]
        if token == "--":
            remaining = remaining[1:]
            break
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", token):
            remaining = remaining[1:]
            continue
        if token.startswith("-"):
            remaining = remaining[1:]
            continue
        break
    return remaining


def _is_read_only_inspection_segment(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False
    return _is_read_only_inspection_tokens([token for token in tokens if token])


def _is_read_only_inspection_tokens(tokens: list[str]) -> bool:
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return False
    executable = tokens[0].rsplit("/", 1)[-1].lower()
    args = [token.lower() for token in tokens[1:]]
    if executable == "git":
        return bool(args) and args[0] in {"diff", "status", "log", "show", "branch", "remote", "rev-parse", "for-each-ref"}
    if executable in {"cat", "sed", "grep", "egrep", "fgrep", "rg", "head", "tail", "nl", "ls", "wc", "pwd", "stat", "file", "find"}:
        if executable == "find" and any(arg in {"-delete", "-exec", "-execdir"} for arg in args):
            return False
        return True
    return False


def _is_behavioral_validation_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and _windows_tokens_are_behavioral_validation(windows_tokens))
    inner = _shell_command_payload(command)
    if inner is not None and inner != command:
        return _is_behavioral_validation_command(inner)
    lowered = command.lower()
    executable_prefix = r"(^|[\s;&|()'\"])(?:npx\s+|(?:\.{0,2}/|/)?(?:[\w.-]+/)*)"
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    node_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*node(?:js)?"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    patterns = (
        executable_prefix + r"mocha(\s|$)",
        r"(^|[\s;&|()'\"])(npm|pnpm|yarn)\s+(run\s+)?test(\s|$|:)",
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+--test(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+-m\s+(pytest|unittest|tox|nose2?)($|[\s;&|()'\"])",
        executable_prefix + r"(jest|ava|tap|vitest|playwright|cypress|pytest|tox|rspec)(\s|$)",
        executable_prefix + r"(go|cargo|mvn|gradle|swift|dotnet|make)\s+test(\s|$)",
    )
    return any(re.search(pattern, lowered) for pattern in patterns) or _is_test_wrapper_script_command(command)


def _is_test_wrapper_script_command(command: str) -> bool:
    lowered = command.lower()
    boundary = r"(?=$|[\s;&|()'\"])"
    test_script_basename = r"(?:tests?(?:[._-][\w.-]+)*|[\w.-]+[._-]tests?(?:[._-][\w.-]+)*)"
    script_with_test_token = (
        r"(?:\.{1,2}/|/)?(?:[\w.-]+/)*" + test_script_basename + r"\.(py|js|mjs|cjs|rb|sh)"
    )
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:python(?:3(?:\.\d+)?)?|node(?:js)?|ruby|bash|sh)"
    shell_prefix = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:bash|sh|zsh)"
    patterns = (
        r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+(?!-)" + script_with_test_token + boundary,
        r"(^|[\s;&|()'\"])" + script_with_test_token + boundary,
        shell_prefix + r"\s+-[a-z]*c\s+['\"]?" + script_with_test_token + boundary,
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _is_direct_script_execution_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(windows_tokens and _windows_tokens_execute_script(windows_tokens))
    lowered = command.lower()
    boundary = r"(?=$|[\s;&|()'\"])"
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:node(?:js)?|ruby|bash|sh)"
    shell_prefix = r"(^|[\s;&|()'\"])(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:bash|sh|zsh)"
    patterns = (
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+(?!-)[\w./-]+\.py" + boundary,
        r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+(?!-)[\w./-]+\.(js|mjs|cjs|rb|sh)" + boundary,
        r"(^|[\s;&|()'\"])(?:\.{1,2}/|/)[\w./-]+\.(py|js|mjs|cjs|rb|sh)" + boundary,
        shell_prefix + r"\s+-[a-z]*c\s+['\"]?(?!-)[\w./-]+\.(py|js|mjs|cjs|rb|sh)" + boundary,
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _is_behavior_demo_command(command: str, *, changed_paths: list[str]) -> bool:
    windows_surface, windows_tokens, payload = _windows_classification_tokens(command)
    if windows_surface:
        return bool(
            windows_tokens
            and payload
            and _windows_tokens_are_behavior_demo(
                windows_tokens,
                payload=payload,
                changed_paths=changed_paths,
            )
        )
    lowered = command.lower()
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    node_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*node(?:js)?"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    ruby_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*ruby"
    inline_patterns = (
        r"(^|[\s;&|()'\"])" + node_exec + r"\s+-e(\s|$)",
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+-c(\s|$)",
        r"(^|[\s;&|()'\"])" + ruby_exec + r"\s+-e(\s|$)",
    )
    http_patterns = (
        r"(^|[\s;&|()'\"])(curl|wget|http|https)\s+",
        r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)",
    )
    return (
        (_has_behavior_demo_marker(command) and _marked_behavior_demo_command_is_plausible(command, changed_paths))
        or _is_direct_script_execution_command(command)
        or _is_stdin_script_demo_command(command)
        or _command_requires_changed_module(command, changed_paths)
        or any(re.search(pattern, lowered) for pattern in inline_patterns)
        or any(re.search(pattern, lowered) for pattern in http_patterns)
    )


def _has_behavior_demo_marker(command: str) -> bool:
    return bool(re.search(r"\bBELLO_BEHAVIOR_DEMO\s*=\s*(?:1|true|yes)\b", command, re.IGNORECASE))


def _marked_behavior_demo_command_is_plausible(command: str, changed_paths: list[str]) -> bool:
    if _is_read_only_inspection_command(command):
        return False
    if _is_observationless_output_command(command):
        return False
    if _is_direct_script_execution_command(command) or _is_stdin_script_demo_command(command):
        return True
    if _command_requires_changed_module(command, changed_paths):
        return True
    lowered = command.lower()
    if re.search(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[?::1\]?)", lowered):
        return True
    normalized_command = lowered.replace("\\", "/")
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/").lstrip("./").lower()
        if not path or _is_internal_runtime_path(path, project_root=None, task_path=None):
            continue
        name = path.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if path in normalized_command or (stem and len(stem) >= 3 and stem in normalized_command):
            return True
    return bool(re.search(r"(^|[\s;&|()'\"])(?:\.{1,2}/|/)[\w./-]+(?:\s|$)", lowered))


def _is_observationless_output_command(command: str) -> bool:
    windows_surface, windows_tokens, _payload = _windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens:
            return False
        return windows_tokens[0] in {
            "cat",
            "echo",
            "false",
            "get-content",
            "head",
            "ls",
            "printf",
            "pwd",
            "rg",
            "tail",
            "true",
            "type",
            "wc",
            "yes",
        }
    segments = [segment.strip() for segment in re.split(r"\s*(?:&&|;|\|)\s*", command) if segment.strip()]
    if not segments:
        return False
    output_only = {"echo", "printf", "true", "false", "yes"}
    read_only_excerpt = {"cat", "sed", "grep", "egrep", "fgrep", "rg", "head", "tail", "nl", "ls", "wc", "pwd"}
    seen_executable = False
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        executable = tokens[0].rsplit("/", 1)[-1].lower()
        seen_executable = True
        if executable not in output_only and executable not in read_only_excerpt:
            return False
    return seen_executable


def _is_stdin_script_demo_command(command: str) -> bool:
    lowered = command.lower()
    if "<<" not in lowered:
        return False
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    python_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*python(?:3(?:\.\d+)?)?"
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:node(?:js)?|ruby|bash|sh|zsh)"
    patterns = (
        r"(^|[\s;&|()'\"])" + python_exec + python_flags + r"*\s+-?\s*<<",
        r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+-?\s*<<",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _command_requires_changed_module(command: str, changed_paths: list[str]) -> bool:
    lowered = command.lower()
    interpreter_exec = r"(?:\.{0,2}/|/)?(?:[\w.-]+/)*(?:node(?:js)?|python(?:3(?:\.\d+)?)?|ruby)"
    if not re.search(r"(^|[\s;&|()'\"])" + interpreter_exec + r"\s+(-e|-c|\S+)", lowered):
        return False
    if not re.search(r"\b(require|import|node|nodejs|python|python3|ruby)\b", lowered):
        return False
    normalized_command = lowered.replace("\\", "/")
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/").lstrip("./").lower()
        if not path or _is_internal_runtime_path(path, project_root=None, task_path=None):
            continue
        candidates = {path}
        if path.endswith((".js", ".ts", ".jsx", ".tsx", ".py", ".rb")):
            candidates.add(path.rsplit(".", 1)[0])
        if any(candidate and candidate in normalized_command for candidate in candidates):
            return True
    return False


def _tests_executed(command: str, output: str) -> bool:
    if not _is_behavioral_validation_command(command):
        return True
    lowered = output.lower()
    zero_test_patterns = (
        r"\b0\s+(passing|failing|pending|tests?|specs?)\b",
        r"\b0\s+tests?\s+(run|executed|passed|failed|total)\b",
        r"\btests?:\s+0\s+total\b",
        r"\btest suites?:\s+0\b",
        r"\bran\s+0\s+tests?\b",
        r"\bno tests?\s+(found|run|executed)\b",
    )
    return not any(re.search(pattern, lowered) for pattern in zero_test_patterns)


def _command_output_from_item(item: Any, *, limit: int = 20000) -> str:
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    _collect_output_strings(item, parts, depth=0)
    return _bounded_text("\n".join(parts), limit=limit)


def _item_with_recorded_output(item: Any, output: str) -> Any:
    if not output or not isinstance(item, dict):
        return item
    existing = _command_output_from_item(item)
    if existing.strip() == output.strip():
        merged = existing
    else:
        merged = output if not existing else f"{existing}\n{output}"
    enriched = dict(item)
    enriched["output"] = merged
    return enriched


def _output_delta_text(params: dict[str, Any], *, limit: int = 20000) -> str:
    parts: list[str] = []
    _collect_output_delta_strings(params, parts, depth=0)
    return _bounded_text("".join(parts), limit=limit)


def _collect_output_delta_strings(value: Any, parts: list[str], *, depth: int) -> None:
    if depth > 5:
        return
    if isinstance(value, str):
        if value:
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_output_delta_strings(item, parts, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        key_text = str(key).lower()
        if key_text in {
            "delta",
            "output",
            "outputtext",
            "aggregatedoutput",
            "aggregated_output",
            "combinedoutput",
            "combined_output",
            "stdout",
            "stdouttext",
            "stdout_text",
            "stderr",
            "stderrtext",
            "stderr_text",
            "text",
            "content",
            "message",
            "chunk",
            "data",
        }:
            _collect_output_delta_strings(nested, parts, depth=depth + 1)
        elif key_text in {"outputs", "chunks", "lines", "items"}:
            _collect_output_delta_strings(nested, parts, depth=depth + 1)


def _validation_summary(summary: str, output: str, *, limit: int = 4000) -> str:
    stripped = output.strip()
    if not stripped:
        return summary
    if stripped in summary:
        return summary
    return _bounded_text(f"{summary}\nOutput:\n{stripped}", limit=limit)


def _validation_id(sequence: int) -> str:
    return f"validation-{sequence}"


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def _stable_validation_id(
    *,
    normalized_command: str,
    cwd: str | None,
    validation_type: str,
    raw_selector: str | None,
    executed_test_names: list[str],
) -> str:
    payload = {
        "normalized_command": normalized_command,
        "cwd": cwd or "",
        "validation_type": validation_type,
        "raw_selector": raw_selector or "",
        "executed_test_names": sorted(dict.fromkeys(executed_test_names)),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"validation-{digest[:16]}"


def _stable_inspection_id(
    *,
    normalized_command: str,
    cwd: str | None,
    inspected_paths: list[str],
) -> str:
    payload = {
        "normalized_command": normalized_command,
        "cwd": cwd or "",
        "inspected_paths": sorted(dict.fromkeys(inspected_paths)),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"inspection-{digest[:16]}"


def _inspection_exit_is_usable(command: str, exit_code: int | None) -> bool:
    if exit_code == 0:
        return True
    if exit_code == 1 and re.search(r"(^|[\s;&|()'\"])(?:rg|grep|egrep|fgrep)\b", command.lower()):
        return True
    return False


def _command_was_filtered(command: str) -> bool:
    return _raw_validation_selector(command) is not None


def _raw_validation_selector(command: str) -> str | None:
    windows_surface, windows_tokens, payload = _windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return None
        command = payload.replace("\\", "/")
    selectors: list[str] = []
    patterns = (
        r"(?:^|\s)(-k)\s+([^\s;&|]+)",
        r"(?:^|\s)(-m)\s+([^\s;&|]+)",
        r"(?:^|\s)(--grep|--testNamePattern|--test-name-pattern|--filter|--test)\s+([^\s;&|]+)",
        r"(?:^|\s)(-g)\s+([^\s;&|]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, command):
            if match.group(1) == "-m" and _is_python_module_flag(command, match.start(1)):
                continue
            selector = match.group(2).strip("\"'")
            selectors.append(f"{match.group(1)} {selector}")
    for target in _explicit_test_selectors(command):
        selectors.append(target)
    return "; ".join(dict.fromkeys(selectors)) or None


def _is_python_module_flag(command: str, start: int) -> bool:
    prefix = command[:start].rstrip().lower()
    python_flags = r"(?:\s+-(?!m(?:\s|$))[a-z][\w-]*(?:=[^\s;&|()'\"]+)?)"
    pattern = r"(^|[\s;&|()'\"])(python|python3)" + python_flags + r"*$"
    return bool(re.search(pattern, prefix))


def _explicit_test_selectors(command: str, *, limit: int = 50) -> list[str]:
    selectors: list[str] = []
    for match in re.finditer(
        r"(?<![\w./-])(?:\.?/)?[\w./-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php)(?:::[\w.*\[\]-]+)+",
        command,
    ):
        selectors.append(match.group(0).strip("'\"").lstrip("./"))
        if len(selectors) >= limit:
            break
    return list(dict.fromkeys(selectors))


def _executed_test_names(command: str, output: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    names.extend(_explicit_test_selectors(command, limit=limit))
    names.extend(_test_names_from_output(output, limit=limit))
    if not names and _is_behavioral_validation_command(command):
        names.extend(_target_files_or_test_files(command))
    return list(dict.fromkeys(names))[:limit]


def _test_names_from_output(output: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    patterns = (
        r"(?m)\b([\w./+\[\]-]+::test_[\w.\[\]-]+)\b",
        r"(?m)\b(test_[A-Za-z0-9_]+)\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS)\b",
        r"(?m)\b(?:✓|PASS|FAIL)\s+([^()\n]{3,160})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, output):
            name = " ".join(match.group(1).strip().split())
            if name:
                names.append(name)
            if len(names) >= limit:
                return list(dict.fromkeys(names))
    return list(dict.fromkeys(names))


def _test_files_from_output(output: str, *, limit: int = 100) -> list[str]:
    files: list[str] = []
    runner_patterns = (
        # Jest/Vitest style suite lines. Prefer these over the broad fallback so stack traces
        # through test helpers do not look like independently executed test files.
        r"(?m)^\s*(?:PASS|FAIL)\s+((?:\.{0,2}/)?[\w@+./-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php|vue|svelte|snap|snapshot|golden))\b",
        # Pytest verbose output.
        r"(?m)^\s*((?:\.{0,2}/)?[\w@+./-]+\.py)::[^\s]+\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b",
    )
    for pattern in runner_patterns:
        for match in re.finditer(pattern, output):
            path = _normalize_output_test_path(match.group(1))
            if path and _file_kind(path) == "test":
                files.append(path)
            if len(files) >= limit:
                return list(dict.fromkeys(files))[:limit]
    if files:
        return list(dict.fromkeys(files))[:limit]

    path_pattern = re.compile(
        r"(?<![\w./-])((?:\.{0,2}/)?[\w@+./-]*(?:test|spec|tests|__tests__|snapshots|__snapshots__|golden|goldens)"
        r"[\w@+./-]*\.(?:py|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php|snap|snapshot|golden))"
        r"(?:::[\w.*\[\]-]+)?",
        re.IGNORECASE,
    )
    for match in path_pattern.finditer(output):
        path = _normalize_output_test_path(match.group(1))
        if path and _file_kind(path) == "test":
            files.append(path)
        if len(files) >= limit:
            break
    return list(dict.fromkeys(files))[:limit]


def _normalize_output_test_path(path: str) -> str:
    normalized = path.strip().strip("'\"`.,;:()[]{}<>")
    if "::" in normalized:
        normalized = normalized.split("::", 1)[0]
    return normalized.replace("\\", "/").lstrip("./")


def _test_count_summary(output: str) -> tuple[int | None, int | None]:
    lowered = output.lower()
    passed = _first_int_match(
        lowered,
        (
            r"\b(\d+)\s+passed\b",
            r"\b(\d+)\s+passing\b",
            r"\bpasses:\s*(\d+)\b",
            r"\btests?:\s*(\d+)\s+passed\b",
        ),
    )
    failed = _first_int_match(
        lowered,
        (
            r"\b(\d+)\s+failed\b",
            r"\b(\d+)\s+failing\b",
            r"\bfailures?:\s*(\d+)\b",
            r"\btests?:\s*\d+\s+passed,\s*(\d+)\s+failed\b",
        ),
    )
    if passed is not None and failed is None:
        failed = 0
    return passed, failed


def _first_int_match(text: str, patterns: tuple[str, ...]) -> int | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _collect_output_strings(value: Any, parts: list[str], *, depth: int) -> None:
    if depth > 4:
        return
    if isinstance(value, str):
        if value.strip():
            parts.append(value)
        return
    if isinstance(value, list):
        for item in value:
            _collect_output_strings(item, parts, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for key, nested in value.items():
        key_text = str(key).lower()
        if key_text in {
            "output",
            "outputtext",
            "aggregatedoutput",
            "aggregated_output",
            "combinedoutput",
            "combined_output",
            "stdout",
            "stdouttext",
            "stdout_text",
            "stderr",
            "stderrtext",
            "stderr_text",
            "text",
            "content",
            "message",
            "summary",
        }:
            _collect_output_strings(nested, parts, depth=depth + 1)
        elif key_text in {"outputs", "chunks", "lines", "items", "result", "results"}:
            _collect_output_strings(nested, parts, depth=depth + 1)


def _has_passing_behavioral_validation(validations: list[ValidationRun]) -> bool:
    return any(_validation_is_usable_behavioral_pass(validation) for validation in validations)


def _is_behavior_proving_validation(validation: ValidationRun) -> bool:
    return validation.type in {"behavioral", "behavior_demo"}


def _validation_is_usable_behavioral_pass(validation: ValidationRun) -> bool:
    if (
        not _is_behavior_proving_validation(validation)
        or validation.outcome != "pass"
        or not validation.passed
        or validation.trusted_validation_outcome != "passed"
    ):
        return False
    if validation.type != "behavior_demo":
        return True
    return _validation_output_kind(
        validation,
        captured_output_present=bool(validation.captured_output.strip()),
    ) == "factual_observation_candidate"


def _has_readiness_marker(text: str) -> bool:
    return bool(READINESS_MARKER_RE.search(text.strip()))


def _is_recoverable_app_server_transport_error(message: str) -> bool:
    normalized = " ".join(message.lower().split())
    return any(
        marker in normalized
        for marker in (
            "app-server stream closed",
            "broken pipe",
            "connection reset",
            "connection closed",
            "unexpected eof",
            "end of file",
        )
    )


def _thread_turn_by_id(
    thread: dict[str, Any],
    turn_id: str | None,
) -> dict[str, Any] | None:
    if not turn_id:
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return turn
    return None


def _has_malformed_readiness_marker(text: str) -> bool:
    if _has_readiness_marker(text):
        return False
    if _readiness_reference_is_negated(text):
        return False
    lowered = text.lower()
    compact = re.sub(r"[\s_\-]+", "_", lowered)
    return any(
        marker in lowered or marker in compact
        for marker in (
            "bello ready for review",
            "bello_ready",
            "bello_ready_for_review",
            "ready_for_review",
        )
    )


def _readiness_reference_is_negated(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    marker = r"(?:bello[\s_`'\-]*ready[\s_`'\-]*for[\s_`'\-]*review|ready[\s_`'\-]*for[\s_`'\-]*review|readiness marker)"
    negator = r"(?:do not|don't|not|cannot|can't|will not|won't|without|no)"
    return bool(re.search(rf"\b{negator}\b.{{0,120}}\b{marker}\b", lowered))


def _appears_to_claim_readiness(text: str) -> bool:
    if _readiness_reference_is_negated(text):
        return False
    lowered = " ".join(text.lower().split())
    phrases = (
        "done",
        "complete",
        "completed",
        "finished",
        "implemented",
        "all tests pass",
        "all tests passed",
        "ready for review",
        "task is complete",
        "validation:",
    )
    return any(phrase in lowered for phrase in phrases)


def _normalized_surface_key(category: str) -> str:
    return re.sub(r"\s+", " ", category).strip().lower()


def _merge_behavior_surface_items(
    existing: list[dict[str, Any]],
    updates: list[BehaviorSurfaceItem],
) -> tuple[list[dict[str, Any]], bool]:
    """Upsert reviewer-returned surface entries into the stored list.

    Entries are never removed: a reviewer that judges an entry not actually required marks it
    status=out_of_scope with a note instead, so the audit trail of what was considered stays
    visible to later reviews.
    """
    merged: list[dict[str, Any]] = [
        dict(item) for item in existing if isinstance(item, dict) and str(item.get("category") or "").strip()
    ]
    index = {_normalized_surface_key(str(item.get("category") or "")): pos for pos, item in enumerate(merged)}
    changed = False
    for item in updates:
        category = (item.category or "").strip()
        if not category:
            continue
        key = _normalized_surface_key(category)
        pos = index.get(key)
        if pos is None:
            merged.append({"category": category, "status": item.status, "note": item.note})
            index[key] = len(merged) - 1
            changed = True
        elif merged[pos].get("status") != item.status or merged[pos].get("note") != item.note:
            merged[pos] = {**merged[pos], "status": item.status, "note": item.note}
            changed = True
    return merged, changed


def _completion_returns_this_generation(controller: Any, generation: int) -> int:
    return sum(
        1
        for record in getattr(controller, "completion_returns", []) or []
        if getattr(record, "generation", None) == generation
    )


def _prior_record_counts_as_health_intervention(record: Any) -> bool:
    reason = str(getattr(record, "reason", "") or "")
    return not reason.startswith("Completion review returned:")


def _latest_relevant_change_sequence(changed_files: list[ChangedFile]) -> int | None:
    sequences = [
        file.sequence
        for file in changed_files
        if file.sequence is not None and _is_relevant_changed_path(file.path, task_contents="")
    ]
    return max(sequences) if sequences else None


def _validation_freshness_summary(
    *,
    validations: list[ValidationRun],
    changed_files: list[ChangedFile],
) -> str:
    latest_change = _latest_relevant_change_sequence(changed_files)
    passing_behavioral = [
        validation.sequence
        for validation in validations
        if _validation_is_usable_behavioral_pass(validation)
    ]
    last_behavioral = max(passing_behavioral) if passing_behavioral else None
    if last_behavioral is None:
        if latest_change is None:
            return "No passing behavioral validation recorded; latest relevant change sequence is unknown."
        return f"No passing behavioral validation recorded after latest relevant change sequence {latest_change}."
    if latest_change is None:
        return (
            f"Last passing behavioral validation sequence {last_behavioral}; "
            "latest relevant change sequence is unknown."
        )
    freshness = "fresh" if last_behavioral >= latest_change else "stale"
    return (
        f"Last passing behavioral validation sequence {last_behavioral}; "
        f"latest relevant change sequence {latest_change}; behavioral validation is {freshness}."
    )


def _classify_supervisor_agent_error(error: BaseException) -> str:
    text = str(error).lower()
    if "did not produce an agent message" in text or "no agent message" in text:
        return "no_message"
    if "rate limit" in text or "rate_limit" in text or "429" in text:
        return "rate"
    if "auth" in text or "unauthorized" in text or "forbidden" in text or "api key" in text:
        return "auth"
    if "timed out" in text or "timeout" in text:
        return "tool_timeout"
    return "unknown"


def _validation_is_fresh_behavioral_pass(validation: ValidationRun, latest_change: int) -> bool:
    return _validation_is_usable_behavioral_pass(validation) and validation.sequence > latest_change


def _strip_test_path_extensions(name: str) -> str:
    stem = name
    suffixes = (
        ".snapshot",
        ".golden",
        ".snap",
        ".tsx",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".js",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".cs",
        ".php",
        ".vue",
        ".svelte",
        ".html",
        ".css",
        ".scss",
    )
    changed = True
    while changed:
        changed = False
        lowered = stem.lower()
        for suffix in suffixes:
            if lowered.endswith(suffix):
                stem = stem[: -len(suffix)]
                changed = True
                break
    return re.sub(r"(?i)(?:^|[._-])(test|tests|spec|specs|case|cases|snapshot|snap|golden|goldens)$", "", stem)


def _completion_return_summary(decision: CompletionReviewDecision) -> str:
    parts = [decision.reason]
    if decision.uncovered_behaviors:
        parts.append("uncovered=" + ", ".join(decision.uncovered_behaviors[:5]))
    if decision.validation_gaps:
        parts.append("validation_gaps=" + ", ".join(decision.validation_gaps[:5]))
    if decision.claim_evidence_mismatches:
        parts.append("mismatches=" + ", ".join(decision.claim_evidence_mismatches[:5]))
    if decision.packet_or_access_limitations:
        parts.append("limitations=" + ", ".join(decision.packet_or_access_limitations[:5]))
    return "; ".join(part for part in parts if part)


def _behavior_evidence_summary(decision: Any) -> list[str]:
    if not isinstance(decision, CompletionReviewDecision):
        return []
    return [
        f"{row.status}: {row.behavior}"
        + (f" ({len(row.evidence)} evidence item{'s' if len(row.evidence) != 1 else ''})" if row.evidence else "")
        for row in decision.behavior_evidence_matrix
    ]


def _files_reviewed_summary(decision: Any) -> list[str]:
    if not isinstance(decision, CompletionReviewDecision):
        return []
    return [
        f"{file.kind}: {file.path} ({'inspected' if file.inspected else 'not inspected'})"
        + (f" - {file.limitation}" if file.limitation else "")
        for file in decision.files_reviewed
    ]


def _normalize_review_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


@dataclass(frozen=True)
class _BoundedFileText:
    text: str
    truncated: bool


def _read_workspace_file(root: Path, path: str, *, limit: int) -> _BoundedFileText | None:
    try:
        candidate = (root / path).resolve()
    except OSError:
        return None
    if not ensure_relative_to(candidate, root):
        return None
    descriptor: int | None = None
    try:
        descriptor = _open_regular_file_no_follow(candidate)
        byte_limit = max(4, (limit + 1) * 4)
        data = bytearray()
        while len(data) < byte_limit:
            chunk = os.read(descriptor, min(1024 * 1024, byte_limit - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        bytes_truncated = os.fstat(descriptor).st_size > len(data)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    raw = bytes(data).decode("utf-8", errors="replace")
    bounded = _bounded_text(raw, limit=limit)
    return _BoundedFileText(text=bounded, truncated=bytes_truncated or len(raw) > len(bounded))


def _open_regular_file_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"not a regular file: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _file_kind(path: str) -> str:
    lowered = path.lower().replace("\\", "/")
    name = lowered.rsplit("/", 1)[-1]
    if (
        lowered.startswith("tests/")
        or lowered.startswith("test/")
        or lowered.startswith("fixtures/")
        or lowered.startswith("fixture/")
        or lowered.startswith("golden/")
        or lowered.startswith("goldens/")
        or lowered.startswith("snapshots/")
        or lowered.startswith("__snapshots__/")
        or "/tests/" in lowered
        or "/test/" in lowered
        or "/fixtures/" in lowered
        or "/fixture/" in lowered
        or "/golden/" in lowered
        or "/goldens/" in lowered
        or "/snapshots/" in lowered
        or "/__snapshots__/" in lowered
        or "/__tests__/" in lowered
        or "/spec/" in lowered
        or ".test." in name
        or ".spec." in name
        or ".snap." in name
        or ".snapshot." in name
        or ".golden." in name
        or re.search(r"(?:^|[._-])(test|tests|spec|specs|case|cases)(?:\.[^.]+)+$", name)
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_spec.rb")
        or name.endswith((".snap", ".snapshot", ".golden"))
    ):
        return "test"
    if (
        lowered.startswith(".github/workflows/")
        or lowered.startswith(".circleci/")
        or lowered.startswith(".buildkite/")
        or lowered.startswith("ci/")
        or lowered.startswith(".gitlab/")
        or name in {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile"}
    ):
        return "config"
    if name in {
        "package.json",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.ini",
        "tsconfig.json",
        "vitest.config.js",
        "vitest.config.ts",
        "jest.config.js",
        "jest.config.ts",
        "playwright.config.js",
        "playwright.config.ts",
    }:
        return "config"
    if lowered.endswith((".toml", ".yaml", ".yml", ".json", ".ini", ".cfg")):
        return "config"
    if lowered.endswith((".md", ".rst", ".txt", ".adoc")):
        return "docs"
    if lowered.endswith(
        (
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".mjs",
            ".cjs",
            ".rb",
            ".go",
            ".rs",
            ".java",
            ".kt",
            ".cs",
            ".php",
            ".swift",
            ".c",
            ".cc",
            ".cpp",
            ".h",
            ".hpp",
            ".css",
            ".scss",
            ".html",
            ".vue",
            ".svelte",
        )
    ):
        return "source"
    return "unknown"


def _is_relevant_changed_path(path: str, *, task_contents: str) -> bool:
    if _is_generated_or_cache_artifact_path(path, project_root=None):
        return False
    kind = _file_kind(path)
    if kind in {"source", "test", "config"}:
        return True
    if _is_suspicious_changed_path(path):
        return True
    if kind == "docs":
        return _task_is_docs_facing(task_contents)
    return False


def _is_suspicious_changed_path(path: str) -> bool:
    normalized = path.lower().replace("\\", "/").strip("/")
    name = normalized.rsplit("/", 1)[-1]
    if _file_kind(path) == "test":
        return True
    suspicious_parts = {
        "fixtures",
        "fixture",
        "golden",
        "goldens",
        "snapshots",
        "__snapshots__",
        "__fixtures__",
        "ci",
    }
    if set(normalized.split("/")) & suspicious_parts:
        return True
    if normalized.startswith((".github/workflows/", ".circleci/", ".buildkite/")):
        return True
    if name in {".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile"}:
        return True
    if any(marker in name for marker in (".snap", ".snapshot", ".golden")):
        return True
    return False


def _task_is_docs_facing(task_contents: str) -> bool:
    lowered = task_contents.lower()
    return any(token in lowered for token in ("documentation", "docs", "readme", ".md", "markdown", "docstring"))


def _read_task_text(task_path: Path) -> str:
    try:
        return task_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _ensure_internal_runtime_git_excluded(project_root: Path) -> None:
    git_dir = project_root / ".git"
    if not git_dir.is_dir():
        return
    info_dir = git_dir / "info"
    exclude_path = info_dir / "exclude"
    try:
        info_dir.mkdir(parents=True, exist_ok=True)
        current = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        entries = {line.strip() for line in current.splitlines()}
        additions = [entry for entry in (".supervisor/", ".supervisor") if entry not in entries]
        if additions:
            suffix = "" if current.endswith("\n") or not current else "\n"
            exclude_path.write_text(current + suffix + "\n".join(additions) + "\n", encoding="utf-8")
    except OSError:
        return


def _diff_line_counts(changed_files: list[ChangedFile]) -> tuple[int, int]:
    additions = sum(changed.additions or 0 for changed in changed_files)
    deletions = sum(changed.deletions or 0 for changed in changed_files)
    return additions, deletions


def _breadth_risk_summary(*, task_contents: str, changed_files: list[ChangedFile]) -> BreadthRiskSummary:
    task_lines = [line for line in task_contents.splitlines() if line.strip()]
    lowered = task_contents.lower()
    requirement_hint_count = sum(
        1
        for line in task_lines
        if re.search(
            r"\b(must|should|support|implement|handle|include|including|ensure|preserve|compatib|require|allow|prevent)\b",
            line,
            re.IGNORECASE,
        )
        or re.match(r"\s*[-*]\s+", line)
    )
    feature_terms = [
        term
        for term in BREADTH_FEATURE_TERMS
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}s?(?![A-Za-z0-9_])", lowered)
    ]
    additions, deletions = _diff_line_counts(changed_files)
    changed_source_files = [changed for changed in changed_files if _file_kind(changed.path) == "source"]
    changed_lines = additions + deletions
    flags: list[str] = []
    if len(task_contents) >= 2500 or len(task_lines) >= 45 or requirement_hint_count >= 10 or len(feature_terms) >= 10:
        flags.append("task_spec_appears_broad")
    if len(changed_source_files) >= 4 or changed_lines >= LARGE_DIFF_CHANGED_LINES_THRESHOLD:
        flags.append("implementation_diff_is_broad")
    if len(feature_terms) >= 8:
        flags.append("many_task_feature_terms")
    suggested_min = 0
    if flags:
        suggested_min = 6
        if len(task_contents) >= 6000 or requirement_hint_count >= 18 or len(feature_terms) >= 16:
            suggested_min = 8
    return BreadthRiskSummary(
        flags=flags,
        task_line_count=len(task_lines),
        requirement_hint_count=requirement_hint_count,
        task_feature_terms=feature_terms,
        changed_source_files_count=len(changed_source_files),
        changed_lines=changed_lines,
        suggested_min_behavior_rows=suggested_min,
    )


def _has_large_diff(changed_files: list[ChangedFile]) -> bool:
    additions, deletions = _diff_line_counts(changed_files)
    return (
        len(changed_files) >= LARGE_DIFF_CHANGED_FILES_THRESHOLD
        or additions + deletions >= LARGE_DIFF_CHANGED_LINES_THRESHOLD
    )


def _large_diff_signature(changed_files: list[ChangedFile]) -> str:
    payload = [
        {
            "path": changed.path,
            "status": changed.status,
            "additions": changed.additions,
            "deletions": changed.deletions,
            "sequence": changed.sequence,
        }
        for changed in sorted(changed_files, key=lambda item: item.path)
    ]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:16]


def _restart_budget_signature(health: HealthState, reason: str) -> str:
    payload = {
        "generation": health.generation,
        "reason": reason,
        # A continuing threshold breach is one state even if its counter keeps rising.
        # A different tracked issue is a genuinely new restart candidate.
        "restart_issue_key": (
            health.restart_issue_key
            if reason == "same issue repeated after two interventions"
            else None
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _runtime_action_signature(action: TriggeringAction) -> str:
    payload = action.model_dump(mode="json")
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _unique_runtime_trigger_actions(
    actions: Any,
) -> list[TriggeringAction]:
    selected: list[TriggeringAction] = []
    seen: set[str] = set()
    for action in actions:
        if action is None:
            continue
        signature = _runtime_action_signature(action)
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(action)
    return selected


def _suspicious_changed_file_signature(
    workspace_root: Path,
    changed_files: list[ChangedFile],
    *,
    cache: dict[str, tuple[tuple[Any, ...], str]] | None = None,
) -> str | None:
    suspicious = sorted(
        (changed for changed in changed_files if _is_suspicious_changed_path(changed.path)),
        key=lambda item: item.path,
    )
    if not suspicious:
        return None
    cache = cache if cache is not None else {}
    payload = [
        {
            "path": changed.path,
            "status": changed.status,
            "additions": changed.additions,
            "deletions": changed.deletions,
            "content": _workspace_path_fingerprint(workspace_root, changed.path, cache=cache),
        }
        for changed in suspicious
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _workspace_path_fingerprint(
    workspace_root: Path,
    relative_path: str,
    *,
    cache: dict[str, tuple[tuple[Any, ...], str]],
) -> str:
    raw_path = Path(relative_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        return "invalid-path"
    path = workspace_root / raw_path
    try:
        lexical_stat = path.lstat()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unavailable:{type(exc).__name__}"
    if stat.S_ISLNK(lexical_stat.st_mode):
        try:
            target = os.readlink(path)
        except OSError as exc:
            target = f"unreadable:{type(exc).__name__}"
        cache.pop(relative_path, None)
        return "symlink:" + hashlib.sha256(target.encode("utf-8", errors="replace")).hexdigest()
    try:
        resolved = path.resolve()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unresolved:{type(exc).__name__}"
    if not ensure_relative_to(resolved, workspace_root):
        cache.pop(relative_path, None)
        return "outside-workspace"
    try:
        file_stat = resolved.stat()
    except OSError as exc:
        cache.pop(relative_path, None)
        return f"unavailable:{type(exc).__name__}"
    stat_key = (
        file_stat.st_mode,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_ino,
    )
    cached = cache.get(relative_path)
    if cached is not None and cached[0] == stat_key:
        return cached[1]
    if not stat.S_ISREG(file_stat.st_mode):
        fingerprint = hashlib.sha256(repr(stat_key).encode("ascii")).hexdigest()
    else:
        try:
            digest = _hash_file(resolved)
            fingerprint = f"regular:{stat.S_IMODE(file_stat.st_mode):o}:{digest}"
        except OSError as exc:
            fingerprint = (
                f"unreadable:{stat.S_IMODE(file_stat.st_mode):o}:{type(exc).__name__}"
            )
    cache[relative_path] = (stat_key, fingerprint)
    return fingerprint


def _action_timed_out(action: TriggeringAction) -> bool:
    return action.timed_out


def _change_kind(status: str) -> str:
    normalized = status.strip().upper()
    if "D" in normalized:
        return "deleted"
    if "R" in normalized:
        return "renamed"
    if "A" in normalized or "?" in normalized:
        return "added"
    if normalized:
        return "modified"
    return "unknown"


def _changed_tests_summary(path: str, text: str, validations: list[ValidationRun]) -> ChangedTestsSummary:
    return ChangedTestsSummary(
        path=path,
        added_or_modified_test_names=_detect_test_names(text),
        changed_assertion_snippets=_assertion_snippets(text),
        grep_or_test_selection_relevant_to_validations=[
            validation.command
            for validation in validations
            if path in _target_files_or_test_files(validation.command)
        ],
        summary_truncated=text.endswith("...<truncated>"),
    )


def _validation_output(validation: ValidationRun) -> ValidationOutput:
    return ValidationOutput(
        validation_id=validation.validation_id,
        command=validation.command,
        raw_command=validation.raw_command,
        normalized_command=validation.normalized_command,
        cwd=validation.cwd,
        exit_code=validation.exit_code,
        shell_exit_code=validation.shell_exit_code,
        type=validation.type,
        outcome=validation.outcome,
        passed=validation.passed,
        trusted_validation_outcome=validation.trusted_validation_outcome,
        masking_reason=validation.masking_reason,
        sequence=validation.sequence,
        stdout_or_summary=validation.summary,
        stderr_or_summary=None,
        captured_output=validation.captured_output,
        output_truncated=validation.summary.endswith("...<truncated>") or validation.captured_output_truncated,
        detected_test_names=_detect_test_names(validation.summary),
        target_files_or_test_files=validation.target_files_or_test_files
        or _target_files_or_test_files(validation.command),
        was_filtered=validation.was_filtered,
        raw_selector=validation.raw_selector,
        executed_test_names=validation.executed_test_names,
        executed_test_files=validation.executed_test_files,
        passed_count=validation.passed_count,
        failed_count=validation.failed_count,
    )


def _completion_delta_evidence_summary(
    validations: list[ValidationRun],
    inspections: list[InspectionRun],
    *,
    since_sequence: int | None,
) -> list[str]:
    if since_sequence is None:
        return []
    items: list[str] = []
    for validation in validations:
        items.append(
            (
                f"validation {validation.validation_id} seq={validation.sequence} "
                f"type={validation.type} outcome={validation.trusted_validation_outcome} "
                f"command={_bounded_text(validation.command, limit=160)}"
            )
        )
    for inspection in inspections:
        outcome = "passed" if inspection.passed and inspection.outcome == "pass" else "failed"
        items.append(
            (
                f"inspection {inspection.inspection_id} seq={inspection.sequence} "
                f"outcome={outcome} command={_bounded_text(inspection.command, limit=160)}"
            )
        )
    if not items:
        return [f"No validation or inspection records after return baseline sequence {since_sequence}."]
    return items[:30]


def _inspection_output(inspection: InspectionRun) -> InspectionOutput:
    return InspectionOutput(
        inspection_id=inspection.inspection_id,
        command=inspection.command,
        raw_command=inspection.raw_command,
        normalized_command=inspection.normalized_command,
        cwd=inspection.cwd,
        exit_code=inspection.exit_code,
        shell_exit_code=inspection.shell_exit_code,
        outcome=inspection.outcome,
        passed=inspection.passed,
        sequence=inspection.sequence,
        stdout_or_summary=inspection.summary,
        captured_output=inspection.captured_output,
        output_truncated=inspection.summary.endswith("...<truncated>") or inspection.captured_output_truncated,
        inspected_paths=inspection.inspected_paths,
    )


def _evidence_provenance_summary(
    *,
    validations: list[ValidationRun],
    changed_files: list[ChangedFile],
    latest_change_sequence: int | None,
) -> EvidenceProvenanceSummary:
    changed_test_files = _changed_test_files(changed_files)
    return EvidenceProvenanceSummary(
        latest_relevant_change_sequence=latest_change_sequence,
        changed_test_files=changed_test_files,
        validations=[
            _validation_provenance(
                validation,
                changed_test_files=changed_test_files,
                latest_change_sequence=latest_change_sequence,
            )
            for validation in validations[-VALIDATION_LEDGER_LIMIT:]
        ],
    )


def _changed_test_files(changed_files: list[ChangedFile]) -> list[str]:
    files = [
        _normalize_review_path(changed.path)
        for changed in changed_files
        if _file_kind(changed.path) == "test"
    ]
    return list(dict.fromkeys(path for path in files if path))


def _changed_test_file_identity_map(changed_test_files: list[str]) -> dict[str, str]:
    identities: dict[str, str] = {}
    for path in changed_test_files:
        identity = _canonical_test_file_identity(path)
        if identity and identity not in identities:
            identities[identity] = path
    return identities


def _partition_executed_test_files(
    executed_files: list[str],
    *,
    changed_test_identities: dict[str, str],
) -> tuple[list[str], list[str]]:
    coder_authored_files: list[str] = []
    untouched_files: list[str] = []
    for path in executed_files:
        identity = _canonical_test_file_identity(path)
        changed_path = changed_test_identities.get(identity)
        if changed_path:
            coder_authored_files.append(changed_path)
        else:
            untouched_files.append(path)
    return list(dict.fromkeys(coder_authored_files)), list(dict.fromkeys(untouched_files))


def _canonical_test_file_identity(path: str) -> str:
    normalized = _normalize_review_path(path)
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part]
    if not parts:
        return ""
    name = parts[-1]
    stem = _strip_test_path_extensions(name)
    if not stem:
        stem = name
    prefix = "/".join(parts[:-1])
    identity = f"{prefix}/{stem}" if prefix else stem
    return identity.lower()


def _validation_provenance(
    validation: ValidationRun,
    *,
    changed_test_files: list[str],
    latest_change_sequence: int | None,
) -> ValidationProvenance:
    executed_files = list(dict.fromkeys(_normalize_review_path(path) for path in validation.executed_test_files if path))
    coder_authored_files, untouched_files = _partition_executed_test_files(
        executed_files,
        changed_test_identities=_changed_test_file_identity_map(changed_test_files),
    )
    captured_output = validation.captured_output or ""
    captured_output_present = bool(captured_output.strip())
    fresh = None if latest_change_sequence is None else validation.sequence > latest_change_sequence
    output_kind = _validation_output_kind(validation, captured_output_present=captured_output_present)
    independence_class, risk_reasons = _validation_independence(
        validation,
        fresh_after_latest_relevant_change=fresh,
        captured_output_present=captured_output_present,
        output_kind=output_kind,
        executed_test_files=executed_files,
        coder_authored_test_files=coder_authored_files,
        untouched_executed_test_files=untouched_files,
    )
    return ValidationProvenance(
        validation_id=validation.validation_id,
        command=validation.command,
        type=validation.type,
        passed=validation.outcome == "pass" and validation.passed,
        trusted_validation_outcome=validation.trusted_validation_outcome,
        sequence=validation.sequence,
        fresh_after_latest_relevant_change=fresh,
        captured_output_present=captured_output_present,
        output_identifies_test_files=bool(executed_files),
        executed_test_files=executed_files,
        coder_authored_test_files=coder_authored_files,
        untouched_executed_test_files=untouched_files,
        target_files_or_test_files=validation.target_files_or_test_files
        or _target_files_or_test_files(validation.command),
        output_kind=output_kind,
        independence_class=independence_class,
        risk_reasons=risk_reasons,
    )


def _validation_output_kind(
    validation: ValidationRun,
    *,
    captured_output_present: bool,
) -> str:
    if validation.type == "static":
        return "not_applicable"
    if not captured_output_present:
        return "missing"
    if validation.type == "behavioral":
        if validation.executed_test_files or validation.passed_count is not None or validation.failed_count is not None:
            return "test_runner_output"
        return "unknown"
    if validation.type == "behavior_demo":
        if _captured_output_looks_like_test_runner(validation.captured_output):
            return "test_runner_output"
        if _captured_output_is_self_verdict_only(validation.captured_output):
            return "self_verdict_only"
        return "factual_observation_candidate"
    return "unknown"


def _validation_independence(
    validation: ValidationRun,
    *,
    fresh_after_latest_relevant_change: bool | None,
    captured_output_present: bool,
    output_kind: str,
    executed_test_files: list[str],
    coder_authored_test_files: list[str],
    untouched_executed_test_files: list[str],
) -> tuple[str, list[str]]:
    risk_reasons: list[str] = []
    if validation.trusted_validation_outcome == "masked_or_unknown":
        risk_reasons.append(validation.masking_reason or "masked_or_unknown_validation")
        return "masked_or_unknown", risk_reasons
    if validation.outcome != "pass" or not validation.passed or validation.trusted_validation_outcome != "passed":
        risk_reasons.append("failed_validation")
        return "failed", risk_reasons
    if fresh_after_latest_relevant_change is False:
        risk_reasons.append("stale_after_latest_relevant_change")
        return "stale", risk_reasons
    if validation.type == "static":
        risk_reasons.append("static_validation_not_behavioral_evidence")
        return "not_independent", risk_reasons
    if validation.type == "behavior_demo":
        if not captured_output_present:
            risk_reasons.append("behavior_demo_missing_captured_output")
            return "not_independent", risk_reasons
        if output_kind == "self_verdict_only":
            risk_reasons.append("behavior_demo_self_verdict_only")
            return "not_independent", risk_reasons
        if output_kind == "test_runner_output":
            risk_reasons.append("behavior_demo_looks_like_test_runner_output")
            return "not_independent", risk_reasons
        return "independent_candidate", risk_reasons
    if validation.type == "behavioral":
        if not executed_test_files:
            risk_reasons.append("unknown_test_file_provenance")
            return "unknown", risk_reasons
        if untouched_executed_test_files:
            return "independent", risk_reasons
        if coder_authored_test_files and len(coder_authored_test_files) == len(executed_test_files):
            risk_reasons.append("all_output_identified_tests_were_coder_authored")
            return "self_confirming", risk_reasons
        risk_reasons.append("unknown_test_file_provenance")
        return "unknown", risk_reasons
    return "unknown", risk_reasons


def _captured_output_is_self_verdict_only(output: str) -> bool:
    lines = [line.strip().strip(".!").lower() for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    verdict_pattern = re.compile(
        r"^(?:pass(?:ed)?|ok|success(?:ful)?|works?|correct|done|green|valid|all good)$"
    )
    return all(verdict_pattern.fullmatch(line) for line in lines)


def _captured_output_looks_like_test_runner(output: str) -> bool:
    text = output.strip()
    if not text:
        return False
    patterns = (
        r"(?m)^\s*(?:PASS|FAIL)\s+[\w@+./-]+",
        r"(?m)\b[\w@+./-]+::test_[\w.\[\]-]+\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS)\b",
        r"(?i)\b\d+\s+(?:passed|passing|failed|failing|skipped)\b",
        r"(?i)\btest result:\s+(?:ok|failed)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _detect_test_names(text: str, *, limit: int = 50) -> list[str]:
    names: list[str] = []
    patterns = (
        r"\b(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]",
        r"\bdef\s+(test_[A-Za-z0-9_]+)\s*\(",
        r"\bclass\s+(Test[A-Za-z0-9_]+)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            names.append(match.group(1).strip())
            if len(names) >= limit:
                return list(dict.fromkeys(names))
    return list(dict.fromkeys(names))


def _assertion_snippets(text: str, *, limit: int = 30) -> list[str]:
    snippets: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if any(token in lowered for token in ("assert", "expect(", ".should", "equal", "strictEqual".lower())):
            snippets.append(_bounded_text(stripped, limit=240))
            if len(snippets) >= limit:
                break
    return snippets


def _target_files_or_test_files(command: str) -> list[str]:
    windows_surface, windows_tokens, payload = _windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return []
        command = payload.replace("\\", "/")
    targets: list[str] = []
    for match in re.finditer(
        r"(?<![\w./-])(?:\.?/)?[\w./-]+\.(?:py|ps1|js|jsx|ts|tsx|mjs|cjs|rb|go|rs|java|cs|php)(?![\w.-])",
        command,
    ):
        target = match.group(0).strip("'\"")
        if target:
            targets.append(target.lstrip("./"))
    return list(dict.fromkeys(targets))


def _inspected_paths_from_command(command: str, *, limit: int = 50) -> list[str]:
    windows_surface, windows_tokens, payload = _windows_classification_tokens(command)
    if windows_surface:
        if not windows_tokens or not payload:
            return []
        tokens = windows_tokens
    else:
        tokens = []
    inner = _shell_command_payload(command)
    if not windows_surface and inner is not None and inner != command:
        return _inspected_paths_from_command(inner, limit=limit)
    targets: list[str] = []
    if not windows_surface:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
    option_value_flags = {"-f", "--file", "--config", "-C"}
    skip_next = False
    commands = {
        "cat",
        "sed",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "head",
        "tail",
        "nl",
        "ls",
        "wc",
        "pwd",
        "stat",
        "file",
        "find",
        "get-childitem",
        "get-content",
        "get-location",
        "select-string",
        "type",
        "git",
        "diff",
        "status",
        "log",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "for-each-ref",
    }
    common_target_dirs = {"src", "lib", "app", "tests", "test", "include", "public", "packages", "pkg"}
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in option_value_flags:
            skip_next = True
            continue
        path_token = token.replace("\\", "/") if windows_surface else token
        stripped = path_token.strip("'\"").lstrip("./")
        if not stripped or stripped.startswith(("-", "/")) or stripped.casefold() in commands:
            continue
        if stripped == ".":
            targets.append(".")
        elif stripped in common_target_dirs:
            targets.append(stripped)
        elif "/" in stripped or re.search(r"\.[A-Za-z0-9_-]{1,12}$", stripped):
            targets.append(stripped)
        if len(targets) >= limit:
            break
    return list(dict.fromkeys(targets))


def _paths_from_item(item: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    raw_paths = item.get("paths")
    if isinstance(raw_paths, list):
        paths.extend(str(path) for path in raw_paths if isinstance(path, str))
    file_changes = item.get("fileChanges")
    if isinstance(file_changes, dict):
        paths.extend(str(path) for path in file_changes)
    changes = item.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            for key in ("path", "filePath", "file_path", "filepath"):
                value = change.get(key)
                if isinstance(value, str):
                    paths.append(value)
    return list(dict.fromkeys(paths))


def _patch_summary_from_item(item: Any, limit: int = 4000) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "fileChange":
        return None
    changes = item.get("changes") or item.get("fileChanges")
    if changes is None:
        return None
    return _bounded_json(changes, limit=limit)


def _patch_summary_from_approval_context(context: ApprovalContext, limit: int = 4000) -> str | None:
    if context.diff:
        return _bounded_text(context.diff, limit=limit)
    if context.file_changes:
        return _bounded_json(context.file_changes, limit=limit)
    return None


def _bounded_text(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...<truncated>"


def _bounded_json(value: Any, *, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return _bounded_text(text, limit=limit)


def _parse_numstat(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _hash_file(path: Path) -> str:
    descriptor = _open_regular_file_no_follow(path)
    digest = hashlib.sha256()
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _schema_file_exists(out_dir: Path, name: str) -> bool:
    return (out_dir / name).exists() or (out_dir / "v2" / name).exists()


def _turn_start_schema_supports_effort(out_dir: Path) -> bool:
    for path in (out_dir / "TurnStartParams.json", out_dir / "v2" / "TurnStartParams.json"):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        properties = payload.get("properties")
        if isinstance(properties, dict) and "effort" in properties:
            return True
    return False


def _run_probe(args: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()


def _controller_executable(
    name: str,
    cwd: Path,
    *,
    environ: dict[str, str] | None = None,
) -> str | None:
    if not is_windows_platform():
        return shutil.which(name, path=(environ or os.environ).get("PATH")) or name
    try:
        return require_trusted_executable(
            name,
            cwd=cwd,
            environ=environ,
            windows=True,
        )
    except ExecutableResolutionError:
        return None


def _resolve_controller_models(
    *,
    model: str | None,
    coder_model: str | None,
    supervisor_model: str | None,
    runtime_model: str | None,
    completion_model: str | None,
    adversary_model: str | None,
) -> tuple[str, str, str, str]:
    if model and (coder_model or supervisor_model or runtime_model or completion_model):
        raise RuntimeError(
            "model cannot be combined with coder_model, supervisor_model, runtime_model, or completion_model"
        )
    if supervisor_model and (runtime_model or completion_model):
        raise RuntimeError("supervisor_model cannot be combined with runtime_model or completion_model")
    if model:
        return model, model, model, adversary_model or DEFAULT_MODEL
    legacy_supervisor_model = supervisor_model or DEFAULT_MODEL
    return (
        coder_model or DEFAULT_MODEL,
        runtime_model or legacy_supervisor_model,
        completion_model or legacy_supervisor_model,
        adversary_model or DEFAULT_MODEL,
    )


def _shared_primary_model(config: ProjectConfig) -> str | None:
    models = {config.coder_mod, config.runtime_mod, config.completion_mod}
    return config.coder_mod if len(models) == 1 else None


def _selected_model_availability(
    models_response: dict[str, Any],
    *,
    coder_model: str | None,
    runtime_model: str | None,
    completion_model: str | None,
    adversary_model: str | None = None,
    revision_coder_model: str | None = None,
    subagent_models: tuple[str, ...] = (),
) -> ModelAvailabilityResult:
    available_models = tuple(sorted(_extract_model_ids(models_response)))
    available = set(available_models)
    missing: list[str] = []
    if coder_model and coder_model not in available:
        missing.append(f"coder={coder_model}")
    if revision_coder_model and revision_coder_model not in available:
        missing.append(f"revision-coder={revision_coder_model}")
    if runtime_model and runtime_model not in available:
        missing.append(f"runtime={runtime_model}")
    if completion_model and completion_model not in available:
        missing.append(f"completion={completion_model}")
    if adversary_model and adversary_model not in available:
        missing.append(f"adversary={adversary_model}")
    for subagent_model in subagent_models:
        if subagent_model not in available:
            missing.append(f"subagent={subagent_model}")
    return ModelAvailabilityResult(missing_roles=tuple(missing), available_models=available_models)


def _extract_model_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key in ("id", "model", "slug", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())
        for key in ("data", "models", "items"):
            if key in value:
                ids.update(_extract_model_ids(value[key]))
        return ids
    if isinstance(value, list):
        for item in value:
            ids.update(_extract_model_ids(item))
        return ids
    if isinstance(value, str) and value.strip():
        ids.add(value.strip())
    return ids


def _sandbox_is_read_only(value: Any) -> bool:
    if value == "read-only":
        return True
    if isinstance(value, dict):
        return value.get("type") == "readOnly" and value.get("networkAccess") is False
    return False


def _sandbox_matches_mode(value: Any, mode: str, *, workspace_root: Path | None = None) -> bool:
    if mode == CODER_SANDBOX_DANGER_FULL_ACCESS:
        if value == "danger-full-access":
            return True
        if isinstance(value, dict):
            return value.get("type") == "dangerFullAccess"
        return False
    if mode == CODER_SANDBOX_WORKSPACE_WRITE:
        if not isinstance(value, dict) or value.get("type") != "workspaceWrite":
            return False
        if value.get("networkAccess") is not False:
            return False
        roots = value.get("writableRoots")
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            return False
        if workspace_root is None:
            return not roots
        expected = workspace_root.resolve()
        for raw in roots:
            try:
                if Path(raw).expanduser().resolve(strict=False) != expected:
                    return False
            except OSError:
                return False
        return True
    return _sandbox_is_read_only(value)


def _approval_resolution_is_denial(decision: str | dict[str, Any]) -> bool:
    return isinstance(decision, str) and decision in {"decline", "cancel", "denied", "abort"}


def _approval_resolution_metric_key(decision: str | dict[str, Any]) -> str:
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict) and decision:
        return str(next(iter(decision)))
    return "unknown"


def _observed_changed_files(controller: Any) -> list[ChangedFile]:
    observed = getattr(controller, "observed_changed_files", None)
    if not isinstance(observed, dict):
        return []
    project_root = getattr(controller, "project_root", None)
    task_path = getattr(controller, "task_path", None)
    return [
        changed
        for changed in observed.values()
        if not _is_ignored_changed_path(changed.path, project_root=project_root, task_path=task_path)
    ][:200]


def _path_from_git_status_line(line: str) -> str:
    if len(line) > 2 and line[2] == " ":
        return line[3:].strip()
    if len(line) > 2:
        return line[2:].strip()
    return line.strip()


def _git_status_entries_from_porcelain_v1_z(output: str) -> list[tuple[str, str]]:
    records = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or len(record) < 4 or record[2] != " ":
            continue
        raw_status = record[:2]
        path = record[3:]
        if path:
            entries.append((path, raw_status.strip() or "modified"))
        if "R" in raw_status or "C" in raw_status:
            # In -z mode Git emits the destination in this record and the
            # source path as the following NUL-delimited record.
            index += 1
    return entries


def _format_validation(validation: ValidationRun) -> str:
    exit_code = "unknown" if validation.exit_code is None else str(validation.exit_code)
    return f"{validation.command} ({validation.type} {validation.outcome}, exit={exit_code})"


def _workspace_display_path(project_root: Path, raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        return str(path)
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return raw_path


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _is_internal_runtime_path(path: str, *, project_root: Path | None, task_path: Path | str | None) -> bool:
    normalized = _normalize_internal_workspace_path(str(path).strip().strip("'\""))
    if not normalized:
        return False
    if normalized == ".git-init.log":
        return True
    if normalized == ".supervisor" or normalized.startswith(".supervisor/"):
        return True
    task_relative = _task_relative_workspace_path(project_root=project_root, task_path=task_path)
    return bool(task_relative and normalized == task_relative)


def _is_ignored_changed_path(path: str, *, project_root: Path | None, task_path: Path | str | None) -> bool:
    return _is_internal_runtime_path(
        path,
        project_root=project_root,
        task_path=task_path,
    ) or _is_generated_or_cache_artifact_path(path, project_root=project_root)


def _is_generated_or_cache_artifact_path(path: str, *, project_root: Path | None) -> bool:
    normalized = _normalize_internal_workspace_path(str(path).strip().strip("'\""))
    if not normalized:
        return False
    parts = set(normalized.lower().split("/"))
    if parts & {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".parcel-cache",
        "node_modules",
    }:
        return True
    name = normalized.rsplit("/", 1)[-1].lower()
    if name.endswith(
        (
            ".pyc",
            ".pyo",
            ".gcda",
            ".gcno",
            ".tsbuildinfo",
        )
    ):
        return True
    return False


def _task_relative_workspace_path(*, project_root: Path | None, task_path: Path | str | None) -> str | None:
    if task_path is None:
        return None
    task = Path(task_path)
    if project_root is not None:
        try:
            task = task.resolve()
            return _normalize_internal_workspace_path(str(task.relative_to(Path(project_root).resolve())))
        except (OSError, ValueError):
            pass
    if task.is_absolute():
        return _normalize_internal_workspace_path(task.name)
    return _normalize_internal_workspace_path(str(task))


def _normalize_internal_workspace_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _filter_internal_git_output(
    output: str,
    *,
    command: list[str],
    project_root: Path,
    task_path: Path,
) -> str:
    if not output:
        return output
    if command[:2] == ["git", "status"]:
        lines = [
            line
            for line in output.splitlines()
            if not _is_ignored_changed_path(
                _git_status_changed_path(line),
                project_root=project_root,
                task_path=task_path,
            )
        ]
        return "\n".join(lines)
    if command[:2] == ["git", "diff"] and "--name-only" in command:
        lines = [
            line
            for line in output.splitlines()
            if not _is_ignored_changed_path(line.strip(), project_root=project_root, task_path=task_path)
        ]
        return "\n".join(lines)
    if command[:2] == ["git", "diff"] and "--stat" in command:
        lines: list[str] = []
        for line in output.splitlines():
            if "|" not in line:
                continue
            path = line.split("|", 1)[0].strip()
            if not _is_ignored_changed_path(path, project_root=project_root, task_path=task_path):
                lines.append(line)
        return "\n".join(lines)
    return output


def _git_status_changed_path(line: str) -> str:
    path = _path_from_git_status_line(line)
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[1].strip()
    return path


def _turn_id_from_params(params: dict[str, Any]) -> str | None:
    if isinstance(params.get("turnId"), str):
        return params["turnId"]
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    return None


def _notification_thread_id(method: str, params: dict[str, Any]) -> str | None:
    thread_id = params.get("threadId")
    if isinstance(thread_id, str):
        return thread_id
    if method == "thread/started":
        thread = params.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("id"), str):
            return thread["id"]
    return None


def _thread_status_type(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"]
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _turn_terminal_status(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("status"), str):
        return value["status"]
    return "idle"


def _bounded_subagent_text(value: Any, *, limit: int = SUBAGENT_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _optional_bounded_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _bounded_subagent_text(value, limit=limit)


def _short_thread_id(thread_id: Any) -> str:
    if not isinstance(thread_id, str):
        return "unknown"
    return thread_id if len(thread_id) <= 12 else thread_id[:12]


def _format_multi_agent_summary(multi_agent: Any) -> str:
    if not getattr(multi_agent, "enabled", False):
        return "off"
    default = getattr(multi_agent, "default", None)
    return (
        f"on(max={getattr(multi_agent, 'max_concurrent', '?')},"
        f"default={getattr(default, 'model', '?')}/{getattr(default, 'intelligence', '?')})"
    )


def _format_allowed_subagent_profiles(multi_agent: Any) -> str:
    allowed = getattr(multi_agent, "allowed", {})
    if not isinstance(allowed, dict) or not allowed:
        return "none"
    return "; ".join(
        f"{model}: {', '.join(str(effort) for effort in efforts)}"
        for model, efforts in allowed.items()
    )


def _bounded_subagent_event_payload(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "thread/started":
        thread = params.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("parentThreadId"), str):
            return {}
        return {
            "parent_thread_id": thread["parentThreadId"],
            "status": _thread_status_type(thread.get("status")),
            "nickname": _optional_bounded_text(thread.get("agentNickname"), 120),
            "role": _optional_bounded_text(thread.get("agentRole"), 120),
        }
    if method == "thread/status/changed":
        return {"status": _thread_status_type(params.get("status"))}
    if method not in {"item/started", "item/completed"}:
        return {}
    item = params.get("item")
    if not isinstance(item, dict) or item.get("type") != "collabAgentToolCall":
        return {}
    agents_states = item.get("agentsStates")
    bounded_states: dict[str, Any] = {}
    if isinstance(agents_states, dict):
        for thread_id, state in list(agents_states.items())[:SUBAGENT_SUMMARY_LIMIT]:
            if not isinstance(thread_id, str):
                continue
            bounded_states[thread_id] = (
                state.get("status") if isinstance(state, dict) else state
            )
    return {
        "tool": item.get("tool"),
        "sender_thread_id": item.get("senderThreadId"),
        "receiver_thread_ids": [
            value for value in (item.get("receiverThreadIds") or [])[:SUBAGENT_SUMMARY_LIMIT]
            if isinstance(value, str)
        ],
        "model": item.get("model"),
        "reasoning_effort": item.get("reasoningEffort"),
        "status": item.get("status"),
        "prompt": _optional_bounded_text(item.get("prompt"), 600),
        "agents_states": bounded_states,
    }


def _item_id_from_params(params: dict[str, Any]) -> str | None:
    if isinstance(params.get("itemId"), str):
        return params["itemId"]
    item = params.get("item")
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return None


def _item_summary(item: Any) -> str:
    if not isinstance(item, dict):
        return "item completed"
    item_type = item.get("type", "item")
    if item_type == "commandExecution":
        return f"command completed: {item.get('command', '')} exit={item.get('exitCode')}"
    if item_type == "fileChange":
        return f"file change completed: {len(item.get('changes') or [])} changes"
    if item_type == "mcpToolCall":
        return f"mcp tool completed: {item.get('server')}/{item.get('tool')}"
    if item_type == "dynamicToolCall":
        return f"dynamic tool completed: {item.get('tool')}"
    if item_type == "agentMessage":
        return "agent message completed"
    return f"{item_type} completed"


def _is_completed_action(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") in {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "webSearch"}


def _adversary_enabled_from_env() -> bool | None:
    raw = os.environ.get("BELLO_ADVERSARY_ENABLED", "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _create_adversary_snapshot(
    project_root: Path,
    *,
    excluded_relative_paths: tuple[str, ...] = (),
) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="bello-adversary-")).resolve()
    snapshot_root = temp_root / "workspace"
    try:
        copy_isolated_workspace_tree(
            project_root,
            snapshot_root,
            ignore=_adversary_snapshot_ignore_with_paths(
                project_root,
                excluded_relative_paths,
            ),
        )
    except Exception:
        try:
            remove_isolated_workspace_tree(temp_root)
        except OSError:
            pass
        raise
    _init_snapshot_git(snapshot_root)
    return snapshot_root


def _init_snapshot_git(snapshot_root: Path) -> None:
    """Give the snapshot a functional git repo so tests/tools that shell out to git work.

    Best-effort: an empty initial commit makes HEAD/status/diff usable while keeping every
    file untracked, so recursive deletes inside the snapshot stay policy-approvable.
    """
    git = _controller_executable("git", snapshot_root, environ=snapshot_git_environment())
    if git is None:
        return
    identity = [
        "-c",
        "user.email=bello@localhost",
        "-c",
        "user.name=Bello Snapshot",
        "-c",
        "commit.gpgsign=false",
    ]
    git_env = snapshot_git_environment()
    try:
        initialized = subprocess.run(
            [git, "-c", "init.templateDir=", "init", "-q"],
            cwd=snapshot_root,
            env=git_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if initialized.returncode != 0:
            return
        subprocess.run(
            [git, "config", "--local", "core.hooksPath", os.devnull],
            cwd=snapshot_root,
            env=git_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                *identity,
                "commit",
                "-q",
                "--no-verify",
                "--allow-empty",
                "-m",
                "bello adversary snapshot baseline",
            ],
            cwd=snapshot_root,
            env=git_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return


def _adversary_snapshot_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {
        ".git",
        ".supervisor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    if is_windows_platform():
        ignored_keys = {name.casefold() for name in ignored}
        return {name for name in names if name.casefold() in ignored_keys}
    return {name for name in names if name in ignored}


def _adversary_snapshot_ignore_with_paths(
    project_root: Path,
    excluded_relative_paths: tuple[str, ...],
):
    root = project_root.resolve()
    if is_windows_platform():
        excluded = {Path(path).as_posix().casefold() for path in excluded_relative_paths}
    else:
        excluded = {Path(path).as_posix() for path in excluded_relative_paths}

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = _adversary_snapshot_ignore(directory, names)
        try:
            relative_directory = Path(directory).resolve().relative_to(root)
        except (OSError, ValueError):
            return ignored
        for name in names:
            relative_path = (relative_directory / name).as_posix()
            key = relative_path.casefold() if is_windows_platform() else relative_path
            if key in excluded:
                ignored.add(name)
        return ignored

    return ignore


def _workspace_state_id(project_root: Path) -> str:
    root = project_root.resolve()
    digest = hashlib.sha256()
    skip_dirs = {
        ".git",
        ".supervisor",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
    }
    for current, dirs, files in os.walk(root, followlinks=False):
        rel_dir = Path(current).relative_to(root)
        traversable_dirs: list[str] = []
        for name in sorted(dirs):
            if name in skip_dirs:
                continue
            path = Path(current) / name
            try:
                metadata = path.lstat()
            except OSError:
                _update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
                continue
            if is_link_or_reparse(path, stat_result=metadata):
                _update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
            elif stat.S_ISDIR(metadata.st_mode):
                traversable_dirs.append(name)
            else:
                _update_workspace_entry_digest(digest, path, (rel_dir / name).as_posix())
        dirs[:] = traversable_dirs
        for name in sorted(files):
            path = Path(current) / name
            rel = (rel_dir / name).as_posix()
            _update_workspace_entry_digest(digest, path, rel)
    return digest.hexdigest()


def _update_workspace_entry_digest(digest: Any, path: Path, relative_path: str) -> None:
    encoded_path = relative_path.encode("utf-8", errors="surrogateescape")
    digest.update(encoded_path)
    digest.update(b"\0")
    try:
        metadata = path.lstat()
        mode = metadata.st_mode
        if is_link_or_reparse(path, stat_result=metadata):
            digest.update(b"symlink\0")
            try:
                target = os.readlink(path)
            except OSError:
                target = "<opaque-reparse-point>"
            digest.update(target.encode("utf-8", errors="surrogateescape"))
        elif stat.S_ISREG(mode):
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                opened_mode = os.fstat(descriptor).st_mode
                if not stat.S_ISREG(opened_mode):
                    digest.update(f"special:{stat.S_IFMT(opened_mode):o}".encode("ascii"))
                else:
                    digest.update(b"file\0")
                    while chunk := os.read(descriptor, 1024 * 1024):
                        digest.update(chunk)
            finally:
                os.close(descriptor)
        else:
            digest.update(f"special:{stat.S_IFMT(mode):o}".encode("ascii"))
    except OSError:
        digest.update(b"unreadable\0")
    digest.update(b"\0")


def _latest_validation_sequence(validations: list[ValidationRun]) -> int | None:
    return max((validation.sequence for validation in validations), default=None)


_ADVERSARY_REPORT_DEFINITIONS = (
    "Finding: a confirmed defect that requires correction.\n"
    "Observation: a concern that is not yet confirmed; investigate it and fix it only if confirmed."
)


def _adversary_report_with_definitions(report_to_coder: str) -> str:
    return f"{_ADVERSARY_REPORT_DEFINITIONS}\n\n{report_to_coder.strip()}"


def _final_adversary_report_summary(report: AdversaryReport | None) -> list[str]:
    if report is None:
        return []
    first_line = next((line.strip() for line in report.report_text.splitlines() if line.strip()), "")
    if len(first_line) > 240:
        first_line = first_line[:237].rstrip() + "..."
    details = [
        f"status={report.status}",
        f"candidate_finding={str(report.candidate_finding).lower()}",
        f"completion_wake_sequence={report.completion_wake_sequence}",
        f"latest_relevant_change_sequence={report.latest_relevant_change_sequence}",
    ]
    if first_line:
        details.append(f"summary={first_line}")
    return ["; ".join(details)]


def _is_stream_delta_method(method: str) -> bool:
    lowered = method.lower()
    return lowered.endswith("delta") or method in {
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "command/exec/outputDelta",
        "process/outputDelta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
    }


def _is_command_output_delta_method(method: str) -> bool:
    lowered = method.lower()
    if method in {
        "item/commandExecution/outputDelta",
        "command/exec/outputDelta",
        "process/outputDelta",
    }:
        return True
    return any(token in lowered for token in ("command", "exec", "process")) and (
        lowered.endswith("outputdelta")
        or lowered.endswith("stdoutdelta")
        or lowered.endswith("stderrdelta")
    )


def _changed_files_from_diff_summary(
    diff: str | None,
    *,
    project_root: Path | None = None,
    task_path: Path | str | None = None,
) -> list[str]:
    if not diff:
        return []
    files: list[str] = []
    status_marker = "$ git status --short"
    if status_marker in diff:
        status_tail = diff.split(status_marker, 1)[1].split("$ git diff --stat", 1)[0]
        for line in status_tail.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("$"):
                continue
            path = _git_status_changed_path(stripped)
            if path and not _is_ignored_changed_path(path, project_root=project_root, task_path=task_path) and path not in files:
                files.append(path)
    marker = "$ git diff --name-only"
    if marker in diff:
        tail = diff.split(marker, 1)[1]
        for line in tail.splitlines():
            path = line.strip()
            if (
                path
                and not path.startswith("$")
                and not _is_ignored_changed_path(path, project_root=project_root, task_path=task_path)
                and path not in files
            ):
                files.append(path)
    return files
