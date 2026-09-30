"""Configuration correctness shared by `bello config` and run preflight (offline).

Discovery goes through the real RuntimeClient -> ClaudeBackend path with the
recorded CLI metadata from test_claude_sonnet55_config; no CLI runs, no login,
and no model request is made.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from supervisor import config_editor
from supervisor.appserver import AppServerError
from supervisor.coder import apply_intelligence, build_multi_agent_developer_instructions
from supervisor.config_validation import (
    ModelCatalog,
    catalog_entry,
    catalog_from_model_list,
    choose_effort,
    claude_model_name,
    project_profiles,
    validate_project_config,
)
from supervisor.controller import BelloController, _readable_available_models
from supervisor.main import _resolve_run_settings, cli
from supervisor.project_config import (
    MultiAgentConfig,
    ProjectConfig,
    SubagentDefaultConfig,
    load_project_config,
    save_project_config,
)
from supervisor.runtime import sandbox
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.engine_status import classify_engine_failure
from supervisor.runtime.models import NO_EFFORT
from tests.test_claude_sonnet55_config import (
    PINNED_CLI_MODELS,
    SONNET_55_CLI_MODELS,
    Unavailable,
    claude,
    queries,
    runtime_catalog,
)
from tests.test_runtime_claude import result_message, wait_completed


SONNET_55 = "claude-code/claude-sonnet-5-5"
HAIKU = "claude-code/haiku"
# The signed-in catalog also lists the previous Sonnet 5 and no Opus [1m] ids.
AUTHENTICATED = copy.deepcopy(SONNET_55_CLI_MODELS) + [{
    "value": "claude-sonnet-5", "resolvedModel": "claude-sonnet-5", "displayName": "Sonnet 5",
    "description": "Sonnet 5 · Previous generation", "supportsEffort": True,
    "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
}]


@pytest.fixture(autouse=True)
def isolated_effort_catalog(monkeypatch):
    monkeypatch.setattr(config_editor, "_model_effort_catalog", {})


@pytest.fixture
def discover(monkeypatch, tmp_path):
    """Run the editor's real discovery against the Claude engine; other engines fail."""

    def install(models, *, codex="native Codex login required", pi="Pi dependencies are not installed",
                claude_engine=None):
        def make_client(*, cwd, **kwargs):
            engine = claude_engine or claude(tmp_path / "engine", models)[0]
            return RuntimeClient(cwd=cwd, backends={
                "codex": Unavailable(codex), "pi": Unavailable(pi), "claude-code": engine,
            }, **kwargs)

        monkeypatch.setattr(config_editor, "RuntimeClient", make_client)
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        return config_editor.available_model_choices(project)

    return install


def params(config, choices):
    return {parameter.key: parameter for parameter in config_editor.parameter_defs(config, choices)}


def rendered(config, choices, state=None):
    return config_editor.render_editor(config, state or config_editor.EditorState(), Path("/p/config.json"),
                                       choices, width=132, height=44)


def claude_only(**overrides) -> ProjectConfig:
    values = dict(coder_mod=SONNET_55, coder_intelligence="high", runtime_mod=SONNET_55,
                  runtime_intelligence="medium", cheap_runtime=False)
    values.update(overrides)
    return ProjectConfig(**values)


# --- Fast is Bello's OpenAI/Codex priority tier ------------------------------------------


def test_fast_help_names_the_priority_tier_and_what_it_is_not(discover):
    choices = discover(AUTHENTICATED)
    help_text = params(ProjectConfig(), choices)["speed"].help_text
    assert "OpenAI/Codex priority service tier" in help_text
    assert "not Smart Execution" in help_text and "not Claude Code's differently named fast mode" in help_text
    assert "every active role and allowed subagent profile" in help_text
    cli_help = " ".join(CliRunner().invoke(cli, ["--help"]).output.split())
    assert "priority service tier" in cli_help and "Not Smart Execution" in cli_help


