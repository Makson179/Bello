"""Offline configuration, editor and CLI contracts for opt-in Goal mode.

Goal is off by default. While it is on, runtime supervision and completion review
are mandatory (a saved or per-run "off" is overridden, loudly) and completion
returns are unlimited. It must never change Smart Execution (``async_tools``),
the revision coder, No Need For Context or the log distiller.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

import supervisor.main as main_module
from supervisor.config_editor import _replace_config_field, parameter_defs, select_current
from supervisor.config_editor_types import EditorState
from supervisor.config_validation import project_profiles
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    ProjectConfig,
    ProjectConfigError,
    changed_project_config_fields,
    load_project_config,
    project_config_path,
    save_project_config,
    sync_runtime_config_fields,
)
from supervisor.schemas import BelloConfig, BelloStatus


def _write_config(tmp_path, payload):
    path = project_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _parameters(config):
    return {parameter.key: parameter for parameter in parameter_defs(config)}


def test_goal_is_opt_in_and_normal_defaults_are_unchanged():
    config = ProjectConfig()
    settings = _resolve_run_settings(project_config=config)
    state = BelloConfig(project_root="fixture", task_path="TASK.md")
    assert config.goal is settings.goal is state.goal is False
    assert settings.goal_notices == ()
    assert settings.runtime_enabled is True
    assert settings.completion_review is False
    assert settings.async_tools is True  # Smart Execution's own default, not Goal's.
    assert settings.adversary is False
    assert config.revision_coder_enabled is False
    assert config.no_need_for_context.enabled is False
    assert settings.log_distiller.enabled is False
    assert (config.completion_returns_before_adversary, config.completion_returns_after_adversary) == (1, 0)
    assert config.effective_runtime_enabled is True and config.effective_completion_review is False


@pytest.mark.parametrize("payload,goal,async_tools", [
    ({}, False, True),
    ({"goal": False}, False, True),
    ({"goal": True}, True, True),
    ({"goal": True, "async_tools": False}, True, False),
    ({"goal": True, "async_tools": True}, True, True),
    ({"goal": False, "async_tools": False}, False, False),
    ({"goal": False, "async_tools": True}, False, True),
])
def test_goal_never_changes_the_smart_execution_value_a_file_resolves_to(tmp_path, payload, goal, async_tools):
    path = _write_config(tmp_path, payload)
    original = path.read_bytes()
    config = load_project_config(tmp_path, create=False)
    assert config.goal is goal
    assert config.async_tools is async_tools
    assert path.read_bytes() == original


@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_goal_config_requires_a_boolean(tmp_path, value):
    _write_config(tmp_path, {"goal": value})
    with pytest.raises(ProjectConfigError, match="goal"):
        load_project_config(tmp_path, create=False)
    with pytest.raises(ValidationError):
        BelloConfig(project_root="fixture", task_path="TASK.md", goal=value)


def test_effective_values_follow_goal_without_rewriting_saved_choices():
    saved_off = ProjectConfig(goal=True, runtime_enabled=False, completion_review=False, cheap_runtime=True)
    assert (saved_off.runtime_enabled, saved_off.completion_review) == (False, False)
    assert saved_off.effective_runtime_enabled is True
    assert saved_off.effective_completion_review is True
    assert saved_off.effective_cheap_runtime is True
    plain = replace(saved_off, goal=False)
    assert (plain.effective_runtime_enabled, plain.effective_completion_review, plain.effective_cheap_runtime) == (
        False, False, False)


@pytest.mark.parametrize("async_tools", [False, True])
def test_saved_goal_roundtrip_preserves_choices_and_other_switches(tmp_path, async_tools):
    config = ProjectConfig(goal=True, async_tools=async_tools, runtime_enabled=False,
                           completion_review=False, adversary=True, adversary_runs=2,
                           completion_returns_before_adversary=3, completion_returns_after_adversary=1)
    save_project_config(tmp_path, config)
    loaded = load_project_config(tmp_path, create=False)
    assert loaded.goal is True
    assert loaded.async_tools is async_tools
    # The saved choices survive; only the effective view is forced while Goal is on.
    assert loaded.runtime_enabled is loaded.completion_review is False
    assert loaded.effective_runtime_enabled is loaded.effective_completion_review is True
    assert (loaded.adversary, loaded.adversary_runs) == (True, 2)
    assert (loaded.completion_returns_before_adversary, loaded.completion_returns_after_adversary) == (3, 1)
    assert config.to_json_data()["goal"] is True
    raw = json.loads(project_config_path(tmp_path).read_text(encoding="utf-8"))
    assert raw["goal"] is True and raw["async_tools"] is async_tools
    assert raw["runtime_enabled"] is False and raw["completion_review_enabled"] is False
    assert raw["cheap_runtime"] is True  # Runtime is on while Goal is on, so triage stays as chosen.


def test_goal_is_synced_without_resetting_other_runtime_state(tmp_path):
    path = _write_config(tmp_path, {
        "project_root": str(tmp_path), "task_path": "TASK.md", "goal": False,
        "generation": 7, "restart_count": 2, "coder_thread_id": "existing-thread",
        "async_tools": False, "runtime_enabled": False, "completion_review_enabled": False,
    })
    before = ProjectConfig(runtime_enabled=False)
    after = replace(before, goal=True)
    fields = changed_project_config_fields(before, after)
    assert fields == ("goal",)
    sync_runtime_config_fields(tmp_path, after, fields)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["goal"] is True
    assert raw["generation"] == 7 and raw["restart_count"] == 2
    assert raw["coder_thread_id"] == "existing-thread"
    assert raw["async_tools"] is raw["runtime_enabled"] is raw["completion_review_enabled"] is False


def test_goal_run_forces_runtime_and_completion_and_says_so():
    config = ProjectConfig(runtime_enabled=False, completion_review=False)
    settings = _resolve_run_settings(project_config=config, goal=True,
                                     runtime_enabled=False, completion_review=False)
    assert settings.goal and settings.completion_review and settings.runtime_enabled
    assert settings.async_tools is True
    assert settings.adversary is False
    assert settings.log_distiller.enabled is False
    assert config.revision_coder_enabled is False
    assert config.goal is False and config.runtime_enabled is False  # a run never edits the saved config
    assert len(settings.goal_notices) == 2
    assert "ignoring --no-runtime" in settings.goal_notices[0]
    assert "--completion-review false" in settings.goal_notices[1]


def test_saved_runtime_off_with_goal_is_forced_on_with_a_notice_but_default_completion_is_silent():
    settings = _resolve_run_settings(project_config=ProjectConfig(goal=True, runtime_enabled=False))
    assert settings.runtime_enabled is settings.completion_review is True
    assert settings.goal_notices == ("Goal mode requires runtime supervision; "
                                     "the saved runtime=false is overridden for this run.",)
    quiet = _resolve_run_settings(project_config=ProjectConfig(goal=True))
    assert quiet.runtime_enabled is quiet.completion_review is True and quiet.goal_notices == ()


@pytest.mark.parametrize("saved_goal,saved_async,goal,override", [
    (False, False, True, None), (False, True, True, None), (True, False, None, None),
    (True, True, None, None), (True, False, True, True), (False, True, True, False),
    (True, True, False, None), (False, False, False, None),
])
def test_goal_never_changes_smart_execution_precedence(saved_goal, saved_async, goal, override):
    config = ProjectConfig(goal=saved_goal, async_tools=saved_async)
    settings = _resolve_run_settings(project_config=config, goal=goal, async_tools=override)
    # The same result as without Goal: an explicit CLI switch wins, else the saved choice.
    assert settings.async_tools is (saved_async if override is None else override)


def test_no_goal_uses_saved_normal_runtime_and_completion_choices():
    config = ProjectConfig(goal=True, runtime_enabled=False, completion_review=False, async_tools=False)
    settings = _resolve_run_settings(project_config=config, goal=False)
    assert settings.goal is settings.runtime_enabled is settings.completion_review is settings.async_tools is False
    assert settings.goal_notices == ()


def test_goal_keeps_optional_adversary_count_configured():
    settings = _resolve_run_settings(project_config=ProjectConfig(adversary=True, adversary_runs=2), goal=True)
    assert settings.adversary is True and settings.adversary_runs == 2
    off = _resolve_run_settings(project_config=ProjectConfig(adversary=True, adversary_runs=2),
                                goal=True, adversary_runs=0)
    assert off.adversary is False and off.adversary_runs == 0


def test_goal_validates_the_forced_runtime_and_completion_profiles(monkeypatch):
    roles = []
    monkeypatch.setattr(main_module, "_validate_model_intelligence", lambda role, *_args: roles.append(role))
    _resolve_run_settings(project_config=ProjectConfig(goal=True, runtime_enabled=False))
    assert roles == ["coder", "runtime", "completion"]
    roles.clear()
    _resolve_run_settings(project_config=ProjectConfig(runtime_enabled=False))
    assert roles == ["coder"]


def test_offline_validation_checks_the_roles_goal_forces_on():
    config = ProjectConfig(goal=True, runtime_enabled=False, completion_review=False)
    assert {use.role for use in project_profiles(config)} >= {"coder", "runtime", "completion"}
    assert {use.role for use in project_profiles(replace(config, goal=False))} == {"coder"}


def test_goal_editor_toggle_changes_only_goal():
    config = ProjectConfig(runtime_enabled=False, completion_review=False, async_tools=False)
    enabled = _replace_config_field(config, "goal", True)
    assert enabled == replace(config, goal=True)
    assert enabled.async_tools is False and enabled.runtime_enabled is False and enabled.completion_review is False
    assert _replace_config_field(enabled, "goal", False) == config
    default_async = _replace_config_field(ProjectConfig(), "goal", True)
    assert default_async.async_tools is True
    explicit_off = _replace_config_field(default_async, "async_tools", False)
    assert explicit_off.goal and not explicit_off.async_tools


def test_goal_editor_shows_forced_roles_and_unlimited_returns_without_mutating_saved_values():
    config = ProjectConfig(goal=True, runtime_enabled=False, completion_review=False,
                           adversary=True, adversary_runs=2)
    parameters = _parameters(config)
    assert [option.value for option in parameters["goal"].options] == [True, False]
    for key in ("runtime_enabled", "completion_review"):
        assert parameters[key].value == "true (goal)"
        assert parameters[key].options == () and parameters[key].edit_kind is None
        assert "goal" in parameters[key].help_text
    assert {"runtime_mod", "runtime_intelligence", "cheap_runtime",
            "completion_mod", "completion_intelligence"} <= set(parameters)
    for key in ("completion_returns_before_adversary", "completion_returns_after_adversary"):
        assert parameters[key].value == "Unlimited (goal)"
        assert parameters[key].edit_kind is None and parameters[key].options == ()
    assert parameters["adversary_runs"].value == "2"
    assert parameters["adversary_runs"].edit_kind == "non_negative_int"
    assert [option.value for option in parameters["async_tools"].options] == [True, False]
    assert (config.runtime_enabled, config.completion_review) == (False, False)
    assert config.completion_returns_before_adversary == 1 and config.completion_returns_after_adversary == 0
    # Read-only rows cannot be entered or changed.
    index = list(parameters).index("runtime_enabled")
    updated, state, action = select_current(config, EditorState(parameter_index=index))
    assert updated == config and action is None and not state.editing


def test_without_goal_the_editor_rows_are_the_unchanged_normal_ones():
    config = ProjectConfig(runtime_enabled=False, completion_review=False)
    parameters = _parameters(config)
    assert parameters["goal"].value == "false"
    assert parameters["runtime_enabled"].value == "false"
    assert [option.value for option in parameters["runtime_enabled"].options] == [True, False]
    assert parameters["completion_review"].value == "false"
    assert [option.value for option in parameters["completion_review"].options] == [True, False]
    assert "runtime_mod" not in parameters and "completion_mod" not in parameters
    review = _parameters(ProjectConfig(completion_review=True))
    assert review["completion_returns_before_adversary"].value == "1"
    assert review["completion_returns_before_adversary"].edit_kind == "review_limit"


def _invoke(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    captured = []

    async def fake_run(settings):
        captured.append(settings)
        return 0

    monkeypatch.setattr(main_module, "_run_bello", fake_run)
    monkeypatch.setattr(main_module, "_run_async_cleanly", asyncio.run)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    return captured[0], result


@pytest.mark.parametrize("args,goal,async_tools,runtime,notice", [
    (["--goal"], True, True, True, False),
    (["--goal", "--no-async-tools"], True, False, True, False),
    (["--no-goal"], False, True, True, False),
    (["--no-goal", "--no-async-tools"], False, False, True, False),
    (["--goal", "--no-runtime"], True, True, True, True),
    (["--no-goal", "--no-runtime"], False, True, False, False),
])
def test_cli_goal_flags_reach_run_settings_without_execution(tmp_path, monkeypatch, args, goal, async_tools, runtime, notice):
    settings, result = _invoke(tmp_path, monkeypatch, args)
    assert settings.goal is goal
    assert settings.async_tools is async_tools
    assert settings.runtime_enabled is runtime
    assert settings.completion_review is goal
    assert ("Goal mode requires runtime supervision; ignoring --no-runtime." in result.output) is notice


def test_cli_help_describes_goal_and_smart_execution_default():
    help_text = " ".join(CliRunner().invoke(cli, ["--help"]).output.split())
    assert "--goal / --no-goal" in help_text
    assert "mandatory" in help_text and "unlimited" in help_text
    assert "Smart Execution" in help_text and "(default: on)" in help_text


def test_config_output_reports_runtime_and_completion_forced_by_goal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    monkeypatch.setattr(main_module, "run_config_editor",
                        lambda root: ProjectConfig(goal=True, runtime_enabled=False, completion_review=False))
    result = CliRunner().invoke(cli, ["config"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "goal: true" in lines
    assert "runtime: true (required by goal)" in lines
    assert "completion-review: true (required by goal)" in lines
    assert "completion-returns: unlimited (goal)" in lines
    assert any(line.startswith("runtime-mod:") for line in lines)
    assert any(line.startswith("completion-mod:") for line in lines)
    assert "async-tools: true" in lines


def test_config_output_without_goal_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    monkeypatch.setattr(main_module, "run_config_editor",
                        lambda root: ProjectConfig(runtime_enabled=False, completion_review=False, async_tools=False))
    lines = CliRunner().invoke(cli, ["config"]).output.splitlines()
    assert "goal: false" in lines and "runtime: false" in lines and "completion-review: false" in lines
    assert "async-tools: false" in lines
    assert not any(line.startswith(("runtime-mod:", "completion-mod:", "completion-returns:")) for line in lines)


@pytest.mark.asyncio
async def test_run_bello_passes_goal_to_controller_without_running_models(tmp_path, monkeypatch):
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
    settings = _resolve_run_settings(project_config=ProjectConfig(), goal=True, async_tools=False, runtime_enabled=False)
    assert settings.runtime_enabled is True and settings.runtime_choice is False
    assert await main_module._run_bello(settings) == 0
    assert captured["goal"] is True
    # The controller gets the run's own runtime switch and forces supervision on for Goal itself,
    # so the recorded run state never rewrites the saved runtime preference.
    assert captured["runtime_enabled"] is False
    assert captured["completion_review"] is True
    assert captured["async_tools"] is False
    plain = _resolve_run_settings(project_config=ProjectConfig(runtime_enabled=False))
    assert plain.runtime_enabled is plain.runtime_choice is False



def test_goal_run_keeps_the_other_optional_features_as_configured():
    from supervisor.project_config import LogDistillerConfig, NoNeedForContextConfig

    config = ProjectConfig(goal=True, revision_coder_enabled=True, async_tools=False,
                           no_need_for_context=NoNeedForContextConfig(True, 100000, 12.5),
                           log_distiller=LogDistillerConfig(enabled=True))
    settings = _resolve_run_settings(project_config=config)
    assert settings.no_need_for_context == NoNeedForContextConfig(True, 100000, 12.5)
    assert settings.log_distiller.enabled is True and settings.async_tools is False
    off = _resolve_run_settings(project_config=ProjectConfig(goal=True))
    assert off.no_need_for_context.enabled is False and off.log_distiller.enabled is False
    parameters = _parameters(config)
    assert parameters["no_need_for_context_enabled"].value == "true"
    assert parameters["log_distiller_enabled"].value == "true"
    assert parameters["revision_coder_enabled"].value == "on"
    assert parameters["async_tools"].value == "false"
    quiet = _parameters(ProjectConfig(goal=True))
    assert quiet["no_need_for_context_enabled"].value == "false"
    assert quiet["log_distiller_enabled"].value == "false"
    assert quiet["revision_coder_enabled"].value == "off"
    assert quiet["async_tools"].value == "true"


def test_config_output_with_goal_still_reports_the_real_smart_execution_choice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    for saved in (False, True):
        monkeypatch.setattr(main_module, "run_config_editor",
                            lambda root, saved=saved: ProjectConfig(goal=True, async_tools=saved))
        lines = CliRunner().invoke(cli, ["config"]).output.splitlines()
        assert f"async-tools: {str(saved).lower()}" in lines
