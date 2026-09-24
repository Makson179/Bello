from __future__ import annotations

import pytest

from supervisor import config_editor
from supervisor.project_config import MultiAgentConfig, ProjectConfig, SubagentDefaultConfig


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(config_editor, "_model_effort_catalog", {})


@pytest.fixture
def runtime_catalog(monkeypatch):
    def install(descriptors=(), *, error=None):
        calls = []

        class Client:
            def __init__(self, **kwargs):
                pass

            async def start(self):
                pass

            async def initialize(self):
                pass

            async def stop(self):
                calls.append("stop")

            async def request(self, method, params):
                calls.append((method, params))
                if error is not None:
                    raise error
                return {"data": list(descriptors)}

        monkeypatch.setattr(config_editor, "RuntimeClient", Client)
        return calls

    return install


def test_editor_uses_both_engines_and_exact_advertised_efforts(monkeypatch, tmp_path):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            pass

        async def initialize(self):
            pass

        async def stop(self):
            calls.append("stop")

        async def request(self, method, params):
            calls.append((method, params))
            return {"data": [
                {"id": "sonnet", "qualifiedId": "claude-code/sonnet", "provider": "claude-code",
                 "supportedEfforts": ["low", "medium", "high", "max"]},
                {"id": "gpt-5.6-sol", "qualifiedId": "openai/gpt-5.6-sol", "provider": "openai",
                 "supportedEfforts": ["low", "high"]},
                {"id": "gpt-5.6-sol", "qualifiedId": "openai-codex/gpt-5.6-sol", "provider": "openai-codex",
                 "supportedEfforts": ["low", "medium", "high", "xhigh", "max", "ultra"]},
            ]}

    monkeypatch.setattr(config_editor, "RuntimeClient", Client)
    choices = config_editor.available_model_choices(tmp_path)
    assert "claude-code/sonnet" in choices
    assert "openai/gpt-5.6-sol" in choices
    assert config_editor.intelligence_choices_for_model("claude-code/sonnet") == ("low", "medium", "high", "max")
    assert config_editor.intelligence_choices_for_model("openai/gpt-5.6-sol") == ("low", "high")
    assert "ultra" in config_editor.intelligence_choices_for_model("gpt-5.6-sol")
    assert calls == [("model/list", {"engines": ["codex", "pi", "claude-code"], "optionalEngines": True}), "stop"]


def test_api_catalog_never_creates_subscription_alias():
    ids = config_editor._extract_model_ids({"id": "gpt-5.6-sol", "name": "A display label",
        "qualifiedId": "openai/gpt-5.6-sol", "provider": "openai"})
    assert ids == {"openai/gpt-5.6-sol"}


@pytest.mark.parametrize("models", [
    ("claude-code/sonnet",),
    ("google/gemini-2.5-pro", "openrouter/qwen/qwen3-coder"),
])
def test_live_connected_models_are_the_complete_choice_list(monkeypatch, tmp_path, runtime_catalog, models):
    runtime_catalog([
        {"id": model.split("/", 1)[1], "qualifiedId": model,
         "provider": model.split("/", 1)[0], "configured": True, "available": True,
         "supportedEfforts": ["low", "high"]}
        for model in models
    ])
    cache_reads = []
    monkeypatch.setattr(config_editor, "_available_models_from_cache", lambda: cache_reads.append(True) or ("gpt-5.5",))

    choices = config_editor.available_model_choices(tmp_path)

    assert choices == models
    assert cache_reads == []
    assert set(config_editor._model_effort_catalog) == set(models)
    for model in models:
        assert config_editor.intelligence_choices_for_model(model) == ("low", "high")


@pytest.mark.parametrize("failed", [False, True], ids=["no-connected-providers", "catalog-failure"])
def test_empty_or_failed_live_catalog_does_not_offer_cached_or_default_models(
    monkeypatch, tmp_path, runtime_catalog, failed,
):
    calls = runtime_catalog(error=RuntimeError("catalog unavailable") if failed else None)
    cache_reads = []
    monkeypatch.setattr(config_editor, "_available_models_from_cache", lambda: cache_reads.append(True) or ("gpt-5.5",))

    assert config_editor.available_model_choices(tmp_path) == ()
    assert cache_reads == []
    assert calls == [("model/list", {"engines": ["codex", "pi", "claude-code"], "optionalEngines": True}), "stop"]