def test_fast_with_claude_roles_is_never_ready_and_explains_at_speed(discover):
    choices = discover(AUTHENTICATED)
    config = claude_only(speed="fast")
    by_key = params(config, choices)
    assert by_key["speed"].issue_level == "error"
    assert "priority service tier" in by_key["speed"].issue and SONNET_55 in by_key["speed"].issue
    assert by_key["coder_mod"].issue_title == "Fast not available"
    assert [option.label for option in by_key["speed"].options] == [
        "usual", f"fast - not offered by {SONNET_55}"]
    output = rendered(config, choices)
    assert "Fast not available" in output and "Ready" not in output
    assert config.speed == "fast"  # flagged, never silently switched


@pytest.mark.parametrize("where", ["subagent", "adversary"])
def test_fast_checks_every_active_profile_including_children(discover, where):
    choices = discover(AUTHENTICATED)
    codex = "openai-codex/gpt-5.6-sol"
    catalog = ModelCatalog(models={**choices.catalog.models, codex: catalog_entry({
        "qualifiedId": codex, "provider": "openai-codex", "supportedEfforts": ["high"]})}, discovered=True)
    base = dict(coder_mod=codex, coder_intelligence="high", runtime_enabled=False, speed="fast")
    if where == "subagent":
        config = ProjectConfig(**base, multi_agent=MultiAgentConfig(
            enabled=True, default=SubagentDefaultConfig(codex, "high"), allowed={codex: ("high",), SONNET_55: ("high",)}))
        inactive = ProjectConfig(**base, multi_agent=MultiAgentConfig(
            enabled=False, default=SubagentDefaultConfig(codex, "high"), allowed={codex: ("high",), SONNET_55: ("high",)}))
    else:
        config = ProjectConfig(**base, adversary=True, adversary_mod=SONNET_55, adversary_intelligence="high")
        inactive = ProjectConfig(**base, adversary=False, adversary_mod=SONNET_55, adversary_intelligence="high")
    assert [issue.category for issue in validate_project_config(config, catalog).errors] == ["fast"]
    assert validate_project_config(inactive, catalog).errors == ()


@pytest.mark.parametrize("tiers,supported", [
    ({"supportsServiceTier": True, "supportedServiceTiers": ["auto", "priority"]}, True),
    ({"supportsServiceTier": True, "supportedServiceTiers": ["flex"]}, False),
    ({"supportsServiceTier": False, "supportedServiceTiers": []}, False),
])
def test_fast_respects_the_advertised_service_tier(tiers, supported):
    model = "openai/gpt-5.6-sol"
    catalog = ModelCatalog(models={model: catalog_entry({
        "qualifiedId": model, "provider": "openai", "supportedEfforts": ["high"], **tiers})}, discovered=True)
    config = ProjectConfig(coder_mod=model, coder_intelligence="high", runtime_enabled=False, speed="fast")
    errors = validate_project_config(config, catalog).errors
    assert (errors == ()) is supported


# --- The side panel agrees with run preflight ---------------------------------------------


def preflight_controller(config: ProjectConfig, client, monkeypatch, tmp_path):
    """A controller whose accessors come from `config`; everything after validation is stubbed."""
    controller = object.__new__(BelloController)
    controller.client = client
    controller.tui = SimpleNamespace(status=lambda _text: None, render=lambda *_args: None)
    controller.store = SimpleNamespace(update_bello_config=lambda _transform: None,
                                       get_bello_config=lambda: SimpleNamespace(status=None))
    adversary = config.adversary and config.adversary_runs > 0
    for role in ("coder", "runtime", "completion", "adversary", "revision_coder"):
        setattr(controller, f"_{role}_model", lambda role=role: getattr(config, f"{role}_mod"))
        setattr(controller, f"_{role}_intelligence", lambda role=role: getattr(config, f"{role}_intelligence"))
    controller._runtime_enabled = lambda: config.runtime_enabled
    controller._effective_completion_review = lambda: config.completion_review
    controller._adversary_model_required_for_preflight = lambda: adversary
    controller._revision_coder_enabled = lambda: config.revision_coder_enabled
    controller._enabled_subagent_models_for_preflight = lambda: tuple(dict.fromkeys(
        model for use in project_profiles(config) if use.role == "subagent" for model in (use.model,)))
    controller._multi_agent_config = lambda: config.multi_agent
    controller._completion_multi_agent_config = lambda: config.completion_multi_agent
    controller._adversary_multi_agent_config = lambda: config.adversary_multi_agent
    controller._persist_model_config = lambda: None
    controller._fast_mode = lambda: config.fast
    controller._active_workspace_root = lambda: tmp_path
    controller._log_distiller_config = lambda: SimpleNamespace(enabled=False)

    async def availability(models):
        from supervisor.controller import _selected_model_availability
        result = _selected_model_availability(models, coder_model=config.coder_mod, runtime_model=None,
                                              completion_model=None,
                                              subagent_models=controller._enabled_subagent_models_for_preflight())
        if not result.ok:
            raise RuntimeError("availability preflight failed")

    async def passed():
        return None

    class Runner:
        def __init__(self, _policy):
            pass

        async def run(self, *_args):
            return SimpleNamespace(exit_code=0, output="bello-sandbox-probe")

    controller._ensure_selected_models_available = availability
    controller._structured_output_self_test = passed
    controller._configure_runtime_triage = passed
    monkeypatch.setattr(sandbox, "SandboxRunner", Runner)
    return controller


