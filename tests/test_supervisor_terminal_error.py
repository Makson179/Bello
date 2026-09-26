"""Offline common-envelope coverage: terminal errors are not JSON repairs."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from supervisor.appserver import AppServerMessage
from supervisor.completion_context import CompletionContextStore
from supervisor.schemas import AdversaryReport, BelloConfig, CompletionReviewDecision
from supervisor.state import SUPERVISOR_WAKES, StateStore
from supervisor.supervisor_agent import (
    StatelessSupervisorAgent, SupervisorAgentError, SupervisorTurnError,
)
from supervisor.workspace_snapshot import VerificationWorkspaceSnapshot


def valid_text() -> str:
    return CompletionReviewDecision(
        decision="accept", reason="Synthetic completed review", message_to_coder=None,
        persistent_decision=None, progress_update=None, clear_handoff=False,
        display_message=None, handoff=None, wake_sequence=7, generation=0,
    ).model_dump_json()


def terminal_turn(engine: str, status: str, content: str) -> dict:
    # These are the common turn/completed envelopes emitted by each backend,
    # not fabricated successful replies from a paid provider.
    message = {
        "pi": "402 in_flight_budget_exhausted",
        "claude": "Claude Code reported that the turn failed",
        "codex": "HTTP connection failed: 402",
    }[engine]
    error = {"message": message}
    if engine == "codex":
        error["codexErrorInfo"] = {"httpConnectionFailed": {"httpStatusCode": 402}}
    item = {"id": f"{engine}-answer", "type": "agentMessage", "text": valid_text() if content == "valid" else ""}
    if engine == "pi":
        item["status"] = "completed"  # item completion is not turn success
    return {"id": "failed-turn", "status": status, "error": error, "items": [] if content == "missing" else [item]}


class TerminalClient:
    def __init__(self, turn: dict, *, direct: bool = False, mutate=None):
        self.turn = turn
        self.direct = direct
        self.mutate = mutate
        self.starts = []
        self.inputs = []
        self.waits = 0
        self.history_reads = 0
        self.archived = []

    async def thread_start(self, params, *, timeout):
        self.starts.append(params)
        return {"thread": {"id": "review-thread"}}

    async def turn_start(self, params, *, timeout):
        self.inputs.append(params)
        assert len(self.inputs) == 1, "terminal provider error must never trigger model repair"
        if self.mutate:
            self.mutate(params)
        return {"turn": self.turn if self.direct else {"id": self.turn["id"], "status": "inProgress", "items": []}}

    async def wait_for_notification(self, predicate, *, timeout):
        self.waits += 1
        assert not self.direct, "already terminal turn must not await another notification"
        message = AppServerMessage({"method": "turn/completed", "params": {"threadId": "review-thread", "turn": self.turn}})
        assert predicate(message)
        return message

    async def thread_turns_list(self, *args, **kwargs):
        self.history_reads += 1
        # A previous valid answer must never turn today's failed turn into accept.
        return {"data": [{"id": "older-turn", "status": "completed", "items": [{"type": "agentMessage", "text": valid_text()}]}]}

    async def thread_archive(self, thread_id, *, timeout):
        self.archived.append(thread_id)
        return {}


def setup_agent(tmp_path, client, *, write_snapshot=True, cleanup=None):
    task = tmp_path / "TASK.md"
    task.write_text("Verify the submitted program.\n")
    (tmp_path / "app.py").write_text("submitted = True\n")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    agent = StatelessSupervisorAgent(client, store, task, completion_workspace_write=write_snapshot,
                                     before_completion_thread_cleanup=cleanup)
    return agent, agent.build_packet(wake_sequence=7, current_summary="Review completion"), store


@pytest.mark.parametrize("engine", ["pi", "claude", "codex"])
@pytest.mark.parametrize("status", ["failed", "interrupted"])
@pytest.mark.parametrize("content", ["empty", "missing", "valid"])
@pytest.mark.parametrize("direct", [False, True])
async def test_terminal_envelope_never_repairs_or_accepts_old_answer(tmp_path, monkeypatch, engine, status, content, direct):
    checks = {"snapshot": 0, "context": 0}
    original_snapshot = VerificationWorkspaceSnapshot.assert_submission_unchanged
    original_context = CompletionContextStore.assert_unchanged

    def snapshot_check(self):
        checks["snapshot"] += 1
        return original_snapshot(self)

    def context_check(self):
        if self._files:
            checks["context"] += 1
        return original_context(self)

    monkeypatch.setattr(VerificationWorkspaceSnapshot, "assert_submission_unchanged", snapshot_check)
    monkeypatch.setattr(CompletionContextStore, "assert_unchanged", context_check)
    cleaned = []

    async def cleanup(thread_id, workspace):
        assert workspace.is_dir(), "children are cleaned before workspace disposal"
        cleaned.append((thread_id, workspace))

    client = TerminalClient(terminal_turn(engine, status, content), direct=direct)
    agent, packet, store = setup_agent(tmp_path, client, cleanup=cleanup)
    with pytest.raises(SupervisorTurnError) as caught:
        await agent.decide_completion(packet)
    assert caught.value.turn_status == status
    if engine in {"pi", "codex"}:
        assert "402" in str(caught.value)
    if engine == "codex":
        assert caught.value.provider_error["error"]["codexErrorInfo"]["httpConnectionFailed"]["httpStatusCode"] == 402
    assert len(client.starts) == len(client.inputs) == 1
    assert client.waits == (0 if direct else 1)
    assert client.history_reads == 0
    assert checks == {"snapshot": 1, "context": 1}
    assert client.archived == ["review-thread"]
    assert len(cleaned) == 1 and cleaned[0][0] == "review-thread"
    assert not cleaned[0][1].exists()
    context_root = Path(client.starts[0]["runtimeWorkspaceRoots"][-1])
    assert not context_root.exists()
    assert agent.completion_thread_id is agent.completion_workspace_snapshot is agent.completion_context_store is None
    assert (tmp_path / "app.py").read_text() == "submitted = True\n"
    audits = [json.loads(row) for row in store.path(SUPERVISOR_WAKES).read_text().splitlines()]
    assert len(audits) == 1 and audits[0]["status"] == "error"
    assert "retry" not in audits[0]["use_case"]


@pytest.mark.parametrize("message", [
    "input_too_large", "invalid supervisor decision:", "did not produce an agent message",
])
async def test_terminal_error_text_cannot_dispatch_generic_completion_retries(tmp_path, message):
    turn = terminal_turn("pi", "failed", "empty")
    turn["error"]["message"] = message + " Bearer offline-secret-value"
    client = TerminalClient(turn, direct=True)
    agent, packet, store = setup_agent(tmp_path, client)
    with pytest.raises(SupervisorTurnError) as caught:
        await agent.decide_completion(packet)
    assert len(client.inputs) == len(client.starts) == 1
    assert "offline-secret-value" not in str(caught.value)
    assert "offline-secret-value" not in store.path(SUPERVISOR_WAKES).read_text()


@pytest.mark.parametrize("mutation", ["submission", "context"])
async def test_failed_turn_does_not_bypass_integrity_checks_or_cleanup(tmp_path, mutation):
    agent = None

    def mutate(params):
        if mutation == "submission":
            Path(params["cwd"], "app.py").write_text("changed = True\n")
        else:
            path = next(iter(agent.completion_context_store._files))
            path.chmod(0o600)
            path.write_text("changed evidence\n")

    client = TerminalClient(terminal_turn("pi", "failed", "empty"), direct=True, mutate=mutate)
    cleaned = []

    async def cleanup(thread_id, workspace):
        assert workspace.exists()
        cleaned.append(workspace)

    agent, packet, store = setup_agent(tmp_path, client, cleanup=cleanup)
    with pytest.raises(SupervisorAgentError, match="modified") as caught:
        await agent.decide_completion(packet)
    assert not isinstance(caught.value, SupervisorTurnError), "integrity failure takes precedence"
    assert len(client.inputs) == 1 and client.history_reads == 0
    assert client.archived == ["review-thread"] and len(cleaned) == 1
    assert not cleaned[0].exists()
    assert agent.completion_context_store is None
    assert (tmp_path / "app.py").read_text() == "submitted = True\n"


async def test_compact_completion_retry_terminal_error_does_not_enter_minimal_retry(tmp_path, monkeypatch):
    agent, packet, _store = setup_agent(tmp_path, object())
    calls = []
    terminal = SupervisorTurnError(terminal_turn("pi", "failed", "empty"))

    async def decide(_packet, **kwargs):
        calls.append(kwargs["use_case"])
        if len(calls) == 1:
            raise SupervisorAgentError("input_too_large")
        terminal.args = ("invalid supervisor decision: synthetic diagnostic",)
        raise terminal

    monkeypatch.setattr(agent, "_decide_completion_with_prompt", decide)
    with pytest.raises(SupervisorTurnError):
        await agent.decide_completion(packet)
    assert calls == ["completion_review", "completion_review_compact_retry"]


def test_terminal_error_diagnostics_are_bounded_and_sanitized():
    turn = terminal_turn("codex", "failed", "empty")
    turn["error"]["message"] = "Bearer offline-secret-value " + "x" * 100_000
    error = SupervisorTurnError(turn)
    assert len(str(error)) < 4200
    assert "offline-secret-value" not in str(error)
    assert "offline-secret-value" not in json.dumps(error.provider_error)
    assert error.turn_status == "failed"


async def test_completed_history_lookup_retains_original_post_read_integrity_check(tmp_path):
    turn = {"id": "completed-turn", "status": "completed", "items": []}

    class MutatingHistoryClient(TerminalClient):
        async def thread_turns_list(self, *args, **kwargs):
            Path(self.starts[0]["cwd"], "app.py").write_text("late mutation\n")
            return await super().thread_turns_list(*args, **kwargs)

    client = MutatingHistoryClient(turn, direct=True)
    agent, packet, _store = setup_agent(tmp_path, client)
    with pytest.raises(SupervisorAgentError, match="modified submitted workspace"):
        await agent.decide_completion(packet)
    assert len(client.inputs) == 1 and client.history_reads == 1
    assert client.archived == ["review-thread"]
    assert agent.completion_workspace_snapshot is None
    assert (tmp_path / "app.py").read_text() == "submitted = True\n"


@pytest.mark.parametrize("engine", ["pi", "claude", "codex"])
@pytest.mark.parametrize("status", ["failed", "interrupted"])
@pytest.mark.parametrize("role", ["runtime", "adversary_report_controller"])
async def test_shared_decision_consumers_do_not_repair_terminal_envelopes(tmp_path, engine, status, role):
    client = TerminalClient(terminal_turn(engine, status, "missing"))
    agent, packet, _store = setup_agent(tmp_path, client, write_snapshot=False)
    if role == "adversary_report_controller":
        packet.adversary_report = AdversaryReport(
            candidate_finding=True, report_text="Synthetic finding to review", generation=0,
            completion_wake_sequence=7, created_at="2026-09-26T00:00:00+00:00",
        )
    with pytest.raises(SupervisorTurnError):
        if role == "runtime":
            await agent.decide(packet)
        else:
            await agent.decide_adv_report(packet)
    assert len(client.inputs) == 1 and client.history_reads == 0
    assert client.archived == ["review-thread"]
    if role == "adversary_report_controller":
        assert not Path(client.starts[0]["cwd"]).exists()
        prompt = json.loads(client.inputs[0]["input"][0]["text"])
        assert not Path(prompt["task_path"]).exists()
        assert not Path(prompt["raw_adversary_report_path"]).exists()


async def test_cancellation_while_waiting_keeps_cleanup_and_never_repairs(tmp_path):
    waiting = asyncio.Event()

    class WaitingClient(TerminalClient):
        async def wait_for_notification(self, predicate, *, timeout):
            self.waits += 1
            waiting.set()
            await asyncio.Future()

    cleaned = []

    async def cleanup(thread_id, workspace):
        assert workspace.exists()
        cleaned.append(workspace)

    client = WaitingClient(terminal_turn("pi", "interrupted", "empty"))
    agent, packet, _store = setup_agent(tmp_path, client, cleanup=cleanup)
    task = asyncio.create_task(agent.decide_completion(packet))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client.inputs) == 1 and client.history_reads == 0
    assert client.archived == ["review-thread"] and len(cleaned) == 1
    assert not cleaned[0].exists()
    assert agent.completion_thread_id is agent.completion_workspace_snapshot is agent.completion_context_store is None
