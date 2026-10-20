"""Smart Execution (``async_tools``) is on for new configurations; explicit saved off choices survive.

Every layer that can supply a value must agree: project config, run-state schema,
`bello config`, the editor, the CLI switch, the controller's fallbacks and the
config-advisor plugin helpers.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

import supervisor.main as main_module
from supervisor.config_editor import _replace_config_field, parameter_defs
from supervisor.controller import BelloController
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    ProjectConfig,
    ProjectConfigError,
    default_project_config,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.schemas import BelloConfig
from tests.support.controller import _FakeTUI

SKILL = Path(__file__).resolve().parents[1] / "plugins/bello/skills/bello-config-advisor"


def _plugin(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path, payload):
    path = project_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _saved(tmp_path):
    return json.loads(project_config_path(tmp_path).read_text(encoding="utf-8"))


def test_new_configurations_default_smart_execution_on(tmp_path):
    assert ProjectConfig().async_tools is True
    assert default_project_config().async_tools is True
    assert BelloConfig(project_root="fixture", task_path="TASK.md").async_tools is True
    assert ProjectConfig().to_json_data()["async_tools"] is True
    fresh = load_project_config(tmp_path, create=True)  # no file yet: a new configuration
    assert fresh.async_tools is True
    assert _saved(tmp_path)["async_tools"] is True
    assert load_project_config(tmp_path, create=False).async_tools is True


@pytest.mark.parametrize("payload", [
    {}, {"coder_mod": "gpt-5.6-sol"}, {"runtime_enabled": False}, {"goal": True}, {"cheap_runtime": False},
])
def test_a_config_without_the_key_means_on(tmp_path, payload):
    path = _write(tmp_path, payload)
    original = path.read_bytes()
    assert load_project_config(tmp_path, create=False).async_tools is True
    assert path.read_bytes() == original  # loading never rewrites the file


@pytest.mark.parametrize("saved", [False, True])
def test_an_explicit_saved_choice_is_preserved_by_load_and_save(tmp_path, saved):
    _write(tmp_path, {"async_tools": saved})
    assert load_project_config(tmp_path, create=False).async_tools is saved
    save_project_config(tmp_path, replace(ProjectConfig(), async_tools=saved))
    assert _saved(tmp_path)["async_tools"] is saved
    assert load_project_config(tmp_path, create=False).async_tools is saved
    # Editing an unrelated setting never resets it.
    updated = replace(load_project_config(tmp_path, create=False), speed="usual", clean=False)
    save_project_config(tmp_path, updated)
    assert load_project_config(tmp_path, create=False).async_tools is saved


@pytest.mark.parametrize("value", [None, "false", "true", 0, 1, [], {}])
def test_async_tools_must_be_a_boolean(tmp_path, value):
    _write(tmp_path, {"async_tools": value})
    with pytest.raises(ProjectConfigError, match="async_tools"):
        load_project_config(tmp_path, create=False)


@pytest.mark.parametrize("saved,override,expected", [
    (True, None, True), (False, None, False), (True, False, False), (False, True, True),
    (True, True, True), (False, False, False),
])
def test_run_settings_use_the_saved_choice_unless_the_cli_overrides(saved, override, expected):
    config = ProjectConfig(async_tools=saved)
    assert _resolve_run_settings(project_config=config, async_tools=override).async_tools is expected
    assert config.async_tools is saved  # a run never edits the saved choice


def test_default_run_settings_are_on():
    assert _resolve_run_settings(project_config=ProjectConfig()).async_tools is True
    assert main_module.RunSettings.__dataclass_fields__["async_tools"].default is True


@pytest.mark.parametrize("args,saved,expected", [
    ([], True, True), ([], False, False), (["--async-tools"], False, True),
    (["--no-async-tools"], True, False), (["--async-tools"], True, True),
])
def test_cli_switch_overrides_the_saved_choice_for_one_run(tmp_path, monkeypatch, args, saved, expected):
    save_project_config(tmp_path, ProjectConfig(async_tools=saved))
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
    assert captured[0].async_tools is expected
    # The flag only changes this run's settings; the file is untouched until a run records its state.
    assert load_project_config(tmp_path, create=False).async_tools is saved


def test_cli_help_states_the_new_default():
    option = next(param for param in cli.params if param.name == "async_tools")
    assert option.opts == ["--async-tools"] and option.secondary_opts == ["--no-async-tools"]
    assert "(default: on)" in option.help and "default: off" not in option.help
    assert "Smart Execution" in option.help


@pytest.mark.parametrize("saved", [False, True])
def test_bello_config_display_reports_the_effective_choice(tmp_path, monkeypatch, saved):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "_startup_update_gate", lambda: None)
    monkeypatch.setattr(main_module, "run_config_editor", lambda root: ProjectConfig(async_tools=saved))
    lines = CliRunner().invoke(cli, ["config"]).output.splitlines()
    assert f"async-tools: {str(saved).lower()}" in lines


def test_editor_shows_on_by_default_and_changes_only_the_switch():
    parameter = next(p for p in parameter_defs(ProjectConfig()) if p.key == "async_tools")
    assert parameter.value == "true"
    assert [option.value for option in parameter.options] == [True, False]
    assert "On by default" in parameter.help_text
    disabled = _replace_config_field(ProjectConfig(), "async_tools", False)
    assert disabled == replace(ProjectConfig(), async_tools=False)
    saved_off = next(p for p in parameter_defs(disabled) if p.key == "async_tools")
    assert saved_off.value == "false"
    assert _replace_config_field(disabled, "async_tools", True) == ProjectConfig()


def _controller(tmp_path, **kwargs):
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    return BelloController(tmp_path, task_path=task, tui=_FakeTUI(), **kwargs)


def test_controller_fallbacks_default_on_and_keep_explicit_off(tmp_path):
    controller = _controller(tmp_path)
    assert controller.project_config is None
    controller.initialize_state()
    assert controller._async_tools_enabled() is True  # the persisted run state of a new config
    assert controller.store.get_bello_config().async_tools is True
    # Raw saved JSON without the key (an old or partial file) -> on; explicit false -> off.
    state = project_config_path(tmp_path)
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload.pop("async_tools")
    state.write_text(json.dumps(payload), encoding="utf-8")
    assert controller._async_tools_enabled() is True
    payload["async_tools"] = False
    state.write_text(json.dumps(payload), encoding="utf-8")
    assert controller._async_tools_enabled() is False
    # A per-run override still wins, in both directions.
    controller.async_tools = True
    assert controller._async_tools_enabled() is True
    controller.async_tools = False
    payload["async_tools"] = True
    state.write_text(json.dumps(payload), encoding="utf-8")
    assert controller._async_tools_enabled() is False


@pytest.mark.parametrize("saved", [False, True])
def test_controller_uses_the_loaded_project_config_and_persists_it(tmp_path, saved):
    controller = _controller(tmp_path, project_config=ProjectConfig(async_tools=saved))
    assert controller._async_tools_enabled() is saved
    controller.initialize_state()
    assert controller.store.get_bello_config().async_tools is saved
    assert "async-tools=" + str(saved).lower() in controller._runtime_settings_summary()


def test_controller_default_without_any_configuration_is_on(tmp_path):
    controller = _controller(tmp_path)  # no project config and no saved run state at all
    assert controller._async_tools_enabled() is True


def test_advisor_scripts_use_the_same_defaults():
    inspector, validator = _plugin("inspect_config"), _plugin("validate_config")
    assert inspector._normalize({}, config_exists=False)["async_tools"] is True
    assert inspector._normalize({}, config_exists=True)["async_tools"] is True
    for saved in (False, True):
        assert inspector._normalize({"async_tools": saved}, config_exists=True)["async_tools"] is saved
    assert validator.OPTIONAL_BOOLEAN_DEFAULTS["async_tools"] is True
    assert "async_tools" in validator.OPTIONAL_BOOLEAN_KEYS
    schema = (SKILL / "references/CONFIG_SCHEMA.md").read_text(encoding="utf-8")
    assert '"async_tools": true' in schema and "defaults on for new configurations" in schema
    assert "defaults off" not in schema.split("Runtime and log-distiller switches")[1].split("`no_need_for_context`")[0]