HAIKU_POOL = MultiAgentConfig(enabled=True, default=SubagentDefaultConfig(SONNET_55, "high"),
                              allowed={SONNET_55: ("high",), HAIKU: ("high",)})
PARITY_CASES = {
    "valid": claude_only(),
    "unadvertised-effort": claude_only(coder_intelligence="ultra"),
    "fast": claude_only(speed="fast"),
    "haiku-no-effort": claude_only(coder_mod=HAIKU, coder_intelligence=NO_EFFORT),
    "haiku-invented-effort": claude_only(coder_mod=HAIKU, coder_intelligence="high"),
    "subagent-invented-effort": claude_only(multi_agent=HAIKU_POOL),
    "old-opus-1m": claude_only(coder_mod="claude-code/claude-opus-5-5[1m]"),
    "inactive-role-ignored": claude_only(completion_review=False, completion_mod=HAIKU,
                                         completion_intelligence="max"),
}


@pytest.mark.parametrize("case", sorted(PARITY_CASES))
async def test_editor_ready_iff_the_real_preflight_validation_passes(case, discover, monkeypatch, tmp_path):
    config = PARITY_CASES[case]
    # The editor's discovery runs its own event loop, as `bello config` does.
    choices = await asyncio.to_thread(discover, AUTHENTICATED)
    report = validate_project_config(config, choices.catalog)

    engine, factory, _ = claude(tmp_path / "preflight-engine", AUTHENTICATED)
    client = RuntimeClient(cwd=tmp_path, backends={"claude-code": engine})
    controller = preflight_controller(config, client, monkeypatch, tmp_path)
    try:
        await client.start()
        try:
            await controller._runtime_preflight()
        except (AppServerError, RuntimeError):
            preflight_passed = False
        else:
            preflight_passed = True
    finally:
        await client.stop()
    assert (report.errors == ()) is preflight_passed, [issue.message for issue in report.errors]
    assert preflight_passed is (case in {"valid", "haiku-no-effort", "inactive-role-ignored"})
    assert queries(factory) == []
    status = rendered(config, choices)
    assert ("Offline checks passed" in status) is preflight_passed
    assert "Config valid" not in status


def test_ready_means_only_offline_checks(discover):
    choices = discover(AUTHENTICATED)
    output = rendered(claude_only(), choices)
    assert "Ready" in output and "Offline checks passed" in output
    assert "Codex: not signed in" in output  # other engines' reasons stay visible


@pytest.mark.parametrize("installed", [False, True])
def test_enabled_distiller_without_its_packages_is_not_ready(discover, monkeypatch, installed):
    from supervisor.project_config import LogDistillerConfig
    from supervisor.runtime import distiller

    monkeypatch.setattr(distiller.importlib.util, "find_spec",
                        lambda name: object() if installed else None)
    choices = discover(AUTHENTICATED)
    config = claude_only(log_distiller=LogDistillerConfig(enabled=True))
    errors = validate_project_config(config, choices.catalog).errors
    if installed:
        assert errors == ()
    else:
        assert [issue.title for issue in errors] == ["Distiller dependencies missing"]
        assert params(config, choices)["log_distiller_enabled"].issue_level == "error"
        assert "Bello[log-distiller]" in errors[0].message


