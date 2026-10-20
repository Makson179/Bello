"""Schema preflight must not inspect or grant access to a user's task."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor.controller import BelloController
from supervisor.schemas import SupervisorDecision
from supervisor.schemas.models import openai_strict_json_schema_for_supervisor_decision
from supervisor.supervisor_agent import SupervisorAgentError


NEUTRAL_TASK = "Structured output self-test. Return noop."


class NeutralClient:
    def __init__(self, *, text: str | None = None, error: Exception | None = None, block: bool = False):
        self.text = text if text is not None else SupervisorDecision(
            decision="noop", reason="neutral transport verified"
        ).model_dump_json()
        self.error = error
        self.block = block
        self.started = asyncio.Event()
        self.thread_params: dict | None = None
        self.turn_params: list[dict] = []
        self.archived: list[str] = []
        self.root: Path | None = None

    async def thread_start(self, params: dict, *, timeout: float) -> dict:
        self.thread_params = params
        self.root = Path(params["cwd"])
        assert self.root.is_dir()
        assert self.root.name.startswith("bello-runtime-self-test-")
        assert {path.name for path in self.root.iterdir()} == {"TASK.md"}
        assert (self.root / "TASK.md").read_text(encoding="utf-8") == NEUTRAL_TASK
        assert params["runtimeWorkspaceRoots"] == [str(self.root)]
        assert params["belloRole"] == "runtime"
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"
        assert params["model"] == "neutral/model"
        assert params["effort"] == "off"
        assert params["serviceTier"] == "priority"
        return {"thread": {"id": "neutral-preflight-thread"}}

    async def turn_start(self, params: dict, *, timeout: float) -> dict:
        self.turn_params.append(params)
        assert self.root is not None and self.root.exists()
        assert params["cwd"] == str(self.root)
        assert params["runtimeWorkspaceRoots"] == [str(self.root)]
        assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
        assert params["approvalPolicy"] == "never"
        assert params["model"] == "neutral/model"
        assert params["effort"] == "off"
        assert params["serviceTier"] == "priority"
        assert params["outputSchema"] == openai_strict_json_schema_for_supervisor_decision()
        if len(self.turn_params) == 1:
            packet = json.loads(params["input"][0]["text"])
            assert packet["task_contents"] == NEUTRAL_TASK
            assert packet["task_path"] == str(self.root / "TASK.md")
            assert packet["latest_event_sequence"] == 41
            assert packet["generation"] == 3
            assert packet["restart_count"] == 2
            assert packet.get("health", {}) == {}
            assert packet.get("progress", "") == packet.get("decisions", "") == ""
            assert not packet.get("progress_path") and not packet.get("decisions_path")
        self.started.set()
        if self.block:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return {"turn": {"id": f"neutral-turn-{len(self.turn_params)}", "status": "completed", "items": [
            {"type": "agentMessage", "text": self.text},
        ]}}

    async def thread_archive(self, thread_id: str, *, timeout: float) -> dict:
        assert self.root is not None and self.root.exists()
        assert (self.root / "TASK.md").read_text(encoding="utf-8") == NEUTRAL_TASK
        self.archived.append(thread_id)
        return {}


def controller_fixture(tmp_path: Path, client: NeutralClient) -> BelloController:
    controller = object.__new__(BelloController)
    controller.client = client
    controller.task_path = tmp_path / "must-not-read-user-task.md"
    forbidden = Mock(side_effect=AssertionError("preflight accessed real task or workspace"))
    controller._canonical_task_text = forbidden
    controller._active_workspace_root = forbidden
    controller._runtime_enabled = lambda: True
    controller._runtime_model = lambda: "neutral/model"
    controller._runtime_intelligence = lambda: "off"
    controller._fast_mode = lambda: True
    controller.store = SimpleNamespace(
        get_bello_config=lambda: SimpleNamespace(last_event_sequence=41, generation=3, restart_count=2),
        get_health=forbidden,
        append_supervisor_wake=Mock(),
        append_raw_log=Mock(),
    )
    return controller


def assert_cleaned(controller: BelloController, client: NeutralClient) -> None:
    assert client.archived == ["neutral-preflight-thread"]
    assert client.root is not None and not client.root.exists()
    controller._canonical_task_text.assert_not_called()
    controller._active_workspace_root.assert_not_called()
    controller.store.get_health.assert_not_called()
    controller.store.append_supervisor_wake.assert_called()
    packet = controller.store.append_supervisor_wake.call_args.args[0]["packet"]
    assert packet["task_contents"] == NEUTRAL_TASK
    assert packet["task_path"] != str(controller.task_path)
    assert packet["generation"] == 3
    assert packet["latest_event_sequence"] == 41


@pytest.mark.parametrize("decision", ["noop", "pause"])
async def test_self_test_has_only_neutral_authority_and_retains_accepted_outcomes(tmp_path: Path, decision: str) -> None:
    client = NeutralClient(text=SupervisorDecision(decision=decision, reason="neutral result").model_dump_json())
    controller = controller_fixture(tmp_path, client)
    await controller._structured_output_self_test()
    assert len(client.turn_params) == 1
    assert_cleaned(controller, client)


async def test_self_test_still_rejects_invalid_schema_after_existing_bounded_repair(tmp_path: Path) -> None:
    client = NeutralClient(text='{"decision":"not-a-decision"}')
    controller = controller_fixture(tmp_path, client)
    with pytest.raises(SupervisorAgentError, match="invalid supervisor decision"):
        await controller._structured_output_self_test()
    assert len(client.turn_params) == 2
    assert_cleaned(controller, client)


async def test_self_test_still_rejects_unexpected_valid_decision(tmp_path: Path) -> None:
    client = NeutralClient(text=SupervisorDecision(
        decision="intervene", reason="neutral unexpected result", message_to_coder="neutral next step"
    ).model_dump_json())
    controller = controller_fixture(tmp_path, client)
    with pytest.raises(RuntimeError, match="unexpected decision"):
        await controller._structured_output_self_test()
    assert len(client.turn_params) == 1
    assert_cleaned(controller, client)


async def test_self_test_removes_fixture_after_provider_failure(tmp_path: Path) -> None:
    client = NeutralClient(error=RuntimeError("neutral provider error"))
    controller = controller_fixture(tmp_path, client)
    with pytest.raises(SupervisorAgentError, match="neutral provider error"):
        await controller._structured_output_self_test()
    assert len(client.turn_params) == 1
    assert_cleaned(controller, client)


async def test_self_test_cancellation_closes_reviewer_before_removing_fixture(tmp_path: Path) -> None:
    client = NeutralClient(block=True)
    controller = controller_fixture(tmp_path, client)
    running = asyncio.create_task(controller._structured_output_self_test())
    await asyncio.wait_for(client.started.wait(), timeout=2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert len(client.turn_params) == 1
    assert_cleaned(controller, client)


async def test_self_test_keeps_240_second_cap_and_cleans_up_on_timeout(tmp_path: Path, monkeypatch) -> None:
    client = NeutralClient(block=True)
    controller = controller_fixture(tmp_path, client)
    real_wait_for = asyncio.wait_for
    requested_timeouts = []

    async def accelerated_wait_for(awaitable, timeout):
        requested_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=0.05 if timeout == 240 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", accelerated_wait_for)
    with pytest.raises(TimeoutError):
        await controller._structured_output_self_test()
    assert requested_timeouts[0] == 240
    assert requested_timeouts.count(240) == 1
    assert len(client.turn_params) == 1
    assert_cleaned(controller, client)


async def test_runtime_disabled_does_not_allocate_or_probe(tmp_path: Path) -> None:
    client = NeutralClient()
    controller = controller_fixture(tmp_path, client)
    controller._runtime_enabled = lambda: False
    await controller._structured_output_self_test()
    assert client.thread_params is None and client.root is None
    assert client.turn_params == []
    controller.store.append_supervisor_wake.assert_not_called()
