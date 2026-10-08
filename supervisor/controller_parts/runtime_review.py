"""RuntimeReview service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class RuntimeReviewState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _active_supervisor_check: compat.QueuedSupervisorCheck | None
    _last_large_diff_signature: str | None
    _last_restart_budget_signature: str | None
    _last_suspicious_file_signature: str | None
    _pending_runtime_trigger_actions: dict[str, compat.TriggeringAction]
    _pending_runtime_trigger_signatures: dict[str, tuple[str | None, str | None]]
    _runtime_apply_retry_count: int
    _runtime_decision_invalidated_completion: bool
    _runtime_decision_retry_count: int
    _supervisor_dirty: bool
    _supervisor_next_completion_check: compat.QueuedSupervisorCheck | None
    _supervisor_next_completion_review: bool
    _supervisor_next_completion_summary: str | None
    _supervisor_next_runtime_check: compat.QueuedSupervisorCheck | None
    _supervisor_next_runtime_summary: str | None
    _supervisor_next_summary: str | None
    _supervisor_task: compat.asyncio.Task[None] | None
    _suspicious_file_hash_cache: dict[str, tuple[tuple[Any, ...], str]]
    prior_interventions: list[compat.PriorIntervention]
    runtime_triage_config: compat.CheapRuntimeTriageConfig
    runtime_triage_reviewer: compat.CheapRuntimeReviewer | None
    supervisor: compat.StatelessSupervisorAgent | None


class RuntimeReviewPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_ack_runtime_trigger_batch',
        '_active_coder_subagents',
        '_active_workspace_root',
        '_adversary_runs_remaining',
        '_append_event',
        '_behavior_surface_items',
        '_cancel_queued_completion_review',
        '_canonical_task_text',
        '_cheap_runtime_enabled',
        '_cheap_runtime_route',
        '_cheap_runtime_structured_output_self_test',
        '_close_completion_review_session',
        '_coder_lifecycle_accepts_activity',
        '_completion_knowledge',
        '_completion_payload_window',
        '_completion_review_budget_action',
        '_continue_after_readiness_marker',
        '_deferred_completion_check',
        '_deliver_coder_message',
        '_deterministic_runtime_noop_reason',
        '_effective_completion_review',
        '_effective_max_adversary_runs',
        '_exposes_review_private_input',
        '_finalize_adversary_only',
        '_finalize_bounded_completion',
        '_finalizing',
        '_fresh_adversary_report',
        '_handle_completion_review_timeout_failure',
        '_handle_no_marker_idle',
        '_handle_supervisor_no_message_failure',
        '_immutable_approval_paths',
        '_is_coder_descendant',
        '_last_completion_marker_sequence',
        '_mark_controller_activity',
        '_post_coder_review_agent',
        '_prepare_runtime_trigger_summary',
        '_queue_supervisor_check',
        '_readiness_journal',
        '_readiness_snapshot_has_new_invalidating_event',
        '_reconcile_intervention_accounting',
        '_record_cheap_runtime_attempt',
        '_record_runtime_intervention',
        '_record_supervisor_decision_metric',
        '_refresh_coder_subagents',
        '_resume_idle_after_runtime_noop',
        '_resume_readiness_after_runtime_noop',
        '_retain_runtime_trigger_summary',
        '_review_safe_packet_state',
        '_review_safe_values',
        '_reviewer_role_for_thread',
        '_run_adversary_before_complete',
        '_run_supervisor_check',
        '_runtime_enabled',
        '_runtime_noop_idle_context_is_current',
        '_runtime_noop_readiness_context_is_current',
        '_runtime_pending_trigger_actions',
        '_runtime_pending_trigger_signatures',
        '_sequence',
        '_subagent_summaries',
        '_supervisor_check_loop',
        '_sync_legacy_supervisor_queue_fields',
        '_terminal_cleanup_started',
        '_transport_error_pending',
        '_update_runtime_metrics',
        '_write_run_checkpoint',
        'apply_completion_decision',
        'apply_supervisor_decision',
        'approvals',
        'changed_files',
        'client',
        'coder',
        'completion_attempt_count',
        'completion_packet_details',
        'completion_returns',
        'completion_review_return_sequence',
        'declared_grading_roots',
        'diff_summary',
        'finalize',
        'inspections',
        'last_coder_message',
        'no_marker_idle_nudge_count',
        'patch_summary',
        'pause',
        'paused',
        'pending_approvals',
        'provider_failure_recovery_counts',
        'restart',
        'running',
        'store',
        'task_path',
        'tui',
        'validations',
    })
    writes = frozenset({
        '_deferred_completion_check',
        'completion_attempt_count',
        'provider_failure_recovery_counts',
    })


class RuntimeReview:
    """Own runtime review behavior; borrow only the declared port."""

    def __init__(self, ports: RuntimeReviewPort) -> None:
        self.state = RuntimeReviewState()
        self.ports = ports

    def _reconcile_intervention_accounting(self) -> None:
        prior = getattr(self.state, "prior_interventions", None)
        if not prior:
            return
        target = sum(1 for record in prior if compat._prior_record_counts_as_health_intervention(record))

        def patch(current):
            if current.interventions >= target:
                return current
            return current.model_copy(update={"interventions": target})

        self.ports.store.patch_health(patch)

    def _schedule_supervisor_check(
        self,
        summary: str,
        *,
        triggering_item_id: str | None = None,
        triggering_action: compat.TriggeringAction | None = None,
        human_message: compat.HumanMessage | None = None,
        patch_summary: str | None = None,
        completion_review: bool = False,
    ) -> None:
        if not completion_review and not self.ports._runtime_enabled():
            return
        if (
            not self.ports.running
            or getattr(self.ports, "paused", False)
            or getattr(self.ports, "_finalizing", False)
            or getattr(self.ports, "_terminal_cleanup_started", False)
            or (self.ports._post_coder_review_agent() if completion_review else getattr(self.state, "supervisor", None)) is None
        ):
            return
        if not completion_review:
            self.ports._retain_runtime_trigger_summary(summary, triggering_action=triggering_action)
        if self.state._supervisor_task and not self.state._supervisor_task.done():
            self.ports._queue_supervisor_check(
                summary,
                triggering_item_id=triggering_item_id,
                triggering_action=triggering_action,
                human_message=human_message,
                patch_summary=patch_summary,
                completion_review=completion_review,
            )
            return
        self.state._supervisor_task = compat.asyncio.create_task(
            self.ports._supervisor_check_loop(
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
        triggering_action: compat.TriggeringAction | None = None,
        human_message: compat.HumanMessage | None = None,
        patch_summary: str | None = None,
        completion_review: bool,
    ) -> None:
        if not completion_review and not self.ports._runtime_enabled():
            return
        self.state._supervisor_dirty = True
        queued = compat.QueuedSupervisorCheck(
            summary=summary,
            triggering_item_id=triggering_item_id,
            triggering_action=triggering_action,
            human_message=human_message,
            patch_summary=patch_summary,
            completion_review=completion_review,
        )
        if completion_review:
            self.state._supervisor_next_completion_check = queued
            self.state._supervisor_next_completion_summary = summary
        else:
            self.ports._retain_runtime_trigger_summary(summary, triggering_action=triggering_action)
            existing = getattr(self.state, "_supervisor_next_runtime_check", None)
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
            self.state._supervisor_next_runtime_check = queued
            self.state._supervisor_next_runtime_summary = queued.summary
        self.ports._sync_legacy_supervisor_queue_fields()

    def _sync_legacy_supervisor_queue_fields(self) -> None:
        """Keep the old single-slot fields coherent for diagnostics and old state tests."""
        runtime_summary = getattr(self.state, "_supervisor_next_runtime_summary", None)
        completion_summary = getattr(self.state, "_supervisor_next_completion_summary", None)
        self.state._supervisor_next_summary = runtime_summary or completion_summary
        self.state._supervisor_next_completion_review = bool(
            completion_summary is not None and runtime_summary is None
        )

    def _cancel_queued_completion_review(self) -> None:
        self.state._supervisor_next_completion_check = None
        self.state._supervisor_next_completion_summary = None
        self.ports._sync_legacy_supervisor_queue_fields()

    def _deterministic_runtime_noop_reason(
        self,
        *,
        reasons: list[str],
    ) -> str | None:
        if not reasons:
            return None
        reason_set = set(reasons)
        if reason_set & compat.PROTECTED_RUNTIME_WAKE_REASONS:
            return None
        if reason_set == {"nonzero_exit"}:
            return "first isolated nonzero exit"
        return None

    def _runtime_pending_trigger_signatures(self) -> dict[str, tuple[str | None, str | None]]:
        pending = getattr(self.state, "_pending_runtime_trigger_signatures", None)
        if pending is None:
            pending = {}
            self.state._pending_runtime_trigger_signatures = pending
        return pending

    def _runtime_pending_trigger_actions(self) -> dict[str, compat.TriggeringAction]:
        pending = getattr(self.state, "_pending_runtime_trigger_actions", None)
        if pending is None:
            pending = {}
            self.state._pending_runtime_trigger_actions = pending
        return pending

    def _retain_runtime_trigger_summary(
        self,
        summary: str,
        *,
        triggering_action: compat.TriggeringAction | None = None,
        replace_existing: bool = True,
    ) -> None:
        """Keep every routed runtime reason alive until a runtime pass consumes it."""
        reasons = list(compat._runtime_trigger_reasons_from_summary(summary))
        if summary.lstrip().startswith("Runtime integrity trigger:"):
            reasons.append("runtime_control_replacement")
        if not reasons:
            return
        pending = self.ports._runtime_pending_trigger_signatures()
        pending_actions = self.ports._runtime_pending_trigger_actions()
        action_signature = (
            compat._runtime_action_signature(triggering_action)
            if triggering_action is not None
            else None
        )
        for reason in dict.fromkeys(reasons):
            if reason == "restart_budget":
                candidate, restart_reason = compat.kill_restart_candidate(self.ports.store.get_health())
                signature = (
                    compat._restart_budget_signature(self.ports.store.get_health(), restart_reason)
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
        existing_reasons = list(compat._runtime_trigger_reasons_from_summary(summary))
        reasons = list(dict.fromkeys((*existing_reasons, *pending.keys())))
        if not reasons:
            return summary

        prepared_summary = summary
        if pending:
            match = compat.re.match(r"\s*Runtime trigger \([^)]*\):\s*", summary)
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
        pending = self.ports._runtime_pending_trigger_signatures()
        for reason, entry in pending_batch.items():
            current_entry = pending.get(reason)
            if current_entry is None:
                # The condition cleared while this review was in flight. Do not stamp its
                # old signature as handled; a later recurrence must be treated as new state.
                continue
            signature = entry[0]
            if reason == "large_diff" and signature is not None:
                self.state._last_large_diff_signature = signature
            elif reason == "suspicious_file_touched" and signature is not None:
                self.state._last_suspicious_file_signature = signature
            elif reason == "restart_budget" and signature is not None:
                self.state._last_restart_budget_signature = signature
            if current_entry == entry:
                pending.pop(reason, None)
                self.ports._runtime_pending_trigger_actions().pop(reason, None)
        queued = getattr(self.state, "_supervisor_next_runtime_check", None)
        if queued is not None:
            queued_reasons = compat._runtime_trigger_reasons_from_summary(queued.summary)
            remaining_reasons = list(pending)
            if queued_reasons and not remaining_reasons:
                self.state._supervisor_next_runtime_check = None
                self.state._supervisor_next_runtime_summary = None
                self.ports._sync_legacy_supervisor_queue_fields()
            elif queued_reasons and tuple(remaining_reasons) != queued_reasons:
                match = compat.re.match(r"\s*Runtime trigger \([^)]*\):\s*", queued.summary)
                detail = queued.summary[match.end() :] if match else queued.summary
                updated_summary = f"Runtime trigger ({', '.join(remaining_reasons)}): {detail}"
                carrier_action = next(
                    (
                        self.ports._runtime_pending_trigger_actions().get(reason)
                        for reason in remaining_reasons
                        if self.ports._runtime_pending_trigger_actions().get(reason) is not None
                    ),
                    queued.triggering_action,
                )
                self.state._supervisor_next_runtime_check = compat.QueuedSupervisorCheck(
                    summary=updated_summary,
                    triggering_item_id=queued.triggering_item_id,
                    triggering_action=carrier_action,
                    human_message=queued.human_message,
                    patch_summary=queued.patch_summary,
                    completion_review=False,
                )
                self.state._supervisor_next_runtime_summary = updated_summary
                self.ports._sync_legacy_supervisor_queue_fields()

    def _update_relevant_edit_state(self, changed_files: list[compat.ChangedFile]) -> None:
        task_contents = self.ports._canonical_task_text()
        relevant_sequences = [
            changed.sequence
            for changed in changed_files
            if changed.sequence is not None and compat._is_relevant_changed_path(changed.path, task_contents=task_contents)
        ]
        if not relevant_sequences:
            return
        latest = max(relevant_sequences)

        def patch(current: compat.BelloConfig) -> compat.BelloConfig:
            existing = current.last_relevant_edit_sequence
            if existing is not None and existing >= latest:
                return current
            return current.model_copy(update={"last_relevant_edit_sequence": latest})

        self.ports.store.update_bello_config(patch)

    def should_wake_runtime_supervisor(
        self,
        *,
        action: compat.TriggeringAction,
        validation: compat.ValidationRun | None,
        changed_files: list[compat.ChangedFile],
        validation_trigger_reasons: tuple[str, ...] = (),
    ) -> compat.RuntimeTriggerDecision:
        if not self.ports._runtime_enabled():
            return compat.RuntimeTriggerDecision(should_wake=False, reasons=())
        reasons: list[str] = list(validation_trigger_reasons)
        read_only_action = bool(action.command and compat._is_read_only_inspection_command(action.command))
        if (
            action.exit_code is not None
            and action.exit_code != 0
            and not (
                action.command
                and compat._is_read_only_inspection_command(action.command)
                and compat._inspection_exit_is_usable(action.command, action.exit_code)
            )
        ):
            reasons.append("nonzero_exit")
        if compat._action_timed_out(action):
            reasons.append("timeout")
        large_diff_signature = compat._large_diff_signature(changed_files) if compat._has_large_diff(changed_files) else None
        large_diff_is_new = bool(
            large_diff_signature is not None
            and large_diff_signature != getattr(self.state, "_last_large_diff_signature", None)
            and large_diff_signature
            != self.ports._runtime_pending_trigger_signatures().get("large_diff", (None, None))[0]
        )
        if large_diff_is_new and not read_only_action:
            reasons.append("large_diff")
            self.ports._runtime_pending_trigger_signatures()["large_diff"] = (large_diff_signature, None)
        suspicious_file_hash_cache = getattr(self.state, "_suspicious_file_hash_cache", None)
        if suspicious_file_hash_cache is None:
            suspicious_file_hash_cache = self.state._suspicious_file_hash_cache = {}
        suspicious_file_signature = compat._suspicious_changed_file_signature(
            self.ports._active_workspace_root(),
            changed_files,
            cache=suspicious_file_hash_cache,
        )
        if suspicious_file_signature is None:
            self.state._last_suspicious_file_signature = None
            self.ports._runtime_pending_trigger_signatures().pop("suspicious_file_touched", None)
            self.ports._runtime_pending_trigger_actions().pop("suspicious_file_touched", None)
        elif suspicious_file_signature != getattr(self.state, "_last_suspicious_file_signature", None):
            pending_suspicious = self.ports._runtime_pending_trigger_signatures().get(
                "suspicious_file_touched", (None, None)
            )[0]
            if suspicious_file_signature != pending_suspicious:
                reasons.append("suspicious_file_touched")
                self.ports._runtime_pending_trigger_signatures()["suspicious_file_touched"] = (
                    suspicious_file_signature,
                    None,
                )
        restart_candidate, restart_reason = compat.kill_restart_candidate(self.ports.store.get_health())
        restart_signature = (
            compat._restart_budget_signature(self.ports.store.get_health(), restart_reason)
            if restart_candidate and restart_reason
            else None
        )
        if restart_signature is None:
            self.state._last_restart_budget_signature = None
            self.ports._runtime_pending_trigger_signatures().pop("restart_budget", None)
            self.ports._runtime_pending_trigger_actions().pop("restart_budget", None)
        if (
            restart_signature is not None
            and restart_signature != getattr(self.state, "_last_restart_budget_signature", None)
            and restart_signature
            != self.ports._runtime_pending_trigger_signatures().get("restart_budget", (None, None))[0]
        ):
            reasons.append("restart_budget")
            self.ports._runtime_pending_trigger_signatures()["restart_budget"] = (
                restart_signature,
                restart_reason,
            )
        reasons = list(dict.fromkeys(reasons))
        if self.ports._deterministic_runtime_noop_reason(
            reasons=reasons,
        ):
            return compat.RuntimeTriggerDecision(should_wake=False, reasons=())
        return compat.RuntimeTriggerDecision(
            should_wake=bool(reasons),
            reasons=tuple(reasons),
            restart_reason=restart_reason if "restart_budget" in reasons else None,
        )

    def _record_runtime_trigger_trace(
        self,
        *,
        event_type: str,
        action: compat.TriggeringAction | None,
        validation: compat.ValidationRun | None,
        changed_files: list[compat.ChangedFile],
        decision: compat.RuntimeTriggerDecision,
    ) -> None:
        additions, deletions = compat._diff_line_counts(changed_files)
        suspicious_paths = [changed.path for changed in changed_files if compat._is_suspicious_changed_path(changed.path)]
        private_input_action = (
            action is not None and self.ports._exposes_review_private_input(action)
        )
        trace = {
            "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
            "event_sequence": getattr(self.ports, "_sequence", None),
            "generation": self.ports.store.get_bello_config().generation,
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
        self.ports.store.append_runtime_trace(trace)
        self.ports._update_runtime_metrics(trace)

    def _declared_grading_access_issue(self, action: compat.TriggeringAction) -> str | None:
        roots = getattr(self.ports, "declared_grading_roots", ())
        if not roots:
            return None
        payload: dict[str, compat.Any] = {}
        if action.command:
            payload["command"] = action.command
        if action.cwd:
            payload["cwd"] = action.cwd
        if action.paths:
            payload["paths"] = action.paths
        if not payload:
            return None
        manager = getattr(self.ports, "approvals", None)
        if manager is None:
            manager = compat.ApprovalManager(
                self.ports._active_workspace_root(),
                declared_grading_roots=roots,
                immutable_paths=self.ports._immutable_approval_paths(),
            )
        decision = manager.policy.evaluate(payload)
        if decision.kind.value != "deny" or "declared grading/hidden path access denied" not in decision.reason:
            return None
        command = f" command `{action.command}`" if action.command else ""
        return f"coder accessed declared grading/hidden path via{command}: {decision.reason}"

    def _update_runtime_metrics(self, trace: dict[str, compat.Any]) -> None:
        reasons = trace.get("trigger_reasons")
        if not isinstance(reasons, list):
            reasons = []

        def patch(current: dict[str, compat.Any]) -> dict[str, compat.Any]:
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

        self.ports.store.update_runtime_metrics(patch)

    def _record_supervisor_decision_metric(self, *, use_case: str, decision: str) -> None:
        def patch(current: dict[str, compat.Any]) -> dict[str, compat.Any]:
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

        self.ports.store.update_runtime_metrics(patch)

    def _record_approval_metric(self, *, decision: str, from_supervisor: bool) -> None:
        def patch(current: dict[str, compat.Any]) -> dict[str, compat.Any]:
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

        self.ports.store.update_runtime_metrics(patch)

    async def _supervisor_check_loop(
        self,
        summary: str,
        triggering_item_id: str | None,
        triggering_action: compat.TriggeringAction | None,
        human_message: compat.HumanMessage | None,
        patch_summary: str | None,
        completion_review: bool,
    ) -> None:
        while True:
            self.state._supervisor_dirty = False
            self.state._runtime_decision_invalidated_completion = False
            active_check = compat.QueuedSupervisorCheck(
                summary=summary,
                triggering_item_id=triggering_item_id,
                triggering_action=triggering_action,
                human_message=human_message,
                patch_summary=patch_summary,
                completion_review=completion_review,
            )
            self.state._active_supervisor_check = active_check
            phase = "completion_review" if completion_review else "runtime_review"
            self.ports._write_run_checkpoint(phase, state="active")
            try:
                await self.ports._run_supervisor_check(
                    summary,
                    triggering_item_id,
                    triggering_action,
                    human_message,
                    patch_summary,
                    completion_review,
                )
            finally:
                if self.state._active_supervisor_check is active_check:
                    self.state._active_supervisor_check = None
            self.ports._mark_controller_activity()
            if (
                not self.ports.running
                or getattr(self.ports, "paused", False)
                or getattr(self.ports, "_finalizing", False)
            ):
                return
            if not completion_review and getattr(
                self.state,
                "_runtime_decision_invalidated_completion",
                False,
            ):
                self.ports._cancel_queued_completion_review()
            runtime_summary = getattr(self.state, "_supervisor_next_runtime_summary", None)
            completion_summary = getattr(self.state, "_supervisor_next_completion_summary", None)
            if runtime_summary is not None:
                queued = getattr(self.state, "_supervisor_next_runtime_check", None) or compat.QueuedSupervisorCheck(
                    summary=runtime_summary,
                    completion_review=False,
                )
                self.state._supervisor_next_runtime_check = None
                self.state._supervisor_next_runtime_summary = None
            elif completion_summary is not None:
                queued = getattr(
                    self.state,
                    "_supervisor_next_completion_check",
                    None,
                ) or compat.QueuedSupervisorCheck(
                    summary=completion_summary,
                    completion_review=True,
                )
                self.state._supervisor_next_completion_check = None
                self.state._supervisor_next_completion_summary = None
            else:
                self.ports._sync_legacy_supervisor_queue_fields()
                return
            self.ports._sync_legacy_supervisor_queue_fields()
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
        triggering_action: compat.TriggeringAction | None,
        human_message: compat.HumanMessage | None,
        patch_summary: str | None,
        completion_review: bool = False,
    ) -> None:
        if not completion_review and not self.ports._runtime_enabled():
            return
        if not self.ports._coder_lifecycle_accepts_activity():
            if completion_review:
                await self.ports._close_completion_review_session()
            return
        if completion_review:
            await self.ports._refresh_coder_subagents()
            active_subagents = self.ports._active_coder_subagents()
            if active_subagents:
                self.ports._deferred_completion_check = compat.QueuedSupervisorCheck(
                    summary=summary,
                    triggering_item_id=triggering_item_id,
                    triggering_action=triggering_action,
                    human_message=human_message,
                    patch_summary=patch_summary,
                    completion_review=True,
                )
                self.ports._append_event(
                    compat.AppEventSource.SUPERVISOR,
                    "completion/deferred_for_subagents",
                    reason="completion snapshot deferred until coder descendants are quiescent",
                )
                self.ports.tui.render(
                    "SUPERVISOR",
                    f"completion deferred: {len(active_subagents)} subagent(s) still active",
                )
                return
        runtime_trigger_batch: dict[str, tuple[str | None, str | None]] = {}
        if not completion_review:
            self.ports._retain_runtime_trigger_summary(
                summary,
                triggering_action=triggering_action,
                replace_existing=False,
            )
            runtime_trigger_batch = dict(self.ports._runtime_pending_trigger_signatures())
            runtime_trigger_actions = compat._unique_runtime_trigger_actions(
                self.ports._runtime_pending_trigger_actions().get(reason)
                for reason in runtime_trigger_batch
            )
        else:
            runtime_trigger_actions = []
        agent = self.ports._post_coder_review_agent() if completion_review else self.state.supervisor
        if agent is None:
            return
        self.ports._reconcile_intervention_accounting()
        cfg = self.ports.store.get_bello_config()
        wake_sequence = cfg.last_event_sequence + 1
        changed_files = await self.ports.changed_files()
        packet_validations = list(self.ports.validations)
        packet_inspections = list(getattr(self.ports, "inspections", []))
        packet_subagents = self.ports._subagent_summaries()
        packet_last_coder_message = self.ports.last_coder_message
        packet_triggering_action = triggering_action
        packet_human_message = human_message
        packet_prior_interventions = list(self.state.prior_interventions)
        packet_patch_summary = patch_summary
        if completion_review:
            packet_validations = self.ports._review_safe_values(packet_validations)
            packet_inspections = self.ports._review_safe_values(packet_inspections)
            packet_subagents = self.ports._review_safe_values(packet_subagents)
            packet_prior_interventions = self.ports._review_safe_values(
                packet_prior_interventions
            )
            if self.ports._exposes_review_private_input(packet_last_coder_message):
                packet_last_coder_message = None
            if self.ports._exposes_review_private_input(packet_triggering_action):
                packet_triggering_action = None
            if self.ports._exposes_review_private_input(packet_human_message):
                packet_human_message = None
            if self.ports._exposes_review_private_input(packet_patch_summary):
                packet_patch_summary = None
            if self.ports._exposes_review_private_input(summary):
                summary = "Coder work is ready for independent review."
        if not completion_review:
            summary = self.ports._prepare_runtime_trigger_summary(
                summary,
                pending=runtime_trigger_batch,
            )
        latest_change_sequence = compat._latest_relevant_change_sequence(changed_files)
        freshness_summary = compat._validation_freshness_summary(
            validations=packet_validations,
            changed_files=changed_files,
        )
        completion_payload_mode: Literal["full", "delta", "full_fallback"] | None = None
        completion_payload_since_sequence: int | None = None
        completion_details: dict[str, compat.Any] = {}
        if completion_review:
            completion_payload_mode, completion_payload_since_sequence = self.ports._completion_payload_window(changed_files)
            completion_details = await self.ports.completion_packet_details(
                changed_files,
                since_sequence=completion_payload_since_sequence,
            )
            completion_details["evidence_provenance_summary"] = compat._evidence_provenance_summary(
                validations=packet_validations,
                changed_files=changed_files,
                latest_change_sequence=latest_change_sequence,
            )
            completion_details["behavior_surface"] = self.ports._behavior_surface_items()
            completion_details["prior_uncovered_edge_candidates"] = list(
                self.ports._completion_knowledge()["uncovered_edge_candidates"]
            )
        packet = agent.build_packet(
            wake_sequence=wake_sequence,
            current_summary=summary,
            diff_summary=await self.ports.diff_summary(),
            triggering_item_id=triggering_item_id,
            pending_approvals=[compat._approval_wake_context(pending) for pending in self.ports.pending_approvals.values()],
            triggering_action=packet_triggering_action,
            runtime_triggering_actions=runtime_trigger_actions,
            subagents=packet_subagents,
            last_coder_message=packet_last_coder_message,
            validations=packet_validations,
            inspections=packet_inspections,
            human_message=packet_human_message,
            prior_interventions=packet_prior_interventions,
            changed_files=changed_files,
            patch_summary=packet_patch_summary or await self.ports.patch_summary(),
            completion_attempt_count=getattr(self.ports, "completion_attempt_count", 0),
            completion_returns_this_generation=compat._completion_returns_this_generation(self.ports, cfg.generation),
            previous_completion_returns=list(getattr(self.ports, "completion_returns", []))[-10:],
            last_readiness_marker_sequence=getattr(self.ports, "_last_completion_marker_sequence", None),
            no_marker_idle_nudge_count=getattr(self.ports, "no_marker_idle_nudge_count", 0),
            latest_relevant_change_sequence=latest_change_sequence,
            validation_freshness_summary=freshness_summary,
            completion_payload_mode=completion_payload_mode,
            completion_payload_since_sequence=completion_payload_since_sequence,
            completion_review_thread_id=getattr(agent, "completion_thread_id", None),
            adversary_report=(
                self.ports._fresh_adversary_report(
                    generation=cfg.generation,
                    latest_relevant_change_sequence=latest_change_sequence,
                )
                if completion_review
                else None
            ),
            **completion_details,
        )
        if completion_review:
            packet = self.ports._review_safe_packet_state(packet)
        if not self.ports._coder_lifecycle_accepts_activity():
            if completion_review:
                await self.ports._close_completion_review_session()
            return
        if completion_review:
            if not self.ports._effective_completion_review():
                if self.ports._adversary_runs_remaining():
                    await self.ports._run_adversary_before_complete(None, packet=packet)
                else:
                    await self.ports._finalize_adversary_only("adversary pass budget exhausted after coder follow-up")
                return
            budget_action = self.ports._completion_review_budget_action(packet=packet)
            if budget_action == "adversary":
                await self.ports._run_adversary_before_complete(None, packet=packet)
                return
            if budget_action == "complete":
                reason = (
                    "completion review budget exhausted"
                    if self.ports._effective_max_adversary_runs() <= 0
                    else "post-adversary completion review budget exhausted"
                )
                await self.ports._finalize_bounded_completion(
                    reason=reason,
                )
                return
        try:
            if completion_review:
                self.ports.completion_attempt_count = getattr(self.ports, "completion_attempt_count", 0) + 1
                decision = await agent.decide_completion(packet)
            else:
                # Cheap-model triage: let a lightweight model route clear non-events to noop
                # before paying for the full supervisor. Never short-circuit human messages or
                # pending approvals (those always need the full supervisor); on any cheap-side
                # error or escalate, fall through to the full supervisor.
                if (
                    getattr(self.state, "runtime_triage_reviewer", None) is not None
                    and human_message is None
                    and not packet.pending_approvals
                    and not compat._runtime_packet_requires_full_supervisor(packet)
                ):
                    cheap = await self.ports._cheap_runtime_route(packet)
                    if cheap is not None and cheap.decision == "noop":
                        self.ports._ack_runtime_trigger_batch(runtime_trigger_batch)
                        await self.ports._resume_idle_after_runtime_noop(packet)
                        return
                decision = await agent.decide(packet)
        except compat.SupervisorAgentError as exc:
            if getattr(self.ports, "_transport_error_pending", False):
                # The controller event loop owns transport recovery. Do not let
                # the same broken stream race that recovery and terminalize the
                # run from this reviewer task.
                return
            if not self.ports._coder_lifecycle_accepts_activity():
                if completion_review:
                    await self.ports._close_completion_review_session()
                return
            failure_kind = compat._classify_supervisor_agent_error(exc)
            message = f"supervisor check failed ({failure_kind}): {exc}"
            self.ports.tui.render("SUPERVISOR", message)
            if failure_kind == "no_message":
                recovered = await self.ports._handle_supervisor_no_message_failure(
                    message=message,
                    summary=summary,
                    completion_review=completion_review,
                )
                if recovered:
                    if (
                        not completion_review
                        and getattr(self.state, "_supervisor_next_runtime_summary", None) is None
                    ):
                        # Runtime no_message has exhausted its bounded retry and the existing
                        # policy explicitly skips this runtime-only review. Acknowledge that
                        # terminal skip so a retained trigger cannot strand the queue forever.
                        self.ports._ack_runtime_trigger_batch(runtime_trigger_batch)
                    return
            if completion_review and failure_kind == "tool_timeout":
                recovered = await self.ports._handle_completion_review_timeout_failure(
                    message=message,
                    summary=summary,
                )
                if recovered:
                    return
            if not completion_review:
                queued_runtime = getattr(self.state, "_supervisor_next_runtime_summary", None)
                queued_completion = getattr(self.state, "_supervisor_next_completion_summary", None)
                legacy_queued_completion = bool(
                    getattr(self.state, "_supervisor_next_completion_review", False)
                )
                if (
                    queued_runtime is None
                    and queued_completion is None
                    and not legacy_queued_completion
                ):
                    await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)
                    return
                counts = getattr(self.ports, "provider_failure_recovery_counts", None)
                if counts is None:
                    counts = {}
                    self.ports.provider_failure_recovery_counts = counts
                retry_key = f"runtime_monitor_{failure_kind}"
                attempts = int(counts.get(retry_key) or 0)
                if attempts < 1:
                    counts[retry_key] = attempts + 1
                    if queued_runtime is None:
                        self.ports._queue_supervisor_check(
                            f"Retry runtime review after {failure_kind}: {summary}",
                            triggering_item_id=triggering_item_id,
                            triggering_action=triggering_action,
                            human_message=human_message,
                            patch_summary=patch_summary,
                            completion_review=False,
                        )
                    self.ports.store.append_text_locked(
                        compat.PROGRESS,
                        f"- Runtime supervisor failed with {failure_kind}; retrying the retained runtime trigger before completion.\n",
                    )
                    return
                self.ports._cancel_queued_completion_review()
                compat.patch_health(
                    self.ports.store,
                    compat.HealthDelta(
                        generation=cfg.generation,
                        timeout_fallback_count=1,
                        add_risk_signals=["stale_runtime_supervisor_timeout"],
                    ),
                )
                self.ports.store.append_text_locked(
                    compat.PROGRESS,
                    f"- Runtime supervisor failed repeatedly with {failure_kind}; refusing to run a stale completion review.\n",
                )
                await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)
                return
            await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)
            return
        if not self.ports._coder_lifecycle_accepts_activity():
            # A lifecycle transition may race an in-flight model call. Its result belongs
            # to the previous live state and must not steer, restart, or complete the run.
            if completion_review:
                await self.ports._close_completion_review_session()
            return
        # Successful supervisor decision: reset the transient provider no_message budget so it
        # counts CONSECUTIVE empty-completion failures, not lifetime ones (a recovered provider
        # should not inherit earlier blips toward an infra-invalid).
        if getattr(self.ports, "provider_failure_recovery_counts", None):
            self.ports.provider_failure_recovery_counts = {}
        if completion_review:
            if getattr(self.state, "_supervisor_next_runtime_summary", None) is not None:
                # Runtime evidence arrived while this completion snapshot was in flight.
                # Do not apply a now-stale accept/return/restart decision; requeue completion
                # behind the pending runtime pass, which may steer or restart the coder. Close
                # the review session now so a later readiness review cannot reuse this frozen
                # verification copy after the coder has acted on runtime steering.
                await self.ports._close_completion_review_session()
                self.ports._queue_supervisor_check(
                    summary,
                    triggering_item_id=triggering_item_id,
                    triggering_action=triggering_action,
                    human_message=human_message,
                    patch_summary=patch_summary,
                    completion_review=True,
                )
                return
            await self.ports.apply_completion_decision(decision, packet_thread_id=packet.coder_thread_id, packet=packet)
        else:
            try:
                applied = await self.ports.apply_supervisor_decision(
                    decision,
                    packet_thread_id=packet.coder_thread_id,
                    packet=packet,
                )
            except Exception as exc:
                if not self.ports._coder_lifecycle_accepts_activity():
                    return
                self.ports._cancel_queued_completion_review()
                attempts = int(getattr(self.state, "_runtime_apply_retry_count", 0) or 0)
                if attempts < 1:
                    self.state._runtime_apply_retry_count = attempts + 1
                    self.ports._queue_supervisor_check(
                        f"Runtime trigger (runtime_apply_retry): retry decision after apply failure; {summary}",
                        triggering_item_id=triggering_item_id,
                        triggering_action=triggering_action,
                        human_message=human_message,
                        patch_summary=patch_summary,
                        completion_review=False,
                    )
                    self.ports.store.append_text_locked(
                        compat.PROGRESS,
                        f"- Runtime decision application failed ({exc.__class__.__name__}); retrying once before completion.\n",
                    )
                    return
                await self.ports.finalize(
                    f"runtime decision application failed after retry: {exc}",
                    status=compat.BelloStatus.PROVIDER_FAILURE,
                )
                return
            self.state._runtime_apply_retry_count = 0
            if not self.ports._coder_lifecycle_accepts_activity():
                return
            if applied and self.ports.store.get_bello_config().generation == packet.generation:
                self.state._runtime_decision_retry_count = 0
                self.ports._ack_runtime_trigger_batch(runtime_trigger_batch)
                await self.ports._resume_readiness_after_runtime_noop(decision, packet)
                if (
                    decision.decision == compat.SupervisorDecisionKind.NOOP
                    and decision.wake_sequence == packet.wake_sequence
                    and decision.generation == packet.generation
                ):
                    await self.ports._resume_idle_after_runtime_noop(packet)
            elif not applied:
                attempts = int(getattr(self.state, "_runtime_decision_retry_count", 0) or 0)
                if attempts < 1:
                    self.state._runtime_decision_retry_count = attempts + 1
                    self.ports._queue_supervisor_check(
                        f"Runtime trigger (runtime_decision_retry): refresh stale runtime decision; {summary}",
                        triggering_item_id=triggering_item_id,
                        triggering_action=triggering_action,
                        human_message=human_message,
                        patch_summary=patch_summary,
                        completion_review=False,
                    )
                    return
                self.ports._cancel_queued_completion_review()
                await self.ports.finalize(
                    "runtime supervisor returned a stale or mismatched decision after retry",
                    status=compat.BelloStatus.PROVIDER_FAILURE,
                )

    async def _resume_idle_after_runtime_noop(self, packet: compat.SupervisorWakePacket) -> bool:
        # A denied native approval can end a turn after its last action. A runtime
        # noop means no intervention is needed, not that the now-idle coder is done.
        # Continue the existing no-marker review/nudge policy without waiting for
        # the idle watchdog, and never revive a paused or superseded lifecycle.
        if not self.ports._runtime_noop_idle_context_is_current(packet):
            return False
        await self.ports._refresh_coder_subagents()
        if not self.ports._runtime_noop_idle_context_is_current(packet) or self.ports._active_coder_subagents():
            return False
        await self.ports._handle_no_marker_idle(subagents_refreshed=True)
        return True

    def _runtime_noop_idle_context_is_current(self, packet: compat.SupervisorWakePacket) -> bool:
        cfg = self.ports.store.get_bello_config()
        coder = getattr(self.ports, "coder", None)
        return bool(
            packet.current_summary == "Coder turn completed"
            and coder is not None
            and self.ports._coder_lifecycle_accepts_activity(cfg)
            and cfg.generation == packet.generation
            and cfg.coder_thread_id == packet.coder_thread_id
            and not self.ports._readiness_snapshot_has_new_invalidating_event(packet, cfg=cfg)
            and cfg.active_coder_turn_id is None
            and not getattr(coder, "active_turn_id", None)
            and not self.ports.pending_approvals
            and not cfg.pending_server_request_ids
            and getattr(self.state, "_supervisor_next_runtime_summary", None) is None
            and getattr(self.state, "_supervisor_next_runtime_check", None) is None
            and getattr(self.state, "_supervisor_next_completion_summary", None) is None
            and getattr(self.state, "_supervisor_next_completion_check", None) is None
            and getattr(self.ports, "_deferred_completion_check", None) is None
        )

    async def _resume_readiness_after_runtime_noop(
        self,
        decision: compat.SupervisorDecision,
        packet: compat.SupervisorWakePacket,
    ) -> bool:
        if decision.decision != compat.SupervisorDecisionKind.NOOP:
            return False
        if "done_without_fresh_validation" not in compat._runtime_trigger_reasons_from_summary(packet.current_summary):
            return False
        marker_sequence = packet.last_readiness_marker_sequence
        if marker_sequence is None or not self.ports._runtime_noop_readiness_context_is_current(
            decision,
            packet,
            marker_sequence=marker_sequence,
        ):
            return False

        await self.ports._refresh_coder_subagents()
        if not self.ports._runtime_noop_readiness_context_is_current(
            decision,
            packet,
            marker_sequence=marker_sequence,
        ):
            return False
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "completion/readiness_validation_waived",
            decision="noop",
            reason=decision.reason,
        )
        await self.ports._continue_after_readiness_marker(
            triggering_item_id=packet.triggering_item_id,
            subagents_refreshed=True,
        )
        return True

    def _runtime_noop_readiness_context_is_current(
        self,
        decision: compat.SupervisorDecision,
        packet: compat.SupervisorWakePacket,
        *,
        marker_sequence: int,
    ) -> bool:
        message = self.ports.last_coder_message
        cfg = self.ports.store.get_bello_config()
        coder = getattr(self.ports, "coder", None)
        return bool(
            decision.wake_sequence == packet.wake_sequence
            and decision.generation == packet.generation
            and cfg.generation == packet.generation
            and cfg.coder_thread_id == packet.coder_thread_id
            and not self.ports._readiness_snapshot_has_new_invalidating_event(packet, cfg=cfg)
            and cfg.active_coder_turn_id is None
            and self.ports._last_completion_marker_sequence == marker_sequence
            and message is not None
            and message.sequence == marker_sequence
            and compat._has_readiness_marker(message.text)
            and not bool(getattr(self.ports, "pending_approvals", None))
            and getattr(self.state, "_supervisor_next_runtime_summary", None) is None
            and getattr(self.state, "_supervisor_next_runtime_check", None) is None
            and getattr(self.ports, "running", False)
            and not getattr(self.ports, "paused", False)
            and not getattr(self.ports, "_finalizing", False)
            and not getattr(self.ports, "_terminal_cleanup_started", False)
            and (coder is None or not getattr(coder, "active_turn_id", None))
        )

    def _readiness_snapshot_has_new_invalidating_event(
        self,
        packet: compat.SupervisorWakePacket,
        *,
        cfg: compat.BelloConfig,
    ) -> bool:
        """Accept only a complete bounded suffix containing known reviewer traffic."""

        if cfg.last_event_sequence == packet.latest_event_sequence:
            return False
        if cfg.last_event_sequence < packet.latest_event_sequence:
            return True

        expected_sequence = packet.latest_event_sequence + 1
        for event in self.ports._readiness_journal():
            if event.sequence < expected_sequence:
                continue
            if event.sequence > cfg.last_event_sequence:
                break
            if event.sequence != expected_sequence:
                return True
            expected_sequence += 1
            if event.source != compat.AppEventSource.APP_SERVER:
                return True
            if event.event_type == "account/rateLimits/updated" and event.thread_id is None:
                continue
            if event.thread_id is None:
                return True
            if event.thread_id == cfg.coder_thread_id or self.ports._is_coder_descendant(
                event.thread_id,
                cfg=cfg,
            ):
                return True
            if self.ports._reviewer_role_for_thread(event.thread_id) is None:
                return True

        # Missing/evicted/non-contiguous entries make provenance unknowable.
        return expected_sequence <= cfg.last_event_sequence

    def _completion_payload_window(
        self,
        changed_files: list[compat.ChangedFile],
    ) -> tuple[Literal["full", "delta", "full_fallback"], int | None]:
        since_sequence = getattr(self.ports, "completion_review_return_sequence", None)
        if since_sequence is None:
            return "full", None
        task_contents = self.ports._canonical_task_text()
        has_unknown_relevant_sequence = any(
            changed.sequence is None and compat._is_relevant_changed_path(changed.path, task_contents=task_contents)
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
        issue: compat.RuntimeRestartIssue | None,
    ) -> None:
        self.state.prior_interventions.append(
            compat.PriorIntervention(reason=reason, message_to_coder=message, sequence=sequence)
        )
        self.state.prior_interventions = self.state.prior_interventions[-20:]
        compat.patch_health(self.ports.store, compat.HealthDelta(generation=generation, interventions=1))
        if issue is not None:
            compat.record_restart_issue_intervention(
                self.ports.store,
                generation=generation,
                issue_key=issue.key,
                sequence=issue.sequence,
                validation_id=issue.validation_id,
            )

    async def apply_supervisor_decision(
        self,
        decision: compat.SupervisorDecision,
        *,
        packet_thread_id: str | None,
        packet: compat.SupervisorWakePacket | None = None,
    ) -> bool:
        if not self.ports._runtime_enabled():
            return False
        cfg = self.ports.store.get_bello_config()
        if not self.ports._coder_lifecycle_accepts_activity(cfg, require_running=False):
            return False
        if decision.generation is not None and decision.generation != cfg.generation:
            return False
        if packet_thread_id != cfg.coder_thread_id:
            return False
        if decision.wake_sequence is not None and decision.wake_sequence <= cfg.last_applied_supervisor_sequence:
            return False
        health = self.ports.store.get_health()
        issue = (
            compat._runtime_restart_issue(
                packet,
                active_issue_key=health.restart_issue_key,
                active_issue_last_sequence=health.restart_issue_last_sequence,
            )
            if packet is not None
            else None
        )
        restart_candidate = False
        restart_candidate_reason: str | None = None
        if decision.decision == compat.SupervisorDecisionKind.RESTART:
            restart_candidate, restart_candidate_reason = compat.kill_restart_candidate(
                health,
                issue_key=issue.key if issue is not None else None,
                issue_sequence=issue.sequence if issue is not None else None,
            )
        apply_restart_metadata = decision.decision != compat.SupervisorDecisionKind.RESTART or restart_candidate

        intervention: tuple[str, str] | None = None
        if decision.decision == compat.SupervisorDecisionKind.INTERVENE and decision.message_to_coder and self.ports.coder:
            self.ports.tui.render("SUPERVISOR", f"steering coder: {decision.reason}")
            delivered, _ = await self.ports._deliver_coder_message(decision.message_to_coder)
            if not delivered:
                return False
            intervention = (decision.reason, decision.message_to_coder)
        elif decision.decision == compat.SupervisorDecisionKind.RESTART:
            if not restart_candidate:
                message = decision.message_to_coder or compat._restart_rejection_steering(decision.handoff)
                self.ports.tui.render("SUPERVISOR", f"restart rejected without health evidence: {decision.reason}")
                if self.ports.coder:
                    delivered, _ = await self.ports._deliver_coder_message(message)
                    if not delivered:
                        return False
                intervention = (decision.reason, message)
            else:
                if restart_candidate_reason:
                    self.ports.tui.render("SUPERVISOR", f"restart candidate: {restart_candidate_reason}")
                await self.ports.restart(decision.reason or "supervisor requested restart", handoff=decision.handoff)
        elif decision.decision == compat.SupervisorDecisionKind.PAUSE:
            await self.ports.pause()

        # Commit the decision only after its externally visible action succeeds. In
        # particular, a failed steer must remain retryable with the same wake sequence.
        if intervention is not None:
            intervention_reason, intervention_message = intervention
            self.ports._record_runtime_intervention(
                reason=intervention_reason,
                message=intervention_message,
                sequence=decision.wake_sequence or cfg.last_event_sequence,
                generation=cfg.generation,
                issue=issue,
            )
        if decision.persistent_decision:
            self.ports.store.append_text_locked(compat.DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update and apply_restart_metadata:
            self.ports.store.append_text_locked(compat.PROGRESS, f"- {decision.progress_update}\n")
            if decision.decision != compat.SupervisorDecisionKind.RESTART:
                compat.patch_health(
                    self.ports.store,
                    compat.HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence),
                )
        if decision.clear_handoff and apply_restart_metadata:
            self.ports.store.write_text_locked(compat.HANDOFF, "")
        if decision.display_message and apply_restart_metadata:
            self.ports.tui.render("SUPERVISOR", decision.display_message)
        if decision.decision != compat.SupervisorDecisionKind.NOOP:
            self.state._runtime_decision_invalidated_completion = True
        self.ports._record_supervisor_decision_metric(use_case="runtime", decision=decision.decision.value)
        self.ports.store.update_bello_config(
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

    async def _configure_runtime_triage(self) -> None:
        if not self.ports._runtime_enabled():
            self.state.runtime_triage_reviewer = None
            return
        config = compat.runtime_triage_config_from_env(enabled=self.ports._cheap_runtime_enabled())
        self.state.runtime_triage_config = config
        self.state.runtime_triage_reviewer = None
        if not config.enabled:
            self.ports.tui.render("SYSTEM", "cheap runtime triage disabled by configuration")
            return
        if config.model is None:
            self.ports.tui.render("SYSTEM", "cheap runtime triage disabled: no model configured")
            self.state.runtime_triage_config = compat.CheapRuntimeTriageConfig(
                enabled=False, model=None, timeout_seconds=config.timeout_seconds
            )
            return
        reviewer = compat.CheapRuntimeReviewer(
            self.ports.client,
            self.ports._active_workspace_root(),
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        try:
            await self.ports._cheap_runtime_structured_output_self_test(reviewer)
        except Exception as exc:
            self.ports.tui.render(
                "SYSTEM",
                f"cheap runtime triage unavailable; full supervisor on every wake ({exc.__class__.__name__})",
            )
            self.state.runtime_triage_config = compat.CheapRuntimeTriageConfig(
                enabled=False, model=config.model, timeout_seconds=config.timeout_seconds
            )
            return
        self.state.runtime_triage_reviewer = reviewer
        self.ports.tui.render("SYSTEM", f"cheap runtime triage enabled with model {config.model}")

    async def _cheap_runtime_structured_output_self_test(self, reviewer: compat.CheapRuntimeReviewer) -> None:
        if not self.ports._runtime_enabled():
            return
        packet = compat.SupervisorWakePacket(
            wake_sequence=1,
            latest_event_sequence=0,
            generation=0,
            restart_count=0,
            task_path=str(self.ports.task_path),
            task_contents="",
            current_summary="Startup runtime-triage self-test: routine read-only progress, no failing checks.",
        )
        decision = await compat.asyncio.wait_for(reviewer.review(packet), timeout=reviewer.timeout_seconds)
        if decision.decision not in {"noop", "escalate"}:
            raise RuntimeError("cheap runtime structured-output self-test returned an unexpected decision")

    async def _cheap_runtime_route(self, packet: compat.SupervisorWakePacket) -> compat.CheapRuntimeDecision | None:
        if not self.ports._runtime_enabled():
            return None
        reviewer = self.state.runtime_triage_reviewer
        if reviewer is None:
            return None
        started = compat.time.monotonic()
        try:
            decision = await reviewer.review(packet)
        except compat.CheapRuntimeReviewerError as exc:
            self.ports._record_cheap_runtime_attempt(
                packet, decision=None, outcome=f"error:{exc.__class__.__name__}", started=started, fallback=True
            )
            return None
        self.ports._record_cheap_runtime_attempt(
            packet,
            decision=decision,
            outcome=decision.decision,
            started=started,
            fallback=(decision.decision == "escalate"),
        )
        if decision.decision == "noop":
            self.ports.tui.render("SUPERVISOR", f"cheap runtime triage: noop ({decision.reason_code})")
        return decision

    def _record_cheap_runtime_attempt(
        self,
        packet: compat.SupervisorWakePacket,
        *,
        decision: compat.CheapRuntimeDecision | None,
        outcome: str,
        started: float,
        fallback: bool,
    ) -> None:
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "cheap_runtime_review",
                "wake_sequence": packet.wake_sequence,
                "generation": packet.generation,
                "current_summary": (packet.current_summary or "")[:160],
                "trigger_reasons": list(compat._runtime_trigger_reasons_from_summary(packet.current_summary)),
                "decision": decision.decision if decision is not None else None,
                "reason_code": decision.reason_code if decision is not None else None,
                "outcome": outcome,
                "latency_seconds": compat.time.monotonic() - started,
                "model": self.state.runtime_triage_config.model,
                "full_supervisor_fallback": fallback,
            }
        )
