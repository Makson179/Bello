from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from supervisor.config_editor import EditorState, parameter_defs, select_current
from supervisor.controller import BelloController
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    NoNeedForContextConfig, ProjectConfig, ProjectConfigError,
    changed_project_config_fields, load_project_config, project_config_path, save_project_config,
)
from supervisor.schemas import BelloConfig
from supervisor.state import StateStore


@pytest.mark.parametrize("value", [
    {"enabled": 1}, {"enabled": "true"}, {"threshold_tokens": True},
    {"threshold_tokens": 0}, {"threshold_tokens": -1}, {"threshold_tokens": 1.5},
    {"threshold_tokens": "249000"}, {"retain_percent": True}, {"retain_percent": "10"},
    {"retain_percent": 0}, {"retain_percent": -1}, {"retain_percent": 50.1},
    {"retain_percent": float("nan")}, {"retain_percent": float("inf")},
    {"retain_percent": 10 ** 400},
])
def test_context_config_strict_validation(value):
    with pytest.raises(ProjectConfigError, match="no_need_for_context"):
        NoNeedForContextConfig(**value)
    with pytest.raises(ValidationError, match="no_need_for_context"):
        BelloConfig(project_root="/project", no_need_for_context=value)


@pytest.mark.parametrize("value", [None, [], {"thresholdTokens": 1000}, {"unknown": True}])
def test_context_config_rejects_invalid_saved_shape(tmp_path, value):
    path = project_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"no_need_for_context": value}))
    with pytest.raises(ProjectConfigError, match="no_need_for_context"):
        load_project_config(tmp_path, create=False)


def test_context_config_legacy_defaults_and_round_trip(tmp_path):
    path = project_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    old = load_project_config(tmp_path, create=False)
    assert old.no_need_for_context == NoNeedForContextConfig(False, 200000, 10)
    config = replace(old, no_need_for_context=NoNeedForContextConfig(True, 100000, 12.5))
    save_project_config(tmp_path, config)
    assert load_project_config(tmp_path, create=False).no_need_for_context == config.no_need_for_context
    state = StateStore(tmp_path).get_bello_config()
    assert state.no_need_for_context == {"enabled": True, "threshold_tokens": 100000, "retain_percent": 12.5}
    assert BelloConfig.model_validate_json(state.model_dump_json()).no_need_for_context == state.no_need_for_context
    assert changed_project_config_fields(old, config) == ("no_need_for_context",)
    assert config.no_need_for_context.to_backend_data() == {
        "enabled": True, "thresholdTokens": 100000, "retainPercent": 12.5,
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_context_sparse_defaults_and_explicit_legacy_threshold_are_preserved(tmp_path, enabled):
    from supervisor.runtime.claude_context import context_policy

    path = project_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"no_need_for_context": {"enabled": enabled}}))
    sparse = load_project_config(tmp_path, create=False)
    assert sparse.no_need_for_context == NoNeedForContextConfig(enabled, 200000, 10)
    assert context_policy({"enabled": enabled}) == sparse.no_need_for_context.to_backend_data()
    assert BelloConfig(project_root=str(tmp_path), task_path="TASK.md", no_need_for_context={"enabled": enabled}).no_need_for_context == {
        "enabled": enabled, "threshold_tokens": 200000, "retain_percent": 10}

    explicit = {"enabled": enabled, "threshold_tokens": 249000, "retain_percent": 10}
    path.write_text(json.dumps({"no_need_for_context": explicit}))
    saved = load_project_config(tmp_path, create=False)
    save_project_config(tmp_path, saved)
    assert load_project_config(tmp_path, create=False).no_need_for_context.to_json_data() == explicit
    assert StateStore(tmp_path).get_bello_config().no_need_for_context == explicit
    assert _resolve_run_settings(project_config=saved).no_need_for_context.to_json_data() == explicit
    assert context_policy(saved.no_need_for_context.to_backend_data()) == {
        "enabled": enabled, "thresholdTokens": 249000, "retainPercent": 10}


def test_context_run_override_preserves_saved_config():
    config = ProjectConfig(no_need_for_context=NoNeedForContextConfig(True, 100000, 12.5))
    assert _resolve_run_settings(project_config=config).no_need_for_context == config.no_need_for_context
    settings = _resolve_run_settings(project_config=config, no_need_for_context_enabled=False,
                                     no_need_for_context_threshold_tokens=50000,
                                     no_need_for_context_retain_percent=20)
    assert settings.no_need_for_context == NoNeedForContextConfig(False, 50000, 20)
    assert config.no_need_for_context == NoNeedForContextConfig(True, 100000, 12.5)


@pytest.mark.parametrize("enabled", [False, True])
def test_context_cli_switch_and_parameters(monkeypatch, tmp_path, enabled):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("supervisor.main._startup_update_gate", lambda: None)
    captured = []

    async def fake_run(settings):
        captured.append(settings)
        return 0

    monkeypatch.setattr("supervisor.main._run_bello", fake_run)
    monkeypatch.setattr("supervisor.main._run_async_cleanly", asyncio.run)
    result = CliRunner().invoke(cli, [
        "--no-need-for-context" if enabled else "--no-no-need-for-context",
        "--context-threshold-tokens", "100000", "--context-retain-percent", "12.5",
    ])
    assert result.exit_code == 0, result.output
    assert captured[0].no_need_for_context == NoNeedForContextConfig(enabled, 100000, 12.5)


