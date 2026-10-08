"""Controller runtime triggers regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from supervisor.controller import _runtime_restart_issue, _validation_freshness_summary
from supervisor.appserver import AppServerMessage
from supervisor.schemas import ChangedFile, RestartHandoff, SupervisorDecision, SupervisorDecisionKind, SupervisorWakePacket, TriggeringAction, ValidationRun
from supervisor.state import PROGRESS, RUNTIME_METRICS, RUNTIME_TRACE

from tests.support.controller import (
    _CheapRuntimeNoopReviewer,
    _runtime_controller,
    _runtime_failure_validation,
    _runtime_unresolved_validation,
    _runtime_validation_packet,
)


def test_runtime_supervisor_schema_rejects_complete() -> None:
    with pytest.raises(Exception):
        SupervisorDecision.model_validate({"decision": "complete"})


def test_validation_freshness_summary_marks_stale_behavioral_pass() -> None:
    summary = _validation_freshness_summary(
        validations=[
            ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=5),
        ],
        changed_files=[ChangedFile(path="app.py", status="modified", sequence=8)],
    )

    assert "behavioral validation is stale" in summary


async def test_runtime_noop_action_skips_supervisor_and_records_trace(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "pwd",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": str(tmp_path) + "\n",
                    },
                },
            }
        )
    )

    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["skipped_noop"] is True
    assert trace["should_wake_runtime_supervisor"] is False
    metrics = json.loads(store.path(RUNTIME_METRICS).read_text(encoding="utf-8"))
    assert metrics["runtime_skipped_noop_total"] == 1


async def test_first_isolated_nonzero_action_is_deterministic_noop(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 -c 'raise SystemExit(1)'",
                        "exitCode": 1,
                        "status": "completed",
                    },
                },
            }
        )
    )
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["should_wake_runtime_supervisor"] is False
    assert trace["trigger_reasons"] == []


def test_sole_nonzero_is_noop_even_with_an_older_unresolved_failure(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller.validation_runtime_state = {
        "older-validation": {
            "trusted_validation_outcome": "failed",
            "consecutive_failed_count": 2,
            "sequence": 2,
        }
    }

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python3 -c 'raise SystemExit(1)'",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[],
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


async def test_runtime_restart_budget_wakes_supervisor(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 -c 'print(1)'",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert "restart candidate because restart cap reached" in fake.runtime_packets[0].current_summary
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "restart_budget" in trace["trigger_reasons"]
    assert trace["restart_reason"] == "restart cap reached"


def test_restart_budget_wakes_once_per_health_state(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    duplicate = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    store.patch_health(
        lambda health: health.model_copy(
            update={"restart_count": 0, "risk_signals": ["bypass_after_denial"]}
        )
    )
    changed = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )

    assert first.reasons == ("restart_budget",)
    assert first.restart_reason == "restart cap reached"
    assert duplicate.should_wake is False
    assert changed.reasons == ("restart_budget",)
    assert changed.restart_reason == "bypass/rephrase attempt after denial"


def test_restart_budget_recurrence_after_clearing_is_new_state(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    first_batch = dict(controller._runtime_pending_trigger_signatures())

    store.patch_health(lambda health: health.model_copy(update={"restart_count": 0}))
    controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    controller._ack_runtime_trigger_batch(first_batch)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))
    recurring = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )

    assert first.reasons == ("restart_budget",)
    assert controller._last_restart_budget_signature is None
    assert recurring.reasons == ("restart_budget",)


def test_runtime_restart_issue_distinguishes_failures_and_ignores_legacy_masking() -> None:
    first = _runtime_failure_validation(sequence=1, output="AssertionError: expected 1, got 2")
    different = _runtime_failure_validation(sequence=2, output="ValueError: malformed header")

    first_issue = _runtime_restart_issue(_runtime_validation_packet(first, wake_sequence=1))
    different_issue = _runtime_restart_issue(_runtime_validation_packet(different, wake_sequence=2))

    assert first_issue is not None
    assert different_issue is not None
    assert first_issue.key != different_issue.key

    masked_a = _runtime_failure_validation(
        sequence=3,
        output="pipeline exit was masked",
        validation_id="validation-a",
        trusted_outcome="masked_or_unknown",
        masking_reason="shell_pipeline_masks_failure",
        command="bash strict-a.sh | tail",
    )
    masked_b = _runtime_failure_validation(
        sequence=4,
        output="different command masked the same way",
        validation_id="validation-b",
        trusted_outcome="masked_or_unknown",
        masking_reason="shell_pipeline_masks_failure",
        command="bash strict-b.sh | head",
    )

    masked_a_issue = _runtime_restart_issue(
        _runtime_validation_packet(masked_a, wake_sequence=3, reason="masked_validation")
    )
    masked_b_issue = _runtime_restart_issue(
        _runtime_validation_packet(masked_b, wake_sequence=4, reason="masked_validation")
    )

    assert masked_a_issue is None
    assert masked_b_issue is None


def test_runtime_restart_issue_groups_nested_shells_for_same_unresolved_command() -> None:
    direct = _runtime_unresolved_validation(
        sequence=1,
        command="/bin/bash -lc ./compile.sh",
        validation_id="validation-direct",
    )
    nested = _runtime_unresolved_validation(
        sequence=2,
        command="/bin/bash -c '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    different = _runtime_unresolved_validation(
        sequence=3,
        command="/bin/bash -lc ./test.sh",
        validation_id="validation-different",
    )

    direct_issue = _runtime_restart_issue(_runtime_validation_packet(direct, wake_sequence=1))
    nested_issue = _runtime_restart_issue(_runtime_validation_packet(nested, wake_sequence=2))
    different_issue = _runtime_restart_issue(
        _runtime_validation_packet(different, wake_sequence=3)
    )

    assert direct_issue is not None
    assert nested_issue is not None
    assert different_issue is not None
    assert direct_issue.key == nested_issue.key
    assert direct_issue.key != different_issue.key


def test_runtime_restart_issue_carries_active_failure_across_turn_completion() -> None:
    first = _runtime_unresolved_validation(
        sequence=10,
        command="/bin/bash -lc ./compile.sh",
        validation_id="validation-direct",
    )
    repeated = _runtime_unresolved_validation(
        sequence=12,
        command="/bin/bash -lc '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    active = _runtime_restart_issue(_runtime_validation_packet(first, wake_sequence=11))
    assert active is not None
    packet = SupervisorWakePacket(
        wake_sequence=13,
        latest_event_sequence=13,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary="Coder turn completed",
        coder_thread_id="thread",
        validations=[first, repeated],
    )

    carried = _runtime_restart_issue(
        packet,
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )
    stale = _runtime_restart_issue(
        packet,
        active_issue_key=active.key,
        active_issue_last_sequence=repeated.sequence,
    )
    different = _runtime_unresolved_validation(
        sequence=14,
        command="/bin/bash -lc ./test.sh",
        validation_id="validation-different",
    )
    superseded = _runtime_restart_issue(
        packet.model_copy(update={"validations": [first, repeated, different]}),
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )
    unrelated_wake = _runtime_restart_issue(
        packet.model_copy(
            update={"current_summary": "Runtime integrity trigger: runtime links restored."}
        ),
        active_issue_key=active.key,
        active_issue_last_sequence=first.sequence,
    )

    assert carried is not None
    assert carried.key == active.key
    assert carried.sequence == repeated.sequence
    assert stale is None
    assert superseded is None
    assert unrelated_wake is None


def test_runtime_event_issue_ignores_optional_file_change_action_metadata() -> None:
    base = SupervisorWakePacket(
        wake_sequence=20,
        latest_event_sequence=21,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary="Runtime trigger (large_diff): file change completed: 1 changes",
        coder_thread_id="thread",
        changed_files=[ChangedFile(path="src/parser.py", status="M", sequence=19)],
    )
    with_action = base.model_copy(
        update={
            "wake_sequence": 22,
            "latest_event_sequence": 23,
            "triggering_action": TriggeringAction(
                kind="fileChange",
                paths=["/tmp/coder/workspace/src/parser.py"],
                status="completed",
                summary="file change completed: 1 changes",
            ),
        }
    )
    different_path = with_action.model_copy(
        update={"changed_files": [ChangedFile(path="src/lexer.py", status="M", sequence=24)]}
    )

    base_issue = _runtime_restart_issue(base)
    action_issue = _runtime_restart_issue(with_action)
    different_issue = _runtime_restart_issue(different_path)

    assert base_issue is not None
    assert action_issue is not None
    assert different_issue is not None
    assert base_issue.key == action_issue.key
    assert base_issue.key != different_issue.key


async def test_runtime_restart_gate_counts_rejected_restart_as_steering_and_ignores_progress_update(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.steers: list[str] = []

        async def steer_or_start(self, message: str) -> None:
            self.steers.append(message)

    coder = FakeCoder()
    controller.coder = coder
    restarts: list[tuple[str, RestartHandoff | None]] = []

    async def capture_restart(reason: str, *, handoff: RestartHandoff | None = None) -> None:
        restarts.append((reason, handoff))

    controller.restart = capture_restart  # type: ignore[method-assign]
    handoff = RestartHandoff(
        objective="finish task",
        restart_reason="same failure repeated after steering",
        bad_pattern="rerunning the same failing validation",
        known_evidence="the same assertion failed repeatedly",
        next_step="inspect the assertion before editing",
        recovery_signal="the validation failure changes or passes",
    )

    for sequence in (1, 2, 3):
        event_sequence = sequence * 2 - 1
        wake_sequence = event_sequence + 1
        store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"last_event_sequence": event_sequence})
        )
        validation = _runtime_failure_validation(
            sequence=event_sequence,
            output="AssertionError: expected 1, got 2",
        )
        packet = _runtime_validation_packet(validation, wake_sequence=wake_sequence)
        if sequence == 1:
            decision = SupervisorDecision(
                decision=SupervisorDecisionKind.INTERVENE,
                reason="the same failure needs a controlled diagnostic",
                message_to_coder="Inspect the failing assertion before another edit.",
                progress_update="Recorded the first steering for this validation failure.",
                wake_sequence=wake_sequence,
                generation=0,
            )
        else:
            decision = SupervisorDecision(
                decision=SupervisorDecisionKind.RESTART,
                reason="coder repeated the same failure after steering",
                progress_update="Restart requested for the repeated validation failure.",
                handoff=handoff,
                wake_sequence=wake_sequence,
                generation=0,
            )
        await controller.apply_supervisor_decision(
            decision,
            packet_thread_id="thread",
            packet=packet,
        )

    assert len(coder.steers) == 2
    assert coder.steers[0] == "Inspect the failing assertion before another edit."
    assert "rerunning the same failing validation" in coder.steers[1]
    assert len(restarts) == 1
    assert restarts[0][0] == "coder repeated the same failure after steering"
    health = store.get_health()
    assert health.restart_issue_interventions == 2
    assert health.last_progress_sequence == 1
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert progress.count("Restart requested for the repeated validation failure.") == 1


def test_trusted_pass_clears_only_matching_runtime_restart_issue(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    failed = _runtime_failure_validation(sequence=1, output="AssertionError: expected 1, got 2")
    issue = _runtime_restart_issue(_runtime_validation_packet(failed, wake_sequence=1))
    assert issue is not None
    controller._record_runtime_intervention(
        reason="first steering",
        message="inspect the failure",
        sequence=1,
        generation=0,
        issue=issue,
    )

    unrelated_pass = _runtime_failure_validation(
        sequence=2,
        output="1 passed",
        validation_id="validation-other",
        trusted_outcome="passed",
    )
    controller._record_validation_runtime_state(unrelated_pass)
    assert store.get_health().restart_issue_key == issue.key

    matching_pass = unrelated_pass.model_copy(
        update={"validation_id": failed.validation_id, "sequence": 3}
    )
    controller._record_validation_runtime_state(matching_pass)
    assert store.get_health().restart_issue_key is None


def test_trusted_pass_clears_unresolved_issue_through_equivalent_shell_wrapper(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    unresolved = _runtime_unresolved_validation(
        sequence=1,
        command="/bin/bash -lc '/bin/bash -lc ./compile.sh'",
        validation_id="validation-nested",
    )
    issue = _runtime_restart_issue(_runtime_validation_packet(unresolved, wake_sequence=1))
    assert issue is not None
    controller._record_runtime_intervention(
        reason="build did not execute",
        message="run the build once through the normal approval path",
        sequence=1,
        generation=0,
        issue=issue,
    )

    passed = unresolved.model_copy(
        update={
            "validation_id": "validation-direct",
            "command": "/bin/bash -lc ./compile.sh",
            "normalized_command": "/bin/bash -lc ./compile.sh",
            "exit_code": 0,
            "shell_exit_code": 0,
            "outcome": "pass",
            "passed": True,
            "trusted_validation_outcome": "passed",
            "summary": "command completed: /bin/bash -lc ./compile.sh exit=0",
            "sequence": 2,
        }
    )
    controller._record_validation_runtime_state(passed)

    assert store.get_health().restart_issue_key is None


@pytest.mark.parametrize(
    "reason",
    [
        "validation_regression",
        "repeated_same_failing_validation",
        "timeout",
        "suspicious_file_touched",
        "unknown_signal",
    ],
)
async def test_quality_runtime_wake_can_be_filtered_by_cheap_runtime(tmp_path: Path, reason: str) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    await controller._run_supervisor_check(
        f"Runtime trigger ({reason}): command completed: sed -n '1,120p' app.test.js exit=0",
        triggering_item_id="cmd-1",
        triggering_action=TriggeringAction(
            kind="commandExecution",
            command="sed -n '1,120p' app.test.js",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        human_message=None,
        patch_summary=None,
        completion_review=False,
    )

    assert len(cheap.calls) == 1
    assert cheap.calls[0].current_summary.startswith(f"Runtime trigger ({reason})")
    assert fake.runtime_packets == []


def test_cheap_runtime_switch_reads_persisted_runtime_config(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    assert controller._cheap_runtime_enabled() is True

    store.update_bello_config(lambda cfg: cfg.model_copy(update={"cheap_runtime": False}))

    assert controller._cheap_runtime_enabled() is False


@pytest.mark.parametrize(
    "summary",
    [
        "Runtime trigger (restart_budget): restart candidate because restart cap reached; command completed",
        "Runtime trigger (runtime_apply_retry): retry decision after apply failure",
        "Runtime trigger (runtime_control_replacement): coder workspace runtime links were restored",
        "Runtime trigger (runtime_decision_retry): refresh stale runtime decision",
        "Runtime integrity trigger: coder workspace runtime links were replaced and restored.",
    ],
)
async def test_mandatory_runtime_wake_bypasses_cheap_runtime_noop(tmp_path: Path, summary: str) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    await controller._run_supervisor_check(
        summary,
        triggering_item_id="message-1",
        triggering_action=None,
        human_message=None,
        patch_summary=None,
        completion_review=False,
    )

    assert cheap.calls == []
    assert len(fake.runtime_packets) == 1


def test_read_only_large_diff_trigger_is_suppressed_but_real_diff_change_wakes(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    read_only_action = TriggeringAction(
        kind="commandExecution",
        command="sed -n '1,20p' src/app.py",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    execution_action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    changed_files = [ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)]

    read_only = controller.should_wake_runtime_supervisor(
        action=read_only_action,
        validation=None,
        changed_files=changed_files,
    )
    first_execution = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=changed_files,
    )
    repeated_execution = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=changed_files,
    )
    changed_signature = controller.should_wake_runtime_supervisor(
        action=execution_action,
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=601, deletions=0, sequence=2)],
    )

    assert read_only.should_wake is False
    assert read_only.reasons == ()
    assert first_execution.should_wake is True
    assert first_execution.reasons == ("large_diff",)
    assert repeated_execution.should_wake is False
    assert repeated_execution.reasons == ()
    assert changed_signature.should_wake is True
    assert changed_signature.reasons == ("large_diff",)


def test_suspicious_file_trigger_wakes_once_per_file_state(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    test_path = tmp_path / "tests" / "test_parser.py"
    test_path.parent.mkdir()
    test_path.write_text("assert parse('a') == 1\n", encoding="utf-8")
    action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    changed_files = [
        ChangedFile(path="tests/test_parser.py", status="M", additions=1, deletions=1, sequence=2)
    ]

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    unchanged = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    test_path.write_text("assert parse('b') == 2\n", encoding="utf-8")
    edited_again = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    cleaned = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=[],
    )
    changed_after_clean = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )

    assert first.reasons == ("suspicious_file_touched",)
    assert unchanged.should_wake is False
    assert edited_again.reasons == ("suspicious_file_touched",)
    assert cleaned.should_wake is False
    assert changed_after_clean.reasons == ("suspicious_file_touched",)


def test_unchanged_suspicious_file_does_not_defeat_isolated_nonzero_noop(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    test_path = tmp_path / "tests" / "test_parser.py"
    test_path.parent.mkdir()
    test_path.write_text("assert parse('a') == 1\n", encoding="utf-8")
    changed_files = [ChangedFile(path="tests/test_parser.py", status="M", additions=1, deletions=0)]
    successful_action = TriggeringAction(
        kind="commandExecution",
        command="python3 -c 'print(1)'",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    failing_action = successful_action.model_copy(update={"exit_code": 1})

    controller.should_wake_runtime_supervisor(
        action=successful_action,
        validation=None,
        changed_files=changed_files,
    )
    decision = controller.should_wake_runtime_supervisor(
        action=failing_action,
        validation=None,
        changed_files=changed_files,
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


def test_file_change_large_diff_wakes_runtime_triage_once(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed: src/app.py",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_project_execution_large_diff_wakes_runtime_supervisor(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="/bin/bash -lc 'make -j4'",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_timeout_trigger_requires_explicit_structured_signal(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    textual = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python -c 'subprocess.run(cmd, timeout=30)'",
            exit_code=0,
            status="completed",
            summary="command mentions timeout but completed",
        ),
        validation=None,
        changed_files=[],
    )
    explicit = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="python worker.py",
            exit_code=None,
            status="failed",
            timed_out=True,
            summary="command failed",
        ),
        validation=None,
        changed_files=[],
    )

    assert textual.should_wake is False
    assert textual.reasons == ()
    assert explicit.should_wake is True
    assert explicit.reasons == ("timeout",)


def test_project_execution_first_nonzero_is_deterministic_noop(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    action = TriggeringAction(
        kind="commandExecution",
        command="pytest tests/public/test_public.py",
        exit_code=1,
        status="completed",
        summary="command completed",
    )

    decision = controller.should_wake_runtime_supervisor(
        action=action,
        validation=ValidationRun(
            command=action.command or "",
            exit_code=1,
            type="behavioral",
            passed=False,
            summary="1 failed",
            trusted_validation_outcome="failed",
            sequence=3,
        ),
        changed_files=[],
    )

    assert decision.should_wake is False
    assert decision.reasons == ()


def test_protected_runtime_reason_stays_visible_for_project_execution(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="pytest tests/public/test_public.py",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[],
        validation_trigger_reasons=("repeated_same_failing_validation",),
    )

    assert decision.should_wake is True
    assert decision.reasons == ("repeated_same_failing_validation", "nonzero_exit")


def test_unresolved_masked_validation_still_wakes_for_project_execution(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller.validation_runtime_state = {
        "validation-old": {
            "trusted_validation_outcome": "masked_or_unknown",
            "consecutive_failed_count": 0,
            "sequence": 2,
        }
    }

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="./run_visible_tests.sh",
            exit_code=0,
            status="completed",
            summary="command completed",
        ),
        validation=ValidationRun(
            command="./run_visible_tests.sh",
            exit_code=0,
            type="behavioral",
            passed=True,
            summary="45 passed",
            trusted_validation_outcome="passed",
            sequence=4,
        ),
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=3)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("large_diff",)


def test_read_only_action_still_wakes_for_restart_budget(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    store.patch_health(lambda health: health.model_copy(update={"restart_count": 100}))

    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="commandExecution",
            command="rg -n \"TODO\" src",
            exit_code=1,
            status="completed",
            summary="command completed",
        ),
        validation=None,
        changed_files=[ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)],
    )

    assert decision.should_wake is True
    assert decision.reasons == ("restart_budget",)
    assert decision.restart_reason == "restart cap reached"


def test_pending_large_diff_trigger_survives_coalesced_turn_boundary(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    action = TriggeringAction(
        kind="fileChange",
        paths=["src/app.py"],
        status="completed",
        summary="file change completed",
    )

    first = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    duplicate_while_queued = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )
    pending_batch = dict(controller._runtime_pending_trigger_signatures())
    prepared = controller._prepare_runtime_trigger_summary(
        "Coder turn completed",
        pending=pending_batch,
    )
    controller._ack_runtime_trigger_batch(pending_batch)
    after_review_started = controller.should_wake_runtime_supervisor(
        action=action,
        validation=None,
        changed_files=changed_files,
    )

    assert first.reasons == ("large_diff",)
    assert duplicate_while_queued.should_wake is False
    assert prepared.startswith("Runtime trigger (large_diff):")
    assert "Coder turn completed" in prepared
    assert after_review_started.should_wake is False


def test_validation_regression_trigger_survives_coalesced_turn_boundary(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression, nonzero_exit): pytest exited 1"
    )
    pending_batch = dict(controller._runtime_pending_trigger_signatures())
    prepared = controller._prepare_runtime_trigger_summary(
        "Coder turn completed",
        pending=pending_batch,
    )

    assert prepared.startswith("Runtime trigger (validation_regression, nonzero_exit):")
    assert "Coder turn completed" in prepared
