"""Explicit cheap-runtime effort is forwarded unchanged and capability-checked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor.approval_triage import (
    CheapRuntimeReviewer,
    CheapRuntimeReviewerError,
    DEFAULT_TRIAGE_MODEL,
    runtime_triage_config_from_env,
)
from supervisor.controller import BelloController
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.codex import CodexBackend
from supervisor.runtime import client as runtime_client, codex as codex_backend
from tests.test_cheap_runtime_effort import TriageNative
from tests.test_pi_plain_json_decisions import PlainJsonClient


@pytest.fixture(autouse=True)
def clean_triage_environment(monkeypatch):
    for name in ("BELLO_RUNTIME_TRIAGE_MODEL", "BELLO_RUNTIME_TRIAGE_EFFORT",
                 "BELLO_RUNTIME_TRIAGE_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


def test_unset_override_keeps_builtin_medium_default():
    config = runtime_triage_config_from_env()
    assert config.model == DEFAULT_TRIAGE_MODEL
    assert config.effort == "medium"
    assert config.timeout_seconds == 25.0


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai-codex/gpt-6-luna"])
def test_env_override_selects_exact_model_and_effort(monkeypatch, model):
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_MODEL", model)
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_EFFORT", " low ")
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_TIMEOUT", "30")
    config = runtime_triage_config_from_env(enabled=False)
    assert (config.model, config.effort, config.timeout_seconds, config.enabled) == (model, "low", 30.0, False)


@pytest.mark.parametrize("effort", [None, "", "  "])
def test_other_model_without_effort_preserves_backend_default(monkeypatch, effort):
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_MODEL", "gpt-6-luna")
    if effort is not None:
        monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_EFFORT", effort)
    assert runtime_triage_config_from_env().effort is None


def test_explicit_effort_is_not_aliased_or_silently_replaced(monkeypatch, tmp_path):
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_EFFORT", "LOW")
    config = runtime_triage_config_from_env()
    assert config.effort == "LOW"
    reviewer = CheapRuntimeReviewer(object(), tmp_path, model=config.model, effort=config.effort)
    assert reviewer._thread_params()["effort"] == "LOW"


@pytest.mark.parametrize("failure", [False, True])
async def test_controller_forwards_effort_to_startup_self_test_and_retains_fallback(
    tmp_path, monkeypatch, failure,
):
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_MODEL", "gpt-6-luna")
    monkeypatch.setenv("BELLO_RUNTIME_TRIAGE_EFFORT", "low")
    client = PlainJsonClient([{"decision": "noop", "reason_code": "routine_progress"}])
    if failure:
        # A rejected self-test must retain the requested configuration, not
        # silently retry another effort or a different cheap model.
        client.replies = [{"decision": "not-a-decision"}]
    controller = object.__new__(BelloController)
    controller.client = client
    controller.tui = SimpleNamespace(render=Mock())
    controller.task_path = tmp_path / "TASK.md"
    controller._runtime_enabled = lambda: True
    controller._cheap_runtime_enabled = lambda: True
    controller._active_workspace_root = lambda: tmp_path

    await controller._configure_runtime_triage()

    assert len(client.threads) == len(client.turns) == 1
    assert client.threads[0]["model"] == client.turns[0]["model"] == "gpt-6-luna"
    assert client.threads[0]["effort"] == client.turns[0]["effort"] == "low"
    assert controller.runtime_triage_config.effort == "low"
    assert controller.runtime_triage_config.enabled is (not failure)
    assert (controller.runtime_triage_reviewer is None) is failure
    assert len(client.archived) == 1


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai-codex/gpt-6-luna"])
@pytest.mark.parametrize("effort", ["low", "ultra", "LOW"])
async def test_explicit_effort_uses_existing_native_advertised_capabilities(
    tmp_path, monkeypatch, model, effort,
):
    monkeypatch.setattr(runtime_client, "_IS_WINDOWS", False)
    monkeypatch.setattr(codex_backend, "_IS_WINDOWS", False)
    neutral_home = tmp_path / "missing-neutral-home"
    monkeypatch.setenv("CODEX_HOME", str(neutral_home))
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instances = []

    def factory(**kwargs):
        native = TriageNative(neutral_source_home=neutral_home, **kwargs)
        native.models = [{
            "id": "gpt-6-luna", "model": "gpt-6-luna",
            "supportedReasoningEfforts": [{"reasoningEffort": value} for value in ("low", "medium", "high")],
            "defaultReasoningEffort": "medium",
        }]
        instances.append(native)
        return native

    async def emit(raw):
        await client._emit(raw, engine="codex")

    backend = CodexBackend(state_dir=tmp_path / "native-state", emit=emit, client_factory=factory,
                           command=["neutral-native-test", "app-server"])
    client = RuntimeClient(cwd=tmp_path, state_dir=tmp_path / "host-state", backends={"codex": backend})
    client.configure_run(windows_native_root_read=True)
    reviewer = CheapRuntimeReviewer(client, workspace, model=model, effort=effort)
    try:
        if effort == "low":
            decision = await reviewer._decide("Neutral routine progress.")
            assert decision.decision == "noop"
            starts = [params for method, params in instances[0].calls if method == "thread/start"]
            turns = [params for method, params in instances[0].calls if method == "turn/start"]
            assert len(starts) == len(turns) == 1
            assert starts[0]["config"]["model_reasoning_effort"] == turns[0]["effort"] == "low"
        else:
            with pytest.raises(CheapRuntimeReviewerError):
                await reviewer._decide("Neutral routine progress.")
            # Invalid efforts are rejected by the existing catalog validation,
            # before any native model thread/turn is started.
            assert not any(method in {"thread/start", "turn/start"} for method, _ in instances[0].calls)
    finally:
        await client.stop()
