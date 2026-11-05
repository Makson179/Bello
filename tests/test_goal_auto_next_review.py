"""Independent Goal continuation regression review; no provider/model calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.appserver import AppServerMessage
from supervisor.schemas import BelloStatus, CoderMessage
from tests.support.controller import _runtime_controller


class _OwnedCoder:
    thread_id = "thread"

    def __init__(self, store):
        self.store = store
        self.active_turn_id = "old-turn"
        self.starts: list[tuple[str, str]] = []
        self.steers: list[tuple[str, str]] = []

    def mark_turn_completed(self, turn_id):
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
            self.store.update_bello_config(
                lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None})
            )

    async def steer_or_start(self, message):
        if self.active_turn_id:
            self.steers.append((self.active_turn_id, message))
            return self.active_turn_id
        turn_id = f"next-{len(self.starts) + 1}"
        self.starts.append((turn_id, message))
        self.active_turn_id = turn_id
        self.store.update_bello_config(
            lambda cfg: cfg.model_copy(update={"active_coder_turn_id": turn_id})
        )
        return turn_id


@pytest.fixture
def goal(tmp_path, monkeypatch):
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal": True,
        "status": BelloStatus.RUNNING,
        "active_coder_turn_id": "old-turn",
    }))
    controller.coder = _OwnedCoder(store)
    controller._generation_has_coder_turn = True
    monkeypatch.setattr(controller, "_track_subagent_notification", AsyncMock())
    monkeypatch.setattr(controller, "_refresh_coder_subagents", AsyncMock())
    monkeypatch.setattr(controller, "_repair_snapshot_runtime_controls", Mock(return_value=False))
    monkeypatch.setattr(controller, "_escalate_runtime_integrity_issue", AsyncMock(return_value=False))
    monkeypatch.setattr(controller, "_write_run_checkpoint", Mock())
    monkeypatch.setattr(controller, "_schedule_supervisor_check", Mock())
    return controller, store, reviewer


def _completed(status="completed", turn_id="old-turn"):
    return AppServerMessage({"method": "turn/completed", "params": {
        "threadId": "thread", "turn": {"id": turn_id, "status": status, "items": []},
    }})


async def test_successful_empty_turn_continues_without_quiescence_rpc(goal):
    c, _, _ = goal
    c.client.read_execution_status = AsyncMock(side_effect=AssertionError("No idle RPC after authoritative completion"))
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1
    assert "original Goal" in c.coder.starts[0][1]
    assert c.coder.steers == []
    c.client.read_execution_status.assert_not_awaited()
    c._schedule_supervisor_check.assert_not_called()


async def test_running_runtime_does_not_serialize_next_coder_turn(goal):
    c, _, _ = goal
    task = asyncio.create_task(asyncio.Event().wait())
    c._supervisor_task = task
    try:
        await asyncio.wait_for(c.handle_notification(_completed()), timeout=0.5)
        assert len(c.coder.starts) == 1
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_duplicate_completed_event_does_not_start_or_steer_again(goal):
    c, _, _ = goal
    await c.handle_notification(_completed())
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1
    assert c.coder.steers == []


async def test_late_runtime_message_during_refresh_satisfies_pending_next(goal):
    c, _, _ = goal
    async def late_runtime():
        await c._deliver_coder_message("Optional neutral observation.")
    c._refresh_coder_subagents.side_effect = late_runtime
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1
    assert c.coder.starts[0][1] == "Optional neutral observation."
    assert c.coder.steers == []


@pytest.mark.parametrize("change", ["pause", "replace", "active"])
async def test_refresh_race_never_reactivates_or_steers_current_work(goal, change):
    c, store, _ = goal
    original = c.coder
    async def race():
        if change == "pause":
            c.paused = True
        elif change == "replace":
            c.coder = _OwnedCoder(store)
            c.coder.active_turn_id = None
        else:
            original.active_turn_id = "external-turn"
            store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "external-turn"}))
    c._refresh_coder_subagents.side_effect = race
    await c.handle_notification(_completed())
    assert original.starts == []
    assert original.steers == []
    assert c.coder.starts == []


async def test_pause_while_waiting_for_delivery_lock_prevents_start(goal):
    c, _, _ = goal
    lock = c._coder_activity_lock()
    await lock.acquire()
    task = asyncio.create_task(c.handle_notification(_completed()))
    try:
        await asyncio.sleep(0)
        c.paused = True
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=0.5)
    assert c.coder.starts == []


@pytest.mark.parametrize("blocker", ["approval", "persisted_request", "child"])
async def test_blocked_successful_turn_starts_once_after_blocker_clears(goal, blocker, monkeypatch):
    c, store, _ = goal
    children = []
    if blocker == "approval":
        c.pending_approvals["approval"] = SimpleNamespace()
    elif blocker == "persisted_request":
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": ["request"]}))
    else:
        children.append(SimpleNamespace(thread_id="child"))
        monkeypatch.setattr(c, "_active_coder_subagents", lambda: children)
    await c.handle_notification(_completed())
    assert c.coder.starts == []
    c.pending_approvals.clear()
    children.clear()
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": []}))
    await c._handle_controller_idle_guard()
    await c._handle_controller_idle_guard()
    assert len(c.coder.starts) == 1
    assert c.coder.steers == []


async def test_exact_marker_preserves_final_review_instead_of_auto_next(goal, monkeypatch):
    c, _, _ = goal
    c.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=3)
    monkeypatch.setattr(c, "_done_without_fresh_behavioral_validation", AsyncMock(return_value=None))
    await c.handle_notification(_completed())
    assert c.coder.starts == []
    assert c._schedule_supervisor_check.call_args.kwargs["completion_review"] is True


@pytest.mark.parametrize("text", ["Summary: done", "bello_ready_for_review"])
async def test_readiness_without_exact_marker_gets_clarification_not_auto_next(goal, text):
    c, _, _ = goal
    c.last_coder_message = CoderMessage(text=text, sequence=3)
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1
    assert "BELLO_READY_FOR_REVIEW" in c.coder.starts[0][1]
    assert "Continue toward the original Goal" not in c.coder.starts[0][1]
    c._schedule_supervisor_check.assert_not_called()


@pytest.mark.parametrize("status", ["interrupted", "unknown", None])
async def test_non_success_does_not_authorize_automatic_next(goal, status):
    c, _, _ = goal
    await c.handle_notification(_completed(status=status))
    await c._handle_controller_idle_guard()
    assert c.coder.starts == []


async def test_failed_turn_keeps_provider_failure_policy(goal, monkeypatch):
    c, store, _ = goal
    fail = AsyncMock()
    monkeypatch.setattr(c, "fail_provider", fail)
    await c.handle_notification(_completed(status="failed"))
    fail.assert_awaited_once()
    assert store.get_bello_config().active_coder_turn_id is None
    assert c.coder.starts == []


@pytest.mark.parametrize("text", ["BELLO_READY_FOR_REVIEW", "Summary: done\nBELLO_READY_FOR_REVIEW"])
async def test_already_handled_marker_cannot_strand_an_empty_later_turn(goal, text):
    c, _, _ = goal
    c.last_coder_message = CoderMessage(text=text, sequence=3)
    c._last_completion_marker_sequence = 3
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1
    assert "Continue toward the original Goal" in c.coder.starts[0][1]
    c._schedule_supervisor_check.assert_not_called()