@pytest.mark.parametrize("flag,value", [
    ("--context-threshold-tokens", "0"), ("--context-threshold-tokens", "1.5"),
    ("--context-retain-percent", "0"), ("--context-retain-percent", "51"),
    ("--context-retain-percent", "nan"), ("--context-retain-percent", "inf"),
])
def test_context_cli_rejects_invalid_numbers(monkeypatch, tmp_path, flag, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("supervisor.main._startup_update_gate", lambda: None)
    result = CliRunner().invoke(cli, [flag, value])
    assert result.exit_code != 0


def test_context_editor_controls_and_validation():
    original = ProjectConfig(runtime_enabled=False)
    parameters = parameter_defs(original)
    keys = [p.key for p in parameters]
    assert "no_need_for_context_threshold_tokens" not in keys
    toggle = keys.index("no_need_for_context_enabled")
    config, _, _ = select_current(original, EditorState(parameter_index=toggle, expanded_index=toggle, option_index=0))
    assert config.no_need_for_context.enabled
    assert not config.runtime_enabled
    for field, raw, expected in (("threshold_tokens", "100000", 100000), ("retain_percent", "12.5", 12.5)):
        parameters = parameter_defs(config)
        index = [p.key for p in parameters].index(f"no_need_for_context_{field}")
        config, edit, _ = select_current(config, EditorState(parameter_index=index))
        config, _, _ = select_current(config, replace(edit, edit_value=raw))
        assert getattr(config.no_need_for_context, field) == expected
    parameters = parameter_defs(config)
    index = [p.key for p in parameters].index("no_need_for_context_retain_percent")
    assert "Recent-tail target" in parameters[index].help_text
    for raw in ("0", "51", "nan", "inf", "true"):
        unchanged, state, _ = select_current(config, EditorState(parameter_index=index, editing=True,
                                                               edit_kind="retain_percent", edit_value=raw))
        assert unchanged == config and state.edit_error


def test_controller_reads_saved_context_and_explicit_override(tmp_path):
    config = NoNeedForContextConfig(True, 100000, 10)
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n")
    controller = BelloController(tmp_path, task_path=task, project_config=ProjectConfig(no_need_for_context=config))
    assert controller._no_need_for_context_config() == config
    controller.no_need_for_context = NoNeedForContextConfig(False, 50000, 5)
    assert controller._no_need_for_context_config() == controller.no_need_for_context


def test_context_is_off_by_default_everywhere():
    assert ProjectConfig().no_need_for_context == NoNeedForContextConfig(False, 200000, 10)
    assert BelloConfig(project_root="/project", task_path="TASK.md").no_need_for_context == {
        "enabled": False, "threshold_tokens": 200000, "retain_percent": 10}
    assert _resolve_run_settings(project_config=ProjectConfig()).no_need_for_context.enabled is False


@pytest.mark.parametrize("coder", [
    "gpt-6-astra", "openai-codex/gpt-5.6-sol", "claude-code/claude-sonnet-4-6",
    "openrouter/author/model", "anthropic/claude-sonnet-4-6",
])
def test_context_run_setting_has_no_engine_restriction(coder):
    settings = _resolve_run_settings(
        project_config=ProjectConfig(coder_mod=coder, coder_intelligence="high"),
        no_need_for_context_enabled=True,
    )
    assert settings.no_need_for_context.enabled is True


def test_context_wording_is_engine_neutral_in_cli_and_editor():
    options = {param.name: param for param in cli.params}
    texts = [options[name].help for name in (
        "no_need_for_context_enabled", "no_need_for_context_threshold_tokens", "no_need_for_context_retain_percent")]
    assert "Pi, Claude subscription and native Codex" in texts[0]
    assert "default: off" in texts[0] and "runtime excluded" in texts[0]
    assert "default: 200000" in texts[1]
    assert "Soft" in texts[2] and "at most 50" in texts[2]
    parameters = {p.key: p for p in parameter_defs(ProjectConfig(no_need_for_context=NoNeedForContextConfig(True, 1000, 5)))}
    context_rows = [p for key, p in parameters.items() if key.startswith("no_need_for_context")]
    assert [p.label for p in context_rows] == ["no-need-for-context", "context-threshold-tokens", "context-retain-percent"]
    visible = [options[name].help for name in options if name.startswith("no_need_for_context")]
    visible += [p.label for p in context_rows] + [p.help_text for p in context_rows]
    engines = "Pi, Claude subscription and native Codex"
    for text in visible:
        # The only engine mention allowed is the neutral list of all three supported engines.
        assert "Pi" not in text.replace(engines, ""), text
    assert "Pi, Claude subscription and native Codex" in parameters["no_need_for_context_enabled"].help_text
    assert "Recent-tail target" in parameters["no_need_for_context_retain_percent"].help_text


@pytest.mark.parametrize("enabled", [False, True])
def test_config_output_names_context_without_an_engine_label(monkeypatch, tmp_path, enabled):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("supervisor.main._startup_update_gate", lambda: None)
    monkeypatch.setattr("supervisor.main.run_config_editor", lambda root: ProjectConfig(
        no_need_for_context=NoNeedForContextConfig(enabled, 100000, 12.5)))
    lines = CliRunner().invoke(cli, ["config"]).output.splitlines()
    assert f"no-need-for-context: {str(enabled).lower()}" in lines
    assert ("context-threshold-tokens: 100000" in lines) is enabled
    assert ("context-retain-percent: 12.5" in lines) is enabled
    assert [line for line in lines if line.startswith("no-need-for-context")] == [
        f"no-need-for-context: {str(enabled).lower()}"]  # one neutral line, no engine-specific variant
