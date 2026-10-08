"""Controller completion lifecycle regression tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
from supervisor.approvals import ApprovalManager
from supervisor.controller import BelloController, _hash_file
from supervisor.schemas import ChangedFile, ChangedFileDiff, CompletionReviewDecision, RestartHandoff, BelloConfig, BelloStatus, SupervisorDecision, SupervisorDecisionKind, ValidationRun
from supervisor.state import FINAL_REPORT, HANDOFF, LOG, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent

from tests.support.controller import (
    _FakeTUI,
    _completion_gate_controller,
    _covered_accept_decision,
    _gate_packet,
)


async def test_completion_return_sends_message_and_continues_same_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0

    class _CloseTrackingSupervisor:
        def __init__(self) -> None:
            self.closed = 0

        async def close_completion_review(self) -> None:
            self.closed += 1

    runtime_supervisor = _CloseTrackingSupervisor()
    completion_supervisor = _CloseTrackingSupervisor()
    controller.supervisor = runtime_supervisor
    controller.completion_supervisor = completion_supervisor

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="return",
            reason="fallback behavior is uncovered",
            uncovered_behaviors=["missing-key fallback"],
            validation_gaps=["only happy path was validated"],
            message_to_coder="Validate missing-key fallback before marking ready again.",
            persistent_decision="Completion review requires fallback coverage.",
            progress_update="Completion review returned missing fallback coverage.",
            clear_handoff=False,
            display_message=None,
            handoff=None,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    assert store.get_bello_config().generation == 0
    assert controller.coder.messages == ["Validate missing-key fallback before marking ready again."]
    assert len(controller.completion_returns) == 1
    assert store.get_health().interventions == 0
    assert "Completion review returned missing fallback coverage" in store.path("PROGRESS.md").read_text(encoding="utf-8")
    # Fresh completion-review thread per review: a normal return closes the session so the
    # next readiness review starts a new thread instead of accumulating prior turns.
    assert runtime_supervisor.closed == 0
    assert completion_supervisor.closed == 1


async def test_completion_accept_finalizes_without_deterministic_gate(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    decision = CompletionReviewDecision(
        decision="accept",
        reason="fresh validation passed",
        message_to_coder=None,
        persistent_decision=None,
        progress_update="Accepted by completion review.",
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller.apply_completion_decision(
        decision,
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert len(controller.completion_returns) == 0
    assert coder.messages == []
    assert "completion_accept_gate" not in store.path(LOG).read_text(encoding="utf-8")


async def test_completion_accept_still_checks_task_integrity_without_snapshot(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller._canonical_task_hash = _hash_file(task)
    packet = _gate_packet(task, validations=validations)
    task.write_text("# Changed task", encoding="utf-8")

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.ESCALATED
    assert controller.completion_returns == []
    assert coder.messages == []
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted workspace failed task integrity validation" in report
    assert "the original task file changed after the run started" in report


async def test_completion_accept_does_not_require_independent_changed_test_evidence(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app_new.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app_new.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app_new.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_files = [
        ChangedFile(path="src/app.py", status="M", sequence=2),
        ChangedFile(path="tests/test_app_new.py", status="A", sequence=2),
    ]
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app_new.py",
            file_kind="test",
            change_kind="added",
            diff="+def test_requested_behavior():\n+    assert app() == 'requested'",
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert len(controller.completion_returns) == 0
    assert coder.messages == []


async def test_completion_accept_is_not_overridden_by_changed_test_masking_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app.py",
            file_kind="test",
            change_kind="modified",
            diff=(
                "diff --git a/tests/test_app.py b/tests/test_app.py\n"
                "@@\n"
                "-    assert app() == 'requested'\n"
                "+    assert True\n"
            ),
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_accept_is_not_overridden_by_skipped_test_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="tests/test_app.py::test_requested_behavior PASSED\n1 passed",
            captured_output="tests/test_app.py::test_requested_behavior PASSED\n1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.changed_file_diffs = [
        ChangedFileDiff(
            path="tests/test_app.py",
            file_kind="test",
            change_kind="modified",
            diff="+test.skip('requested behavior', () => expect(app()).toBe('requested'))",
        )
    ]

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_accept_is_not_overridden_by_behavioral_validation_heuristic(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="python -m py_compile src/app.py",
            exit_code=0,
            type="static",
            passed=True,
            summary="compiled",
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller.completion_returns == []
    assert coder.messages == []


async def test_completion_return_is_not_blocked_by_controller_freshness_gate(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            validation_id="validation-new",
            command="BELLO_BEHAVIOR_DEMO=1 ./bin/app --scenario fixed",
            exit_code=0,
            type="behavior_demo",
            passed=True,
            trusted_validation_outcome="passed",
            summary="fixed=1",
            captured_output="fixed=1\n",
            sequence=12,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations, wake_sequence=20, latest_change=11)
    packet.latest_event_sequence = 20
    decision = CompletionReviewDecision.model_validate(
        {
            "decision": "return",
            "reason": "old gap still open",
            "decision_artifact": {
                "current_state": "old state",
                "resolved_concerns": [],
                "stale_concerns": ["old gap"],
                "uncovered_edge_candidates": [],
                "actionable_gap_or_none": "old gap",
            },
            "files_reviewed": [
                {"path": "src/app.py", "reason": "changed source", "kind": "source", "inspected": True, "limitation": None}
            ],
            "behavior_evidence_matrix": [
                {
                    "behavior": "requested behavior",
                    "task_basis": "TASK.md",
                    "files_considered": ["src/app.py"],
                    "evidence": [],
                    "status": "partial",
                    "gap": "old gap",
                }
            ],
            "uncovered_behaviors": ["requested behavior"],
            "validation_gaps": [],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "fix old gap",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 20,
            "generation": 0,
        }
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert store.get_bello_config().status == BelloStatus.STARTING
    assert len(controller.completion_returns) == 1
    assert coder.messages == ["fix old gap"]
    assert "completion_decision_staleness_failure" not in store.path(LOG).read_text(encoding="utf-8")


async def test_completion_restart_writes_handoff_and_starts_new_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.started_turns = []

        async def respond(self, request_id, response):
            return None

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "new-thread"}}

        async def turn_start(self, params, *, timeout):
            self.started_turns.append(params)
            return {"turn": {"id": "new-turn"}}

    handoff = RestartHandoff(
        objective="task",
        restart_reason="repeated completion miss",
        bad_pattern="validated only happy path",
        known_evidence="fallback unvalidated",
        next_step="read task",
        recovery_signal="fallback validated",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.model = None
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = None
    controller.pending_approvals = {}
    controller.prior_interventions = []
    controller.validations = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = [
        {
            "reason": "fallback missing",
            "uncovered_behaviors": ["fallback"],
            "validation_gaps": [],
            "message_to_coder": "cover fallback",
            "sequence": 1,
            "generation": 0,
        }
    ]
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="restart",
            reason="non-converging completion returns",
            uncovered_behaviors=["fallback"],
            validation_gaps=["same stale validation"],
            message_to_coder=None,
            persistent_decision=None,
            progress_update="Restarting from completion review.",
            clear_handoff=False,
            display_message=None,
            handoff=handoff,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    assert store.get_bello_config().generation == 1
    assert "repeated completion miss" in store.path(HANDOFF).read_text(encoding="utf-8")
    assert controller.completion_restarts == 1
    assert controller.client.started_turns


async def test_supervisor_decision_can_clear_handoff(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    store.write_handoff("restart context\n")

    controller = BelloController.__new__(BelloController)
    controller.store = store

    await controller.apply_supervisor_decision(
        SupervisorDecision(
            decision=SupervisorDecisionKind.NOOP,
            clear_handoff=True,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id=None,
    )

    assert store.path(HANDOFF).read_text(encoding="utf-8") == ""


def test_structured_handoff_is_read_back_verbatim(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    handoff = RestartHandoff(
        objective="task",
        restart_reason="loop",
        bad_pattern="repeat",
        known_evidence="evidence",
        next_step="step",
        recovery_signal="signal",
    )
    store.write_handoff(handoff.model_dump_json(indent=2) + "\n")

    packet = StatelessSupervisorAgent(None, store, task).build_packet(  # type: ignore[arg-type]
        wake_sequence=1,
        current_summary="progress check",
    )

    assert packet.handoff == handoff


async def test_completion_return_with_delta_evidence_goes_to_coder(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            validation_id="validation-old",
            command="pytest tests/public",
            exit_code=0,
            passed=True,
            summary="old public pass",
            sequence=5,
        ),
        ValidationRun(
            validation_id="validation-demo",
            command="BELLO_BEHAVIOR_DEMO=1 ./c_compiler sample.c",
            exit_code=0,
            type="behavior_demo",
            passed=True,
            trusted_validation_outcome="passed",
            summary="returns 42",
            captured_output="program exit=42\n",
            sequence=15,
        ),
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations, wake_sequence=20)
    packet.completion_payload_mode = "delta"
    packet.completion_payload_since_sequence = 10
    decision = CompletionReviewDecision.model_validate(
        {
            "decision": "return",
            "reason": "old gap still lacks proof",
            "files_reviewed": [],
            "behavior_evidence_matrix": [],
            "uncovered_behaviors": [],
            "validation_gaps": ["needs direct behavior evidence"],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "provide direct behavior evidence",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 20,
            "generation": 0,
        }
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert coder.messages == ["provide direct behavior evidence"]
    assert len(controller.completion_returns) == 1
    assert "completion_return_freshness_failure" not in store.path(LOG).read_text(encoding="utf-8")
