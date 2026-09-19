from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from supervisor.appserver import AppServerError, AppServerMessage, AppServerTimeoutError
from supervisor.runtime.codex import CodexBackend


MODELS = [{"id": "astra-picker", "model": "gpt-6-astra", "displayName": "Astra",
    "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "xhigh"}],
    "defaultReasoningEffort": "high"}]
TOOLS = [{"name": "exec_command", "description": "custom command", "parameters": {"type": "object"}},
    {"name": "spawn_agent", "description": "Delegate", "parameters": {"type": "object"}},
    {"name": "wait_agent", "description": "Wait", "parameters": {"type": "object"}}]


class FakeNative:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.calls = []
        self.responses = []
        self.started = False
        self.stopped = 0
        self.account = {"type": "chatgpt", "email": "private@example.test", "planType": "pro"}
        self.models = deepcopy(MODELS)
        self.next_thread = 0
        self.next_turn = 0
        self.early_events = True
        self.complete_early = False
        self.lose_turn_ack = False
        self.lose_thread_ack = False
        self.host = None
        self.gate = None
        self.actual_effort = None
        self.active_profile = {"id": "bello-native", "extends": None}

    async def start(self):
        self.started = True

    async def initialize(self, timeout=30):
        return {"userAgent": "codex/test"}

    async def stop(self):
        self.stopped += 1

    async def notify_event(self, method, params, request_id=None):
        raw = {"method": method, "params": params}
        if request_id is not None:
            raw["id"] = request_id
        callback = self.options["server_request_handler" if request_id is not None else "notification_handler"]
        await callback(AppServerMessage(raw))

    async def request(self, method, params=None, timeout=30):
        params = deepcopy(params or {})
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": deepcopy(self.account)}
        if method == "model/list":
            return {"data": deepcopy(self.models), "nextCursor": None}
        if method == "thread/start":
            self.next_thread += 1
            native = "native-thread-" + str(self.next_thread)
            if self.early_events:
                await self.notify_event("thread/started", {"thread": {"id": native}})
                await asyncio.sleep(0)
            if self.lose_thread_ack:
                raise AppServerTimeoutError("lost thread acknowledgement")
            return {"thread": {"id": native, "modelProvider": "openai", "turns": []},
                    "reasoningEffort": self.actual_effort or params["config"].get("model_reasoning_effort"),
                    **({"activePermissionProfile": self.active_profile} if "permissions" in params else {})}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "turns": []}}
        if method == "turn/start":
            self.next_turn += 1
            native_turn = "native-turn-" + str(self.next_turn)
            if self.early_events:
                await self.notify_event("turn/started", {"threadId": params["threadId"], "turn": {"id": native_turn, "status": "inProgress"}})
                await self.notify_event("item/completed", {"threadId": params["threadId"], "turnId": native_turn,
                    "item": {"id": "native-item-1", "type": "agentMessage", "text": "native-thread-1"}})
                await asyncio.sleep(0)
            if self.complete_early:
                await self.notify_event("turn/completed", {"threadId": params["threadId"], "turn": {"id": native_turn, "status": "completed"}})
                await asyncio.sleep(0)
            if self.gate is not None:
                await self.gate.wait()
            if self.lose_turn_ack:
                raise AppServerTimeoutError("lost turn acknowledgement")
            return {"turn": {"id": native_turn, "status": "inProgress", "items": []}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": [{"id": "native-turn-1", "status": "completed"}]}}
        return {}

    async def respond(self, request_id, result=None, error=None, timeout=15):
        self.responses.append((request_id, result, error))


def make_backend(tmp_path, **kwargs):
    events, instances = [], []
    async def emit(raw):
        events.append(raw)
    def factory(**options):
        client = FakeNative(**options)
        instances.append(client)
        return client
    backend = CodexBackend(state_dir=tmp_path / "state", emit=emit, client_factory=factory, **kwargs)
    return backend, events, instances


def thread_params(tmp_path, host="host-thread", **overrides):
    return {"threadId": host, "cwd": str(tmp_path), "model": "gpt-6-astra", "provider": "openai-codex",
        "sandbox": "workspace-write", "approvalPolicy": "on-request", "effort": "xhigh", "tools": deepcopy(TOOLS),
        "config": {"agents": {"enabled": False}}, "networkAccess": True, **overrides}


async def drain(backend):
    await backend._queue.join()
    for _ in range(3):
        await asyncio.sleep(0)


async def test_native_prompt_tools_and_subscription_are_preserved(tmp_path):
    backend, events, clients = make_backend(tmp_path)
    try:
        reply = await backend.request("thread/start", thread_params(tmp_path, developerInstructions="Only an extra instruction."))
        assert reply["thread"]["id"] == "host-thread"
        assert reply["thread"]["reasoningEffort"] == "xhigh"
        client = clients[0]
        native = next(p for m, p in client.calls if m == "thread/start")
        assert native["developerInstructions"] == "Only an extra instruction."
        assert "baseInstructions" not in native and "tools" not in native
        assert native["dynamicTools"] == []
        assert native["modelProvider"] == "openai"
        assert native["config"]["features.multi_agent"] is False
        assert native["config"]["features.multi_agent_v2"] is False
        assert native["config"]["agents"] == {"enabled": False}
        assert native["config"]["model_reasoning_effort"] == "xhigh"
        assert native["permissions"] == "bello-native" and "sandbox" not in native
        profile = native["config"]["permissions"]["bello-native"]
        assert profile["network"]["enabled"] is True
        assert "sandbox_workspace_write.network_access" not in native["config"]
        assert profile["filesystem"][str(tmp_path)] == "write"
        assert profile["filesystem"][str(backend._tool_tmp)] == "write"
        assert profile["filesystem"][":workspace_roots"] == "read"
        assert native["runtimeWorkspaceRoots"] == [str(tmp_path)]
        assert native["serviceTier"] == "default"
        assert not ({"threadId", "runtimeTaskPath", "distillerEnabled", "provider"} & native.keys())
        command = client.options["command"]
        assert command[1:4] == ["app-server", "--listen", "stdio://"]
        assert 'forced_login_method="chatgpt"' in command
        assert "features.multi_agent=false" in command
        assert "features.multi_agent_v2=false" in command
        assert "agents.enabled=false" in command
        assert client.options["environment_overrides"]["OPENAI_API_KEY"] is None
        assert all(client.options["environment_overrides"][key] is None for key in (
            "BELLO_SELECTOR_SOCKET", "BELLO_SELECTOR_TCP", "BELLO_SELECTOR_TOKEN"))
        assert client.options["environment_overrides"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert client.options["environment_overrides"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert {client.options["environment_overrides"][key] for key in ("TMPDIR", "TMP", "TEMP")} == {str(backend._tool_tmp)}
        await drain(backend)
        assert events[0]["params"]["thread"]["id"] == "host-thread"
    finally:
        await backend.stop()


async def test_review_scratch_is_thread_scoped_preserved_on_resume_and_used_by_native_child(tmp_path):
    workspace = tmp_path / "review-copy"
    scratch = workspace / "review-scratch"
    scratch.mkdir(parents=True)
    other_workspace = tmp_path / "other-review-copy"
    other_scratch = other_workspace / "review-scratch"
    other_scratch.mkdir(parents=True)
    backend, _, clients = make_backend(tmp_path)
    try:
        params = thread_params(workspace, runtimeScratchRoot=str(scratch), belloRole="completion_review")
        await backend.request("thread/start", params)
        await backend.request("thread/start", thread_params(workspace, host="review-child",
            runtimeScratchRoot=str(scratch), belloRole="completion_review", parentThreadId="host-thread"))
        await backend.request("thread/start", thread_params(other_workspace, host="other-review",
            runtimeScratchRoot=str(other_scratch), belloRole="completion_review"))
        starts = [p for method, p in clients[0].calls if method == "thread/start"]
        assert len(starts) == 3
        for native, expected in zip(starts, (scratch, scratch, other_scratch)):
            assert "runtimeScratchRoot" not in native
            assert native["config"]["shell_environment_policy"]["set"] == {
                key: str(expected) for key in ("TMPDIR", "TMP", "TEMP")}
            fs = native["config"]["permissions"]["bello-native"]["filesystem"]
            assert fs[str(expected)] == "write"
            assert str(backend._tool_tmp) not in fs
        # Reviewer scratch never becomes a shared app-server environment setting.
        assert {clients[0].options["environment_overrides"][key] for key in ("TMPDIR", "TMP", "TEMP")} == {str(backend._tool_tmp)}
        await backend.request("thread/resume", {"threadId": "host-thread"})
        resumed = next(p for method, p in reversed(clients[0].calls) if method == "thread/resume")
        assert resumed["config"]["shell_environment_policy"]["set"] == starts[0]["config"]["shell_environment_policy"]["set"]
    finally:
        await backend.stop()


async def test_review_scratch_preserves_unrelated_shell_settings_and_normalizes_temp_overrides(tmp_path):
    scratch = tmp_path / "review-scratch"
    scratch.mkdir()
    config = {
        "shell_environment_policy": {"inherit": "core", "set": {"MY_FLAG": "keep", "TMPDIR": "/wrong"}},
        "shell_environment_policy.set": {"OTHER_FLAG": "also-keep"},
        "shell_environment_policy.set.TEMP": "/also-wrong",
    }
    backend, _, _ = make_backend(tmp_path)
    try:
        native = backend._thread_params(thread_params(tmp_path,
            runtimeScratchRoot=str(scratch), config=config))
        policy = native["config"]["shell_environment_policy"]
        assert policy["inherit"] == "core"
        assert policy["set"] == {"MY_FLAG": "keep", "OTHER_FLAG": "also-keep",
                                 **{key: str(scratch) for key in ("TMPDIR", "TMP", "TEMP")}}
        assert not any(key.startswith("shell_environment_policy.set") for key in native["config"])
        assert config["shell_environment_policy"]["set"]["TMPDIR"] == "/wrong"
    finally:
        await backend.stop()


@pytest.mark.parametrize("kind", ["relative", "workspace", "outside", "missing", "file", "symlink_escape", "read_only"])
async def test_native_review_scratch_rejects_invalid_or_outside_paths(tmp_path, kind):
    workspace = tmp_path / "review-copy"
    workspace.mkdir()
    scratch = workspace / "scratch"
    scratch.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "relative":
        supplied = "scratch"
    elif kind == "workspace":
        supplied = str(workspace)
    elif kind == "outside":
        supplied = str(outside)
    elif kind == "missing":
        supplied = str(workspace / "missing")
    elif kind == "file":
        (workspace / "file").write_text("not a directory")
        supplied = str(workspace / "file")
    elif kind == "symlink_escape":
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        supplied = str(workspace / "link")
    else:
        supplied = str(scratch)
    backend, _, _ = make_backend(tmp_path)
    try:
        with pytest.raises(AppServerError, match="scratch must be an existing directory inside its writable workspace"):
            backend._thread_params(thread_params(workspace, runtimeScratchRoot=supplied,
                sandbox="read-only" if kind == "read_only" else "workspace-write"))
    finally:
        await backend.stop()


async def test_only_configured_delegation_is_dynamic(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path, config={"agents": {"enabled": True}}))
        native = next(p for m, p in clients[0].calls if m == "thread/start")
        assert [tool["name"] for tool in native["dynamicTools"]] == ["bello_spawn_agent", "bello_wait_agent"]
        assert native["config"]["features.multi_agent"] is False
        assert native["config"]["features.multi_agent_v2"] is False
        assert native["config"]["agents"] == {"enabled": False}
        assert all("inputSchema" in tool for tool in native["dynamicTools"])
        assert "bello_spawn_agent" in native["developerInstructions"]
    finally:
        await backend.stop()


@pytest.mark.parametrize("bello_enabled", [False, True])
async def test_native_collaboration_hard_disabled_on_start_and_resume(tmp_path, bello_enabled):
    backend, _, clients = make_backend(tmp_path)
    requested = {
        "agents": {"enabled": bello_enabled, "allowed": {"claude-code/claude-sonnet-5": ["high"]}},
        "agents.enabled": True,
        "features.multi_agent": True,
        "features.multi_agent_v2": True,
        "features.multi_agent_v2.enabled": True,
        "features": {"multi_agent": True, "multi_agent_v2": {"enabled": True}, "other": True},
    }
    original = deepcopy(requested)
    try:
        await backend.request("thread/start", thread_params(tmp_path, config=requested))
        await backend.request("thread/resume", {"threadId": "host-thread", "config": requested})
        for method, native in clients[0].calls:
            if method not in {"thread/start", "thread/resume"}:
                continue
            config = native["config"]
            # A model-advertised multi-agent version takes precedence over the
            # old features.multi_agent=false flag. Both of these are required.
            assert config["agents"] == {"enabled": False}
            assert config["features.multi_agent_v2"] is False
            assert config["features"]["multi_agent_v2"] is False
            assert config["features.multi_agent"] is False
            assert config["features"]["multi_agent"] is False
            assert config["features"]["other"] is True
            assert not any(key.startswith("agents.") or key.startswith("features.multi_agent_v2.") for key in config)
            if method == "thread/start":
                assert bool(native["dynamicTools"]) is bello_enabled
        assert requested == original
    finally:
        await backend.stop()


async def test_config_effort_is_validated_and_native_ack_not_forged(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        params = thread_params(tmp_path, effort=None, config={"model_reasoning_effort": "xhigh"})
        await backend.request("initialize")
        clients[0].actual_effort = "high"
        with pytest.raises(AppServerError, match="different reasoning effort"):
            await backend.request("thread/start", params)
        assert clients[0].stopped == 1
    finally:
        await backend.stop()


async def test_fast_priority_translates_to_native_fast(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path, serviceTier="priority"))
        native = next(p for m, p in clients[0].calls if m == "thread/start")
        assert native["serviceTier"] == "fast"
    finally:
        await backend.stop()


async def test_native_launcher_target_is_readable_but_install_parent_is_not(tmp_path):
    install = tmp_path / "private-install"
    install.mkdir()
    executable = install / ("codex-native.exe" if os.name == "nt" else "codex-native")
    executable.write_bytes(b"fixture-not-executed")
    executable.chmod(0o700)
    launcher = tmp_path / ("codex-launcher.exe" if os.name == "nt" else "codex-launcher")
    launcher.symlink_to(executable)
    workspace = tmp_path / "workspace"
    backend, _, clients = make_backend(tmp_path, command=[str(launcher), "app-server"])
    try:
        await backend.request("thread/start", thread_params(workspace))
        await backend.request("thread/resume", {"threadId": "host-thread"})
        for method, native in clients[0].calls:
            if method not in {"thread/start", "thread/resume"}:
                continue
            filesystem = native["config"]["permissions"]["bello-native"]["filesystem"]
            assert filesystem[str(launcher)] == filesystem[str(executable)] == "read"
            assert str(install) not in filesystem and str(tmp_path) not in filesystem
    finally:
        await backend.stop()


@pytest.mark.parametrize("profile", [None, {"id": "other"}, {"id": "bello-native", "extends": ":workspace"}])
async def test_native_ack_rejects_a_different_permission_profile(tmp_path, profile):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("initialize")
        clients[0].active_profile = profile
        with pytest.raises(AppServerError, match="different filesystem permission"):
            await backend.request("thread/start", thread_params(tmp_path))
    finally:
        await backend.stop()


@pytest.mark.parametrize("had_turn,exact_missing", [(False, True), (True, True), (False, False)])
async def test_empty_native_thread_archive_uses_only_narrow_unsubscribe_fallback(tmp_path, had_turn, exact_missing):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path))
        if had_turn:
            clients[0].complete_early = True
            await backend.request("turn/start", {"threadId": "host-thread", "turnId": "turn-1", "input": "test"})
            await drain(backend)
        original = clients[0].request
        async def request(method, params=None, timeout=30):
            if method == "thread/archive":
                clients[0].calls.append((method, deepcopy(params)))
                message = "no rollout found for thread id native-thread-1" if exact_missing else "archive storage unavailable"
                raise AppServerError(str({"code": -32600, "message": message}))
            return await original(method, params, timeout)
        clients[0].request = request
        if not had_turn and exact_missing:
            await backend.request("thread/archive", {"threadId": "host-thread"})
            assert backend._threads["host-thread"]["closed"]
        else:
            with pytest.raises(AppServerError):
                await backend.request("thread/archive", {"threadId": "host-thread"})
        assert sum(m == "thread/unsubscribe" for m, _ in clients[0].calls) == (not had_turn and exact_missing)
    finally:
        await backend.stop()


