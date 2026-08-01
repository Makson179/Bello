from __future__ import annotations

from supervisor.schemas import HealthDelta, HealthState
from supervisor.schemas.models import RestartIssueState
from supervisor.state import StateStore


MAX_TRACKED_RESTART_ISSUES = 8


def _restart_issues(state: HealthState) -> dict[str, RestartIssueState]:
    issues = dict(state.restart_issues)
    if state.restart_issue_key is not None and state.restart_issue_key not in issues:
        issues[state.restart_issue_key] = RestartIssueState(
            interventions=state.restart_issue_interventions,
            last_sequence=state.restart_issue_last_sequence,
            validation_id=state.restart_issue_validation_id,
        )
    return issues


def _legacy_restart_issue_fields(
    issues: dict[str, RestartIssueState],
) -> dict[str, str | int | None]:
    if not issues:
        return {
            "restart_issue_key": None,
            "restart_issue_interventions": 0,
            "restart_issue_last_sequence": 0,
            "restart_issue_validation_id": None,
        }
    key, issue = max(
        issues.items(),
        key=lambda item: (item[1].last_sequence, item[0]),
    )
    return {
        "restart_issue_key": key,
        "restart_issue_interventions": issue.interventions,
        "restart_issue_last_sequence": issue.last_sequence,
        "restart_issue_validation_id": issue.validation_id,
    }


def apply_delta(state: HealthState, delta: HealthDelta) -> HealthState:
    if state.generation != delta.generation:
        return state
    if delta.reset_generation_scoped:
        state.denied_requests = 0
        state.consecutive_failed_tests = 0
        state.repeated_command_count = 0
        state.interventions = 0
        state.minutes_without_progress = 0
        state.risk_signals = []
        state.last_denial = None
        state.timeout_fallback_count = 0
        state.parse_failure_count = 0
        state.last_progress_sequence = 0
        state.restart_issue_key = None
        state.restart_issue_interventions = 0
        state.restart_issue_last_sequence = 0
        state.restart_issue_validation_id = None
        state.restart_issues = {}
    state.denied_requests += delta.denied_requests
    state.consecutive_failed_tests += delta.consecutive_failed_tests
    state.repeated_command_count += delta.repeated_command_count
    state.interventions += delta.interventions
    state.minutes_without_progress += delta.minutes_without_progress
    state.timeout_fallback_count += delta.timeout_fallback_count
    state.parse_failure_count += delta.parse_failure_count
    state.restart_count += delta.restart_count
    if delta.new_generation is not None:
        state.generation = delta.new_generation
    if delta.last_denial is not None:
        state.last_denial = delta.last_denial
    if delta.last_progress_sequence is not None:
        state.last_progress_sequence = max(
            state.last_progress_sequence, delta.last_progress_sequence
        )
        state.minutes_without_progress = 0
        state.consecutive_failed_tests = 0
        state.repeated_command_count = 0
    if delta.clear_risk_signals:
        state.risk_signals = []
    for signal in delta.add_risk_signals:
        if signal not in state.risk_signals:
            state.risk_signals.append(signal)
    return state


def patch_health(store: StateStore, delta: HealthDelta) -> HealthState:
    return store.patch_health(lambda current: apply_delta(current, delta))


def record_restart_issue_intervention(
    store: StateStore,
    *,
    generation: int,
    issue_key: str,
    sequence: int,
    validation_id: str | None,
) -> HealthState:
    def patch(state: HealthState) -> HealthState:
        if state.generation != generation:
            return state
        issues = _restart_issues(state)
        existing = issues.get(issue_key)
        issues[issue_key] = RestartIssueState(
            interventions=(existing.interventions + 1 if existing is not None else 1),
            last_sequence=(
                max(existing.last_sequence, sequence)
                if existing is not None
                else sequence
            ),
            validation_id=validation_id,
        )
        if len(issues) > MAX_TRACKED_RESTART_ISSUES:
            oldest = sorted(
                issues,
                key=lambda key: (issues[key].last_sequence, key),
            )[: len(issues) - MAX_TRACKED_RESTART_ISSUES]
            for key in oldest:
                del issues[key]
        return state.model_copy(
            update={
                "restart_issues": issues,
                **_legacy_restart_issue_fields(issues),
            }
        )

    return store.patch_health(patch)


def clear_restart_issue_for_validation(
    store: StateStore,
    *,
    generation: int,
    validation_id: str,
    sequence: int,
    matching_issue_keys: tuple[str, ...] = (),
) -> HealthState:
    def patch(state: HealthState) -> HealthState:
        if state.generation != generation:
            return state
        issues = _restart_issues(state)
        cleared = [
            key
            for key, issue in issues.items()
            if sequence > issue.last_sequence
            and (issue.validation_id == validation_id or key in matching_issue_keys)
        ]
        if not cleared:
            return state
        for key in cleared:
            del issues[key]
        return state.model_copy(
            update={
                "restart_issues": issues,
                **_legacy_restart_issue_fields(issues),
            }
        )

    return store.patch_health(patch)


def kill_restart_candidate(
    state: HealthState,
    *,
    issue_key: str | None = None,
    issue_sequence: int | None = None,
) -> tuple[bool, str | None]:
    # Keep this aligned with BelloConfig.max_restarts (schemas/models.py).
    if state.restart_count >= 5:
        return True, "restart cap reached"
    issue = _restart_issues(state).get(issue_key) if issue_key is not None else None
    if (
        issue is not None
        and issue.interventions >= 2
        and issue_sequence is not None
        and issue_sequence > issue.last_sequence
    ):
        return True, "same issue repeated after two interventions"
    if state.repeated_command_count >= 4:
        return True, "four substantially similar failing commands"
    if state.minutes_without_progress >= 15:
        return True, "fifteen minutes without progress"
    if "bypass_after_denial" in state.risk_signals:
        return True, "bypass/rephrase attempt after denial"
    if "completion_non_convergence" in state.risk_signals:
        return True, "completion review non-convergence"
    if "no_marker_idle_loop" in state.risk_signals:
        return True, "coder repeatedly idled without readiness marker"
    if state.timeout_fallback_count >= 3:
        return True, "repeated timeout fallbacks"
    if state.parse_failure_count >= 3:
        return True, "repeated parse failures"
    return False, None
