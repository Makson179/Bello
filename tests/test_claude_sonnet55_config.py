"""Claude Sonnet 5.5 on the Claude Code subscription route, from discovery to preflight.

Every test is offline and sends no model request. The two initialize payloads
below are real metadata emitted by the official CLI bundled in the named
claude-agent-sdk wheel. They were captured on 2026-09-29 through the SDK
initialize/get_server_info exchange, run in an isolated, signed-out
CLAUDE_CONFIG_DIR with no prompt written. They are therefore neither invented
fixtures nor the user's live authenticated catalog: a subscription can add,
remove or disable rows (entitlement, organization policy, served catalog), so
these tests do not claim live provider compatibility.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from supervisor import config_editor
from supervisor.appserver import AppServerError
from supervisor.controller import _selected_model_availability
from supervisor.project_config import (
    MultiAgentConfig,
    ProjectConfig,
    SubagentDefaultConfig,
    load_project_config,
    project_config_path,
    save_project_config,
)
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.models import parse_model_selection
from tests.test_runtime_claude import FakeFactory, backend, result_message, wait_completed


# claude-agent-sdk==0.2.159 (Bello's Windows pin) bundles Claude Code 2.1.281.
PINNED_CLI_MODELS = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5-5[1m]",
        "displayName": "Default (recommended)",
        "description": "Use the default model (currently Opus 5.5 (1M context)) · $4/$20 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsFastMode": True,
        "supportsAutoMode": True,
    },
    {
        "value": "opus[1m]",
        "resolvedModel": "claude-opus-5-5[1m]",
        "displayName": "Opus (1M context)",
        "description": "Opus 5.5 with 1M context · Best for everyday, complex tasks · $4/$20 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsFastMode": True,
        "supportsAutoMode": True,
    },
    {
        "value": "claude-fable-5-1",
        "resolvedModel": "claude-fable-5-1",
        "displayName": "Fable",
        "description": "Fable 5.1 · Most capable for your hardest and longest-running tasks · $10/$50 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsAutoMode": True,
    },
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5",
        "displayName": "Sonnet",
        "description": "Sonnet 5 · Efficient for routine tasks · $2/$10 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsAutoMode": True,
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku",
        "description": "Haiku 4.5 · Fastest for quick answers · $1/$5 per Mtok",
    },
]

# claude-agent-sdk==0.2.161 bundles Claude Code 2.1.284, the first bundled CLI
# whose catalog contains claude-sonnet-5-5. Bello pins it on macOS and Linux.
SONNET_55_CLI_MODELS = [
    {
        "value": "default",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Default (recommended)",
        "description": "Use the default model (currently Opus 5.5) · $4/$20 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsFastMode": True,
        "supportsAutoMode": True,
    },
    {
        "value": "opus",
        "resolvedModel": "claude-opus-5-5",
        "displayName": "Opus",
        "description": "Opus 5.5 · Best for everyday, complex tasks · $4/$20 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsFastMode": True,
        "supportsAutoMode": True,
    },
    {
        "value": "claude-fable-5-1",
        "resolvedModel": "claude-fable-5-1",
        "displayName": "Fable",
        "description": "Fable 5.1 · Most capable for your hardest and longest-running tasks · $10/$50 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsAutoMode": True,
    },
    {
        "value": "sonnet",
        "resolvedModel": "claude-sonnet-5-5",
        "displayName": "Sonnet",
        "description": "Sonnet 5.5 · Efficient for routine tasks · $2/$10 per Mtok",
        "supportsEffort": True,
        "supportedEffortLevels": ["low", "medium", "high", "xhigh", "max"],
        "supportsAdaptiveThinking": True,
        "supportsAutoMode": True,
    },
    {
        "value": "haiku",
        "resolvedModel": "claude-haiku-4-5-20251001",
        "displayName": "Haiku",
        "description": "Haiku 4.5 · Fastest for quick answers · $1/$5 per Mtok",
    },
]

SONNET_55 = "claude-code/claude-sonnet-5-5"
# The exact efforts the CLI advertised for the row that resolves to Sonnet 5.5.
ADVERTISED = tuple(
    next(row for row in SONNET_55_CLI_MODELS if row["resolvedModel"] == "claude-sonnet-5-5")[
        "supportedEffortLevels"
    ]
)


def sonnet_55_with_efforts(efforts: list[str]) -> list[dict]:
    models = copy.deepcopy(SONNET_55_CLI_MODELS)
    for row in models:
        if row["resolvedModel"] == "claude-sonnet-5-5":
            row["supportedEffortLevels"] = list(efforts)
    return models


def claude(root: Path, models: list[dict], messages=()) -> tuple:
    factory = FakeFactory(list(messages), models=copy.deepcopy(models))
    events: list[dict] = []
    return backend(root, factory, events), factory, events


def queries(factory: FakeFactory) -> list:
    return [query for client in factory.clients for query in client.queries]


class Unavailable:
    """An optional engine that is not connected for this account."""

    def __init__(self, reason: str):
        self.reason = reason

    async def request(self, method, params, timeout=30):
        raise AppServerError(self.reason)

    async def stop(self):
        pass


class Recorder:
    def __init__(self):
        self.calls = []

    async def request(self, method, params, timeout=30):
        self.calls.append((method, params))
        return {"valid": True}

    async def stop(self):
        pass


async def runtime_catalog(tmp_path: Path, models: list[dict]) -> dict:
    """The model/list response RuntimeClient gives the editor and controller."""
    engine, factory, _ = claude(tmp_path / "engine", models)
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    client = RuntimeClient(cwd=project, backends={"claude-code": engine})
    try:
        await client.start()
        response = await client.request("model/list", {"engines": ["claude-code"]})
    finally:
        await client.stop()
    assert queries(factory) == []
    return response


@pytest.fixture(autouse=True)
def isolated_effort_catalog(monkeypatch):
    monkeypatch.setattr(config_editor, "_model_effort_catalog", {})


@pytest.fixture
def editor_runtime(monkeypatch, tmp_path):
    """Route the real editor discovery through RuntimeClient and ClaudeBackend."""

    factories: list[FakeFactory] = []

    def install(models: list[dict]) -> list[FakeFactory]:
        def make_client(*, cwd, **kwargs):
            engine, factory, _ = claude(tmp_path / "engine", models)
            factories.append(factory)
            return RuntimeClient(cwd=cwd, backends={
                "codex": Unavailable("native Codex is not signed in"),
                "pi": Unavailable("no Pi provider is connected"),
                "claude-code": engine,
            }, **kwargs)

        monkeypatch.setattr(config_editor, "RuntimeClient", make_client)
        return factories

    return install


def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return root


def choose(config: ProjectConfig, choices: tuple[str, ...], key: str, value) -> ProjectConfig:
    parameters = config_editor.parameter_defs(config, choices)
    index = next(index for index, parameter in enumerate(parameters) if parameter.key == key)
    option = next(
        position for position, option in enumerate(parameters[index].options)
        if option.value == value or option.label == value
    )
    state = config_editor.EditorState(parameter_index=index, expanded_index=index, option_index=option)
    updated, _, action = config_editor.select_current(config, state, choices)
    assert action is None
    return updated


def parameter(config: ProjectConfig, choices: tuple[str, ...], key: str):
    return next(item for item in config_editor.parameter_defs(config, choices) if item.key == key)


# --- Claude Code backend: discovery and validation --------------------------------


async def test_pinned_cli_metadata_does_not_advertise_sonnet_55(tmp_path):
    engine, factory, _ = claude(tmp_path, PINNED_CLI_MODELS)
    response = await engine.request("model/list", {})
    by_id = {entry["id"]: entry for entry in response["data"]}

    assert by_id["sonnet"]["resolvedModel"] == "claude-sonnet-5"
    assert "claude-sonnet-5-5" not in by_id
    assert not any("sonnet-5-5" in json.dumps(entry) for entry in response["data"])
    with pytest.raises(AppServerError, match="did not advertise model 'claude-sonnet-5-5'"):
        await engine.request(
            "model/validate", {"provider": "claude-code", "model": "claude-sonnet-5-5", "effort": "high"}
        )
    assert queries(factory) == []
    await engine.stop()


async def test_sonnet_55_metadata_is_discovered_with_the_cli_advertised_efforts(tmp_path):
    engine, factory, _ = claude(tmp_path, SONNET_55_CLI_MODELS)
    response = await engine.request("model/list", {})
    by_id = {entry["id"]: entry for entry in response["data"]}

    assert ADVERTISED == ("low", "medium", "high", "xhigh", "max")
    explicit, alias = by_id["claude-sonnet-5-5"], by_id["sonnet"]
    assert explicit["qualifiedId"] == SONNET_55
    assert alias["qualifiedId"] == "claude-code/sonnet"
    for entry in (explicit, alias):
        assert entry["provider"] == "claude-code"
        assert entry["billingRoute"] == "subscription"
        assert entry["resolvedModel"] == "claude-sonnet-5-5"
        assert entry["available"] is True and entry["configured"] is True
        assert entry["supportedReasoningEfforts"] == list(ADVERTISED)
        assert entry["supportedEfforts"] == list(ADVERTISED)
        assert entry["supportsServiceTier"] is False
    # One metadata connection, closed again, and no prompt.
    assert len(factory.clients) == 1
    assert factory.clients[0].disconnect_task is not None
    assert queries(factory) == []
    await engine.stop()


async def test_sonnet_55_efforts_come_from_cli_metadata_not_a_bello_table(tmp_path):
    engine, factory, _ = claude(tmp_path, sonnet_55_with_efforts(["low", "medium", "high"]))
    response = await engine.request("model/list", {})
    entry = next(item for item in response["data"] if item["id"] == "claude-sonnet-5-5")

    assert entry["supportedReasoningEfforts"] == ["low", "medium", "high"]
    accepted = await engine.request(
        "model/validate", {"provider": "claude-code", "model": "claude-sonnet-5-5", "effort": "high"}
    )
    assert accepted["valid"] is True
    for effort in ("xhigh", "max"):
        with pytest.raises(AppServerError, match="available: low, medium, high"):
            await engine.request(
                "model/validate", {"provider": "claude-code", "model": "claude-sonnet-5-5", "effort": effort}
            )
    assert queries(factory) == []
    await engine.stop()


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "sonnet"])
@pytest.mark.parametrize("effort", ADVERTISED)
async def test_each_advertised_sonnet_55_effort_validates_without_a_model_request(tmp_path, model, effort):
    engine, factory, _ = claude(tmp_path, SONNET_55_CLI_MODELS)
    result = await engine.request("model/validate", {"provider": "claude-code", "model": model, "effort": effort})

    assert result["valid"] is True
    assert result["model"]["resolvedModel"] == "claude-sonnet-5-5"
    assert result["requested"] == {"effort": effort, "serviceTier": None}
    assert result["execution"] == {"engine": "claude-code", "effort": effort}
    assert queries(factory) == []
    await engine.stop()


@pytest.mark.parametrize(
    "params,match",
    [
        ({"effort": "ultra"}, "not supported"),
        ({"effort": "minimal"}, "not supported"),
        ({"effort": "off"}, "not supported"),
        ({"effort": "high", "serviceTier": "priority"}, "service tier"),
        ({"effort": "high", "provider": "anthropic"}, "provider 'claude-code'"),
    ],
)
async def test_sonnet_55_rejects_unadvertised_effort_tier_or_route(tmp_path, params, match):
    engine, factory, _ = claude(tmp_path, SONNET_55_CLI_MODELS)
    request = {"provider": "claude-code", "model": "claude-sonnet-5-5", **params}
    with pytest.raises(AppServerError, match=match):
        await engine.request("model/validate", request)
    assert queries(factory) == []
    await engine.stop()


async def test_explicit_sonnet_55_turn_uses_that_exact_model_and_effort_without_fallback(tmp_path):
    engine, factory, events = claude(tmp_path, SONNET_55_CLI_MODELS, [result_message()])
    await engine.request("initialize", {})
    started = await engine.request("thread/start", {
        "threadId": "thread-55", "provider": "claude-code", "model": "claude-sonnet-5-5",
        "cwd": str(tmp_path), "tools": [], "effort": "xhigh",
    })
    assert started["thread"]["model"] == SONNET_55
    await engine.request("turn/start", {
        "threadId": "thread-55", "turnId": "turn-55", "input": [{"type": "text", "text": "work"}],
    })

    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "completed"
    options = factory.clients[-1].options
    assert options.model == "claude-sonnet-5-5"
    assert options.effort == "xhigh"
    assert options.fallback_model is None
    await engine.stop()


async def test_pinned_cli_fails_a_sonnet_55_turn_before_any_model_request(tmp_path):
    engine, factory, events = claude(tmp_path, PINNED_CLI_MODELS, [result_message()])
    await engine.request("initialize", {})
    await engine.request("thread/start", {
        "threadId": "thread-55", "provider": "claude-code", "model": "claude-sonnet-5-5",
        "cwd": str(tmp_path), "tools": [], "effort": "high",
    })
    await engine.request("turn/start", {
        "threadId": "thread-55", "turnId": "turn-55", "input": [{"type": "text", "text": "work"}],
    })

    completed = await wait_completed(events)
    assert completed["params"]["turn"]["status"] == "failed"
    assert "claude-sonnet-5-5" in completed["params"]["turn"]["error"]["message"]
    assert factory.clients[-1].options.model == "claude-sonnet-5-5"
    assert queries(factory) == []
    await engine.stop()


# --- Runtime routing and controller preflight --------------------------------------


async def test_runtime_routes_sonnet_55_only_through_the_subscription_engine(tmp_path):
    engine, factory, _ = claude(tmp_path / "engine", SONNET_55_CLI_MODELS)
    api_route = Recorder()
    project = project_root(tmp_path)
    client = RuntimeClient(cwd=project, backends={"claude-code": engine, "pi": api_route})
    try:
        await client.start()
        listed = await client.request("model/list", {"engines": ["claude-code"]})
        assert SONNET_55 in {item["id"] for item in listed["data"]}

        validation = await client.request(
            "model/validate", {"model": SONNET_55, "effort": "max", "serviceTier": None}
        )
        assert validation["valid"] is True
        assert validation["model"]["qualifiedId"] == SONNET_55
        assert validation["model"]["billingRoute"] == "subscription"

        # A same-named API model is a different, explicitly selected route.
        await client.request("model/validate", {"model": "anthropic/claude-sonnet-5-5", "effort": "high"})
        assert api_route.calls[-1][1]["provider"] == "anthropic"
        assert api_route.calls[-1][1]["model"] == "claude-sonnet-5-5"
        with pytest.raises(AppServerError, match="service tier"):
            await client.request("model/validate", {"model": SONNET_55, "effort": "high", "serviceTier": "priority"})
    finally:
        await client.stop()
    assert parse_model_selection(SONNET_55).billing_route == "subscription"
    assert parse_model_selection("anthropic/claude-sonnet-5-5").billing_route == "provider-api"
    assert queries(factory) == []


@pytest.mark.parametrize("models,available", [(SONNET_55_CLI_MODELS, True), (PINNED_CLI_MODELS, False)])
async def test_preflight_availability_follows_the_cli_catalog(tmp_path, models, available):
    response = await runtime_catalog(tmp_path, models)
    result = _selected_model_availability(
        response, coder_model=SONNET_55, runtime_model=None, completion_model=None,
        subagent_models=(SONNET_55,),
    )
    assert result.ok is available
    if not available:
        assert result.missing_roles == (f"coder={SONNET_55}", f"subagent={SONNET_55}")
        assert "claude-code/sonnet" in result.available_models


# --- Configuration editor: discovery, selection and persistence ----------------------


def test_editor_offers_sonnet_55_with_exact_efforts_and_persists_the_choice(tmp_path, editor_runtime):
    factories = editor_runtime(SONNET_55_CLI_MODELS)
    project = project_root(tmp_path)
    config = load_project_config(project, create=True)

    choices = config_editor.available_model_choices(project)

    assert SONNET_55 in choices and "claude-code/sonnet" in choices
    assert not any(choice.startswith(("anthropic/", "openrouter/")) for choice in choices)
    assert config_editor.intelligence_choices_for_model(SONNET_55) == ADVERTISED
    assert config_editor.intelligence_choices_for_model("claude-code/sonnet") == ADVERTISED
    assert SONNET_55 in {option.value for option in parameter(config, choices, "coder_mod").options}

    selected = choose(config, choices, "coder_mod", SONNET_55)
    assert selected.coder_mod == SONNET_55
    assert selected.coder_intelligence == config.coder_intelligence == "xhigh"
    assert tuple(option.label for option in parameter(selected, choices, "coder_intelligence").options) == ADVERTISED
    config_editor._save_config_change(project, config, selected)

    maxed = choose(selected, choices, "coder_intelligence", "max")
    config_editor._save_config_change(project, selected, maxed)

    reloaded = load_project_config(project, create=False)
    assert (reloaded.coder_mod, reloaded.coder_intelligence) == (SONNET_55, "max")
    assert reloaded.runtime_mod == config.runtime_mod  # other roles are not rewritten
    raw = json.loads(project_config_path(project).read_text(encoding="utf-8"))
    assert raw["coder_mod"] == raw["coder_model"] == SONNET_55
    assert raw["coder_intelligence"] == "max"
    assert all(queries(factory) == [] for factory in factories)


def test_switching_a_role_to_sonnet_55_never_keeps_an_unadvertised_effort(tmp_path, editor_runtime):
    editor_runtime(SONNET_55_CLI_MODELS)
    choices = config_editor.available_model_choices(project_root(tmp_path))
    config = ProjectConfig(coder_mod="gpt-6-astra", coder_intelligence="ultra")

    selected = choose(config, choices, "coder_mod", SONNET_55)

    assert selected.coder_mod == SONNET_55
    assert selected.coder_intelligence in ADVERTISED


def test_editor_does_not_invent_sonnet_55_from_the_pinned_cli(tmp_path, editor_runtime):
    editor_runtime(PINNED_CLI_MODELS)
    project = project_root(tmp_path)
    choices = config_editor.available_model_choices(project)

    assert SONNET_55 not in choices
    assert {"claude-code/sonnet", "claude-code/claude-sonnet-5"} <= set(choices)
    config = ProjectConfig(coder_mod=SONNET_55, coder_intelligence="high", runtime_enabled=False)
    saved = config.to_json_data()
    coder = parameter(config, choices, "coder_mod")
    assert coder.value == f"{SONNET_55} (unavailable)"
    assert SONNET_55 not in {option.value for option in coder.options}
    rendered = config_editor.render_editor(
        config, config_editor.EditorState(), project / "config.json", choices, width=120, height=40,
    )
    assert "Saved model unavailable" in rendered
    assert config.to_json_data() == saved  # never silently replaced by Sonnet 5 or the alias


def test_subagent_pool_uses_sonnet_55_advertised_efforts_and_persists(tmp_path, editor_runtime):
    editor_runtime(SONNET_55_CLI_MODELS)
    project = project_root(tmp_path)
    choices = config_editor.available_model_choices(project)
    config = ProjectConfig(
        coder_mod=SONNET_55,
        coder_intelligence="high",
        runtime_enabled=False,
        multi_agent=MultiAgentConfig(
            enabled=True,
            default=SubagentDefaultConfig(model=SONNET_55, intelligence="high"),
            allowed={SONNET_55: ("high",)},
        ),
    )
    save_project_config(project, config)
    key = f"multi_agent_allowed:{SONNET_55}"
    assert tuple(option.label for option in parameter(config, choices, key).options) == ADVERTISED

    updated = choose(config, choices, key, "max")
    config_editor._save_config_change(project, config, updated)

    reloaded = load_project_config(project, create=False)
    assert reloaded.multi_agent.allowed == {SONNET_55: ("high", "max")}
    assert reloaded.multi_agent.default == SubagentDefaultConfig(model=SONNET_55, intelligence="high")
    assert reloaded.multi_agent.is_allowed(SONNET_55, "max")
    assert not reloaded.multi_agent.is_allowed("anthropic/claude-sonnet-5-5", "max")


# --- Config advisor helper: the same catalog, no fixed model list ---------------------


SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/bello/skills/bello-config-advisor/scripts"


def advisor_module(name: str):
    spec = importlib.util.spec_from_file_location(f"sonnet55_{name}", SKILL_SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def advisor_config(effort: str = "high", speed: str = "usual") -> dict:
    result = dict(
        review_limit_format="explicit", task="TASK.md", speed=speed, revision_coder_enabled=False,
        runtime_enabled=True, cheap_runtime=False, async_tools=False, windows_native_root_read=False,
        log_distiller={"enabled": False, "model_path": None}, start_over=False, completion_review=True,
        adversary=False, max_adversary_runs=0, max_completion_returns_before_adversary=1,
        max_completion_returns_after_adversary=0, clean=False, protected_path=[],
    )
    for role in ("coder", "revision_coder", "runtime", "completion", "adversary"):
        result[f"{role}_mod"] = SONNET_55
        result[f"{role}_intelligence"] = effort
    for field in ("multi_agent", "completion_multi_agent", "adversary_multi_agent"):
        result[field] = {
            "enabled": False, "max_concurrent": 1,
            "default": {"model": SONNET_55, "intelligence": effort}, "allowed": {SONNET_55: [effort]},
        }
    return result


async def test_config_advisor_validates_sonnet_55_against_the_runtime_catalog(tmp_path):
    models = advisor_module("inspect_models")
    validator = advisor_module("validate_config")
    catalog = await runtime_catalog(tmp_path / "current", SONNET_55_CLI_MODELS)
    pinned = await runtime_catalog(tmp_path / "pinned", PINNED_CLI_MODELS)

    entry = next(item for item in models.summarize_catalog(catalog) if item["qualifiedId"] == SONNET_55)
    assert entry["supportedEfforts"] == list(ADVERTISED)
    assert entry["billingRoute"] == "subscription"
    assert entry["resolvedModel"] == "claude-sonnet-5-5"

    def validate(config, source=catalog):
        return validator.validate(config, catalog=source, allow_clean=False, allow_unlimited=False)

    assert validate(advisor_config("max")) == []
    assert any("'ultra' is not advertised for claude-code/claude-sonnet-5-5" in error
               for error in validate(advisor_config("ultra")))
    assert any("priority service tier" in error for error in validate(advisor_config(speed="fast")))
    assert any("claude-code/claude-sonnet-5-5 is not available in the supplied catalog" in error
               for error in validate(advisor_config(), pinned))