@pytest.mark.parametrize("account", [None, {"type": "apiKey"}, {"type": "chatgptAuthTokens"}])
async def test_account_guard_refuses_api_or_unknown_auth(tmp_path, account):
    def factory(**kwargs):
        client = FakeNative(**kwargs)
        client.account = account
        return client
    backend = CodexBackend(state_dir=tmp_path / "state", emit=AsyncMock(), client_factory=factory)
    with pytest.raises(AppServerError, match="ChatGPT subscription"):
        await backend.request("initialize")
    assert backend._client.stopped == 1


async def test_account_and_catalog_discard_private_identity(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        account = await backend.request("account/read")
        assert account == {"provider": "openai-codex", "billingRoute": "subscription", "authMethod": "chatgpt", "planType": "pro"}
        catalog = (await backend.request("model/list"))["data"]
        assert catalog[0]["id"] == "gpt-6-astra"
        assert catalog[0]["qualifiedId"] == "openai-codex/gpt-6-astra"
        assert catalog[0]["supportedReasoningEfforts"] == ["high", "xhigh"]
        assert "private@example.test" not in json.dumps(account)
        assert not any(m.startswith("turn/") for m, _ in clients[0].calls)
    finally:
        await backend.stop()


@pytest.mark.parametrize("overrides,match", [({"effort": "ultra"}, "effort"), ({"model": "unknown"}, "advertise"),
    ({"provider": "openai"}, "billing route"), ({"serviceTier": "flex"}, "service tier")])
async def test_exact_model_validation_no_fallback(tmp_path, overrides, match):
    backend, _, clients = make_backend(tmp_path)
    try:
        with pytest.raises(AppServerError, match=match):
            await backend.request("model/validate", {"model": "gpt-6-astra", "effort": "xhigh", **overrides})
        assert not any(m.startswith("turn/") for m, _ in clients[0].calls)
    finally:
        await backend.stop()


async def test_early_turn_events_use_host_ids_without_rewriting_text_or_item_ids(tmp_path):
    backend, events, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path))
        clients[0].complete_early = True
        reply = await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "Do work", "effort": "xhigh"})
        await drain(backend)
        assert reply["turn"]["id"] == "host-turn"
        completed = next(e for e in events if e["method"] == "item/completed")
        assert completed["params"]["threadId"] == "host-thread"
        assert completed["params"]["turnId"] == "host-turn"
        assert completed["params"]["item"]["id"] == "native-item-1"
        assert completed["params"]["item"]["text"] == "native-thread-1"
        assert "activeTurnId" not in backend._threads["host-thread"]
        assert next(p for m, p in clients[0].calls if m == "turn/start")["threadId"] == "native-thread-1"
        assert "turnId" not in next(p for m, p in clients[0].calls if m == "turn/start")
    finally:
        await backend.stop()


