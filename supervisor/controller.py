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
from supervisor.runtime.client import RuntimeClient
from supervisor.controller_recovery import (
    DurableRun, RecoveryBlocked, RunOwner, durable_transition, resume_coder,
    observed_item_key, observed_item_value,
)
from supervisor.runtime_errors import bounded_provider_error, sanitize_error_text
from supervisor.runtime.models import engine_effort, parse_model_selection
from supervisor.config_validation import preflight_profiles
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
from supervisor.immutable_reads import _literal_parts
from supervisor.filesystem_safety import is_link_or_reparse, is_windows_platform
from supervisor.executables import ExecutableResolutionError, require_trusted_executable
from supervisor.project_config import DEFAULT_MODEL, LogDistillerConfig, MultiAgentConfig, ProjectConfig
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
from supervisor.state import CONFIG, DECISIONS, HANDOFF, PROGRESS, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError, SupervisorTurnError
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
from supervisor.controller_parts.defaults import COMPLETION_EVIDENCE_FILE_LIMIT, NO_MARKER_IDLE_NUDGE, SUBAGENT_TEXT_LIMIT


VALIDATION_LEDGER_LIMIT = 50
INSPECTION_LEDGER_LIMIT = 50
SUBAGENT_SUMMARY_LIMIT = 12
SUBAGENT_ACTION_LIMIT = 5
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
CONTROLLER_IDLE_GUARD_STALL_SECONDS = 120.0
# These are transport diagnostics, not a time limit on coding or commands. Silence
# alone never fails a run. Only an explicitly retrying provider gets a budget.
CODER_STATUS_PROBE_AFTER_SECONDS = 300.0
CODER_STATUS_PROBE_TIMEOUT_SECONDS = 10.0
CODER_PROVIDER_RETRY_BUDGET_SECONDS = 300.0
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


@dataclass
class _ActiveCoderWatch:
    identity: tuple[int, str, str, int]
    last_progress: float
    last_probe: float = float("-inf")
    retry_since: float | None = None
    last_error: str = ""
    reported_stall: bool = False
    running_tools: set[str] = field(default_factory=set)


from supervisor.controller_parts.interfaces import OwnedField, service_method
from supervisor.controller_parts.coder_lifecycle import CoderLifecycle, CoderLifecyclePort
from supervisor.controller_parts.evidence import Evidence, EvidencePort
from supervisor.controller_parts.settings import Settings, SettingsPort
from supervisor.controller_parts.commands import Commands, CommandsPort
from supervisor.controller_parts.children import Children, ChildrenPort
from supervisor.controller_parts.completion import Completion, CompletionPort
from supervisor.controller_parts.shutdown import Shutdown, ShutdownPort
from supervisor.controller_parts.runtime_review import RuntimeReview, RuntimeReviewPort


