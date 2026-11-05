"""Neutral virtual-time coverage for automatic, no-marker completion only."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from supervisor.appserver import AppServerMessage
from supervisor.controller import ControllerEvent
from supervisor.controller_parts.defaults import NO_MARKER_IDLE_REVIEW_SUMMARY
from supervisor.schemas import BelloStatus, CoderMessage
from supervisor.tui import UserCommand
from tests.support.controller import _FakeSteerCoder, _runtime_controller


@pytest.fixture
def idle(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    controller.coder = _FakeSteerCoder()
    controller.coder.active_turn_id = None
    controller._generation_has_coder_turn = True
    controller._track_subagent_notification = _noop
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
    return controller, store, reviewer, clock


async def _noop(*args, **kwargs):
    pass


@pytest.mark.parametrize("runtime", [False, True])
async def test_turn_completed_cannot_force_review_before_40_minutes(idle, runtime):
    controller, store, reviewer, clock = idle
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"runtime_enabled": runtime}))
    await controller._handle_coder_turn_completed(item_id=None)
    assert reviewer.completion_packets == []
    for seconds in (60, 120, 600, 1800, 2399):
        clock[0] = seconds
        await controller._handle_controller_idle_guard(force=True)
        assert controller._supervisor_task is None
    clock[0] = 2400
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_technical_heartbeat_and_controller_polls_do_not_restart_clock(idle):
    controller, _, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    for seconds in range(60, 2401, 60):
        clock[0] = seconds
        controller._mark_controller_activity()
        await controller.handle_notification(AppServerMessage({"method": "deprecationNotice", "params": {}}))
        await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_real_coder_message_starts_a_new_idle_window(idle):
    controller, _, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2300
    await controller.handle_notification(AppServerMessage({
        "method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
        "item": {"id": "message", "type": "agentMessage", "text": "Neutral progress."}},
    }))
    await controller._handle_no_marker_idle()
    clock[0] = 4699
    await controller._handle_controller_idle_guard()
    assert reviewer.completion_packets == []
    clock[0] = 4700
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


@pytest.mark.parametrize("blocker", ["turn", "coder_turn", "approval", "persisted_approval", "child", "pause", "stopped"])
async def test_active_work_resets_the_window(idle, blocker, monkeypatch):
    controller, store, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2390
    if blocker == "turn":
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "active"}))
    elif blocker == "coder_turn":
        controller.coder.active_turn_id = "active"
    elif blocker == "approval":
        controller.pending_approvals[1] = object()
    elif blocker == "persisted_approval":
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": [1]}))
    elif blocker == "child":
        monkeypatch.setattr(controller, "_active_coder_subagents", lambda: [object()])
    elif blocker == "pause":
        controller.paused = True
    else:
        controller.running = False
    assert controller._no_marker_idle_ready() is False
    assert controller._no_marker_idle_since is None
    controller.coder.active_turn_id = None
    controller.pending_approvals.clear()
    controller.paused = False
    controller.running = True
    controller._coder_watch = None
    monkeypatch.setattr(controller, "_active_coder_subagents", lambda: [])
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None, "pending_server_request_ids": []}))
    clock[0] = 2500
    await controller._handle_no_marker_idle()
    clock[0] = 4899
    await controller._handle_controller_idle_guard()
    assert reviewer.completion_packets == []
    clock[0] = 4900
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


@pytest.mark.parametrize("changed", ["generation", "thread", "coder"])
async def test_recovery_or_replacement_never_inherits_monotonic_credit(idle, changed):
    controller, store, _, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 5000
    if changed == "coder":
        controller.coder = _FakeSteerCoder()
    else:
        update = {"generation": 1} if changed == "generation" else {"coder_thread_id": "new-thread"}
        store.update_bello_config(lambda cfg: cfg.model_copy(update=update))
    assert controller._no_marker_idle_ready() is False
    assert controller._no_marker_idle_since == 5000


async def test_refresh_race_cannot_review_a_new_active_turn(idle, monkeypatch):
    controller, store, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    async def refreshed():
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "new"}))
    monkeypatch.setattr(controller, "_refresh_coder_subagents", refreshed)
    await controller._handle_no_marker_idle()
    assert reviewer.completion_packets == []
    assert controller._no_marker_idle_since is None


@pytest.mark.parametrize("during_snapshot", [False, True])
async def test_queued_idle_review_rechecks_activity_before_model(idle, monkeypatch, during_snapshot):
    controller, store, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    async def changed():
        controller._reset_no_marker_idle()
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "new"}))
        return []
    if during_snapshot:
        monkeypatch.setattr(controller, "changed_files", changed)
    else:
        await changed()
    await controller._run_supervisor_check(NO_MARKER_IDLE_REVIEW_SUMMARY, None, None, None, None, True)
    assert reviewer.completion_packets == []


async def test_pending_events_defer_but_do_not_strand_idle_review(idle):
    controller, _, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    controller.event_queue.put_nowait(object())
    await controller._handle_no_marker_idle()
    assert reviewer.completion_packets == []
    controller.event_queue.get_nowait()
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_exact_readiness_is_immediate(idle):
    controller, _, reviewer, _ = idle
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=1)
    await controller._handle_coder_turn_completed(item_id=None)
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_review_return_can_start_a_new_window_without_new_message(idle):
    controller, store, reviewer, clock = idle
    controller.adversary_enabled = False
    # The ordinary fallback still returns work after an idle review. Goal now
    # continues the coder instead; its dedicated regression covers that path.
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal": False, "adversary_enabled": False, "max_adversary_runs": 0,
        "max_completion_returns_before_adversary": 2,
    }))
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    await controller._handle_no_marker_idle()
    await controller._supervisor_task
    # Fake coder's steer emits no message/validation, but delivery is activity.
    assert controller._no_marker_completion_review_key is None
    await controller._handle_no_marker_idle()
    clock[0] = 4800
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 2


async def test_terminal_turn_stale_tool_watch_cannot_block_idle_forever(idle):
    controller, _, reviewer, clock = idle
    controller._coder_watch = SimpleNamespace(identity=(0, "thread", "old-turn", 123), running_tools={"old-tool"})
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_pause_and_resume_do_not_credit_paused_time(idle, monkeypatch):
    controller, _, reviewer, clock = idle
    for name in ("_wait_for_revision_switch", "_wait_for_coder_activity", "_quiesce_coder_tree", "_resolve_pending_approvals"):
        monkeypatch.setattr(controller, name, _noop)
    await controller._handle_no_marker_idle()
    clock[0] = 2300
    await controller.pause()
    assert controller._no_marker_idle_since is None
    clock[0] = 10000
    await controller.handle_user_command(UserCommand(text="/resume"))
    await controller._handle_controller_idle_guard()
    assert reviewer.completion_packets == []
    clock[0] = 12399
    await controller._handle_controller_idle_guard()
    assert reviewer.completion_packets == []
    clock[0] = 12400
    await controller._handle_controller_idle_guard()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_activity_during_refresh_uses_fresh_clock_not_tick_timestamp(idle, monkeypatch):
    controller, _, reviewer, clock = idle
    await controller._handle_no_marker_idle()
    clock[0] = 2400
    async def refreshed():
        clock[0] = 2500
        controller._reset_no_marker_idle()
    monkeypatch.setattr(controller, "_refresh_coder_subagents", refreshed)
    await controller._handle_controller_idle_guard(now=2400)
    assert controller._no_marker_idle_since == 2500
    assert reviewer.completion_packets == []


async def test_event_loop_ticks_idle_window_despite_status_notifications(idle, monkeypatch):
    controller, _, _, clock = idle
    controller.tui.input_queue = asyncio.Queue()
    scheduled = []
    def schedule(summary, **kwargs):
        scheduled.append((clock[0], summary, kwargs))
        controller.running = False
    monkeypatch.setattr(controller, "_schedule_supervisor_check", schedule)
    await controller._handle_no_marker_idle()
    original_wait = asyncio.wait
    async def notification_wait(tasks, **kwargs):
        clock[0] += 60
        assert clock[0] <= 2400, "busy status queue starved the idle watchdog"
        controller.event_queue.put_nowait(ControllerEvent(kind="notification", message=AppServerMessage({
            "method": "deprecationNotice", "params": {},
        })))
        return await original_wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    monkeypatch.setattr("supervisor.controller.asyncio.wait", notification_wait)
    await controller.event_loop()
    assert len(scheduled) == 1
    assert scheduled[0][0] == 2400
    assert scheduled[0][2]["completion_review"] is True
