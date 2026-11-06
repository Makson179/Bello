"""A Goal run continues after /pause and /resume instead of idling forever.

Goal auto-next follows only successful turns, and Goal skips the idle review.
A pause interrupts the coder, so before this fix nothing ever restarted it.
/resume now continues the coder, and an idle coder that nothing else will
continue gets one continuation message after the continuous-idle window.
Offline: fake coder and reviewers, no model or provider process.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.appserver import AppServerMessage
from supervisor.controller import CONTROLLER_IDLE_GUARD_STALL_SECONDS
from supervisor.schemas import BelloStatus
from tests.support.controller import _runtime_controller

CONTINUE = "Continue toward the original Goal."


class _Coder:
    thread_id = "thread"

    def __init__(self, store):
        self.store = store
        self.active_turn_id = "work-turn"
        self.starts = []
        self.interrupts = 0

    def mark_turn_completed(self, turn_id):
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))

    async def interrupt(self):
        self.interrupts += 1

    async def steer_or_start(self, message):
        if self.active_turn_id:
            return self.active_turn_id
        self.starts.append(message)
        self.active_turn_id = f"turn-{len(self.starts)}"
        self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": self.active_turn_id}))
        return self.active_turn_id


def _paused_run(tmp_path, monkeypatch, *, goal):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal": goal, "status": BelloStatus.RUNNING, "active_coder_turn_id": "work-turn"}))
    controller.coder = _Coder(store)
    controller._generation_has_coder_turn = True
    controller.adversary_enabled = False
    monkeypatch.setattr(controller, "_track_subagent_notification", AsyncMock())
    monkeypatch.setattr(controller, "_refresh_coder_subagents", AsyncMock())
    monkeypatch.setattr(controller, "_write_run_checkpoint", Mock())
    monkeypatch.setattr(controller, "_resolve_pending_approvals", AsyncMock())
    controller.finalize = AsyncMock()
    controller.client.thread_read = AsyncMock(return_value={"thread": {"id": "thread", "turns": []}})
    return controller, store, reviewer, clock


async def _command(controller, text):
    await controller.handle_user_command(SimpleNamespace(kind="message", text=text))


async def _interrupted_turn_completes(controller):
    await controller.handle_notification(AppServerMessage({"method": "turn/completed", "params": {
        "threadId": "thread", "turn": {"id": "work-turn", "status": "interrupted", "items": []}}}))


async def _idle_until(controller, clock, seconds):
    for tick in range(int(clock[0] // 60) + 1, int(seconds // 60) + 1):
        clock[0] = tick * 60.0
        await controller._handle_controller_idle_guard(now=clock[0])
        task = controller._supervisor_task
        if task is not None and not task.done():
            await task


@pytest.mark.asyncio
async def test_goal_resume_continues_the_coder_that_the_pause_interrupted(tmp_path, monkeypatch):
    controller, store, _, clock = _paused_run(tmp_path, monkeypatch, goal=True)
    await _command(controller, "/pause")
    assert store.get_bello_config().status == BelloStatus.PAUSED and controller.coder.interrupts == 1
    await _interrupted_turn_completes(controller)
    assert controller.coder.starts == []
    await _command(controller, "/resume")
    assert store.get_bello_config().status == BelloStatus.RUNNING
    assert len(controller.coder.starts) == 1 and CONTINUE in controller.coder.starts[0]


@pytest.mark.asyncio
async def test_goal_idle_safety_net_continues_once_after_the_idle_window(tmp_path, monkeypatch):
    # /resume can arrive before the interrupted turn's completion, which then
    # drops the continuation; the idle safety net recovers the run.
    controller, store, reviewer, clock = _paused_run(tmp_path, monkeypatch, goal=True)
    await _command(controller, "/pause")
    await _command(controller, "/resume")
    await _interrupted_turn_completes(controller)
    assert controller.coder.starts == []
    await _idle_until(controller, clock, CONTROLLER_IDLE_GUARD_STALL_SECONDS - 60)
    assert controller.coder.starts == []
    await _idle_until(controller, clock, CONTROLLER_IDLE_GUARD_STALL_SECONDS + 120)
    assert len(controller.coder.starts) == 1 and CONTINUE in controller.coder.starts[0]
    assert reviewer.completion_packets == []


@pytest.mark.asyncio
async def test_goal_idle_safety_net_waits_for_queued_supervisor_work(tmp_path, monkeypatch):
    controller, store, _, clock = _paused_run(tmp_path, monkeypatch, goal=True)
    await _command(controller, "/pause")
    await _command(controller, "/resume")
    await _interrupted_turn_completes(controller)
    controller._supervisor_next_completion_summary = "Goal completion after adversary"
    monkeypatch.setattr(controller, "_ensure_supervisor_task", lambda: None, raising=False)
    await _idle_until(controller, clock, 2 * CONTROLLER_IDLE_GUARD_STALL_SECONDS)
    assert controller.coder.starts == []


@pytest.mark.asyncio
async def test_non_goal_resume_keeps_the_idle_review(tmp_path, monkeypatch):
    controller, store, reviewer, clock = _paused_run(tmp_path, monkeypatch, goal=False)
    await _command(controller, "/pause")
    await _interrupted_turn_completes(controller)
    await _command(controller, "/resume")
    assert controller.coder.starts == []
    await _idle_until(controller, clock, CONTROLLER_IDLE_GUARD_STALL_SECONDS + 120)
    assert reviewer.completion_packets


@pytest.mark.asyncio
async def test_goal_resume_of_a_fresh_restart_generation_sends_its_restart_kickoff(tmp_path, monkeypatch):
    # A pause during a restart's kickoff leaves a new thread without its first
    # prompt; the generic Goal continuation would lack the task and handoff.
    from supervisor.prompts.supervisor import build_restart_prompt

    controller, store, _, clock = _paused_run(tmp_path, monkeypatch, goal=True)
    controller.coder.task_read_path = tmp_path / "TASK.md"
    controller.coder.plan_path = None
    await _command(controller, "/pause")
    await _interrupted_turn_completes(controller)
    controller._generation_has_coder_turn = False
    await _command(controller, "/resume")
    assert controller.coder.starts == [build_restart_prompt(tmp_path / "TASK.md", plan_path=None, goal=True)]
    assert "HANDOFF" in controller.coder.starts[0] and "BELLO_READY_FOR_REVIEW" in controller.coder.starts[0]

