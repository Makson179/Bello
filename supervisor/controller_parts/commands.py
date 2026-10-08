"""Commands service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class CommandsState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _command_output_chunks: dict[str, list[str]]
    inspections: list[compat.InspectionRun]
    validation_runtime_state: dict[str, dict[str, Any]]
    validations: list[compat.ValidationRun]


class CommandsPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_append_event',
        '_current_turn_action_count',
        '_declared_grading_access_issue',
        '_escalate_runtime_integrity_issue',
        '_exposes_review_private_input',
        '_handle_completed_coder_action',
        '_pop_command_output',
        '_record_changed_files',
        '_record_runtime_trigger_trace',
        '_record_validation_progress',
        '_record_validation_runtime_state',
        '_repair_snapshot_runtime_controls',
        '_runtime_enabled',
        '_schedule_supervisor_check',
        '_sequence',
        '_subagent_registry',
        '_update_relevant_edit_state',
        'changed_files',
        'finalize',
        'observed_changed_files',
        'should_wake_runtime_supervisor',
        'store',
        'tui',
    })
    writes = frozenset({
        '_current_turn_action_count',
    })


class Commands:
    """Own commands behavior; borrow only the declared port."""

    def __init__(self, ports: CommandsPort) -> None:
        self.state = CommandsState()
        self.ports = ports

    def _record_command_output_delta(self, method: str, params: dict[str, compat.Any], *, item_id: str | None) -> None:
        if not compat._is_command_output_delta_method(method):
            return
        if not item_id:
            return
        text = compat._output_delta_text(params)
        if not text:
            return
        chunks = getattr(self.state, "_command_output_chunks", None)
        if chunks is None:
            chunks = {}
            self.state._command_output_chunks = chunks
        chunks.setdefault(item_id, []).append(text)

    def _pop_command_output(self, item_id: str | None) -> str:
        if not item_id:
            return ""
        chunks = getattr(self.state, "_command_output_chunks", None)
        if not chunks:
            return ""
        return "".join(chunks.pop(item_id, []))

    async def _handle_subagent_item_completed(
        self,
        item: compat.Any,
        *,
        item_id: str | None,
        method: str,
        thread_id: str | None,
    ) -> None:
        if not isinstance(thread_id, str):
            return
        state = self.ports._subagent_registry().get(thread_id)
        if state is None:
            return
        if isinstance(item, dict) and item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                state.last_message = compat._bounded_subagent_text(text)
                state.last_sequence = self.ports._sequence
                self.ports.tui.render("SUBAGENT", f"{compat._short_thread_id(thread_id)}: {state.last_message}")
            return
        if compat._is_completed_action(item):
            await self.ports._handle_completed_coder_action(
                item,
                item_id=item_id,
                method=method,
                thread_id=thread_id,
                is_subagent=True,
            )

    async def _handle_completed_coder_action(
        self,
        item: dict[str, compat.Any],
        *,
        item_id: str | None,
        method: str,
        thread_id: str | None,
        is_subagent: bool,
    ) -> None:
        summary = compat._item_summary(item)
        display_summary = (
            f"subagent {compat._short_thread_id(thread_id)}: {summary}" if is_subagent else summary
        )
        if not is_subagent:
            self.ports._current_turn_action_count = getattr(self.ports, "_current_turn_action_count", 0) + 1
        persisted_summary = display_summary
        if self.ports._exposes_review_private_input((item, display_summary)):
            persisted_summary = (
                "subagent workspace action completed"
                if is_subagent
                else "workspace action completed"
            )
        self.ports.store.append_recent_action(persisted_summary)
        triggering_action = compat._triggering_action_from_item(item, item_id=item_id, summary=display_summary)
        repaired_runtime_controls = self.ports._repair_snapshot_runtime_controls(
            source="subagent_action" if is_subagent else "coder_action"
        )
        if await self.ports._escalate_runtime_integrity_issue(
            source="subagent_action" if is_subagent else "coder_action"
        ):
            return
        self.ports._record_changed_files(triggering_action)
        declared_grading_issue = self.ports._declared_grading_access_issue(triggering_action)
        if declared_grading_issue is not None:
            if is_subagent:
                declared_grading_issue = (
                    f"subagent {compat._short_thread_id(thread_id)}: {declared_grading_issue}"
                )
            self.ports.tui.render("INTEGRITY", declared_grading_issue)
            self.ports.store.append_text_locked(compat.PROGRESS, f"- Integrity failure: {declared_grading_issue}\n")
            self.ports._append_event(
                compat.AppEventSource.SUPERVISOR,
                "integrity/declared_grading_path_access",
                thread_id=thread_id,
                reason=declared_grading_issue,
            )
            await self.ports.finalize(
                f"escalated: {declared_grading_issue}",
                status=compat.BelloStatus.ESCALATED,
            )
            return
        validation_item = compat._item_with_recorded_output(item, self.ports._pop_command_output(item_id))
        validation = compat._validation_from_action(
            triggering_action,
            sequence=self.ports._sequence,
            item=validation_item,
            changed_paths=list(getattr(self.ports, "observed_changed_files", {}) or {}),
        )
        inspection = compat._inspection_from_action(
            triggering_action,
            sequence=self.ports._sequence,
            item=validation_item,
        )
        if validation is not None and self.ports._exposes_review_private_input(validation):
            validation = None
        if inspection is not None and self.ports._exposes_review_private_input(inspection):
            inspection = None
        validation_trigger_reasons: tuple[str, ...] = ()
        if validation is not None:
            self.state.validations.append(validation)
            self.state.validations = self.state.validations[-compat.VALIDATION_LEDGER_LIMIT:]
            self.ports._record_validation_progress(validation)
            validation_trigger_reasons = self.ports._record_validation_runtime_state(validation)
        if inspection is not None:
            self.state.inspections.append(inspection)
            self.state.inspections = self.state.inspections[-compat.INSPECTION_LEDGER_LIMIT:]
        changed_files = await self.ports.changed_files()
        self.ports._update_relevant_edit_state(changed_files)
        if is_subagent:
            state = self.ports._subagent_registry().get(thread_id or "")
            if state is not None:
                state.record_action(
                    display_summary,
                    sequence=self.ports._sequence,
                    kind=str(item.get("type") or "action"),
                    item_id=item_id,
                )
                if validation is not None:
                    state.validation_ids.append(validation.validation_id)
                    state.validation_ids = state.validation_ids[-compat.SUBAGENT_ACTION_LIMIT:]
            self.ports.tui.render("SUBAGENT", display_summary)
            return
        runtime_decision = self.ports.should_wake_runtime_supervisor(
            action=triggering_action,
            validation=validation,
            changed_files=changed_files,
            validation_trigger_reasons=validation_trigger_reasons,
        )
        if repaired_runtime_controls and self.ports._runtime_enabled():
            runtime_decision = compat.RuntimeTriggerDecision(
                should_wake=True,
                reasons=tuple(dict.fromkeys((*runtime_decision.reasons, "runtime_control_replacement"))),
                restart_reason=runtime_decision.restart_reason,
            )
        self.ports.tui.render("TOOL", summary)
        self.ports._record_runtime_trigger_trace(
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
            self.ports._schedule_supervisor_check(
                runtime_summary,
                triggering_item_id=item_id,
                triggering_action=triggering_action,
                patch_summary=compat._patch_summary_from_item(item),
            )

    def _record_validation_progress(self, validation: compat.ValidationRun) -> None:
        def patch(current: compat.BelloConfig) -> compat.BelloConfig:
            updates: dict[str, compat.Any] = {"last_validation_sequence": validation.sequence}
            if compat._is_behavior_proving_validation(validation) and validation.trusted_validation_outcome != "masked_or_unknown":
                updates["last_trusted_behavioral_validation_sequence"] = validation.sequence
            if compat._validation_is_usable_behavioral_pass(validation):
                updates["last_trusted_passing_behavioral_validation_sequence"] = validation.sequence
            return current.model_copy(update=updates)

        self.ports.store.update_bello_config(patch)

    def _record_validation_runtime_state(self, validation: compat.ValidationRun) -> tuple[str, ...]:
        key = validation.validation_id
        state = getattr(self.state, "validation_runtime_state", None)
        if state is None:
            state = {}
            self.state.validation_runtime_state = state
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
            compat.clear_restart_issue_for_validation(
                self.ports.store,
                generation=self.ports.store.get_bello_config().generation,
                validation_id=validation.validation_id,
                sequence=validation.sequence,
                matching_issue_keys=(
                    compat._runtime_unresolved_execution_key(validation.command, validation.cwd),
                ),
            )
        return tuple(dict.fromkeys(reasons))
