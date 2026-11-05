"""A failed optional Goal progress review is skipped instead of ending the run.

Only an authentication failure stays fatal: it is a configuration error that
every later runtime check would hit as well. Offline: fake reviewer and coder.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from supervisor.schemas import BelloStatus
from supervisor.supervisor_agent import SupervisorAgentError
from tests.support.controller import _runtime_controller


@pytest.mark.asyncio
@pytest.mark.parametrize("error, kind", [
    ("supervisor turn timed out after 300s", "tool_timeout"),
    ("rate limit exceeded (429)", "rate"),
    ("invalid supervisor decision: runtime advisory must be a noop without correction", "unknown"),
    ("unauthorized: the runtime model login expired", "auth"),
])
async def test_failed_goal_progress_review_is_skipped_unless_authentication_failed(tmp_path, monkeypatch, error, kind):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "last_event_sequence": 10}))
    controller.coder = SimpleNamespace(thread_id="thread", active_turn_id="turn", steer_or_start=AsyncMock())
    controller.finalize = AsyncMock()

    async def decide(packet):
        reviewer.runtime_packets.append(packet)
        raise SupervisorAgentError(error)

    reviewer.decide = decide
    controller._record_goal_work(1)
    controller._maybe_schedule_goal_progress_review()
    clock[0] = 1200
    controller._record_goal_work(2)
    controller._maybe_schedule_goal_progress_review()
    await controller._supervisor_task
    assert reviewer.runtime_packets and reviewer.runtime_packets[0].goal_progress_review
    progress = (store.state_dir / "PROGRESS.md").read_text(encoding="utf-8")
    if kind == "auth":
        controller.finalize.assert_awaited_once()
        assert controller.finalize.await_args.kwargs["status"] == BelloStatus.PROVIDER_FAILURE
    else:
        controller.finalize.assert_not_awaited()
        assert f"Optional Goal progress review failed with {kind}; skipped." in progress
        # The cadence slot was consumed: no immediate retry is scheduled.
        assert getattr(controller, "_supervisor_next_runtime_summary", None) is None
