from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.codex import CodexBackend
from tests.test_runtime_codex_backend import FakeNative, drain


async def active_runtime(tmp_path):
    project, workspace = tmp_path / "project", tmp_path / "snapshot"
    project.mkdir()
    workspace.mkdir()
    notifications, natives = [], []

    async def notify(message):
        notifications.append(message)

    def factory(**options):
        native = FakeNative(**options)
        natives.append(native)
        return native

    client = RuntimeClient(cwd=project, notification_handler=notify)
    await client.start()
    backend = CodexBackend(state_dir=client.state_dir / "codex", client_factory=factory,
                           emit=lambda raw: client._emit(raw, engine="codex"))
    client._engines["codex"] = backend
    reply = await client.thread_start({"cwd": str(workspace), "runtimeWorkspaceRoots": [str(workspace)],
        "model": "gpt-6-astra", "sandbox": "workspace-write", "approvalPolicy": "on-request",
        "effort": "xhigh"})
    thread_id = reply["thread"]["id"]
    turn_id = (await client.turn_start({"threadId": thread_id, "input": "Test"}))["turn"]["id"]
    await drain(backend)
    notifications.clear()
    return client, backend, natives[0], notifications, thread_id, turn_id


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_reconcile_terminal_updates_both_layers_without_requests(tmp_path, status):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        terminal = {"id": turn_id, "status": status, "items": [], "error": None}
        original = deepcopy(terminal)
        client._host.finish_turn = AsyncMock()
        calls = deepcopy(native.calls)
        assert await client.reconcile_terminal_turn(thread, turn_id, terminal)
        assert native.calls == calls
        assert terminal == original
        assert backend._threads[thread].get("activeTurnId") is None
        assert client._threads[thread].get("activeTurnId") is None
        assert client._threads[thread]["lastTurnId"] == turn_id
        assert client._threads[thread]["lastTurnStatus"] == status
        assert backend._journal.threads()[thread].get("activeTurnId") is None
        assert client._journal.threads()[thread]["lastTurnStatus"] == status
        client._host.finish_turn.assert_awaited_once_with(thread, turn_id)
        assert len(notifications) == 1
        assert notifications[0].method == "turn/completed"
        assert notifications[0].params == {"threadId": thread, "turn": terminal}
        assert not await client.reconcile_terminal_turn(thread, turn_id, terminal)
        await native.notify_event("turn/completed", {"threadId": "native-thread-1",
            "turn": {"id": "native-turn-1", "status": status}})
        await drain(backend)
        assert len(notifications) == 1
        client._host.finish_turn.assert_awaited_once()
    finally:
        await client.stop()


@pytest.mark.parametrize("status", ["inProgress", "unknown", None, {}, []])
async def test_reconcile_nonterminal_is_noop(tmp_path, status):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        calls = deepcopy(native.calls)
        turn = {"id": turn_id, "status": status}
        assert not await client.reconcile_terminal_turn(thread, turn_id, turn)
        assert not backend.reconcile_terminal_turn(thread, turn_id, turn)
        assert native.calls == calls and not notifications
        assert backend._threads[thread]["activeTurnId"] == turn_id
        assert client._threads[thread]["activeTurnId"] == turn_id
    finally:
        await client.stop()


@pytest.mark.parametrize("layer", ["host", "backend"])
async def test_reconcile_stale_turn_cannot_change_new_active(tmp_path, layer):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        record = (client if layer == "host" else backend)._threads[thread]
        record["activeTurnId"] = "new-turn"
        before_host, before_backend = deepcopy(client._threads), deepcopy(backend._threads)
        calls = deepcopy(native.calls)
        assert not await client.reconcile_terminal_turn(thread, turn_id,
            {"id": turn_id, "status": "completed"})
        assert client._threads == before_host and backend._threads == before_backend
        assert native.calls == calls and not notifications
    finally:
        await client.stop()


@pytest.mark.parametrize("change", ["wrong_id", "unknown_thread", "wrong_engine", "closed", "stopping", "no_backend", "unmapped"])
async def test_reconcile_rejects_unowned_or_inactive_context(tmp_path, change):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        terminal = {"id": turn_id, "status": "completed"}
        requested_thread = thread
        if change == "wrong_id":
            terminal["id"] = "native-turn-1"
        elif change == "unknown_thread":
            requested_thread = "not-owned"
        elif change == "wrong_engine":
            client._threads[thread]["engine"] = "pi"
        elif change == "closed":
            client._threads[thread]["closed"] = True
        elif change == "stopping":
            client._closing = True
        elif change == "no_backend":
            client._engines.pop("codex")
        elif change == "unmapped":
            backend._threads[thread]["turnIds"] = {}
        before_host, before_backend = deepcopy(client._threads), deepcopy(backend._threads)
        calls = deepcopy(native.calls)
        assert not await client.reconcile_terminal_turn(requested_thread, turn_id, terminal)
        assert client._threads == before_host and backend._threads == before_backend
        assert native.calls == calls and not notifications
    finally:
        client._closing = False
        client._threads[thread]["engine"] = "codex"
        client._engines["codex"] = backend
        await client.stop()


