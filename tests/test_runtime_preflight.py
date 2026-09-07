from __future__ import annotations

from types import SimpleNamespace

import pytest

from supervisor.controller import BelloController
from supervisor.runtime import sandbox


def controller_for(tmp_path, monkeypatch, *, invalid=False, completion=False, adversary=False, fast=False):
    events = []

    async def model_list():
        events.append("catalog")
        return {"data": []}

    async def request(method, params):
        events.append((method, params))
        return {"valid": not invalid}

    controller = object.__new__(BelloController)
    controller.client = SimpleNamespace(model_list=model_list, request=request)
    controller.tui = SimpleNamespace(status=lambda text: None)
    controller.store = SimpleNamespace(update_bello_config=lambda transform: None,
                                      get_bello_config=lambda: SimpleNamespace(status=None))
    for role in ("coder", "runtime", "completion", "adversary", "revision_coder"):
        setattr(controller, f"_{role}_model", lambda role=role: f"test/{role}")
        setattr(controller, f"_{role}_intelligence", lambda: "high")
    controller._effective_completion_review = lambda: completion
    controller._adversary_model_required_for_preflight = lambda: adversary
    controller._revision_coder_enabled = lambda: completion
    controller._enabled_subagent_models_for_preflight = lambda: ()
    controller._multi_agent_config = lambda: SimpleNamespace(enabled=False)
    controller._completion_multi_agent_config = lambda: SimpleNamespace(enabled=False)
    controller._adversary_multi_agent_config = lambda: SimpleNamespace(enabled=False)
    controller._persist_model_config = lambda: None
    controller._fast_mode = lambda: fast
    controller._active_workspace_root = lambda: tmp_path

    async def ensure_available(models):
        events.append("availability")

    async def self_test():
        events.append("paid-self-test")

    async def triage():
        events.append("triage")

    class Runner:
        def __init__(self, policy):
            assert policy.root == tmp_path

        async def run(self, command, root, timeout):
            events.append("sandbox")
            return SimpleNamespace(exit_code=0, output="bello-sandbox-probe")

    controller._ensure_selected_models_available = ensure_available
    controller._structured_output_self_test = self_test
    controller._configure_runtime_triage = triage
    monkeypatch.setattr(sandbox, "SandboxRunner", Runner)
    return controller, events


@pytest.mark.asyncio
async def test_exact_profiles_checked_before_any_paid_self_test(tmp_path, monkeypatch):
    controller, events = controller_for(tmp_path, monkeypatch, invalid=True)
    with pytest.raises(RuntimeError, match="exact model profile"):
        await controller._runtime_preflight()
    assert "paid-self-test" not in events
    assert "sandbox" not in events
    assert controller.client.required_models == ("test/coder", "test/runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("completion,adversary", [(False, False), (True, False), (False, True), (True, True)])
async def test_only_active_roles_are_required_and_validated(tmp_path, monkeypatch, completion, adversary):
    controller, events = controller_for(tmp_path, monkeypatch, completion=completion, adversary=adversary)
    await controller._runtime_preflight()
    profiles = [params for method, params in (event for event in events if isinstance(event, tuple))
                if method == "model/validate"]
    expected = {"test/coder", "test/runtime"}
    if completion:
        expected.update({"test/completion", "test/revision_coder"})
    if adversary:
        expected.add("test/adversary")
    assert {params["model"] for params in profiles} == expected == set(controller.client.required_models)
    assert all(params["effort"] == "high" and params["serviceTier"] is None for params in profiles)
    assert events[-3:] == ["sandbox", "paid-self-test", "triage"]


@pytest.mark.asyncio
async def test_fast_tier_must_be_explicitly_validated_not_silently_ignored(tmp_path, monkeypatch):
    controller, events = controller_for(tmp_path, monkeypatch, invalid=True, fast=True)
    with pytest.raises(RuntimeError):
        await controller._runtime_preflight()
    request = next(event for event in events if isinstance(event, tuple))
    assert request[1]["serviceTier"] == "priority"
    assert "paid-self-test" not in events