from supervisor.controller_parts.runtime_rules import (
    _approval_wake_context,
    _runtime_packet_requires_full_supervisor,
    _runtime_trigger_reasons_from_summary,
    _runtime_unresolved_execution_key,
    _runtime_validation_restart_issue,
    _matching_active_validation_issue,
    _runtime_event_issue_payload,
    _runtime_restart_issue,
    _runtime_triggering_validation,
    _approval_resolution_is_denial,
    _approval_resolution_metric_key,
)
from supervisor.controller_parts.command_parsing import (
    _RESTART_SHELL_NAMES,
    _LITERAL_POWERSHELL_PYTHONPATH_PREFIX,
    _QUOTED_POWERSHELL_PYTHONPATH_WRAPPER,
    _SPLICE_QUOTED_POWERSHELL_PYTHONPATH_WRAPPER,
    _literal_powershell_file_invocation,
    _literal_powershell_pythonpath_payload,
    _literal_powershell_pythonpath_invocation,
    _windows_classification_tokens,
    _windows_tokens_are_git_inspection,
    _windows_effective_tool_tokens,
    _windows_python_args,
    _windows_python_action,
    _PYTEST_NO_RUN_OPTIONS,
    _pytest_args_request_no_test_execution,
    _windows_tokens_are_static_validation,
    _windows_tokens_are_behavioral_validation,
    _windows_tokens_execute_script,
    _windows_tokens_are_read_only_inspection,
    _windows_tokens_are_behavior_demo,
    _canonical_restart_command,
    _triggering_action_from_item,
    _item_explicitly_timed_out,
    _validation_from_action,
    _inspection_from_action,
    _classify_validation_command,
    _is_static_validation_command,
    _is_git_inspection_command,
    _is_git_diff_check_command,
    _is_read_only_inspection_command,
    _inspection_command_segments,
    _shell_command_payload,
    _strip_env_command_prefix,
    _is_read_only_inspection_segment,
    _is_read_only_inspection_tokens,
    _is_behavioral_validation_command,
    _posix_validation_command_segments,
    _posix_tokens_are_behavioral_validation,
    _posix_script_execution_operand,
    _is_test_wrapper_script_command,
    _is_direct_script_execution_command,
    _is_behavior_demo_command,
    _has_behavior_demo_marker,
    _marked_behavior_demo_command_is_plausible,
    _is_observationless_output_command,
    _is_stdin_script_demo_command,
    _command_requires_changed_module,
    _tests_executed,
    _command_output_from_item,
    _item_with_recorded_output,
    _output_delta_text,
    _collect_output_delta_strings,
    _validation_summary,
    _validation_id,
    _normalize_command,
    _stable_validation_id,
    _stable_inspection_id,
    _inspection_exit_is_usable,
    _command_was_filtered,
    _raw_validation_selector,
    _is_python_module_flag,
    _explicit_test_selectors,
    _executed_test_names,
    _test_names_from_output,
    _test_files_from_output,
    _normalize_output_test_path,
    _test_count_summary,
    _first_int_match,
    _collect_output_strings,
    _has_passing_behavioral_validation,
    _is_behavior_proving_validation,
    _validation_is_usable_behavioral_pass,
    _action_timed_out,
    _target_files_or_test_files,
    _inspected_paths_from_command,
    _paths_from_item,
)
from supervisor.controller_parts.lifecycle_rules import (
    _is_no_active_turn_to_steer_error,
    _is_turn_already_inactive_error,
    _fallback_restart_handoff,
    _restart_rejection_steering,
    _has_readiness_marker,
    _is_recoverable_app_server_transport_error,
    _thread_turn_by_id,
    _has_malformed_readiness_marker,
    _readiness_reference_is_negated,
    _appears_to_claim_readiness,
)
from supervisor.controller_parts.completion_rules import (
    _normalized_surface_key,
    _merge_behavior_surface_items,
    _completion_returns_this_generation,
    _prior_record_counts_as_health_intervention,
    _completion_return_summary,
    _behavior_evidence_summary,
    _files_reviewed_summary,
    _normalize_review_path,
    _adversary_enabled_from_env,
    _latest_validation_sequence,
    _ADVERSARY_REPORT_DEFINITIONS,
    _adversary_report_with_definitions,
    _final_adversary_report_summary,
)
from supervisor.controller_parts.evidence_rules import (
    _latest_relevant_change_sequence,
    _validation_freshness_summary,
    _classify_supervisor_agent_error,
    _validation_is_fresh_behavioral_pass,
    _strip_test_path_extensions,
    _BoundedFileText,
    _read_workspace_file,
    _open_regular_file_no_follow,
    _file_kind,
    _is_relevant_changed_path,
    _is_suspicious_changed_path,
    _task_is_docs_facing,
    _read_task_text,
    _ensure_internal_runtime_git_excluded,
    _diff_line_counts,
    _breadth_risk_summary,
    _has_large_diff,
    _change_kind,
    _changed_tests_summary,
    _validation_output,
    _completion_delta_evidence_summary,
    _inspection_output,
    _evidence_provenance_summary,
    _changed_test_files,
    _changed_test_file_identity_map,
    _partition_executed_test_files,
    _canonical_test_file_identity,
    _validation_provenance,
    _validation_output_kind,
    _validation_independence,
    _captured_output_is_self_verdict_only,
    _captured_output_looks_like_test_runner,
    _detect_test_names,
    _assertion_snippets,
    _patch_summary_from_item,
    _patch_summary_from_approval_context,
    _bounded_text,
    _bounded_json,
    _parse_numstat,
    _observed_changed_files,
    _path_from_git_status_line,
    _git_status_entries_from_porcelain_v1_z,
    _format_validation,
    _workspace_display_path,
    _format_bool,
    _is_internal_runtime_path,
    _is_ignored_changed_path,
    _is_generated_or_cache_artifact_path,
    _task_relative_workspace_path,
    _normalize_internal_workspace_path,
    _filter_internal_git_output,
    _git_status_changed_path,
    _changed_files_from_diff_summary,
)
from supervisor.controller_parts.fingerprints import (
    _large_diff_signature,
    _restart_budget_signature,
    _runtime_action_signature,
    _unique_runtime_trigger_actions,
    _suspicious_changed_file_signature,
    _workspace_path_fingerprint,
    _hash_file,
    _workspace_state_id,
    _update_workspace_entry_digest,
)
from supervisor.controller_parts.preflight import (
    _schema_file_exists,
    _turn_start_schema_supports_effort,
    _run_probe,
    _controller_executable,
    _resolve_controller_models,
    _shared_primary_model,
    _selected_model_availability,
    _readable_available_models,
    _extract_model_ids,
    _sandbox_is_read_only,
    _sandbox_matches_mode,
)
from supervisor.controller_parts.event_protocol import (
    _turn_id_from_params,
    _notification_thread_id,
    _thread_status_type,
    _turn_terminal_status,
    _bounded_subagent_text,
    _optional_bounded_text,
    _short_thread_id,
    _format_multi_agent_summary,
    _format_allowed_subagent_profiles,
    _bounded_subagent_event_payload,
    _item_id_from_params,
    _item_summary,
    _is_completed_action,
    _is_stream_delta_method,
    _is_command_output_delta_method,
)
from supervisor.controller_parts.adversary_workspace import (
    _create_adversary_snapshot,
    _init_snapshot_git,
    _adversary_snapshot_ignore,
    _adversary_snapshot_ignore_with_paths,
)


_SERVICE_TYPES = {
    'coder_lifecycle': (CoderLifecycle, CoderLifecyclePort),
    'evidence': (Evidence, EvidencePort),
    'settings': (Settings, SettingsPort),
    'commands': (Commands, CommandsPort),
    'children': (Children, ChildrenPort),
    'completion': (Completion, CompletionPort),
    'shutdown': (Shutdown, ShutdownPort),
    'runtime_review': (RuntimeReview, RuntimeReviewPort),
}


