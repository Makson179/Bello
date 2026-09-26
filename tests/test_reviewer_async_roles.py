from __future__ import annotations

import json

import pytest

from supervisor.approval_triage import CheapRuntimeReviewer
from supervisor.runtime.async_tools import ASYNC_TOOLS_GUIDANCE
from supervisor.runtime.client import RuntimeClient
from supervisor.schemas import AdversaryReport, BelloConfig
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent
from tests.test_runtime_client import FakeBackend


class DecisionBackend(FakeBackend):
    async def request(self, method, params, timeout=30):
        response = await super().request(method, params, timeout)
        if method == "turn/start" and "outputSchema" in params:
            properties = params["outputSchema"]["properties"]
            if "reason_code" in properties:
                decision = {"decision": "noop", "reason_code": "routine_progress"}
            elif "forward_to_coder" in properties:
                decision = {"forward_to_coder": False, "reason": "no finding", "report_to_coder": None}
            elif "validation_gaps" in properties:
                decision = {"decision": "accept", "reason": "reviewed", "validation_gaps": [],
                            "message_to_coder": None, "persistent_decision": None, "progress_update": None,
                            "clear_handoff": False, "display_message": None, "handoff": None,
                            "wake_sequence": 7, "generation": 0}
            else:
                decision = {"decision": "noop", "reason": "routine"}
            response["turn"].update(status="completed", items=[{
                "type": "agentMessage", "text": json.dumps(decision),
            }])
        return response


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "claude-code/claude-sonnet-4-6", "openai/gpt-5.6-sol"])
@pytest.mark.parametrize("role", ["runtime", "cheap_runtime", "completion_review", "adv_report_controller"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_reviewer_threads_receive_their_async_policy(tmp_path, model, role, enabled):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = workspace / "TASK.md"
    task.write_text("# Task\nImplement the requested behavior.")
    store = StateStore(workspace)
    store.initialize_bello(BelloConfig(project_root=str(workspace), task_path=str(task)), overwrite=True)
    backends = {name: DecisionBackend() for name in ("codex", "claude-code", "pi")}
    client = RuntimeClient(cwd=tmp_path, state_dir=tmp_path / "runtime-state", backends=backends)
    client.configure_run(async_tools=enabled)
    agent = StatelessSupervisorAgent(client, store, task, model=model)
    packet = agent.build_packet(wake_sequence=7, current_summary="check")
    try:
        if role == "runtime":
            await agent.decide(packet)
        elif role == "cheap_runtime":
            await CheapRuntimeReviewer(client, workspace, model=model).review(packet)
        elif role == "completion_review":
            await agent.decide_completion(packet)
        else:
            packet.adversary_report = AdversaryReport(
                report_text="No material finding.", generation=0, completion_wake_sequence=7,
                created_at="2026-09-24T00:00:00Z",
            )
            await agent.decide_adv_report(packet)
        starts = [params for backend in backends.values() for method, params in backend.calls
                  if method == "thread/start"]
        assert len(starts) == 1
        actual = starts[0]
        review = role in {"completion_review", "adv_report_controller"}
        assert actual["belloRole"] == ("completion_review" if review else "runtime")
        assert actual["asyncTools"] is (enabled and review)
        assert (ASYNC_TOOLS_GUIDANCE in actual.get("developerInstructions", "")) is (enabled and review)
        assert actual["distillerEnabled"] is False
        assert actual["config"]["agents"]["enabled"] is False
    finally:
        await agent.close_completion_review()
        await client.stop()