def test_cheap_runtime_route_is_a_visible_warning_not_an_error(discover):
    choices = discover(AUTHENTICATED)
    config = claude_only(cheap_runtime=True)
    report = validate_project_config(config, choices.catalog)
    assert report.errors == ()
    assert [issue.title for issue in report.warnings] == ["Cheap triage route not connected"]
    assert params(config, choices)["cheap_runtime"].issue_level == "warning"
    assert "gpt-5.6-luna" in params(config, choices)["cheap_runtime"].issue


# --- Discovery failures are shown, sanitized -----------------------------------------------


def test_discovery_failures_are_visible_but_never_echo_external_text(discover):
    secret = "sk-ant-api03-SECRETVALUE"
    choices = discover(
        AUTHENTICATED,
        codex=f"login failed: upstream said {{'error': 'token {secret}'}}",
        pi=f"Pi runtime failed: HTTP 500 body=<html>{secret}</html>",
    )
    failures = choices.catalog.failures
    assert set(failures) == {"codex", "pi"}
    assert failures["codex"].kind == "login" and failures["codex"].action == "bello runtime login openai-codex"
    assert failures["pi"].kind == "dependency"
    output = rendered(claude_only(), choices)
    assert "Codex: not signed in" in output
    assert secret not in output and "upstream" not in output and "html" not in output
    assert SONNET_55 in choices  # one failed engine does not drop the others' models


async def test_claude_login_and_environment_failures_are_distinguished(tmp_path, monkeypatch):
    from supervisor.runtime.claude import ClaudeBackend

    async def emit(_event):
        pass

    signed_out = ClaudeBackend(tmp_path / "a", emit, tool_handler=lambda _r: None, client_factory=lambda _o: None,
                               auth_probe=lambda: {"loggedIn": False}, environment={},
                               cli_path=tmp_path / "cli-fixture")
    blocked = ClaudeBackend(tmp_path / "b", emit, tool_handler=lambda _r: None, client_factory=lambda _o: None,
                            auth_probe=lambda: {"loggedIn": True}, cli_path=tmp_path / "cli-fixture",
                            environment={"ANTHROPIC_API_KEY": "sk-ant-api03-SECRET", "CLAUDE_CODE_USE_VERTEX": "1"})
    for engine, kind, expected in ((signed_out, "login", "not signed in"),
                                   (blocked, "unsupported-setting", "ANTHROPIC_API_KEY, CLAUDE_CODE_USE_VERTEX")):
        client = RuntimeClient(cwd=tmp_path, backends={"claude-code": engine})
        try:
            await client.start()
            response = await client.request("model/list", {"engines": ["claude-code"], "optionalEngines": True})
        finally:
            await client.stop()
        reason = response["unavailableReasons"]["claude-code"]
        assert reason["kind"] == kind and expected in reason["summary"]
        assert "SECRET" not in json.dumps(response)
        catalog = catalog_from_model_list(response)
        assert catalog.failures["claude-code"].kind == kind


def _bello_failure_messages():
    from supervisor.runtime import claude_cli

    release = claude_cli.MANAGED_RELEASES[("Windows", "x86_64")]
    return [
        ("claude-code", str(claude_cli._not_prepared(release)), "dependency", "bello runtime install claude-code"),
        ("claude-code", str(claude_cli._invalid_cache(Path("cache"), ValueError("checksum"))), "dependency",
         "bello doctor"),
        ("claude-code", "installed claude-agent-sdk 0.2.162 (bundled-CLI version 2.1.285) is not the release Bello "
         "pairs with its verified Claude Code CLI 2.1.284", "dependency", "bello update"),
        ("claude-code", "Claude Code support requires the pinned claude-agent-sdk package", "dependency",
         "pipx install 'bello[claude]' --force"),
        ("claude-code", "Claude Code is signed in, but no paid Claude subscription was reported by the official CLI",
         "login", "sign in with a paid Claude account: bello runtime login claude-code"),
        ("pi", "Pi dependencies are not installed. Run `bello runtime install`.", "dependency",
         "bello runtime install pi"),
        ("pi", "Pi requires Node.js >= 22.19.0; found 20.0.0. Set BELLO_NODE to the supported executable.",
         "dependency", "install Node.js, then bello runtime install pi"),
    ]


