from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from supervisor.adversary_agent import AdversaryAgent
from supervisor.coder import CoderSession
from supervisor.config_editor import EditorState, available_model_choices, parameter_defs, render_editor, select_current
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    DEFAULT_MODEL,
    MODEL_GPT_5_5,
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_SOL,
    MODEL_GPT_5_6_TERRA,
    MODEL_GPT_6_ASTRA,
    SUPPORTED_MODEL_CHOICES,
    ProjectConfig,
    intelligence_choices_for_model,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent


ROLES = ("coder", "runtime", "completion", "adversary")
ASTRA_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


def test_astra_catalog_preserves_existing_defaults_and_effort_limits() -> None:
    assert SUPPORTED_MODEL_CHOICES == (
        MODEL_GPT_6_ASTRA,
        MODEL_GPT_5_6_SOL,
        MODEL_GPT_5_6_TERRA,
        MODEL_GPT_5_6_LUNA,
        MODEL_GPT_5_5,
    )
    assert DEFAULT_MODEL == MODEL_GPT_5_6_SOL
    assert intelligence_choices_for_model(MODEL_GPT_6_ASTRA) == ASTRA_EFFORTS
    assert intelligence_choices_for_model(MODEL_GPT_5_6_SOL) == ASTRA_EFFORTS
    assert intelligence_choices_for_model(MODEL_GPT_5_6_TERRA) == ASTRA_EFFORTS
    assert intelligence_choices_for_model(MODEL_GPT_5_6_LUNA) == ASTRA_EFFORTS[:-1]
    assert intelligence_choices_for_model(MODEL_GPT_5_5) == ASTRA_EFFORTS[:-2]


@pytest.mark.parametrize("effort", ASTRA_EFFORTS)
def test_astra_profiles_round_trip_project_and_runtime_config(tmp_path: Path, effort: str) -> None:
    config = ProjectConfig(
        completion_review=True,
        adversary=True,
        **{f"{role}_mod": MODEL_GPT_6_ASTRA for role in ROLES},
        **{f"{role}_intelligence": effort for role in ROLES},
    )
    save_project_config(tmp_path, config)

    reloaded = load_project_config(tmp_path, create=False)
    settings = _resolve_run_settings(project_config=reloaded)
    runtime_config = StateStore(tmp_path).get_bello_config()

    assert reloaded == config
    for role in ROLES:
        assert getattr(settings, f"{role}_model") == MODEL_GPT_6_ASTRA
        assert getattr(settings, f"{role}_intelligence") == effort
        assert getattr(runtime_config, f"{role}_model") == MODEL_GPT_6_ASTRA
        assert getattr(runtime_config, f"{role}_intelligence") == effort


@pytest.mark.parametrize(
    "efforts",
    [("low", "medium", "high", "xhigh"), ("max", "ultra", "low", "medium")],
)
def test_astra_cli_overrides_are_independent_and_do_not_rewrite_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, efforts: tuple[str, ...]
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\nPrint hello.\n", encoding="utf-8")
    save_project_config(tmp_path, ProjectConfig(task="TASK.md"))
    original_config = project_config_path(tmp_path).read_text(encoding="utf-8")
    captured = []

    async def fake_run_bello(settings):
        captured.append(settings)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("supervisor.main._startup_update_gate", lambda: None)
    monkeypatch.setattr("supervisor.main._run_bello", fake_run_bello)
    monkeypatch.setattr("supervisor.main._run_async_cleanly", asyncio.run)
    arguments = ["--task", str(task)]
    for role, effort in zip(ROLES, efforts):
        arguments.extend([f"--{role}-mod", MODEL_GPT_6_ASTRA, f"--{role}-intelligence", effort])

    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    for role, effort in zip(ROLES, efforts):
        assert getattr(captured[0], f"{role}_model") == MODEL_GPT_6_ASTRA
        assert getattr(captured[0], f"{role}_intelligence") == effort
    assert project_config_path(tmp_path).read_text(encoding="utf-8") == original_config


def _select_option(config: ProjectConfig, key: str, label: str) -> ProjectConfig:
    parameters = parameter_defs(config, model_choices=SUPPORTED_MODEL_CHOICES)
    parameter_index = [parameter.key for parameter in parameters].index(key)
    option_index = [option.label for option in parameters[parameter_index].options].index(label)
    updated, _state, action = select_current(
        config,
        EditorState(parameter_index=parameter_index, expanded_index=parameter_index, option_index=option_index),
        model_choices=SUPPORTED_MODEL_CHOICES,
    )
    assert action is None
    return updated


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(
    ("destination", "expected_effort"),
    [(MODEL_GPT_5_6_SOL, "ultra"), (MODEL_GPT_5_6_LUNA, "max"), (MODEL_GPT_5_5, "xhigh")],
)
def test_editor_switches_astra_per_role_and_restores_model_specific_efforts(
    role: str, destination: str, expected_effort: str
) -> None:
    original = ProjectConfig(completion_review=True, adversary=True)
    config = _select_option(original, f"{role}_mod", "GPT-6 Astra")
    config = _select_option(config, f"{role}_intelligence", "ultra")

    assert getattr(config, f"{role}_mod") == MODEL_GPT_6_ASTRA
    assert getattr(config, f"{role}_intelligence") == "ultra"
    parameters = {parameter.key: parameter for parameter in parameter_defs(config)}
    assert parameters[f"{role}_mod"].value == "GPT-6 Astra"
    assert f"{role}_mod_variant" not in parameters
    assert tuple(option.label for option in parameters[f"{role}_intelligence"].options) == ASTRA_EFFORTS

    family = "GPT-5.5" if destination == MODEL_GPT_5_5 else "GPT-5.6"
    config = _select_option(config, f"{role}_mod", family)
    if destination == MODEL_GPT_5_6_LUNA:
        config = _select_option(config, f"{role}_mod_variant", "Luna")
    assert getattr(config, f"{role}_mod") == destination
    assert getattr(config, f"{role}_intelligence") == expected_effort
    parameters = {parameter.key: parameter for parameter in parameter_defs(config)}
    assert tuple(option.label for option in parameters[f"{role}_intelligence"].options) == intelligence_choices_for_model(destination)

    config = _select_option(config, f"{role}_mod", "GPT-6 Astra")
    assert getattr(config, f"{role}_intelligence") == expected_effort
    parameters = {parameter.key: parameter for parameter in parameter_defs(config)}
    assert f"{role}_mod_variant" not in parameters
    assert tuple(option.label for option in parameters[f"{role}_intelligence"].options) == ASTRA_EFFORTS
    assert replace(config, **{f"{role}_mod": DEFAULT_MODEL, f"{role}_intelligence": "xhigh"}) == original


