"""Controller reviewer sessions regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from supervisor.coder import CODEX_FAST_SERVICE_TIER
from supervisor.schemas import BelloConfig, SupervisorDecisionKind, ValidationRun
from supervisor.state import SUPERVISOR_WAKES, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent


async def test_completion_review_agent_reuses_thread_until_closed(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_starts = 0
            self.turn_starts = []
            self.archived = []

        async def thread_start(self, params, *, timeout):
            self.thread_starts += 1
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts.append(params["threadId"])
            return {
                "turn": {
                    "id": f"turn-{len(self.turn_starts)}",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "decision": "return",
                                    "reason": "needs more validation",
                                    "files_reviewed": [],
                                    "behavior_evidence_matrix": [],
                                    "uncovered_behaviors": ["fallback"],
                                    "validation_gaps": ["missing fallback test"],
                                    "claim_evidence_mismatches": [],
                                    "packet_or_access_limitations": [],
                                    "changed_test_risks": [],
                                    "message_to_coder": "validate fallback",
                                    "persistent_decision": None,
                                    "progress_update": None,
                                    "clear_handoff": False,
                                    "display_message": None,
                                    "handoff": None,
                                    "wake_sequence": 7,
                                    "generation": 0,
                                }
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    await agent.decide_completion(packet)
    await agent.decide_completion(packet)

    assert client.thread_starts == 1
    assert client.turn_starts == ["completion-thread", "completion-thread"]
    assert client.archived == []

    await agent.close_completion_review()

    assert client.archived == ["completion-thread"]


async def test_supervisor_fast_mode_sets_codex_service_tier(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = None
            self.turn_params = None

        async def thread_start(self, params, *, timeout):
            self.thread_params = params
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps({"decision": "noop", "reason": "routine progress"}),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    default_agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    assert default_agent._thread_params()["serviceTier"] is None

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task, fast=True)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="runtime check")

    decision = await agent.decide(packet)

    assert decision.decision == SupervisorDecisionKind.NOOP
    assert client.thread_params["serviceTier"] == CODEX_FAST_SERVICE_TIER
    assert client.turn_params["serviceTier"] == CODEX_FAST_SERVICE_TIER


async def test_supervisor_agent_sets_intelligence_effort(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = None
            self.turn_params = None

        async def thread_start(self, params, *, timeout):
            self.thread_params = params
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps({"decision": "noop", "reason": "routine progress"}),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task, intelligence="high")  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="runtime check")

    decision = await agent.decide(packet)

    assert decision.decision == SupervisorDecisionKind.NOOP
    assert client.thread_params["effort"] == "high"
    assert client.turn_params["effort"] == "high"


async def test_completion_review_agent_overrides_stale_model_wake_sequence(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            return {
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "decision": "return",
                                    "reason": "needs independent demo",
                                    "files_reviewed": [],
                                    "behavior_evidence_matrix": [],
                                    "uncovered_behaviors": ["rendered element"],
                                    "validation_gaps": ["missing factual demo output"],
                                    "claim_evidence_mismatches": [],
                                    "packet_or_access_limitations": [],
                                    "changed_test_risks": [],
                                    "message_to_coder": "provide a factual behavior_demo",
                                    "persistent_decision": None,
                                    "progress_update": None,
                                    "clear_handoff": False,
                                    "display_message": None,
                                    "handoff": None,
                                    "wake_sequence": 3,
                                    "generation": 99,
                                }
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=11, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.wake_sequence == 11
    assert decision.generation == 0
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["decision"]["wake_sequence"] == 11
    assert '"wake_sequence": 3' in audit["raw_text"]


async def test_completion_review_reads_assistant_message_content_from_turns_list(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    decision_text = json.dumps(
        {
            "decision": "return",
            "reason": "needs captured output",
            "files_reviewed": [],
            "behavior_evidence_matrix": [],
            "uncovered_behaviors": ["demo"],
            "validation_gaps": ["missing captured demo output"],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "message_to_coder": "record demo output",
            "persistent_decision": None,
            "progress_update": None,
            "clear_handoff": False,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 7,
            "generation": 0,
        }
    )

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "turn-1", "status": "completed", "items": []}}

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            assert limit == 5
            assert items_view == "full"
            return {
                "data": [
                    {"id": "older-turn", "items": [{"type": "agentMessage", "text": "{}"}]},
                    {
                        "id": "turn-1",
                        "items": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": decision_text}],
                            }
                        ],
                    },
                ]
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    agent = StatelessSupervisorAgent(FakeClient(), store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["status"] == "decision"
    assert audit["raw_text"] == decision_text


async def test_completion_review_no_message_retries_with_selective_context_and_minimal_prompt(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\nImplement the compiler.\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    valid_decision = {
        "decision": "return",
        "reason": "needs independent demo",
        "decision_artifact": {
            "current_state": "provider recovered on compact retry",
            "resolved_concerns": [],
            "stale_concerns": [],
            "uncovered_edge_candidates": ["independent demo missing"],
            "actionable_gap_or_none": "run an independent demo",
        },
        "files_reviewed": [],
        "behavior_evidence_matrix": [],
        "uncovered_behaviors": ["independent demo"],
        "validation_gaps": [],
        "claim_evidence_mismatches": [],
        "packet_or_access_limitations": [],
        "changed_test_risks": [],
        "message_to_coder": "Run an independent demo for the claimed compiler behavior.",
        "persistent_decision": None,
        "progress_update": None,
        "clear_handoff": False,
        "display_message": None,
        "handoff": None,
        "wake_sequence": 7,
        "generation": 0,
    }

    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0
            self.turn_inputs: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": f"completion-thread-{self.thread_count}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_inputs.append(params["input"][0]["text"])
            turn_number = len(self.turn_inputs)
            if turn_number <= 2:
                return {
                    "turn": {
                        "id": f"turn-{turn_number}",
                        "status": "completed",
                        "items": [],
                    }
                }
            return {
                "turn": {
                    "id": "turn-3",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": json.dumps(valid_decision)}],
                }
            }

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            return {"data": []}

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(
        wake_sequence=7,
        current_summary="completion review",
        validations=[
            ValidationRun(
                command="pytest tests/public",
                exit_code=0,
                passed=True,
                summary="46 passed\n" + ("x" * 5000),
                captured_output="46 passed\n" + ("y" * 5000),
                sequence=6,
            )
        ],
    )

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    assert len(client.turn_inputs) == 3
    assert "Emergency compact JSON retry" in client.turn_inputs[2]
    assert '"review_context_mode": "selective_files"' in client.turn_inputs[2]
    retry_context = json.loads(client.turn_inputs[2].split("\n\n# Emergency compact JSON retry", 1)[0])
    validation_index = Path(retry_context["available_evidence"]["validations"]["index_path"])
    record = json.loads(validation_index.read_text().splitlines()[0])
    evidence = json.loads(Path(record["path"]).read_text())
    assert evidence["captured_output"] == packet.validations[0].captured_output
    assert "supervisor did not produce an agent message" in client.turn_inputs[2]
    assert client.archived == ["completion-thread-1"]
    audit_rows = [
        json.loads(line)
        for line in store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_rows[-1]["use_case"] == "completion_review_no_message_minimal_retry"
    assert audit_rows[-1]["status"] == "decision"


async def test_supervisor_agent_retries_invalid_structured_output_once(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    valid_decision = {
        "decision": "return",
        "reason": "needs factual demo",
        "files_reviewed": [],
        "behavior_evidence_matrix": [],
        "uncovered_behaviors": ["demo"],
        "validation_gaps": ["missing factual demo output"],
        "claim_evidence_mismatches": [],
        "packet_or_access_limitations": [],
        "changed_test_risks": [],
        "message_to_coder": "record demo output",
        "persistent_decision": None,
        "progress_update": None,
        "clear_handoff": False,
        "display_message": None,
        "handoff": None,
        "wake_sequence": 7,
        "generation": 0,
    }

    class FakeClient:
        def __init__(self) -> None:
            self.turn_inputs = []

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "completion-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_inputs.append(params["input"][0]["text"])
            if len(self.turn_inputs) == 1:
                return {
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"type": "agentMessage", "text": '{"decision":"return","reason":"unterminated'}],
                    }
                }
            return {
                "turn": {
                    "id": "turn-2",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": json.dumps(valid_decision)}],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(client, store, task)  # type: ignore[arg-type]
    packet = agent.build_packet(wake_sequence=7, current_summary="completion review")

    decision = await agent.decide_completion(packet)

    assert decision.decision == "return"
    assert len(client.turn_inputs) == 2
    assert "previous completion-review response was not valid structured JSON" in client.turn_inputs[1]
    assert "compact completion-review JSON object" in client.turn_inputs[1]
    assert "files_reviewed=[]" in client.turn_inputs[1]
    assert "behavior_evidence_matrix=[]" in client.turn_inputs[1]
    assert "under 3000 characters" in client.turn_inputs[1]
    audits = [json.loads(line) for line in store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()]
    assert audits[-2]["use_case"] == "completion_review_parse_retry"
    assert audits[-2]["status"] == "error"
    assert audits[-1]["status"] == "decision"
