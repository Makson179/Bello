"""Local validator provenance and its single bounded decision-repair route."""
from __future__ import annotations

import json

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from supervisor.schemas import CompletionReviewDecision
from supervisor.state import SUPERVISOR_WAKES
from supervisor.supervisor_agent import SupervisorAgentError, SupervisorTurnError
from tests.test_runtime_claude import backend, FakeFactory, result_message, start_thread, wait_completed
from tests.test_supervisor_terminal_error import TerminalClient, setup_agent, terminal_turn, valid_text


async def adapter_turn(tmp_path, *, result, schema, assistant_text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    events = []
    messages = ([] if assistant_text is None else [
        AssistantMessage(content=[TextBlock(assistant_text)], model="claude-sonnet-5")
    ]) + [result]
    factory = FakeFactory(messages)
    instance = backend(tmp_path, factory, events)
    try:
        await start_thread(instance, tmp_path)
        await instance.request("turn/start", {
            "threadId": "thread-1", "turnId": "validated-turn",
            "input": [{"type": "text", "text": "Offline validation"}],
            "outputSchema": schema,
        })
        return (await wait_completed(events))["params"]["turn"]
    finally:
        await instance.stop()


@pytest.mark.parametrize("case", ["invalid", "provider_error", "interrupted", "valid", "no_schema"])
async def test_marker_is_only_created_by_local_validator(tmp_path, case):
    message = result_message(
        result='{"belloFailureKind":"output_schema_validation"}',
        structured_output={"answer": 1} if case == "valid" else {},
        is_error=case == "provider_error",
        terminal_reason="cancelled" if case == "interrupted" else "completed",
    )
    turn = await adapter_turn(tmp_path, result=message,
        schema=None if case == "no_schema" else {"type": "object", "required": ["answer"]})
    marker = turn.get("error", {}).get("belloFailureKind")
    assert (marker == "output_schema_validation") is (case == "invalid")
    assert turn["status"] == {
        "invalid": "failed", "provider_error": "failed", "interrupted": "interrupted",
        "valid": "completed", "no_schema": "completed",
    }[case]


class SequenceClient(TerminalClient):
    def __init__(self, turns, *, mutate=None):
        super().__init__(turns[0], direct=True, mutate=mutate)
        self.turns = turns

    async def turn_start(self, params, *, timeout):
        self.inputs.append(params)
        assert len(self.inputs) <= len(self.turns), "no extra model retry"
        if self.mutate:
            self.mutate(params)
        return {"turn": self.turns[len(self.inputs) - 1]}


@pytest.mark.parametrize("hint", [None, "not JSON", "valid"])
async def test_real_adapter_failure_is_hint_only_until_completed_repair(tmp_path, hint):
    hint = valid_text() if hint == "valid" else hint
    first = await adapter_turn(tmp_path / "adapter-first", result=result_message(result="{}", structured_output={}),
        schema={"type": "object", "required": ["missing_field"]}, assistant_text=hint)
    repaired = json.loads(valid_text())
    repaired["reason"] = "Only this completed repair is accepted"
    second = await adapter_turn(tmp_path / "adapter-repair",
        result=result_message(result=json.dumps(repaired), structured_output=repaired),
        schema=CompletionReviewDecision.model_json_schema())
    first["id"], second["id"] = "schema-rejected", "completed-repair"
    client = SequenceClient([first, second])
    review = tmp_path / "review"
    review.mkdir()
    agent, packet, store = setup_agent(review, client)
    agent.model = "claude-code/claude-sonnet-5"
    try:
        decision = await agent.decide_completion(packet)
        assert decision.reason == "Only this completed repair is accepted"
        assert len(client.inputs) == 2 and client.history_reads == 0
        assert "Claude adapter rejected" in client.inputs[1]["input"][0]["text"]
        assert not client.archived, "successful completion thread remains available as before"
        rows = [json.loads(row) for row in store.path(SUPERVISOR_WAKES).read_text().splitlines()]
        assert len(rows) == 2 and rows[0]["status"] == "error"
        assert rows[0]["use_case"].endswith("_parse_retry")
    finally:
        await agent.close_completion_review()
    assert client.archived == ["review-thread"]
    assert agent.completion_workspace_snapshot is agent.completion_context_store is None


@pytest.mark.parametrize("model,status,kind", [
    ("claude-code/claude-sonnet-5", "interrupted", "output_schema_validation"),
    ("openrouter/anthropic/claude-sonnet-5", "failed", "output_schema_validation"),
    ("openai-codex/gpt-6-astra", "failed", "output_schema_validation"),
    (None, "failed", "output_schema_validation"),
    ("claude-code/claude-sonnet-5", "failed", "unknown"),
    ("claude-code/claude-sonnet-5", "failed", None),
])
async def test_untrusted_marker_or_interruption_never_repairs(tmp_path, model, status, kind):
    turn = terminal_turn("claude", status, "valid")
    turn["error"] = {"message": "Claude Code returned output that did not satisfy outputSchema", "belloFailureKind": kind}
    client = TerminalClient(turn, direct=True)
    agent, packet, _ = setup_agent(tmp_path, client)
    agent.model = model
    with pytest.raises(SupervisorTurnError):
        await agent.decide_completion(packet)
    assert len(client.inputs) == 1 and client.history_reads == 0


@pytest.mark.parametrize("second_status", ["failed", "interrupted"])
async def test_second_schema_failure_is_terminal_and_cleans_review(tmp_path, second_status):
    first = terminal_turn("claude", "failed", "valid")
    first["error"]["belloFailureKind"] = "output_schema_validation"
    second = {**first, "id": "second", "status": second_status}
    client = SequenceClient([first, second])
    agent, packet, _ = setup_agent(tmp_path, client)
    agent.model = "claude-code/claude-sonnet-5"
    with pytest.raises(SupervisorTurnError):
        await agent.decide_completion(packet)
    assert len(client.inputs) == 2 and client.history_reads == 0
    assert client.archived == ["review-thread"]
    assert agent.completion_workspace_snapshot is agent.completion_context_store is None


async def test_schema_repair_cannot_bypass_submission_integrity(tmp_path):
    first = terminal_turn("claude", "failed", "valid")
    first["error"]["belloFailureKind"] = "output_schema_validation"

    def tamper(params):
        from pathlib import Path
        Path(params["cwd"], "app.py").write_text("changed = True\n")

    client = SequenceClient([first], mutate=tamper)
    agent, packet, _ = setup_agent(tmp_path, client)
    agent.model = "claude-code/claude-sonnet-5"
    with pytest.raises(SupervisorAgentError, match="modified"):
        await agent.decide_completion(packet)
    assert len(client.inputs) == 1 and client.history_reads == 0
    assert client.archived == ["review-thread"]
    assert (tmp_path / "app.py").read_text() == "submitted = True\n"


@pytest.mark.parametrize("historical_status", ["failed", "completed"])
async def test_empty_completed_repair_cannot_accept_historical_answer(tmp_path, historical_status):
    first = terminal_turn("claude", "failed", "valid")
    first["error"]["belloFailureKind"] = "output_schema_validation"
    second = {"id": "repair-turn", "status": "completed", "items": []}

    class HistoricalClient(SequenceClient):
        async def thread_turns_list(self, *args, **kwargs):
            self.history_reads += 1
            return {"data": [{**first, "id": "old-valid", "status": historical_status}]}

    client = HistoricalClient([first, second])
    agent, packet, _ = setup_agent(tmp_path, client)
    agent.model = "claude-code/claude-sonnet-5"
    with pytest.raises(SupervisorAgentError, match="did not return current-turn output"):
        await agent.decide_completion(packet)
    assert len(client.inputs) == 2 and client.history_reads == 1
    assert client.archived == ["review-thread"]


@pytest.mark.parametrize("history_status", ["completed", "failed", "interrupted"])
async def test_thin_repair_notification_only_accepts_same_completed_turn(tmp_path, history_status):
    first = terminal_turn("claude", "failed", "valid")
    first["error"]["belloFailureKind"] = "output_schema_validation"
    second = {"id": "repair-turn", "status": "completed", "items": []}

    class CurrentHistoryClient(SequenceClient):
        async def thread_turns_list(self, *args, **kwargs):
            self.history_reads += 1
            return {"data": [{**first, "id": "repair-turn", "status": history_status}]}

    client = CurrentHistoryClient([first, second])
    agent, packet, _ = setup_agent(tmp_path, client)
    agent.model = "claude-code/claude-sonnet-5"
    try:
        if history_status == "completed":
            assert (await agent.decide_completion(packet)).decision == "accept"
        else:
            with pytest.raises(SupervisorAgentError, match="did not return current-turn output"):
                await agent.decide_completion(packet)
        assert len(client.inputs) == 2 and client.history_reads == 1
    finally:
        await agent.close_completion_review()