async def test_turn_mapping_is_scoped_per_thread(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path, host="one"))
        await backend.request("thread/start", thread_params(tmp_path, host="two"))
        await backend.request("turn/start", {"threadId": "one", "turnId": "host-one", "input": "a"})
        clients[0].next_turn = 0  # Native turn numbers are allowed to repeat in different threads.
        await backend.request("turn/start", {"threadId": "two", "turnId": "host-two", "input": "b"})
        assert backend._native_turns[("native-thread-1", "native-turn-1")] == "host-one"
        assert backend._native_turns[("native-thread-2", "native-turn-1")] == "host-two"
    finally:
        await backend.stop()


async def test_persisted_mapping_resumes_native_session_not_new_thread(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    await backend.request("thread/start", thread_params(tmp_path))
    await backend.request("turn/start", {"threadId": "host-thread", "turnId": "old-host-turn", "input": "a"})
    await backend.stop()
    resumed, _, new_clients = make_backend(tmp_path)
    try:
        result = await resumed.request("thread/resume", {"threadId": "host-thread"})
        assert result["thread"]["id"] == "host-thread"
        assert next(p for m, p in new_clients[0].calls if m == "thread/resume")["threadId"] == "native-thread-1"
        assert not any(m == "thread/start" for m, _ in new_clients[0].calls)
        read = await resumed.request("thread/read", {"threadId": "host-thread"})
        assert read["thread"]["turns"][0]["id"] == "old-host-turn"
    finally:
        await resumed.stop()


async def test_native_approval_is_namespaced_and_reply_forwarded(tmp_path):
    backend, events, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/commandExecution/requestApproval", {"threadId": "native-thread-1", "turnId": "native-turn-1", "command": "pytest"}, 17)
        await drain(backend)
        event = next(e for e in events if e.get("id"))
        assert event["id"].startswith("codex:")
        assert event["params"]["threadId"] == "host-thread"
        assert await backend.respond(event["id"], {"decision": "accept"}) is True
        assert clients[0].responses[-1] == (17, {"decision": "accept"}, None)
        assert await backend.respond(event["id"], {}) is False
        await clients[0].notify_event("serverRequest/resolved", {"threadId": "native-thread-1", "requestId": 17})
        await drain(backend)
        assert events[-1]["params"]["requestId"] == event["id"]
        assert 17 not in backend._request_history
    finally:
        await backend.stop()


async def test_approval_never_does_not_secretly_escalate(tmp_path):
    backend, events, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path, approvalPolicy="never"))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/commandExecution/requestApproval", {"threadId": "native-thread-1", "turnId": "native-turn-1"}, 19)
        await drain(backend)
        assert not any(e.get("id") for e in events)
        assert clients[0].responses[-1] == (19, {"decision": "decline"}, None)
    finally:
        await backend.stop()