class BelloController:
    """Coordinate events and compose the stateful controller services.

    Historical attributes below are views of their service's state. They keep
    embedding code and __new__ test fixtures working without duplicate storage.
    """

    def _service(self, name: str) -> Any:
        services = self.__dict__.setdefault('_services', {})
        if name not in services:
            service_type, port_type = _SERVICE_TYPES[name]
            services[name] = service_type(port_type(self))
        return services[name]

    # Coder Lifecycle: sole owner of these legacy fields.
    _active_provider_phase = OwnedField('coder_lifecycle', '_active_provider_phase')
    _coder_activity_mutex = OwnedField('coder_lifecycle', '_coder_activity_mutex')
    _coder_snapshot = OwnedField('coder_lifecycle', '_coder_snapshot')
    _coder_started = OwnedField('coder_lifecycle', '_coder_started')
    _coder_watch = OwnedField('coder_lifecycle', '_coder_watch')
    _current_turn_action_count = OwnedField('coder_lifecycle', '_current_turn_action_count')
    _generation_has_coder_turn = OwnedField('coder_lifecycle', '_generation_has_coder_turn')
    _idle_guard_fired_for_sequence = OwnedField('coder_lifecycle', '_idle_guard_fired_for_sequence')
    _last_controller_activity_monotonic = OwnedField('coder_lifecycle', '_last_controller_activity_monotonic')
    _restart_transition_token = OwnedField('coder_lifecycle', '_restart_transition_token')
    _revision_switch_done = OwnedField('coder_lifecycle', '_revision_switch_done')
    _revision_switch_in_progress = OwnedField('coder_lifecycle', '_revision_switch_in_progress')
    _revision_switch_owner = OwnedField('coder_lifecycle', '_revision_switch_owner')
    _snapshot_patch_applied = OwnedField('coder_lifecycle', '_snapshot_patch_applied')
    _transport_error_pending = OwnedField('coder_lifecycle', '_transport_error_pending')
    _transport_recovery_lock = OwnedField('coder_lifecycle', '_transport_recovery_lock')
    _transport_recovery_total = OwnedField('coder_lifecycle', '_transport_recovery_total')
    coder = OwnedField('coder_lifecycle', 'coder')
    last_coder_message = OwnedField('coder_lifecycle', 'last_coder_message')
    workspace_plan_path = OwnedField('coder_lifecycle', 'workspace_plan_path')
    workspace_root = OwnedField('coder_lifecycle', 'workspace_root')
    workspace_task_path = OwnedField('coder_lifecycle', 'workspace_task_path')
    _active_workspace_root = service_method('coder_lifecycle', CoderLifecycle._active_workspace_root)
    _active_dependency_roots = service_method('coder_lifecycle', CoderLifecycle._active_dependency_roots)
    _active_task_path = service_method('coder_lifecycle', CoderLifecycle._active_task_path)
    _active_coder_plan_path = service_method('coder_lifecycle', CoderLifecycle._active_coder_plan_path)
    _canonical_task_text = service_method('coder_lifecycle', CoderLifecycle._canonical_task_text)
    _immutable_approval_paths = service_method('coder_lifecycle', CoderLifecycle._immutable_approval_paths)
    _task_integrity_issue = service_method('coder_lifecycle', CoderLifecycle._task_integrity_issue)
    _runtime_integrity_issue = service_method('coder_lifecycle', CoderLifecycle._runtime_integrity_issue)
    _escalate_runtime_integrity_issue = service_method('coder_lifecycle', CoderLifecycle._escalate_runtime_integrity_issue)
    _repair_snapshot_runtime_controls = service_method('coder_lifecycle', CoderLifecycle._repair_snapshot_runtime_controls)
    _uses_coder_snapshot = service_method('coder_lifecycle', CoderLifecycle._uses_coder_snapshot)
    _prepare_coder_workspace = service_method('coder_lifecycle', CoderLifecycle._prepare_coder_workspace)
    _coder_lifecycle_accepts_activity = service_method('coder_lifecycle', CoderLifecycle._coder_lifecycle_accepts_activity)
    _coder_activity_lock = service_method('coder_lifecycle', CoderLifecycle._coder_activity_lock)
    _wait_for_coder_activity = service_method('coder_lifecycle', CoderLifecycle._wait_for_coder_activity)
    _deliver_coder_message = durable_transition(service_method('coder_lifecycle', CoderLifecycle._deliver_coder_message))
    _mark_controller_activity = service_method('coder_lifecycle', CoderLifecycle._mark_controller_activity)
    _handle_controller_idle_guard = durable_transition(service_method('coder_lifecycle', CoderLifecycle._handle_controller_idle_guard))
    _active_coder_watch = service_method('coder_lifecycle', CoderLifecycle._active_coder_watch)
    _record_coder_progress = service_method('coder_lifecycle', CoderLifecycle._record_coder_progress)
    _handle_active_coder_guard = durable_transition(service_method('coder_lifecycle', CoderLifecycle._handle_active_coder_guard))
    handle_transport_error = service_method('coder_lifecycle', CoderLifecycle.handle_transport_error)
    _transport_recovery_mutex = service_method('coder_lifecycle', CoderLifecycle._transport_recovery_mutex)
    _recover_app_server_transport = service_method('coder_lifecycle', CoderLifecycle._recover_app_server_transport)
    _restart_app_server_client = service_method('coder_lifecycle', CoderLifecycle._restart_app_server_client)
    _abandon_dead_completion_review = service_method('coder_lifecycle', CoderLifecycle._abandon_dead_completion_review)
    _rollback_interrupted_adversary_reservation = service_method('coder_lifecycle', CoderLifecycle._rollback_interrupted_adversary_reservation)
    _recover_coder_thread_after_transport = service_method('coder_lifecycle', CoderLifecycle._recover_coder_thread_after_transport)
    _start_fallback_recovery_coder = service_method('coder_lifecycle', CoderLifecycle._start_fallback_recovery_coder)
    fail_provider = service_method('coder_lifecycle', CoderLifecycle.fail_provider)
    _cleanup_preflight_probe_thread = service_method('coder_lifecycle', CoderLifecycle._cleanup_preflight_probe_thread)
    _clear_persisted_coder_turn = service_method('coder_lifecycle', CoderLifecycle._clear_persisted_coder_turn)
    _reject_late_coder_turn = service_method('coder_lifecycle', CoderLifecycle._reject_late_coder_turn)
    pause = durable_transition(service_method('coder_lifecycle', CoderLifecycle.pause))
    restart = durable_transition(service_method('coder_lifecycle', CoderLifecycle.restart))
    _restart_after_activity_barrier = service_method('coder_lifecycle', CoderLifecycle._restart_after_activity_barrier)
    _restart_transition_is_current = service_method('coder_lifecycle', CoderLifecycle._restart_transition_is_current)
    _switch_to_revision_coder = service_method('coder_lifecycle', CoderLifecycle._switch_to_revision_coder)
    _fail_revision_coder_switch = service_method('coder_lifecycle', CoderLifecycle._fail_revision_coder_switch)
    _wait_for_revision_switch = service_method('coder_lifecycle', CoderLifecycle._wait_for_revision_switch)
    _perform_revision_coder_switch = durable_transition(service_method('coder_lifecycle', CoderLifecycle._perform_revision_coder_switch))
    _revision_switch_context_is_current = service_method('coder_lifecycle', CoderLifecycle._revision_switch_context_is_current)
    _record_cancelled_revision_switch = service_method('coder_lifecycle', CoderLifecycle._record_cancelled_revision_switch)
    _discard_uncommitted_revision_thread = service_method('coder_lifecycle', CoderLifecycle._discard_uncommitted_revision_thread)
    _interrupt_stale_revision_turn = service_method('coder_lifecycle', CoderLifecycle._interrupt_stale_revision_turn)

    # Evidence: sole owner of these legacy fields.
    observed_changed_files = OwnedField('evidence', 'observed_changed_files')
    _review_private_relative_paths = service_method('evidence', Evidence._review_private_relative_paths)
    _is_review_private_path = service_method('evidence', Evidence._is_review_private_path)
    _exposes_review_private_input = service_method('evidence', Evidence._exposes_review_private_input)
    _review_safe_values = service_method('evidence', Evidence._review_safe_values)
    _review_safe_packet_state = service_method('evidence', Evidence._review_safe_packet_state)
    _git_command_excluding_review_private_inputs = service_method('evidence', Evidence._git_command_excluding_review_private_inputs)
    diff_summary = service_method('evidence', Evidence.diff_summary)
    changed_files = service_method('evidence', Evidence.changed_files)
    _record_changed_files = service_method('evidence', Evidence._record_changed_files)
    _is_git_work_tree = service_method('evidence', Evidence._is_git_work_tree)
    _git_output = service_method('evidence', Evidence._git_output)
    patch_summary = service_method('evidence', Evidence.patch_summary)
    completion_packet_details = service_method('evidence', Evidence.completion_packet_details)
    _changed_file_diff = service_method('evidence', Evidence._changed_file_diff)

    # Settings: sole owner of these legacy fields.
    _coder_model = service_method('settings', Settings._coder_model)
    _revision_coder_enabled = service_method('settings', Settings._revision_coder_enabled)
    _revision_coder_model = service_method('settings', Settings._revision_coder_model)
    _revision_coder_active = service_method('settings', Settings._revision_coder_active)
    _active_coder_model = service_method('settings', Settings._active_coder_model)
    _runtime_model = service_method('settings', Settings._runtime_model)
    _supervisor_model = service_method('settings', Settings._supervisor_model)
    _completion_model = service_method('settings', Settings._completion_model)
    _adversary_model = service_method('settings', Settings._adversary_model)
    _fast_mode = service_method('settings', Settings._fast_mode)
    _cheap_runtime_enabled = service_method('settings', Settings._cheap_runtime_enabled)
    _runtime_enabled = service_method('settings', Settings._runtime_enabled)
    _post_coder_review_enabled = service_method('settings', Settings._post_coder_review_enabled)
    _async_tools_enabled = service_method('settings', Settings._async_tools_enabled)
    _log_distiller_config = service_method('settings', Settings._log_distiller_config)
    _windows_native_root_read_enabled = service_method('settings', Settings._windows_native_root_read_enabled)
    _effective_completion_review = service_method('settings', Settings._effective_completion_review)
    _adversary_enabled_for_config = service_method('settings', Settings._adversary_enabled_for_config)
    _configured_adversary_runs = service_method('settings', Settings._configured_adversary_runs)
    _project_config_for_persistence = service_method('settings', Settings._project_config_for_persistence)
    _runtime_settings_summary = service_method('settings', Settings._runtime_settings_summary)
    _coder_intelligence = service_method('settings', Settings._coder_intelligence)
    _revision_coder_intelligence = service_method('settings', Settings._revision_coder_intelligence)
    _active_coder_intelligence = service_method('settings', Settings._active_coder_intelligence)
    _runtime_intelligence = service_method('settings', Settings._runtime_intelligence)
    _supervisor_intelligence = service_method('settings', Settings._supervisor_intelligence)
    _completion_intelligence = service_method('settings', Settings._completion_intelligence)
    _adversary_intelligence = service_method('settings', Settings._adversary_intelligence)
    _completion_supervisor_agent = service_method('settings', Settings._completion_supervisor_agent)
    _post_coder_review_agent = service_method('settings', Settings._post_coder_review_agent)
    _adv_report_controller_agent = service_method('settings', Settings._adv_report_controller_agent)
    preflight = service_method('settings', Settings.preflight)
    _runtime_preflight = service_method('settings', Settings._runtime_preflight)
    _report_alias_resolution = service_method('settings', Settings._report_alias_resolution)
    _ensure_selected_models_available = service_method('settings', Settings._ensure_selected_models_available)
    _enabled_subagent_models_for_preflight = service_method('settings', Settings._enabled_subagent_models_for_preflight)
    _adversary_model_required_for_preflight = service_method('settings', Settings._adversary_model_required_for_preflight)
    _generate_schema_hash = service_method('settings', Settings._generate_schema_hash)
    _generate_schema_hash_async = service_method('settings', Settings._generate_schema_hash_async)
    _structured_output_self_test = service_method('settings', Settings._structured_output_self_test)

    # Commands: sole owner of these legacy fields.
    _command_output_chunks = OwnedField('commands', '_command_output_chunks')
    inspections = OwnedField('commands', 'inspections')
    validation_runtime_state = OwnedField('commands', 'validation_runtime_state')
    validations = OwnedField('commands', 'validations')
    _record_command_output_delta = service_method('commands', Commands._record_command_output_delta)
    _pop_command_output = service_method('commands', Commands._pop_command_output)
    _handle_subagent_item_completed = service_method('commands', Commands._handle_subagent_item_completed)
    _handle_completed_coder_action = service_method('commands', Commands._handle_completed_coder_action)
    _record_validation_progress = service_method('commands', Commands._record_validation_progress)
    _record_validation_runtime_state = service_method('commands', Commands._record_validation_runtime_state)

    # Children: sole owner of these legacy fields.
    _coder_quiesce_mutex = OwnedField('children', '_coder_quiesce_mutex')
    _deferred_completion_check = OwnedField('children', '_deferred_completion_check')
    _quiescing_coder_tree = OwnedField('children', '_quiescing_coder_tree')
    _reviewer_thread_ids = OwnedField('children', '_reviewer_thread_ids')
    _reviewer_thread_roles = OwnedField('children', '_reviewer_thread_roles')
    _subagent_policy_notified = OwnedField('children', '_subagent_policy_notified')
    _subagents = OwnedField('children', '_subagents')
    _subagent_registry = service_method('children', Children._subagent_registry)
    _subagent_policy_notifications = service_method('children', Children._subagent_policy_notifications)
    _track_subagent_notification = service_method('children', Children._track_subagent_notification)
    _upsert_subagent_thread = service_method('children', Children._upsert_subagent_thread)
    _track_collab_agent_tool_call = service_method('children', Children._track_collab_agent_tool_call)
    _is_coder_descendant = service_method('children', Children._is_coder_descendant)
    _subagent_depth_from_root = service_method('children', Children._subagent_depth_from_root)
    _subagent_depth = service_method('children', Children._subagent_depth)
    _active_coder_subagents = service_method('children', Children._active_coder_subagents)
    _subagent_summaries = service_method('children', Children._subagent_summaries)
    _enforce_subagent_profile = service_method('children', Children._enforce_subagent_profile)
    _subagent_multi_agent_policy = service_method('children', Children._subagent_multi_agent_policy)
    _multi_agent_config = service_method('children', Children._multi_agent_config)
    _completion_multi_agent_config = service_method('children', Children._completion_multi_agent_config)
    _adversary_multi_agent_config = service_method('children', Children._adversary_multi_agent_config)
    _refresh_coder_subagents = service_method('children', Children._refresh_coder_subagents)
    _refresh_reviewer_subagents = service_method('children', Children._refresh_reviewer_subagents)
    _cleanup_completion_reviewer_descendants = service_method('children', Children._cleanup_completion_reviewer_descendants)
    _cleanup_adversary_reviewer_descendants = service_method('children', Children._cleanup_adversary_reviewer_descendants)
    _cleanup_reviewer_descendants = service_method('children', Children._cleanup_reviewer_descendants)
    _interrupt_subagent = service_method('children', Children._interrupt_subagent)
    _quiesce_coder_tree = service_method('children', Children._quiesce_coder_tree)
    _resume_deferred_completion_if_quiescent = service_method('children', Children._resume_deferred_completion_if_quiescent)
    _register_reviewer_thread = service_method('children', Children._register_reviewer_thread)
    _reviewer_role_for_thread = service_method('children', Children._reviewer_role_for_thread)
    _reviewer_descendant_depth = service_method('children', Children._reviewer_descendant_depth)

    # Completion: sole owner of these legacy fields.
    _accepted_adversary_report = OwnedField('completion', '_accepted_adversary_report')
    _accepted_completion_decision = OwnedField('completion', '_accepted_completion_decision')
    _active_adversary_thread_id = OwnedField('completion', '_active_adversary_thread_id')
    _active_adversary_workspace_root = OwnedField('completion', '_active_adversary_workspace_root')
    _adversary_reservation_recovery_pending = OwnedField('completion', '_adversary_reservation_recovery_pending')
    _completion_knowledge_state = OwnedField('completion', '_completion_knowledge_state')
    _last_completion_marker_sequence = OwnedField('completion', '_last_completion_marker_sequence')
    _no_marker_completion_review_key = OwnedField('completion', '_no_marker_completion_review_key')
    _pending_adversary_report = OwnedField('completion', '_pending_adversary_report')
    _readiness_event_journal = OwnedField('completion', '_readiness_event_journal')
    adv_report_controller = OwnedField('completion', 'adv_report_controller')
    completion_attempt_count = OwnedField('completion', 'completion_attempt_count')
    completion_restarts = OwnedField('completion', 'completion_restarts')
    completion_returns = OwnedField('completion', 'completion_returns')
    completion_review_return_sequence = OwnedField('completion', 'completion_review_return_sequence')
    completion_supervisor = OwnedField('completion', 'completion_supervisor')
    no_marker_idle_nudge_count = OwnedField('completion', 'no_marker_idle_nudge_count')
    provider_failure_recovery_counts = OwnedField('completion', 'provider_failure_recovery_counts')
    _handle_coder_turn_completed = service_method('completion', Completion._handle_coder_turn_completed)
    _continue_after_readiness_marker = service_method('completion', Completion._continue_after_readiness_marker)
    _done_without_fresh_behavioral_validation = service_method('completion', Completion._done_without_fresh_behavioral_validation)
    _steer_for_marker = service_method('completion', Completion._steer_for_marker)
    _handle_no_marker_idle = service_method('completion', Completion._handle_no_marker_idle)
    _handle_completion_review_timeout_failure = service_method('completion', Completion._handle_completion_review_timeout_failure)
    _handle_supervisor_no_message_failure = service_method('completion', Completion._handle_supervisor_no_message_failure)
    apply_completion_decision = service_method('completion', Completion.apply_completion_decision)
    _run_adversary_before_complete = service_method('completion', Completion._run_adversary_before_complete)
    _run_adv_report_controller = service_method('completion', Completion._run_adv_report_controller)
    _adv_report_controller_staleness_reason = service_method('completion', Completion._adv_report_controller_staleness_reason)
    _completion_packet_lifecycle_is_current = service_method('completion', Completion._completion_packet_lifecycle_is_current)
    _record_stale_adversary_discard = service_method('completion', Completion._record_stale_adversary_discard)
    _fail_adv_report_controller = service_method('completion', Completion._fail_adv_report_controller)
    _completion_review_budget_action = service_method('completion', Completion._completion_review_budget_action)
    _finalize_bounded_completion = service_method('completion', Completion._finalize_bounded_completion)
    _fail_required_adversary = service_method('completion', Completion._fail_required_adversary)
    _finalize_completion_review_disabled = service_method('completion', Completion._finalize_completion_review_disabled)
    _finalize_adversary_only = service_method('completion', Completion._finalize_adversary_only)
    _finalize_accepted_completion = service_method('completion', Completion._finalize_accepted_completion)
    _complete_after_adversary_unavailable = service_method('completion', Completion._complete_after_adversary_unavailable)
    _adversary_runs_remaining = service_method('completion', Completion._adversary_runs_remaining)
    _should_run_adversary_before_complete = service_method('completion', Completion._should_run_adversary_before_complete)
    _reserve_adversary_run = service_method('completion', Completion._reserve_adversary_run)
    _record_adversary_limit_reached = service_method('completion', Completion._record_adversary_limit_reached)
    _effective_max_adversary_runs = service_method('completion', Completion._effective_max_adversary_runs)
    _fresh_adversary_report = service_method('completion', Completion._fresh_adversary_report)
    _packet_has_fresh_adversary_report = service_method('completion', Completion._packet_has_fresh_adversary_report)
    _mark_adversary_thread_started = service_method('completion', Completion._mark_adversary_thread_started)
    _mark_adversary_thread_done = service_method('completion', Completion._mark_adversary_thread_done)
    _append_completion_anchor_log = service_method('completion', Completion._append_completion_anchor_log)
    _return_completion_to_coder = service_method('completion', Completion._return_completion_to_coder)
    _completion_knowledge = service_method('completion', Completion._completion_knowledge)
    _behavior_surface_items = service_method('completion', Completion._behavior_surface_items)
    _record_completion_knowledge = service_method('completion', Completion._record_completion_knowledge)
    _readiness_journal = service_method('completion', Completion._readiness_journal)

    # Shutdown: sole owner of these legacy fields.
    _final_report_archived = OwnedField('shutdown', '_final_report_archived')
    _finalizing = OwnedField('shutdown', '_finalizing')
    _snapshot_recovery_path = OwnedField('shutdown', '_snapshot_recovery_path')
    _terminal_cleanup_started = OwnedField('shutdown', '_terminal_cleanup_started')
    _terminal_coder_tree_quiesced = OwnedField('shutdown', '_terminal_coder_tree_quiesced')
    finalize = durable_transition(service_method('shutdown', Shutdown.finalize))
    _apply_final_snapshot_patch_if_needed = service_method('shutdown', Shutdown._apply_final_snapshot_patch_if_needed)
    _preserve_snapshot_for_recovery = service_method('shutdown', Shutdown._preserve_snapshot_for_recovery)
    _archive_final_report_once = service_method('shutdown', Shutdown._archive_final_report_once)
    _prepare_terminal_shutdown = service_method('shutdown', Shutdown._prepare_terminal_shutdown)
    _close_completion_review_session = service_method('shutdown', Shutdown._close_completion_review_session)
    _wake_event_loop_for_shutdown = service_method('shutdown', Shutdown._wake_event_loop_for_shutdown)
    _resolve_pending_approvals = service_method('shutdown', Shutdown._resolve_pending_approvals)
    _stop_supervisor_task = service_method('shutdown', Shutdown._stop_supervisor_task)

    # Runtime Review: sole owner of these legacy fields.
    _active_supervisor_check = OwnedField('runtime_review', '_active_supervisor_check')
    _last_large_diff_signature = OwnedField('runtime_review', '_last_large_diff_signature')
    _last_restart_budget_signature = OwnedField('runtime_review', '_last_restart_budget_signature')
    _last_suspicious_file_signature = OwnedField('runtime_review', '_last_suspicious_file_signature')
    _pending_runtime_trigger_actions = OwnedField('runtime_review', '_pending_runtime_trigger_actions')
    _pending_runtime_trigger_signatures = OwnedField('runtime_review', '_pending_runtime_trigger_signatures')
    _runtime_apply_retry_count = OwnedField('runtime_review', '_runtime_apply_retry_count')
    _runtime_decision_invalidated_completion = OwnedField('runtime_review', '_runtime_decision_invalidated_completion')
    _runtime_decision_retry_count = OwnedField('runtime_review', '_runtime_decision_retry_count')
    _supervisor_dirty = OwnedField('runtime_review', '_supervisor_dirty')
    _supervisor_next_completion_check = OwnedField('runtime_review', '_supervisor_next_completion_check')
    _supervisor_next_completion_review = OwnedField('runtime_review', '_supervisor_next_completion_review')
    _supervisor_next_completion_summary = OwnedField('runtime_review', '_supervisor_next_completion_summary')
    _supervisor_next_runtime_check = OwnedField('runtime_review', '_supervisor_next_runtime_check')
    _supervisor_next_runtime_summary = OwnedField('runtime_review', '_supervisor_next_runtime_summary')
    _supervisor_next_summary = OwnedField('runtime_review', '_supervisor_next_summary')
    _supervisor_task = OwnedField('runtime_review', '_supervisor_task')
    _suspicious_file_hash_cache = OwnedField('runtime_review', '_suspicious_file_hash_cache')
    prior_interventions = OwnedField('runtime_review', 'prior_interventions')
    runtime_triage_config = OwnedField('runtime_review', 'runtime_triage_config')
    runtime_triage_reviewer = OwnedField('runtime_review', 'runtime_triage_reviewer')
    supervisor = OwnedField('runtime_review', 'supervisor')
    _reconcile_intervention_accounting = service_method('runtime_review', RuntimeReview._reconcile_intervention_accounting)
    _schedule_supervisor_check = service_method('runtime_review', RuntimeReview._schedule_supervisor_check)
    _queue_supervisor_check = service_method('runtime_review', RuntimeReview._queue_supervisor_check)
    _sync_legacy_supervisor_queue_fields = service_method('runtime_review', RuntimeReview._sync_legacy_supervisor_queue_fields)
    _cancel_queued_completion_review = service_method('runtime_review', RuntimeReview._cancel_queued_completion_review)
    _deterministic_runtime_noop_reason = service_method('runtime_review', RuntimeReview._deterministic_runtime_noop_reason)
    _runtime_pending_trigger_signatures = service_method('runtime_review', RuntimeReview._runtime_pending_trigger_signatures)
    _runtime_pending_trigger_actions = service_method('runtime_review', RuntimeReview._runtime_pending_trigger_actions)
    _retain_runtime_trigger_summary = service_method('runtime_review', RuntimeReview._retain_runtime_trigger_summary)
    _prepare_runtime_trigger_summary = service_method('runtime_review', RuntimeReview._prepare_runtime_trigger_summary)
    _ack_runtime_trigger_batch = service_method('runtime_review', RuntimeReview._ack_runtime_trigger_batch)
    _update_relevant_edit_state = service_method('runtime_review', RuntimeReview._update_relevant_edit_state)
    should_wake_runtime_supervisor = service_method('runtime_review', RuntimeReview.should_wake_runtime_supervisor)
    _record_runtime_trigger_trace = service_method('runtime_review', RuntimeReview._record_runtime_trigger_trace)
    _declared_grading_access_issue = service_method('runtime_review', RuntimeReview._declared_grading_access_issue)
    _update_runtime_metrics = service_method('runtime_review', RuntimeReview._update_runtime_metrics)
    _record_supervisor_decision_metric = service_method('runtime_review', RuntimeReview._record_supervisor_decision_metric)
    _record_approval_metric = service_method('runtime_review', RuntimeReview._record_approval_metric)
    _supervisor_check_loop = durable_transition(service_method('runtime_review', RuntimeReview._supervisor_check_loop))
    _run_supervisor_check = service_method('runtime_review', RuntimeReview._run_supervisor_check)
    _resume_idle_after_runtime_noop = service_method('runtime_review', RuntimeReview._resume_idle_after_runtime_noop)
    _runtime_noop_idle_context_is_current = service_method('runtime_review', RuntimeReview._runtime_noop_idle_context_is_current)
    _resume_readiness_after_runtime_noop = service_method('runtime_review', RuntimeReview._resume_readiness_after_runtime_noop)
    _runtime_noop_readiness_context_is_current = service_method('runtime_review', RuntimeReview._runtime_noop_readiness_context_is_current)
    _readiness_snapshot_has_new_invalidating_event = service_method('runtime_review', RuntimeReview._readiness_snapshot_has_new_invalidating_event)
    _completion_payload_window = service_method('runtime_review', RuntimeReview._completion_payload_window)
    _record_runtime_intervention = service_method('runtime_review', RuntimeReview._record_runtime_intervention)
    apply_supervisor_decision = service_method('runtime_review', RuntimeReview.apply_supervisor_decision)
    _configure_runtime_triage = service_method('runtime_review', RuntimeReview._configure_runtime_triage)
    _cheap_runtime_structured_output_self_test = service_method('runtime_review', RuntimeReview._cheap_runtime_structured_output_self_test)
    _cheap_runtime_route = service_method('runtime_review', RuntimeReview._cheap_runtime_route)
    _record_cheap_runtime_attempt = service_method('runtime_review', RuntimeReview._record_cheap_runtime_attempt)

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
        runtime_enabled: bool | None = None,
        async_tools: bool | None = None,
        log_distiller: LogDistillerConfig | None = None,
        declared_grading_roots: list[str | Path] | tuple[str | Path, ...] | None = None,
        project_config: ProjectConfig | None = None,
        recovery_enabled: bool = True,
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
        self.store = StateStore(self.project_root, create=False)
        self.recovery_enabled = recovery_enabled
        self._durable_run: DurableRun | None = None
        self._recovery_observed_items: dict[str, dict[str, Any]] = {}
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
        self.runtime_enabled = runtime_enabled
        self.async_tools = async_tools
        self.log_distiller = log_distiller
        self.project_config = project_config
        self.event_queue: asyncio.Queue[ControllerEvent] = asyncio.Queue()
        self.client = client or RuntimeClient(
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
        with RunOwner(self.project_root, controller=True):
            # Even an explicitly nonrecovering run records its disposition so
            # the next invocation cannot silently reset unfinished work.
            self._durable_run = DurableRun(self)
            await self._run_owned()


    async def _run_owned(self) -> None:
        self.initialize_state()
        self._write_run_checkpoint("startup", state="active")
        try:
            configure_run = getattr(self.client, "configure_run", None)
            if callable(configure_run):
                configure_run(
                    runtime_enabled=self._runtime_enabled(),
                    async_tools=self._async_tools_enabled(),
                    windows_native_root_read=self._windows_native_root_read_enabled(),
                    log_distiller=self._log_distiller_config(),
                )
            await self.client.start()
            await self.client.initialize()
            await self.tui.start()
            self.running = True
            self.tui.render("SYSTEM", self._runtime_settings_summary())
            if self._durable_run is None or not self._durable_run.restored:
                self._prepare_coder_workspace()
            self._write_run_checkpoint("coder_workspace", state="stable")
            await self.preflight()
            self._durable_run.verify_engine_selection()
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
            ) if self._runtime_enabled() else None
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
            ) if self._effective_completion_review() else None
            self.adv_report_controller = StatelessSupervisorAgent(
                self.client,
                self.store,
                self.task_path,
                workspace_root=self._active_workspace_root(),
                task_contents=self._canonical_task_contents,
                model=self._completion_model() if self._effective_completion_review() else self._adversary_model(),
                fast=self._fast_mode(),
                intelligence=self._completion_intelligence() if self._effective_completion_review() else self._adversary_intelligence(),
                completion_source_snapshot=getattr(self, "_coder_snapshot", None),
                on_thread_start=lambda thread_id: self._register_reviewer_thread(
                    thread_id,
                    role="adv_report_controller",
                ),
            ) if self._adversary_model_required_for_preflight() else None
            self.approvals = ApprovalManager(
                self._active_workspace_root(),
                supervisor=self if self._runtime_enabled() else None,
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
                readonly_roots=self._active_dependency_roots(),
            )
            if self._durable_run is not None and self._durable_run.restored:
                await resume_coder(self)
            else:
                await self.coder.start_thread()
                self._coder_started = True
                await self.coder.start_initial_turn()
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": BelloStatus.RUNNING}))
            self._active_provider_phase = "coder"
            self._write_run_checkpoint("coder", state="active")
            self.tui.status("supervised coder started")
            await self.event_loop()
        except RecoveryBlocked:
            # A recovery refusal must not detach or reset trusted old state.
            # The caller reports the error and leaves all artifacts inspectable.
            raise
        except (AppServerError, SupervisorAgentError) as exc:
            await self.fail_provider(f"app-server RPC failed: {exc}")
        except WorkspaceSnapshotError as exc:
            await self.fail_provider(f"run infrastructure failed: {exc}")
        except Exception as exc:
            # An unexpected local failure must not leave a dead run advertised
            # as running. Preserve the unaccepted snapshot via the existing
            # failure path, then re-raise so CLI callers retain a nonzero exit
            # and the original traceback. Cancellation/KeyboardInterrupt are
            # BaseExceptions and deliberately keep their existing behavior.
            detail = f"run infrastructure failed: {type(exc).__name__}: {sanitize_error_text(str(exc))}"
            try:
                await self.fail_provider(detail)
                self.pending_approvals.clear()
                if self.coder is not None:
                    self.coder.active_turn_id = None
                self.store.update_bello_config(
                    lambda cfg: cfg.model_copy(update={
                        "active_coder_turn_id": None,
                        "pending_server_request_ids": [],
                    })
                )
                self._write_run_checkpoint("terminal", state="terminal", detail=detail)
            except Exception as finalization_error:
                exc.add_note(f"Failure finalization also failed: {type(finalization_error).__name__}")
            raise
        finally:
            self.running = False
            await self._stop_supervisor_task()
            await self._close_completion_review_session()
            snapshot = getattr(self, "_coder_snapshot", None)
            preserve_live = bool(
                self._durable_run is not None
                and self.store.get_bello_config().status in {
                    BelloStatus.STARTING, BelloStatus.RUNNING, BelloStatus.PAUSED, BelloStatus.RESTARTING,
                }
            )
            if snapshot is not None and not getattr(self, "_snapshot_patch_applied", False) and not preserve_live:
                if getattr(self, "_coder_started", False):
                    await self._preserve_snapshot_for_recovery(snapshot, reason="unhandled_shutdown")
                else:
                    snapshot.cleanup()
                    self._coder_snapshot = None
            await self.tui.stop()
            await self.client.stop()
            if preserve_live and snapshot is not None:
                snapshot.close_windows_runtime_controls()
                # A successful stop cannot prove that arbitrary detached native
                # descendants died. Keep the prior disposition and require the
                # guardian's full-tree fencing receipt on every continuation.


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
            cheap_runtime=self._runtime_enabled() and project_config.cheap_runtime,
            runtime_enabled=self._runtime_enabled(),
            async_tools=self._async_tools_enabled(),
            windows_native_root_read=self._windows_native_root_read_enabled(),
            log_distiller=self._log_distiller_config().to_json_data(),
            multi_agent=project_config.multi_agent.to_json_data(),
            completion_multi_agent=project_config.completion_multi_agent.to_json_data(),
            adversary_multi_agent=project_config.adversary_multi_agent.to_json_data(),
        )
        mode = "fresh" if self.overwrite_state else "resume"
        owner = self._durable_run
        restored = owner.initialize(config, fresh=self.overwrite_state) if owner is not None else False
        if not restored:
            if self.clean_workspace:
                clean_preserved_paths: tuple[str | Path, ...] = (
                    *self.declared_grading_roots, self.store.state_dir,
                )
                if self.plan_path is not None:
                    clean_preserved_paths = (*clean_preserved_paths, self.plan_path)
                clean_workspace_except_task(self.project_root, self.task_path, protected_paths=clean_preserved_paths)
            self.store.initialize_bello(config, mode=mode)
            if owner is not None:
                owner.reset_new_run()
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
        owner = getattr(self, "_durable_run", None)
        if owner is not None:
            # Required safety state: unlike the human-readable diagnostic below,
            # a failed durable write must stop the run before another action.
            owner.checkpoint(phase)
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
                    "cheap_runtime": self._runtime_enabled() and project_config.cheap_runtime,
                    "runtime_enabled": self._runtime_enabled(),
                    "async_tools": self._async_tools_enabled(),
                    "windows_native_root_read": self._windows_native_root_read_enabled(),
                    "log_distiller": self._log_distiller_config().to_json_data(),
                    "multi_agent": project_config.multi_agent.to_json_data(),
                    "completion_multi_agent": project_config.completion_multi_agent.to_json_data(),
                    "adversary_multi_agent": project_config.adversary_multi_agent.to_json_data(),
                }
            )
        )


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
            # Quota/status notifications must not indefinitely postpone the
            # active-turn diagnostic just because the event queue stays busy.
            now = time.monotonic()
            if now - getattr(self, "_last_active_guard_tick", float("-inf")) >= CONTROLLER_IDLE_GUARD_INTERVAL_SECONDS:
                self._last_active_guard_tick = now
                await self._handle_active_coder_guard(now=now)


    @durable_transition
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


    @durable_transition
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
        if not self._runtime_enabled():
            await self._deliver_coder_message(command.text)
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
        if not self._runtime_enabled():
            # The runtime grants network inside each assigned filesystem sandbox.
            # An explicit sandbox escape needs separate user authority, which this
            # unattended approval protocol does not provide. Do not call any model
            # or auto-approve a host-wide command as a substitute.
            manager = ApprovalManager(self._active_workspace_root())
            resolution = manager._deny(context, "outside-sandbox approval is unavailable while runtime supervision is disabled")
            response = manager.response_payload(context, resolution)
        elif is_adversary_request:
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
            elif self.coder is not None and self._runtime_enabled():
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
        if not self._runtime_enabled() or self.supervisor is None:
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
        current_coder_turn = bool(thread_id == cfg.coder_thread_id and turn_id
                                  and turn_id == cfg.active_coder_turn_id)
        if current_coder_turn:
            self._record_coder_progress(method, params, cfg)
        if _is_stream_delta_method(method):
            # Completion/adversary commands are deliberately outside the coder evidence
            # ledger. Do not retain their potentially large output chunks waiting for a
            # coder item/completed event that can never consume them.
            if thread_id == cfg.coder_thread_id or self._is_coder_descendant(thread_id, cfg=cfg):
                self._record_command_output_delta(method, params, item_id=item_id)
            return
        event_payload = (bounded_provider_error(params) if method == "error"
                         else _bounded_subagent_event_payload(method, params))
        if self._exposes_review_private_input(event_payload):
            event_payload.pop("prompt", None)
        self._append_event(
            AppEventSource.APP_SERVER,
            method,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            reason=event_payload.get("error", {}).get("message") if method == "error" else None,
            payload=event_payload,
        )
        if method == "item/completed" and thread_id == cfg.coder_thread_id and isinstance(turn_id, str):
            item = params.get("item")
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                if not hasattr(self, "_recovery_observed_items"):
                    self._recovery_observed_items = {}
                self._recovery_observed_items[observed_item_key(thread_id, turn_id, item["id"])] = observed_item_value(item)
        if getattr(self, "_terminal_cleanup_started", False) and method != "serverRequest/resolved":
            return

        lifecycle_accepts_activity = self._coder_lifecycle_accepts_activity(cfg)

        if method == "error":
            # Reviewer/old-turn errors remain journalled but may not terminate
            # the current coder. Missing willRetry is unknown, never False.
            if not lifecycle_accepts_activity or not current_coder_turn:
                return
            error = event_payload.get("error", {})
            detail = error.get("message") if isinstance(error, dict) else None
            detail = detail or "provider sent an error without a message"
            retry = event_payload.get("willRetry")
            if retry is False:
                await self.fail_provider(f"coder execution failed: {detail}")
            else:
                watch = self._active_coder_watch()
                if watch is not None:
                    if retry is True and watch.retry_since is None:
                        watch.retry_since = time.monotonic()
                    watch.last_error = detail
                self.tui.render("SYSTEM", f"Coder provider {'is retrying' if retry is True else 'reported an error'}: {detail}")
            return
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
            self._active_coder_watch()
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
            completion_key = (cfg.generation, thread_id, turn_id)
            seen_completions = getattr(self, "_handled_coder_completions", None)
            if seen_completions is None:
                seen_completions = deque(maxlen=128)
                self._handled_coder_completions = seen_completions
            if completion_key in seen_completions or (
                    cfg.active_coder_turn_id and turn_id != cfg.active_coder_turn_id):
                return
            seen_completions.append(completion_key)
            turn = params.get("turn", {})
            if isinstance(turn, dict) and turn.get("status") == "failed":
                detail = bounded_provider_error({"error": turn.get("error")}).get("error", {})
                await self.fail_provider(f"coder execution failed: {detail or 'provider returned a failed turn'}")
                return
            if self.coder and isinstance(turn_id, str):
                self.coder.mark_turn_completed(turn_id)
            self._write_run_checkpoint("coder_turn_complete", state="stable")
            await self._handle_coder_turn_completed(item_id=item_id)
            return
        if method in {"turn/completed", "thread/status/changed", "thread/closed"}:
            await self._resume_deferred_completion_if_quiescent()


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
