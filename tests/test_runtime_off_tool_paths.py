"""Offline runtime-off path checks: project names do not grant host authority."""

import json

import pytest

from supervisor.appserver import AppServerError
from supervisor.policy import PolicyEngine
from supervisor.runtime.tools import ToolHost, ToolScope
from supervisor.schemas import PolicyDecisionKind
from tests.test_runtime_client import start
from tests.test_runtime_feature_switches import make_client


@pytest.mark.parametrize("name", [
    "tokenizer.py", "t/err_unexpected_token.json", ".env.example", "fixtures/test.pem",
    "fixtures/.ssh/id_rsa", "fixtures/.aws/credentials", ".config/gh", ".docker/config.json",
    "private_fixture.txt",
])
@pytest.mark.parametrize("writing", [False, True])
def test_runtime_off_allows_project_fixture_names_only(tmp_path, name, writing):
    root = tmp_path / "workspace"
    root.mkdir()
    host = object.__new__(ToolHost)
    scope = ToolScope(root, "workspace-write", runtime_enabled=False)
    assert host._path(scope, name, writing=writing) == root / name
    with pytest.raises(PermissionError, match="secret material"):
        host._path(ToolScope(root, "workspace-write"), name, writing=writing)


@pytest.mark.parametrize("name", [".git/config", ".supervisor/state.json", ".codex/bello-run/launch.json"])
@pytest.mark.parametrize("writing", [False, True])
def test_runtime_off_keeps_reserved_controller_authority(tmp_path, name, writing):
    host = object.__new__(ToolHost)
    with pytest.raises(PermissionError, match="secret material"):
        host._path(ToolScope(tmp_path, "workspace-write", runtime_enabled=False), name, writing=writing)


@pytest.mark.parametrize("control,alias", [
    (".git", ".GIT"), (".supervisor", ".SUPERVISOR"),
    (".codex/bello-run", ".CODEX/BELLO-RUN"),
    (".codex/bello-run", ".CoDeX/BeLLo-RuN"),
])
@pytest.mark.parametrize("existing", [False, True])
def test_runtime_off_control_aliases_follow_real_filesystem_identity(tmp_path, control, alias, existing):
    boundary = tmp_path / control
    boundary.mkdir(parents=True)
    if existing:
        (boundary / "state.json").write_text("synthetic control fixture")
    alternate = tmp_path / alias
    if not alternate.exists():
        pytest.skip("temporary filesystem is case-sensitive")
    assert alternate.samefile(boundary)
    host = object.__new__(ToolHost)
    scope = ToolScope(tmp_path, "workspace-write", runtime_enabled=False)
    for writing in (False, True):
        with pytest.raises(PermissionError, match="secret material"):
            host._path(scope, f"{alias}/state.json", writing=writing)
    decision = PolicyEngine(tmp_path).evaluate_patch_paths([f"{alias}/state.json"], check_path_heuristics=False)
    assert decision.kind == PolicyDecisionKind.DENY


def test_runtime_off_keeps_distinct_names_on_case_sensitive_filesystems(tmp_path):
    (tmp_path / ".git").mkdir()
    alternate = tmp_path / ".GIT"
    if alternate.exists():
        pytest.skip("temporary filesystem is case-insensitive")
    alternate.mkdir()
    host = object.__new__(ToolHost)
    scope = ToolScope(tmp_path, "workspace-write", runtime_enabled=False)
    assert host._path(scope, ".GIT/fixture.txt", writing=True) == alternate / "fixture.txt"
    decision = PolicyEngine(tmp_path).evaluate_patch_paths([".GIT/fixture.txt"], check_path_heuristics=False)
    assert decision.kind == PolicyDecisionKind.ALLOW


@pytest.mark.parametrize("immutable", [False, True])
def test_runtime_off_keeps_case_aliased_explicit_authorities(tmp_path, immutable):
    boundary = tmp_path / "assessment"
    boundary.mkdir()
    if not (tmp_path / "ASSESSMENT").exists():
        pytest.skip("temporary filesystem is case-sensitive")
    assert (tmp_path / "ASSESSMENT").samefile(boundary)
    options = {"immutable_paths" if immutable else "declared_grading_roots": (boundary,)}
    decision = PolicyEngine(tmp_path, **options).evaluate_patch_paths(
        ["ASSESSMENT/token.json"], check_path_heuristics=False,
    )
    assert decision.kind == PolicyDecisionKind.DENY