async def test_dynamic_delegation_goes_through_host_with_stable_identity(tmp_path):
    handler = AsyncMock(return_value={"content": [{"type": "text", "text": "child-id"}], "isError": False})
    backend, _, clients = make_backend(tmp_path, tool_handler=handler)
    try:
        await backend.request("thread/start", thread_params(tmp_path, config={"agents": {"enabled": True}}))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_spawn_agent", "arguments": {"task": "Inspect"}, "callId": "call-1"}, 20)
        await drain(backend)
        called = handler.call_args.args[0]
        assert called["threadId"] == "host-thread" and called["turnId"] == "host-turn"
        assert called["name"] == "spawn_agent"
        assert called["callId"] == "codex:host-thread:call-1"
        assert clients[0].responses[-1][1] == {"contentItems": [{"type": "inputText", "text": "child-id"}], "success": True}
    finally:
        await backend.stop()


async def test_disabled_delegation_rejects_unadvertised_dynamic_call(tmp_path):
    handler = AsyncMock()
    backend, _, clients = make_backend(tmp_path, tool_handler=handler)
    try:
        await backend.request("thread/start", thread_params(tmp_path))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_spawn_agent", "arguments": {"task": "Inspect"}, "callId": "disabled-call"}, 22)
        await drain(backend)
        handler.assert_not_awaited()
        assert clients[0].responses[-1][0] == 22
        assert clients[0].responses[-1][1]["success"] is False
    finally:
        await backend.stop()


