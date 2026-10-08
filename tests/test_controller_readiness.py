"""Controller readiness regression tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from supervisor.approvals import ApprovalManager
from supervisor.controller import NO_MARKER_IDLE_NUDGE, POST_RESTART_CONTINUE_NUDGE, BelloController, _has_malformed_readiness_marker, _has_readiness_marker
from supervisor.appserver import AppServerMessage
from supervisor.schemas import AppEventSource, CoderMessage, CompletionReviewDecision, RestartHandoff, BelloConfig, BelloStatus, SupervisorDecisionKind, SupervisorWakePacket, ValidationRun
from supervisor.state import CONFIG, EVENTS, FINAL_REPORT, LOG, PROGRESS, RUNTIME_TRACE, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent

from tests.support.controller import (
    _CheapRuntimeNoopReviewer,
    _FakeSteerCoder,
    _FakeTUI,
    _prepare_done_without_fresh_validation,
    _runtime_controller,
)


def test_readiness_marker_detection_requires_own_exact_line() -> None:
    assert _has_readiness_marker("Summary\n  BELLO_READY_FOR_REVIEW  \n")
    assert not _has_readiness_marker("Summary BELLO_READY_FOR_REVIEW")
    assert not _has_readiness_marker("bello_ready_for_review")
    assert _has_malformed_readiness_marker("bello_ready_for_review")
    assert _has_malformed_readiness_marker("BELLO READY FOR REVIEW")
    assert not _has_malformed_readiness_marker("I am not emitting `BELLO_READY_FOR_REVIEW`.")


async def test_exact_marker_triggers_completion_review_accept(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    class CompletionSupervisor:
        def __init__(self) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.completion_packets = []

        def build_packet(self, **kwargs):
            packet = self.agent.build_packet(**kwargs)
            return packet

        async def decide(self, packet):
            raise AssertionError("runtime monitor should not handle exact marker")

        async def decide_completion(self, packet):
            self.completion_packets.append(packet)
            return CompletionReviewDecision(
                decision="accept",
                reason="fresh behavioral validation covers the task",
                files_reviewed=[
                    {"path": "TASK.md", "reason": "task contract", "kind": "other", "inspected": True, "limitation": None}
                ],
                behavior_evidence_matrix=[
                    {
                        "behavior": "task is complete",
                        "task_basis": "TASK.md",
                        "files_considered": ["TASK.md"],
                        "evidence": [
                            {
                                "validation_id": "validation-1",
                                "command": "pytest",
                                "sequence": 1,
                                "validation_type": "behavioral",
                                "outcome": "pass",
                                "freshness": "fresh",
                                "why_it_covers_behavior": "passes the submitted validation",
                            }
                        ],
                        "status": "covered",
                        "gap": None,
                    }
                ],
                uncovered_behaviors=[],
                validation_gaps=[],
                claim_evidence_mismatches=[],
                packet_or_access_limitations=[],
                changed_test_risks=[],
                message_to_coder=None,
                persistent_decision=None,
                progress_update="Completion review accepted final readiness.",
                clear_handoff=False,
                display_message=None,
                handoff=None,
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    fake = CompletionSupervisor()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.supervisor = fake
    controller.pending_approvals = {}
    controller.last_coder_message = CoderMessage(
        text="Summary: done\nValidation: pytest\nBELLO_READY_FOR_REVIEW",
        sequence=1,
    )
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=1)
    ]
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._supervisor_task = None
    controller._last_completion_marker_sequence = None
    controller.no_marker_idle_nudge_count = 0
    controller.completion_returns = []
    controller.completion_attempt_count = 0
    controller.completion_restarts = 0
    controller.paused = False

    await controller._handle_coder_turn_completed(item_id="message-item")
    await controller._supervisor_task

    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_coder_message.text.endswith("BELLO_READY_FOR_REVIEW")
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "accepted by completion_review" in report
    assert "- Completion review accepted: true" in report


async def test_summary_done_without_marker_steers_for_exact_marker_not_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"), overwrite=True)

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
    controller.supervisor = None
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.last_coder_message = CoderMessage(text="All tests pass. Done.", sequence=1)
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.adversary_enabled = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller._supervisor_dirty = False
    controller._supervisor_next_summary = None
    controller._supervisor_next_completion_review = False
    controller._supervisor_task = None
    controller.paused = False

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert controller.coder.messages == [NO_MARKER_IDLE_NUDGE]
    assert store.get_bello_config().status == BelloStatus.STARTING


@pytest.mark.parametrize(
    "phrase",
    [
        "material limitation",
        "validation limitation",
        "independent behavioral evidence is still missing",
        "independent behavioral evidence is missing",
        "independent evidence is still missing",
        "independent evidence is missing",
        "no untouched output-identified",
        "no compliant next validation step",
        "no compliant validation step",
        "cannot provide independent",
        "can't provide independent",
        "not ready under the independent-evidence requirement",
    ],
)
async def test_former_material_limitation_phrases_are_not_terminal(tmp_path: Path, phrase: str) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = False

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
    controller.last_coder_message = CoderMessage(
        text=f"I am not ready for review. Current constraint: {phrase}.",
        sequence=7,
    )
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={"active_coder_turn_id": None, "completion_review_enabled": False}
        )
    )

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert store.get_bello_config().status == BelloStatus.STARTING
    assert coder.messages == [NO_MARKER_IDLE_NUDGE]
    assert coder.interrupted is False
    assert "coder/material_limitation" not in store.path(EVENTS).read_text(encoding="utf-8")
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""


async def test_no_marker_idle_forces_completion_review_once(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None, "last_event_sequence": 17})
    )

    await controller._handle_no_marker_idle()
    await controller._supervisor_task

    assert len(fake.completion_packets) == 1
    assert controller.completion_returns[0].reason == "not used"
    assert "Controller forcing completion_review" in store.path(PROGRESS).read_text(encoding="utf-8")

    await controller._handle_no_marker_idle()

    assert len(fake.completion_packets) == 1


async def test_marker_with_completion_review_disabled_finalizes_without_review(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    controller.last_coder_message = CoderMessage(
        text="Summary: done\nValidation: pytest\nBELLO_READY_FOR_REVIEW",
        sequence=1,
    )
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="passed", sequence=1)
    ]

    await controller._handle_coder_turn_completed(item_id="message-item")

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert fake.completion_packets == []
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "completion review disabled by config" in report
    assert "- Completion review accepted: false" in report
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "completion review is disabled by config" in progress
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    assert any(event["event_type"] == "completion/review_disabled_finalize" for event in events)


async def test_completion_review_cli_override_beats_persisted_config(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    controller.completion_review = False
    assert controller._effective_completion_review() is False

    controller.completion_review = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    assert controller._effective_completion_review() is True

    controller.completion_review = None
    assert controller._effective_completion_review() is False


async def test_completion_review_disabled_preserves_independent_adversary(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller.adversary_runs = None
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"max_adversary_runs": 2, "completion_review_enabled": False})
    )

    assert controller._effective_max_adversary_runs() == 2
    assert controller._adversary_model_required_for_preflight() is True


async def test_no_marker_idle_nudges_coder_when_completion_review_disabled(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={"active_coder_turn_id": None, "last_event_sequence": 17, "completion_review_enabled": False}
        )
    )

    class FakeCoder:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def steer_or_start(self, message: str) -> str:
            self.messages.append(message)
            return "turn"

    controller.coder = FakeCoder()

    await controller._handle_no_marker_idle()

    assert fake.completion_packets == []
    assert controller.coder.messages == [NO_MARKER_IDLE_NUDGE]


@pytest.mark.parametrize("cheap_runtime", [False, True])
@pytest.mark.parametrize("completion_review", [False, True])
async def test_denied_native_turn_runtime_noop_continues_idle_policy(
    tmp_path: Path, cheap_runtime: bool, completion_review: bool,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={
            "active_coder_turn_id": "turn",
            "completion_review_enabled": completion_review,
        })
    )

    class NativeCoder:
        thread_id = "thread"
        active_turn_id = "turn"

        def __init__(self):
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            self.active_turn_id = self.active_turn_id or "resumed-turn"
            store.update_bello_config(
                lambda cfg: cfg.model_copy(update={"active_coder_turn_id": self.active_turn_id})
            )
            return self.active_turn_id

        def mark_turn_completed(self, turn_id):
            if self.active_turn_id == turn_id:
                self.active_turn_id = None
                store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))

    class NativeClient:
        def __init__(self):
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    controller.coder = NativeCoder()
    controller.client = NativeClient()
    controller.approvals = ApprovalManager(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    if cheap_runtime:
        controller.runtime_triage_reviewer = cheap
        controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    controller._current_turn_action_count = 1
    await controller.handle_server_request(AppServerMessage({
        "id": 56,
        "method": "item/fileChange/requestApproval",
        "params": {
            "threadId": "thread", "turnId": "turn",
            "grantRoot": str(store.path(CONFIG)),
            "availableDecisions": ["accept", "decline", "cancel"],
        },
    }))
    await controller.handle_notification(AppServerMessage({
        "method": "serverRequest/resolved", "params": {"requestId": 56},
    }))
    await controller.handle_notification(AppServerMessage({
        "method": "turn/completed",
        "params": {"threadId": "thread", "turn": {"id": "turn", "status": "interrupted"}},
    }))
    await controller._supervisor_task

    assert controller.client.responses == [(56, {"decision": "decline"})]
    assert controller.pending_approvals == {}
    assert len(cheap.calls) == int(cheap_runtime)
    assert len(fake.runtime_packets) == int(not cheap_runtime)
    assert len(fake.completion_packets) == int(completion_review)
    assert controller.coder.messages[-1] == ("not used" if completion_review else NO_MARKER_IDLE_NUDGE)
    assert controller.coder.active_turn_id == "resumed-turn"
    assert store.get_bello_config().active_coder_turn_id == "resumed-turn"
    assert store.get_bello_config().status != BelloStatus.COMPLETE


@pytest.mark.parametrize("changed_during_refresh", [False, True])
@pytest.mark.parametrize("blocker", [
    "missing_coder", "active_turn", "coder_active_turn", "active_subagent", "generation", "thread", "new_event",
    "pending_approval", "persisted_approval", "queued_runtime", "queued_completion",
    "deferred_completion", "paused", "paused_status", "stopped", "finalizing", "terminal_cleanup",
])
async def test_runtime_noop_idle_does_not_resume_changed_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str, changed_during_refresh: bool,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    controller.coder = SimpleNamespace(thread_id="thread", active_turn_id=None)
    packet = SupervisorWakePacket(
        wake_sequence=1, latest_event_sequence=0, generation=0, restart_count=0,
        coder_thread_id="thread", task_path=str(controller.task_path), task_contents="# Task",
        current_summary="Coder turn completed",
    )

    def change_state():
        config_changes = {
            "active_turn": {"active_coder_turn_id": "new-turn"},
            "generation": {"generation": 1},
            "thread": {"coder_thread_id": "new-thread"},
            "persisted_approval": {"pending_server_request_ids": [57]},
            "paused_status": {"status": BelloStatus.PAUSED},
        }
        if blocker in config_changes:
            store.update_bello_config(lambda cfg: cfg.model_copy(update=config_changes[blocker]))
        elif blocker == "missing_coder":
            controller.coder = None
        elif blocker == "coder_active_turn":
            controller.coder = SimpleNamespace(active_turn_id="new-turn")
        elif blocker == "active_subagent":
            monkeypatch.setattr(controller, "_active_coder_subagents", lambda: [object()])
        elif blocker == "new_event":
            controller._append_event(AppEventSource.APP_SERVER, "turn/started", thread_id="thread")
        elif blocker == "pending_approval":
            controller.pending_approvals[57] = object()
        elif blocker == "queued_runtime":
            controller._supervisor_next_runtime_summary = "new runtime evidence"
        elif blocker == "queued_completion":
            controller._supervisor_next_completion_summary = "ready for review"
        elif blocker == "deferred_completion":
            controller._deferred_completion_check = object()
        else:
            attribute, value = {
                "paused": ("paused", True), "stopped": ("running", False),
                "finalizing": ("_finalizing", True), "terminal_cleanup": ("_terminal_cleanup_started", True),
            }[blocker]
            setattr(controller, attribute, value)

    async def refresh():
        if changed_during_refresh:
            change_state()

    async def unexpected_idle(**kwargs):
        raise AssertionError("stale runtime noop must not restart or review the coder")

    monkeypatch.setattr(controller, "_refresh_coder_subagents", refresh)
    monkeypatch.setattr(controller, "_handle_no_marker_idle", unexpected_idle)
    if not changed_during_refresh:
        change_state()
    assert await controller._resume_idle_after_runtime_noop(packet) is False


async def test_done_without_fresh_validation_runtime_noop_resumes_completion(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    cheap = _CheapRuntimeNoopReviewer()
    controller.runtime_triage_reviewer = cheap
    controller.runtime_triage_config = SimpleNamespace(model=cheap.model)
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-1")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.runtime_packets[0].current_summary.startswith(
        "Runtime trigger (done_without_fresh_validation):"
    )
    assert cheap.calls == []
    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_readiness_marker_sequence == 3
    assert fake.completion_packets[0].wake_sequence > fake.runtime_packets[0].wake_sequence
    assert len(controller.completion_returns) == 1
    assert store.get_bello_config().last_relevant_edit_sequence == 2
    assert "completion/readiness_validation_waived" in store.path(EVENTS).read_text(encoding="utf-8")
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["trigger_reasons"] == ["done_without_fresh_validation"]
    assert trace["should_wake_runtime_supervisor"] is True
    assert trace["deterministic_action"] is None
    assert trace["skipped_noop"] is False


async def test_done_without_fresh_validation_runtime_noop_ignores_reviewer_notifications(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    reviewer_thread = "runtime-reviewer-thread"
    fake.runtime_thread_id = reviewer_thread

    async def append_reviewer_notifications() -> None:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "thread/started",
                    "params": {
                        "thread": {
                            "id": reviewer_thread,
                            "status": {"type": "active"},
                        }
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": reviewer_thread,
                        "turn": {"id": "runtime-reviewer-turn"},
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": reviewer_thread,
                        "turnId": "runtime-reviewer-turn",
                        "item": {
                            "id": "runtime-reviewer-message",
                            "type": "agentMessage",
                            "text": '{"decision":"noop"}',
                        },
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {"method": "account/rateLimits/updated", "params": {}}
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": reviewer_thread,
                        "turn": {"id": "runtime-reviewer-turn"},
                    },
                }
            )
        )

    fake.before_runtime_decision = append_reviewer_notifications

    await controller._handle_coder_turn_completed(item_id="done-reviewer-events")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert reviewer_thread in controller._reviewer_thread_ids
    assert store.get_bello_config().last_event_sequence > fake.runtime_packets[0].latest_event_sequence
    assert len(fake.completion_packets) == 1
    assert fake.completion_packets[0].last_readiness_marker_sequence == 3


@pytest.mark.parametrize(
    ("source", "event_type", "thread_id", "invalidates"),
    [
        (AppEventSource.APP_SERVER, "turn/started", "thread", True),
        (AppEventSource.APP_SERVER, "turn/started", "unknown-thread", True),
        (AppEventSource.APP_SERVER, "configWarning", None, True),
        (AppEventSource.USER, "user/input", None, True),
        (AppEventSource.SUPERVISOR, "controller/restart", None, True),
        (AppEventSource.APP_SERVER, "account/rateLimits/updated", None, False),
    ],
)
def test_readiness_snapshot_classifies_new_activity_fail_closed(
    tmp_path: Path,
    source: AppEventSource,
    event_type: str,
    thread_id: str | None,
    invalidates: bool,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    controller._append_event(source, event_type, thread_id=thread_id)

    assert (
        controller._readiness_snapshot_has_new_invalidating_event(
            packet,  # type: ignore[arg-type]
            cfg=store.get_bello_config(),
        )
        is invalidates
    )


async def test_readiness_snapshot_rejects_coder_descendant_activity(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "coder-child",
                        "parentThreadId": "thread",
                        "status": {"type": "active"},
                    }
                },
            }
        )
    )

    assert controller._is_coder_descendant("coder-child")
    assert controller._readiness_snapshot_has_new_invalidating_event(
        packet,  # type: ignore[arg-type]
        cfg=store.get_bello_config(),
    )


def test_readiness_snapshot_rejects_bounded_journal_coverage_gap(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)
    controller._readiness_event_journal_limit = 2
    reviewer_thread = "runtime-reviewer-thread"
    controller._register_reviewer_thread(reviewer_thread)
    packet = SimpleNamespace(
        latest_event_sequence=store.get_bello_config().last_event_sequence
    )

    for event_type in ("turn/started", "item/completed", "turn/completed"):
        controller._append_event(
            AppEventSource.APP_SERVER,
            event_type,
            thread_id=reviewer_thread,
        )

    assert len(controller._readiness_journal()) == 2
    assert controller._readiness_snapshot_has_new_invalidating_event(
        packet,  # type: ignore[arg-type]
        cfg=store.get_bello_config(),
    )


async def test_runtime_noop_rechecks_readiness_after_subagent_refresh(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    refresh_calls = 0

    async def refresh_with_late_user_activity() -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            controller._append_event(
                AppEventSource.USER,
                "user/input",
                reason="late activity during reviewer completion",
            )

    controller._refresh_coder_subagents = refresh_with_late_user_activity  # type: ignore[method-assign]

    await controller._handle_coder_turn_completed(item_id="done-refresh-race")
    await controller._supervisor_task

    assert refresh_calls == 2
    assert fake.completion_packets == []
    assert "completion/readiness_validation_waived" not in store.path(EVENTS).read_text(
        encoding="utf-8"
    )


async def test_done_without_fresh_validation_runtime_intervene_does_not_resume_completion(
    tmp_path: Path,
) -> None:
    controller, _, fake = _runtime_controller(tmp_path)
    fake.runtime_decision_kind = SupervisorDecisionKind.INTERVENE
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-intervene")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []


async def test_done_without_fresh_validation_runtime_noop_finalizes_when_review_disabled(
    tmp_path: Path,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    controller.adversary_enabled = False
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_review_enabled": False}))
    _prepare_done_without_fresh_validation(controller)

    await controller._handle_coder_turn_completed(item_id="done-review-disabled")
    await controller._supervisor_task

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert fake.completion_packets == []
    assert "completion/readiness_validation_waived" in store.path(EVENTS).read_text(encoding="utf-8")


async def test_done_without_fresh_validation_stale_runtime_noop_does_not_resume_completion(
    tmp_path: Path,
) -> None:
    controller, _, fake = _runtime_controller(tmp_path)
    _prepare_done_without_fresh_validation(controller)
    fake.before_runtime_decision = lambda: controller._append_event(
        AppEventSource.APP_SERVER,
        "test/newer_event",
    )

    await controller._handle_coder_turn_completed(item_id="done-stale")
    await controller._supervisor_task

    assert len(fake.runtime_packets) == 1
    assert fake.completion_packets == []


async def test_controller_idle_guard_forces_completion_review_for_stalled_no_active_turn(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    class FakeCoder:
        active_turn_id = None

        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    coder = FakeCoder()
    controller.coder = coder
    controller.running = True
    controller._last_controller_activity_monotonic = 0.0
    store.update_bello_config(
        lambda cfg: cfg.model_copy(
            update={
                "status": BelloStatus.RUNNING,
                "last_event_sequence": 17,
                "active_coder_turn_id": None,
            }
        )
    )

    await controller._handle_controller_idle_guard(now=119.0)

    assert getattr(controller, "_supervisor_task", None) is None

    await controller._handle_controller_idle_guard(now=121.0)
    await controller._supervisor_task

    assert coder.messages == ["not used"]
    assert coder.messages != [NO_MARKER_IDLE_NUDGE]
    assert len(fake.completion_packets) == 1
    log = store.path(LOG).read_text(encoding="utf-8")
    assert '"type": "controller_idle_guard"' in log


async def test_no_marker_idle_skips_review_for_virgin_generation(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None, "last_event_sequence": 17})
    )
    controller._generation_has_coder_turn = False
    controller.coder = _FakeSteerCoder()

    await controller._handle_no_marker_idle()

    assert fake.completion_packets == []
    assert "Controller forcing completion_review" not in store.path(PROGRESS).read_text(encoding="utf-8")
    assert controller.coder.steers == [POST_RESTART_CONTINUE_NUDGE]


async def test_completion_restart_discarded_for_virgin_generation(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )
    handoff = RestartHandoff(
        objective="task",
        restart_reason="recovery restart before any coder work",
        bad_pattern="none",
        known_evidence="handoff from prior generation",
        next_step="continue",
        recovery_signal="new coder work",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.coder = _FakeSteerCoder()
    controller.pending_approvals = {}
    controller.prior_interventions = []
    controller.validations = []
    controller.observed_changed_files = {}
    controller.use_git_diff = False
    controller.tui = _FakeTUI()
    controller.running = True
    controller.event_queue = asyncio.Queue()
    controller._sequence = 0
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.no_marker_idle_nudge_count = 0
    controller._generation_has_coder_turn = False

    await controller.apply_completion_decision(
        CompletionReviewDecision(
            decision="restart",
            reason="generation recovery before any new coder work",
            uncovered_behaviors=[],
            validation_gaps=["stale prior-generation state"],
            message_to_coder=None,
            persistent_decision=None,
            progress_update="Restarting into recovery.",
            clear_handoff=False,
            display_message=None,
            handoff=handoff,
            wake_sequence=1,
            generation=0,
        ),
        packet_thread_id="thread",
    )

    cfg = store.get_bello_config()
    assert cfg.generation == 0
    assert cfg.status not in (BelloStatus.STUCK, BelloStatus.RESTARTING)
    assert controller.completion_restarts == 0
    assert "Discarded completion restart" in store.path(PROGRESS).read_text(encoding="utf-8")
    events = store.path(EVENTS).read_text(encoding="utf-8")
    assert "completion/restart_discarded_virgin_generation" in events
    assert controller.coder.steers == [POST_RESTART_CONTINUE_NUDGE]
