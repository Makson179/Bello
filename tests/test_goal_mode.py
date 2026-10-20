"""Goal-mode controller, prompt and lifecycle contracts, without providers or model calls.

While Goal is on: runtime supervision and completion review are mandatory, completion
returns are unlimited before AND after the optional adversary, the adversary's prompt and
configured pass count are unchanged, and only the completion/coder prompts gain brief
guidance. Everything else (Smart Execution, revision coder, NNC, log distiller, and all
ordinary non-Goal review semantics) is untouched.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from supervisor.coder import CoderSession
from supervisor.controller import TRANSPORT_RECOVERY_CODER_PROMPT, BelloController
from supervisor.project_config import ProjectConfig, load_project_config
from supervisor.prompts import (
    build_adv_report_controller_prompt,
    build_adversary_prompt,
    build_coder_prompt,
    build_completion_review_prompt,
    build_restart_prompt,
    build_revision_prompt,
    build_stateless_supervisor_prompt,
)
from supervisor.prompts.supervisor import GOAL_CODER_GUIDANCE, GOAL_COMPLETION_GUIDANCE
from supervisor.schemas import AdversaryReport, BelloConfig, BelloStatus, CompletionReviewDecision
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent
from tests.support.controller import (
    _FakeTUI,
    _completion_gate_controller,
    _gate_packet,
    _runtime_controller,
)


def _decision(kind, wake, reason="Required behavior missing"):
    returning = kind == "return"
    return CompletionReviewDecision(
        decision=kind, reason=reason,
        uncovered_behaviors=["Required behavior missing"] if returning else [],
        message_to_coder="Implement the missing behavior" if returning else None,
        persistent_decision=None, progress_update=None, clear_handoff=False,
        display_message=None, handoff=None, wake_sequence=wake, generation=0,
    )


# --- return budgets: unlimited under Goal, byte-for-byte unchanged otherwise ----------------


@pytest.mark.parametrize("adversary,passes,expected", [
    (False, 0, "complete"), (True, 0, "adversary"), (True, 1, "complete"),
])
@pytest.mark.parametrize("goal", [False, True])
@pytest.mark.parametrize("runtime", [False, True])
def test_goal_removes_only_the_completion_return_cap(tmp_path, adversary, passes, expected, goal, runtime):
    controller, store, _ = _runtime_controller(tmp_path)
    controller.goal = goal
    controller.runtime_enabled = runtime
    controller.adversary_enabled = adversary
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_completion_returns_before_adversary": 0,
        "max_completion_returns_after_adversary": 0,
        "completion_return_count": 1000,
        "completion_returns_since_adversary": 1000,
        "max_adversary_runs": 1,
        "adversary_run_count": passes,
    }))
    assert controller._completion_review_budget_action() == (None if goal else expected)
    assert controller._effective_max_adversary_runs() == (1 if adversary else 0)
    cfg = store.get_bello_config()
    assert cfg.adversary_run_count == passes and cfg.max_adversary_runs == 1
    assert cfg.max_completion_returns_before_adversary == 0  # nothing is rewritten


@pytest.mark.parametrize("before,after", [(1, 0), (2, 1), (0, 0), (3, 3)])
@pytest.mark.parametrize("scenario", ["no_adversary", "before_adversary", "after_adversary"])
def test_ordinary_review_limits_are_unchanged_without_goal(tmp_path, scenario, before, after):
    controller, store, _ = _runtime_controller(tmp_path)
    controller.goal = False
    controller.adversary_enabled = scenario != "no_adversary"
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_completion_returns_before_adversary": before,
        "max_completion_returns_after_adversary": after,
        "max_adversary_runs": 2,
        "adversary_run_count": 1 if scenario == "after_adversary" else 0,
    }))
    for returns in range(0, 6):
        store.update_bello_config(lambda cfg, returns=returns: cfg.model_copy(update={
            "completion_return_count": returns, "completion_returns_since_adversary": returns,
        }))
        limit = after if scenario == "after_adversary" else before
        reached = returns >= limit
        if scenario == "no_adversary":
            expected = "complete" if reached else None
        elif scenario == "before_adversary":
            expected = "adversary" if reached else None
        else:
            expected = "adversary" if reached else None  # one pass remains (1 of 2 used)
        assert controller._completion_review_budget_action() == expected


async def test_goal_still_reviews_after_the_normal_completion_budget_is_exhausted(tmp_path):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.goal = True
    controller.adversary_enabled = False
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_completion_returns_before_adversary": 0, "completion_return_count": 1000,
    }))
    controller.apply_completion_decision = AsyncMock()
    controller._finalize_bounded_completion = AsyncMock()
    await controller._run_supervisor_check("goal ready", None, None, None, None, True)
    assert len(fake.completion_packets) == 1
    controller.apply_completion_decision.assert_awaited_once()
    controller._finalize_bounded_completion.assert_not_awaited()


async def test_without_goal_an_exhausted_budget_still_finalizes_without_another_review(tmp_path):
    controller, store, fake = _runtime_controller(tmp_path)
    controller.goal = False
    controller.adversary_enabled = False
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_completion_returns_before_adversary": 1, "completion_return_count": 1,
    }))
    controller.apply_completion_decision = AsyncMock()
    controller._finalize_bounded_completion = AsyncMock()
    await controller._run_supervisor_check("ready", None, None, None, None, True)
    assert fake.completion_packets == []
    controller._finalize_bounded_completion.assert_awaited_once()


async def test_goal_returns_are_unlimited_before_the_adversary_which_runs_only_after_an_accept(tmp_path):
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=[])
    controller.goal = True
    controller.adversary_enabled = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_adversary_runs": 1, "adversary_run_count": 0,
        "max_completion_returns_before_adversary": 1,  # a finite cap that Goal must ignore
        "max_completion_returns_after_adversary": 0,
    }))
    controller.finalize = AsyncMock()
    controller._run_adversary_before_complete = AsyncMock()
    for wake in range(1, 8):
        assert controller._completion_review_budget_action() is None
        await controller.apply_completion_decision(
            _decision("return", wake), packet_thread_id="thread",
            packet=_gate_packet(task, validations=[], wake_sequence=wake),
        )
    assert len(coder.messages) == 7
    assert store.get_bello_config().completion_return_count == 7
    controller._run_adversary_before_complete.assert_not_awaited()  # not scheduled by a spent budget
    controller.finalize.assert_not_awaited()
    await controller.apply_completion_decision(
        _decision("accept", 8, "Original objective satisfied"), packet_thread_id="thread",
        packet=_gate_packet(task, validations=[], wake_sequence=8),
    )
    controller._run_adversary_before_complete.assert_awaited_once()
    controller.finalize.assert_not_awaited()  # the configured adversary pass runs before completion


async def test_goal_returns_after_the_last_adversary_then_finishes_on_reviewer_accept(tmp_path):
    controller, store, task, coder = _completion_gate_controller(tmp_path, validations=[])
    controller.goal = True
    controller.adversary_enabled = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "max_adversary_runs": 1, "adversary_run_count": 1,
        "max_completion_returns_after_adversary": 0,
    }))
    controller.finalize = AsyncMock()
    controller._run_adversary_before_complete = AsyncMock()
    for wake in range(1, 5):
        returning = wake < 4
        assert controller._completion_review_budget_action() is None
        await controller.apply_completion_decision(
            _decision("return" if returning else "accept", wake,
                      "Required behavior missing" if returning else "Original objective satisfied"),
            packet_thread_id="thread",
            packet=_gate_packet(task, validations=[], wake_sequence=wake),
        )
        if returning:
            controller.finalize.assert_not_awaited()
    assert len(coder.messages) == 3
    assert store.get_bello_config().completion_returns_since_adversary == 3
    assert store.get_bello_config().adversary_run_count == 1  # the configured pass count is unchanged
    controller._run_adversary_before_complete.assert_not_awaited()
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs == {
        "status": BelloStatus.COMPLETE, "completion_review_accepted": True,
    }


@pytest.mark.parametrize("goal", [False, True])
async def test_adversary_pass_count_is_not_changed_by_goal(tmp_path, goal):
    controller, store, task, _ = _completion_gate_controller(tmp_path, validations=[])
    controller.goal = goal
    controller.adversary_enabled = True
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"max_adversary_runs": 2, "adversary_run_count": 2}))
    controller.finalize = AsyncMock()
    controller._run_adversary_before_complete = AsyncMock()
    assert controller._effective_max_adversary_runs() == 2
    assert not controller._adversary_runs_remaining()
    await controller.apply_completion_decision(
        _decision("accept", 1, "Original objective satisfied"), packet_thread_id="thread",
        packet=_gate_packet(task, validations=[], wake_sequence=1),
    )
    controller._run_adversary_before_complete.assert_not_awaited()  # no extra pass beyond the configured two
    controller.finalize.assert_awaited_once()
    assert controller.finalize.call_args.kwargs["completion_review_accepted"] is True


@pytest.mark.parametrize("goal", [False, True])
async def test_no_findings_adversary_cannot_replace_a_goal_completion_accept(tmp_path, goal):
    controller, store, task, _ = _completion_gate_controller(tmp_path, validations=[])
    controller.goal = goal
    controller._adv_report_controller_staleness_reason = Mock(return_value=None)
    controller._queue_supervisor_check = Mock()
    controller._finalize_bounded_completion = AsyncMock()
    report = AdversaryReport(report_text="No findings", candidate_finding=False,
                             generation=0, completion_wake_sequence=1, created_at="fixture")
    await controller._run_adv_report_controller(
        report, packet=_gate_packet(task, validations=[]), accepted_completion_decision=None,
    )
    assert len(controller.adv_report_controller.packets) == 1
    if goal:
        controller._queue_supervisor_check.assert_called_once_with(
            "Goal completion after adversary", completion_review=True,
        )
        controller._finalize_bounded_completion.assert_not_awaited()
        assert store.get_bello_config().status != BelloStatus.COMPLETE
    else:
        controller._queue_supervisor_check.assert_not_called()
        controller._finalize_bounded_completion.assert_awaited_once()


# --- mandatory runtime supervision and completion review -------------------------------------


def test_goal_forces_runtime_and_completion_review_whatever_is_saved_or_overridden(tmp_path):
    controller, _, _ = _runtime_controller(tmp_path)
    controller.project_config = ProjectConfig(goal=True, runtime_enabled=False, completion_review=False)
    controller.runtime_enabled = False  # --no-runtime
    controller.completion_review = False  # --completion-review false
    controller.adversary_enabled = False
    assert controller._goal_enabled()
    assert controller._runtime_enabled()
    assert controller._effective_completion_review()
    assert controller._post_coder_review_enabled()
    assert controller._effective_max_adversary_runs() == 0  # the adversary stays optional and off
    assert not controller._revision_coder_enabled()
    assert not controller._log_distiller_config().enabled
    assert not controller._no_need_for_context_config().enabled
    # Cheap triage rides on runtime: under Goal it follows the stored preference; without Goal it is off.
    for stored in (True, False):
        controller.store.update_bello_config(lambda cfg, stored=stored: cfg.model_copy(update={"cheap_runtime": stored}))
        assert controller._cheap_runtime_enabled() is stored
    # Without Goal the same saved/overridden choices are honored exactly as before.
    controller.goal = False
    assert not controller._runtime_enabled()
    assert not controller._effective_completion_review()
    assert not controller._cheap_runtime_enabled()


def test_goal_leaves_the_other_optional_features_exactly_as_configured(tmp_path):
    from supervisor.project_config import LogDistillerConfig, NoNeedForContextConfig

    controller, _, _ = _runtime_controller(tmp_path)
    for enabled in (False, True):
        controller.project_config = ProjectConfig(
            goal=True, revision_coder_enabled=enabled,
            no_need_for_context=NoNeedForContextConfig(enabled, 100000, 12.5),
            log_distiller=LogDistillerConfig(enabled=enabled), async_tools=enabled,
        )
        assert controller._revision_coder_enabled() is enabled
        assert controller._no_need_for_context_config() == NoNeedForContextConfig(enabled, 100000, 12.5)
        assert controller._log_distiller_config().enabled is enabled
        assert controller._async_tools_enabled() is enabled


def test_goal_loads_from_runtime_state_and_an_explicit_off_wins(tmp_path):
    controller, store, _ = _runtime_controller(tmp_path)
    assert not controller._goal_enabled()
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "runtime_enabled": False,
                                                                 "completion_review_enabled": False}))
    assert controller._goal_enabled()
    assert controller._runtime_enabled() and controller._effective_completion_review()
    controller.goal = False
    assert not controller._goal_enabled()
    assert not controller._runtime_enabled() and not controller._effective_completion_review()


def test_goal_never_changes_smart_execution(tmp_path):
    controller, store, _ = _runtime_controller(tmp_path)
    assert controller._async_tools_enabled() is True  # the default; no key and no Goal
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "async_tools": False}))
    assert controller._goal_enabled()
    assert not controller._async_tools_enabled()  # an explicit saved off stays off under Goal
    controller.async_tools = True
    assert controller._async_tools_enabled()  # the per-run switch still wins
    controller.async_tools = None
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"async_tools": True}))
    assert controller._async_tools_enabled()
    controller.project_config = ProjectConfig(goal=True, async_tools=False)
    assert not controller._async_tools_enabled()
    controller.project_config = ProjectConfig(goal=False, async_tools=False)
    assert not controller._async_tools_enabled()


def test_settings_summary_names_goal_only_when_it_is_on(tmp_path):
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    plain = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), runtime_enabled=False,
                            completion_review=False, adversary_enabled=False)
    text = plain._runtime_settings_summary()
    assert text.startswith("settings: task=TASK.md ") and "goal" not in text
    assert "runtime=false" in text and "completion-review=false" in text
    forced = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), runtime_enabled=False,
                             completion_review=False, adversary_enabled=False, goal=True)
    summary = forced._runtime_settings_summary()
    assert summary.startswith("settings: goal=true task=TASK.md ")
    assert " runtime=true " in summary and " completion-review=true " in summary
    # Goal changes only those mandatory roles (and the triage that rides on runtime), nothing else.
    assert " async-tools=true " in text and " async-tools=true " in summary
    assert " cheap-runtime=false " in text and " cheap-runtime=true " in summary
    changed = {"runtime", "completion-review", "cheap-runtime"}
    def fields(line):
        return dict(part.split("=", 1) for part in line.removeprefix("settings: ").replace("goal=true ", "").split(" ")
                    if "=" in part)
    assert {key for key, value in fields(summary).items() if fields(text)[key] != value} == changed


@pytest.mark.parametrize("runtime_saved", [False, True])
async def test_goal_startup_wires_mandatory_roles_and_keeps_the_other_switches(tmp_path, monkeypatch, runtime_saved):
    task = tmp_path / "TASK.md"
    task.write_text("Implement the requested behavior.", encoding="utf-8")
    client = SimpleNamespace(
        configure_run=Mock(), start=AsyncMock(), initialize=AsyncMock(), stop=AsyncMock(),
        thread_start=AsyncMock(return_value={"thread": {"id": "coder-thread"}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "coder-turn"}}),
    )
    config = ProjectConfig(task="TASK.md", runtime_enabled=runtime_saved, completion_review=False,
                           completion_returns_before_adversary=2, completion_returns_after_adversary=1)
    controller = BelloController(tmp_path, task_path=task, client=client, tui=_FakeTUI(),
                                 project_config=config, goal=True, async_tools=False,
                                 runtime_enabled=False, completion_review=False, adversary_enabled=False)
    monkeypatch.setattr(controller, "preflight", AsyncMock())
    monkeypatch.setattr(controller, "_prepare_coder_workspace", lambda: None)
    monkeypatch.setattr(controller, "event_loop", AsyncMock())
    await controller.run()
    kwargs = client.configure_run.call_args.kwargs
    assert kwargs["runtime_enabled"] is True  # forced on, despite the saved and per-run off
    assert kwargs["async_tools"] is False  # Goal does not touch Smart Execution
    assert controller.supervisor is not None
    assert controller.completion_supervisor is not None and controller.completion_supervisor.goal is True
    assert controller.supervisor.goal is False  # runtime prompts get no Goal guidance
    assert controller.adv_report_controller is None  # the optional adversary stays off
    assert controller.coder.goal is True
    assert not controller._revision_coder_enabled() and not controller._log_distiller_config().enabled
    config_state = controller.store.get_bello_config()
    # The run records its own runtime choice (here the per-run off); Goal forces it on when used.
    assert config_state.goal is True and config_state.runtime_enabled is False
    assert config_state.async_tools is False
    # Both persistence paths keep the saved return limits and the other switches.
    controller._persist_model_config()
    restored = load_project_config(tmp_path, create=False)
    assert restored.goal is True
    assert restored.runtime_enabled is False and restored.effective_runtime_enabled is True
    assert restored.completion_review is restored.async_tools is False
    assert (restored.completion_returns_before_adversary, restored.completion_returns_after_adversary) == (2, 1)


def test_goal_off_startup_has_no_goal_state_or_prompts(tmp_path):
    task = tmp_path / "TASK.md"
    task.write_text("Implement the requested behavior.", encoding="utf-8")
    controller = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), project_config=ProjectConfig(task="TASK.md"))
    controller.initialize_state()
    state = controller.store.get_bello_config()
    assert state.goal is False and state.async_tools is True and state.runtime_enabled is True
    assert "goal" not in controller._runtime_settings_summary()


async def test_goal_preflight_validates_and_self_tests_the_forced_roles(tmp_path, monkeypatch):
    from tests.test_runtime_preflight import controller_for

    controller, events = controller_for(tmp_path, monkeypatch)
    controller.goal = True
    controller.runtime_enabled = False
    controller.completion_review = False
    controller._effective_completion_review = BelloController._effective_completion_review.__get__(controller)
    await controller._runtime_preflight()
    profiles = {params["model"] for method, params in (event for event in events if isinstance(event, tuple))
                if method == "model/validate"}
    assert profiles == {"test/coder", "test/runtime", "test/completion"} == set(controller.client.required_models)
    assert events[-3:] == ["sandbox", "paid-self-test", "triage"]  # runtime supervision is really exercised


# --- prompts: Goal guidance is brief, additive and present only under Goal ---------------------


@pytest.mark.parametrize("builder", [build_coder_prompt, build_restart_prompt, build_revision_prompt])
def test_goal_coder_guidance_is_short_additive_and_optional(tmp_path, builder):
    args = (tmp_path / "TASK.md", "Fix the returned defect") if builder is build_revision_prompt else (tmp_path / "TASK.md",)
    ordinary = builder(*args)
    assert builder(*args, goal=False) == ordinary
    assert builder(*args, goal=True) == GOAL_CODER_GUIDANCE + "\n\n" + ordinary
    assert builder(*args, goal=True).endswith("When complete, output BELLO_READY_FOR_REVIEW on its own line.")
    assert "GOAL_MAP.md" not in ordinary
    assert not (tmp_path / "GOAL_MAP.md").exists()  # building a prompt never creates the file


def test_goal_coder_guidance_content_is_brief_and_complete():
    text = " ".join(GOAL_CODER_GUIDANCE.split())
    assert len(text) < 700
    for fragment in ("GOAL_MAP.md", "approaches", "results", "evidence", "untried", "Consult",
                     "Repeat an approach that failed only when something relevant has changed", "record what changed"):
        assert fragment in text
    assert "parser" not in text.lower() and "score" not in text.lower()


def test_goal_completion_guidance_content_is_brief_and_complete():
    text = " ".join(GOAL_COMPLETION_GUIDANCE.split())
    assert len(text) < 700
    for fragment in ("original objectives", "current evidence", "concrete unmet requirement", "65/100", "70/100",
                     "non-numeric", "Do not invent requirements", "raise the targets", "polish"):
        assert fragment in text


def test_goal_completion_prompt_is_additive_and_leaves_other_prompts_unchanged(tmp_path):
    controller, store, _ = _runtime_controller(tmp_path)
    agent = StatelessSupervisorAgent(object(), store, controller.task_path)
    packet = agent.build_packet(wake_sequence=1, current_summary="ready")
    before = json.loads(build_completion_review_prompt(packet))
    after = json.loads(build_completion_review_prompt(packet, goal=True))
    assert json.loads(build_completion_review_prompt(packet, goal=False)) == before
    assert after.pop("instructions") == before.pop("instructions") + [GOAL_COMPLETION_GUIDANCE]
    assert after.pop("prompt_sections") == before.pop("prompt_sections") + ["goal"]
    assert after == before
    # The adversary and the other reviewers' prompts have no Goal parameter and no Goal text.
    assert GOAL_COMPLETION_GUIDANCE not in build_adversary_prompt(packet)
    assert "Goal mode" not in build_adversary_prompt(packet)
    assert "Goal mode" not in build_stateless_supervisor_prompt(packet)
    assert "Goal mode" not in build_adv_report_controller_prompt(
        task_path=controller.task_path, raw_adversary_report_path=tmp_path / "report.md",
    )


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("goal", [False, True])
async def test_goal_note_reaches_the_completion_reviewer_in_every_prompt_form(tmp_path, compact, goal):
    controller, store, _ = _runtime_controller(tmp_path)
    agent = StatelessSupervisorAgent(object(), store, controller.task_path, goal=goal)
    packet = agent.build_packet(wake_sequence=1, current_summary="ready")
    accept = CompletionReviewDecision(
        decision="accept", reason="objective verified", message_to_coder=None,
        persistent_decision=None, progress_update=None, clear_handoff=False,
        display_message=None, handoff=None, wake_sequence=1, generation=0,
    )
    agent._decide = AsyncMock(return_value=accept)
    try:
        assert await agent._decide_completion_with_prompt(
            packet, use_case="completion_review", task_in_file=compact,
        ) is accept
        payload = json.loads(agent._decide.call_args.kwargs["prompt"])
        assert (GOAL_COMPLETION_GUIDANCE in payload["instructions"]) is goal
        retry = await agent._decide_completion_with_prompt(
            packet, use_case="completion_review_minimal_retry", task_in_file=compact, retry_error="invalid",
        )
        assert retry is accept
        assert (GOAL_COMPLETION_GUIDANCE in agent._decide.call_args.kwargs["prompt"]) is goal
    finally:
        await agent.close_completion_review()


# --- coder lifecycle: GOAL_MAP.md metadata and guidance in every coder prompt -------------------


@pytest.mark.parametrize("goal", [False, True])
async def test_coder_goal_metadata_and_prompts_across_the_lifecycle(tmp_path, goal):
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path="TASK.md"), overwrite=True)
    client = SimpleNamespace(
        thread_start=AsyncMock(return_value={"thread": {"id": "coder"}}),
        thread_resume=AsyncMock(return_value={"thread": {"id": "coder"}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "turn", "status": "completed"}}),
    )
    root = tmp_path / "workspace"
    root.mkdir()
    # The map lives beside the coder's workspace root, not beside the task file.
    coder = CoderSession(client, store, root, root / "tasks" / "TASK.md", goal=goal)
    await coder.start_thread()
    await coder.start_initial_turn()
    await coder.resume_thread()
    await coder.start_restart_turn()
    await coder.start_revision_turn("Correct the unmet objective")
    calls = [*client.thread_start.call_args_list, *client.thread_resume.call_args_list,
             *client.turn_start.call_args_list]
    assert len(calls) == 5
    for call in calls:
        if goal:
            assert call.args[0]["runtimeGoalMapPath"] == str(root.resolve() / "GOAL_MAP.md")
            # Output-selection metadata only: the filesystem scope is the same as without Goal.
            assert "GOAL_MAP.md" not in json.dumps(call.args[0].get("runtimeWorkspaceRoots", []))
        else:
            assert "runtimeGoalMapPath" not in call.args[0]
    assert len(client.turn_start.call_args_list) == 3
    for call in client.turn_start.call_args_list:
        assert (GOAL_CODER_GUIDANCE in call.args[0]["input"][0]["text"]) is goal
    assert not (root / "GOAL_MAP.md").exists()  # nothing requires the file to exist


@pytest.mark.parametrize("goal", [False, True])
async def test_fresh_transport_recovery_carries_the_goal_guidance(tmp_path, goal):
    controller, store, _ = _runtime_controller(tmp_path)
    controller.goal = goal
    controller.client = SimpleNamespace(
        thread_start=AsyncMock(return_value={"thread": {"id": "recovered"}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "turn", "status": "completed"}}),
    )
    previous = CoderSession(controller.client, store, tmp_path, controller.task_path, goal=goal)
    await controller._start_fallback_recovery_coder(previous, start_continuation=True)
    assert controller.coder.goal is goal
    prompt = controller.client.turn_start.call_args.args[0]["input"][0]["text"]
    assert prompt == (GOAL_CODER_GUIDANCE + "\n\n" + TRANSPORT_RECOVERY_CODER_PROMPT if goal
                      else TRANSPORT_RECOVERY_CODER_PROMPT)
    assert ("runtimeGoalMapPath" in controller.client.turn_start.call_args.args[0]) is goal


@pytest.mark.parametrize("goal", [False, True])
async def test_resumed_transport_recovery_continuation_carries_the_goal_guidance(tmp_path, goal):
    controller, store, _ = _runtime_controller(tmp_path)
    controller.goal = goal
    client = SimpleNamespace(
        thread_resume=AsyncMock(return_value={"thread": {"id": "thread", "turns": []}}),
        turn_start=AsyncMock(return_value={"turn": {"id": "turn", "status": "inProgress"}}),
    )
    controller.client = client
    controller.coder = CoderSession(client, store, tmp_path, controller.task_path, goal=goal)
    await controller._recover_coder_thread_after_transport(start_continuation=True)
    prompt = client.turn_start.call_args.args[0]["input"][0]["text"]
    assert prompt == (GOAL_CODER_GUIDANCE + "\n\n" + TRANSPORT_RECOVERY_CODER_PROMPT if goal
                      else TRANSPORT_RECOVERY_CODER_PROMPT)
    assert ("runtimeGoalMapPath" in client.thread_resume.call_args.args[0]) is goal


@pytest.mark.parametrize("goal", [False, True])
async def test_restarted_coder_inherits_goal(tmp_path, goal):
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turn_params = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": "restart-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params.append(params)
            return {"turn": {"id": "restart-turn", "status": "completed"}}

    client = FakeClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.approvals = None
    controller.coder = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller._sequence = 0
    controller.coder_model = "gpt-coder"
    controller.coder_intelligence = "high"
    controller.fast = False
    controller.goal = goal
    await controller.restart("test restart")
    assert controller.coder is not None and controller.coder.goal is goal
    assert ("runtimeGoalMapPath" in client.thread_params[-1]) is goal
    assert ("runtimeGoalMapPath" in client.turn_params[-1]) is goal
    if goal:
        assert client.thread_params[-1]["runtimeGoalMapPath"] == str(tmp_path.resolve() / "GOAL_MAP.md")
    prompt = client.turn_params[-1]["input"][0]["text"]
    assert (GOAL_CODER_GUIDANCE in prompt) is goal
    assert prompt.endswith("on its own line.")


def test_a_started_run_records_its_switches_and_keeps_the_saved_runtime_choice(tmp_path):
    # The run state records the switches the run used. Goal forces runtime supervision on at every
    # point of use, so the record keeps the saved runtime choice instead of rewriting it; review
    # limits and the completion-review field also stay as saved.
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    saved = ProjectConfig(task="TASK.md", runtime_enabled=False, completion_review=False, async_tools=True,
                          completion_returns_before_adversary=2, completion_returns_after_adversary=1)
    controller = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), project_config=saved,
                                 goal=True, async_tools=False, adversary_enabled=False)
    controller.initialize_state()
    state = controller.store.get_bello_config()
    assert (state.goal, state.runtime_enabled, state.async_tools) == (True, False, False)
    assert controller._runtime_enabled() is True and controller._effective_completion_review() is True
    assert state.completion_review_enabled is False
    assert (state.max_completion_returns_before_adversary, state.max_completion_returns_after_adversary) == (2, 1)
    assert saved.goal is False and saved.runtime_enabled is False  # the loaded config object is not edited

    reloaded = load_project_config(tmp_path, create=False)
    assert reloaded.goal is True and reloaded.effective_runtime_enabled is True
    # A recovered or later Goal run still supervises; turning Goal off restores the saved choice.
    recovered = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), project_config=reloaded)
    assert recovered._runtime_enabled() is True
    normal = BelloController(tmp_path, task_path=task, tui=_FakeTUI(), project_config=reloaded, goal=False)
    assert normal._runtime_enabled() is False


@pytest.mark.parametrize("goal", [False, True])
def test_goal_map_edits_do_not_make_validation_stale_but_other_edits_do(tmp_path, goal):
    from supervisor.schemas import ChangedFile

    controller, store, _ = _runtime_controller(tmp_path)
    controller.goal = goal
    # A docs-facing task makes markdown files count as relevant edits for ordinary runs.
    controller.task_path.write_text("Document the behavior in the README.", encoding="utf-8")
    controller._update_relevant_edit_state([ChangedFile(path="GOAL_MAP.md", status="A", sequence=5)])
    assert store.get_bello_config().last_relevant_edit_sequence == (None if goal else 5)
    controller._update_relevant_edit_state([ChangedFile(path="docs/GOAL_MAP.md", status="A", sequence=6)])
    assert store.get_bello_config().last_relevant_edit_sequence == 6  # only the root map is exempt
    controller._update_relevant_edit_state([ChangedFile(path="src/app.py", status="M", sequence=7)])
    assert store.get_bello_config().last_relevant_edit_sequence == 7