@pytest.mark.parametrize("timeout", [120000, -1, "private-value-must-not-leak"])
async def test_invalid_wait_timeout_explains_seconds_without_replay_or_argument_leak(tmp_path, timeout):
    from supervisor.runtime.tools import _SCHEMAS, tool_definitions

    executed = []

    async def handler(request):
        _SCHEMAS[request["name"]].validate(request["arguments"])
        executed.append(request)
        raise AssertionError("invalid wait must not reach delegation")

    backend, _, clients = make_backend(tmp_path, tool_handler=handler)
    try:
        await backend.request("thread/start", thread_params(tmp_path,
            tools=tool_definitions(), config={"agents": {"enabled": True}}))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_wait_agent", "arguments": {"agent_id": "owned-child", "timeout": timeout},
            "callId": "bad-timeout"}, 23)
        await drain(backend)
        reply = clients[0].responses[-1][1]
        assert reply["success"] is False
        text = reply["contentItems"][0]["text"]
        assert "seconds from 0 to 3600" in text and "default 60" in text
        assert "120 for two minutes" in text and "did not stop the child" in text
        assert "private-value-must-not-leak" not in text and "owned-child" not in text
        assert executed == []
        assert not any(method in {"thread/archive", "turn/interrupt"} for method, _ in clients[0].calls)
        advertised = next(
            tool for method, params in clients[0].calls if method == "thread/start"
            for tool in params["dynamicTools"] if tool["name"] == "bello_wait_agent")
        assert "seconds, not milliseconds" in advertised["description"]
        assert "Default 60" in advertised["inputSchema"]["properties"]["timeout"]["description"]
    finally:
        await backend.stop()


@pytest.mark.parametrize("arguments", [{"agent_id": "owned-child", "timeout": 120}, {"agent_id": "owned-child"}])
async def test_valid_wait_seconds_and_default_preserve_arguments_and_returned_report(tmp_path, arguments):
    from supervisor.runtime.tools import _SCHEMAS, tool_definitions

    executed = []

    async def handler(request):
        _SCHEMAS[request["name"]].validate(request["arguments"])
        executed.append(request)
        return {"content": [{"type": "text", "text": "child report"}], "isError": False}

    backend, _, clients = make_backend(tmp_path, tool_handler=handler)
    try:
        await backend.request("thread/start", thread_params(tmp_path,
            tools=tool_definitions(), config={"agents": {"enabled": True}}))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_wait_agent", "arguments": arguments, "callId": "valid-wait"}, 24)
        await drain(backend)
        assert len(executed) == 1
        assert executed[0]["arguments"] == arguments
        assert executed[0]["threadId"] == "host-thread" and executed[0]["turnId"] == "host-turn"
        assert clients[0].responses[-1][1] == {
            "contentItems": [{"type": "inputText", "text": "child report"}], "success": True}
    finally:
        await backend.stop()


@pytest.mark.parametrize("validation_error", [False, True])
async def test_unrelated_delegation_errors_still_redact_internal_details(tmp_path, validation_error):
    from supervisor.runtime.tools import _SCHEMAS

    async def handler(request):
        if validation_error:
            _SCHEMAS["wait_agent"].validate({"agent_id": ["private-internal-value"]})
        raise RuntimeError("private-internal-value")

    backend, _, clients = make_backend(tmp_path, tool_handler=handler)
    try:
        await backend.request("thread/start", thread_params(tmp_path, config={"agents": {"enabled": True}}))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_wait_agent", "arguments": {"agent_id": "owned-child"}, "callId": "other-error"}, 25)
        await drain(backend)
        assert clients[0].responses[-1][1] == {
            "contentItems": [{"type": "inputText", "text": "Bello delegation failed."}], "success": False}
    finally:
        await backend.stop()


async def test_turn_steer_interrupt_and_archive_translate_ids(tmp_path):
    backend, _, clients = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        reply = await backend.request("turn/steer", {"threadId": "host-thread", "expectedTurnId": "host-turn", "input": [{"type": "text", "text": "b"}]})
        assert reply["turnId"] == "host-turn"
        await backend.request("turn/interrupt", {"threadId": "host-thread", "turnId": "host-turn"})
        assert clients[0].calls[-1] == ("turn/interrupt", {"threadId": "native-thread-1", "turnId": "native-turn-1"})
        await backend.request("thread/archive", {"threadId": "host-thread"})
        assert clients[0].calls[-1] == ("thread/archive", {"threadId": "native-thread-1"})
    finally:
        await backend.stop()


