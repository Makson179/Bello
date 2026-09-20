"""Offline controller checks for independent runtime, completion and adversary roles."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.appserver import AppServerMessage
from supervisor.controller import BelloController
from supervisor.project_config import LogDistillerConfig, ProjectConfig
from supervisor.schemas import (
    AdvReportControllerDecision,
    BelloStatus,
    CoderMessage,
    SupervisorDecision,
    TriggeringAction,
)
from supervisor.tui import UserCommand
from supervisor.state import RUNTIME_METRICS
from tests.test_bello_state import _FakeTUI, _runtime_controller
from tests.test_runtime_preflight import controller_for


@pytest.mark.parametrize("completion,adversary", [(False, False), (True, False), (False, True), (True, True)])
async def test_runtime_off_preflight_excludes_runtime_and_cheap_model_calls(tmp_path, monkeypatch, completion, adversary):
    controller, events = controller_for(tmp_path, monkeypatch, completion=completion, adversary=adversary)
    controller.runtime_enabled = False
    await controller._runtime_preflight()
    models = {params["model"] for method, params in (event for event in events if isinstance(event, tuple))}
    expected = {"test/coder"}
    if completion:
        expected.update({"test/completion", "test/revision_coder"})
    if adversary:
        expected.add("test/adversary")
    assert models == set(controller.client.required_models) == expected
    assert "paid-self-test" not in events
    assert "triage" not in events
    assert "sandbox" in events


@pytest.mark.parametrize("runtime,completion,adversary", [
    (runtime, completion, adversary)
    for runtime in (False, True) for completion in (False, True) for adversary in (False, True)
])
async def test_startup_constructs_only_enabled_reviewers_and_configures_engine_first(
    tmp_path, monkeypatch, runtime, completion, adversary,
):
    task = tmp_path / "TASK.md"
    task.write_text("Implement the requested behavior.", encoding="utf-8")
    order = []

    async def start():
        order.append("start")

    client = SimpleNamespace(
        configure_run=Mock(side_effect=lambda **kwargs: order.append(("configure", kwargs))),
        start=start, initialize=AsyncMock(), stop=AsyncMock(),
        thread_start=AsyncMock(return_value={"thread": {"id": "coder-thread"}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "coder-turn"}}),
    )
    config = ProjectConfig(task="TASK.md", completion_review=completion, adversary=adversary)
    controller = BelloController(
        tmp_path, task_path=task, client=client, tui=_FakeTUI(), project_config=config,
        runtime_enabled=runtime, completion_review=completion, adversary_enabled=adversary,
        completion_model="gpt-5.6-terra", adversary_model="gpt-5.6-luna",
    )
    monkeypatch.setattr(controller, "preflight", AsyncMock())
    monkeypatch.setattr(controller, "_prepare_coder_workspace", lambda: None)
    monkeypatch.setattr(controller, "event_loop", AsyncMock())
    await controller.run()

    assert order[0][0] == "configure"
    assert order[0][1]["runtime_enabled"] is runtime
    assert order[1] == "start"
    assert (controller.supervisor is not None) is runtime
    assert (controller.completion_supervisor is not None) is completion
    assert (controller.adv_report_controller is not None) is adversary
    if adversary:
        assert controller.adv_report_controller.model == ("gpt-5.6-terra" if completion else "gpt-5.6-luna")
    assert controller.store.get_bello_config().runtime_enabled is runtime
    assert client.thread_start.await_count == 1
    assert client.turn_start.await_count == 1


async def test_off_runtime_calls_and_queues_stay_inert_even_with_an_agent_attached(tmp_path):
    controller, _, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    cheap = SimpleNamespace(review=AsyncMock())
    controller.runtime_triage_reviewer = cheap
    controller._schedule_supervisor_check("Runtime trigger (timeout): failed")
    controller._queue_supervisor_check("runtime retry", completion_review=False)
    await controller._run_supervisor_check("runtime retry", None, None, None, None)
    await controller._structured_output_self_test()
    await controller._cheap_runtime_structured_output_self_test(cheap)
    assert await controller._cheap_runtime_route(None) is None
    await controller._configure_runtime_triage()
    applied = await controller.apply_supervisor_decision(
        SupervisorDecision(decision="intervene", reason="heuristic", message_to_coder="change course"),
        packet_thread_id="thread",
    )
    wake = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(kind="commandExecution", command="pytest", exit_code=1, status="failed", summary="pytest failed"),
        validation=None, changed_files=[], validation_trigger_reasons=("validation_regression",),
    )
    assert not wake.should_wake
    assert applied is False
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []
    cheap.review.assert_not_awaited()


@pytest.mark.parametrize("completion,adversary", [(False, False), (True, False), (False, True)])
async def test_runtime_off_readiness_skips_validation_heuristic_and_reaches_selected_review(
    tmp_path, monkeypatch, completion, adversary,
):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    controller.completion_review = completion
    controller.adversary_enabled = adversary
    controller.completion_supervisor = fake if completion else None
    controller.supervisor = None
    controller.adv_report_controller.build_packet = fake.build_packet
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=10)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"last_relevant_edit_sequence": 9}))
    validation_gate = AsyncMock(side_effect=AssertionError("disabled runtime gate was invoked"))
    monkeypatch.setattr(controller, "_done_without_fresh_behavioral_validation", validation_gate)
    adversary_run = AsyncMock()
    monkeypatch.setattr(controller, "_run_adversary_before_complete", adversary_run)

    await controller._handle_coder_turn_completed(item_id="ready")
    if controller._supervisor_task is not None:
        await controller._supervisor_task

    validation_gate.assert_not_awaited()
    assert fake.runtime_packets == []
    assert len(fake.completion_packets) == int(completion)
    assert adversary_run.await_count == int(adversary)
    if not completion and not adversary:
        assert store.get_bello_config().status == BelloStatus.COMPLETE


async def test_runtime_off_explicit_escape_is_denied_without_reviewer_or_coder_steering(tmp_path, monkeypatch):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    controller.client = SimpleNamespace(respond=AsyncMock())
    controller.coder = SimpleNamespace(steer_or_start=AsyncMock())
    approval = AsyncMock(side_effect=AssertionError("runtime approval model was called"))
    monkeypatch.setattr(controller, "decide_approval", approval)
    request = AppServerMessage({
        "id": "outside", "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "thread", "turnId": "turn", "command": "curl https://example.com",
                   "cwd": str(tmp_path), "availableDecisions": ["accept", "decline"]},
    })
    await controller.handle_server_request(request)
    controller.client.respond.assert_awaited_once_with("outside", {"decision": "decline"})
    approval.assert_not_awaited()
    controller.coder.steer_or_start.assert_not_awaited()
    assert fake.runtime_packets == []
    assert json.loads(store.path(RUNTIME_METRICS).read_text())["approval_requests_total"] == 1


async def test_runtime_off_human_message_goes_to_coder(tmp_path, monkeypatch):
    controller, _, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    deliver = AsyncMock(return_value=(True, "turn"))
    monkeypatch.setattr(controller, "_deliver_coder_message", deliver)
    await controller.handle_user_command(UserCommand("Also handle empty input."))
    deliver.assert_awaited_once_with("Also handle empty input.")
    assert controller._supervisor_task is None
    assert fake.runtime_packets == []


async def test_adversary_only_full_report_cycle_can_finish_without_completion_model(tmp_path, monkeypatch):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    controller.completion_review = False
    controller.adversary_enabled = True
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.adv_report_controller.build_packet = fake.build_packet
    controller.client = object()
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=1)

    class Adversary:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, packet, **kwargs):
            return SimpleNamespace(report_text="No defects found.", thread_id="adversary", turn_id="adv-turn", candidate_finding=False)

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", Adversary)
    await controller._handle_coder_turn_completed(item_id="ready")
    await controller._supervisor_task
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.get_bello_config().adversary_run_count == 1
    assert len(controller.adv_report_controller.packets) == 1
    assert fake.completion_packets == fake.runtime_packets == []
    assert controller._accepted_completion_decision is None
    assert "adversary-only review" in store.path("FINAL_REPORT.md").read_text()


async def test_revision_and_transport_restart_preserve_runtime_and_distiller_flags(tmp_path, monkeypatch):
    controller, store, _ = _runtime_controller(tmp_path)
    bundle = LogDistillerConfig(enabled=True, model_path="local-distiller-bundle")
    controller.runtime_enabled = False
    controller.log_distiller = bundle
    controller.completion_review = False
    controller.adversary_enabled = True
    controller.declared_grading_roots = ()
    controller.coder = SimpleNamespace(thread_id="thread")
    client = SimpleNamespace(
        thread_start=AsyncMock(return_value={"thread": {"id": "revision-thread"}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "revision-turn"}}),
        restart=AsyncMock(), initialize=AsyncMock(),
    )
    controller.client = client
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "runtime_enabled": False, "log_distiller": bundle.to_json_data(),
        "revision_coder_enabled": True, "revision_coder_mod": "gpt-5.6-luna",
        "revision_coder_intelligence": "high",
    }))
    monkeypatch.setattr(controller, "_quiesce_coder_tree", AsyncMock())
    await controller._perform_revision_coder_switch("Fix empty input handling.", source="adversary_report_controller")
    await controller._restart_app_server_client()
    config = store.get_bello_config()
    assert config.revision_coder_active is True
    assert config.coder_thread_id == "revision-thread"
    assert config.runtime_enabled is False
    assert config.log_distiller == bundle.to_json_data()
    assert controller._runtime_enabled() is False
    assert controller._log_distiller_config() == bundle
    assert controller.client is client
    assert client.thread_start.await_count == client.turn_start.await_count == 1
    client.restart.assert_awaited_once()
    assert "Fix empty input handling." in client.turn_start.call_args.args[0]["input"][0]["text"]


def test_cli_run_flags_survive_initialization_and_model_persistence(tmp_path):
    task = tmp_path / "TASK.md"
    task.write_text("Build the feature.", encoding="utf-8")
    bundle = LogDistillerConfig(enabled=True, model_path="local-distiller-bundle")
    controller = BelloController(
        tmp_path, task_path=task, project_config=ProjectConfig(task="TASK.md"),
        runtime_enabled=False, log_distiller=bundle,
    )
    controller.initialize_state()
    controller._persist_model_config()
    config = controller.store.get_bello_config()
    assert config.runtime_enabled is False
    assert config.cheap_runtime is False
    assert config.log_distiller == bundle.to_json_data()


async def test_adversary_only_findings_return_then_budget_finishes_after_coder_revision(tmp_path, monkeypatch):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.runtime_enabled = False
    controller.completion_review = False
    controller.adversary_enabled = True
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.adv_report_controller.build_packet = fake.build_packet
    controller.adv_report_controller.decisions = [AdvReportControllerDecision(
        forward_to_coder=True, reason="missing edge behavior",
        report_to_coder="## Findings requiring correction\n- Fix empty input handling.",
    )]
    controller.client = object()
    controller.coder = SimpleNamespace(thread_id="thread", steer_or_start=AsyncMock(return_value="revision-turn"))
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=1)

    class Adversary:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, packet, **kwargs):
            return SimpleNamespace(report_text="Empty input fails.", thread_id="adversary", turn_id="adv-turn", candidate_finding=True)

    monkeypatch.setattr("supervisor.controller.AdversaryAgent", Adversary)
    await controller._handle_coder_turn_completed(item_id="ready")
    await controller._supervisor_task
    assert controller.coder.steer_or_start.await_count == 1
    assert store.get_bello_config().status != BelloStatus.COMPLETE
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=10)
    await controller._handle_coder_turn_completed(item_id="revised-ready")
    await controller._supervisor_task
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.get_bello_config().adversary_run_count == 1
    assert fake.completion_packets == fake.runtime_packets == []
