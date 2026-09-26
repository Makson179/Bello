from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from supervisor.completion_context import CompletionContextStore
from supervisor.appserver import AppServerError
from supervisor.prompts import build_adversary_prompt, build_completion_review_prompt
from supervisor.schemas import (
    BehaviorSurfaceItem,
    BelloConfig,
    EvidenceProvenanceSummary,
    SupervisorWakePacket,
    ValidationRun,
)
from supervisor.schemas.models import CompletionReturnRecord, ValidationProvenance
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError


def packet(**values) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=10, latest_event_sequence=9, generation=0, restart_count=0,
        task_path="TASK.md", task_contents="Implement the task exactly.", **values,
    )


@pytest.fixture
def context_store():
    store = CompletionContextStore()
    try:
        yield store
    finally:
        store.cleanup()


def read_entry(payload: dict, field: str, position: int = 0):
    index = Path(payload["available_evidence"][field]["index_path"])
    row = json.loads(index.read_text().splitlines()[position])
    return json.loads(Path(row["path"]).read_text())


def test_selective_packet_preserves_full_evidence_and_exposes_risks(context_store) -> None:
    validations = [
        ValidationRun(validation_id="old", command="pytest old", passed=True,
                      summary="old success", sequence=3),
        ValidationRun(validation_id="masked", command="pytest || true", passed=True,
                      trusted_validation_outcome="masked_or_unknown", masking_reason="masked status",
                      summary="CLAIM" * 2000, captured_output="KEEP-EXACT-OUTPUT" * 2000, sequence=7),
        ValidationRun(validation_id="fresh", command="pytest changed", passed=True,
                      summary="success", sequence=8),
    ]
    original = packet(
        progress="private progress" * 3000,
        recent_events=[{"method": "test", "body": "historical output" * 3000}],
        validations=validations, latest_relevant_change_sequence=5,
        behavior_surface=[BehaviorSurfaceItem(category="invalid inputs", status="required")],
        prior_uncovered_edge_candidates=["unicode parsing"],
        previous_completion_returns=[CompletionReturnRecord(reason="missing behavior", sequence=4, generation=0)],
        evidence_provenance_summary=EvidenceProvenanceSummary(
            capture_inconsistencies=["output unavailable"],
            validations=[ValidationProvenance(
                validation_id="masked", command="pytest || true", passed=True, type="behavioral",
                trusted_validation_outcome="masked_or_unknown", sequence=7,
                independence_class="masked_or_unknown", risk_reasons=["masked status"],
            )],
        ),
    )
    raw_before = original.model_dump(mode="json")
    context = context_store.write_packet(original)
    prompt = build_completion_review_prompt(original, selective_context=context)
    result = json.loads(prompt)
    assert len(prompt) < 35_000
    assert "private progress" not in prompt
    assert "historical output" not in prompt
    assert "KEEP-EXACT-OUTPUT" not in prompt
    assert result["task_contents"] == original.task_contents
    assert result["behavior_surface"] == raw_before["behavior_surface"]
    assert result["prior_uncovered_edge_candidates"] == ["unicode parsing"]
    assert result["evidence_summary"]["fresh_passing_behavioral_records"] == 1
    assert result["evidence_summary"]["stale_validation_records"] == 1
    assert result["evidence_summary"]["validation_outcomes"]["masked_or_unknown"] == 1
    assert result["evidence_summary"]["provenance_flagged_records"] == 1
    assert result["evidence_summary"]["capture_inconsistencies"] == 1
    assert read_entry(result, "validations", 1) == validations[1].model_dump(mode="json")
    assert read_entry(result, "previous_completion_returns")["reason"] == "missing behavior"
    assert original.model_dump(mode="json") == raw_before
    assert list(result).index("instructions") < list(result).index("task_contents")
    assert list(result).index("task_contents") < list(result).index("available_evidence")
    instructions = "\n".join(result["instructions"])
    assert "Do not read every file just because it is listed" in instructions
    assert "Read prior completion returns relevant to this revision before accepting" in instructions
    assert "fresh passing count alone never proves" in instructions


def test_context_wakes_are_immutable_and_task_fallback_does_not_truncate(context_store) -> None:
    original = packet()
    first = context_store.write_packet(original)
    index_path = Path(first["evidence_index_path"])
    before = index_path.read_bytes()
    second = context_store.write_packet(original, task_in_file=True)
    assert index_path.read_bytes() == before
    assert second["evidence_index_path"] != first["evidence_index_path"]
    earlier = Path(second["earlier_evidence_contexts"]["index_path"])
    assert json.loads(earlier.read_text().splitlines()[0])["index_path"] == str(index_path)
    assert "task_contents" not in second
    assert Path(second["task_path"]).read_text() == original.task_contents
    assert not (index_path.stat().st_mode & 0o200)
    context_store.assert_unchanged()


def test_modified_input_rejected_and_cleanup_handles_readonly_files(context_store) -> None:
    context = context_store.write_packet(packet())
    path = Path(context["task_path"])
    path.chmod(0o600)
    path.write_text("replacement")
    with pytest.raises(OSError, match="modified"):
        context_store.assert_unchanged()
    context_store.cleanup()
    assert not context_store.root.exists()