@pytest.mark.parametrize("lose", ["lose_turn_ack", "lose_thread_ack"])
async def test_lost_ack_kills_native_execution_and_never_retries(tmp_path, lose):
    errors = AsyncMock()
    backend, _, clients = make_backend(tmp_path, on_error=errors)
    try:
        await backend.request("initialize")
        setattr(clients[0], lose, True)
        if lose == "lose_turn_ack":
            await backend.request("thread/start", thread_params(tmp_path))
            method, params = "turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"}
        else:
            method, params = "thread/start", thread_params(tmp_path)
        with pytest.raises(AppServerTimeoutError):
            await backend.request(method, params)
        assert clients[0].stopped == 1
        assert sum(m == method for m, _ in clients[0].calls) == 1
        assert errors.await_count == 1
        with pytest.raises(AppServerError, match="transport failed"):
            await backend.request(method, params)
    finally:
        await backend.stop()


async def test_native_distiller_requires_verified_bridge_not_silent_off(tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller
    validate = AsyncMock(side_effect=RuntimeError("unverified native build"))
    monkeypatch.setattr(codex_distiller, "validate_native_selection", validate)
    bridge = type("Bridge", (), {"environment": {}, "start": AsyncMock(), "close": AsyncMock()})()
    monkeypatch.setattr(codex_distiller, "CodexDistillerBridge", lambda *a, **k: bridge)
    backend, _, clients = make_backend(tmp_path, distiller=object())
    try:
        await backend.request("initialize")
        await backend.request("model/validate", {"model": "gpt-6-astra", "effort": "xhigh"})
        await backend.request("thread/start", thread_params(tmp_path, host="review", distillerEnabled=False))
        validate.assert_not_awaited()
        native = next(p for m, p in clients[0].calls if m == "thread/start")
        assert "features.bello_native_selection" not in native["config"]
        for method, params in [
            ("model/validate", {"model": "gpt-6-astra", "distillerEnabled": True}),
            ("thread/start", thread_params(tmp_path, distillerEnabled=True)),
            ("thread/resume", {"threadId": "review", "distillerEnabled": True}),
        ]:
            with pytest.raises(RuntimeError, match="unverified"):
                await backend.request(method, params)
        assert not any(m in {"turn/start", "thread/resume"} for m, _ in clients[0].calls)
        assert sum(m == "thread/start" for m, _ in clients[0].calls) == 1
    finally:
        await backend.stop()


async def test_native_distiller_feature_and_scope_are_coder_only(tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller
    bridge = type("Bridge", (), {"environment": {"BELLO_SELECTOR_SOCKET": "/private/socket"},
        "thread_config": {"features.bello_native_selection": True}, "start": AsyncMock(), "close": AsyncMock(),
        "register_scope": lambda self, root, task=None: self.scopes.append((root, task)), "scopes": []})()
    monkeypatch.setattr(codex_distiller, "validate_native_selection", AsyncMock(return_value={}))
    monkeypatch.setattr(codex_distiller, "CodexDistillerBridge", lambda *a, **k: bridge)
    backend, _, clients = make_backend(tmp_path, distiller=object())
    try:
        await backend.request("thread/start", thread_params(tmp_path, distillerEnabled=True, runtimeTaskPath=str(tmp_path / "TASK.md")))
        await backend.request("thread/start", thread_params(tmp_path, host="review", distillerEnabled=False))
        native = [p for m, p in clients[0].calls if m == "thread/start"]
        assert native[0]["config"]["features.bello_native_selection"] is True
        assert native[1]["config"]["features.bello_native_selection"] is False
        assert bridge.scopes == [(tmp_path, tmp_path / "TASK.md")]
        assert clients[0].options["environment_overrides"]["BELLO_SELECTOR_SOCKET"] == "/private/socket"
    finally:
        await backend.stop()
    bridge.close.assert_awaited_once()


async def test_native_selection_manifest_is_explicit_host_setting_and_checked_once(tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller
    manifest = tmp_path / "deployment-capability.json"
    monkeypatch.setenv("BELLO_CODEX_SELECTION_MANIFEST", str(manifest))
    validator = AsyncMock(return_value={"version": "0.153.4"})
    monkeypatch.setattr(codex_distiller, "validate_native_selection", validator)
    bridge = type("Bridge", (), {"environment": {}, "start": AsyncMock(), "close": AsyncMock()})()
    monkeypatch.setattr(codex_distiller, "CodexDistillerBridge", lambda *a, **k: bridge)
    command = ["/opt/bello-native/bin/codex", "app-server", "--listen", "stdio://"]
    backend, _, clients = make_backend(tmp_path, command=command, distiller=object())
    try:
        await backend.request("initialize")
        await backend.request("model/validate", {"model": "gpt-6-astra", "distillerEnabled": False})
        validator.assert_not_awaited()
        await backend.request("model/validate", {"model": "gpt-6-astra", "distillerEnabled": True})
        await backend.request("model/validate", {"model": "gpt-6-astra", "distillerEnabled": True})
        validator.assert_awaited_once_with(command, manifest_path=manifest)
        assert all(str(manifest) not in str(params) for _, params in clients[0].calls)
        assert not any(method == "turn/start" for method, _ in clients[0].calls)
    finally:
        await backend.stop()


async def test_disabled_distiller_ignores_invalid_manifest(tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller
    monkeypatch.setenv("BELLO_CODEX_SELECTION_MANIFEST", "/missing/manifest.json")
    validator = AsyncMock(side_effect=AssertionError("D-off must not validate a selection build"))
    bridge = AsyncMock(side_effect=AssertionError("D-off must not start a selector"))
    monkeypatch.setattr(codex_distiller, "validate_native_selection", validator)
    monkeypatch.setattr(codex_distiller, "CodexDistillerBridge", bridge)
    backend, _, _ = make_backend(tmp_path)
    try:
        await backend.request("thread/start", thread_params(tmp_path, distillerEnabled=False))
        validator.assert_not_awaited()
        bridge.assert_not_called()
    finally:
        await backend.stop()


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "danger-full-access"])
async def test_native_scope_is_preserved_across_resume_and_turn(tmp_path, mode):
    backend, _, clients = make_backend(tmp_path)
    try:
        params = thread_params(tmp_path, sandbox=mode, networkAccess=False,
            runtimeWorkspaceRoots=[str(tmp_path / "read-dependency")],
            config={"sandbox_workspace_write.network_access": True})
        await backend.request("thread/start", params)
        await backend.request("thread/resume", {"threadId": "host-thread"})
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "turn-1", "input": "test",
            "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True}})
        for method, native in clients[0].calls:
            if method in {"thread/start", "thread/resume"}:
                if mode == "danger-full-access":
                    assert native["sandbox"] == mode and "permissions" not in native
                else:
                    assert "sandbox" not in native and native["permissions"] == "bello-native"
                    assert native["runtimeWorkspaceRoots"] == [str(tmp_path), str(tmp_path / "read-dependency")]
                    assert "sandbox_workspace_write.network_access" not in native["config"]
                    profile = native["config"]["permissions"]["bello-native"]
                    assert profile["network"] == {"enabled": False}
                    assert ("write" in profile["filesystem"].values()) == (mode == "workspace-write")
            if method == "turn/start":
                assert ("sandboxPolicy" in native) == (mode == "danger-full-access")
                assert "permissions" not in native
    finally:
        await backend.stop()


def test_native_owned_temp_directory_cannot_follow_symlink(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    destination = tmp_path / "other"
    destination.mkdir(mode=0o700)
    (state / "codex-tmp").symlink_to(destination, target_is_directory=True)
    with pytest.raises(AppServerError, match="private|symlink|directory"):
        CodexBackend(state_dir=state, emit=AsyncMock())


async def test_appserver_environment_overrides_are_child_local(tmp_path, monkeypatch):
    from supervisor import appserver
    monkeypatch.setenv("BELLO_TEST_PARENT_VALUE", "parent")
    monkeypatch.setattr(appserver, "_codex_home_from_environment", lambda env: tmp_path / "nonexistent-home")
    monkeypatch.setattr(appserver, "_app_server_command", lambda command, **kwargs: command)
    spawn = AsyncMock(side_effect=RuntimeError("synthetic spawn interception"))
    monkeypatch.setattr(appserver.asyncio, "create_subprocess_exec", spawn)
    client = appserver.AppServerClient(command=["codex", "app-server"], environment_overrides={
        "BELLO_TEST_PARENT_VALUE": None, "BELLO_TEST_CHILD_VALUE": "child"})
    with pytest.raises(RuntimeError, match="synthetic spawn interception"):
        await client.start()
    child_env = spawn.call_args.kwargs["env"]
    assert "BELLO_TEST_PARENT_VALUE" not in child_env
    assert child_env["BELLO_TEST_CHILD_VALUE"] == "child"
    assert appserver.os.environ["BELLO_TEST_PARENT_VALUE"] == "parent"
    assert "BELLO_TEST_CHILD_VALUE" not in appserver.os.environ


async def test_background_dynamic_reply_failure_fences_engine_and_notifies_host(tmp_path):
    notified = asyncio.Event()
    errors = []
    async def on_error(error):
        errors.append(error)
        notified.set()
    handler = AsyncMock(return_value={"content": [{"type": "text", "text": "child"}]})
    backend, _, clients = make_backend(tmp_path, tool_handler=handler, on_error=on_error)
    try:
        await backend.request("thread/start", thread_params(tmp_path, config={"agents": {"enabled": True}}))
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
        clients[0].respond = AsyncMock(side_effect=AppServerError("synthetic lost reply"))
        await clients[0].notify_event("item/tool/call", {"threadId": "native-thread-1", "turnId": "native-turn-1",
            "tool": "bello_spawn_agent", "arguments": {}, "callId": "call-2"}, 21)
        await asyncio.wait_for(notified.wait(), 1)
        assert len(errors) == 1 and clients[0].stopped == 1
        assert handler.await_count == 1
        assert clients[0].respond.await_count == 1
        with pytest.raises(AppServerError, match="transport failed"):
            await backend.request("model/list")
    finally:
        await backend.stop()


async def test_cleanup_failure_still_notifies_host(tmp_path):
    notified = AsyncMock()
    backend, _, clients = make_backend(tmp_path, on_error=notified)
    try:
        await backend.request("initialize")
        clients[0].stop = AsyncMock(side_effect=AppServerError("synthetic cleanup failure"))
        await backend._fail(AppServerError("original transport failure"))
        notified.assert_awaited_once()
        assert "cleanup" in notified.call_args.args[0].__notes__[0]
    finally:
        clients[0].stop = AsyncMock()
        await backend.stop()


def test_persistent_home_reuses_exact_path_and_default_home_still_cleans(tmp_path):
    from supervisor import appserver
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    source = tmp_path / "source-home"
    source.mkdir()
    (source / "config.toml").write_text('model = "test-model"\n')
    stable = state / "codex-home"
    first = appserver._prepare_persistent_codex_home(source, stable)
    (first / "saved-rollout.json").write_text('{"path":"persistent"}')
    client = appserver.AppServerClient(persistent_isolated_home=stable)
    client._isolated_codex_home = first
    client._cleanup_isolated_codex_home()
    assert first.is_dir()
    second = appserver._prepare_persistent_codex_home(source, stable)
    assert first == second and (second / "saved-rollout.json").is_file()
    temporary = appserver._create_isolated_codex_home(source)
    legacy = appserver.AppServerClient()
    legacy._isolated_codex_home = temporary
    legacy._cleanup_isolated_codex_home()
    assert not temporary.exists()
    assert (source / "config.toml").read_text() == 'model = "test-model"\n'


@pytest.mark.parametrize("unsafe", ["directory_link", "parent_link", "unmarked", "marker_link", "marker_hardlink", "public_parent"])
def test_persistent_home_rejects_untrusted_path_or_marker(tmp_path, unsafe):
    from supervisor import appserver
    state = tmp_path / "private-state"
    state.mkdir(mode=0o700)
    source = tmp_path / "source-home"
    source.mkdir()
    stable = state / "codex-home"
    if unsafe == "directory_link":
        stable.symlink_to(source, target_is_directory=True)
    elif unsafe == "parent_link":
        linked_parent = tmp_path / "linked-state"
        linked_parent.symlink_to(state, target_is_directory=True)
        stable = linked_parent / "codex-home"
    elif unsafe == "unmarked":
        stable.mkdir(mode=0o700)
    elif unsafe == "public_parent":
        if os.name == "nt":
            pytest.skip("POSIX permission-mode assertion")
        state.chmod(0o755)
    else:
        stable.mkdir(mode=0o700)
        marker_source = tmp_path / "foreign-marker"
        marker_source.write_bytes(appserver._PERSISTENT_HOME_CONTENT)
        marker_source.chmod(0o600)
        marker = stable / appserver._PERSISTENT_HOME_MARKER
        if unsafe == "marker_link":
            marker.symlink_to(marker_source)
        else:
            os.link(marker_source, marker)
    with pytest.raises(AppServerError):
        appserver._prepare_persistent_codex_home(source, stable)
    assert source.is_dir()


_FAKE_NATIVE_RPC = r'''
import json, os, sys
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
index = home / 'fake-index.json'
def read_thread():
    return json.loads(Path(json.loads(index.read_text())['path']).read_text())
def save_thread(thread):
    path = home / 'sessions' / 'native-thread-1.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(thread))
    index.write_text(json.dumps({'path':str(path.absolute())}))
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request['method'], request.get('params') or {}
    event = None
    try:
        if method == 'initialize': result = {}
        elif method == 'account/read': result = {'account': {'type':'chatgpt'}}
        elif method == 'model/list': result = {'data':[{'id':'gpt-6-astra','model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'xhigh'}]}]}
        elif method == 'thread/start':
            thread = {'id':'native-thread-1','turns':[]}
            save_thread(thread)
            result = {'thread':thread,'reasoningEffort':'xhigh'}
        elif method in ('thread/resume','thread/read'):
            result = {'thread':read_thread(),'reasoningEffort':'xhigh'}
        elif method == 'turn/start':
            thread = read_thread()
            turn = {'id':'native-turn-'+str(len(thread['turns'])+1),'status':'completed','items':[]}
            thread['turns'].append(turn)
            save_thread(thread)
            result = {'turn':turn}
            event = {'method':'turn/completed','params':{'threadId':thread['id'],'turn':turn}}
        else: result = {}
        send({'id':request['id'],'result':result})
        if event: send(event)
    except Exception as error:
        send({'id':request['id'],'error':{'code':-32000,'message':type(error).__name__}})
'''


async def test_real_appserver_lifecycle_and_fake_rpc_resume_across_processes(tmp_path, monkeypatch):
    """Real subprocess/isolation/storage; fake provider-free JSON-RPC only."""
    source = tmp_path / "empty-source-home"
    source.mkdir(mode=0o700)
    monkeypatch.setenv("CODEX_HOME", str(source))
    state = tmp_path / "state"
    command = [str(Path(sys.executable).resolve()), "-u", "-c", _FAKE_NATIVE_RPC]
    first = CodexBackend(state_dir=state, emit=AsyncMock(), command=command)
    try:
        await first.request("thread/start", thread_params(tmp_path))
        await first.request("turn/start", {"threadId": "host-thread", "turnId": "first-host-turn", "input": "synthetic fixture"})
        first_pid = first._client.process.pid
    finally:
        await first.stop()
    stable = state / "codex-home"
    saved_path = Path(json.loads((stable / "fake-index.json").read_text())["path"])
    assert saved_path.is_file() and saved_path.is_relative_to(stable)
    assert first._client.process is None
    second = CodexBackend(state_dir=state, emit=AsyncMock(), command=command)
    try:
        resumed = await second.request("thread/resume", {"threadId": "host-thread"})
        assert second._client.process.pid != first_pid
        assert second._client._isolated_codex_home == stable.resolve()
        assert resumed["thread"]["id"] == "host-thread"
        assert resumed["thread"]["turns"][0]["id"] == "first-host-turn"
        following = await second.request("turn/start", {"threadId": "host-thread", "turnId": "second-host-turn", "input": "synthetic continuation"})
        assert following["turn"]["id"] == "second-host-turn"
        assert len(json.loads(saved_path.read_text())["turns"]) == 2
    finally:
        await second.stop()