def test_runtime_off_keeps_outside_and_readonly_authorities(tmp_path):
    root, dependency, private = (tmp_path / name for name in ("workspace", "dependency", "auth"))
    for directory in (root, dependency, private):
        directory.mkdir()
    host = object.__new__(ToolHost)
    scope = ToolScope(root, "workspace-write", readable_roots=(dependency,), runtime_enabled=False)
    assert host._path(scope, str(dependency / "module.py")) == dependency / "module.py"
    for path, writing in ((dependency / "module.py", True), (private / "auth.json", False),
                          (root / ".." / "auth" / "auth.json", True)):
        with pytest.raises(PermissionError, match="outside"):
            host._path(scope, str(path), writing=writing)
    for name in ("tokenizer.py", "test.pem", ".ssh/id_rsa"):
        with pytest.raises(PermissionError, match="secret material"):
            host._path(scope, str(dependency / name))
    with pytest.raises(PermissionError, match="read-only"):
        host._path(ToolScope(root, "read-only", runtime_enabled=False), "tokenizer.py", writing=True)


def test_runtime_off_resolves_links_before_authorizing_paths(tmp_path):
    root, outside = tmp_path / "workspace", tmp_path / "auth"
    root.mkdir()
    outside.mkdir()
    (root / "fixture").symlink_to(outside, target_is_directory=True)
    (root / ".supervisor").mkdir()
    (root / "state_alias").symlink_to(root / ".supervisor", target_is_directory=True)
    host = object.__new__(ToolHost)
    scope = ToolScope(root, "workspace-write", runtime_enabled=False)
    for writing in (False, True):
        with pytest.raises(PermissionError, match="outside"):
            host._path(scope, "fixture/auth.json", writing=writing)
        with pytest.raises(PermissionError, match="secret material"):
            host._path(scope, "state_alias/state.json", writing=writing)


@pytest.mark.parametrize("relative", ["tokenizer.py", ".env", "fixtures/certificate.pem"])
def test_runtime_off_keeps_explicit_immutable_patch_authority(tmp_path, relative):
    policy = PolicyEngine(tmp_path, immutable_paths=(relative,))
    decision = policy.evaluate_patch_paths([relative], check_path_heuristics=False)
    assert decision.kind == PolicyDecisionKind.DENY
    assert "immutable path" in decision.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True])
async def test_client_host_owns_path_policy_for_new_resumed_and_child_threads(tmp_path, runtime):
    client, root, pi, claude = make_client(tmp_path, runtime=runtime)
    try:
        parent = await start(client, root, belloRole="coder", runtime_enabled=not runtime,
                             config={"agents": {"enabled": True, "role": "coder",
                                 "max_concurrent_threads_per_session": 2,
                                 "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"]}}})
        turn = (await client.turn_start({"threadId": parent}))["turn"]["id"]

        def check(thread, active_turn):
            scope = client._scope_for(thread, active_turn)
            assert scope.runtime_enabled is runtime
            assert scope.network_access is (not runtime)
            if runtime:
                with pytest.raises(PermissionError, match="secret material"):
                    client._host._path(scope, "tokenizer.py", writing=True)
            else:
                assert client._host._path(scope, "tokenizer.py", writing=True) == root / "tokenizer.py"

        check(parent, turn)
        reply = await client._delegate("spawn_agent", {
            "model": "claude-code/claude-sonnet-4-6", "effort": "high", "message": "inspect fixtures",
        }, parent, turn)
        child = json.loads(reply["content"][0]["text"])["agent_id"]
        check(child, client._threads[child]["activeTurnId"])
        await client._emit({"method": "turn/completed", "params": {
            "threadId": parent, "turn": {"id": turn, "status": "completed"},
        }})
        await client.stop()
        await client.start()
        client._engines.update({"codex": pi, "claude-code": claude})
        await client.request("thread/resume", {"threadId": parent, "runtime_enabled": not runtime})
        resumed = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        check(parent, resumed)
        with pytest.raises(AppServerError, match="stopping the runtime"):
            client.configure_run(runtime_enabled=not runtime)
    finally:
        await client.stop()