@pytest.mark.parametrize("engine,message,kind,action", _bello_failure_messages())
def test_bello_readiness_messages_map_to_the_next_command(engine, message, kind, action):
    failure = classify_engine_failure(engine, message)
    assert (failure.kind, failure.action) == (kind, action)


def test_unknown_failures_hide_details():
    failure = classify_engine_failure("claude-code", "Traceback ... Authorization: Bearer abc.def")
    assert failure.kind == "unavailable" and failure.summary == "unavailable (details hidden)"
    assert "Bearer" not in failure.text()


def test_total_discovery_failure_is_reported_as_not_verified(monkeypatch, tmp_path):
    def broken(**_kwargs):
        raise RuntimeError("socket failure with private payload")

    monkeypatch.setattr(config_editor, "RuntimeClient", broken)
    choices = config_editor.available_model_choices(tmp_path)
    assert choices == ()
    assert choices.catalog.discovery_error == "RuntimeError"
    output = rendered(claude_only(), choices)
    flat = " ".join(output.replace("│", " ").split())
    assert "Not verified" in flat and "Model discovery failed" in flat and "(RuntimeError)" in flat
    assert "private payload" not in output and "Ready" not in output


# --- Claude model identity and aliases -----------------------------------------------------


def test_catalog_marks_aliases_and_names_exact_models_by_their_own_row():
    from supervisor.runtime.claude import ClaudeBackend

    entries = {entry["id"]: entry for entry in ClaudeBackend._catalog_entries({"models": SONNET_55_CLI_MODELS})}
    assert entries["default"]["alias"] is True and entries["default"]["resolvedModel"] == "claude-opus-5-5"
    assert entries["sonnet"]["alias"] is True and entries["sonnet"]["resolvedModel"] == "claude-sonnet-5-5"
    exact = entries["claude-opus-5-5"]
    assert exact["alias"] is False
    assert exact["displayName"] == "Opus"  # not the "Default (recommended)" row
    assert exact["description"].startswith("Opus 5.5 ·")
    assert entries["claude-sonnet-5-5"]["description"].startswith("Sonnet 5.5 ·")
    assert entries["claude-fable-5-1"]["alias"] is False
    assert [claude_model_name(name) for name in (
        "claude-sonnet-5-5", "claude-sonnet-5", "claude-opus-5-5[1m]", "claude-haiku-4-5-20251001", "sonnet",
    )] == ["Sonnet 5.5", "Sonnet 5", "Opus 5.5 [1m]", "Haiku 4.5", None]


def test_editor_shows_exact_identity_and_what_an_alias_resolves_to_now(discover):
    choices = discover(AUTHENTICATED)
    labels = {option.value: option.label for option in params(ProjectConfig(), choices)["coder_mod"].options}
    assert labels[SONNET_55] == f"Sonnet 5.5 - {SONNET_55}"
    assert labels["claude-code/claude-sonnet-5"] == "Sonnet 5 - claude-code/claude-sonnet-5"
    assert labels["claude-code/sonnet"] == "claude-code/sonnet - alias, now Sonnet 5.5 (claude-sonnet-5-5)"
    assert labels["claude-code/default"] == "claude-code/default - alias, now Opus 5.5 (claude-opus-5-5)"
    selected = params(claude_only(coder_mod="claude-code/sonnet"), choices)["coder_mod"]
    assert "floating alias" in selected.help_text and "claude-sonnet-5-5" in selected.help_text
    assert "Sonnet 5.5 · Efficient" in selected.help_text


