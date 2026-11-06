"""Goal with its own runtime switch off: no runtime-model calls, the rest of Goal unchanged.

Offline; no provider or model calls. Where the default (goal_runtime on) would reach
a runtime model, the same rig is checked with it on as a control.
"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

import supervisor.main as main_module
from supervisor.config_editor import _replace_config_field, parameter_defs
from supervisor.appserver import AppServerMessage
from supervisor.config_validation import project_profiles
from supervisor.controller import BelloController
from supervisor.controller_recovery import DurableRun, RunOwner
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    LogDistillerConfig,
    ProjectConfig,
    ProjectConfigError,
    changed_project_config_fields,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.schemas import (
    AppEventSource,
    BelloConfig,
    BelloStatus,
    CoderMessage,
    SupervisorDecision,
    TriggeringAction,
)
from supervisor.state import RUNTIME_METRICS, StateStore
from tests.support.controller import _FakeTUI, _runtime_controller
from tests.test_config_advisor_060 import INSPECTOR, config as advisor_config, validate
from tests.test_controller_process_recovery import _bytes, _controller as _recovery_controller
from tests.test_goal_adversary_checkpoint import _checkpoint_controller, _install_adversary, _readiness
from tests.test_goal_auto_next_review import _OwnedCoder, _completed
from tests.test_goal_config import _invoke
from tests.test_goal_mode import _decision
from tests.test_runtime_preflight import controller_for


def _rows(config):
    return {parameter.key: parameter for parameter in parameter_defs(config)}


# Configuration, CLI and editor.

def test_goal_runtime_defaults_on_and_is_dormant_without_goal():
    assert ProjectConfig().goal_runtime is True
    assert BelloConfig(project_root="fixture", task_path="TASK.md").goal_runtime is True
    assert ProjectConfig().to_json_data()["goal_runtime"] is True
    # The default keeps Goal's mandatory runtime, whatever runtime_enabled saved.
    assert ProjectConfig(goal=True, runtime_enabled=False).effective_runtime_enabled is True
    off = ProjectConfig(goal=True, goal_runtime=False, cheap_runtime=True)
    assert off.effective_runtime_enabled is off.effective_cheap_runtime is False
    assert off.effective_completion_review is True
    assert ProjectConfig(goal_runtime=False).effective_runtime_enabled is True
    assert ProjectConfig(goal_runtime=False, runtime_enabled=False).effective_runtime_enabled is False


def test_goal_runtime_loads_strictly_and_syncs_run_state(tmp_path):
    invalid = tmp_path / "invalid"
    path = project_config_path(invalid)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"goal": True, "goal_runtime": "false"}), encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="goal_runtime must be true or false"):
        load_project_config(invalid, create=False)

    before = ProjectConfig(goal=True, cheap_runtime=True)
    after = replace(before, goal_runtime=False)
    assert "goal_runtime" in changed_project_config_fields(before, after)
    save_project_config(tmp_path, after)
    state = StateStore(tmp_path).get_bello_config()
    assert state.goal is True and state.goal_runtime is False
    # Cheap triage follows the effective runtime; the saved non-Goal preference is kept.
    assert state.cheap_runtime is False and state.runtime_enabled is True
    reloaded = load_project_config(tmp_path, create=False)
    assert reloaded.goal_runtime is False and reloaded.runtime_enabled is True


def test_offline_profiles_drop_only_the_runtime_role():
    roles = {use.role for use in project_profiles(ProjectConfig(goal=True, goal_runtime=False))}
    assert "runtime" not in roles and {"coder", "completion"} <= roles
    assert "runtime" in {use.role for use in project_profiles(ProjectConfig(goal=True, runtime_enabled=False))}


def test_run_settings_turn_runtime_off_only_in_goal(monkeypatch):
    roles = []
    monkeypatch.setattr(main_module, "_validate_model_intelligence", lambda role, *_args: roles.append(role))
    settings = _resolve_run_settings(project_config=ProjectConfig(goal=True), goal_runtime=False)
    assert settings.goal is True and settings.goal_runtime is False
    assert settings.runtime_enabled is False and settings.completion_review is True
    assert settings.runtime_choice is True and settings.goal_notices == ()
    assert roles == ["coder", "completion"]

    # A saved (possibly dormant) false is announced, never silently applied.
    saved = _resolve_run_settings(project_config=ProjectConfig(goal=True, goal_runtime=False))
    assert saved.runtime_enabled is False
    assert saved.goal_notices == (
        "Goal runs without runtime supervision (saved goal_runtime=false): network is enabled inside the "
        "filesystem sandbox and outside-sandbox escalation is unavailable; --goal-runtime turns it on for this run.",
    )

    explicit = _resolve_run_settings(project_config=ProjectConfig(goal=True), goal_runtime=False, runtime_enabled=True)
    assert explicit.runtime_enabled is False
    assert explicit.goal_notices == ("Goal runtime supervision is off (goal_runtime=false); ignoring --runtime.",)
    restored = _resolve_run_settings(project_config=ProjectConfig(goal=True, goal_runtime=False), goal_runtime=True)
    assert restored.runtime_enabled is True and restored.goal_runtime is True
    dormant = _resolve_run_settings(project_config=ProjectConfig(goal_runtime=False))
    assert dormant.goal is False and dormant.runtime_enabled is True


@pytest.mark.parametrize("args,runtime,goal_runtime", [
    (["--goal", "--no-goal-runtime"], False, False),
    (["--goal", "--goal-runtime"], True, True),
    (["--goal", "--no-runtime"], True, True),
    (["--no-goal", "--no-goal-runtime"], True, False),
    (["--no-goal", "--no-goal-runtime", "--no-runtime"], False, False),
])
def test_cli_goal_runtime_flag_reaches_run_settings(tmp_path, monkeypatch, args, runtime, goal_runtime):
    settings, _ = _invoke(tmp_path, monkeypatch, args)
    assert settings.runtime_enabled is runtime and settings.goal_runtime is goal_runtime
    assert settings.completion_review is settings.goal


def test_cli_help_names_the_goal_runtime_switch():
    help_text = " ".join(CliRunner().invoke(cli, ["--help"]).output.split())
    assert "--goal-runtime / --no-goal-runtime" in help_text
    assert "runtime supervision follows --goal-runtime/--no-goal-runtime" in help_text
    assert "it has no effect while Goal is off" in help_text


async def test_run_bello_passes_goal_runtime_to_the_controller(tmp_path, monkeypatch):
    captured = {}

    class FakeController:
        def __init__(self, _root, **kwargs):
            captured.update(kwargs)
            self.store = SimpleNamespace(get_bello_config=lambda: SimpleNamespace(status=BelloStatus.COMPLETE))

        async def run(self):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "BelloController", FakeController)
    monkeypatch.setattr(main_module, "load_project_config", lambda *_args, **_kwargs: ProjectConfig())
    settings = _resolve_run_settings(project_config=ProjectConfig(goal=True, goal_runtime=False))
    assert await main_module._run_bello(settings) == 0
    assert captured["goal"] is True and captured["goal_runtime"] is False
    # The saved non-Goal runtime choice is what the controller records.
    assert captured["runtime_enabled"] is True and captured["completion_review"] is True


def test_config_output_reports_goal_runtime_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    monkeypatch.setattr(main_module, "run_config_editor",
                        lambda root: ProjectConfig(goal=True, goal_runtime=False))
    result = CliRunner().invoke(cli, ["config"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "goal-runtime: false" in lines and "runtime: false (goal-runtime off)" in lines
    assert "completion-review: true (required by goal)" in lines
    assert not any(line.startswith(("runtime-mod:", "runtime-intelligence:", "cheap-runtime:")) for line in lines)


def test_editor_goal_runtime_row_hides_the_runtime_profile():
    rows = _rows(ProjectConfig(goal=True, goal_runtime=False, cheap_runtime=True))
    assert rows["goal_runtime"].value == "false"
    assert [option.value for option in rows["goal_runtime"].options] == [True, False]
    assert rows["runtime_enabled"].value == "false (goal-runtime)"
    assert rows["runtime_enabled"].options == () and "goal-runtime" in rows["runtime_enabled"].help_text
    assert not {"runtime_mod", "runtime_intelligence", "cheap_runtime"} & set(rows)
    assert {"completion_mod", "completion_intelligence"} <= set(rows)

    default = _rows(ProjectConfig(goal=True))
    assert default["goal_runtime"].value == "true" and default["runtime_enabled"].value == "true (goal)"
    assert {"runtime_mod", "runtime_intelligence", "cheap_runtime"} <= set(default)
    assert "goal_runtime" not in _rows(ProjectConfig())
    assert _rows(ProjectConfig(goal_runtime=False))["goal_runtime"].value == "false (inactive)"
    changed = _replace_config_field(ProjectConfig(goal=True), "goal_runtime", False)
    assert changed == ProjectConfig(goal=True, goal_runtime=False)


def test_advisor_inspects_and_validates_goal_runtime():
    payload = advisor_config()
    payload.pop("goal_runtime")
    assert INSPECTOR._normalize(payload, config_exists=True)["goal_runtime"] is True
    payload["goal_runtime"] = "false"
    current = INSPECTOR._normalize(payload, config_exists=True)
    assert "goal_runtime must be boolean" in INSPECTOR._source_config_errors(payload, current, config_exists=True)

    goal = advisor_config()
    goal.update(goal=True, max_completion_returns_before_adversary="unlimited",
                max_completion_returns_after_adversary="unlimited")
    assert validate(goal) == []
    # runtime_enabled stays the dormant non-Goal preference with either value.
    off = dict(goal, goal_runtime=False)
    assert validate(off) == validate(dict(off, runtime_enabled=False)) == []
    assert validate(dict(off, cheap_runtime=True)) == ["cheap_runtime: must be false when goal_runtime=false"]
    assert "goal: requires completion_review=true" in validate(dict(off, completion_review=False))
    assert "goal_runtime: expected boolean" in validate(dict(off, goal_runtime="false"))
    # Older recommendations omit the field and keep Goal's mandatory runtime.
    legacy = dict(goal)
    legacy.pop("goal_runtime")
    assert validate(legacy) == []


# Controller behavior.

def _runtime_off(controller):
    controller.goal = True
    controller.goal_runtime = False
    assert controller._goal_enabled() is True and controller._runtime_enabled() is False


@pytest.mark.parametrize("completion,adversary", [(True, False), (True, True)])
async def test_preflight_selects_and_self_tests_no_runtime(tmp_path, monkeypatch, completion, adversary):
    c, events = controller_for(tmp_path, monkeypatch, completion=completion, adversary=adversary)
    _runtime_off(c)
    await c._runtime_preflight()
    selected = set(c.client.required_models)
    assert "test/runtime" not in selected and "test/coder" in selected
    assert ("test/completion" in selected) == completion and ("test/adversary" in selected) == adversary
    assert "paid-self-test" not in events and "triage" not in events and "sandbox" in events

    control, control_events = controller_for(tmp_path, monkeypatch, completion=completion, adversary=adversary)
    control.goal = True
    await control._runtime_preflight()
    assert "test/runtime" in set(control.client.required_models)
    assert "paid-self-test" in control_events and "triage" in control_events


async def test_startup_runs_goal_with_the_ordinary_runtime_off_policy(tmp_path, monkeypatch):
    task = tmp_path / "TASK.md"
    task.write_text("Synthetic test.", encoding="utf-8")
    client = SimpleNamespace(configure_run=Mock(), start=AsyncMock(), initialize=AsyncMock(), stop=AsyncMock(),
                             thread_start=AsyncMock(return_value={"thread": {"id": "coder-thread"}}),
                             turn_start=AsyncMock(return_value={"turn": {"id": "coder-turn"}}))
    c = BelloController(tmp_path, task_path=task, client=client, tui=_FakeTUI(),
                        project_config=ProjectConfig(task="TASK.md", goal=True, goal_runtime=False,
                                                     completion_review=True, adversary=True),
                        goal=True, goal_runtime=False, completion_review=True, adversary_enabled=True)
    monkeypatch.setattr(c, "preflight", AsyncMock())
    monkeypatch.setattr(c, "_prepare_coder_workspace", lambda: None)
    monkeypatch.setattr(c, "event_loop", AsyncMock())
    await c.run()
    assert client.configure_run.call_args.kwargs["runtime_enabled"] is False
    assert c.supervisor is None and c.completion_supervisor is not None
    assert c.adv_report_controller is not None and c.approvals.supervisor is None
    state = c.store.get_bello_config()
    assert state.goal is True and state.goal_runtime is False
    assert state.runtime_enabled is True and state.cheap_runtime is False


async def test_no_runtime_triage_or_goal_progress_review_model_calls(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    c, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "last_event_sequence": 10}))
    c.coder = SimpleNamespace(thread_id="thread", active_turn_id=None, steer_or_start=AsyncMock(return_value="turn"))
    _runtime_off(c)
    cheap = SimpleNamespace(review=AsyncMock())
    c.runtime_triage_reviewer = cheap

    c._record_goal_work(1)
    c._maybe_schedule_goal_progress_review()
    clock[0] = 9000
    c._record_goal_work(2)
    c._maybe_schedule_goal_progress_review()
    c._schedule_supervisor_check("Runtime trigger (timeout): failed")
    c._queue_supervisor_check("runtime retry", completion_review=False)
    await c._run_supervisor_check("runtime retry", None, None, None, None)
    await c._structured_output_self_test()
    await c._cheap_runtime_structured_output_self_test(cheap)
    assert await c._cheap_runtime_route(None) is None
    await c._configure_runtime_triage()
    assert c.runtime_triage_reviewer is None and c._cheap_runtime_enabled() is False
    wake = c.should_wake_runtime_supervisor(
        action=TriggeringAction(kind="commandExecution", command="pytest", exit_code=1, status="failed",
                                summary="failed"),
        validation=None, changed_files=[], validation_trigger_reasons=("validation_regression",))
    assert not wake.should_wake and c._supervisor_task is None
    assert not await c.apply_supervisor_decision(
        SupervisorDecision(decision="intervene", reason="old", message_to_coder="old"), packet_thread_id="thread")
    with pytest.raises(Exception, match="supervisor not ready"):
        await c.decide_approval(SimpleNamespace(), "requires judgment")
    assert reviewer.runtime_packets == [] and reviewer.completion_packets == []
    cheap.review.assert_not_awaited()
    c.coder.steer_or_start.assert_not_awaited()

    # Control: the same Goal progress clock reaches the runtime model with the default switch.
    c.goal_runtime = True
    c._record_goal_work(3)
    c._maybe_schedule_goal_progress_review()
    clock[0] = 20000
    c._record_goal_work(4)
    c._maybe_schedule_goal_progress_review()
    await c._supervisor_task
    assert len(reviewer.runtime_packets) == 1 and reviewer.runtime_packets[0].goal_progress_review


async def test_human_message_goes_straight_to_the_coder(tmp_path):
    c, store, reviewer = _runtime_controller(tmp_path)
    _runtime_off(c)
    c._deliver_coder_message = AsyncMock(return_value=(True, None))
    await c.handle_user_command(SimpleNamespace(kind="message", text="Please also cover the empty input."))
    c._deliver_coder_message.assert_awaited_once_with("Please also cover the empty input.")
    assert reviewer.runtime_packets == [] and c._supervisor_task is None


@pytest.fixture
def goal_off(tmp_path, monkeypatch):
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "goal": True, "goal_runtime": False, "status": BelloStatus.RUNNING, "active_coder_turn_id": "old-turn",
    }))
    controller.coder = _OwnedCoder(store)
    controller._generation_has_coder_turn = True
    monkeypatch.setattr(controller, "_track_subagent_notification", AsyncMock())
    monkeypatch.setattr(controller, "_refresh_coder_subagents", AsyncMock())
    monkeypatch.setattr(controller, "_repair_snapshot_runtime_controls", Mock(return_value=False))
    monkeypatch.setattr(controller, "_escalate_runtime_integrity_issue", AsyncMock(return_value=False))
    monkeypatch.setattr(controller, "_write_run_checkpoint", Mock())
    # The run state alone (no per-run override) turns Goal's runtime off.
    assert controller._goal_enabled() and not controller._runtime_enabled()
    return controller, store, reviewer


async def test_successful_turn_still_continues_automatically(goal_off):
    c, _, reviewer = goal_off
    await c.handle_notification(_completed())
    assert len(c.coder.starts) == 1 and "original Goal" in c.coder.starts[0][1]
    assert c._supervisor_task is None and reviewer.runtime_packets == []


async def test_readiness_marker_still_starts_completion_review_without_runtime_gate(goal_off, monkeypatch):
    c, _, reviewer = goal_off
    gate = AsyncMock(return_value="coder marked done without a trusted fresh behavioral validation")
    monkeypatch.setattr(c, "_done_without_fresh_behavioral_validation", gate)
    monkeypatch.setattr(c, "_schedule_supervisor_check", Mock())
    c.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=3)
    await c.handle_notification(_completed())
    # The runtime-only validation gate does not run; the configured final review does.
    gate.assert_not_awaited()
    assert c._schedule_supervisor_check.call_args.kwargs["completion_review"] is True
    assert c.coder.starts == [] and reviewer.runtime_packets == []


async def test_idle_goal_without_runtime_never_probes_nudges_or_reviews(goal_off):
    c, _, reviewer = goal_off
    c.coder.active_turn_id = None
    c.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": None}))
    c.client.read_execution_status = AsyncMock(side_effect=AssertionError("no idle probe in Goal"))
    for _ in range(3):
        await c._handle_controller_idle_guard()
    assert c.coder.starts == [] and c._supervisor_task is None
    assert reviewer.runtime_packets == [] and reviewer.completion_packets == []


async def test_adversary_checkpoint_and_completion_work_without_runtime(tmp_path, monkeypatch):
    controller, store, reviewer = _checkpoint_controller(tmp_path)
    controller.goal_runtime = False
    assert controller._runtime_enabled() is False
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal_adversary_after_returns": 1}))
    controller._queue_supervisor_check = Mock()
    adversary_calls = _install_adversary(monkeypatch)

    await _readiness(controller)
    assert len(reviewer.completion_packets) == 1 and adversary_calls == []
    await _readiness(controller)
    assert len(reviewer.completion_packets) == 1 and len(adversary_calls) == 1
    controller._queue_supervisor_check.assert_called_once_with(
        "Goal completion after adversary", completion_review=True,
    )
    reviewer.decide_completion = AsyncMock(side_effect=lambda packet: _decision(
        "accept", packet.wake_sequence, "Original objective satisfied after adversary",
    ))
    await _readiness(controller)
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs == {
        "status": BelloStatus.COMPLETE, "completion_review_accepted": True,
    }
    assert reviewer.runtime_packets == []


async def test_explicit_escalation_is_denied_without_a_model_or_coder_steering(tmp_path, monkeypatch):
    controller, store, reviewer = _runtime_controller(tmp_path)
    _runtime_off(controller)
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
    assert reviewer.runtime_packets == []
    assert json.loads(store.path(RUNTIME_METRICS).read_text())["approval_requests_total"] == 1


async def test_revision_and_transport_restart_keep_goal_runtime_off(tmp_path, monkeypatch):
    controller, store, _ = _runtime_controller(tmp_path)
    bundle = LogDistillerConfig(enabled=True, model_path="local-distiller-bundle")
    controller.goal = True
    controller.log_distiller = bundle
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
        "goal": True, "goal_runtime": False, "log_distiller": bundle.to_json_data(),
        "revision_coder_enabled": True, "revision_coder_mod": "gpt-5.6-luna",
        "revision_coder_intelligence": "high",
    }))
    monkeypatch.setattr(controller, "_quiesce_coder_tree", AsyncMock())
    await controller._perform_revision_coder_switch("Fix empty input handling.", source="adversary_report_controller")
    await controller._restart_app_server_client()
    config = store.get_bello_config()
    assert config.revision_coder_active is True and config.coder_thread_id == "revision-thread"
    assert config.goal is True and config.goal_runtime is False
    assert controller._runtime_enabled() is False and controller._cheap_runtime_enabled() is False
    client.restart.assert_awaited_once()


def _saved_goal_run(project, *, goal_runtime):
    (project / "TASK.md").write_text("Implement the task.\n")
    (project / "solution.py").write_text("VALUE = 0\n")
    controller = _recovery_controller(project, goal=True, goal_runtime=goal_runtime)
    with RunOwner(project, controller=True):
        controller._durable_run = DurableRun(controller)
        controller.initialize_state()
        controller._prepare_coder_workspace()
        controller.store.update_bello_config(lambda cfg: cfg.model_copy(update={
            "status": BelloStatus.RUNNING, "coder_thread_id": "persisted-coder-thread"}))
        controller._append_event(AppEventSource.SYSTEM, "fixture/observed")
        controller._write_run_checkpoint("coder", state="active")
        assert controller._durable_run.record.eligible
    return controller


@pytest.mark.parametrize("restored_goal_runtime,error", [
    (True, "effective run configuration changed"),
    # Unchanged: the configuration binding passes and only the missing tree fence refuses.
    (False, "fenc"),
])
def test_recovery_is_bound_to_the_goal_runtime_choice(tmp_path, restored_goal_runtime, error):
    saved = _saved_goal_run(tmp_path, goal_runtime=False)
    try:
        assert saved.store.get_bello_config().goal_runtime is False
        before = _bytes(saved.store.state_dir)
        restored = _recovery_controller(tmp_path, goal=True, goal_runtime=restored_goal_runtime)
        with pytest.raises(RuntimeError, match=error):
            with RunOwner(tmp_path, controller=True):
                restored._durable_run = DurableRun(restored)
                restored.initialize_state()
        assert _bytes(saved.store.state_dir) == before
    finally:
        snapshot = saved._coder_snapshot
        if snapshot is not None and snapshot.temp_root.exists():
            snapshot.cleanup()