@pytest.mark.parametrize("source", ["appserver", "cache", "unavailable"])
def test_astra_discovery_uses_live_models_before_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cache = tmp_path / ".codex" / "models_cache.json"
    cache.parent.mkdir()
    cache.write_text(json.dumps({"models": [{"slug": MODEL_GPT_6_ASTRA, "visibility": "list"}]}), encoding="utf-8")
    reported = {
        "appserver": (MODEL_GPT_6_ASTRA,),
        "cache": (),
        "unavailable": (MODEL_GPT_5_6_SOL,),
    }[source]
    monkeypatch.setattr("supervisor.config_editor._available_models_from_app_server", lambda project_root: reported)

    choices = available_model_choices(tmp_path)

    assert (MODEL_GPT_6_ASTRA in choices) is (source != "unavailable")
    options = next(parameter.options for parameter in parameter_defs(ProjectConfig(), choices) if parameter.key == "coder_mod")
    assert ("GPT-6 Astra" in [option.label for option in options]) is (source != "unavailable")
    selected = ProjectConfig(coder_mod=MODEL_GPT_6_ASTRA)
    selected_options = next(parameter.options for parameter in parameter_defs(selected, choices) if parameter.key == "coder_mod")
    assert "GPT-6 Astra" in [option.label for option in selected_options]


def test_editor_renders_astra_family_and_six_efforts() -> None:
    config = ProjectConfig(coder_mod=MODEL_GPT_6_ASTRA, coder_intelligence="ultra")
    parameters = parameter_defs(config)
    effort_index = [parameter.key for parameter in parameters].index("coder_intelligence")
    output = render_editor(
        config,
        EditorState(parameter_index=effort_index, expanded_index=effort_index),
        Path("/tmp/project/.supervisor/config.json"),
        width=120,
        height=24,
    )

    assert "GPT-6 Astra" in output
    assert "coder-5.6-variant" not in output
    for effort in ASTRA_EFFORTS:
        assert effort in output


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("effort", ["low", "ultra"])
async def test_astra_profiles_reach_agent_thread_and_turn_requests(
    store: StateStore, role: str, effort: str
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turn_params = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": "astra-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params.append(params)
            if role == "adversary":
                response = "candidate_finding: false\nattacked: output\nfindings: none\noverall: held"
            elif role == "completion":
                response = json.dumps(
                    {
                        "decision": "accept",
                        "reason": "correct",
                        "message_to_coder": None,
                        "persistent_decision": None,
                        "progress_update": None,
                        "clear_handoff": False,
                        "display_message": None,
                        "handoff": None,
                        "wake_sequence": 1,
                        "generation": 0,
                    }
                )
            else:
                response = json.dumps({"decision": "noop", "reason": "correct"})
            return {"turn": {"id": "astra-turn", "status": "completed", "items": [{"type": "agentMessage", "text": response}]}}

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = RecordingClient()
    task = store.workspace / "TASK.md"
    supervisor = StatelessSupervisorAgent(client, store, task, model=MODEL_GPT_6_ASTRA, intelligence=effort)
    packet = supervisor.build_packet(wake_sequence=1, current_summary="minimal task complete")
    if role == "coder":
        agent = CoderSession(client, store, store.workspace, task, model=MODEL_GPT_6_ASTRA, intelligence=effort)
        assert await agent.start_initial_turn() == "astra-turn"
    elif role == "runtime":
        assert (await supervisor.decide(packet)).decision == "noop"
    elif role == "completion":
        assert (await supervisor.decide_completion(packet)).decision == "accept"
        await supervisor.close_completion_review()
    else:
        agent = AdversaryAgent(client, store.workspace, model=MODEL_GPT_6_ASTRA, intelligence=effort)
        assert (await agent.run(packet)).candidate_finding is False

    assert len(client.thread_params) == 1
    assert len(client.turn_params) == 1
    assert client.thread_params[0]["model"] == MODEL_GPT_6_ASTRA
    assert client.turn_params[0]["model"] == MODEL_GPT_6_ASTRA
    assert client.turn_params[0]["effort"] == effort
