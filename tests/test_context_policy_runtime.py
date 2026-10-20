"""Offline No Need For Context (NNC) policy contracts (no provider calls).

NNC changes conversation history only. It adds no sandbox, readable-root, network,
approval or escalation restriction, so the parity tests below run the same scenario
with the policy OFF and ON and require identical host behavior. The remaining tests
cover the run-owned policy plumbing and the native helper's capability gate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import re
from unittest.mock import AsyncMock

import pytest

from supervisor.appserver import AppServerError
from supervisor.project_config import NoNeedForContextConfig
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.codex import CodexBackend
from tests.test_runtime_client import FakeBackend, start
from tests.test_runtime_codex_backend import FakeNative, drain, thread_params


POLICY = NoNeedForContextConfig(enabled=True, threshold_tokens=120000, retain_percent=12.5)
OFF = NoNeedForContextConfig()
MODELS = ("gpt-6-astra", "claude-code/claude-sonnet-4-6", "openai/gpt-6-astra", "anthropic/claude-sonnet-4-6")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
# The only native thread-config entries NNC may add or change.
COMPACTION_KEYS = {"features.bello_context_compaction", "model_auto_compact_token_limit",
                   "model_auto_compact_token_limit_scope", "model_post_turn_compact_threshold_percent",
                   "bello_context_retain_percent"}
APPROVALS = ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
             "item/permissions/requestApproval", "execCommandApproval", "applyPatchApproval")


@pytest.fixture(autouse=True)
def forbid_real_provider_processes(monkeypatch):
    monkeypatch.setattr("supervisor.appserver.AppServerClient.start",
                        AsyncMock(side_effect=AssertionError("offline test attempted a real app-server")))


def make_client(tmp_path, *, enabled=True, name=None, runtime_enabled=True):
    base = tmp_path / name if name else tmp_path
    root, workspace = base / "project", base / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    engines = {engine: FakeBackend() for engine in ("codex", "claude-code", "pi")}
    client = RuntimeClient(cwd=root, backends=engines)
    client.configure_run(no_need_for_context=POLICY if enabled else OFF, windows_native_root_read=True,
                         runtime_enabled=runtime_enabled)
    return client, workspace


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("model", MODELS)
async def test_run_owner_controls_every_role_and_ignores_thread_override(tmp_path, enabled, model):
    client, workspace = make_client(tmp_path, enabled=enabled)
    try:
        # The revision coder is a "coder" thread; reviewers use their own roles.
        for role in ("coder", "completion_review", "adversary", "runtime"):
            thread = await start(client, workspace, model, belloRole=role,
                                 noNeedForContext={"enabled": not enabled, "thresholdTokens": 1, "retainPercent": 50})
            record = client._threads[thread]
            expected = client.no_need_for_context.to_backend_data()
            expected["enabled"] = enabled and role != "runtime"
            assert record["noNeedForContext"] == expected
            backend = client._engines[record["engine"]]
            sent = next(params for method, params in reversed(backend.calls) if method == "thread/start")
            assert sent["noNeedForContext"] == expected
            assert sent["approvalPolicy"] == "on-request"
            assert "context" not in sent.get("developerInstructions", "").lower()
    finally:
        await client.stop()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("role", ["coder", "completion_review", "adversary", "runtime"])
async def test_descendants_inherit_owner_policy_across_providers(tmp_path, enabled, role):
    client, workspace = make_client(tmp_path, enabled=enabled)
    agents = {"enabled": True, "role": role, "max_concurrent_threads_per_session": 4,
              "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"], "openai/gpt-6-astra": ["high"]}}
    try:
        parent = await start(client, workspace, belloRole=role, config={"agents": agents})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        reply = await client._delegate("spawn_agent", {"model": "claude-code/claude-sonnet-4-6",
            "effort": "high", "message": "inspect"}, parent, turn)
        child = json.loads(reply["content"][0]["text"])["agent_id"]
        assert client._threads[child]["noNeedForContext"] == client._threads[parent]["noNeedForContext"]
        if role == "coder":
            reply = await client._delegate("spawn_agent", {"model": "openai/gpt-6-astra",
                "effort": "high", "message": "inspect"}, child, client._threads[child]["activeTurnId"])
            grandchild = json.loads(reply["content"][0]["text"])["agent_id"]
            assert client._threads[grandchild]["noNeedForContext"] == client._threads[parent]["noNeedForContext"]
    finally:
        await client.stop()


@pytest.mark.parametrize("method", ["thread/resume", "turn/start"])
@pytest.mark.parametrize("field,value", [("enabled", False), ("thresholdTokens", 1), ("retainPercent", 50)])
async def test_context_policy_is_immutable_on_resume_and_turn(tmp_path, method, field, value):
    client, workspace = make_client(tmp_path)
    try:
        thread = await start(client, workspace)
        backend = client._engines["codex"]
        before = len(backend.calls)
        changed = {**POLICY.to_backend_data(), field: value}
        with pytest.raises(AppServerError, match="cannot change.*context compaction"):
            await client.request(method, {"threadId": thread, "noNeedForContext": changed})
        assert len(backend.calls) == before
        assert client._threads[thread]["noNeedForContext"] == POLICY.to_backend_data()
    finally:
        await client.stop()


async def test_saved_run_rejects_changed_global_switch_before_backend_dispatch(tmp_path):
    client, workspace = make_client(tmp_path)
    thread = await start(client, workspace)
    await client.stop()
    resumed, _ = make_client(tmp_path, enabled=False)
    try:
        for method in ("thread/resume", "turn/start"):
            with pytest.raises(AppServerError, match="changed switches"):
                await resumed.request(method, {"threadId": thread})
        assert not any(backend.calls for backend in resumed._engines.values())
    finally:
        await resumed.stop()


# ---------------------------------------------------------------------------
# NNC adds no sandbox, readable-root, network or approval restriction (client)
# ---------------------------------------------------------------------------

class Intercepted(RuntimeError):
    pass


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"host cannot create fixture symlink: {error}")


def shape(value, *directories):
    """JSON-normalize a value; per-run directories and generated ids become tokens."""
    text = json.dumps(value, default=str, sort_keys=True)
    for token, directory in directories:
        # JSON doubles Windows backslashes; match the escaped form first.
        text = text.replace(json.dumps(str(directory))[1:-1], token).replace(str(directory), token)
    return json.loads(re.sub(r"tool-[0-9a-f]{64}", "<item>", text))


async def client_scenario(tmp_path, *, enabled, model, sandbox, approve):
    """Run one deterministic thread; return everything NNC must leave unchanged."""
    name = "on" if enabled else "off"
    client, workspace = make_client(tmp_path, enabled=enabled, name=name)
    home, dependency = tmp_path / "home", tmp_path / "shared-dependency"
    custom_codex = tmp_path / "custom-codex-state"
    requests, policies = [], []

    async def handler(message):
        requests.append((message.method, deepcopy(message.params)))
        await client.respond(message.request_id, {"decision": "accept" if approve else "decline"})

    def runner_factory(policy):
        policies.append(policy)
        raise Intercepted("runner intercepted")

    client.server_request_handler = handler
    try:
        await client.start()
        client._host.runner_factory = runner_factory
        # Private-looking roots are ordinary caller-assigned read dependencies.
        roots = [str(workspace), str(client.state_dir), str(home / ".claude"), str(home / ".codex"),
                 str(custom_codex), str(dependency)]
        response = await client.thread_start({"cwd": str(workspace), "runtimeWorkspaceRoots": roots,
            "model": model, "sandbox": sandbox, "approvalPolicy": "on-request"})
        thread = response["thread"]["id"]
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        base = {"threadId": thread, "turnId": turn}
        calls = {
            "read": ("read_file", {"path": str(dependency / "notes.txt")}),
            "run": ("exec_command", {"command": "echo ordinary"}),
            "escalated": ("exec_command", {"command": "echo escalated", "justification": "parity",
                                           "sandbox_permissions": "require_escalated"}),
        }
        results = {}
        for label, (tool, arguments) in calls.items():
            outcome = await client._host.call({**base, "callId": label, "name": tool, "arguments": arguments})
            results[label] = (outcome["isError"], outcome["content"][0]["text"])
        record = {key: value for key, value in client._threads[thread].items() if key != "noNeedForContext"}
        engine = client._engines[record["engine"]]
        sent = [(method, {k: v for k, v in params.items() if k != "noNeedForContext"})
                for method, params in engine.calls if method in {"thread/start", "turn/start"}]
        scope = asdict(client._scope_for(thread, turn))
        sandboxes = [(policy.mode, policy.network_access, policy.readable_roots, policy.temp_root)
                     for policy in policies]
        directories = (("<run>", tmp_path / name), ("<thread>", thread), ("<turn>", turn))
        return {
            "roots": roots, "response": shape(response, *directories), "record": shape(record, *directories),
            "sent": shape(sent, *directories), "scope": shape(scope, *directories),
            "sandboxes": shape(sandboxes, *directories), "results": shape(results, *directories),
            "approvals": shape(requests, *directories), "policy": client._threads[thread]["noNeedForContext"],
        }
    finally:
        await client.stop()


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("sandbox", SANDBOXES)
@pytest.mark.parametrize("model", MODELS)
async def test_nnc_on_keeps_client_scope_sandbox_and_approvals_identical_to_off(
    tmp_path, monkeypatch, model, sandbox, approve,
):
    home = tmp_path / "home"
    custom_codex = tmp_path / "custom-codex-state"
    for directory in (home / ".claude", home / ".codex", custom_codex, tmp_path / "shared-dependency"):
        directory.mkdir(parents=True)
    (tmp_path / "shared-dependency" / "notes.txt").write_text("synthetic dependency", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    off = await client_scenario(tmp_path, enabled=False, model=model, sandbox=sandbox, approve=approve)
    on = await client_scenario(tmp_path, enabled=True, model=model, sandbox=sandbox, approve=approve)
    assert off["policy"]["enabled"] is False and on["policy"] == POLICY.to_backend_data()
    for key in ("response", "record", "sent", "scope", "sandboxes", "results", "approvals"):
        assert on[key] == off[key], key
    # Independent expectations: nothing was rejected, narrowed or pre-declined.
    start_request = on["sent"][0][1]
    assert start_request["sandbox"] == sandbox and start_request["approvalPolicy"] == "on-request"
    assert start_request["networkAccess"] is False
    assert start_request["runtimeWorkspaceRoots"] == [root.replace(str(tmp_path / "on"), "<run>") for root in on["roots"]]
    assert len(start_request["runtimeWorkspaceRoots"]) == 6
    assert on["scope"]["approval_policy"] == "on-request" and on["scope"]["mode"] == sandbox
    assert [root.replace(str(tmp_path / "on"), "<run>") for root in on["roots"]] == [
        root.replace(str(tmp_path / "on"), "<run>") for root in on["scope"]["readable_roots"]]
    assert len(on["approvals"]) == 1
    assert on["approvals"][0][0] == "item/commandExecution/requestApproval"
    assert "belloApprovalPolicy" not in on["approvals"][0][1]
    if approve:
        # The escalation is granted exactly as before and only to that command.
        assert [mode for mode, *_ in on["sandboxes"]] == [sandbox, sandbox, "danger-full-access"]
        assert on["results"]["escalated"][1] == "runner intercepted"
    else:
        assert [mode for mode, *_ in on["sandboxes"]] == [sandbox, sandbox]
        assert "denied" in on["results"]["escalated"][1].lower()


@pytest.mark.parametrize("decision", [True, False])
async def test_nnc_on_client_approvals_follow_the_host_exactly_like_off(tmp_path, decision):
    outcomes = {}
    for enabled in (False, True):
        client, workspace = make_client(tmp_path, enabled=enabled, name="on" if enabled else "off")
        seen = []

        async def handler(message, client=client, seen=seen):
            seen.append((message.method, message.params["command"], message.params["cwd"]))
            await client.respond(message.request_id, {"decision": "accept" if decision else "decline"})

        client.server_request_handler = handler
        try:
            thread = await start(client, workspace)
            request = {"threadId": thread, "command": "pytest", "cwd": str(workspace), "belloApprovalPolicy": "on-request"}
            answer = await client._approve("item/commandExecution/requestApproval", request)
            record = client._threads[thread]
            outcomes[enabled] = (answer, [(m, c, cwd.replace(str(workspace), "<ws>")) for m, c, cwd in seen],
                                 record["approvalPolicy"], dict(client._approval_waiters))
            assert record["noNeedForContext"]["enabled"] is enabled
        finally:
            await client.stop()
    assert outcomes[True] == outcomes[False] == (
        decision, [("item/commandExecution/requestApproval", "pytest", "<ws>")], "on-request", {})
    # Without a host handler an approval is still refused, with or without NNC.
    for enabled in (False, True):
        client, workspace = make_client(tmp_path, enabled=enabled, name="unhandled-" + str(enabled))
        try:
            thread = await start(client, workspace)
            assert await client._approve("item/commandExecution/requestApproval",
                {"threadId": thread, "command": "pytest", "cwd": str(workspace)}) is False
        finally:
            await client.stop()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("sandbox", SANDBOXES)
async def test_nnc_does_not_reject_or_rewrite_assigned_dependency_roots(tmp_path, monkeypatch, enabled, sandbox):
    home = tmp_path / "home"
    custom_codex = tmp_path / "custom-codex-state"
    dependency = tmp_path / "public-dependency"
    for directory in (home / ".claude", home / ".codex", custom_codex, dependency):
        directory.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(custom_codex))
    client, workspace = make_client(tmp_path, enabled=enabled)
    link = workspace / "dependency-link"
    symlink_or_skip(link, dependency)
    nested = workspace / "nested-dependency"
    nested.mkdir()
    try:
        await client.start()
        roots = [str(client.state_dir), str(home), str(home / ".claude"), str(home / ".codex"), str(custom_codex),
                 str(link), str(nested), str(dependency)]
        thread = await start(client, workspace, "claude-code/claude-sonnet-4-6", sandbox=sandbox,
                             runtimeWorkspaceRoots=roots)
        # Stored exactly as supplied: no private-root rejection, no symlink pinning
        # or pivoting to the workspace, with NNC on or off.
        assert client._threads[thread]["runtimeWorkspaceRoots"] == roots
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        assert client._scope_for(thread, turn).readable_roots == tuple(Path(root) for root in roots)
        await client._emit({"method": "turn/completed", "params": {
            "threadId": thread, "turn": {"id": turn, "status": "completed"}}})
        # Re-asserting the assigned scope remains valid; a different scope remains rejected.
        await client.request("thread/resume", {"threadId": thread, "runtimeWorkspaceRoots": roots})
        with pytest.raises(AppServerError, match="cannot expand its assigned filesystem scope"):
            await client.request("thread/resume", {"threadId": thread, "runtimeWorkspaceRoots": [*roots, str(tmp_path)]})
    finally:
        await client.stop()


@pytest.mark.parametrize("enabled", [False, True])
async def test_nnc_does_not_pin_a_retargeted_dependency_symlink(tmp_path, enabled):
    client, workspace = make_client(tmp_path, enabled=enabled)
    first, second, link = tmp_path / "dependency-first", tmp_path / "dependency-second", tmp_path / "dependency-link"
    first.mkdir()
    second.mkdir()
    symlink_or_skip(link, first)
    policies = []

    def runner_factory(policy):
        policies.append(policy)
        raise Intercepted("runner intercepted")

    try:
        await client.start()
        client._host.runner_factory = runner_factory
        thread = await start(client, workspace, "claude-code/claude-sonnet-4-6", runtimeWorkspaceRoots=[str(link)])
        link.unlink()
        symlink_or_skip(link, second)
        await client.request("thread/resume", {"threadId": thread, "runtimeWorkspaceRoots": [str(link)]})
        turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
        await client._host.call({"threadId": thread, "turnId": turn, "callId": "run",
            "name": "exec_command", "arguments": {"command": "echo ordinary"}})
        # Ordinary host-scope semantics: the grant resolves where it points now
        # (the host's own helper script follows the assigned dependency roots).
        assert [policy.readable_roots[0] for policy in policies] == [second.resolve()]
    finally:
        await client.stop()


@pytest.mark.parametrize("sandbox", SANDBOXES)
async def test_nnc_keeps_runtime_off_network_and_never_approval_rules(tmp_path, sandbox):
    seen = {}
    for enabled in (False, True):
        client, workspace = make_client(tmp_path, enabled=enabled, name="on" if enabled else "off",
                                        runtime_enabled=False)
        try:
            response = await client.thread_start({"cwd": str(workspace), "runtimeWorkspaceRoots": [str(workspace)],
                "model": "gpt-6-astra", "sandbox": sandbox, "approvalPolicy": "on-request"})
            sent = next(params for method, params in client._engines["codex"].calls if method == "thread/start")
            seen[enabled] = (sent["networkAccess"], sent["approvalPolicy"], response["approvalPolicy"],
                             response["sandbox"].get("networkAccess"), client._threads[response["thread"]["id"]]["sandbox"])
        finally:
            await client.stop()
    assert seen[True] == seen[False] == (True, "never", "never", None if sandbox == "danger-full-access" else True, sandbox)


async def test_nnc_does_not_change_which_command_runs_get_escalation_prompts(tmp_path):
    prompts = {}
    for enabled in (False, True):
        client, workspace = make_client(tmp_path, enabled=enabled, name="on" if enabled else "off")
        client.server_request_handler = AsyncMock()
        try:
            await client.start()
            thread = await start(client, workspace, approvalPolicy="never")
            turn = (await client.turn_start({"threadId": thread}))["turn"]["id"]
            result = await client._host.call({"threadId": thread, "turnId": turn, "callId": "escape",
                "name": "exec_command", "arguments": {"command": "echo forbidden", "justification": "test",
                                                       "sandbox_permissions": "require_escalated"}})
            prompts[enabled] = (result["isError"], "denied" in result["content"][0]["text"].lower(),
                                client.server_request_handler.await_count,
                                client._scope_for(thread, turn).approval_policy)
        finally:
            await client.stop()
    # approvalPolicy=never never asks and never escalates; that is independent of NNC.
    assert prompts[True] == prompts[False] == (True, True, 0, "never")


# ---------------------------------------------------------------------------
# Native Codex backend: capability gate, config mapping and permission parity
# ---------------------------------------------------------------------------

class ContextNative(FakeNative):
    def __init__(self, *, context_supported, **options):
        super().__init__(**options)
        self.context_supported = context_supported

    async def request(self, method, params=None, timeout=30):
        result = await super().request(method, params, timeout)
        if method == "experimentalFeature/list" and self.context_supported:
            result["data"].append({"name": "bello_context_compaction"})
        return result


def make_native(tmp_path, monkeypatch, *, supported=True, windows=False):
    monkeypatch.setattr("supervisor.runtime.codex._IS_WINDOWS", windows)
    monkeypatch.setattr("supervisor.runtime.codex.native_toolchain_read_paths", lambda cwd: ())
    root, archive, source = tmp_path / "workspace", tmp_path / "archive", tmp_path / "source-home"
    for directory in (root, archive, source):
        directory.mkdir(mode=0o700)
    monkeypatch.setattr("supervisor.runtime.codex._codex_home_from_environment", lambda _: source)
    clients, events = [], []

    def factory(**options):
        options["persistent_isolated_home"].mkdir(mode=0o700, exist_ok=True)
        client = ContextNative(context_supported=supported, **options)
        clients.append(client)
        return client

    async def emit(raw):
        events.append(raw)

    backend = CodexBackend(state_dir=archive / "native", emit=emit, client_factory=factory,
                           private_read_roots=(archive, source))
    return backend, root, archive, clients, events


def without_compaction(native):
    """A native request with the five NNC-owned entries and the native id removed."""
    result = deepcopy(native)
    result.pop("threadId", None)
    for key in COMPACTION_KEYS:
        result["config"].pop(key, None)
    return result


MISSING = object()


def compaction_delta(on, off):
    return {key for key in set(on["config"]) | set(off["config"])
            if on["config"].get(key, MISSING) != off["config"].get(key, MISSING)}


@pytest.mark.parametrize("windows", [False, True])
@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("sandbox", SANDBOXES)
async def test_nnc_on_keeps_native_permission_profile_identical_to_off(tmp_path, monkeypatch, sandbox, network, windows):
    backend, root, archive, clients, _ = make_native(tmp_path, monkeypatch, windows=windows)
    dependency = tmp_path / "dependency"
    # Controller-private-looking directories are ordinary read grants, as before.
    roots = [str(dependency), str(archive), str(archive / "native"), str(tmp_path / "source-home")]
    try:
        for host, policy in (("off", OFF), ("on", POLICY)):
            await backend.request("thread/start", thread_params(root, host=host, sandbox=sandbox,
                networkAccess=network, runtimeWorkspaceRoots=roots, windowsNativeRootRead=windows,
                noNeedForContext=policy.to_backend_data()))
            await backend.request("thread/resume", {"threadId": host})
            await backend.request("turn/start", {"threadId": host, "turnId": "turn-" + host, "input": "fixture",
                "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": True}})
        calls = clients[0].calls
        starts = [params for method, params in calls if method == "thread/start"]
        resumes = [params for method, params in calls if method == "thread/resume"]
        turns = [params for method, params in calls if method == "turn/start"]
        assert len(starts) == len(resumes) == len(turns) == 2
        for off, on in ((starts[0], starts[1]), (resumes[0], resumes[1])):
            assert without_compaction(on) == without_compaction(off)
            assert compaction_delta(on, off) == COMPACTION_KEYS
            assert on["approvalPolicy"] == off["approvalPolicy"] == "on-request"
        # Turn requests inherit the thread profile; NNC adds nothing to them.
        off_turn, on_turn = turns
        assert {k: v for k, v in on_turn.items() if k != "threadId"} == {
            k: v for k, v in off_turn.items() if k != "threadId"}
        for params in (starts[1], resumes[1]):
            if sandbox == "danger-full-access":
                assert params["sandbox"] == sandbox and "permissions" not in params
                assert "default_permissions" not in params["config"]
                continue
            assert "sandbox" not in params and params["permissions"] == "bello-native"
            profile = params["config"]["permissions"]["bello-native"]
            assert profile["network"] == {"enabled": network}
            assert ("write" in profile["filesystem"].values()) == (sandbox == "workspace-write")
            assert params["runtimeWorkspaceRoots"] == [str(root), *roots]
            denied = {path for path, access in profile["filesystem"].items() if access == "deny"}
            # Only Windows root-read (consented separately from NNC) denies controller state.
            expected_denied = {str(archive), str(tmp_path / "source-home"), str(backend.state_dir),
                               str(backend.state_dir / "codex-home")} if windows else set()
            assert denied == expected_denied
    finally:
        await backend.stop()


async def test_nnc_on_adds_no_private_denial_or_scratch_to_a_non_windows_thread(tmp_path, monkeypatch):
    backend, root, archive, clients, _ = make_native(tmp_path, monkeypatch)
    snapshot_state = root / ".supervisor"
    snapshot_state.mkdir()
    try:
        await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data()))
        await backend.request("thread/resume", {"threadId": "host-thread"})
        for method, params in clients[0].calls:
            if method not in {"thread/start", "thread/resume"}:
                continue
            filesystem = params["config"]["permissions"]["bello-native"]["filesystem"]
            assert "deny" not in filesystem.values()
            assert not {str(archive), str(backend.state_dir), str(snapshot_state)} & set(filesystem)
            assert "shell_environment_policy" not in params["config"]
        assert not backend._windows_scratch
        assert not any(path.name.startswith("bello-native-") for path in root.rglob("*"))
    finally:
        await backend.stop()


@pytest.mark.parametrize("network", [False, True])
async def test_nnc_on_accepts_full_access_native_threads(tmp_path, monkeypatch, network):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch)
    legacy = {"sandbox_workspace_write.network_access": network}
    try:
        await backend.request("thread/start", thread_params(root, sandbox="danger-full-access", networkAccess=network,
            noNeedForContext=POLICY.to_backend_data(), config=legacy))
        await backend.request("thread/resume", {"threadId": "host-thread"})
        await backend.request("turn/start", {"threadId": "host-thread", "turnId": "turn", "input": "fixture"})
        for method, params in clients[0].calls:
            if method in {"thread/start", "thread/resume"}:
                assert params["sandbox"] == "danger-full-access" and "permissions" not in params
                assert params["config"]["sandbox_workspace_write.network_access"] is network
                assert params["config"]["features.bello_context_compaction"] is True
        assert any(method == "turn/start" for method, _ in clients[0].calls)
    finally:
        await backend.stop()


@pytest.mark.parametrize("policy_name", ["on-request", "never"])
@pytest.mark.parametrize("method", APPROVALS)
async def test_nnc_on_native_approval_requests_reach_the_host_exactly_like_off(tmp_path, monkeypatch, method, policy_name):
    observed = {}
    for enabled in (False, True):
        base = tmp_path / ("on" if enabled else "off")
        base.mkdir()
        backend, root, _, clients, events = make_native(base, monkeypatch)
        try:
            await backend.request("thread/start", thread_params(root, approvalPolicy=policy_name,
                noNeedForContext=(POLICY if enabled else OFF).to_backend_data()))
            await backend.request("turn/start", {"threadId": "host-thread", "turnId": "host-turn", "input": "a"})
            await clients[0].notify_event(method, {"threadId": "native-thread-1", "turnId": "native-turn-1",
                                                   "command": "pytest"}, request_id=401)
            await drain(backend)
            forwarded = [(raw["method"], raw["params"]["threadId"]) for raw in events if raw.get("id")]
            if forwarded:
                request_id = next(raw["id"] for raw in events if raw.get("id"))
                assert request_id.startswith("codex:")
                decision = {"permissions": {}} if "permissions" in method else {"decision": "accept"}
                assert await backend.respond(request_id, decision) is True
            observed[enabled] = (forwarded, clients[0].responses,
                                 backend._threads["host-thread"]["params"]["approvalPolicy"])
        finally:
            await backend.stop()
    assert observed[True] == observed[False]
    forwarded, responses, recorded = observed[True]
    assert recorded == policy_name
    if policy_name == "on-request":
        # Not auto-declined: the request reaches the host and its answer is relayed.
        assert forwarded == [(method, "host-thread")]
        decision = {"permissions": {}} if "permissions" in method else {"decision": "accept"}
        assert responses == [(401, decision, None)]
    else:
        # The pre-existing approvalPolicy=never rule declines; NNC is not involved.
        assert forwarded == []
        assert responses == [(401, {"permissions": {}} if "permissions" in method else {"decision": "decline"}, None)]


@pytest.mark.parametrize("async_only", [False, True])
async def test_native_missing_capability_fails_before_creating_thread_or_turn(tmp_path, monkeypatch, async_only):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch, supported=False)
    try:
        await backend.request("initialize")
        clients[0].async_tools_supported = async_only
        with pytest.raises(AppServerError, match="bello_context_compaction") as raised:
            await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data(),
                                                                asyncTools=async_only))
        message = str(raised.value)
        assert "No engine or API fallback was used" in message and "no model turn was started" in message
        assert "--no-no-need-for-context" in message and "BELLO_CODEX_BINARY" in message
        assert not any(method in {"thread/start", "turn/start"} for method, _ in clients[0].calls)
        # The same helper still serves a run that has NNC off.
        await backend.request("thread/start", thread_params(root, host="off", asyncTools=async_only))
        assert sum(method == "thread/start" for method, _ in clients[0].calls) == 1
    finally:
        await backend.stop()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("role", [None, "coder", "completion_review", "runtime"])
async def test_model_validation_carries_only_the_run_owned_policy(tmp_path, enabled, role):
    client, _ = make_client(tmp_path, enabled=enabled)
    try:
        params = {"model": "gpt-6-astra", "effort": "high", **({"belloRole": role} if role else {})}
        await client.request("model/validate", {**params, "noNeedForContext": {"enabled": not enabled}})
        sent = next(p for m, p in client._engines["codex"].calls if m == "model/validate")
        expected = client.no_need_for_context.to_backend_data()
        expected["enabled"] = enabled and role != "runtime"
        assert sent["noNeedForContext"] == expected
    finally:
        await client.stop()


@pytest.mark.parametrize("role,expected", [(None, "fails"), ("coder", "fails"), ("runtime", "valid")])
async def test_preflight_fails_closed_for_a_native_helper_without_the_capability(tmp_path, monkeypatch, role, expected):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch, supported=False)
    project = tmp_path / "project"
    project.mkdir()
    client = RuntimeClient(cwd=project, backends={"codex": backend})
    client.configure_run(no_need_for_context=POLICY, async_tools=False)
    params = {"model": "gpt-6-astra", "effort": "high", **({"belloRole": role} if role else {})}
    try:
        if expected == "fails":
            with pytest.raises(AppServerError, match="(?i)bello_context_compaction.*no engine or API fallback"):
                await client.request("model/validate", params)
        else:
            # Runtime supervision never compacts, so it needs no compaction helper.
            assert (await client.request("model/validate", params))["valid"] is True
        assert not any(method in {"thread/start", "turn/start"} for method, _ in clients[0].calls)
    finally:
        await client.stop()


@pytest.mark.parametrize("windows", [False, True])
async def test_client_fails_closed_before_any_model_turn_when_native_helper_lacks_the_capability(tmp_path, monkeypatch, windows):
    monkeypatch.setattr("supervisor.runtime.client._IS_WINDOWS", windows)
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch, supported=False, windows=windows)
    project = tmp_path / "project"
    project.mkdir()
    client = RuntimeClient(cwd=project, backends={"codex": backend})
    # Reach capability discovery with an explicitly supported Windows scope.
    # The real permission validator stays enabled; consent is host-owned.
    client.configure_run(no_need_for_context=POLICY, windows_native_root_read=True)
    try:
        with pytest.raises(AppServerError, match="(?i)bello_context_compaction.*no engine or API fallback"):
            await client.thread_start({"cwd": str(root), "runtimeWorkspaceRoots": [str(root)], "model": "gpt-6-astra",
                "sandbox": "workspace-write", "approvalPolicy": "on-request"})
        assert len(clients) == 1
        assert any(method == "experimentalFeature/list" for method, _ in clients[0].calls)
        assert not any(method in {"thread/start", "turn/start"} for method, _ in clients[0].calls)
        assert len(client._threads) == 1
        record = next(iter(client._threads.values()))
        assert record["closed"] and record["noNeedForContext"] == POLICY.to_backend_data()
        assert record["sandbox"] == "workspace-write" and record["approvalPolicy"] == "on-request"
        assert record["runtimeWorkspaceRoots"] == [str(root)]
        assert record.get("windowsNativeRootRead", False) is windows
    finally:
        await client.stop()


async def test_saved_native_policy_rechecks_capability_on_a_replacement_helper(tmp_path, monkeypatch):
    backend, root, _, _, _ = make_native(tmp_path, monkeypatch)
    await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data()))
    await backend.stop()
    old_clients = []

    def old_helper(**options):
        client = ContextNative(context_supported=False, **options)
        client.async_tools_supported = True
        old_clients.append(client)
        return client

    resumed = CodexBackend(state_dir=backend.state_dir, emit=AsyncMock(), client_factory=old_helper)
    try:
        for method, extra in (("thread/resume", {}), ("turn/start", {"turnId": "turn", "input": "fixture"})):
            with pytest.raises(AppServerError, match="bello_context_compaction"):
                await resumed.request(method, {"threadId": "host-thread", **extra})
        assert not any(method in {"thread/start", "thread/resume", "turn/start"}
                       for client in old_clients for method, _ in client.calls)
    finally:
        await resumed.stop()


@pytest.mark.parametrize("windows", [False, True])
async def test_native_forwards_policy_through_start_and_resume(tmp_path, monkeypatch, windows):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch, windows=windows)
    try:
        await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data(),
                                                            windowsNativeRootRead=windows))
        await backend.request("thread/resume", {"threadId": "host-thread"})
        for method, native in clients[0].calls:
            if method not in {"thread/start", "thread/resume"}:
                continue
            config = native["config"]
            assert config["features.bello_context_compaction"] is True
            assert config["model_auto_compact_token_limit"] == 120000
            assert config["model_auto_compact_token_limit_scope"] == "total"
            assert config["model_post_turn_compact_threshold_percent"] == 0
            assert config["bello_context_retain_percent"] == 12.5
            assert "noNeedForContext" not in native
            assert native["approvalPolicy"] == "on-request"
    finally:
        await backend.stop()


@pytest.mark.parametrize("threshold", [None, 249000])
async def test_native_sparse_policy_default_and_explicit_legacy_threshold(tmp_path, monkeypatch, threshold):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch)
    policy = {"enabled": True}
    if threshold is not None:
        policy["thresholdTokens"] = threshold
    try:
        await backend.request("thread/start", thread_params(root, noNeedForContext=policy))
        await backend.request("thread/resume", {"threadId": "host-thread"})
        for method, native in clients[0].calls:
            if method in {"thread/start", "thread/resume"}:
                assert native["config"]["model_auto_compact_token_limit"] == (threshold or 200000)
                assert native["config"]["bello_context_retain_percent"] == 10
    finally:
        await backend.stop()


async def test_native_on_policy_overrides_untrusted_context_config(tmp_path, monkeypatch):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch)
    try:
        await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data(), config={
            "features": {"bello_context_compaction": False}, "features.bello_context_compaction": False,
            "bello_context_retain_percent": 50, "model_auto_compact_token_limit": 5,
            "model_post_turn_compact_threshold_percent": 90}))
        config = next(params for method, params in clients[0].calls if method == "thread/start")["config"]
        assert config["features.bello_context_compaction"] is True
        assert "bello_context_compaction" not in config.get("features", {})
        assert config["bello_context_retain_percent"] == 12.5
        assert config["model_auto_compact_token_limit"] == 120000
        assert config["model_post_turn_compact_threshold_percent"] == 0
    finally:
        await backend.stop()


@pytest.mark.parametrize("method", ["thread/resume", "turn/start"])
async def test_native_rejects_policy_mutation_even_without_host_gate(tmp_path, monkeypatch, method):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch)
    try:
        await backend.request("thread/start", thread_params(root, noNeedForContext=POLICY.to_backend_data()))
        before = len(clients[0].calls)
        with pytest.raises(AppServerError, match="cannot change context compaction policy"):
            await backend.request(method, {"threadId": "host-thread", "turnId": "turn",
                "noNeedForContext": NoNeedForContextConfig().to_backend_data()})
        assert len(clients[0].calls) == before
    finally:
        await backend.stop()


@pytest.mark.parametrize("supported", [False, True])
async def test_native_off_has_no_compaction_side_effects_and_masks_feature_injection(tmp_path, monkeypatch, supported):
    backend, root, _, clients, _ = make_native(tmp_path, monkeypatch, supported=supported)
    try:
        params = thread_params(root, noNeedForContext=OFF.to_backend_data(), config={
            "features": {"bello_context_compaction": True}, "features.bello_context_compaction": True,
            "bello_context_retain_percent": 50})
        await backend.request("thread/start", params)
        config = next(params for method, params in clients[0].calls if method == "thread/start")["config"]
        assert config.get("features.bello_context_compaction", False) is False
        assert "bello_context_compaction" not in config["features"]
        for key in ("model_auto_compact_token_limit", "model_auto_compact_token_limit_scope",
                    "model_post_turn_compact_threshold_percent", "bello_context_retain_percent"):
            assert key not in config
        assert not backend._windows_scratch
        assert not any(path.name.startswith("bello-native-") for path in root.rglob("*"))
    finally:
        await backend.stop()
