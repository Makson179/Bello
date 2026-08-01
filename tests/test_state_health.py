from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from supervisor.health import (
    clear_restart_issue_for_validation,
    kill_restart_candidate,
    patch_health,
    record_restart_issue_intervention,
)
from supervisor.schemas import HealthDelta
from supervisor.state import HEALTH, StateStore


def test_health_concurrent_delta_application(store: StateStore) -> None:
    def increment() -> None:
        patch_health(store, HealthDelta(generation=0, denied_requests=1, interventions=1))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: increment(), range(100)))

    health = store.get_health()
    assert health.denied_requests == 100
    assert health.interventions == 100


def test_progress_update_preserves_active_task_intervention_count(store: StateStore) -> None:
    patch_health(store, HealthDelta(generation=0, interventions=2))
    patch_health(store, HealthDelta(generation=0, last_progress_sequence=1071))

    health = store.get_health()
    assert health.last_progress_sequence == 1071
    assert health.interventions == 2


def test_restart_issue_counts_matching_interventions_independently(
    store: StateStore,
) -> None:
    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="failure-a",
        sequence=10,
        validation_id="validation-a",
    )
    patch_health(store, HealthDelta(generation=0, last_progress_sequence=11))
    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="failure-a",
        sequence=12,
        validation_id="validation-a",
    )

    health = store.get_health()
    assert health.restart_issue_interventions == 2
    assert kill_restart_candidate(health, issue_key="failure-a", issue_sequence=12) == (
        False,
        None,
    )
    assert kill_restart_candidate(health, issue_key="failure-a", issue_sequence=13) == (
        True,
        "same issue repeated after two interventions",
    )
    assert kill_restart_candidate(health, issue_key="failure-b", issue_sequence=13) == (
        False,
        None,
    )

    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="failure-b",
        sequence=13,
        validation_id="validation-b",
    )

    health = store.get_health()
    assert health.restart_issue_key == "failure-b"
    assert health.restart_issue_interventions == 1
    assert health.restart_issues["failure-a"].interventions == 2
    assert health.restart_issues["failure-b"].interventions == 1
    assert kill_restart_candidate(health, issue_key="failure-a", issue_sequence=14) == (
        True,
        "same issue repeated after two interventions",
    )
    assert kill_restart_candidate(health, issue_key="failure-b", issue_sequence=14) == (
        False,
        None,
    )


def test_restart_issue_ledger_is_bounded_without_merging_distinct_issues(
    store: StateStore,
) -> None:
    for index in range(10):
        record_restart_issue_intervention(
            store,
            generation=0,
            issue_key=f"failure-{index}",
            sequence=index + 1,
            validation_id=f"validation-{index}",
        )

    health = store.get_health()
    assert list(health.restart_issues) == [f"failure-{index}" for index in range(2, 10)]
    assert all(issue.interventions == 1 for issue in health.restart_issues.values())


def test_restart_issue_clears_only_for_its_validation_or_new_generation(
    store: StateStore,
) -> None:
    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="failure-a",
        sequence=10,
        validation_id="validation-a",
    )

    clear_restart_issue_for_validation(
        store,
        generation=0,
        validation_id="validation-b",
        sequence=11,
    )
    assert store.get_health().restart_issue_key == "failure-a"

    clear_restart_issue_for_validation(
        store,
        generation=0,
        validation_id="validation-a",
        sequence=10,
    )
    assert store.get_health().restart_issue_key == "failure-a"

    clear_restart_issue_for_validation(
        store,
        generation=0,
        validation_id="validation-a",
        sequence=11,
    )
    health = store.get_health()
    assert health.restart_issue_key is None
    assert health.restart_issue_interventions == 0
    assert health.restart_issues == {}

    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="failure-c",
        sequence=20,
        validation_id=None,
    )
    patch_health(
        store,
        HealthDelta(generation=0, reset_generation_scoped=True, new_generation=1),
    )
    health = store.get_health()
    assert health.generation == 1
    assert health.restart_issue_key is None
    assert health.restart_issue_interventions == 0


def test_restart_issue_clears_for_equivalent_issue_key(store: StateStore) -> None:
    record_restart_issue_intervention(
        store,
        generation=0,
        issue_key="unresolved-execution:build",
        sequence=10,
        validation_id="validation-nested-shell",
    )

    clear_restart_issue_for_validation(
        store,
        generation=0,
        validation_id="validation-direct-shell",
        sequence=11,
        matching_issue_keys=("unresolved-execution:build",),
    )

    health = store.get_health()
    assert health.restart_issue_key is None
    assert health.restart_issue_interventions == 0


def test_atomic_write_behavior(store: StateStore) -> None:
    store.write_json_locked(HEALTH, {"generation": 0, "restart_count": 2})
    assert json.loads(store.path(HEALTH).read_text(encoding="utf-8"))["restart_count"] == 2
    health = store.get_health()
    assert health.restart_issue_key is None
    assert health.restart_issue_interventions == 0
    assert not list(store.state_dir.glob("*.tmp"))


def test_recent_action_history_is_capped(store: StateStore) -> None:
    for index in range(12):
        store.append_recent_action(f"action {index}")

    assert store.read_recent_actions() == [f"action {index}" for index in range(2, 12)]
