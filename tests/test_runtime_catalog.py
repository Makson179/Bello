from __future__ import annotations

import pytest

from supervisor import config_editor


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch):
    monkeypatch.setattr(config_editor, "_model_effort_catalog", {})


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
    assert calls == [("model/list", {"engines": ["pi", "claude-code"], "optionalEngines": True}), "stop"]


def test_api_catalog_never_creates_subscription_alias():
    ids = config_editor._extract_model_ids({"id": "gpt-5.6-sol", "name": "A display label",
        "qualifiedId": "openai/gpt-5.6-sol", "provider": "openai"})
    assert ids == {"openai/gpt-5.6-sol"}


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
