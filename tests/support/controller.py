"""Shared synthetic controller sessions, packets, and platform fixtures."""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
import supervisor.controller as controller_module
import supervisor.policy as policy_module
from supervisor.controller import BelloController
from supervisor.schemas import AdvReportControllerDecision, ChangedFile, CheapRuntimeDecision, CoderMessage, CompletionReviewDecision, BelloConfig, SupervisorDecision, SupervisorDecisionKind, SupervisorWakePacket, TriggeringAction, ValidationRun
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent
from supervisor.workspace_snapshot import create_workspace_snapshot


@pytest.fixture
def posix_command_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the legacy POSIX command corpus explicit on native Windows."""

    monkeypatch.setattr(controller_module, "native_shell_kind", lambda: "posix")
    monkeypatch.setattr(policy_module, "native_shell_kind", lambda: "posix")


def _runtime_failure_validation(
    *,
    sequence: int,
    output: str,
    validation_id: str = "validation-repeat",
    trusted_outcome: str = "failed",
    masking_reason: str | None = None,
    command: str = "pytest tests/test_parser.py",
) -> ValidationRun:
    passed = trusted_outcome == "passed"
    return ValidationRun(
        validation_id=validation_id,
        command=command,
        normalized_command=command,
        exit_code=0 if passed else 1,
        shell_exit_code=0 if passed else 1,
        outcome="pass" if passed else "fail",
        passed=passed,
        trusted_validation_outcome=trusted_outcome,
        masking_reason=masking_reason,
        summary=output,
        captured_output=output,
        sequence=sequence,
        executed_test_names=["tests/test_parser.py::test_parse"],
        executed_test_files=["tests/test_parser.py"],
        failed_count=0 if passed else 1,
    )


def _runtime_validation_packet(
    validation: ValidationRun,
    *,
    wake_sequence: int,
    reason: str = "repeated_same_failing_validation",
) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=wake_sequence,
        latest_event_sequence=wake_sequence,
        generation=0,
        restart_count=0,
        task_path="TASK.md",
        task_contents="# Task",
        current_summary=f"Runtime trigger ({reason}): validation requires review",
        coder_thread_id="thread",
        triggering_action=TriggeringAction(
            kind="commandExecution",
            command=validation.command,
            exit_code=validation.exit_code,
            status="completed",
            summary=validation.summary,
        ),
        validations=[validation],
    )


def _runtime_unresolved_validation(
    *,
    sequence: int,
    command: str,
    validation_id: str,
) -> ValidationRun:
    return ValidationRun(
        validation_id=validation_id,
        command=command,
        normalized_command=command,
        exit_code=None,
        shell_exit_code=None,
        outcome="fail",
        passed=False,
        trusted_validation_outcome="failed",
        summary=f"command completed: {command} exit=None",
        sequence=sequence,
    )


def _prepare_done_without_fresh_validation(controller: BelloController) -> None:
    controller.last_coder_message = CoderMessage(text="Summary\nBELLO_READY_FOR_REVIEW", sequence=3)
    controller.observed_changed_files = {
        "src/app.py": ChangedFile(path="src/app.py", status="modified", sequence=2)
    }
    controller.validations = [
        ValidationRun(
            command="node --check src/app.js",
            exit_code=0,
            type="static",
            passed=True,
            summary="ok",
            sequence=3,
        )
    ]


async def _async_noop() -> None:
    return None


def _mock_codex_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "_controller_executable",
        lambda name, cwd, **kwargs: name,
    )
    monkeypatch.setattr(
        controller_module,
        "_run_probe",
        lambda args: (True, "codex-cli test"),
    )


async def _async_schema_hash() -> str:
    return "schema"


class _GateFakeCoder:
    def __init__(self) -> None:
        self.messages = []

    async def steer_or_start(self, message):
        self.messages.append(message)
        return "turn"


class _FakeAdvReportController:
    def __init__(
        self,
        decisions: list[AdvReportControllerDecision] | None = None,
    ) -> None:
        self.decisions = list(decisions or [])
        self.packets: list[SupervisorWakePacket] = []

    async def decide_adv_report(
        self,
        packet: SupervisorWakePacket,
    ) -> AdvReportControllerDecision:
        self.packets.append(packet)
        if self.decisions:
            return self.decisions.pop(0)
        return AdvReportControllerDecision(
            forward_to_coder=False,
            reason="no findings or observations remained",
            report_to_coder=None,
        )


def _completion_gate_controller(
    tmp_path: Path,
    *,
    validations: list[ValidationRun],
) -> tuple[BelloController, StateStore, Path, _GateFakeCoder]:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    coder = _GateFakeCoder()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = None
    controller.adv_report_controller = _FakeAdvReportController()
    controller.coder = coder
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = validations
    controller.inspections = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.paused = False
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_task = None
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0
    controller.provider_failure_recovery_counts = {}
    controller.validation_runtime_state = {}
    controller.completion_review_return_sequence = None
    controller._terminal_cleanup_started = False
    controller._command_output_chunks = {}
    controller._last_large_diff_signature = None
    controller._last_restart_budget_signature = None
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    return controller, store, task, coder


class _RuntimeFakeSupervisor:
    def __init__(self, store: StateStore, task: Path) -> None:
        self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
        self.runtime_packets = []
        self.completion_packets = []
        self.completion_thread_id = None
        self.closed_completion_reviews = 0
        self.runtime_decision_kind = SupervisorDecisionKind.NOOP
        self.before_runtime_decision = None
        self.runtime_thread_id = None
        self.on_thread_start = None

    def build_packet(self, **kwargs):
        return self.agent.build_packet(**kwargs)

    async def decide(self, packet):
        self.runtime_packets.append(packet)
        if self.runtime_thread_id is not None and self.on_thread_start is not None:
            self.on_thread_start(self.runtime_thread_id)
        if self.before_runtime_decision is not None:
            pending = self.before_runtime_decision()
            if pending is not None:
                await pending
        return SupervisorDecision(
            decision=self.runtime_decision_kind,
            reason="observed",
            message_to_coder=(
                "Run a task-relevant behavioral validation."
                if self.runtime_decision_kind == SupervisorDecisionKind.INTERVENE
                else None
            ),
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    async def decide_completion(self, packet):
        self.completion_packets.append(packet)
        return CompletionReviewDecision(
            decision="return",
            reason="not used",
            uncovered_behaviors=[],
            validation_gaps=["fake completion gap"],
            claim_evidence_mismatches=[],
            packet_or_access_limitations=[],
            changed_test_risks=[],
            message_to_coder="not used",
            persistent_decision=None,
            progress_update=None,
            clear_handoff=False,
            display_message=None,
            handoff=None,
            wake_sequence=packet.wake_sequence,
            generation=packet.generation,
        )

    async def close_completion_review(self):
        self.closed_completion_reviews += 1
        self.completion_thread_id = None
        return None


class _CheapRuntimeNoopReviewer:
    model = "cheap-runtime-test"

    def __init__(self) -> None:
        self.calls = []

    async def review(self, packet):
        self.calls.append(packet)
        return CheapRuntimeDecision(decision="noop", reason_code="routine_progress")


def _runtime_controller(tmp_path: Path) -> tuple[BelloController, StateStore, _RuntimeFakeSupervisor]:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    fake = _RuntimeFakeSupervisor(store, task)
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = fake
    controller.adv_report_controller = _FakeAdvReportController()
    controller.coder = None
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.inspections = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.paused = False
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_task = None
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._current_turn_action_count = 0
    controller._last_completion_marker_sequence = None
    controller.no_marker_idle_nudge_count = 0
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.validation_runtime_state = {}
    controller.provider_failure_recovery_counts = {}
    controller.completion_review_return_sequence = None
    controller._terminal_cleanup_started = False
    controller._command_output_chunks = {}
    controller._last_large_diff_signature = None
    controller._last_restart_budget_signature = None
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    fake.on_thread_start = controller._register_reviewer_thread
    return controller, store, fake


def _runtime_controller_with_plan(
    tmp_path: Path,
    plan_text: str,
):
    controller, store, fake = _runtime_controller(tmp_path)
    plan = tmp_path / "PLAN.md"
    plan.write_text(plan_text, encoding="utf-8")
    snapshot = create_workspace_snapshot(
        tmp_path,
        controller.task_path,
        plan_path=plan,
    )
    controller.plan_path = plan.resolve()
    controller._coder_snapshot = snapshot
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.declared_grading_roots = ()
    return controller, store, fake, snapshot, plan


def _covered_accept_decision(*, wake_sequence: int, validation_id: str = "validation-3") -> CompletionReviewDecision:
    return CompletionReviewDecision.model_validate(
        {
            "decision": "accept",
            "reason": "covered",
            "files_reviewed": [
                {"path": "src/app.py", "reason": "changed source", "kind": "source", "inspected": True, "limitation": None},
                {"path": "tests/test_app.py", "reason": "changed test", "kind": "test", "inspected": True, "limitation": None},
            ],
            "behavior_evidence_matrix": [
                {
                    "behavior": "requested behavior",
                    "task_basis": "TASK.md",
                    "files_considered": ["src/app.py", "tests/test_app.py"],
                    "evidence": [
                        {
                            "validation_id": validation_id,
                            "command": "pytest tests/test_app.py",
                            "sequence": 3,
                            "validation_type": "behavioral",
                            "outcome": "pass",
                            "freshness": "fresh",
                            "why_it_covers_behavior": "executes the changed behavior",
                        }
                    ],
                    "status": "covered",
                    "gap": None,
                }
            ],
            "uncovered_behaviors": [],
            "validation_gaps": [],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": None,
            "persistent_decision": None,
            "progress_update": "Accepted by completion review.",
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": wake_sequence,
            "generation": 0,
        }
    )


def _gate_packet(
    task: Path,
    *,
    validations: list[ValidationRun],
    wake_sequence: int = 1,
    latest_change: int | None = 2,
) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=wake_sequence,
        latest_event_sequence=wake_sequence,
        generation=0,
        restart_count=0,
        task_path=str(task),
        task_contents=task.read_text(encoding="utf-8"),
        coder_thread_id="thread",
        changed_files=[
            ChangedFile(path="src/app.py", status="M", sequence=2),
            ChangedFile(path="tests/test_app.py", status="M", sequence=2),
        ],
        validations=validations,
        latest_relevant_change_sequence=latest_change,
    )


class _FakeTUI:
    def __init__(self) -> None:
        self.messages = []
        self.input_queue = asyncio.Queue()

    def render(self, title, message):
        self.messages.append((title, message))

    def status(self, message):
        self.messages.append(("STATUS", message))

    async def start(self):
        self.messages.append(("START", ""))

    async def stop(self):
        self.messages.append(("STOP", ""))


class _FakeSteerCoder:
    def __init__(self) -> None:
        self.steers: list[str] = []

    async def steer_or_start(self, message: str) -> None:
        self.steers.append(message)
