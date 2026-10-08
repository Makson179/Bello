"""A post-adversary terminal status must preserve the actual review verdict."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor.schemas import BelloStatus, CompletionReviewDecision
from supervisor.state import EVENTS, FINAL_REPORT
from tests.support.controller import _covered_accept_decision, _runtime_controller


@pytest.mark.parametrize("remaining_returns", [0, 1, 2])
async def test_post_adversary_followup_requires_a_new_accept_when_budget_remains(
    tmp_path: Path, remaining_returns: int,
) -> None:
    controller, store, reviewer = _runtime_controller(tmp_path)
    controller.adversary_enabled = True
    controller._sequence = 20
    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={
            "adversary_run_count": 1,
            "max_adversary_runs": 1,
            "max_completion_returns_after_adversary": remaining_returns,
            "last_applied_supervisor_sequence": 10,
            "last_event_sequence": 20,
        })
    )
    earlier_accept = _covered_accept_decision(wake_sequence=10)
    controller._accepted_completion_decision = earlier_accept
    followup = CompletionReviewDecision(
        decision="return", reason="Investigate the normalized adversary observation.",
        uncovered_behaviors=["Adversary observation requires investigation."],
        message_to_coder="Investigate and validate the observation, then report readiness.",
        wake_sequence=11, generation=0,
        persistent_decision=None, progress_update=None, clear_handoff=False,
        display_message=None, handoff=None,
    )
    await controller._return_completion_to_coder(
        followup, source="adversary_report_controller",
    )

    # A normalized adversary report is not itself a completion-review return.
    assert store.get_bello_config().completion_returns_since_adversary == 0
    assert store.get_bello_config().completion_return_count == 0
    assert reviewer.closed_completion_reviews == 1

    async def accept_current_packet(packet):
        reviewer.completion_packets.append(packet)
        return _covered_accept_decision(wake_sequence=packet.wake_sequence).model_copy(
            update={"generation": packet.generation},
        )

    reviewer.decide_completion = accept_current_packet
    await controller._run_supervisor_check(
        "Coder ready after adversary follow-up", None, None, None, None, True,
    )

    assert store.get_bello_config().status == BelloStatus.COMPLETE
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    if remaining_returns:
        assert len(reviewer.completion_packets) == 1
        reviewed = reviewer.completion_packets[0]
        assert reviewed.wake_sequence > earlier_accept.wake_sequence
        assert controller._accepted_completion_decision.wake_sequence == reviewed.wake_sequence
        assert controller._accepted_completion_decision is not earlier_accept
        assert "- Completion review accepted: true" in report
        assert any(event["event_type"] == "completion/accept" for event in events)
        assert not any(event["event_type"] == "completion/budget_finalize" for event in events)
    else:
        assert reviewer.completion_packets == []
        assert controller._accepted_completion_decision is None
        assert "- Completion review accepted:" not in report
        assert any(event["event_type"] == "completion/budget_finalize" for event in events)
        assert not any(event["event_type"] == "completion/accept" for event in events)