async def test_thread_read_maps_terminal_identity_but_does_not_reconcile_globally(tmp_path):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        calls = len(native.calls)
        response = await client.thread_read(thread, include_turns=True, timeout=2)
        assert native.calls[calls:] == [("thread/read", {"threadId": "native-thread-1", "includeTurns": True})]
        assert response["thread"]["id"] == thread
        terminal = response["thread"]["turns"][0]
        assert terminal == {"id": turn_id, "status": "completed"}
        assert client._threads[thread]["activeTurnId"] == turn_id
        assert backend._threads[thread]["activeTurnId"] == turn_id
        assert not notifications
        assert await client.reconcile_terminal_turn(thread, turn_id, terminal)
    finally:
        await client.stop()


async def test_concurrent_reconcile_and_native_completion_emit_once(tmp_path):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def finish(*_args):
        started.set()
        await release.wait()

    client._host.finish_turn = AsyncMock(side_effect=finish)
    try:
        terminal = {"id": turn_id, "status": "completed"}
        first = asyncio.create_task(client.reconcile_terminal_turn(thread, turn_id, terminal))
        await started.wait()
        assert not await client.reconcile_terminal_turn(thread, turn_id, terminal)
        await native.notify_event("turn/completed", {"threadId": "native-thread-1",
            "turn": {"id": "native-turn-1", "status": "completed"}})
        await drain(backend)
        assert not notifications
        assert backend._threads[thread].get("activeTurnId") is None
        assert client._threads[thread]["activeTurnId"] == turn_id
        release.set()
        assert await first
        assert len(notifications) == 1
        client._host.finish_turn.assert_awaited_once_with(thread, turn_id)
    finally:
        release.set()
        await client.stop()


async def test_turn_replacement_during_cleanup_drops_old_completion(tmp_path):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    try:
        async def finish(*_args):
            # Simulate interruption and replacement while cleanup yields.
            client._threads[thread]["activeTurnId"] = "new-turn"
            backend._threads[thread]["activeTurnId"] = "new-turn"
            await asyncio.sleep(0)

        client._host.finish_turn = AsyncMock(side_effect=finish)
        assert not await client.reconcile_terminal_turn(thread, turn_id,
            {"id": turn_id, "status": "completed"})
        assert client._threads[thread]["activeTurnId"] == "new-turn"
        assert backend._threads[thread]["activeTurnId"] == "new-turn"
        assert client._threads[thread].get("lastTurnId") is None
        assert not notifications
    finally:
        await client.stop()


async def test_cancelled_cleanup_can_reconcile_same_terminal_again(tmp_path):
    client, backend, native, notifications, thread, turn_id = await active_runtime(tmp_path)
    entered = asyncio.Event()

    async def blocked_finish(*_args):
        entered.set()
        await asyncio.Event().wait()

    client._host.finish_turn = AsyncMock(side_effect=blocked_finish)
    terminal = {"id": turn_id, "status": "completed"}
    try:
        operation = asyncio.create_task(client.reconcile_terminal_turn(thread, turn_id, terminal))
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert not notifications
        assert client._threads[thread]["activeTurnId"] == turn_id
        assert backend._threads[thread]["activeTurnId"] == turn_id
        client._host.finish_turn = AsyncMock()
        calls = deepcopy(native.calls)
        assert await client.reconcile_terminal_turn(thread, turn_id, terminal)
        assert native.calls == calls
        assert client._threads[thread].get("activeTurnId") is None
        assert backend._threads[thread].get("activeTurnId") is None
        assert len(notifications) == 1
    finally:
        await client.stop()


async def test_late_old_reconciliation_does_not_touch_new_turn(tmp_path):
    client, backend, native, notifications, thread, old_turn = await active_runtime(tmp_path)
    try:
        terminal = {"id": old_turn, "status": "completed"}
        assert await client.reconcile_terminal_turn(thread, old_turn, terminal)
        current = (await client.turn_start({"threadId": thread, "input": "Next"}))["turn"]["id"]
        await drain(backend)
        notifications.clear()
        calls = deepcopy(native.calls)
        assert not await client.reconcile_terminal_turn(thread, old_turn, terminal)
        assert not backend.reconcile_terminal_turn(thread, old_turn, terminal)
        await native.notify_event("turn/completed", {"threadId": "native-thread-1",
            "turn": {"id": "native-turn-1", "status": "failed"}})
        await drain(backend)
        assert client._threads[thread]["activeTurnId"] == current
        assert backend._threads[thread]["activeTurnId"] == current
        assert client._threads[thread]["lastTurnStatus"] == "completed"
        assert native.calls == calls and not notifications
    finally:
        await client.stop()