@pytest.mark.parametrize("unavailable", [
    {"configured": False},
    {"available": False},
    {"hidden": True},
    {"visibility": "hidden"},
])
def test_unavailable_descriptors_cannot_supply_choices_or_efforts(tmp_path, runtime_catalog, unavailable):
    disconnected = {
        "id": "gpt-5.6-sol", "qualifiedId": "openai-codex/gpt-5.6-sol", "provider": "openai-codex",
        "supportedEfforts": ["low"], **unavailable,
    }
    connected = {"id": "sonnet", "qualifiedId": "claude-code/sonnet", "provider": "claude-code"}
    runtime_catalog([disconnected, connected])

    assert config_editor._extract_model_ids(disconnected) == set()
    assert config_editor.available_model_choices(tmp_path) == ("claude-code/sonnet",)
    assert "gpt-5.6-sol" not in config_editor._model_effort_catalog
    assert "openai-codex/gpt-5.6-sol" not in config_editor._model_effort_catalog


@pytest.mark.parametrize("connected", [
    ("claude-code/sonnet",),
    ("google/gemini-2.5-pro", "openrouter/qwen/qwen3-coder"),
    (),
])
def test_saved_disconnected_role_models_are_preserved_but_never_offered(connected):
    config = ProjectConfig(
        coder_mod="gpt-6-astra",
        revision_coder_enabled=True,
        revision_coder_mod="gpt-5.6-sol",
        runtime_mod="gpt-5.5",
        completion_review=True,
        completion_mod="openai/gpt-4.1",
        adversary=True,
        adversary_mod="anthropic/claude-sonnet-4-6",
    )
    saved = config.to_json_data()
    parameters = config_editor.parameter_defs(config, connected)
    by_key = {parameter.key: parameter for parameter in parameters}

    assert config_editor._model_choices_for_config(config, connected) == connected
    for field in ("coder_mod", "revision_coder_mod", "runtime_mod", "completion_mod", "adversary_mod"):
        assert {option.value for option in by_key[field].options} == set(connected)
        assert all(
            option.value in connected
            for parameter in parameters
            for option in parameter.options
            if option.field == field
        )
        assert config_editor._model_family_label(getattr(config, field)) in by_key[field].value
    assert config.to_json_data() == saved


@pytest.mark.parametrize("connected", [("claude-code/sonnet",), ()])
def test_disconnected_subagents_are_preserved_but_not_selectable(connected):
    settings = MultiAgentConfig(
        enabled=True,
        default=SubagentDefaultConfig(model="gpt-5.6-luna", intelligence="high"),
        allowed={
            "gpt-5.6-luna": ("high",),
            "openai/gpt-4.1": ("off",),
            "claude-code/sonnet": ("high",),
        },
    )
    config = ProjectConfig(
        completion_review=True,
        adversary=True,
        multi_agent=settings,
        completion_multi_agent=settings,
        adversary_multi_agent=settings,
    )
    saved = config.to_json_data()
    parameters = config_editor.parameter_defs(config, connected)
    by_key = {parameter.key: parameter for parameter in parameters}

    for group in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
        field = f"{group}_default_model"
        assert {option.value for option in by_key[field].options} == set(connected)
        assert all(
            option.value in connected
            for parameter in parameters
            for option in parameter.options
            if option.field == field
        )
        prefix = f"{group}_allowed:"
        assert {parameter.key.removeprefix(prefix) for parameter in parameters if parameter.key.startswith(prefix)} == set(connected)
    assert config.to_json_data() == saved


