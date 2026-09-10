"""Plain final JSON still goes through the existing Python decision consumers.

These tests intentionally return only agentMessage items, without a structured
result tool call or provider-side validation. Parsing, repair and semantic
validation must remain the consumer's responsibility.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from supervisor.approval_triage import CheapRuntimeReviewer, CheapRuntimeReviewerError
from supervisor.schemas import AdversaryReport, BelloConfig
from supervisor.state import SUPERVISOR_WAKES, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError


class PlainJsonClient:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.threads: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.archived: list[str] = []

    async def thread_start(self, params, *, timeout):
        self.threads.append(params)
        return {"thread": {"id": f"review-{len(self.threads)}"}}

    async def turn_start(self, params, *, timeout):
        index = len(self.turns)
        self.turns.append(params)
        if index >= len(self.replies):
            raise AssertionError("consumer requested an unexpected extra turn")
        return {
            "turn": {
                "id": f"turn-{index + 1}",
                "status": "completed",
                "items": [{"type": "agentMessage", "text": json.dumps(self.replies[index])}],
            }
        }

    async def thread_archive(self, thread_id, *, timeout):
        self.archived.append(thread_id)
        return {}


def _agent(tmp_path: Path, client: PlainJsonClient, **kwargs):
    task = tmp_path / "TASK.md"
    task.write_text("Reject malformed input without changing the destination.\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("RESULT = 'submitted'\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True
    )
    agent = StatelessSupervisorAgent(client, store, task, **kwargs)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="Review the submitted change.")
    return agent, packet, store


def _audits(store: StateStore) -> list[dict[str, Any]]:
    return [json.loads(line) for line in store.path(SUPERVISOR_WAKES).read_text().splitlines()]


def _completion_accept() -> dict[str, Any]:
    return {
        "decision": "accept",
        "reason": "Malformed input preserves the destination; regression passed.",
        "decision_artifact": {
            "current_state": "Required behavior implemented and checked.",
            "resolved_concerns": [],
            "stale_concerns": [],
            "uncovered_edge_candidates": [],
            "actionable_gap_or_none": None,
        },
        "message_to_coder": None,
        "persistent_decision": None,
        "progress_update": None,
        "clear_handoff": False,
        "display_message": None,
        "handoff": None,
        "wake_sequence": 7,
        "generation": 0,
    }


async def test_runtime_plain_json_invalid_decision_is_repaired_not_accepted(tmp_path: Path) -> None:
    client = PlainJsonClient([
        {"decision": "accept", "reason": "wrong role's decision"},
        {"decision": "noop", "reason": "ordinary coding progress"},
    ])
    agent, packet, store = _agent(tmp_path, client)

    decision = await agent.decide(packet)

    assert decision.decision == "noop"
    assert decision.wake_sequence == 7
    assert len(client.threads) == 1
    assert len(client.turns) == 2
    assert client.turns[0]["outputSchema"] == client.turns[1]["outputSchema"]
    assert "runtime supervisor response was not valid structured JSON" in client.turns[1]["input"][0]["text"]
    assert client.archived == ["review-1"]
    assert [(row["use_case"], row["status"]) for row in _audits(store)] == [
        ("runtime_monitor_parse_retry", "error"),
        ("runtime_monitor", "decision"),
    ]


@pytest.mark.parametrize("invalid_field", ["message_to_coder", "decision_artifact"])
async def test_completion_plain_json_keeps_semantic_and_nested_field_validation(
    tmp_path: Path, invalid_field: str
) -> None:
    valid = _completion_accept()
    invalid = {**valid, invalid_field: "not a valid value for an accepted decision"}
    client = PlainJsonClient([invalid, valid])
    agent, packet, store = _agent(tmp_path, client, completion_workspace_write=True)

    try:
        decision = await agent.decide_completion(packet)
        review_root = Path(client.threads[0]["cwd"])
        assert decision.decision == "accept"
        assert decision.message_to_coder is None
        assert decision.decision_artifact.current_state == valid["decision_artifact"]["current_state"]
        assert len(client.threads) == 1
        assert len(client.turns) == 2
        assert review_root != tmp_path.resolve()
        assert review_root.joinpath("app.py").read_text() == "RESULT = 'submitted'\n"
        assert "compact completion-review JSON object" in client.turns[1]["input"][0]["text"]
        assert client.turns[0]["outputSchema"] == client.turns[1]["outputSchema"]
        assert [(row["use_case"], row["status"]) for row in _audits(store)] == [
            ("completion_review_parse_retry", "error"),
            ("completion_review", "decision"),
        ]
    finally:
        await agent.close_completion_review()

    assert client.archived == ["review-1"]
    assert not review_root.exists()
    assert tmp_path.joinpath("app.py").read_text() == "RESULT = 'submitted'\n"


@pytest.mark.parametrize("repair_succeeds", [False, True])
async def test_adv_controller_plain_json_cannot_forward_null_report(
    tmp_path: Path, repair_succeeds: bool
) -> None:
    invalid = {"forward_to_coder": True, "reason": "a defect remains", "report_to_coder": None}
    fixed = {
        **invalid,
        "report_to_coder": "## Findings requiring correction\n- Malformed input overwrites the destination.",
    }
    client = PlainJsonClient([invalid, fixed if repair_succeeds else invalid])
    agent, packet, store = _agent(tmp_path, client)
    packet.adversary_report = AdversaryReport(
        candidate_finding=True,
        report_text="Malformed input overwrites the destination; a reproduction confirmed this.",
        generation=0,
        completion_wake_sequence=7,
        created_at="2026-09-10T00:00:00+00:00",
    )

    if repair_succeeds:
        decision = await agent.decide_adv_report(packet)
        assert decision.forward_to_coder is True
        assert decision.report_to_coder == fixed["report_to_coder"]
    else:
        with pytest.raises(SupervisorAgentError, match="forward_to_coder=true requires report_to_coder"):
            await agent.decide_adv_report(packet)

    assert len(client.threads) == 1
    assert len(client.turns) == 2
    repair_prompt = client.turns[1]["input"][0]["text"]
    assert "forward_to_coder=true requires report_to_coder" in repair_prompt
    assert client.turns[0]["outputSchema"] == client.turns[1]["outputSchema"]
    assert client.archived == ["review-1"]
    inputs = json.loads(client.turns[0]["input"][0]["text"])
    assert not Path(inputs["task_path"]).exists()
    assert not Path(inputs["raw_adversary_report_path"]).exists()
    assert not Path(client.threads[0]["cwd"]).exists()
    audits = _audits(store)
    assert audits[0]["use_case"] == "adv_report_controller_parse_retry"
    assert audits[0]["status"] == "error"
    assert audits[-1]["status"] == ("decision" if repair_succeeds else "error")
    if not repair_succeeds:
        assert all("decision" not in row for row in audits)


@pytest.mark.parametrize(
    ("decision", "reason_code"),
    [("noop", "routine_progress"), ("escalate", "failed_validation")],
)
async def test_cheap_runtime_consumes_plain_json_and_closes_thread(
    tmp_path: Path, decision: str, reason_code: str
) -> None:
    response = {"decision": decision, "reason_code": reason_code}
    client = PlainJsonClient([response])
    _, packet, _ = _agent(tmp_path, client)
    reviewer = CheapRuntimeReviewer(client, tmp_path, model="triage-model")  # type: ignore[arg-type]

    result = await reviewer.review(packet)

    assert result.model_dump() == response
    assert len(client.turns) == 1
    assert client.turns[0]["outputSchema"]["additionalProperties"] is False
    assert client.archived == ["review-1"]


async def test_cheap_runtime_plain_json_cannot_skip_a_failed_validation(tmp_path: Path) -> None:
    client = PlainJsonClient([{"decision": "noop", "reason_code": "failed_validation"}])
    _, packet, _ = _agent(tmp_path, client)
    reviewer = CheapRuntimeReviewer(client, tmp_path, model="triage-model")  # type: ignore[arg-type]

    with pytest.raises(CheapRuntimeReviewerError, match="invalid cheap runtime decision"):
        await reviewer.review(packet)

    assert len(client.turns) == 1
    assert client.archived == ["review-1"]
