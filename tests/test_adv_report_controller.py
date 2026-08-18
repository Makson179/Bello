from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from supervisor.controller import _adversary_report_with_definitions
from supervisor.prompts import (
    build_adv_report_controller_prompt,
    build_completion_review_prompt,
)
from supervisor.schemas import (
    AdvReportControllerDecision,
    AdversaryReport,
    BelloConfig,
    SupervisorWakePacket,
)
from supervisor.schemas.models import (
    openai_strict_json_schema_for_adv_report_controller_decision,
)
from supervisor.state import SUPERVISOR_WAKES, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError


RAW_REPORT = """candidate_finding: true
attacked: parser and response ordering
findings:
- parser accepts an invalid mode
  command: `tool --mode impossible`
  output: accepted
- response order reverses
observations:
- cache count changed without the expected header
held: normal inputs
not_reached: network path
overall: Defects remain in the submitted solution
"""


def _packet(tmp_path: Path) -> SupervisorWakePacket:
    return SupervisorWakePacket(
        wake_sequence=7,
        latest_event_sequence=7,
        generation=0,
        restart_count=0,
        task_path=str(tmp_path / "TASK.md"),
        task_contents="Reject unsupported modes and preserve response order.",
        current_summary="Submitted implementation is ready for review.",
        adversary_report=AdversaryReport(
            candidate_finding=True,
            report_text=RAW_REPORT,
            generation=0,
            completion_wake_sequence=7,
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )


def test_adv_report_controller_schema_is_strict_and_sections_only() -> None:
    schema = openai_strict_json_schema_for_adv_report_controller_decision()

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    with pytest.raises(ValidationError):
        AdvReportControllerDecision(
            forward_to_coder=True,
            reason="invalid preamble",
            report_to_coder=(
                "This was independently verified.\n\n"
                "## Findings requiring correction\n- defect"
            ),
        )
    with pytest.raises(ValidationError):
        AdvReportControllerDecision(
            forward_to_coder=False,
            reason="nothing remains",
            report_to_coder="## Findings requiring correction\n- defect",
        )


def test_controller_prompt_contains_only_input_paths_and_completion_prompt_has_no_report(
    tmp_path: Path,
) -> None:
    packet = _packet(tmp_path)
    packet.current_summary = "SECRET_ACCUMULATED_CONTEXT"
    task_path = tmp_path / "controller-inputs" / "TASK.md"
    report_path = task_path.parent / "raw_adversary_report.md"
    task_path.parent.mkdir()
    task_path.write_text(packet.task_contents, encoding="utf-8")
    report_path.write_text(RAW_REPORT, encoding="utf-8")

    controller_payload = json.loads(
        build_adv_report_controller_prompt(
            task_path=task_path,
            raw_adversary_report_path=report_path,
        )
    )
    completion_payload = json.loads(build_completion_review_prompt(packet))

    assert set(controller_payload) == {
        "instructions",
        "raw_adversary_report_path",
        "task_path",
    }
    assert controller_payload["task_path"] == str(task_path.resolve())
    assert controller_payload["raw_adversary_report_path"] == str(report_path.resolve())
    assert RAW_REPORT not in json.dumps(controller_payload)
    assert packet.task_contents not in json.dumps(controller_payload)
    assert "Read both files completely before classifying" in controller_payload[
        "instructions"
    ][0]
    assert "Downgrade a finding to an observation only" in controller_payload[
        "instructions"
    ][0]
    assert "Do not adjudicate the raw observations" in controller_payload[
        "instructions"
    ][0]
    assert "adversary_report" not in completion_payload
    assert "parser and response ordering" not in json.dumps(completion_payload)


def test_coder_report_definitions_are_fixed_and_short() -> None:
    report = _adversary_report_with_definitions(
        "## Findings requiring correction\n- exact raw finding"
    )

    assert report.startswith(
        "Finding: a confirmed defect that requires correction.\n"
        "Observation: a concern that is not yet confirmed; investigate it and fix it only if confirmed.\n\n"
    )
    assert report.endswith("## Findings requiring correction\n- exact raw finding")


@pytest.mark.asyncio
async def test_dedicated_agent_uses_completion_settings_and_does_not_log_raw_packet(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task)),
        overwrite=True,
    )

    class FakeClient:
        turn_params: dict[str, object] | None = None
        thread_params: dict[str, object] | None = None
        input_root: Path | None = None

        async def thread_start(self, params, *, timeout):
            self.thread_params = params
            return {"thread": {"id": "adv-controller-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params = params
            prompt = json.loads(params["input"][0]["text"])
            assert set(prompt) == {
                "instructions",
                "raw_adversary_report_path",
                "task_path",
            }
            isolated_task = Path(prompt["task_path"])
            isolated_report = Path(prompt["raw_adversary_report_path"])
            self.input_root = isolated_task.parent
            assert isolated_task.read_text(encoding="utf-8") == "# Task"
            assert isolated_report.parent == self.input_root
            assert isolated_report.read_text(encoding="utf-8") == RAW_REPORT
            assert RAW_REPORT not in params["input"][0]["text"]
            assert "# Task" not in params["input"][0]["text"]
            return {
                "turn": {
                    "id": "adv-controller-turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {
                                    "forward_to_coder": True,
                                    "reason": "kept one finding",
                                    "report_to_coder": (
                                        "## Findings requiring correction\n"
                                        "- response order reverses"
                                    ),
                                }
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    agent = StatelessSupervisorAgent(
        client,  # type: ignore[arg-type]
        store,
        task,
        model="gpt-completion",
        intelligence="high",
    )
    packet = _packet(tmp_path)
    packet.current_summary = "SECRET_ACCUMULATED_CONTEXT"

    decision = await agent.decide_adv_report(packet)

    assert decision.forward_to_coder is True
    assert client.turn_params is not None
    assert client.turn_params["model"] == "gpt-completion"
    assert client.turn_params["effort"] == "high"
    assert client.thread_params is not None
    assert client.input_root is not None
    expected_roots = [str(tmp_path.resolve()), str(client.input_root.resolve())]
    assert client.thread_params["runtimeWorkspaceRoots"] == expected_roots
    assert client.turn_params["runtimeWorkspaceRoots"] == expected_roots
    assert client.turn_params["sandboxPolicy"] == {
        "type": "readOnly",
        "networkAccess": False,
    }
    assert not client.input_root.exists()
    audit = json.loads(
        store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert audit["use_case"] == "adv_report_controller"
    assert set(audit["packet"]) == {
        "latest_event_sequence",
        "task_path",
    }
    assert "raw_text" not in audit
    assert "SECRET_ACCUMULATED_CONTEXT" not in json.dumps(audit)


@pytest.mark.asyncio
async def test_invalid_controller_output_cannot_leak_raw_text_through_audit_errors(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task)),
        overwrite=True,
    )

    class InvalidClient:
        input_root: Path | None = None

        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "adv-controller-thread"}}

        async def turn_start(self, params, *, timeout):
            prompt = json.loads(params["input"][0]["text"])
            self.input_root = Path(prompt["raw_adversary_report_path"]).parent
            return {
                "turn": {
                    "id": "adv-controller-turn",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "not json; attacked: SECRET_RAW_REPORT_TEXT",
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = InvalidClient()
    agent = StatelessSupervisorAgent(
        client,  # type: ignore[arg-type]
        store,
        task,
        model="gpt-completion",
        intelligence="high",
    )

    with pytest.raises(SupervisorAgentError):
        await agent.decide_adv_report(_packet(tmp_path))

    audit_text = store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8")
    assert client.input_root is not None
    assert not client.input_root.exists()
    assert RAW_REPORT not in audit_text
    assert "# Task" not in audit_text
    assert "SECRET_RAW_REPORT_TEXT" not in audit_text
    assert "adv_report_controller decision failed" in audit_text