@pytest.mark.parametrize("group", ["multi_agent", "completion_multi_agent", "adversary_multi_agent"])
def test_unavailable_subagent_cleanup_requires_connected_default_and_explicit_selection(group):
    connected = ("claude-code/sonnet",)
    settings = MultiAgentConfig(
        enabled=True,
        default=SubagentDefaultConfig(model="gpt-5.6-luna", intelligence="high"),
        allowed={
            "gpt-5.6-luna": ("high",),
            "openai/gpt-4.1": ("off",),
            "claude-code/sonnet": ("high",),
        },
    )
    config = ProjectConfig(
        completion_review=True,
        adversary=True,
        multi_agent=settings,
        completion_multi_agent=settings,
        adversary_multi_agent=settings,
    )
    saved = config.to_json_data()
    cleanup_key = f"{group}_unavailable_profiles"

    def parameter_state(current, key):
        parameters = config_editor.parameter_defs(current, connected)
        index = next(index for index, parameter in enumerate(parameters) if parameter.key == key)
        return parameters[index], config_editor.EditorState(
            parameter_index=index, expanded_index=index, option_index=0,
        )

    cleanup, state = parameter_state(config, cleanup_key)
    assert cleanup.value == "2"
    assert cleanup.options == ()
    unchanged, _, _ = config_editor.select_current(config, state, connected)
    assert unchanged.to_json_data() == saved

    default, state = parameter_state(config, f"{group}_default_model")
    assert [option.value for option in default.options] == ["claude-code/sonnet"]
    with_connected_default, _, _ = config_editor.select_current(config, state, connected)
    assert getattr(with_connected_default, group).default.model == "claude-code/sonnet"
    assert getattr(with_connected_default, group).allowed == settings.allowed
    cleanup, state = parameter_state(with_connected_default, cleanup_key)
    assert [option.label for option in cleanup.options] == ["remove unavailable profiles"]
    assert getattr(with_connected_default, group).allowed == settings.allowed

    cleaned, _, action = config_editor.select_current(with_connected_default, state, connected)
    assert action is None
    assert getattr(cleaned, group).allowed == {"claude-code/sonnet": ("high",)}
    assert getattr(cleaned, group).default == SubagentDefaultConfig(model="claude-code/sonnet", intelligence="high")
    parameters = config_editor.parameter_defs(cleaned, connected)
    assert cleanup_key not in {parameter.key for parameter in parameters}
    for other_group in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
        if other_group != group:
            assert getattr(cleaned, other_group) == getattr(config, other_group)
        assert {
            option.value for parameter in parameters for option in parameter.options
            if option.field == f"{other_group}_default_model"
        } == set(connected)
    assert config.to_json_data() == saved


@pytest.mark.parametrize("connected", [("claude-code/sonnet",), ()])
def test_editor_status_warns_when_saved_model_is_unavailable(tmp_path, connected):
    config = ProjectConfig(coder_mod="gpt-6-astra", runtime_enabled=False)

    rendered = config_editor.render_editor(
        config, config_editor.EditorState(), tmp_path / "config.json", connected,
        width=120, height=40,
    )

    assert "Check model access" in rendered
    assert "Saved model unavailable" in rendered
    assert "Config valid" not in rendered


def test_disconnected_selected_gpt_variant_cannot_reenter_connected_family_options():
    config = ProjectConfig(coder_mod="gpt-5.6-sol")
    parameters = config_editor.parameter_defs(config, ("gpt-5.6-luna",))
    options = [option for parameter in parameters for option in parameter.options if option.field == "coder_mod"]

    assert options
    assert {option.value for option in options} == {"gpt-5.6-luna"}
    assert config.coder_mod == "gpt-5.6-sol"


def test_refresh_discards_previous_accounts_capabilities(monkeypatch, tmp_path):
    config_editor._model_effort_catalog["gpt-5.6-sol"] = ("low",)
    monkeypatch.setattr(config_editor, "_available_models_from_app_server", lambda root: ())
    monkeypatch.setattr(config_editor, "_available_models_from_cache", lambda: ())
    config_editor.available_model_choices(tmp_path)
    assert "xhigh" in config_editor.intelligence_choices_for_model("gpt-5.6-sol")


@pytest.mark.parametrize("model", ["openai/gpt-4.1", "openai-codex/nonreasoning-model", "openrouter/qwen/coder"])
def test_provider_qualified_nonreasoning_profile_survives_saved_config(tmp_path, model):
    import json
    from supervisor.project_config import load_project_config, project_config_path

    path = project_config_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({
        "coder_mod": model, "coder_intelligence": "off",
        "revision_coder_mod": model, "revision_coder_intelligence": "off",
        "runtime_mod": model, "runtime_intelligence": "off",
        "completion_mod": model, "completion_intelligence": "off",
        "adversary_mod": model, "adversary_intelligence": "off",
        "multi_agent": {"enabled": True,
            "default": {"model": model, "intelligence": "off"},
            "allowed": {model: ["off"]}},
    }))
    config = load_project_config(tmp_path, create=False)
    assert config.coder_mod == model
    assert config.coder_intelligence == "off"
    assert config.multi_agent.is_allowed(model, "off")
