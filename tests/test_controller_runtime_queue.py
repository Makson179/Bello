"""Controller runtime queue regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from supervisor.appserver import AppServerMessage
from supervisor.schemas import ChangedFile, CheapRuntimeDecision, HumanMessage, BelloStatus, SupervisorDecision, SupervisorDecisionKind, TriggeringAction, ValidationRun
from supervisor.state import PROGRESS, RUNTIME_TRACE, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError

from tests.support.controller import (
    _CheapRuntimeNoopReviewer,
    _runtime_controller,
)


async def test_runtime_intervention_cancels_queued_completion_review(tmp_path: Path) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

    coder = FakeCoder()
    controller.coder = coder

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="concrete runtime correction",
            message_to_coder="Correct the runtime issue before declaring readiness.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (validation_regression): pytest regressed",
        None,
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []
    assert coder.messages == ["Correct the runtime issue before declaring readiness."]
    assert controller._supervisor_next_completion_summary is None


async def test_pause_closes_completion_review_session(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.pause()

    assert controller.paused is True
    assert fake.closed_completion_reviews == 1
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_runtime_no_message_retry_keeps_runtime_and_completion_in_separate_slots(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    recovered = await controller._handle_supervisor_no_message_failure(
        message="supervisor did not produce an agent message",
        summary="Runtime trigger (timeout): command timed out",
        completion_review=False,
    )

    assert recovered is True
    assert "Retry supervisor review" in (controller._supervisor_next_runtime_summary or "")
    assert controller._supervisor_next_completion_summary is not None
    assert controller._supervisor_next_completion_review is False
    assert controller._supervisor_next_summary == controller._supervisor_next_runtime_summary


async def test_repeated_runtime_timeout_blocks_stale_queued_completion(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class AlwaysTimeoutSupervisor:
        def __init__(self, state_store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, state_store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            raise SupervisorAgentError("runtime supervisor timed out")

    supervisor = AlwaysTimeoutSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (timeout): command execution timed out",
        "cmd-timeout",
        TriggeringAction(
            item_id="cmd-timeout",
            kind="commandExecution",
            command="pytest",
            status="failed",
            timed_out=True,
            summary="command execution timed out",
        ),
        None,
        None,
        False,
    )

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert controller._supervisor_next_completion_summary is None
    assert "refusing to run a stale completion review" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_queued_human_runtime_wake_preserves_full_context_and_bypasses_cheap(
    tmp_path: Path,
) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    human = HumanMessage(text="Discussion only; do not change files.", sequence=9)
    action = TriggeringAction(
        item_id="cmd-9",
        kind="commandExecution",
        command="pwd",
        exit_code=0,
        status="completed",
        summary="command completed",
    )
    controller._queue_supervisor_check(
        "Human message received",
        triggering_item_id="message-9",
        triggering_action=action,
        human_message=human,
        patch_summary="queued patch context",
        completion_review=False,
    )
    controller._queue_supervisor_check(
        "Coder turn completed",
        completion_review=False,
    )

    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): initial event",
        None,
        None,
        None,
        None,
        False,
    )

    assert len(cheap.calls) == 1
    assert len(fake.runtime_packets) == 1
    packet = fake.runtime_packets[0]
    assert packet.current_summary == "Human message received"
    assert packet.human_message == human
    assert packet.triggering_item_id == "message-9"
    assert packet.triggering_action == action
    assert packet.patch_summary == "queued patch context"


async def test_stale_runtime_decision_does_not_ack_trigger_signature(tmp_path: Path) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)
    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed",
        ),
        validation=None,
        changed_files=changed_files,
    )
    signature = controller._runtime_pending_trigger_signatures()["large_diff"][0]

    async def stale_decision(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            reason="stale generation",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation + 1,
        )

    fake.decide = stale_decision
    await controller._run_supervisor_check(
        "Runtime trigger (large_diff): file change completed",
        None,
        None,
        None,
        None,
        False,
    )

    assert decision.reasons == ("large_diff",)
    assert controller._last_large_diff_signature is None
    assert controller._runtime_pending_trigger_signatures()["large_diff"][0] == signature
    assert controller._supervisor_next_runtime_summary is not None


async def test_runtime_wake_arriving_during_completion_defers_completion_decision(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    original_completion = fake.decide_completion
    action = TriggeringAction(
        item_id="cmd-regression",
        kind="commandExecution",
        command="pytest",
        exit_code=1,
        status="failed",
        summary="pytest regressed",
    )

    async def completion_with_concurrent_runtime(packet):
        controller._queue_supervisor_check(
            "Runtime trigger (validation_regression): pytest regressed",
            triggering_item_id="cmd-regression",
            triggering_action=action,
            completion_review=False,
        )
        return await original_completion(packet)

    fake.decide_completion = completion_with_concurrent_runtime
    await controller._run_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        "message-ready",
        None,
        None,
        None,
        True,
    )

    assert store.get_bello_config().last_applied_supervisor_sequence == 0
    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_runtime_check.triggering_action == action
    assert controller._supervisor_next_completion_summary is not None
    assert controller._supervisor_next_completion_check.completion_review is True
    assert fake.closed_completion_reviews == 1


async def test_coalesced_runtime_reasons_retain_each_trigger_action_for_luna(
    tmp_path: Path,
) -> None:
    from supervisor.approval_triage import cheap_runtime_packet

    controller, _store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    regression = TriggeringAction(
        item_id="pytest-1",
        kind="commandExecution",
        command="pytest tests/test_parser.py",
        exit_code=1,
        status="failed",
        summary="parser regression",
    )
    suspicious_edit = TriggeringAction(
        item_id="edit-2",
        kind="fileChange",
        paths=["tests/fixtures/parser.json"],
        status="completed",
        summary="fixture changed",
    )
    controller.validations = [
        ValidationRun(
            validation_id="parser-tests",
            command="pytest tests/test_parser.py",
            normalized_command="pytest tests/test_parser.py",
            exit_code=1,
            shell_exit_code=1,
            passed=False,
            trusted_validation_outcome="failed",
            summary="parser test failed",
            sequence=1,
        )
    ]
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression): parser regression",
        triggering_action=regression,
    )
    controller._queue_supervisor_check(
        "Runtime trigger (suspicious_file_touched): fixture changed",
        triggering_action=suspicious_edit,
        completion_review=False,
    )

    await controller._run_supervisor_check(
        "Runtime trigger (validation_regression): parser regression",
        "pytest-1",
        regression,
        None,
        None,
        False,
    )

    assert fake.runtime_packets == []
    assert len(cheap.calls) == 1
    packet = cheap.calls[0]
    assert set(packet.current_summary.split("(", 1)[1].split(")", 1)[0].split(", ")) == {
        "validation_regression",
        "suspicious_file_touched",
    }
    assert {action.item_id for action in packet.runtime_triggering_actions} == {
        "pytest-1",
        "edit-2",
    }
    slim = cheap_runtime_packet(packet)
    events = {event["action"]["item_id"]: event for event in slim["triggering_events"]}
    assert events["pytest-1"]["validation"]["validation_id"] == "parser-tests"
    assert events["edit-2"]["action"]["paths"] == ["tests/fixtures/parser.json"]
    assert controller._supervisor_next_runtime_summary is None


def test_ack_keeps_new_same_reason_trigger_when_other_queued_reason_is_covered(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)
    old_a = TriggeringAction(
        item_id="a-old",
        kind="commandExecution",
        command="pytest tests/test_a.py",
        exit_code=1,
        status="failed",
        summary="old A regression",
    )
    old_b = TriggeringAction(
        item_id="b-old",
        kind="commandExecution",
        command="pytest tests/test_b.py",
        exit_code=1,
        status="failed",
        summary="old B regression",
    )
    new_a = old_a.model_copy(update={"item_id": "a-new", "summary": "new A regression"})
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (validation_regression): old A regression",
        triggering_action=old_a,
    )
    controller._retain_runtime_trigger_summary(
        "Runtime trigger (repeated_same_failing_validation): old B regression",
        triggering_action=old_b,
    )
    old_batch = dict(controller._runtime_pending_trigger_signatures())
    controller._queue_supervisor_check(
        "Runtime trigger (validation_regression): new A regression",
        triggering_action=new_a,
        completion_review=False,
    )
    controller._queue_supervisor_check(
        "Runtime trigger (repeated_same_failing_validation): old B duplicate",
        triggering_action=old_b,
        completion_review=False,
    )

    controller._ack_runtime_trigger_batch(old_batch)

    assert list(controller._runtime_pending_trigger_signatures()) == ["validation_regression"]
    assert controller._runtime_pending_trigger_actions()["validation_regression"].item_id == "a-new"
    assert controller._supervisor_next_runtime_summary is not None
    assert "validation_regression" in controller._supervisor_next_runtime_summary
    assert controller._supervisor_next_runtime_check.triggering_action.item_id == "a-new"


async def test_failed_runtime_apply_requeues_trigger_and_cancels_stale_completion(
    tmp_path: Path,
) -> None:
    controller, _store, fake = _runtime_controller(tmp_path)

    class FailingCoder:
        async def steer_or_start(self, message: str) -> str:
            raise RuntimeError("steering failed")

    controller.coder = FailingCoder()
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="runtime correction",
            message_to_coder="Correct the regression.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    await controller._run_supervisor_check(
        "Runtime trigger (validation_regression): pytest regressed",
        "cmd-regression",
        None,
        None,
        None,
        False,
    )

    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_completion_summary is None
    assert "validation_regression" in controller._runtime_pending_trigger_signatures()
    assert controller._runtime_apply_retry_count == 1


async def test_failed_runtime_steer_retries_same_wake_sequence_then_commits(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class InitialEscalatingCheapReviewer(_CheapRuntimeNoopReviewer):
        async def review(self, packet):
            self.calls.append(packet)
            return CheapRuntimeDecision(
                decision="escalate",
                reason_code="needs_supervisor_judgment",
            )

    cheap = InitialEscalatingCheapReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)

    class FlakyCoder:
        def __init__(self) -> None:
            self.attempts = 0
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.attempts += 1
            self.messages.append(message)
            if self.attempts == 1:
                raise RuntimeError("transient steering failure")
            return "turn"

    coder = FlakyCoder()
    controller.coder = coder
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def intervene(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="runtime correction",
            message_to_coder="Correct the regression.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene
    await controller._supervisor_check_loop(
        "Runtime trigger (validation_regression): pytest regressed",
        "cmd-regression",
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 2
    assert [packet.wake_sequence for packet in fake.runtime_packets] == [1, 1]
    assert coder.attempts == 2
    assert coder.messages == ["Correct the regression.", "Correct the regression."]
    assert store.get_bello_config().last_applied_supervisor_sequence == 1
    assert controller._runtime_pending_trigger_signatures() == {}
    assert controller._supervisor_next_completion_summary is None
    assert controller._runtime_apply_retry_count == 0
    assert len(cheap.calls) == 1


async def test_repeated_stale_runtime_decision_fails_bounded_without_completion(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class InitialEscalatingCheapReviewer(_CheapRuntimeNoopReviewer):
        async def review(self, packet):
            self.calls.append(packet)
            return CheapRuntimeDecision(
                decision="escalate",
                reason_code="needs_supervisor_judgment",
            )

    cheap = InitialEscalatingCheapReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def stale(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            reason="stale generation",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation + 1,
        )

    fake.decide = stale
    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): file change completed",
        "file-1",
        None,
        None,
        None,
        False,
    )

    assert len(fake.runtime_packets) == 2
    assert len(cheap.calls) == 1
    assert controller._runtime_decision_retry_count == 1
    assert controller._supervisor_next_completion_summary is None
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


async def test_runtime_pause_discards_queued_reviews_and_stops_queue_loop(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    async def pause(packet):
        fake.runtime_packets.append(packet)
        return SupervisorDecision(
            decision=SupervisorDecisionKind.PAUSE,
            reason="human-only input required",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = pause
    await controller._supervisor_check_loop(
        "Human message to supervisor: wait for credentials",
        None,
        None,
        HumanMessage(text="wait for credentials", sequence=1),
        None,
        False,
    )

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []
    assert controller.paused is True
    assert controller._supervisor_next_runtime_summary is None
    assert controller._supervisor_next_completion_summary is None
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_external_pause_discards_inflight_runtime_decision(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.interrupted = False

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

        async def interrupt(self) -> None:
            self.interrupted = True

    coder = FakeCoder()
    controller.coder = coder

    async def intervene_after_external_pause(packet):
        fake.runtime_packets.append(packet)
        await controller.pause()
        return SupervisorDecision(
            decision=SupervisorDecisionKind.INTERVENE,
            reason="late pre-pause correction",
            message_to_coder="This must not be delivered after pause.",
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    fake.decide = intervene_after_external_pause
    await controller._supervisor_check_loop(
        "Human message to supervisor: inspect current state",
        None,
        None,
        HumanMessage(text="inspect current state", sequence=1),
        None,
        False,
    )

    assert coder.interrupted is True
    assert coder.messages == []
    assert store.get_bello_config().last_applied_supervisor_sequence == 0
    assert store.get_bello_config().status == BelloStatus.PAUSED


async def test_shell_command_shape_does_not_create_masked_validation_wake(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
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
                        "command": "pytest tests/test_app.py | cat",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "tests/test_app.py::test_app PASSED\n1 passed in 0.01s\n",
                    },
                },
            }
        )
    )
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    assert controller.validations[0].trusted_validation_outcome == "passed"
    assert controller.validations[0].masking_reason is None
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "masked_validation" not in trace["trigger_reasons"]


async def test_test_runner_failure_output_is_failed_without_masked_gate(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-failed",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_app.py | cat",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "tests/test_app.py::test_app FAILED\n1 failed in 0.01s\n",
                    },
                },
            }
        )
    )

    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    validation = controller.validations[0]
    assert validation.outcome == "fail"
    assert validation.passed is False
    assert validation.trusted_validation_outcome == "failed"
    assert validation.masking_reason is None
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["trigger_reasons"] == []


async def test_repeated_same_failing_validation_uses_command_identity(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    item = {
        "type": "commandExecution",
        "command": "pytest tests/test_app.py",
        "exitCode": 1,
        "status": "completed",
        "stdout": "tests/test_app.py::test_app FAILED\n1 failed in 0.01s\n",
    }

    await controller.handle_notification(
        AppServerMessage({"method": "item/completed", "params": {"threadId": "thread", "itemId": "cmd-1", "item": item}})
    )
    assert controller._supervisor_task is None
    await controller.handle_notification(
        AppServerMessage({"method": "item/completed", "params": {"threadId": "thread", "itemId": "cmd-2", "item": item}})
    )
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert controller.validations[0].validation_id == controller.validations[1].validation_id
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert "repeated_same_failing_validation" in trace["trigger_reasons"]