def test_old_opus_1m_id_is_preserved_explained_and_never_replaced(discover, tmp_path):
    choices = discover(AUTHENTICATED)
    old = "claude-code/claude-opus-5-5[1m]"
    project = tmp_path / "saved"
    project.mkdir()
    save_project_config(project, claude_only(coder_mod=old))
    config = load_project_config(project, create=False)
    saved = config.to_json_data()
    coder = params(config, choices)["coder_mod"]
    assert coder.value == f"{old} (unavailable)"
    assert old not in {option.value for option in coder.options}
    assert "does not infer a smaller context window" in coder.issue
    assert "1M-context" in coder.issue and "nothing is replaced automatically" in coder.issue
    effort_index = list(params(config, choices)).index("coder_intelligence")
    option = [o.value for o in params(config, choices)["coder_intelligence"].options].index("max")
    changed, _, _ = config_editor.select_current(
        config, config_editor.EditorState(parameter_index=effort_index, expanded_index=effort_index,
                                          option_index=option), choices)
    assert changed.coder_mod == old  # editing another field never rewrites the model
    assert load_project_config(project, create=False).to_json_data() == saved


def test_preflight_lists_qualified_ids_with_alias_resolution_not_display_names():
    async def catalog(tmp):
        return await runtime_catalog(tmp, PINNED_CLI_MODELS)

    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        response = asyncio.run(catalog(Path(directory)))
    readable = _readable_available_models(response)
    assert "claude-code/sonnet (alias, now claude-sonnet-5)" in readable
    assert "claude-code/claude-sonnet-5" in readable
    assert not any(value in readable for value in ("Default (recommended)", "Sonnet", "sonnet", "default"))
    assert all(value.startswith("claude-code/") for value in readable)


def test_run_start_reports_alias_resolution_without_changing_the_saved_alias():
    rendered_lines = []
    controller = object.__new__(BelloController)
    controller.tui = SimpleNamespace(render=lambda *args: rendered_lines.append(args))
    controller.client = SimpleNamespace(required_models=("claude-code/sonnet", SONNET_55))
    entry = {"qualifiedId": "claude-code/sonnet", "alias": True, "resolvedModel": "claude-sonnet-5-5"}
    controller._report_alias_resolution({"data": [entry, {**entry, "id": "claude-code/sonnet"},
                                                  {"qualifiedId": SONNET_55, "alias": False}]})
    assert rendered_lines == [("SYSTEM", "claude-code/sonnet is a floating Claude Code alias; this run uses "
                                         "claude-sonnet-5-5, as the CLI resolves it now")]


# --- Effort after a model change -----------------------------------------------------------


@pytest.mark.parametrize("before,expected", [
    ("xhigh", "xhigh"),   # supported: preserved
    ("ultra", "max"),     # above the range: nearest lower level
    ("off", "low"),       # below the range: lowest level, never the maximum
    ("minimal", "low"),
    (NO_EFFORT, "high"),  # from a no-effort model
])
def test_switching_to_sonnet_55_keeps_a_justified_supported_effort(discover, before, expected):
    choices = discover(AUTHENTICATED)
    model = HAIKU if before == NO_EFFORT else "openrouter/qwen/qwen3-coder"
    config = ProjectConfig(coder_mod=model, coder_intelligence=before, runtime_enabled=False)
    parameters = config_editor.parameter_defs(config, choices)
    index = [parameter.key for parameter in parameters].index("coder_mod")
    option = [option.value for option in parameters[index].options].index(SONNET_55)
    updated, state, _ = config_editor.select_current(
        config, config_editor.EditorState(parameter_index=index, expanded_index=index, option_index=option), choices)
    assert updated.coder_mod == SONNET_55 and updated.coder_intelligence == expected
    if before == expected:
        assert state.notice is None
    else:
        assert state.notice == (f"Saved; coder effort {before} -> {expected} "
                                f"({before} is not offered by {SONNET_55})")


def test_choose_effort_policy_is_shared_and_never_raises_to_the_maximum():
    assert choose_effort("high", ("low", "high", "max")) == "high"
    assert choose_effort("xhigh", ("low", "high", "max")) == "high"
    assert choose_effort("off", ("low", "medium", "high", "xhigh", "max")) == "low"
    assert choose_effort("ultra", ("medium", "high")) == "high"
    assert choose_effort("high", ()) == NO_EFFORT
    assert choose_effort(NO_EFFORT, ("low", "medium", "xhigh"), advertised_default="medium") == "medium"
    assert choose_effort(NO_EFFORT, ("low", "xhigh")) == "xhigh"


