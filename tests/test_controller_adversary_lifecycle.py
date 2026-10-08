"""Controller adversary lifecycle regression tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from supervisor.controller import BelloController
from supervisor.adversary_agent import AdversaryAgentError
from supervisor.project_config import MODEL_GPT_5_5
from supervisor.schemas import AdvReportControllerDecision, AdversaryReport, ChangedFile, CompletionReviewDecision, BelloConfig, BelloStatus, ValidationRun
from supervisor.state import EVENTS, FINAL_REPORT, LOG, PROGRESS, StateStore

from tests.support.controller import (
    _FakeAdvReportController,
    _RuntimeFakeSupervisor,
    _completion_gate_controller,
    _covered_accept_decision,
    _gate_packet,
    _runtime_controller,
)


async def test_adversary_remaining_limit_runs_before_completion_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = None
    controller.client = object()
    controller.model = None
    controller.running = False
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None
    (tmp_path / ".supervisor" / "secret.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".supervisor" / "secret.txt").write_text("runtime history", encoding="utf-8")
    seen_snapshot_roots: list[Path] = []

    class FakeAdversary:
        def __init__(self, client, project_root, *, on_thread_start=None, on_thread_done=None, **kwargs) -> None:
            self.project_root = Path(project_root)
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            seen_snapshot_roots.append(self.project_root)
            assert self.project_root != tmp_path
            assert (self.project_root / "TASK.md").exists()
            assert not (self.project_root / ".supervisor").exists()
            (self.project_root / "adversary_probe.txt").write_text("probe", encoding="utf-8")
            assert previous_adversary_report is None
            if self.on_thread_start:
                self.on_thread_start("adv-thread")
            if self.on_thread_done:
                self.on_thread_done("adv-thread")
            return SimpleNamespace(
                report_text=(
                    "attacked: boundary inputs\n"
                    "findings: none\n"
                    "held: boundary inputs held\n"
                    "not_reached: none\n"
                    "overall: held"
                ),
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller._pending_adversary_report is not None
    assert controller._pending_adversary_report.thread_id == "adv-thread"
    assert controller._pending_adversary_report.candidate_finding is False
    assert controller._pending_adversary_report.latest_relevant_change_sequence == 2
    assert controller._pending_adversary_report.workspace_state_id is not None
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == []
    assert seen_snapshot_roots and not seen_snapshot_roots[0].exists()
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversarial tester completed" in progress
    assert "adv_report_controller found no findings or observations" in progress


async def test_adversary_run_limit_skips_additional_run_and_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 1, "adversary_run_count": 1})
    )

    class UnexpectedAdversary:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("adversary should not run after limit is reached")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", UnexpectedAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Skipping adversarial tester before complete: adversary run limit reached (1/1)" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "adversary/limit_reached" for event in events)


async def test_adversary_infra_failure_completes_with_recorded_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An adversary that cannot run is a tester-availability problem, not evidence against
    # the accepted work: the run must finalize the accept with the gap recorded loudly,
    # not die as infrastructure-invalid.
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.running = False
    controller._pending_adversary_report = None
    controller._active_adversary_thread_id = None
    controller._active_adversary_workspace_root = None

    class FailingAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            raise AdversaryAgentError("adversary did not produce an agent message")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FailingAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert coder.messages == []
    accepted = controller._accepted_adversary_report
    assert accepted is not None
    assert accepted.status == "error"
    assert "did not produce an agent message" in accepted.report_text
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversarial tester could not run" in progress
    assert "adversary coverage recorded as missing" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "adversary/unavailable" for event in events)
    assert any(event["event_type"] == "completion/accept" for event in events)
    final_report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "status=error" in final_report
    assert "provider_failure" not in final_report.lower()


async def test_adversary_fresh_report_allows_completion_finalize(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    packet = _gate_packet(task, validations=validations)
    packet.adversary_report = AdversaryReport(
        report_text="attacked: boundary\nfindings: none\noverall: held",
        thread_id="adv-thread",
        turn_id="adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=2,
        validation_sequence=3,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=packet,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert coder.messages == []
    final_report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "## Adversary Reports" in final_report


async def test_adversary_report_controller_routes_schema_valid_normalized_report_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    fake = _RuntimeFakeSupervisor(store, task)
    controller.supervisor = fake
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.coder_model = MODEL_GPT_5_5
    controller.supervisor_model = MODEL_GPT_5_5
    controller.adversary_model = "gpt-adversary"
    controller.adversary_intelligence = "ultra"
    controller.running = True
    controller.observed_changed_files = {"src/app.py": ChangedFile(path="src/app.py", status="M", sequence=2)}
    normalized = _FakeAdvReportController(
        [
            AdvReportControllerDecision(
                forward_to_coder=True,
                reason="kept one reworded finding",
                report_to_coder=(
                    "## Findings requiring correction\n"
                    "- invoking with seven positional arguments crashes"
                ),
            )
        ]
    )
    controller.adv_report_controller = normalized

    class FakeAdversary:
        def __init__(
            self,
            *args,
            model=None,
            intelligence=None,
            on_thread_start=None,
            on_thread_done=None,
            **kwargs,
        ) -> None:
            assert model == "gpt-adversary"
            assert intelligence == "ultra"
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            assert previous_adversary_report is None
            if self.on_thread_start:
                self.on_thread_start("adv-thread")
            if self.on_thread_done:
                self.on_thread_done("adv-thread")
            return SimpleNamespace(
                report_text="attacked: stack args\nfindings: crash on seven args\noverall: broke",
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=True,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert fake.completion_packets == []
    assert len(normalized.packets) == 1
    assert normalized.packets[0].adversary_report is not None
    assert normalized.packets[0].adversary_report.candidate_finding is True
    assert store.get_bello_config().adversary_run_count == 1
    assert coder.messages == [
        "Finding: a confirmed defect that requires correction.\n"
        "Observation: a concern that is not yet confirmed; investigate it and fix it only if confirmed.\n\n"
        "## Findings requiring correction\n"
        "- invoking with seven positional arguments crashes"
    ]
    assert "attacked:" not in coder.messages[0]
    assert "overall:" not in coder.messages[0]
    assert controller.completion_returns[0].source == "adversary_report_controller"
    assert store.get_bello_config().completion_return_count == 0
    coder_readable_log = store.path(LOG).read_text(encoding="utf-8")
    assert "attacked: stack args" not in coder_readable_log
    assert "overall: broke" not in coder_readable_log


@pytest.mark.parametrize(
    "raw_report",
    [
        pytest.param(
            "candidate_finding: false\n"
            "attacked: cache behavior\n"
            "findings: none\n"
            "observations:\n"
            "- cache count changed without the expected header\n"
            "held: ordinary cache path\n"
            "overall: I believe no defects remain in the submitted solution",
            id="declared-no-findings",
        ),
        pytest.param(
            "candidate_finding: false\n\n## observations\n"
            "Cache count changed without the expected header.",
            id="markdown-without-required-sections",
        ),
        pytest.param(
            "No defects found. Cache count changed without the expected header.",
            id="heading-free-report-without-routing-line",
        ),
    ],
)
async def test_nonempty_adversary_reports_reach_report_controller_without_format_retry(
    tmp_path: Path,
    raw_report: str,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(
        tmp_path,
        validations=validations,
    )
    controller.adversary_enabled = True
    controller.running = True
    normalized = _FakeAdvReportController(
        [
            AdvReportControllerDecision(
                forward_to_coder=True,
                reason="carried one observation",
                report_to_coder=(
                    "## Observations requiring investigation\n"
                    "- cache count changed without the expected header"
                ),
            )
        ]
    )
    controller.adv_report_controller = normalized

    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0
            self.turn_count = 0
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": f"adv-thread-{self.thread_count}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_count += 1
            return {
                "turn": {
                    "id": f"adv-turn-{self.turn_count}",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": raw_report}],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    controller.client = client

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert client.thread_count == 1
    assert client.turn_count == 1
    assert client.archived == ["adv-thread-1"]
    assert len(normalized.packets) == 1
    assert normalized.packets[0].adversary_report is not None
    assert normalized.packets[0].adversary_report.report_text == raw_report
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert len(coder.messages) == 1
    assert "## Observations requiring investigation" in coder.messages[0]
    assert "cache count changed without the expected header" in coder.messages[0]
    assert "attacked:" not in coder.messages[0]


async def test_completion_return_budget_waits_for_coder_readiness_before_forcing_adversary(
    tmp_path: Path,
) -> None:
    controller, store, _, coder = _completion_gate_controller(tmp_path, validations=[])
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 1,
                "max_completion_returns_after_adversary": 2,
            }
        )
    )
    decision = CompletionReviewDecision(
        decision="return",
        reason="one material gap remains",
        validation_gaps=["edge case is not validated"],
        message_to_coder="Fix and validate the edge case, then report readiness again.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller._return_completion_to_coder(decision)

    cfg = store.get_bello_config()
    assert cfg.completion_return_count == 1
    assert cfg.completion_returns_since_adversary == 0
    assert cfg.adversary_run_count == 0
    assert coder.messages == ["Fix and validate the edge case, then report readiness again."]
    assert controller._completion_review_budget_action() == "adversary"


async def test_completion_only_review_budget_finalizes_without_restart_or_extra_review(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 4,
                "completion_return_count": 4,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    await controller._run_supervisor_check("coder ready after final allowed review", None, None, None, None, True)

    assert fake.completion_packets == []
    assert controller.completion_restarts == 0
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]


def test_completion_only_zero_review_budget_skips_review(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_completion_returns_before_adversary": 0,
                "completion_return_count": 100,
            }
        )
    )

    assert controller._completion_review_budget_action() == "complete"


def test_completion_only_unlimited_review_budget_has_no_cap(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_completion_returns_before_adversary": "unlimited",
                "completion_return_count": 100,
            }
        )
    )

    assert controller._completion_review_budget_action() is None


def test_zero_pre_adversary_review_budget_starts_adversary(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 0,
                "completion_return_count": 0,
            }
        )
    )

    assert controller._completion_review_budget_action() == "adversary"


def test_zero_post_adversary_review_budget_completes(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_after_adversary": 0,
                "adversary_run_count": 1,
                "completion_returns_since_adversary": 0,
            }
        )
    )

    assert controller._completion_review_budget_action() == "complete"


def test_zero_post_adversary_budget_has_no_legacy_completion_adjudication(tmp_path: Path) -> None:
    controller, store, task, _ = _completion_gate_controller(tmp_path, validations=[])
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_after_adversary": 0,
                "adversary_run_count": 1,
                "completion_returns_since_adversary": 0,
            }
        )
    )
    packet = _gate_packet(task, validations=[])
    packet.adversary_report = AdversaryReport(
        candidate_finding=True,
        report_text="attacked: edge\nfindings: candidate defect\noverall: broke",
        generation=packet.generation,
        completion_wake_sequence=packet.wake_sequence,
        latest_relevant_change_sequence=packet.latest_relevant_change_sequence,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    assert controller._completion_review_budget_action(packet=packet) == "complete"


async def test_pre_adversary_return_budget_runs_adversary_without_an_extra_completion_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.adversary_model = "gpt-adversary"
    controller.adversary_intelligence = "ultra"
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 7,
                "max_completion_returns_after_adversary": 2,
                "completion_return_count": 7,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    class CleanAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            return SimpleNamespace(
                report_text="attacked: boundaries\nfindings: none\noverall: held",
                thread_id="adv-thread",
                turn_id="adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", CleanAdversary)

    await controller._run_supervisor_check("coder ready", None, None, None, None, True)

    cfg = store.get_bello_config()
    assert fake.completion_packets == []
    assert cfg.adversary_run_count == 1
    assert cfg.completion_returns_since_adversary == 0
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]


async def test_post_adversary_return_budget_finalizes_on_next_readiness_without_extra_review(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 7,
                "max_completion_returns_after_adversary": 2,
                "adversary_run_count": 1,
                "completion_return_count": 9,
                "completion_returns_since_adversary": 2,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool | None]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    await controller._run_supervisor_check("coder ready after final return", None, None, None, None, True)

    assert fake.completion_packets == []
    assert finalized == [
        (
            "completed normally",
            BelloStatus.COMPLETE,
            None,
        )
    ]
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event_type"] == "completion/budget_finalize"
    assert events[-1]["decision"]["completion_return_count"] == 9
    assert events[-1]["reason"] == "post-adversary completion review budget exhausted"


async def test_required_budget_adversary_failure_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.client = object()
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "max_adversary_runs": 1,
                "max_completion_returns_before_adversary": 1,
                "completion_return_count": 1,
            }
        )
    )
    finalized: list[tuple[str, BelloStatus, bool]] = []

    async def capture_finalize(
        result: str,
        *,
        status: BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))

    controller.finalize = capture_finalize

    class FailingAdversary:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            raise AdversaryAgentError("provider unavailable")

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FailingAdversary)

    await controller._run_supervisor_check("coder ready", None, None, None, None, True)

    assert fake.completion_packets == []
    assert finalized == [
        (
            "required adversary failed under bounded review policy: provider unavailable",
            BelloStatus.PROVIDER_FAILURE,
            False,
        )
    ]
    assert controller._pending_adversary_report.status == "error"


async def test_adversary_receives_previous_report_as_regression_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    controller.adversary_enabled = True
    controller.client = object()
    controller.model = None
    controller.running = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 2, "adversary_run_count": 1})
    )
    controller._pending_adversary_report = AdversaryReport(
        candidate_finding=True,
        report_text="attacked: previous edge\nfindings: previous crash\noverall: broke",
        thread_id="old-adv-thread",
        turn_id="old-adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=1,
        validation_sequence=2,
        workspace_state_id="old-state",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    class FakeAdversary:
        def __init__(self, *args, on_thread_start=None, on_thread_done=None, **kwargs) -> None:
            self.on_thread_start = on_thread_start
            self.on_thread_done = on_thread_done

        async def run(self, packet, *, previous_adversary_report=None):
            assert previous_adversary_report is not None
            assert previous_adversary_report["report_text"].startswith("attacked: previous edge")
            if self.on_thread_start:
                self.on_thread_start("new-adv-thread")
            if self.on_thread_done:
                self.on_thread_done("new-adv-thread")
            return SimpleNamespace(
                report_text="attacked: previous edge, fresh edge\nfindings: none\noverall: held",
                thread_id="new-adv-thread",
                turn_id="new-adv-turn",
                candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", FakeAdversary)

    await controller.apply_completion_decision(
        _covered_accept_decision(wake_sequence=1, validation_id="validation-3"),
        packet_thread_id="thread",
        packet=_gate_packet(task, validations=validations),
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert controller._accepted_adversary_report.thread_id == "new-adv-thread"
    assert store.get_bello_config().adversary_run_count == 2
    assert coder.messages == []


async def test_completion_return_never_appends_raw_adversary_report(tmp_path: Path) -> None:
    validations = [
        ValidationRun(
            command="pytest tests/test_app.py",
            exit_code=0,
            passed=True,
            summary="1 passed",
            captured_output="1 passed\n",
            executed_test_files=["tests/test_app.py"],
            sequence=3,
        )
    ]
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=validations)
    packet = _gate_packet(task, validations=validations)
    packet.adversary_report = AdversaryReport(
        report_text="attacked: stack args\nfindings: crash on seven args\nraw observed output: SIGSEGV\noverall: broke",
        thread_id="adv-thread",
        turn_id="adv-turn",
        generation=0,
        completion_wake_sequence=1,
        latest_relevant_change_sequence=2,
        validation_sequence=3,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    decision = CompletionReviewDecision(
        decision="return",
        reason="adversary reproduced stack arg crash",
        uncovered_behaviors=["stack-passed arguments crash"],
        validation_gaps=[],
        claim_evidence_mismatches=[],
        packet_or_access_limitations=[],
        changed_test_risks=[],
        message_to_coder="Fix the reproduced stack-argument crash.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller.apply_completion_decision(decision, packet_thread_id="thread", packet=packet)

    assert len(controller.completion_returns) == 1
    assert coder.messages == ["Fix the reproduced stack-argument crash."]
    assert "Adversarial tester report:" not in coder.messages[0]
    assert "SIGSEGV" not in coder.messages[0]


def test_effective_max_adversary_runs_cli_override(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    # No overrides: falls back to the persisted config (default 1).
    controller.adversary_enabled = None
    assert controller._effective_max_adversary_runs() == 1

    # CLI --adversary true --adversary-runs 3: budget honored without touching persisted config.
    controller.adversary_enabled = True
    controller.adversary_runs = 3
    assert controller._effective_max_adversary_runs() == 3
    assert store.get_bello_config().max_adversary_runs == 1

    # CLI --adversary false wins regardless of budget.
    controller.adversary_enabled = False
    assert controller._effective_max_adversary_runs() == 0