@pytest.mark.skipif(os.name == "nt", reason="unprivileged Windows symlinks unavailable")
def test_evidence_symlink_rejected_without_following(context_store, tmp_path: Path) -> None:
    context = context_store.write_packet(packet())
    path = Path(context["task_path"])
    outside = tmp_path / "outside.txt"
    outside.write_text("do not change")
    path.parent.chmod(0o700)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError, match="replaced"):
        context_store.assert_unchanged()
    context_store.cleanup()
    assert outside.read_text() == "do not change"


def test_completion_context_does_not_change_adversary_payload(context_store) -> None:
    original = packet(validations=[ValidationRun(
        validation_id="v", command="pytest", passed=True, summary="ok", sequence=8,
    )])
    before = build_adversary_prompt(original)
    context_store.write_packet(original)
    assert build_adversary_prompt(original) == before
    assert "available_evidence" not in before


class ReviewClient:
    def __init__(self, *, reject_first_input: bool = False, mutate_input: bool = False):
        self.starts: list[dict] = []
        self.inputs: list[dict] = []
        self.archived: list[str] = []
        self.reject_first_input = reject_first_input
        self.mutate_input = mutate_input

    async def thread_start(self, params, *, timeout):
        self.starts.append(params)
        return {"thread": {"id": f"review-{len(self.starts)}"}}

    async def turn_start(self, params, *, timeout):
        context = json.loads(params["input"][0]["text"])
        self.inputs.append(context)
        if self.reject_first_input and len(self.inputs) == 1:
            raise AppServerError("input_too_large")
        if self.mutate_input:
            path = Path(context["task_path"])
            path.chmod(0o600)
            path.write_text("modified")
        return {"turn": {
            "id": f"turn-{len(self.inputs)}", "status": "completed", "items": [{
                "type": "agentMessage", "text": json.dumps({
                    "decision": "accept", "reason": "reviewed", "validation_gaps": [],
                    "message_to_coder": None, "persistent_decision": None, "progress_update": None,
                    "clear_handoff": False, "display_message": None, "handoff": None,
                    "wake_sequence": context["wake_sequence"], "generation": context["generation"],
                }),
            }],
        }}

    async def thread_archive(self, thread_id, *, timeout):
        self.archived.append(thread_id)
        return {}


def make_agent(tmp_path: Path, client: ReviewClient):
    task = tmp_path / "TASK.md"
    task.write_text("# Task\nCheck the submitted implementation.")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    return StatelessSupervisorAgent(client, store, task)


async def test_persistent_review_retains_indexes_and_same_read_scope(tmp_path: Path) -> None:
    client = ReviewClient()
    agent = make_agent(tmp_path, client)
    first = agent.build_packet(wake_sequence=7, current_summary="first review")
    second = agent.build_packet(wake_sequence=8, current_summary="continued review")
    try:
        await agent.decide_completion(first)
        first_path = Path(client.inputs[0]["evidence_index_path"])
        first_bytes = first_path.read_bytes()
        first_root = agent.completion_context_store.root
        await agent.decide_completion(second)
        assert len(client.starts) == 1
        assert agent.completion_context_store.root == first_root
        assert str(first_root) in client.starts[0]["runtimeWorkspaceRoots"]
        assert first_path.read_bytes() == first_bytes
        assert client.inputs[1]["evidence_index_path"] != str(first_path)
        assert first_root != tmp_path
    finally:
        await agent.close_completion_review()
    assert not first_root.exists()
    assert client.archived == ["review-1"]


async def test_input_size_retry_rebuilds_readable_files_and_retains_complete_task(tmp_path: Path) -> None:
    client = ReviewClient(reject_first_input=True)
    agent = make_agent(tmp_path, client)
    original = agent.build_packet(wake_sequence=7, current_summary="review")
    try:
        result = await agent.decide_completion(original)
        assert result.decision == "accept"
        assert len(client.starts) == 2
        assert Path(client.inputs[0]["task_path"]).read_text() == original.task_contents
        assert "task_contents" not in client.inputs[1]
        assert Path(client.inputs[1]["task_path"]).read_text() == original.task_contents
        assert client.starts[0]["runtimeWorkspaceRoots"] == client.starts[1]["runtimeWorkspaceRoots"]
        earlier = Path(client.inputs[1]["earlier_evidence_contexts"]["index_path"])
        assert json.loads(earlier.read_text().splitlines()[0])["index_path"] == client.inputs[0]["evidence_index_path"]
    finally:
        await agent.close_completion_review()


async def test_modified_evidence_cannot_result_in_accept(tmp_path: Path) -> None:
    client = ReviewClient(mutate_input=True)
    agent = make_agent(tmp_path, client)
    original = agent.build_packet(wake_sequence=7, current_summary="review")
    with pytest.raises(SupervisorAgentError, match="evidence was modified"):
        await agent.decide_completion(original)
    assert agent.completion_context_store is None
    assert agent.completion_thread_id is None
    assert client.archived == ["review-1"]
    assert not Path(client.inputs[0]["task_path"]).exists()
