from __future__ import annotations

import json

import pytest

from supervisor.appserver import (
    AppServerError, _prepare_persistent_codex_home, _validate_persistent_codex_home,
)
from supervisor.approval_triage import CheapRuntimeReviewer, DEFAULT_TRIAGE_MODEL
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.codex import CodexBackend
from supervisor.runtime import client as runtime_client, codex as codex_backend
from tests.test_runtime_codex_backend import FakeNative


class TriageNative(FakeNative):
    """Neutral native protocol: unrelated user effort is ultra unless overridden."""

    def __init__(self, *, neutral_source_home, **kwargs):
        super().__init__(**kwargs)
        self.neutral_source_home = neutral_source_home
        self.early_events = False
        self.models = [{
            "id": DEFAULT_TRIAGE_MODEL,
            "model": DEFAULT_TRIAGE_MODEL,
            "supportedReasoningEfforts": [
                {"reasoningEffort": value}
                for value in ("low", "medium", "high", "xhigh", "max")
            ],
            "defaultReasoningEffort": "medium",
        }]

    async def start(self):
        # Match AppServerClient.start's owned persistent-home preparation, using
        # only the missing neutral test source, never the user's actual home.
        self._isolated_codex_home = _prepare_persistent_codex_home(
            self.neutral_source_home, self.options["persistent_isolated_home"])
        await super().start()

    async def request(self, method, params=None, timeout=30):
        if method == "thread/start":
            self.actual_effort = params["config"].get("model_reasoning_effort", "ultra")
        response = await super().request(method, params, timeout)
        if method == "turn/start":
            response["turn"].update(status="completed", items=[{
                "type": "agentMessage",
                "text": json.dumps({"decision": "noop", "reason_code": "routine_progress"}),
            }])
        return response


@pytest.mark.parametrize("model", [DEFAULT_TRIAGE_MODEL, f"openai-codex/{DEFAULT_TRIAGE_MODEL}"])
@pytest.mark.parametrize("windows", [False, True], ids=["posix-scope", "windows-scope"])
async def test_cheap_runtime_builtin_effort_does_not_inherit_unsupported_native_default(tmp_path, monkeypatch, model, windows):
    # Exercise the Windows scope branch on every host without changing os.name.
    monkeypatch.setattr(runtime_client, "_IS_WINDOWS", windows)
    monkeypatch.setattr(codex_backend, "_IS_WINDOWS", windows)
    source_home = tmp_path / "neutral-source-home"
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    instances = []

    def factory(**kwargs):
        native = TriageNative(neutral_source_home=source_home, **kwargs)
        instances.append(native)
        return native

    async def emit(raw):
        await client._emit(raw, engine="codex")

    backend = CodexBackend(state_dir=tmp_path / "native-state", emit=emit, client_factory=factory,
                           command=["neutral-native-test", "app-server"])
    client = RuntimeClient(cwd=tmp_path, state_dir=tmp_path / "host-state", backends={"codex": backend})
    client.configure_run(windows_native_root_read=True)
    requested_turns = []
    original_turn_start = client.turn_start

    async def record_turn_start(params, **kwargs):
        requested_turns.append(dict(params))
        return await original_turn_start(params, **kwargs)

    client.turn_start = record_turn_start
    try:
        decision = await CheapRuntimeReviewer(client, workspace, model=model)._decide("Neutral routine progress.")
        assert decision.model_dump() == {"decision": "noop", "reason_code": "routine_progress"}
        starts = [params for method, params in instances[0].calls if method == "thread/start"]
        turns = [params for method, params in instances[0].calls if method == "turn/start"]
        assert len(starts) == len(turns) == 1
        assert starts[0]["model"] == turns[0]["model"] == DEFAULT_TRIAGE_MODEL
        assert starts[0]["config"]["model_reasoning_effort"] == turns[0]["effort"] == "medium"
        assert requested_turns[0]["effort"] == "medium"
        home = backend.state_dir / "codex-home"
        _validate_persistent_codex_home(home)
        assert instances[0]._isolated_codex_home == home.resolve()
        assert (home / "rules").is_dir()
        assert not source_home.exists()
        if windows:
            profile = starts[0]["config"]["permissions"]["bello-native"]
            assert profile["filesystem"][str(home)] == "deny"
            assert profile["filesystem"][str(backend.state_dir)] == "deny"
        assert all(record["closed"] for record in client._threads.values())
    finally:
        await client.stop()


async def test_triage_native_start_rejects_unrecognized_persistent_home(tmp_path):
    state = tmp_path / "native-state"
    state.mkdir(mode=0o700)
    home = state / "codex-home"
    home.mkdir(mode=0o700)
    native = TriageNative(neutral_source_home=tmp_path / "neutral-source-home",
                          persistent_isolated_home=home)
    with pytest.raises(AppServerError, match="unrecognized persistent Codex home"):
        await native.start()
    assert not native.started


@pytest.mark.parametrize("model", ["gpt-6-sol", "claude-code/claude-sonnet-5-5", "openai/gpt-5.6-luna"])
async def test_cheap_runtime_model_override_keeps_existing_effort_behavior(tmp_path, model):
    class OverrideClient:
        def __init__(self):
            self.starts = []
            self.turns = []
            self.archived = []

        async def thread_start(self, params, **kwargs):
            self.starts.append(params)
            return {"thread": {"id": "override-thread"}}

        async def turn_start(self, params, **kwargs):
            self.turns.append(params)
            return {"turn": {"id": "override-turn", "status": "completed", "items": [{
                "type": "agentMessage", "text": '{"decision":"noop","reason_code":"routine_progress"}',
            }]}}

        async def thread_archive(self, thread_id, **kwargs):
            self.archived.append(thread_id)

    client = OverrideClient()
    await CheapRuntimeReviewer(client, tmp_path, model=model)._decide("Neutral routine progress.")
    assert client.starts[0]["model"] == client.turns[0]["model"] == model
    assert "effort" not in client.starts[0] and "effort" not in client.turns[0]
    assert "model_reasoning_effort" not in client.starts[0]["config"]
    assert client.archived == ["override-thread"]
