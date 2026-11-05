"""Offline contracts for the opt-in first-adversary checkpoint in Goal mode."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.adversary_agent import AdversaryAgentError
from supervisor.controller import BelloController
from supervisor.project_config import ProjectConfig, load_project_config
from supervisor.schemas import AdvReportControllerDecision, BelloStatus
from supervisor.state import EVENTS
from tests.support.controller import (
    _FakeAdvReportController,
    _FakeTUI,
    _GateFakeCoder,
    _runtime_controller,
)
from tests.test_goal_mode import _decision


def _checkpoint_controller(tmp_path):
    controller, store, reviewer = _runtime_controller(tmp_path)
    controller.goal = True
    controller.adversary_enabled = True
    controller.client = object()
    controller.coder = _GateFakeCoder()
    controller.finalize = AsyncMock()
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal_adversary_after_returns": 3,
        "max_adversary_runs": 1,
        "max_completion_returns_before_adversary": 0,
        "max_completion_returns_after_adversary": 0,
    }))
    return controller, store, reviewer


def _install_adversary(monkeypatch, *, error=None, started=None, release=None):
    calls = []

    class OfflineAdversary:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, packet, *, previous_adversary_report=None):
            calls.append(packet)
            if started is not None:
                started.set()
            if release is not None:
                await release.wait()
            if error is not None:
                raise error
            return SimpleNamespace(
                report_text="attacked: boundaries\nfindings: none\noverall: held",
                thread_id="offline-adversary", turn_id="offline-turn", candidate_finding=False,
            )

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", OfflineAdversary)
    return calls


async def _readiness(controller):
    await controller._run_supervisor_check("coder ready after repair", None, None, None, None, True)


def _events(store):
    return [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]


async def test_checkpoint_runs_after_third_repair_then_requires_a_post_adversary_accept(tmp_path, monkeypatch):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    controller._queue_supervisor_check = Mock()
    adversary_calls = _install_adversary(monkeypatch)

    for returned in range(1, 4):
        await _readiness(controller)
        assert len(reviewer.completion_packets) == returned
        assert len(controller.coder.messages) == returned
        assert store.get_bello_config().completion_return_count == returned
        assert store.get_bello_config().adversary_run_count == 0
        assert adversary_calls == []
        controller._queue_supervisor_check.assert_not_called()
        controller.finalize.assert_not_awaited()

    # The third return was delivered; the coder's next readiness triggers A, not C4.
    await _readiness(controller)
    assert len(reviewer.completion_packets) == 3
    assert len(controller.coder.messages) == 3
    assert len(adversary_calls) == 1
    assert store.get_bello_config().adversary_run_count == 1
    assert store.get_bello_config().completion_return_count == 3
    assert store.get_bello_config().completion_returns_since_adversary == 0
    controller._queue_supervisor_check.assert_called_once_with(
        "Goal completion after adversary", completion_review=True,
    )
    controller.finalize.assert_not_awaited()
    assert getattr(controller, "_accepted_completion_decision", None) is None
    assert not any(event["event_type"] in {"completion/accept", "completion/budget_finalize"}
                   for event in _events(store))

    # Goal ignores the normal post-adversary cap (zero) and the checkpoint (three).
    for returned in range(1, 6):
        await _readiness(controller)
        assert store.get_bello_config().completion_returns_since_adversary == returned
        assert store.get_bello_config().completion_return_count == 3 + returned
        assert len(controller.coder.messages) == 3 + returned
        assert len(adversary_calls) == 1
        controller.finalize.assert_not_awaited()

    reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _decision(
        "accept", packet.wake_sequence, "Original objective satisfied after adversary",
    ))
    await _readiness(controller)
    reviewer.decide_completion.assert_awaited_once()
    assert len(adversary_calls) == store.get_bello_config().adversary_run_count == 1
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs == {
        "status": BelloStatus.COMPLETE, "completion_review_accepted": True,
    }


@pytest.mark.parametrize("prior_returns", [0, 2])
async def test_early_accept_runs_adversary_and_still_queues_a_fresh_completion_review(
    tmp_path, monkeypatch, prior_returns,
):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_return_count": prior_returns}))
    controller._queue_supervisor_check = Mock()
    reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _decision(
        "accept", packet.wake_sequence, "Original objective satisfied",
    ))
    adversary_calls = _install_adversary(monkeypatch)

    await _readiness(controller)

    reviewer.decide_completion.assert_awaited_once()
    assert len(adversary_calls) == 1
    assert store.get_bello_config().completion_return_count == prior_returns
    controller._queue_supervisor_check.assert_called_once_with(
        "Goal completion after adversary", completion_review=True,
    )
    controller.finalize.assert_not_awaited()
    assert getattr(controller, "_accepted_completion_decision", None) is None
    assert not any(event["event_type"] == "completion/accept" for event in _events(store))

    await _readiness(controller)
    assert reviewer.decide_completion.await_count == 2
    assert len(adversary_calls) == 1
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs["completion_review_accepted"] is True


async def test_repeated_readiness_while_adversary_runs_reserves_only_one_pass(tmp_path, monkeypatch):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_return_count": 3}))
    started, release = asyncio.Event(), asyncio.Event()
    adversary_calls = _install_adversary(monkeypatch, started=started, release=release)

    controller._schedule_supervisor_check("checkpoint readiness", completion_review=True)
    task = controller._supervisor_task
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        for _ in range(5):
            controller._schedule_supervisor_check("repeated readiness", completion_review=True)
        assert len(adversary_calls) == store.get_bello_config().adversary_run_count == 1
        assert reviewer.completion_packets == []
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)

    assert len(adversary_calls) == store.get_bello_config().adversary_run_count == 1
    assert len(reviewer.completion_packets) == 1
    assert store.get_bello_config().completion_return_count == 4
    assert len(controller.coder.messages) == 1
    controller.finalize.assert_not_awaited()


async def test_adversary_findings_return_does_not_consume_completion_return_count(tmp_path, monkeypatch):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_return_count": 3}))
    controller.adv_report_controller = _FakeAdvReportController([
        AdvReportControllerDecision(
            forward_to_coder=True, reason="Boundary input still fails",
            report_to_coder="## Findings requiring correction\n- Fix the failing boundary input.",
        ),
    ])
    adversary_calls = _install_adversary(monkeypatch)

    await _readiness(controller)

    assert len(adversary_calls) == 1
    assert reviewer.completion_packets == []
    assert len(controller.coder.messages) == 1
    assert "Fix the failing boundary input." in controller.coder.messages[0]
    assert store.get_bello_config().completion_return_count == 3
    assert store.get_bello_config().completion_returns_since_adversary == 0
    assert controller.completion_returns[-1].source == "adversary_report_controller"
    controller.finalize.assert_not_awaited()

    await _readiness(controller)
    assert len(adversary_calls) == 1
    assert len(reviewer.completion_packets) == 1
    assert store.get_bello_config().completion_return_count == 4
    assert store.get_bello_config().completion_returns_since_adversary == 1


@pytest.mark.parametrize("early_accept", [False, True])
@pytest.mark.parametrize("failure", ["provider", "snapshot"])
async def test_required_checkpoint_adversary_failure_cannot_reuse_an_early_accept(
    tmp_path, monkeypatch, early_accept, failure,
):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    if early_accept:
        reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _decision(
            "accept", packet.wake_sequence, "Original objective satisfied",
        ))
    else:
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"completion_return_count": 3}))
    adversary_calls = _install_adversary(monkeypatch, error=AdversaryAgentError("provider unavailable"))
    if failure == "snapshot":
        monkeypatch.setattr("supervisor.controller._create_adversary_snapshot", Mock(
            side_effect=OSError("snapshot unavailable"),
        ))

    await _readiness(controller)

    assert len(adversary_calls) == (1 if failure == "provider" else 0)
    assert store.get_bello_config().adversary_run_count == 1
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs == {
        "status": BelloStatus.PROVIDER_FAILURE, "completion_review_accepted": False,
    }
    assert controller._pending_adversary_report.status == "error"
    assert getattr(controller, "_accepted_completion_decision", None) is None
    assert not any(event["event_type"] == "completion/accept" for event in _events(store))


@pytest.mark.parametrize("goal,adversary,checkpoint,expected", [
    (True, False, 3, None),
    (True, True, 0, None),
    (False, True, 3, "adversary"),
    (False, False, 3, "complete"),
])
def test_disabled_checkpoint_preserves_existing_review_policy(tmp_path, goal, adversary, checkpoint, expected):
    controller, store, _ = _checkpoint_controller(tmp_path)
    controller.goal = goal
    controller.adversary_enabled = adversary
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal_adversary_after_returns": checkpoint, "completion_return_count": 1000,
    }))
    assert controller._completion_review_budget_action() == expected


@pytest.mark.parametrize("goal,checkpoint", [(True, 0), (False, 3)])
async def test_disabled_checkpoint_keeps_early_accept_after_clean_adversary(
    tmp_path, monkeypatch, goal, checkpoint,
):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    controller.goal = goal
    controller._queue_supervisor_check = Mock()
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal_adversary_after_returns": checkpoint,
        "max_completion_returns_before_adversary": "unlimited",
    }))
    reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _decision(
        "accept", packet.wake_sequence, "Original objective satisfied",
    ))
    adversary_calls = _install_adversary(monkeypatch)

    await _readiness(controller)

    reviewer.decide_completion.assert_awaited_once()
    assert len(adversary_calls) == 1
    controller._queue_supervisor_check.assert_not_called()
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs == {
        "status": BelloStatus.COMPLETE, "completion_review_accepted": True,
    }


@pytest.mark.parametrize("max_runs", [1, 2])
def test_checkpoint_only_advances_the_first_pass_and_never_caps_post_adversary_returns(tmp_path, max_runs):
    controller, store, _ = _checkpoint_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_adversary_runs": max_runs, "adversary_run_count": 1,
        "completion_return_count": 1000, "completion_returns_since_adversary": 997,
    }))
    assert controller._completion_review_budget_action() is None
    assert controller._effective_max_adversary_runs() == max_runs


@pytest.mark.parametrize("override,expected", [(None, 3), (0, 0), (7, 7)])
def test_checkpoint_setting_persists_to_run_and_project_config(tmp_path, override, expected):
    task = tmp_path / "TASK.md"
    task.write_text("Implement the requested behavior.\n", encoding="utf-8")
    saved = ProjectConfig(task="TASK.md", goal=True, adversary=True, goal_adversary_after_returns=3)
    controller = BelloController(
        tmp_path, task_path=task, tui=_FakeTUI(), project_config=saved,
        goal_adversary_after_returns=override,
    )
    controller.initialize_state()
    assert controller.store.get_bello_config().goal_adversary_after_returns == expected
    assert saved.goal_adversary_after_returns == 3
    controller._persist_model_config()
    reloaded = load_project_config(tmp_path, create=False)
    assert reloaded.goal_adversary_after_returns == expected
    recovered = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), project_config=reloaded)
    assert recovered._goal_adversary_after_returns() == expected


def test_checkpoint_setting_restores_from_runtime_state_and_explicit_zero_wins(tmp_path):
    controller, _, _ = _checkpoint_controller(tmp_path)
    assert controller._goal_adversary_after_returns() == 3
    controller.goal_adversary_after_returns = 0
    assert controller._goal_adversary_after_returns() == 0
