"""Completion service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import compat
from .interfaces import CoordinatorPort
from .defaults import NO_MARKER_IDLE_NUDGE


@dataclass(init=False, slots=True)
class CompletionState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _accepted_adversary_report: compat.AdversaryReport | None
    _accepted_completion_decision: compat.CompletionReviewDecision | None
    _active_adversary_thread_id: str | None
    _active_adversary_workspace_root: compat.Path | None
    _adversary_reservation_recovery_pending: bool
    _completion_knowledge_state: dict[str, list[Any]]
    _last_completion_marker_sequence: int | None
    _no_marker_completion_review_key: str | None
    _pending_adversary_report: compat.AdversaryReport | None
    _readiness_event_journal: compat.deque[compat._ReadinessJournalEvent]
    adv_report_controller: compat.StatelessSupervisorAgent | None
    completion_attempt_count: int
    completion_restarts: int
    completion_returns: list[compat.CompletionReturnRecord]
    completion_review_return_sequence: int | None
    completion_supervisor: compat.StatelessSupervisorAgent | None
    no_marker_idle_nudge_count: int
    provider_failure_recovery_counts: dict[str, int]


class CompletionPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_active_coder_subagents',
        '_active_workspace_root',
        '_adv_report_controller_agent',
        '_adv_report_controller_staleness_reason',
        '_adversary_denied_commands',
        '_adversary_intelligence',
        '_adversary_model',
        '_adversary_multi_agent_config',
        '_adversary_runs_remaining',
        '_append_completion_anchor_log',
        '_append_event',
        '_cleanup_adversary_reviewer_descendants',
        '_coder_lifecycle_accepts_activity',
        '_complete_after_adversary_unavailable',
        '_completion_knowledge',
        '_completion_no_message_max_retries',
        '_completion_packet_lifecycle_is_current',
        '_completion_supervisor_agent',
        '_completion_timeout_max_retries',
        '_continue_after_readiness_marker',
        '_current_turn_action_count',
        '_deferred_completion_check',
        '_deliver_coder_message',
        '_done_without_fresh_behavioral_validation',
        '_effective_completion_review',
        '_effective_max_adversary_runs',
        '_escalate_runtime_integrity_issue',
        '_fail_adv_report_controller',
        '_fail_required_adversary',
        '_fail_revision_coder_switch',
        '_finalize_accepted_completion',
        '_finalize_adversary_only',
        '_finalize_bounded_completion',
        '_finalize_completion_review_disabled',
        '_generation_has_coder_turn',
        '_handle_no_marker_idle',
        '_mark_adversary_thread_done',
        '_mark_adversary_thread_started',
        '_no_message_backoff_seconds',
        '_packet_has_fresh_adversary_report',
        '_post_coder_review_enabled',
        '_queue_supervisor_check',
        '_readiness_event_journal_limit',
        '_record_adversary_limit_reached',
        '_record_cancelled_revision_switch',
        '_record_completion_knowledge',
        '_record_runtime_trigger_trace',
        '_record_stale_adversary_discard',
        '_record_supervisor_decision_metric',
        '_refresh_coder_subagents',
        '_register_reviewer_thread',
        '_repair_snapshot_runtime_controls',
        '_reserve_adversary_run',
        '_return_completion_to_coder',
        '_review_private_relative_paths',
        '_revision_coder_active',
        '_revision_coder_enabled',
        '_revision_switch_context_is_current',
        '_run_adv_report_controller',
        '_run_adversary_before_complete',
        '_runtime_enabled',
        '_schedule_supervisor_check',
        '_should_run_adversary_before_complete',
        '_steer_for_marker',
        '_switch_to_revision_coder',
        '_task_integrity_issue',
        '_transport_error_pending',
        '_update_relevant_edit_state',
        '_write_run_checkpoint',
        'adversary_enabled',
        'adversary_runs',
        'changed_files',
        'client',
        'coder',
        'finalize',
        'last_coder_message',
        'pending_approvals',
        'prior_interventions',
        'restart',
        'store',
        'tui',
        'validations',
    })
    writes = frozenset({
        '_adversary_denied_commands',
        '_deferred_completion_check',
        'prior_interventions',
    })


class Completion:
    """Own completion behavior; borrow only the declared port."""

    def __init__(self, ports: CompletionPort) -> None:
        self.state = CompletionState()
        self.ports = ports

    async def _handle_coder_turn_completed(self, *, item_id: str | None) -> None:
        repaired_runtime_controls = self.ports._repair_snapshot_runtime_controls(source="coder_turn_completed")
        if await self.ports._escalate_runtime_integrity_issue(source="coder_turn_completed"):
            return
        if repaired_runtime_controls and self.ports._runtime_enabled():
            self.ports._schedule_supervisor_check(
                "Runtime integrity trigger: coder workspace runtime links were replaced and restored.",
                triggering_item_id=item_id,
            )
            return
        message = self.ports.last_coder_message
        if message is not None and compat._has_readiness_marker(message.text):
            if self.state._last_completion_marker_sequence != message.sequence:
                await self.ports._refresh_coder_subagents()
                active_subagents = self.ports._active_coder_subagents()
                if active_subagents:
                    child_ids = ", ".join(compat._short_thread_id(state.thread_id) for state in active_subagents)
                    self.ports._deferred_completion_check = compat.QueuedSupervisorCheck(
                        summary="Coder provided exact readiness marker; waiting for active subagents before completion.",
                        triggering_item_id=item_id,
                        completion_review=self.ports._post_coder_review_enabled(),
                    )
                    reason = (
                        "Coder declared readiness while relevant subagents are still active: "
                        f"{child_ids}. Wait for their results, review and integrate them, then emit the readiness marker again."
                    )
                    self.ports._append_event(
                        compat.AppEventSource.SUPERVISOR,
                        "completion/deferred_for_subagents",
                        reason=reason,
                    )
                    await self.ports._steer_for_marker(reason, sequence=message.sequence, message=reason)
                    return
                self.state._last_completion_marker_sequence = message.sequence
                self.state.no_marker_idle_nudge_count = 0
                done_gap = await self.ports._done_without_fresh_behavioral_validation() if self.ports._runtime_enabled() else None
                if done_gap is not None:
                    self.ports._record_runtime_trigger_trace(
                        event_type="turn/completed",
                        action=compat.TriggeringAction(
                            item_id=item_id,
                            kind="done",
                            status="completed",
                            summary=done_gap,
                        ),
                        validation=None,
                        changed_files=await self.ports.changed_files(),
                        decision=compat.RuntimeTriggerDecision(
                            should_wake=True,
                            reasons=("done_without_fresh_validation",),
                        ),
                    )
                    self.ports._schedule_supervisor_check(
                        f"Runtime trigger (done_without_fresh_validation): {done_gap}",
                        triggering_item_id=item_id,
                    )
                    return
                await self.ports._continue_after_readiness_marker(triggering_item_id=item_id)
            return
        if message is not None and compat._has_malformed_readiness_marker(message.text):
            await self.ports._steer_for_marker(
                "Coder used a malformed readiness marker; require exact marker only after validation.",
                sequence=message.sequence,
            )
            return
        if message is not None and compat._appears_to_claim_readiness(message.text):
            await self.ports._steer_for_marker(
                "Coder appears to be claiming readiness but did not provide exact readiness marker.",
                sequence=message.sequence,
            )
            return
        if not self.ports._runtime_enabled():
            await self.ports._handle_no_marker_idle()
            return
        if self.ports.pending_approvals:
            self.ports._schedule_supervisor_check("Coder turn completed", triggering_item_id=item_id)
            return
        if getattr(self.ports, "_current_turn_action_count", 0) == 0:
            await self.ports._handle_no_marker_idle()
            return
        self.ports._schedule_supervisor_check("Coder turn completed", triggering_item_id=item_id)

    async def _continue_after_readiness_marker(
        self,
        *,
        triggering_item_id: str | None,
        subagents_refreshed: bool = False,
    ) -> None:
        if not subagents_refreshed:
            await self.ports._refresh_coder_subagents()
        active_subagents = self.ports._active_coder_subagents()
        if active_subagents:
            self.ports._deferred_completion_check = compat.QueuedSupervisorCheck(
                summary="Coder provided exact readiness marker; continuing completion after active subagents finish.",
                triggering_item_id=triggering_item_id,
                completion_review=self.ports._post_coder_review_enabled(),
            )
            reason = "Wait for active subagents, review and integrate their results, then emit the readiness marker again."
            self.ports._append_event(
                compat.AppEventSource.SUPERVISOR,
                "completion/deferred_for_subagents",
                reason=reason,
            )
            await self.ports._steer_for_marker(reason, message=reason)
            return
        if not self.ports._post_coder_review_enabled():
            await self.ports._finalize_completion_review_disabled()
            return
        self.ports._schedule_supervisor_check(
            "Coder provided exact readiness marker; running configured final reviews.",
            triggering_item_id=triggering_item_id,
            completion_review=True,
        )

    async def _done_without_fresh_behavioral_validation(self) -> str | None:
        changed_files = await self.ports.changed_files()
        self.ports._update_relevant_edit_state(changed_files)
        cfg = self.ports.store.get_bello_config()
        latest_relevant_edit = cfg.last_relevant_edit_sequence
        if latest_relevant_edit is None:
            return None
        if any(compat._validation_is_fresh_behavioral_pass(validation, latest_relevant_edit) for validation in self.ports.validations):
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
        cfg = self.ports.store.get_bello_config()
        self.ports.prior_interventions.append(
            compat.PriorIntervention(reason=reason, message_to_coder=message, sequence=sequence or cfg.last_event_sequence)
        )
        self.ports.prior_interventions = self.ports.prior_interventions[-20:]
        compat.patch_health(self.ports.store, compat.HealthDelta(generation=cfg.generation, interventions=1))
        self.ports.tui.render("SUPERVISOR", reason)
        if self.ports.coder:
            await self.ports._deliver_coder_message(message)

    async def _handle_no_marker_idle(self, *, subagents_refreshed: bool = False) -> None:
        cfg = self.ports.store.get_bello_config()
        if cfg.active_coder_turn_id:
            return
        if not subagents_refreshed:
            await self.ports._refresh_coder_subagents()
        if self.ports._active_coder_subagents():
            return
        if not getattr(self.ports, "_generation_has_coder_turn", True):
            # A freshly restarted generation has produced no coder work yet: forcing a completion
            # review here would judge the previous generation's leftover state (observed killing a
            # run via restart-with-exhausted-budget). Kick the coder instead; steer_or_start starts
            # a turn if the restart kickoff died.
            if self.ports.coder:
                await self.ports._deliver_coder_message(compat.POST_RESTART_CONTINUE_NUDGE)
            return
        latest_validation_sequence = max((validation.sequence for validation in self.ports.validations), default=None)
        last_message_sequence = self.ports.last_coder_message.sequence if self.ports.last_coder_message is not None else None
        review_key = f"{cfg.generation}:{last_message_sequence}:{latest_validation_sequence}"
        if getattr(self.state, "_no_marker_completion_review_key", None) == review_key:
            return
        self.state._no_marker_completion_review_key = review_key
        if not self.ports._post_coder_review_enabled():
            # No review gate to force: nudge the coder to finish and emit the marker,
            # which is the only terminal signal in this mode.
            await self.ports._steer_for_marker(
                "Coder is idle with no active turn and no readiness marker; completion review is disabled, nudging coder to finish.",
            )
            return
        review_label = "completion_review" if self.ports._effective_completion_review() else "adversary review"
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Controller forcing {review_label}: coder is idle with no active turn and no readiness marker.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "completion/no_marker_idle_review",
            reason="coder idle with no active turn and no readiness marker",
        )
        self.ports._schedule_supervisor_check(
            f"Coder is idle with no active turn and no readiness marker. Run {review_label} on the current state.",
            completion_review=True,
        )

    async def _handle_completion_review_timeout_failure(self, *, message: str, summary: str) -> bool:
        """One fresh-thread retry when a completion-review turn times out.

        A timed-out review turn used to finalize the whole run as provider_failure, discarding
        hours of coder work over a single slow review. Close the review session (abandoning the
        hung turn) and re-enter the review loop once; on a consecutive second timeout, fall
        through to the existing fatal path. The counter resets on any successful decision.
        """
        counts = getattr(self.state, "provider_failure_recovery_counts", None)
        if counts is None:
            counts = {}
            self.state.provider_failure_recovery_counts = counts
        key = "completion_review_tool_timeout"
        attempts = int(counts.get(key) or 0)
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "provider_failure_recovery",
                "kind": "tool_timeout",
                "scope": "completion_review",
                "attempts_before": attempts,
                "message": message,
            }
        )
        budget = getattr(self.ports, "_completion_timeout_max_retries", compat.COMPLETION_TIMEOUT_MAX_RETRIES)
        if attempts >= budget:
            return False
        counts[key] = attempts + 1
        completion_supervisor = self.ports._completion_supervisor_agent()
        if completion_supervisor is not None and hasattr(completion_supervisor, "close_completion_review"):
            await completion_supervisor.close_completion_review()
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Provider recovery: completion review turn timed out; retrying once on a fresh review "
            f"thread (attempt {attempts + 1}/{budget}).\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "provider/completion_timeout_retry",
            decision="retry",
            reason=message,
        )
        retry_summary = (
            "Retry completion review on a fresh thread after the previous review turn timed out. "
            f"Previous review summary: {summary}"
        )
        self.ports._queue_supervisor_check(retry_summary, completion_review=True)
        return True

    async def _handle_supervisor_no_message_failure(
        self,
        *,
        message: str,
        summary: str,
        completion_review: bool,
    ) -> bool:
        counts = getattr(self.state, "provider_failure_recovery_counts", None)
        if counts is None:
            counts = {}
            self.state.provider_failure_recovery_counts = counts
        scope = "completion_review" if completion_review else "runtime_monitor"
        count_key = f"{scope}_no_message"
        attempts = int(counts.get(count_key) or 0)
        counts["no_message"] = int(counts.get("no_message") or 0) + 1
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "provider_failure_recovery",
                "kind": "no_message",
                "scope": scope,
                "attempts_before": attempts,
                "completion_review": completion_review,
                "message": message,
            }
        )
        budget = (
            getattr(self.ports, "_completion_no_message_max_retries", compat.COMPLETION_NO_MESSAGE_MAX_RETRIES)
            if completion_review
            else 1
        )
        if attempts < budget:
            counts[count_key] = attempts + 1
            completion_supervisor = self.ports._completion_supervisor_agent()
            if completion_review and completion_supervisor is not None and hasattr(
                completion_supervisor,
                "close_completion_review",
            ):
                await completion_supervisor.close_completion_review()
            backoff = 0.0
            if completion_review:
                schedule = getattr(self.ports, "_no_message_backoff_seconds", compat.NO_MESSAGE_RETRY_BACKOFF_SECONDS)
                if schedule:
                    backoff = float(schedule[min(attempts, len(schedule) - 1)])
            self.ports.store.append_text_locked(
                compat.PROGRESS,
                f"- Provider recovery: supervisor produced no agent message; retrying review from latest "
                f"stable state (attempt {attempts + 1}/{budget}, backoff {backoff:.0f}s).\n",
            )
            self.ports._append_event(
                compat.AppEventSource.SUPERVISOR,
                "provider/no_message_retry",
                decision="retry",
                reason=message,
            )
            if backoff > 0:
                await compat.asyncio.sleep(backoff)
            retry_summary = (
                "Retry supervisor review from the latest stable controller state after provider no_message. "
                f"Previous review summary: {summary}"
            )
            self.ports._queue_supervisor_check(
                retry_summary,
                completion_review=completion_review,
            )
            return True
        counts[count_key] = attempts + 1
        if not completion_review:
            self.ports.store.append_text_locked(
                compat.PROGRESS,
                "- Provider recovery: runtime supervisor produced no agent message after retry; skipping this runtime-only review.\n",
            )
            self.ports._append_event(
                compat.AppEventSource.SUPERVISOR,
                "provider/runtime_no_message_skipped",
                decision="continue",
                reason=message,
            )
            return True
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- Provider recovery failed: repeated supervisor no_message; marking run infra-invalid before scoring.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "provider/no_message_infra_invalid",
            decision="infra-invalid",
            reason=message,
        )
        await self.ports.finalize(
            f"infra-invalid: supervisor no_message provider failure after retry/resume: {message}",
            status=compat.BelloStatus.PROVIDER_FAILURE,
        )
        return True

    async def apply_completion_decision(
        self,
        decision: compat.CompletionReviewDecision,
        *,
        packet_thread_id: str | None,
        packet: compat.SupervisorWakePacket | None = None,
    ) -> None:
        cfg = self.ports.store.get_bello_config()
        if not self.ports._coder_lifecycle_accepts_activity(cfg, require_running=False):
            return
        if decision.generation != cfg.generation:
            return
        if packet_thread_id != cfg.coder_thread_id:
            return
        if decision.wake_sequence <= cfg.last_applied_supervisor_sequence:
            return
        self.ports.store.update_bello_config(
            lambda current: current.model_copy(update={"last_applied_supervisor_sequence": decision.wake_sequence})
        )
        self.ports._append_completion_anchor_log(decision, packet=packet)
        self.ports._record_supervisor_decision_metric(use_case="completion", decision=decision.decision.value)
        self.ports._record_completion_knowledge(decision)
        if decision.decision == compat.CompletionReviewDecisionKind.ACCEPT:
            if self.ports._should_run_adversary_before_complete(packet):
                if packet is None or self.ports._adversary_runs_remaining():
                    await self.ports._run_adversary_before_complete(decision, packet=packet)
                    return
                self.ports._record_adversary_limit_reached(packet)
        if decision.persistent_decision:
            self.ports.store.append_text_locked(compat.DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update:
            self.ports.store.append_text_locked(compat.PROGRESS, f"- {decision.progress_update}\n")
            compat.patch_health(self.ports.store, compat.HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence))
        if decision.clear_handoff:
            self.ports.store.write_text_locked(compat.HANDOFF, "")
        if decision.display_message:
            self.ports.tui.render("SUPERVISOR", decision.display_message)
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            f"completion/{decision.decision.value}",
            decision=decision.decision.value,
            reason=decision.reason,
        )
        if decision.decision == compat.CompletionReviewDecisionKind.ACCEPT:
            self.state._accepted_completion_decision = decision
            self.state._accepted_adversary_report = packet.adversary_report if packet is not None else None
            await self.ports.finalize(
                f"accepted by completion_review: {decision.reason or 'task complete'}",
                status=compat.BelloStatus.COMPLETE,
                completion_review_accepted=True,
            )
            return
        if decision.decision == compat.CompletionReviewDecisionKind.RETURN:
            await self.ports._return_completion_to_coder(decision)
            return
        if decision.decision == compat.CompletionReviewDecisionKind.RESTART:
            if not getattr(self.ports, "_generation_has_coder_turn", True):
                # Nothing to restart: the current generation has not run a single coder turn, so
                # this verdict can only be judging the previous generation's leftover state. With
                # the restart budget exhausted it would finalize the run as STUCK for no reason.
                self.ports.store.append_text_locked(
                    compat.PROGRESS,
                    "- Discarded completion restart issued before any coder work in the current generation.\n",
                )
                self.ports._append_event(
                    compat.AppEventSource.SUPERVISOR,
                    "completion/restart_discarded_virgin_generation",
                    reason=decision.reason,
                )
                if self.ports.coder:
                    await self.ports._deliver_coder_message(compat.POST_RESTART_CONTINUE_NUDGE)
                return
            self.state.completion_restarts = getattr(self.state, "completion_restarts", 0) + 1
            await self.ports.restart(decision.reason or "completion review requested restart", handoff=decision.handoff)
            return

    async def _run_adversary_before_complete(
        self,
        decision: compat.CompletionReviewDecision | None,
        *,
        packet: compat.SupervisorWakePacket | None,
    ) -> None:
        if packet is None:
            error_summary = "completion packet missing for the adversary run"
            if decision is None:
                await self.ports._fail_required_adversary(packet=None, error_summary=error_summary)
            else:
                await self.ports._complete_after_adversary_unavailable(
                    decision,
                    packet=None,
                    error_summary=error_summary,
                )
            return
        adversary_run_count, max_adversary_runs = self.ports._reserve_adversary_run()
        self.state._adversary_reservation_recovery_pending = False
        self.ports._write_run_checkpoint("adversary", state="active")
        run_reason = (
            "coder readiness" if not self.ports._effective_completion_review()
            else "completion review budget" if decision is None else "completion accept"
        )
        self.ports.tui.render(
            "ADVERSARY",
            f"running pre-complete adversarial tester ({adversary_run_count}/{max_adversary_runs}; {run_reason})",
        )
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Adversarial tester starting before final complete ({adversary_run_count}/{max_adversary_runs}; "
            f"trigger: {run_reason}).\n",
        )
        workspace_state_id = compat._workspace_state_id(self.ports._active_workspace_root())
        snapshot_root: compat.Path | None = None
        previous_report = getattr(self.state, "_pending_adversary_report", None)
        previous_report_payload = previous_report.model_dump(mode="json") if previous_report is not None else None
        try:
            snapshot_root = compat._create_adversary_snapshot(
                self.ports._active_workspace_root(),
                excluded_relative_paths=self.ports._review_private_relative_paths(),
            )
        except Exception as exc:
            error_summary = f"snapshot setup failed: {exc.__class__.__name__}: {exc}"
            if decision is None:
                await self.ports._fail_required_adversary(packet=packet, error_summary=error_summary)
            else:
                await self.ports._complete_after_adversary_unavailable(
                    decision,
                    packet=packet,
                    error_summary=error_summary,
                )
            return
        self.state._active_adversary_workspace_root = snapshot_root
        self.ports._adversary_denied_commands = []
        agent = compat.AdversaryAgent(
            self.ports.client,
            snapshot_root,
            model=self.ports._adversary_model(),
            intelligence=self.ports._adversary_intelligence(),
            on_thread_start=self.ports._mark_adversary_thread_started,
            on_thread_done=self.ports._mark_adversary_thread_done,
            denied_probes=lambda: list(getattr(self.ports, "_adversary_denied_commands", [])),
            multi_agent=self.ports._adversary_multi_agent_config(),
            before_thread_cleanup=self.ports._cleanup_adversary_reviewer_descendants,
        )
        try:
            result = await agent.run(packet, previous_adversary_report=previous_report_payload)
        except compat.AdversaryAgentError as exc:
            if getattr(self.ports, "_transport_error_pending", False):
                return
            if not self.ports._completion_packet_lifecycle_is_current(packet):
                self.ports._record_stale_adversary_discard(
                    packet,
                    reason=f"adversary failed after lifecycle changed: {exc}",
                )
                return
            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
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
                await self.ports._fail_required_adversary(packet=packet, error_summary=str(exc))
            else:
                await self.ports._complete_after_adversary_unavailable(
                    decision,
                    packet=packet,
                    error_summary=str(exc),
                )
            return
        finally:
            self.state._active_adversary_workspace_root = None
            if snapshot_root is not None:
                compat.remove_isolated_workspace_tree(snapshot_root.parent)

        if not self.ports._completion_packet_lifecycle_is_current(packet):
            self.ports._record_stale_adversary_discard(
                packet,
                reason="adversary completed after lifecycle changed",
            )
            return

        report = compat.AdversaryReport(
            candidate_finding=result.candidate_finding,
            report_text=result.report_text,
            thread_id=result.thread_id,
            turn_id=result.turn_id,
            generation=packet.generation,
            completion_wake_sequence=decision.wake_sequence if decision is not None else packet.wake_sequence,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence,
            validation_sequence=compat._latest_validation_sequence(packet.validations),
            workspace_state_id=workspace_state_id,
            created_at=compat.datetime.now(compat.timezone.utc).isoformat(),
        )
        self.state._pending_adversary_report = report
        self.ports._write_run_checkpoint("adversary_report", state="stable")
        self.ports.store.append_raw_log(
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
                "report_sha256": compat.hashlib.sha256(
                    report.report_text.encode("utf-8")
                ).hexdigest(),
            }
        )
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- Adversarial tester completed; adv_report_controller is normalizing findings and observations.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/report_ready",
            decision="normalize",
            reason="pre-complete adversarial report is ready for normalization",
        )
        await self.ports._run_adv_report_controller(
            report,
            packet=packet,
            accepted_completion_decision=decision,
        )

    async def _run_adv_report_controller(
        self,
        report: compat.AdversaryReport,
        *,
        packet: compat.SupervisorWakePacket,
        accepted_completion_decision: compat.CompletionReviewDecision | None,
    ) -> None:
        if not self.ports._completion_packet_lifecycle_is_current(packet):
            self.ports._record_stale_adversary_discard(
                packet,
                reason="adversary report controller skipped after lifecycle changed",
            )
            return
        agent = self.ports._adv_report_controller_agent()
        if agent is None:
            await self.ports._fail_adv_report_controller(
                "adv_report_controller agent is unavailable"
            )
            return
        review_packet = packet.model_copy(
            update={
                "current_summary": "Normalize the completed adversary report for the coder.",
                "adversary_report": report,
            }
        )
        self.ports.tui.render(
            "ADVERSARY", "normalizing adversary findings and observations"
        )
        self.ports._write_run_checkpoint("adversary_report_review", state="active")
        try:
            normalized = await agent.decide_adv_report(review_packet)
        except compat.SupervisorAgentError:
            if getattr(self.ports, "_transport_error_pending", False):
                return
            if not self.ports._completion_packet_lifecycle_is_current(packet):
                self.ports._record_stale_adversary_discard(
                    packet,
                    reason="adversary report controller failed after lifecycle changed",
                )
                return
            await self.ports._fail_adv_report_controller(
                "agent or structured-output failure"
            )
            return

        if not self.ports._completion_packet_lifecycle_is_current(packet):
            self.ports._record_stale_adversary_discard(
                packet,
                reason="adversary report controller completed after lifecycle changed",
            )
            return

        stale_reason = self.ports._adv_report_controller_staleness_reason(report)
        if stale_reason is not None:
            if not self.ports._completion_packet_lifecycle_is_current(packet):
                self.ports._record_stale_adversary_discard(packet, reason=stale_reason)
                return
            await self.ports._fail_adv_report_controller(stale_reason)
            return

        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "adv_report_controller_decision",
                "generation": packet.generation,
                "completion_wake_sequence": report.completion_wake_sequence,
                "forward_to_coder": normalized.forward_to_coder,
                "reason": normalized.reason,
                "report_to_coder": normalized.report_to_coder,
            }
        )
        if normalized.forward_to_coder:
            report_to_coder = compat._adversary_report_with_definitions(
                normalized.report_to_coder or ""
            )
            self.ports._append_event(
                compat.AppEventSource.SUPERVISOR,
                "adversary/report_normalized",
                decision="return",
                reason=normalized.reason,
            )
            return_decision = compat.CompletionReviewDecision(
                decision=compat.CompletionReviewDecisionKind.RETURN,
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
            await self.ports._return_completion_to_coder(
                return_decision,
                source="adversary_report_controller",
            )
            return

        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- adv_report_controller found no findings or observations to send to the coder; finalizing.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/report_normalized",
            decision="complete",
            reason=normalized.reason,
        )
        if accepted_completion_decision is None:
            self.state._accepted_adversary_report = report
            if not self.ports._effective_completion_review():
                await self.ports._finalize_adversary_only("adversary report contained no findings to return")
                return
            await self.ports._finalize_bounded_completion(
                reason=(
                    "completion review budget reached and the normalized adversary "
                    "report had nothing for the coder"
                ),
            )
            return
        await self.ports._finalize_accepted_completion(
            accepted_completion_decision,
            adversary_report=report,
            result=(
                "accepted by completion_review after adversary report normalization: "
                f"{accepted_completion_decision.reason or 'task complete'}"
            ),
        )

    def _adv_report_controller_staleness_reason(
        self,
        report: compat.AdversaryReport,
    ) -> str | None:
        cfg = self.ports.store.get_bello_config()
        if report.generation != cfg.generation:
            return "adversary normalization became stale because the generation changed"
        task_integrity_issue = self.ports._task_integrity_issue()
        if task_integrity_issue is not None:
            return f"adversary normalization detected task integrity failure: {task_integrity_issue}"
        if not report.workspace_state_id:
            return "adversary report is not bound to a workspace state"
        if report.workspace_state_id != compat._workspace_state_id(
            self.ports._active_workspace_root()
        ):
            return "adversary normalization became stale because the workspace changed"
        return None

    def _completion_packet_lifecycle_is_current(self, packet: compat.SupervisorWakePacket) -> bool:
        cfg = self.ports.store.get_bello_config()
        return bool(
            self.ports._coder_lifecycle_accepts_activity(cfg, require_running=False)
            and cfg.generation == packet.generation
            and cfg.coder_thread_id == packet.coder_thread_id
        )

    def _record_stale_adversary_discard(
        self,
        packet: compat.SupervisorWakePacket,
        *,
        reason: str,
    ) -> None:
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "stale_adversary_result_discarded",
                "packet_generation": packet.generation,
                "current_generation": self.ports.store.get_bello_config().generation,
                "packet_thread_id": packet.coder_thread_id,
                "current_thread_id": self.ports.store.get_bello_config().coder_thread_id,
                "reason": reason,
            }
        )

    async def _fail_adv_report_controller(self, error_summary: str) -> None:
        self.ports.tui.render(
            "ADVERSARY", f"adv_report_controller failed: {error_summary}"
        )
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- adv_report_controller failed ({error_summary}); the raw adversary report was not sent to the coder.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/report_controller_failed",
            reason=error_summary,
        )
        await self.ports.finalize(
            f"adv_report_controller failed: {error_summary}",
            status=compat.BelloStatus.PROVIDER_FAILURE,
            completion_review_accepted=False,
        )

    def _completion_review_budget_action(
        self,
        *,
        packet: compat.SupervisorWakePacket | None = None,
    ) -> Literal["adversary", "complete"] | None:
        cfg = self.ports.store.get_bello_config()
        if self.ports._effective_max_adversary_runs() <= 0:
            limit = cfg.max_completion_returns_before_adversary
            if compat.review_limit_reached(limit, cfg.completion_return_count):
                return "complete"
            return None
        if cfg.adversary_run_count == 0:
            limit = cfg.max_completion_returns_before_adversary
            if compat.review_limit_reached(limit, cfg.completion_return_count):
                return "adversary"
            return None
        limit = cfg.max_completion_returns_after_adversary
        if not compat.review_limit_reached(limit, cfg.completion_returns_since_adversary):
            return None
        if self.ports._adversary_runs_remaining():
            return "adversary"
        return "complete"

    async def _finalize_bounded_completion(self, *, reason: str) -> None:
        cfg = self.ports.store.get_bello_config()
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- Bounded completion policy reached its final review budget after the coder applied the last return; "
            "finalizing without fabricating a completion-review accept.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "completion/budget_finalize",
            decision={
                "kind": "complete",
                "completion_return_count": cfg.completion_return_count,
                "completion_returns_since_adversary": cfg.completion_returns_since_adversary,
                "adversary_run_count": cfg.adversary_run_count,
                "max_adversary_runs": self.ports._effective_max_adversary_runs(),
            },
            reason=reason,
        )
        self.state._accepted_completion_decision = None
        await self.ports.finalize(
            "completed normally",
            status=compat.BelloStatus.COMPLETE,
            completion_review_accepted=None,
        )

    async def _fail_required_adversary(
        self,
        *,
        packet: compat.SupervisorWakePacket | None,
        error_summary: str,
    ) -> None:
        cfg = self.ports.store.get_bello_config()
        report = compat.AdversaryReport(
            status="error",
            candidate_finding=False,
            report_text=f"required adversary did not run: {error_summary}",
            generation=packet.generation if packet is not None else cfg.generation,
            completion_wake_sequence=packet.wake_sequence if packet is not None else cfg.last_event_sequence + 1,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence if packet is not None else None,
            validation_sequence=compat._latest_validation_sequence(packet.validations) if packet is not None else None,
            workspace_state_id=compat._workspace_state_id(self.ports._active_workspace_root()),
            created_at=compat.datetime.now(compat.timezone.utc).isoformat(),
        )
        self.state._pending_adversary_report = report
        self.ports.tui.render("ADVERSARY", f"required adversarial tester could not run: {error_summary}")
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Required adversarial tester could not run ({error_summary}); failing the run instead of treating it as accepted.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/required_unavailable",
            reason=error_summary,
        )
        await self.ports.finalize(
            f"required adversary failed under bounded review policy: {error_summary}",
            status=compat.BelloStatus.PROVIDER_FAILURE,
            completion_review_accepted=False,
        )

    async def _finalize_completion_review_disabled(self) -> None:
        """Finalize coder readiness when neither final review is configured."""
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- Coder declared readiness; completion review is disabled by config, finalizing without review.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "completion/review_disabled_finalize",
            reason="coder readiness marker with completion review disabled",
        )
        await self.ports.finalize(
            "coder declared readiness; completion review disabled by config (no review or adversary certification)",
            status=compat.BelloStatus.COMPLETE,
            completion_review_accepted=False,
        )

    async def _finalize_adversary_only(self, reason: str) -> None:
        self.state._accepted_completion_decision = None
        self.ports.store.append_text_locked(compat.PROGRESS, f"- Adversary-only review completed: {reason}; completion review is disabled.\n")
        await self.ports.finalize(
            f"coder completed with adversary-only review: {reason}",
            status=compat.BelloStatus.COMPLETE,
            completion_review_accepted=False,
        )

    async def _finalize_accepted_completion(
        self,
        decision: compat.CompletionReviewDecision,
        *,
        adversary_report: compat.AdversaryReport | None,
        result: str,
    ) -> None:
        cfg = self.ports.store.get_bello_config()
        if decision.persistent_decision:
            self.ports.store.append_text_locked(compat.DECISIONS, f"- {decision.persistent_decision}\n")
        if decision.progress_update:
            self.ports.store.append_text_locked(compat.PROGRESS, f"- {decision.progress_update}\n")
            compat.patch_health(
                self.ports.store,
                compat.HealthDelta(generation=cfg.generation, last_progress_sequence=cfg.last_event_sequence),
            )
        if decision.clear_handoff:
            self.ports.store.write_text_locked(compat.HANDOFF, "")
        if decision.display_message:
            self.ports.tui.render("SUPERVISOR", decision.display_message)
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "completion/accept",
            decision="accept",
            reason=decision.reason,
        )
        self.state._accepted_completion_decision = decision
        self.state._accepted_adversary_report = adversary_report
        await self.ports.finalize(
            result,
            status=compat.BelloStatus.COMPLETE,
            completion_review_accepted=True,
        )

    async def _complete_after_adversary_unavailable(
        self,
        decision: compat.CompletionReviewDecision,
        *,
        packet: compat.SupervisorWakePacket | None,
        error_summary: str,
    ) -> None:
        """The completion review accepted and only the adversary could not run.

        That is a tester-availability problem, not evidence against the accepted work:
        finalize the accept with the missing adversary coverage recorded loudly (same
        terminal shape as adversary-disabled or limit-reached) instead of declaring the
        whole run infrastructure-invalid and discarding a reviewed, accepted solution.
        """
        cfg = self.ports.store.get_bello_config()
        report = compat.AdversaryReport(
            status="error",
            candidate_finding=False,
            report_text=f"adversary did not run: {error_summary}",
            generation=packet.generation if packet is not None else cfg.generation,
            completion_wake_sequence=decision.wake_sequence,
            latest_relevant_change_sequence=packet.latest_relevant_change_sequence if packet is not None else None,
            validation_sequence=compat._latest_validation_sequence(packet.validations) if packet is not None else None,
            workspace_state_id=compat._workspace_state_id(self.ports._active_workspace_root()),
            created_at=compat.datetime.now(compat.timezone.utc).isoformat(),
        )
        self.ports.tui.render(
            "ADVERSARY",
            f"adversarial tester could not run ({error_summary}); finalizing completion accept",
        )
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Adversarial tester could not run ({error_summary}); finalizing prior completion accept "
            "with adversary coverage recorded as missing.\n",
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/unavailable",
            reason=error_summary,
        )
        await self.ports._finalize_accepted_completion(
            decision,
            adversary_report=report,
            result=f"accepted by completion_review; adversary tester could not run: {error_summary}",
        )

    def _adversary_runs_remaining(self) -> bool:
        cfg = self.ports.store.get_bello_config()
        return cfg.adversary_run_count < self.ports._effective_max_adversary_runs()

    def _should_run_adversary_before_complete(self, packet: compat.SupervisorWakePacket | None) -> bool:
        if self.ports._packet_has_fresh_adversary_report(packet):
            return False
        enabled = getattr(self.ports, "adversary_enabled", None)
        if enabled is False:
            return False
        if enabled is True:
            return True
        return self.ports.store.get_bello_config().max_adversary_runs > 0

    def _reserve_adversary_run(self) -> tuple[int, int]:
        max_adversary_runs = self.ports._effective_max_adversary_runs()
        updated = self.ports.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "adversary_run_count": current.adversary_run_count + 1,
                    "completion_returns_since_adversary": 0,
                }
            )
        )
        return updated.adversary_run_count, max_adversary_runs

    def _record_adversary_limit_reached(self, packet: compat.SupervisorWakePacket | None) -> None:
        cfg = self.ports.store.get_bello_config()
        max_adversary_runs = self.ports._effective_max_adversary_runs()
        reason = f"adversary run limit reached ({cfg.adversary_run_count}/{max_adversary_runs})"
        self.ports.tui.render("ADVERSARY", f"{reason}; finalizing completion accept")
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Skipping adversarial tester before complete: {reason}.\n",
        )
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "adversary_limit_reached",
                "generation": packet.generation if packet is not None else None,
                "wake_sequence": packet.wake_sequence if packet is not None else None,
                "adversary_run_count": cfg.adversary_run_count,
                "max_adversary_runs": max_adversary_runs,
            }
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "adversary/limit_reached",
            reason=reason,
        )

    def _effective_max_adversary_runs(self) -> int:
        enabled = getattr(self.ports, "adversary_enabled", None)
        if enabled is False:
            return 0
        override = getattr(self.ports, "adversary_runs", None)
        configured_runs = self.ports.store.get_bello_config().max_adversary_runs if override is None else override
        if enabled is True:
            return max(1, configured_runs)
        return configured_runs

    def _fresh_adversary_report(
        self,
        *,
        generation: int,
        latest_relevant_change_sequence: int | None,
    ) -> compat.AdversaryReport | None:
        report = getattr(self.state, "_pending_adversary_report", None)
        if report is None:
            return None
        if report.status != "completed" or report.generation != generation:
            return None
        if report.latest_relevant_change_sequence != latest_relevant_change_sequence:
            return None
        if report.workspace_state_id and report.workspace_state_id != compat._workspace_state_id(self.ports._active_workspace_root()):
            return None
        return report

    def _packet_has_fresh_adversary_report(self, packet: compat.SupervisorWakePacket | None) -> bool:
        if packet is None:
            return False
        report = packet.adversary_report
        if report is None:
            return False
        if report.status != "completed" or report.generation != packet.generation:
            return False
        if report.latest_relevant_change_sequence != packet.latest_relevant_change_sequence:
            return False
        if report.workspace_state_id and report.workspace_state_id != compat._workspace_state_id(self.ports._active_workspace_root()):
            return False
        return True

    def _mark_adversary_thread_started(self, thread_id: str) -> None:
        self.state._active_adversary_thread_id = thread_id
        self.ports._register_reviewer_thread(thread_id, role="adversary")

    def _mark_adversary_thread_done(self, thread_id: str) -> None:
        if getattr(self.state, "_active_adversary_thread_id", None) == thread_id:
            self.state._active_adversary_thread_id = None

    def _append_completion_anchor_log(
        self,
        decision: compat.CompletionReviewDecision,
        *,
        packet: compat.SupervisorWakePacket | None,
    ) -> None:
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
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
        decision: compat.CompletionReviewDecision,
        *,
        source: Literal["completion_review", "adversary_report_controller"] = "completion_review",
    ) -> None:
        record = compat.CompletionReturnRecord(
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
        self.state.completion_returns = [*getattr(self.state, "completion_returns", []), record][-50:]
        self.state.completion_review_return_sequence = decision.wake_sequence
        if not decision.progress_update:
            details = compat._completion_return_summary(decision)
            source_label = (
                "Adversary report controller"
                if source == "adversary_report_controller"
                else "Completion review"
            )
            self.ports.store.append_text_locked(
                compat.PROGRESS, f"- {source_label} returned: {details}\n"
            )
        self.ports.prior_interventions.append(
            compat.PriorIntervention(
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
        self.ports.prior_interventions = self.ports.prior_interventions[-20:]
        if source != "adversary_report_controller":
            self.ports.store.update_bello_config(
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
        if self.ports.coder and decision.message_to_coder:
            if self.ports._revision_coder_enabled() and not self.ports._revision_coder_active():
                try:
                    await self.ports._switch_to_revision_coder(decision.message_to_coder, source=source)
                except compat._RevisionCoderDeliveryError as exc:
                    if not self.ports._revision_switch_context_is_current(
                        generation=exc.generation,
                        thread_id=exc.thread_id,
                        coder=exc.coder,
                    ):
                        self.ports._record_cancelled_revision_switch(
                            f"lifecycle changed while revision coder {exc.stage} failed"
                        )
                        return
                    await self.ports._fail_revision_coder_switch(exc, source=source)
                    return
            else:
                await self.ports._deliver_coder_message(decision.message_to_coder)
        # Fresh completion-review thread per review: close the session after each return so
        # the next readiness review starts a new thread instead of accumulating prior turns.
        # The persistent thread otherwise grows ~55-85k tokens per return and crossed the
        # model context window within a generation, forcing lossy auto-compaction. Prior
        # returns are still carried into the next review via previous_completion_returns,
        # and the reviewer re-reads the workspace live, so no context is lost.
        supervisor = self.ports._completion_supervisor_agent()
        if supervisor is not None and hasattr(supervisor, "close_completion_review"):
            await supervisor.close_completion_review()

    def _completion_knowledge(self) -> dict[str, list[compat.Any]]:
        # In-memory only (see __init__): survives in-run restarts because the controller object
        # is reused, and is never sourced from the coder-writable workspace. Lazily initialized
        # so controllers built via __new__ in tests still work.
        state = getattr(self.state, "_completion_knowledge_state", None)
        if state is None:
            state = {"behavior_surface": [], "uncovered_edge_candidates": []}
            self.state._completion_knowledge_state = state
        return state

    def _behavior_surface_items(self) -> list[compat.BehaviorSurfaceItem]:
        items: list[compat.BehaviorSurfaceItem] = []
        for entry in self.ports._completion_knowledge()["behavior_surface"]:
            try:
                items.append(compat.BehaviorSurfaceItem.model_validate(entry))
            except Exception:
                continue
        return items

    def _record_completion_knowledge(self, decision: compat.CompletionReviewDecision) -> None:
        """Merge the reviewer-returned surface (merge-only: entries are never removed) into the
        in-memory knowledge and carry its unverified suspicions to the next review."""
        knowledge = self.ports._completion_knowledge()
        merged, changed = compat._merge_behavior_surface_items(knowledge["behavior_surface"], decision.behavior_surface)
        if changed:
            knowledge["behavior_surface"] = merged
        artifact = decision.decision_artifact
        if artifact is not None:
            candidates = [item for item in artifact.uncovered_edge_candidates if isinstance(item, str) and item.strip()]
            if candidates != knowledge["uncovered_edge_candidates"]:
                knowledge["uncovered_edge_candidates"] = candidates

    def _readiness_journal(self) -> compat.deque[compat._ReadinessJournalEvent]:
        limit = max(
            1,
            int(
                getattr(
                    self.ports,
                    "_readiness_event_journal_limit",
                    compat.READINESS_EVENT_JOURNAL_LIMIT,
                )
            ),
        )
        journal = getattr(self.state, "_readiness_event_journal", None)
        if not isinstance(journal, compat.deque) or journal.maxlen != limit:
            journal = compat.deque(journal or (), maxlen=limit)
            self.state._readiness_event_journal = journal
        return journal
