"""Offline regressions for errors and a native turn whose completion was lost."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import supervisor.controller as module
from supervisor.appserver import AppServerError, AppServerMessage
from supervisor.schemas import BelloStatus
from supervisor.state import EVENTS
from tests.test_bello_state import _runtime_controller


@pytest.fixture
def active(tmp_path, monkeypatch):
    controller, store, _ = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "status": BelloStatus.RUNNING, "active_coder_turn_id": "turn",
    }))
    controller.coder = SimpleNamespace(thread_id="thread", active_turn_id="turn", model="gpt-5.6-luna")
    controller.coder_model = "gpt-5.6-luna"
    controller.client = SimpleNamespace(
        thread_read=AsyncMock(return_value={"thread": {"id": "thread", "turns": [
            {"id": "turn", "status": "inProgress"},
        ]}}),
        reconcile_terminal_turn=AsyncMock(return_value=True),
        turn_start=AsyncMock(), turn_steer=AsyncMock(),
    )
    controller.fail_provider = AsyncMock()
    controller._track_subagent_notification = AsyncMock()
    controller._write_run_checkpoint = Mock()
    controller._handle_coder_turn_completed = AsyncMock()
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    controller._active_coder_watch()
    return controller, store, clock


def notification(method, **extra):
    return AppServerMessage({"method": method, "params": {
        "threadId": "thread", "turnId": "turn", **extra,
    }})


async def error(controller, retry=True, **extra):
    await controller.handle_notification(notification("error", willRetry=retry, error={
        "message": "stream disconnected https://service/path?token=private-secret",
        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 503}},
        "additionalDetails": "connection reset",
    }, **extra))


@pytest.mark.parametrize("retry", [True, False, None, "false", 0])
async def test_error_is_persisted_and_only_explicit_false_is_terminal(active, retry):
    c, store, _ = active
    await error(c, retry)
    logged = store.path(EVENTS).read_text()
    assert "private-secret" not in logged
    assert json.loads(logged.splitlines()[-1])["payload"]["error"]["codexErrorInfo"]["httpConnectionFailed"]["httpStatusCode"] == 503
    assert "connection reset" in logged
    assert "willRetry" in logged
    assert c.fail_provider.await_count == (1 if retry is False else 0)
    assert store.get_bello_config().active_coder_turn_id == "turn"
    assert c.client.turn_start.await_count == c.client.turn_steer.await_count == 0


async def test_absent_retry_is_not_terminal(active):
    c, _, _ = active
    await c.handle_notification(notification("error", error={"message": "unknown"}))
    c.fail_provider.assert_not_awaited()
    assert c._coder_watch.retry_since is None


@pytest.mark.parametrize("change", ["thread", "turn", "missing_turn", "paused", "terminal"])
async def test_foreign_or_stale_error_cannot_stop_current_coder(active, change):
    c, _, _ = active
    extra = {}
    if change == "thread":
        extra["threadId"] = "reviewer"
    elif change == "turn":
        extra["turnId"] = "old"
    elif change == "missing_turn":
        extra["turnId"] = None
    elif change == "paused":
        c.paused = True
    else:
        c._terminal_cleanup_started = True
    await error(c, False, **extra)
    c.fail_provider.assert_not_awaited()


async def test_silence_probes_once_per_interval_but_does_not_fail_or_replay(active):
    c, store, clock = active
    clock.now = 299
    await c._handle_active_coder_guard()
    c.client.thread_read.assert_not_awaited()
    clock.now = 301
    await c._handle_active_coder_guard()
    await c._handle_active_coder_guard()
    c.client.thread_read.assert_awaited_once_with("thread", include_turns=True, timeout=10.0)
    clock.now = 3601
    await c._handle_active_coder_guard()
    c.fail_provider.assert_not_awaited()
    assert sum(json.loads(line)["event_type"] == "coder/progressStalled"
               for line in store.path(EVENTS).read_text().splitlines()) == 1
    c.client.turn_start.assert_not_awaited()
    c.client.turn_steer.assert_not_awaited()


async def test_retry_budget_survives_quota_status_and_repeated_retry(active):
    c, _, clock = active
    await error(c)
    clock.now = 200
    await error(c)
    await c.handle_notification(notification("thread/tokenUsage/updated", tokenUsage={"total": 123}))
    c._mark_controller_activity()
    assert c._coder_watch.retry_since == 0
    clock.now = 301
    await c._handle_active_coder_guard()
    c.fail_provider.assert_awaited_once()
    assert "retry budget exceeded" in c.fail_provider.call_args.args[0]
    assert "private-secret" not in c.fail_provider.call_args.args[0]
    c.client.turn_start.assert_not_awaited()


@pytest.mark.parametrize("method,extra", [
    ("item/agentMessage/delta", {"delta": "working"}),
    ("item/reasoning/textDelta", {"delta": "considering"}),
    ("item/tool/argumentsDelta", {"delta": "large patch"}),
    ("item/commandExecution/outputDelta", {"delta": "progress"}),
    ("item/completed", {"item": {"type": "agentMessage", "id": "message", "text": "working"}}),
])
async def test_real_progress_resets_retry_budget(active, method, extra):
    c, _, clock = active
    await error(c)
    clock.now = 250
    await c.handle_notification(notification(method, **extra))
    assert c._coder_watch.retry_since is None
    clock.now = 551
    await c._handle_active_coder_guard()
    c.fail_provider.assert_not_awaited()


@pytest.mark.parametrize("waiting", ["commandExecution", "mcpToolCall", "dynamicToolCall", "webSearch", "fileChange", "fileRead", "approval", "subagent"])
async def test_live_work_is_not_a_retry_timeout(active, waiting):
    c, _, clock = active
    await error(c)
    if waiting not in {"approval", "subagent"}:
        await c.handle_notification(notification("item/started", item={
            "type": waiting, "id": "tool", "command": "pytest", "status": "inProgress",
        }))
        assert c._coder_watch.running_tools == {"tool"}
    elif waiting == "approval":
        c.pending_approvals[1] = object()
    else:
        c._active_coder_subagents = Mock(return_value=[object()])
    clock.now = 10000
    await c._handle_active_coder_guard()
    c.fail_provider.assert_not_awaited()
    c.client.thread_read.assert_awaited_once()  # Read-only reconciliation is still allowed.


@pytest.mark.parametrize("change", ["paused", "generation", "turn", "coder", "progress", "queued_event"])
async def test_probe_result_is_ignored_after_concurrent_change(active, change):
    c, store, clock = active
    await error(c)

    async def probe(*args, **kwargs):
        if change == "paused":
            c.paused = True
        elif change == "generation":
            store.update_bello_config(lambda cfg: cfg.model_copy(update={"generation": cfg.generation + 1}))
        elif change == "turn":
            store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": "new"}))
        elif change == "coder":
            c.coder = SimpleNamespace(thread_id="thread", active_turn_id="turn")
        elif change == "progress":
            c._coder_watch.last_progress = clock.now
        else:
            c.event_queue.put_nowait(object())
        return {"thread": {"id": "thread", "turns": [{"id": "turn", "status": "completed"}]}}

    c.client.thread_read.side_effect = probe
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.reconcile_terminal_turn.assert_not_awaited()
    c.fail_provider.assert_not_awaited()


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_exact_terminal_turn_is_reconciled_without_task_replay(active, status):
    c, _, clock = active
    terminal = {"id": "turn", "status": status, "error": {"message": "upstream failed"}}
    c.client.thread_read.return_value = {"thread": {"id": "thread", "turns": [terminal]}}
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.reconcile_terminal_turn.assert_awaited_once_with("thread", "turn", terminal)
    c.client.turn_start.assert_not_awaited()


@pytest.mark.parametrize("thread", [
    {"id": "other", "turns": [{"id": "turn", "status": "completed"}]},
    {"id": "thread", "turns": [{"id": "old", "status": "completed"}]},
    {"id": "thread", "status": {"type": "idle"}, "turns": []},
])
async def test_missing_or_foreign_turn_is_not_assumed_complete(active, thread):
    c, _, clock = active
    c.client.thread_read.return_value = {"thread": thread}
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.reconcile_terminal_turn.assert_not_awaited()
    c.fail_provider.assert_not_awaited()


async def test_probe_timeout_is_bounded_and_is_not_success(active, monkeypatch):
    c, _, clock = active

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    c.client.thread_read.side_effect = hang
    monkeypatch.setattr(module, "CODER_STATUS_PROBE_TIMEOUT_SECONDS", 0.001)
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.reconcile_terminal_turn.assert_not_awaited()
    c.fail_provider.assert_not_awaited()


async def test_reconciliation_failure_is_not_mislabeled_retry_exhaustion(active):
    c, _, clock = active
    await error(c)
    c.client.thread_read.return_value = {"thread": {"id": "thread", "turns": [{
        "id": "turn", "status": "completed",
    }]}}
    c.client.reconcile_terminal_turn.side_effect = AppServerError("local cleanup failure")
    clock.now = 301
    await c._handle_active_coder_guard()
    c.fail_provider.assert_not_awaited()


async def test_duplicate_terminal_notification_does_not_repeat_review(active):
    c, store, _ = active

    def mark_complete(turn):
        store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
        c.coder.active_turn_id = None

    c.coder.mark_turn_completed = mark_complete
    msg = notification("turn/completed", turn={"id": "turn", "status": "completed"})
    await c.handle_notification(msg)
    await c.handle_notification(msg)
    c._handle_coder_turn_completed.assert_awaited_once()


async def test_non_native_engine_does_not_use_codex_watchdog(active):
    c, _, clock = active
    c.coder_model = "claude-code/claude-sonnet-4-6"
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.thread_read.assert_not_awaited()


async def test_active_revision_model_selects_engine_not_original_coder(active):
    c, _, clock = active
    c._active_coder_model = Mock(return_value="claude-code/claude-sonnet-4-6")
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.thread_read.assert_not_awaited()


async def test_terminal_probe_recovers_missing_final_message(active):
    c, _, clock = active
    c.client.thread_read.return_value = {"thread": {"id": "thread", "turns": [{
        "id": "turn", "status": "completed", "items": [{
            "type": "agentMessage", "id": "final", "text": "Done\nBELLO_READY_FOR_REVIEW",
        }],
    }]}}
    clock.now = 301
    await c._handle_active_coder_guard()
    assert c.last_coder_message.text == "Done\nBELLO_READY_FOR_REVIEW"
    c.client.turn_start.assert_not_awaited()


async def test_lost_command_completion_does_not_block_terminal_probe(active):
    c, _, clock = active
    c._coder_watch.running_tools.add("lost-command")
    c.client.thread_read.return_value = {"thread": {"id": "thread", "turns": [{
        "id": "turn", "status": "completed",
    }]}}
    clock.now = 301
    await c._handle_active_coder_guard()
    c.client.reconcile_terminal_turn.assert_awaited_once()


async def test_tool_completion_clears_running_tool_tracking(active):
    c, store, _ = active
    c._coder_watch.running_tools.add("mcp")
    c._record_coder_progress("item/completed", {
        "item": {"id": "mcp", "type": "mcpToolCall", "status": "completed"},
    }, store.get_bello_config())
    assert not c._coder_watch.running_tools


async def test_repeated_empty_command_placeholder_is_not_running_work(active):
    c, _, clock = active
    await error(c)
    clock.now = 250
    for _ in range(2):
        await c.handle_notification(notification("item/started", item={
            "type": "commandExecution", "id": "partial", "command": "", "status": "inProgress",
        }))
    assert not c._coder_watch.running_tools
    assert c._coder_watch.retry_since == 0
    clock.now = 301
    await c._handle_active_coder_guard()
    c.fail_provider.assert_awaited_once()
