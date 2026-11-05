"""Synthetic metadata-only idle fencing; no models, real processes or network."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from supervisor.appserver import AppServerError
from supervisor.controller_parts.defaults import NO_MARKER_IDLE_REVIEW_SUMMARY
from tests.support.controller import _FakeSteerCoder, _runtime_controller
from tests.test_runtime_codex_backend import drain
from tests.test_runtime_terminal_reconciliation import active_runtime, native_platform_gate


def idle_reply():
    return {"thread": {"id": "native-thread-1", "status": {"type": "idle"}, "turns": [
        {"id": "native-turn-1", "status": "completed", "items": []},
    ]}}


@pytest.fixture
async def native(tmp_path):
    state = await active_runtime(tmp_path)
    client, backend, peer, _, thread, _ = state
    peer.request = AsyncMock(return_value=idle_reply())
    try:
        yield state
    finally:
        await client.stop()


async def test_exact_full_native_read_is_quiescent_and_metadata_only(native):
    client, backend, peer, notifications, thread, _ = native
    reply = idle_reply()
    reply["thread"]["turns"][0]["items"] = [{"type": "agentMessage", "text": "PRIVATE_CANARY"}]
    peer.request.return_value = reply
    before = deepcopy(backend._native_turns)
    assert await client.read_execution_status(thread) == {
        "state": "quiescent", "pending_tools": False, "unowned_active_turn": False,
    }
    peer.request.assert_awaited_once_with("thread/read", {"threadId": "native-thread-1", "includeTurns": True}, timeout=10)
    assert backend._native_turns == before and notifications == []
    assert reply["thread"]["turns"][0]["items"][0]["text"] == "PRIVATE_CANARY"


async def test_unknown_live_successor_is_busy_without_adopting_or_dispatching(native):
    client, backend, peer, notifications, thread, turn = native
    assert await client.reconcile_terminal_turn(thread, turn, {"id": turn, "status": "completed", "items": []})
    notifications.clear()
    reply = idle_reply()
    reply["thread"]["turns"].append({"id": "unowned-successor", "status": "inProgress", "items": []})
    peer.request.return_value = reply
    before = deepcopy(backend._native_turns)
    assert await client.read_execution_status(thread) == {
        "state": "busy", "pending_tools": False, "unowned_active_turn": True,
    }
    assert backend._native_turns == before
    assert backend._threads[thread].get("activeTurnId") is None
    assert client._threads[thread].get("activeTurnId") is None
    assert notifications == []
    assert [call.args[0] for call in peer.request.await_args_list] == ["thread/read"]


@pytest.mark.parametrize("kind", ["commandExecution", "dynamicToolCall", "mcpToolCall", "collabAgentToolCall"])
async def test_explicit_pending_tool_never_proves_quiescence(native, kind):
    client, _, peer, _, thread, _ = native
    reply = idle_reply()
    reply["thread"]["turns"][0]["items"] = [{"type": kind, "status": "inProgress", "command": "PRIVATE_CANARY"}]
    peer.request.return_value = reply
    assert await client.read_execution_status(thread) == {
        "state": "busy", "pending_tools": True, "unowned_active_turn": False,
    }


@pytest.mark.parametrize("variant", [
    "wrong_thread", "no_status", "unknown_status", "malformed_status", "no_turns", "empty_turns",
    "duplicate_turn", "missing_turn_status", "bad_turn_status", "missing_items", "bad_item",
    "tool_missing_status", "tool_bad_status", "next_cursor", "has_more", "truncated", "unowned_terminal",
    "missing_owned_turn",
])
async def test_ambiguous_or_partial_native_metadata_is_unknown(native, variant):
    client, backend, peer, _, thread, _ = native
    reply = idle_reply()
    body, turn = reply["thread"], reply["thread"]["turns"][0]
    if variant == "wrong_thread": body["id"] = "another-native-thread"
    elif variant == "no_status": del body["status"]
    elif variant == "unknown_status": body["status"] = "notLoaded"
    elif variant == "malformed_status": body["status"] = {"type": []}
    elif variant == "no_turns": del body["turns"]
    elif variant == "empty_turns": body["turns"] = []
    elif variant == "duplicate_turn": body["turns"].append(deepcopy(turn))
    elif variant == "missing_turn_status": del turn["status"]
    elif variant == "bad_turn_status": turn["status"] = {}
    elif variant == "missing_items": del turn["items"]
    elif variant == "bad_item": turn["items"] = [None]
    elif variant == "tool_missing_status": turn["items"] = [{"type": "commandExecution"}]
    elif variant == "tool_bad_status": turn["items"] = [{"type": "commandExecution", "status": []}]
    elif variant == "next_cursor": reply["nextCursor"] = "another-page"
    elif variant == "has_more": body["hasMore"] = True
    elif variant == "truncated": body["truncated"] = True
    elif variant == "unowned_terminal": body["turns"].append({"id": "unowned", "status": "completed", "items": []})
    elif variant == "missing_owned_turn": backend._threads[thread]["turnIds"]["missing-native-turn"] = "owned-turn"
    peer.request.return_value = reply
    assert (await client.read_execution_status(thread))["state"] == "unknown"


@pytest.mark.parametrize("race", ["native_event", "record_replaced", "native_changed", "mapping_changed", "backend_closed", "native_unloaded", "host_closed", "host_replaced", "host_turn_changed", "terminal_inflight"])
async def test_probe_rechecks_native_and_host_identity_after_await(native, race):
    client, backend, peer, _, thread, turn = native
    async def racing_read(*args, **kwargs):
        if race == "native_event":
            await peer.notify_event("item/started", {"threadId": "native-thread-1", "turnId": "unowned-live", "item": {"id": "x"}})
            await drain(backend)  # Still detect activity after the queue drains.
        elif race == "record_replaced": backend._threads[thread] = deepcopy(backend._threads[thread])
        elif race == "native_changed": backend._threads[thread]["nativeId"] = "replacement"
        elif race == "mapping_changed": backend._threads[thread]["turnIds"]["new"] = "new-host-turn"
        elif race == "backend_closed": backend._threads[thread]["closed"] = True
        elif race == "native_unloaded": backend._loaded.discard(thread)
        elif race == "host_closed": client._threads[thread]["closed"] = True
        elif race == "host_replaced": client._threads[thread] = deepcopy(client._threads[thread])
        elif race == "host_turn_changed": client._threads[thread]["activeTurnId"] = "next"
        elif race == "terminal_inflight": client._terminal_turns_inflight.add((thread, turn))
        return idle_reply()
    peer.request.side_effect = racing_read
    result = await client.read_execution_status(thread)
    assert result["state"] == "unknown"
    assert result.get("activity_observed") is (True if race == "native_event" else None)


async def test_account_heartbeat_is_not_coder_activity(native):
    client, backend, peer, _, thread, _ = native
    async def read(*args, **kwargs):
        await peer.notify_event("account/rateLimits/updated", {"rateLimits": {}})
        await drain(backend)
        return idle_reply()
    peer.request.side_effect = read
    assert (await client.read_execution_status(thread))["state"] == "quiescent"


@pytest.mark.parametrize("error", [AppServerError("PRIVATE_CANARY"), TimeoutError("PRIVATE_CANARY")])
async def test_probe_failure_is_unknown_not_provider_failure(native, error):
    client, _, peer, _, thread, _ = native
    peer.request.side_effect = error
    assert await client.read_execution_status(thread) == {"state": "unknown"}


async def test_unknown_host_thread_never_reads_native(native):
    client, _, peer, _, _, _ = native
    assert await client.read_execution_status("other") == {"state": "unknown"}
    peer.request.assert_not_awaited()


async def test_owned_host_wait_is_busy_without_running_another_turn(native):
    client, _, peer, _, thread, turn = native
    waiting = asyncio.get_running_loop().create_future()
    client._host._active[(thread, "owned-wait")] = waiting
    client._host._active_turns[(thread, "owned-wait")] = turn
    try:
        assert await client.read_execution_status(thread) == {"state": "busy", "pending_tools": True}
        peer.request.assert_not_awaited()
        waiting.set_result(None)
        assert (await client.read_execution_status(thread))["state"] == "quiescent"
        assert [call.args[0] for call in peer.request.await_args_list] == ["thread/read"]
    finally:
        client._host._active.pop((thread, "owned-wait"), None)
        client._host._active_turns.pop((thread, "owned-wait"), None)


@pytest.fixture
def idle_controller(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    controller.coder = _FakeSteerCoder()
    controller.coder.active_turn_id = None
    controller._generation_has_coder_turn = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
    assert not controller._no_marker_idle_ready()
    clock[0] = 2400.0
    return controller, store, reviewer, clock


@pytest.mark.parametrize("result", [{"state": "busy"}, {"state": "unknown"}, {}, None, {"state": []},
                                   {"state": "unknown", "activity_observed": True}])
async def test_idle_gate_defers_without_review_or_steering(idle_controller, result):
    controller, _, reviewer, _ = idle_controller
    controller.client.read_execution_status.return_value = result
    controller._deliver_coder_message = AsyncMock()
    await controller._handle_no_marker_idle(subagents_refreshed=True)
    assert reviewer.completion_packets == []
    assert controller._supervisor_task is None
    if isinstance(result, dict) and (result.get("state") == "busy" or result.get("activity_observed") is True):
        assert controller._no_marker_idle_since is None
    else:
        assert controller._no_marker_idle_since == 0.0
    controller._deliver_coder_message.assert_not_awaited()


async def test_missing_status_capability_fails_closed(idle_controller):
    controller, _, _, _ = idle_controller
    controller.client = SimpleNamespace()
    assert not await controller._confirm_coder_quiescent()
    assert controller._no_marker_idle_since == 0.0


@pytest.mark.parametrize("race", ["generation", "thread", "coder", "client", "active", "pause", "queued_event"])
async def test_controller_rechecks_lifecycle_after_probe(idle_controller, race):
    controller, store, _, _ = idle_controller
    async def read(*args, **kwargs):
        if race == "generation": store.update_bello_config(lambda cfg: cfg.model_copy(update={"generation": cfg.generation + 1}))
        elif race == "thread": store.update_bello_config(lambda cfg: cfg.model_copy(update={"coder_thread_id": "replacement"}))
        elif race == "coder": controller.coder = _FakeSteerCoder()
        elif race == "client": controller.client = SimpleNamespace()
        elif race == "active": controller.coder.active_turn_id = "new-turn"
        elif race == "pause": controller.paused = True
        elif race == "queued_event": controller.event_queue.put_nowait(object())
        return {"state": "quiescent"}
    controller.client.read_execution_status.side_effect = read
    assert not await controller._confirm_coder_quiescent()
    assert controller._no_marker_idle_since == (0.0 if race == "queued_event" else None)


async def test_quiescent_native_preserves_40_minute_idle_policy(idle_controller):
    controller, _, _, _ = idle_controller
    assert await controller._confirm_coder_quiescent()
    assert controller._no_marker_idle_ready()
    controller.client.read_execution_status.assert_awaited_once_with("thread", timeout=10.0)


async def test_non_codex_lifecycle_unchanged(idle_controller):
    controller, _, _, _ = idle_controller
    controller._active_coder_model = lambda: "claude-code/claude-sonnet-4-5"
    assert await controller._confirm_coder_quiescent()
    controller.client.read_execution_status.assert_not_awaited()


@pytest.mark.parametrize("probe_results", [
    [{"state": "busy"}],
    [{"state": "quiescent"}, {"state": "busy"}],
    [{"state": "quiescent"}, {"state": "unknown"}],
])
async def test_idle_review_rechecks_native_status_before_snapshot_and_dispatch(idle_controller, probe_results):
    controller, _, reviewer, _ = idle_controller
    controller.client.read_execution_status.side_effect = probe_results
    await controller._run_supervisor_check(NO_MARKER_IDLE_REVIEW_SUMMARY, None, None, None, None, True)
    assert reviewer.completion_packets == []
    assert controller.client.read_execution_status.await_count == len(probe_results)
    assert controller._no_marker_idle_since == (None if any(result.get("state") == "busy" for result in probe_results) else 0.0)


async def test_busy_probe_requires_a_new_full_idle_window(idle_controller):
    controller, _, reviewer, clock = idle_controller
    controller.client.read_execution_status.return_value = {"state": "busy"}
    await controller._handle_no_marker_idle(subagents_refreshed=True)
    clock[0] = 2500
    controller.client.read_execution_status.return_value = {"state": "quiescent"}
    await controller._handle_no_marker_idle(subagents_refreshed=True)
    assert controller._no_marker_idle_since == 2500
    clock[0] = 4899
    await controller._handle_no_marker_idle(subagents_refreshed=True)
    assert reviewer.completion_packets == []
    clock[0] = 4900
    await controller._handle_no_marker_idle(subagents_refreshed=True)
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


@pytest.mark.parametrize("queued", [False, True])
async def test_native_deprecation_notice_during_probe_is_not_execution(native, queued):
    client, backend, peer, _, thread, _ = native
    async def read(*args, **kwargs):
        if queued:
            backend._queue.put_nowait({"method": "deprecationNotice", "params": {"message": "PRIVATE_CANARY"}})
        else:
            await peer.notify_event("deprecationNotice", {"message": "PRIVATE_CANARY"})
            await drain(backend)
        return idle_reply()
    peer.request.side_effect = read
    assert await client.read_execution_status(thread) == {
        "state": "quiescent", "pending_tools": False, "unowned_active_turn": False,
    }
    assert peer.request.await_count == 1


@pytest.mark.parametrize("params", [{"threadId": "native-thread-1"}, {"turnId": "turn"}, {"item": {"id": "item"}}])
async def test_threaded_or_action_bearing_notice_remains_a_probe_blocker(native, params):
    client, backend, peer, _, thread, _ = native
    async def read(*args, **kwargs):
        backend._queue.put_nowait({"method": "deprecationNotice", "params": params})
        return idle_reply()
    peer.request.side_effect = read
    assert (await client.read_execution_status(thread))["state"] == "unknown"


@pytest.mark.parametrize("queued", [False, True])
async def test_controller_deprecation_during_probe_preserves_mature_idle_window(idle_controller, queued):
    from supervisor.appserver import AppServerMessage
    from supervisor.controller import ControllerEvent
    controller, store, _, _ = idle_controller
    before_sequence = store.get_bello_config().last_event_sequence
    async def read(*args, **kwargs):
        notice = AppServerMessage({"method": "deprecationNotice", "params": {"message": "PRIVATE_CANARY"}})
        if queued:
            controller.event_queue.put_nowait(ControllerEvent(kind="notification", message=notice))
        else:
            await controller.handle_notification(notice)
        return {"state": "quiescent"}
    controller.client.read_execution_status.side_effect = read
    assert await controller._confirm_coder_quiescent()
    assert controller._no_marker_idle_since == 0.0
    assert controller._no_marker_idle_ready()
    if not queued:
        assert store.get_bello_config().last_event_sequence > before_sequence


@pytest.mark.parametrize("result", [{"state": "unknown"}, None, {}, {"state": []}])
async def test_unknown_probe_retries_next_guard_tick_without_another_40_minutes(idle_controller, result):
    # Goal continues successful turns itself and never uses the idle window, so
    # the retry contract belongs to the ordinary automatic completion review.
    controller, store, reviewer, clock = idle_controller
    controller._deliver_coder_message = AsyncMock(return_value=(True, None))
    controller.client.read_execution_status.return_value = result
    await controller._handle_controller_idle_guard()
    controller._deliver_coder_message.assert_not_awaited()
    assert controller._supervisor_task is None
    assert controller._no_marker_idle_since == 0.0
    assert controller.client.read_execution_status.await_count == 1
    clock[0] += 60
    controller.client.read_execution_status.return_value = {"state": "quiescent"}
    await controller._handle_controller_idle_guard()
    assert controller.client.read_execution_status.await_count == 2
    controller._deliver_coder_message.assert_not_awaited()
    await controller._supervisor_task
    assert len(reviewer.completion_packets) == 1


async def test_goal_idle_guard_never_probes_or_reviews(idle_controller):
    controller, store, reviewer, clock = idle_controller
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True}))
    controller._deliver_coder_message = AsyncMock(return_value=(True, None))
    controller.client.read_execution_status.return_value = {"state": "quiescent"}
    for _ in range(3):
        await controller._handle_controller_idle_guard()
        clock[0] += 60
    controller._deliver_coder_message.assert_not_awaited()
    controller.client.read_execution_status.assert_not_awaited()
    assert controller._supervisor_task is None and reviewer.completion_packets == []


@pytest.mark.parametrize("activity", ["message", "turn", "approval", "native_tool"])
async def test_real_activity_during_probe_still_restarts_idle_countdown(idle_controller, activity):
    from supervisor.appserver import AppServerMessage
    controller, _, _, clock = idle_controller
    clock[0] = 2500.0
    async def read(*args, **kwargs):
        if activity == "message":
            await controller.handle_notification(AppServerMessage({"method": "item/completed", "params": {
                "threadId": "thread", "turnId": "turn", "item": {"id": "m", "type": "agentMessage", "text": "Progress."},
            }}))
        elif activity == "turn":
            controller.coder.active_turn_id = "next-turn"
        elif activity == "approval":
            controller.pending_approvals["approval"] = object()
        elif activity == "native_tool":
            return {"state": "unknown", "activity_observed": True}
        return {"state": "quiescent"}
    controller.client.read_execution_status.side_effect = read
    controller._deliver_coder_message = AsyncMock()
    assert not await controller._confirm_coder_quiescent()
    assert controller._no_marker_idle_since == (2500.0 if activity == "message" else None)
    assert not controller._no_marker_idle_ready()
    controller._deliver_coder_message.assert_not_awaited()


@pytest.mark.parametrize("event", [
    {"method": "deprecationNotice", "id": 1, "params": {}},
    {"method": "deprecationNotice", "threadId": "thread", "params": {}},
    {"method": "deprecationNotice", "params": {"threadId": "thread"}},
    {"method": "error", "params": {}},
    {"method": "turn/started", "params": {}},
    {"method": "item/started", "params": {}},
    {"method": "item/commandExecution/requestApproval", "id": 1, "params": {}},
])
def test_only_unthreaded_deprecation_notices_are_ignored_in_pending_queue(event):
    from supervisor.quiescence import execution_queue_clear
    queue = asyncio.Queue()
    queue.put_nowait({"method": "deprecationNotice", "params": {}})
    assert execution_queue_clear(queue)
    assert queue.qsize() == 1  # Inspection does not consume or discard audit events.
    queue.put_nowait(event)
    assert not execution_queue_clear(queue)
    assert queue.qsize() == 2


def test_unknown_queue_implementation_and_large_notice_queue_fail_closed():
    from supervisor.quiescence import execution_queue_clear
    assert not execution_queue_clear(object())
    assert not execution_queue_clear(SimpleNamespace(empty=lambda: False))
    queue = asyncio.Queue()
    for _ in range(257):
        queue.put_nowait({"method": "deprecationNotice", "params": {}})
    assert not execution_queue_clear(queue)
    assert queue.qsize() == 257
