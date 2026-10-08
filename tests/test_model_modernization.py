"""New defaults, legacy choices, and provider-scoped retirement guidance."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from supervisor.config_validation import catalog_from_model_list, model_retirement_notice, validate_project_config
from supervisor.project_config import (
    DEFAULT_MODEL,
    LEGACY_DEFAULT_MODEL,
    MODEL_GPT_5_5,
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_SOL,
    MODEL_GPT_5_6_TERRA,
    MODEL_GPT_6_ASTRA,
    ProjectConfig,
    intelligence_choices_for_model,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.runtime.models import parse_model_selection


ROLES = ('coder', 'revision_coder', 'runtime', 'completion', 'adversary')


def test_new_default_is_verified_native_astra_without_changing_child_or_triage_routes(tmp_path: Path) -> None:
    from supervisor.approval_triage import DEFAULT_TRIAGE_MODEL
    config = load_project_config(tmp_path)
    assert DEFAULT_MODEL == MODEL_GPT_6_ASTRA
    assert all(getattr(config, f'{role}_mod') == MODEL_GPT_6_ASTRA for role in ROLES)
    assert all(getattr(config, f'{role}_intelligence') == 'xhigh' for role in ROLES)
    assert config.multi_agent.default.model == MODEL_GPT_5_6_LUNA
    assert DEFAULT_TRIAGE_MODEL == MODEL_GPT_5_6_LUNA
    assert parse_model_selection(DEFAULT_MODEL).billing_route == 'subscription'
    reloaded = load_project_config(tmp_path, create=False)
    assert all(getattr(reloaded, f'{role}_mod') == MODEL_GPT_6_ASTRA for role in ROLES)


def test_existing_partial_config_retains_effective_legacy_defaults_and_bytes(tmp_path: Path) -> None:
    path = project_config_path(tmp_path)
    path.parent.mkdir()
    original = '{"task": "TASK.md", "coder_mod": "openai/gpt-5.5"}\n'
    path.write_text(original)
    config = load_project_config(tmp_path)
    assert config.coder_mod == config.revision_coder_mod == 'openai/gpt-5.5'
    assert parse_model_selection(config.coder_mod).billing_route == 'provider-api'
    for role in ('runtime', 'completion', 'adversary'):
        assert getattr(config, f'{role}_mod') == LEGACY_DEFAULT_MODEL == MODEL_GPT_5_6_SOL
    assert path.read_text() == original


@pytest.mark.parametrize('model', [MODEL_GPT_5_6_SOL, MODEL_GPT_5_6_TERRA, MODEL_GPT_5_6_LUNA, MODEL_GPT_5_5])
def test_existing_model_choices_keep_every_supported_effort(tmp_path: Path, model: str) -> None:
    for effort in intelligence_choices_for_model(model):
        config = ProjectConfig(adversary=True, **{
            **{f'{role}_mod': model for role in ROLES},
            **{f'{role}_intelligence': effort for role in ROLES},
        })
        save_project_config(tmp_path, config)
        original = project_config_path(tmp_path).read_bytes()
        assert load_project_config(tmp_path, create=False) == config
        assert project_config_path(tmp_path).read_bytes() == original


@pytest.mark.parametrize('model', ['gpt-5.5', 'openai-codex/gpt-5.5'])
def test_retirement_notice_is_scoped_and_does_not_rewrite_subscription_choice(model: str) -> None:
    notice = model_retirement_notice(model)
    assert notice is not None
    assert 'October 14, 2026' in notice
    assert 'ChatGPT sign-in' in notice
    assert 'OpenAI API is unaffected' in notice
    config = ProjectConfig(coder_mod=model, runtime_enabled=False)
    report = validate_project_config(config, None)
    retirement = [issue for issue in report.issues if issue.category == 'retirement']
    assert len(retirement) == 1
    assert retirement[0].level == 'warning'
    assert config.coder_mod == model
    assert parse_model_selection(config.coder_mod).billing_route == 'subscription'


@pytest.mark.parametrize('model', ['openai/gpt-5.5', 'openrouter/openai/gpt-5.5', 'gpt-5.6-sol', 'gpt-6-astra'])
def test_retirement_does_not_conflate_api_or_other_models(model: str) -> None:
    assert model_retirement_notice(model) is None


def test_newer_announced_profiles_still_require_exact_discovered_capabilities(tmp_path: Path) -> None:
    model = 'openai-codex/gpt-6.1-sol'
    config = ProjectConfig(coder_mod=model, runtime_enabled=False)
    empty = catalog_from_model_list({'data': []})
    assert any(issue.category == 'availability' for issue in validate_project_config(config, empty).issues)
    catalog = catalog_from_model_list({'data': [{
        'qualifiedId': model, 'provider': 'openai-codex', 'model': 'gpt-6.1-sol',
        'supportedEfforts': ['high', 'xhigh'], 'available': True,
    }]})
    assert not validate_project_config(config, catalog).issues
    save_project_config(tmp_path, config)
    assert json.loads(project_config_path(tmp_path).read_text())['coder_mod'] == model
