"""A Goal advisory that starts a coder turn cancels a readiness review queued behind it.

The periodic Goal progress review can be in flight when the coder declares
readiness. If that review then delivers an advisory, the coder works again, so
the queued readiness review must not judge (and accept) the changing workspace.
Offline: fake coder and reviewers, no model or provider process.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from supervisor.schemas import AppEventSource, BelloStatus, CoderMessage, CompletionReviewDecision, SupervisorDecision
from tests.support.controller import _runtime_controller


def _accept(wake):
    return CompletionReviewDecision(
        decision="accept", reason="looks complete", uncovered_behaviors=[], message_to_coder=None,
        persistent_decision=None, progress_update=None, clear_handoff=False, display_message=None,
        handoff=None, wake_sequence=wake, generation=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("advisory", [True, False])
async def test_delivered_advisory_cancels_the_queued_readiness_review(tmp_path, monkeypatch, advisory):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal": True, "last_event_sequence": 10, "active_coder_turn_id": "work-turn"}))
    controller.adversary_enabled = False
    controller._generation_has_coder_turn = True
    controller._sequence = 10
    starts = []

    class Coder:
        thread_id = "thread"
        active_turn_id = "work-turn"

        async def steer_or_start(self, message):
            if self.active_turn_id:
                return self.active_turn_id
            starts.append(message)
            self.active_turn_id = "advisory-turn"
            store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "advisory-turn"}))
            return "advisory-turn"

    controller.coder = Coder()
    controller.restart, controller.pause = AsyncMock(), AsyncMock()
    finalized = []

    async def finalize(result, *, status=BelloStatus.COMPLETE, completion_review_accepted=False):
        finalized.append((status, completion_review_accepted, store.get_bello_config().active_coder_turn_id))
        controller.running = False

    controller.finalize = finalize
    release, started = asyncio.Event(), asyncio.Event()

    async def decide(packet):
        started.set()
        await release.wait()
        extra = {"advisory_key": "approach:A", "advisory_to_coder": "Consider approach B."} if advisory else {}
        return SupervisorDecision(decision="noop", generation=0, wake_sequence=packet.wake_sequence, **extra)

    reviewer.decide = decide
    reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _accept(packet.wake_sequence))
    # Twenty minutes of Goal work schedule the progress review during a turn.
    controller._record_goal_work(1)
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 1200
    controller._record_goal_work(2)
    controller._maybe_schedule_goal_progress_review()
    await started.wait()
    # While it is in flight, the coder ends its turn with the readiness marker.
    controller.coder.active_turn_id = None
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
    controller._append_event(AppEventSource.APP_SERVER, "turn/completed", thread_id="thread", turn_id="work-turn")
    sequence = store.get_bello_config().last_event_sequence
    controller._sequence = sequence
    controller.last_coder_message = CoderMessage(text="Done.\nBELLO_READY_FOR_REVIEW", sequence=sequence)
    await controller._handle_coder_turn_completed(item_id=None)
    assert controller._supervisor_next_completion_check is not None
    release.set()
    await controller._supervisor_task
    if advisory:
        assert len(starts) == 1 and "Optional Goal observation" in starts[0]
        reviewer.decide_completion.assert_not_awaited()
        assert finalized == [] and controller._supervisor_next_completion_check is None
    else:
        assert starts == []
        assert finalized == [(BelloStatus.COMPLETE, True, None)]