def test_subagent_default_model_change_uses_the_same_rule(discover):
    choices = discover(AUTHENTICATED)
    policy = MultiAgentConfig(enabled=True, default=SubagentDefaultConfig(SONNET_55, "max"),
                              allowed={SONNET_55: ("max",), "claude-code/claude-sonnet-5": ("medium", "xhigh")})
    config = claude_only(multi_agent=policy)
    parameters = config_editor.parameter_defs(config, choices)
    index = [parameter.key for parameter in parameters].index("multi_agent_default_model")
    option = [option.value for option in parameters[index].options].index("claude-code/claude-sonnet-5")
    updated, state, _ = config_editor.select_current(
        config, config_editor.EditorState(parameter_index=index, expanded_index=index, option_index=option), choices)
    # Old rule: "high" if allowed else the first allowed effort; now the nearest lower level.
    assert updated.multi_agent.default == SubagentDefaultConfig("claude-code/claude-sonnet-5", "xhigh")
    assert "multi-agent default effort max -> xhigh" in state.notice


# --- Models with no advertised effort (Claude Haiku) --------------------------------------


def test_haiku_is_selectable_with_no_effort_and_round_trips(discover, tmp_path):
    choices = discover(AUTHENTICATED)
    assert config_editor.intelligence_choices_for_model(HAIKU) == (NO_EFFORT,)
    config = claude_only()
    parameters = config_editor.parameter_defs(config, choices)
    index = [parameter.key for parameter in parameters].index("coder_mod")
    option = [option.value for option in parameters[index].options].index(HAIKU)
    updated, state, _ = config_editor.select_current(
        config, config_editor.EditorState(parameter_index=index, expanded_index=index, option_index=option), choices)
    assert (updated.coder_mod, updated.coder_intelligence) == (HAIKU, NO_EFFORT)
    assert "high -> default" in state.notice
    effort = params(updated, choices)["coder_intelligence"]
    assert [option.label for option in effort.options] == [NO_EFFORT] and "sends none" in effort.help_text
    assert validate_project_config(updated, choices.catalog).errors == ()

    pool = MultiAgentConfig(enabled=True, default=SubagentDefaultConfig(HAIKU, NO_EFFORT),
                            allowed={HAIKU: (NO_EFFORT,), SONNET_55: ("high",)})
    updated = ProjectConfig(**{**updated.__dict__, "multi_agent": pool})
    project = tmp_path / "round-trip"
    project.mkdir()
    save_project_config(project, updated)
    reloaded = load_project_config(project, create=False)
    assert (reloaded.coder_mod, reloaded.coder_intelligence, reloaded.multi_agent) == (
        HAIKU, NO_EFFORT, pool)
    save_project_config(project, reloaded)
    assert load_project_config(project, create=False) == reloaded  # stable round trip
    settings = _resolve_run_settings(project_config=reloaded)
    assert settings.coder_intelligence == NO_EFFORT
    assert reloaded.multi_agent.is_allowed(HAIKU, NO_EFFORT)


def test_unknown_capabilities_are_not_confused_with_no_effort(discover, monkeypatch):
    # Engine unavailable: Haiku is unknown, not "advertises none".
    unknown = ModelCatalog(discovered=True)
    config = claude_only(coder_mod=HAIKU, coder_intelligence="high", runtime_enabled=False)
    issues = validate_project_config(config, unknown).errors
    assert [(issue.title, issue.settings) for issue in issues] == [("Saved model unavailable", ("coder_mod",))]
    assert config_editor.intelligence_choices_for_model(HAIKU) != (NO_EFFORT,)
    assert NO_EFFORT not in config_editor.intelligence_choices_for_model("openai/gpt-4.1")
    # A descriptor without a supportedEfforts field is unknown too.
    entry = catalog_entry({"qualifiedId": "pi/x", "provider": "pi"})
    assert entry.efforts is None
    assert catalog_entry({"qualifiedId": "pi/x", "provider": "pi", "supportedEfforts": []}).efforts == ()


