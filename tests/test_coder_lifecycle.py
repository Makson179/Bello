from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.appserver import APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS, AppServerClient, AppServerTimeoutError
from supervisor.coder import CoderSession
from supervisor.state import StateStore


async def test_coder_interrupt_uses_cleanup_deadline_not_coder_deadline(tmp_path: Path) -> None:
    calls = []

    class Client:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            calls.append((thread_id, turn_id, timeout))

    coder = CoderSession(Client(), StateStore(tmp_path), tmp_path, tmp_path / "TASK.md",
                         thread_id="thread", active_turn_id="turn", coder_rpc_timeout_seconds=3600)
    await coder.interrupt()
    assert calls == [("thread", "turn", APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS)]
    assert APP_SERVER_CLEANUP_RPC_TIMEOUT_SECONDS == 10.0
    assert coder.active_turn_id == "turn"  # An interrupt ACK is not a terminal event.


async def test_hung_interrupt_times_out_without_replaying_or_clearing_turn(tmp_path: Path) -> None:
    writes = []

    class Stdin:
        def write(self, data):
            writes.append(json.loads(data))

        async def drain(self):
            return None

    client = AppServerClient()
    client.process = SimpleNamespace(stdin=Stdin())
    coder = CoderSession(client, StateStore(tmp_path), tmp_path, tmp_path / "TASK.md",
                         thread_id="thread", active_turn_id="turn", coder_rpc_timeout_seconds=3600,
                         cleanup_rpc_timeout_seconds=0.01)
    with pytest.raises(AppServerTimeoutError, match="turn/interrupt response timed out after 0.01s"):
        await asyncio.wait_for(coder.interrupt(), timeout=0.5)
    assert len(writes) == 1
    assert writes[0]["method"] == "turn/interrupt"
    assert writes[0]["params"] == {"threadId": "thread", "turnId": "turn"}
    assert not client._pending
    assert coder.active_turn_id == "turn"


async def test_inactive_coder_interrupt_does_not_issue_rpc(tmp_path: Path) -> None:
    coder = CoderSession(object(), StateStore(tmp_path), tmp_path, tmp_path / "TASK.md",
                         thread_id="thread")
    await coder.interrupt()
