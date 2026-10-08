"""Haiku 5.5 discovery and saved-profile compatibility on the subscription route.

The fixture contains the model rows captured on 2026-10-07 from official Claude
Code 2.1.293 via claude-agent-sdk 0.2.164 (whose declared bundled CLI is 2.1.292).
The separate signed-out metadata probe used a verified official executable and
sent no prompt or model request. These offline tests replay those rows, not a
claim about authenticated account entitlement or live generation. Old CLI rows
remain covered independently; no static model is inserted into production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor import config_editor
from supervisor.appserver import AppServerError
from supervisor.config_validation import catalog_from_model_list, validate_project_config
from supervisor.controller import _selected_model_availability
from supervisor.project_config import (
    MultiAgentConfig,
    ProjectConfig,
    SubagentDefaultConfig,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.runtime.models import NO_EFFORT, engine_effort, parse_model_selection
from tests.test_claude_sonnet55_config import (
    SONNET_55_CLI_MODELS,
    advisor_config,
    advisor_module,
    choose,
    claude,
    editor_runtime,  # noqa: F401 - shared offline discovery fixture
    isolated_effort_catalog,  # noqa: F401 - shared autouse fixture
    parameter,
    project_root,
    queries,
    runtime_catalog,
)
from tests.test_runtime_claude import result_message, wait_completed


HAIKU_55_CLI_MODELS = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_code_2_1_293_models.json").read_text(encoding="utf-8")
)
HAIKU_55 = "claude-code/claude-haiku-5-5"
HAIKU_ALIAS = "claude-code/haiku"
ADVERTISED = tuple(next(row for row in HAIKU_55_CLI_MODELS if row["value"] == "haiku")["supportedEffortLevels"])


async def test_haiku_55_catalog_preserves_exact_identity_alias_and_efforts(tmp_path):
    engine, factory, _ = claude(tmp_path, HAIKU_55_CLI_MODELS)
    try:
        response = await engine.request("model/list", {})
        by_id = {entry["id"]: entry for entry in response["data"]}
        assert ADVERTISED == ("low", "medium", "high", "xhigh", "max")
        for model, alias in (("claude-haiku-5-5", False), ("haiku", True)):
            entry = by_id[model]
            assert entry["qualifiedId"] == f"claude-code/{model}"
            assert entry["resolvedModel"] == "claude-haiku-5-5"
            assert entry["alias"] is alias
            assert entry["billingRoute"] == "subscription"
            assert entry["supportedEfforts"] == list(ADVERTISED)
            assert entry["supportedReasoningEfforts"] == list(ADVERTISED)
            assert entry["supportsServiceTier"] is False
        assert by_id["claude-haiku-5-5"]["description"].startswith("Haiku 5.5")
        assert queries(factory) == []
    finally:
        await engine.stop()


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "haiku"])
@pytest.mark.parametrize("effort", ADVERTISED)
async def test_haiku_55_advertised_efforts_validate_without_query(tmp_path, model, effort):
    engine, factory, _ = claude(tmp_path, HAIKU_55_CLI_MODELS)
    try:
        result = await engine.request("model/validate", {
            "provider": "claude-code", "model": model, "effort": effort,
        })
        assert result["valid"] is True
        assert result["model"]["resolvedModel"] == "claude-haiku-5-5"
        assert result["execution"] == {"engine": "claude-code", "effort": effort}
        assert queries(factory) == []
    finally:
        await engine.stop()


@pytest.mark.parametrize("parameters,match", [
    ({"effort": "ultra"}, "not supported"),
    ({"effort": "minimal"}, "not supported"),
    ({"effort": "off"}, "not supported"),
    ({"serviceTier": "priority"}, "service tier"),
    ({"provider": "anthropic"}, "provider 'claude-code'"),
])
async def test_haiku_55_does_not_invent_efforts_tiers_or_api_routes(tmp_path, parameters, match):
    engine, factory, _ = claude(tmp_path, HAIKU_55_CLI_MODELS)
    try:
        with pytest.raises(AppServerError, match=match):
            await engine.request("model/validate", {
                "provider": "claude-code", "model": "claude-haiku-5-5", **parameters,
            })
        assert queries(factory) == []
    finally:
        await engine.stop()


async def test_old_cli_does_not_invent_haiku_55_or_query_it(tmp_path):
    engine, factory, events = claude(tmp_path, SONNET_55_CLI_MODELS, [result_message()])
    try:
        response = await engine.request("model/list", {})
        by_id = {entry["id"]: entry for entry in response["data"]}
        assert by_id["haiku"]["resolvedModel"] == "claude-haiku-4-5-20251001"
        assert "claude-haiku-5-5" not in by_id
        with pytest.raises(AppServerError, match="did not advertise model 'claude-haiku-5-5'"):
            await engine.request("model/validate", {"provider": "claude-code", "model": "claude-haiku-5-5"})
        await engine.request("thread/start", {
            "threadId": "old-cli", "provider": "claude-code", "model": "claude-haiku-5-5",
            "cwd": str(tmp_path), "tools": [], "effort": None,
        })
        await engine.request("turn/start", {
            "threadId": "old-cli", "turnId": "turn", "input": [{"type": "text", "text": "offline fixture"}],
        })
        completed = await wait_completed(events)
        assert completed["params"]["turn"]["status"] == "failed"
        assert "no model request was sent" in completed["params"]["turn"]["error"]["message"]
        assert queries(factory) == []
    finally:
        await engine.stop()


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "haiku"])
@pytest.mark.parametrize("effort", [NO_EFFORT, "low", "max"])
async def test_haiku_55_mock_turn_preserves_requested_model_and_effort(tmp_path, model, effort):
    engine, factory, events = claude(tmp_path, HAIKU_55_CLI_MODELS, [result_message()])
    try:
        await engine.request("thread/start", {
            "threadId": "haiku", "provider": "claude-code", "model": model,
            "cwd": str(tmp_path), "tools": [], "effort": engine_effort(effort),
        })
        await engine.request("turn/start", {
            "threadId": "haiku", "turnId": "turn", "input": [{"type": "text", "text": "offline fixture"}],
        })
        assert (await wait_completed(events))["params"]["turn"]["status"] == "completed"
        options = factory.clients[-1].options
        assert options.model == model
        assert options.effort == engine_effort(effort)
        assert options.fallback_model is None
    finally:
        await engine.stop()


@pytest.mark.parametrize("models,available", [(HAIKU_55_CLI_MODELS, True), (SONNET_55_CLI_MODELS, False)])
async def test_every_role_preflight_requires_haiku_55_in_the_actual_catalog(tmp_path, models, available):
    response = await runtime_catalog(tmp_path, models)
    result = _selected_model_availability(
        response, coder_model=HAIKU_55, runtime_model=HAIKU_55, completion_model=HAIKU_55,
        adversary_model=HAIKU_55, revision_coder_model=HAIKU_55, subagent_models=(HAIKU_55,),
    )
    assert result.ok is available
    if not available:
        assert len(result.missing_roles) == 6
    assert parse_model_selection(HAIKU_55).billing_route == "subscription"
    assert parse_model_selection("anthropic/claude-haiku-5-5").billing_route == "provider-api"


def test_editor_selects_and_persists_exact_haiku_55_and_child_profile(tmp_path, editor_runtime):
    factories = editor_runtime(HAIKU_55_CLI_MODELS)
    project = project_root(tmp_path)
    choices = config_editor.available_model_choices(project)
    assert {HAIKU_55, HAIKU_ALIAS} <= set(choices)
    assert not any(choice.startswith(("anthropic/", "openrouter/")) for choice in choices)
    assert config_editor.intelligence_choices_for_model(HAIKU_55) == ADVERTISED
    config = ProjectConfig(coder_mod="gpt-6-astra", coder_intelligence="ultra", runtime_enabled=False,
                           cheap_runtime=False, adversary_runs=0)
    selected = choose(config, choices, "coder_mod", HAIKU_55)
    assert (selected.coder_mod, selected.coder_intelligence) == (HAIKU_55, "max")
    assert tuple(option.value for option in parameter(selected, choices, "coder_intelligence").options) == ADVERTISED
    selected = ProjectConfig(**{**selected.__dict__, "multi_agent": MultiAgentConfig(
        enabled=True, default=SubagentDefaultConfig(HAIKU_55, "low"), allowed={HAIKU_55: ("low", "max")},
    )})
    save_project_config(project, selected)
    reloaded = load_project_config(project, create=False)
    assert reloaded == selected
    assert validate_project_config(reloaded, choices.catalog).errors == ()
    assert reloaded.multi_agent.is_allowed(HAIKU_55, "max")
    assert not reloaded.multi_agent.is_allowed("anthropic/claude-haiku-5-5", "max")
    assert all(queries(factory) == [] for factory in factories)


def test_alias_refresh_preserves_saved_default_and_child_policy(tmp_path, editor_runtime):
    project = project_root(tmp_path)
    config = ProjectConfig(coder_mod=HAIKU_ALIAS, coder_intelligence=NO_EFFORT, runtime_enabled=False,
                           cheap_runtime=False, adversary_runs=0,
                           multi_agent=MultiAgentConfig(
                               enabled=True, default=SubagentDefaultConfig(HAIKU_ALIAS, NO_EFFORT),
                               allowed={HAIKU_ALIAS: (NO_EFFORT,)},
                           ))
    save_project_config(project, config)
    saved = project_config_path(project).read_bytes()
    editor_runtime(SONNET_55_CLI_MODELS)
    old = config_editor.available_model_choices(project)
    assert config_editor.intelligence_choices_for_model(HAIKU_ALIAS) == (NO_EFFORT,)
    assert old.catalog.get(HAIKU_ALIAS).resolved_model == "claude-haiku-4-5-20251001"
    editor_runtime(HAIKU_55_CLI_MODELS)
    current = config_editor.available_model_choices(project)
    assert config_editor.intelligence_choices_for_model(HAIKU_ALIAS) == ADVERTISED
    assert current.catalog.get(HAIKU_ALIAS).resolved_model == "claude-haiku-5-5"
    reloaded = load_project_config(project, create=False)
    assert reloaded == config
    assert validate_project_config(reloaded, current.catalog).errors == ()
    config_editor.parameter_defs(reloaded, current)
    config_editor.render_editor(reloaded, config_editor.EditorState(), project_config_path(project), current,
                                width=132, height=44)
    assert project_config_path(project).read_bytes() == saved
    assert reloaded.multi_agent.is_allowed(HAIKU_ALIAS, NO_EFFORT)


async def test_advisor_and_core_agree_on_haiku_55_default_and_explicit_effort(tmp_path):
    validator = advisor_module("validate_config")
    current = await runtime_catalog(tmp_path / "current", HAIKU_55_CLI_MODELS)
    old = await runtime_catalog(tmp_path / "old", SONNET_55_CLI_MODELS)

    def validate(config, catalog=current):
        return validator.validate(config, catalog=catalog, allow_clean=False, allow_unlimited=False)

    for model in (HAIKU_55, HAIKU_ALIAS):
        for effort in (*ADVERTISED, NO_EFFORT):
            candidate = advisor_config()
            candidate["coder_mod"], candidate["coder_intelligence"] = model, effort
            candidate["multi_agent"] = {"enabled": True, "max_concurrent": 1,
                                        "default": {"model": model, "intelligence": effort},
                                        "allowed": {model: [effort]}}
            assert validate(candidate) == []
            core = ProjectConfig(coder_mod=model, coder_intelligence=effort, runtime_enabled=False)
            assert validate_project_config(core, catalog_from_model_list(current)).errors == ()
    candidate["coder_mod"] = HAIKU_55
    candidate["coder_intelligence"] = "ultra"
    assert any("'ultra' is not advertised" in error for error in validate(candidate))
    candidate["coder_intelligence"] = "low"
    assert any("not available" in error for error in validate(candidate, old))
    candidate["coder_mod"] = "anthropic/claude-haiku-5-5"
    assert any("not available" in error for error in validate(candidate))