def test_no_effort_is_never_sent_to_an_engine():
    assert "effort" not in apply_intelligence({}, NO_EFFORT)
    assert apply_intelligence({}, "high")["effort"] == "high"
    instructions = build_multi_agent_developer_instructions(MultiAgentConfig(
        enabled=True, default=SubagentDefaultConfig(HAIKU, NO_EFFORT), allowed={HAIKU: (NO_EFFORT,)}))
    assert f"{HAIKU}: {NO_EFFORT}" in instructions and "Bello sends none" in instructions


async def test_no_effort_child_and_turn_reach_claude_without_an_effort(tmp_path):
    engine, factory, events = claude(tmp_path / "engine", AUTHENTICATED, [result_message()])
    await engine.request("initialize", {})
    await engine.request("thread/start", {"threadId": "haiku", "provider": "claude-code", "model": "haiku",
                                          "cwd": str(tmp_path), "tools": [], "effort": None})
    await engine.request("turn/start", {"threadId": "haiku", "turnId": "t1",
                                        "input": [{"type": "text", "text": "work"}]})
    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "completed"
    assert factory.clients[-1].options.effort is None
    with pytest.raises(AppServerError, match="available: none"):
        await engine.request("model/validate", {"provider": "claude-code", "model": "haiku", "effort": "high"})
    assert (await engine.request("model/validate", {"provider": "claude-code", "model": "haiku"}))["valid"]
    await engine.stop()


async def test_unadvertised_model_fails_before_query_even_without_effort(tmp_path):
    engine, factory, events = claude(tmp_path / "engine", PINNED_CLI_MODELS, [result_message()])
    await engine.request("initialize", {})
    await engine.request("thread/start", {"threadId": "t", "provider": "claude-code", "model": "claude-sonnet-5-5",
                                          "cwd": str(tmp_path), "tools": [], "effort": None})
    await engine.request("turn/start", {"threadId": "t", "turnId": "t1", "input": [{"type": "text", "text": "x"}]})
    completed = await wait_completed(events)
    message = completed["params"]["turn"]["error"]["message"]
    assert completed["params"]["turn"]["status"] == "failed"
    assert "did not advertise model 'claude-sonnet-5-5'" in message and "no model request was sent" in message
    assert queries(factory) == []
    await engine.stop()


async def test_config_advisor_accepts_no_effort_only_where_none_is_advertised(tmp_path):
    from tests.test_claude_sonnet55_config import advisor_config, advisor_module

    models = advisor_module("inspect_models")
    validator = advisor_module("validate_config")
    catalog = await runtime_catalog(tmp_path / "catalog", AUTHENTICATED)
    entries = {entry["qualifiedId"]: entry for entry in models.summarize_catalog(catalog)}
    assert entries[HAIKU]["supportedEfforts"] == [] and entries[HAIKU]["alias"] is True
    assert entries[SONNET_55]["alias"] is False

    def validate(config):
        return validator.validate(config, catalog=catalog, allow_clean=False, allow_unlimited=False)

    haiku = advisor_config()
    haiku["coder_mod"], haiku["coder_intelligence"] = HAIKU, NO_EFFORT
    assert validate(haiku) == []
    haiku["coder_intelligence"] = "high"
    assert any("'high' is not advertised for claude-code/haiku" in error for error in validate(haiku))
    sonnet = advisor_config()
    sonnet["coder_intelligence"] = NO_EFFORT
    assert any("only for models that advertise no effort" in error for error in validate(sonnet))


def test_cli_accepts_no_effort_for_qualified_models(monkeypatch, tmp_path):
    captured = []
    monkeypatch.chdir(tmp_path)
    (tmp_path / "TASK.md").write_text("# Task\n", encoding="utf-8")
    monkeypatch.setattr("supervisor.main._startup_update_gate", lambda: None)

    async def fake_run(settings):
        captured.append(settings)
        return 0

    monkeypatch.setattr("supervisor.main._run_bello", fake_run)
    monkeypatch.setattr("supervisor.main._run_async_cleanly", asyncio.run)
    result = CliRunner().invoke(cli, ["--task", "TASK.md", "--coder-mod", HAIKU, "--coder-intelligence",
                                      NO_EFFORT, "--no-runtime"])
    assert result.exit_code == 0, result.output
    assert (captured[0].coder_model, captured[0].coder_intelligence) == (HAIKU, NO_EFFORT)
