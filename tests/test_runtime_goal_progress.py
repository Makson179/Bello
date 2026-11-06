"""Neutral cadence/advisory checks; no provider calls or campaign access."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from supervisor.runtime_goal import (
    GOAL_PROGRESS_ADVICE_COOLDOWN_SECONDS,
    GOAL_PROGRESS_REVIEW_INTERVAL_SECONDS,
    GoalRuntimeProgressPolicy,
)


def due(policy: GoalRuntimeProgressPolicy, now: float, sequence: int = 1, **kwargs) -> bool:
    return policy.maybe_due(
        now=now,
        goal_enabled=kwargs.pop("goal_enabled", True),
        runtime_enabled=kwargs.pop("runtime_enabled", True),
        meaningful_sequence=sequence,
        **kwargs,
    )


def test_default_cadence_is_twenty_minutes_and_advice_is_less_frequent() -> None:
    assert GOAL_PROGRESS_REVIEW_INTERVAL_SECONDS == 1200
    assert GOAL_PROGRESS_ADVICE_COOLDOWN_SECONDS == 1800


def test_startup_is_not_a_progress_audit_and_continuous_work_does_not_starve_review() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 100, 1)
    assert not due(policy, 400, 2)
    assert not due(policy, 1000, 3)  # The previous 15-minute deadline is too early.
    assert not due(policy, 1299, 4)
    assert due(policy, 1300, 5)


def test_maybe_due_does_not_consume_slot_before_enqueue_succeeds() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 0)
    assert not due(policy, 900)
    assert not due(policy, 1199)
    assert due(policy, 1200)
    assert due(policy, 1201)
    policy.mark_requested(now=1201, meaningful_sequence=1)
    assert not due(policy, 1202, 2)
    assert not due(policy, 2400, 2)
    assert due(policy, 2401, 2)


def test_unchanged_work_is_not_repeatedly_audited_after_a_noop() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 0, 10, evidence_fingerprint="map:A")
    assert due(policy, 1200, 10, evidence_fingerprint="map:A")
    policy.mark_requested(now=1200, meaningful_sequence=10, evidence_fingerprint="map:A")
    assert not due(policy, 2400, 10, evidence_fingerprint="map:A")
    assert not due(policy, 9000, 10, evidence_fingerprint="map:A")
    assert due(policy, 9000, 11, evidence_fingerprint="map:A")


def test_new_external_evidence_can_trigger_without_a_new_coder_sequence() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0, 10, evidence_fingerprint="result:pending")
    policy.mark_requested(now=1200, meaningful_sequence=10, evidence_fingerprint="result:pending")
    assert not due(policy, 1201, 10, evidence_fingerprint="result:complete")
    assert due(policy, 2400, 10, evidence_fingerprint="result:complete")


def test_missing_map_never_blocks_a_progress_review() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0)
    assert due(policy, 1200)


def test_preexisting_map_alone_does_not_wake_a_run_that_has_done_no_work() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 0, 0, evidence_fingerprint="old-map")
    assert not due(policy, 9000, 0, evidence_fingerprint="old-map")


def test_long_startup_wait_does_not_spend_the_first_actual_work_grace() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 0, 0)
    assert not due(policy, 9000, 0)
    assert not due(policy, 9001, 1)
    assert not due(policy, 10200, 2)
    assert due(policy, 10201, 3)


@pytest.mark.parametrize("blocker", ["review_running", "approval_pending"])
def test_busy_review_or_approval_defers_without_starting_a_second_review(blocker: str) -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0)
    assert not due(policy, 1200, **{blocker: True})
    assert not due(policy, 2400, **{blocker: True})
    assert due(policy, 2401)
    policy.mark_requested(now=2401, meaningful_sequence=1)
    assert not due(policy, 2402, 2)


@pytest.mark.parametrize("disabled", ["goal_enabled", "runtime_enabled", "runnable"])
def test_non_goal_disabled_or_paused_state_is_inert_and_reentry_has_full_grace(disabled: str) -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not due(policy, 0, **{disabled: False})
    assert not due(policy, 9000, **{disabled: False})
    assert not due(policy, 9001)
    assert not due(policy, 10200)
    assert due(policy, 10201)
    policy.mark_requested(now=10201, meaningful_sequence=1)
    assert not due(policy, 11_000, 2, **{disabled: False})
    assert not due(policy, 20_000, 2)
    assert not due(policy, 21_199, 2)
    assert due(policy, 21_200, 2)


def test_request_captures_start_evidence_not_work_completed_while_review_runs() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0, 10)
    policy.mark_requested(now=1200, meaningful_sequence=10, evidence_fingerprint="A")
    assert not due(policy, 2400, 11, evidence_fingerprint="B", review_running=True)
    assert due(policy, 2401, 11, evidence_fingerprint="B")


def test_review_error_cannot_create_immediate_periodic_retry() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0)
    policy.mark_requested(now=1200, meaningful_sequence=1)
    assert not due(policy, 1201, 2)
    assert not due(policy, 2400, 1)
    assert due(policy, 2400, 2)


def test_rewound_sequence_is_not_itself_new_progress() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0, 10)
    policy.mark_requested(now=1200, meaningful_sequence=10)
    assert not due(policy, 2400, 9)


def test_advice_requires_evidence_and_does_not_consume_slot_until_delivered() -> None:
    policy = GoalRuntimeProgressPolicy()
    assert not policy.can_deliver(now=0, evidence_key=None)
    assert not policy.can_deliver(now=0, evidence_key="  ")
    assert policy.can_deliver(now=0, evidence_key="approach:A:v3-v5")
    assert policy.can_deliver(now=1, evidence_key="approach:A:v3-v5")
    policy.mark_delivered(now=1, evidence_key="approach:A:v3-v5")
    assert not policy.can_deliver(now=1801, evidence_key="approach:A:v3-v5")


def test_advice_cooldown_applies_even_to_a_different_finding() -> None:
    policy = GoalRuntimeProgressPolicy()
    policy.mark_delivered(now=100, evidence_key="approach:A")
    assert not policy.can_deliver(now=1899, evidence_key="approach:B")
    assert policy.can_deliver(now=1900, evidence_key="approach:B")


def test_advice_does_not_reset_review_cadence_or_create_progress() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0, 10)
    policy.mark_requested(now=1200, meaningful_sequence=10)
    policy.mark_delivered(now=1300, evidence_key="approach:A")
    assert not due(policy, 2400, 10)
    assert due(policy, 2400, 11)


def test_review_does_not_reset_advice_cooldown_or_allow_duplicate_nudges() -> None:
    policy = GoalRuntimeProgressPolicy()
    policy.mark_delivered(now=100, evidence_key="approach:A")
    policy.mark_requested(now=1000, meaningful_sequence=20)
    assert not policy.can_deliver(now=1900, evidence_key="approach:A")
    assert policy.can_deliver(now=1900, evidence_key="approach:B")


def test_advice_history_is_bounded_but_latest_duplicate_remains_suppressed() -> None:
    policy = GoalRuntimeProgressPolicy(remembered_advice=2)
    for now, key in [(0, "A"), (1800, "B"), (3600, "C")]:
        assert policy.can_deliver(now=now, evidence_key=key)
        policy.mark_delivered(now=now, evidence_key=key)
    assert not policy.can_deliver(now=5400, evidence_key="B")
    assert not policy.can_deliver(now=5400, evidence_key="C")
    assert policy.can_deliver(now=5400, evidence_key="A")


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_intervals_fail_explicitly(value: float) -> None:
    with pytest.raises(ValueError):
        GoalRuntimeProgressPolicy(review_interval_seconds=value)
    with pytest.raises(ValueError):
        GoalRuntimeProgressPolicy(advice_cooldown_seconds=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_history_limit(value) -> None:
    with pytest.raises(ValueError):
        GoalRuntimeProgressPolicy(remembered_advice=value)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_clock(value: float) -> None:
    with pytest.raises(ValueError):
        due(GoalRuntimeProgressPolicy(), value)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_invalid_progress_sequence(value) -> None:
    with pytest.raises(ValueError):
        due(GoalRuntimeProgressPolicy(), 0, value)


def test_delivered_advice_needs_a_key() -> None:
    with pytest.raises(ValueError):
        GoalRuntimeProgressPolicy().mark_delivered(now=0, evidence_key=" ")


def test_review_cadence_reset_preserves_advice_cooldown_and_history() -> None:
    policy = GoalRuntimeProgressPolicy()
    due(policy, 0, 10)
    policy.mark_requested(now=1200, meaningful_sequence=10)
    policy.mark_delivered(now=1300, evidence_key="attempts:1-4")
    policy.reset_review_cadence()
    assert not due(policy, 2000, 20)
    assert not due(policy, 3199, 21)
    assert due(policy, 3200, 22)
    assert not policy.can_deliver(now=3099, evidence_key="different-attempts")
    assert policy.can_deliver(now=3100, evidence_key="different-attempts")
    assert not policy.can_deliver(now=5000, evidence_key="attempts:1-4")


@pytest.mark.parametrize("identity_change", ["generation", "thread"])
def test_controller_new_identity_ignores_old_work_and_gets_fresh_review_grace(tmp_path, monkeypatch, identity_change) -> None:
    from supervisor.schemas import CoderMessage
    from tests.support.controller import _runtime_controller, _runtime_failure_validation

    controller, store, _ = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "last_event_sequence": 10}))
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    calls = []

    def schedule(summary, **_kwargs):
        calls.append(summary)
        controller._supervisor_task = SimpleNamespace(done=lambda: False)

    controller._schedule_supervisor_check = schedule
    controller._record_goal_work(10)
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 1200
    controller._maybe_schedule_goal_progress_review()
    assert len(calls) == 1
    controller._supervisor_task = None
    controller.validations = [_runtime_failure_validation(sequence=10, output="old neutral result")]
    controller.last_coder_message = CoderMessage(text="Old generation message.", sequence=10)
    update = {"generation": 1} if identity_change == "generation" else {"coder_thread_id": "new-thread"}
    store.update_bello_config(lambda cfg: cfg.model_copy(update={**update, "last_event_sequence": 20}))
    clock[0] = 2000
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 9000
    controller._maybe_schedule_goal_progress_review()
    assert len(calls) == 1  # Old ledger cannot arm a new-generation review.
    controller._record_goal_work(21)
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 10199
    controller._maybe_schedule_goal_progress_review()
    assert len(calls) == 1
    clock[0] = 10200
    controller._maybe_schedule_goal_progress_review()
    assert len(calls) == 2


def test_first_new_generation_action_before_poll_is_retained_with_full_grace(tmp_path, monkeypatch) -> None:
    from tests.support.controller import _runtime_controller

    controller, store, _ = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "last_event_sequence": 10}))
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    calls = []

    def schedule(summary, **_kwargs):
        calls.append(summary)
        controller._supervisor_task = SimpleNamespace(done=lambda: False)

    controller._schedule_supervisor_check = schedule
    controller._record_goal_work(10)
    controller._maybe_schedule_goal_progress_review()
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"generation": 1, "last_event_sequence": 21}))
    clock[0] = 1000
    controller._record_goal_work(21)  # The action, not a timer, discovers restart.
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 2199
    controller._maybe_schedule_goal_progress_review()
    assert calls == []
    clock[0] = 2200
    controller._maybe_schedule_goal_progress_review()
    assert len(calls) == 1
