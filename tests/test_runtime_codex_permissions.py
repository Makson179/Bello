from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from supervisor.appserver import AppServerError
from supervisor.runtime.codex_permissions import PROFILE_ID, native_permission_params


def profile(result):
    return result["config"]["permissions"][PROFILE_ID]


@pytest.mark.parametrize("network", [False, True])
def test_workspace_scope_uses_native_profile_without_full_disk_read(tmp_path, network):
    workspace, deps = tmp_path / "worktree", tmp_path / "deps"
    source = {"cwd": str(workspace), "sandbox": "workspace-write",
              "runtimeWorkspaceRoots": [str(deps)], "networkAccess": network}
    before = deepcopy(source)
    result = native_permission_params(source)
    rules = profile(result)
    assert result["permissions"] == PROFILE_ID
    assert result["runtimeWorkspaceRoots"] == [str(workspace), str(deps)]
    assert rules["network"] == {"enabled": network}
    assert rules["filesystem"] == {
        ":minimal": "read", ":workspace_roots": "read", str(workspace): "write",
        **{str(workspace / name): "read" for name in (".git", ".agents", ".codex")},
    }
    assert "sandbox" not in result and "extends" not in rules
    assert ":root" not in rules["filesystem"]
    assert source == before
    assert json.loads(json.dumps(result)) == result


def test_only_explicit_backend_scratch_is_writable(tmp_path):
    workspace = tmp_path / "worktree"
    scratch = tmp_path / "native-state" / "tool-tmp"
    result = native_permission_params({"cwd": str(workspace)}, temp_dir=scratch)
    filesystem = profile(result)["filesystem"]
    assert {path for path, mode in filesystem.items() if mode == "write"} == {
        str(workspace), str(scratch),
    }
    for broad in ("/tmp", "/private/tmp", str(Path.home()), str(scratch.parent)):
        assert broad not in filesystem
    assert ":tmpdir" not in filesystem and ":slash_tmp" not in filesystem
    assert not scratch.exists()  # The helper never creates anything.


def test_readonly_profile_does_not_grant_any_writes(tmp_path):
    result = native_permission_params(
        {"cwd": str(tmp_path / "worktree"), "sandbox": "read-only"},
        temp_dir=tmp_path / "native-state" / "tool-tmp",
    )
    assert profile(result)["filesystem"] == {
        ":minimal": "read", ":workspace_roots": "read",
    }


def test_explicit_full_access_keeps_legacy_and_does_not_add_a_profile():
    assert native_permission_params({"sandbox": "danger-full-access"}) == {
        "sandbox": "danger-full-access",
    }


@pytest.mark.parametrize("roots", [None, []])
def test_default_read_scope_is_cwd(tmp_path, roots):
    result = native_permission_params({"cwd": str(tmp_path), "runtimeWorkspaceRoots": roots})
    assert result["runtimeWorkspaceRoots"] == [str(tmp_path)]


def test_explicit_file_roots_and_duplicate_roots_are_preserved(tmp_path):
    workspace, task = tmp_path / "worktree", tmp_path / "TASK.md"
    result = native_permission_params({"cwd": str(workspace), "runtimeWorkspaceRoots": [
        str(task), str(workspace), str(task),
    ]})
    assert result["runtimeWorkspaceRoots"] == [str(workspace), str(task)]
    assert str(task) not in profile(result)["filesystem"]  # Read, never write.


@pytest.mark.parametrize("overrides", [
    {"sandbox": "external-sandbox"}, {"sandbox": []}, {"cwd": "relative"}, {"cwd": None},
    {"cwd": "/tmp/bad\x00path"}, {"runtimeWorkspaceRoots": "/tmp"},
    {"runtimeWorkspaceRoots": ["relative"]}, {"runtimeWorkspaceRoots": [None]},
    {"networkAccess": "false"},
])
def test_invalid_scope_is_not_silently_weakened(tmp_path, overrides):
    with pytest.raises(AppServerError):
        native_permission_params({"cwd": str(tmp_path), **overrides})


def test_relative_scratch_is_rejected(tmp_path):
    with pytest.raises(AppServerError):
        native_permission_params({"cwd": str(tmp_path)}, temp_dir=Path("relative"))


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
def test_exact_host_runtime_file_is_readable_without_opening_parent(tmp_path, mode):
    executable = tmp_path / "private-install" / "bin" / "codex"
    result = native_permission_params({"cwd": str(tmp_path / "workspace"), "sandbox": mode},
        runtime_read_paths=(executable,))
    filesystem = profile(result)["filesystem"]
    assert filesystem[str(executable)] == "read"
    assert all(str(parent) not in filesystem for parent in executable.parents)
    assert str(executable) not in result["runtimeWorkspaceRoots"]


def test_mapping_is_stable_for_resume_and_does_not_inherit_external_config(tmp_path):
    source = {"cwd": str(tmp_path), "config": {
        "default_permissions": ":danger-full-access",
        "permissions": {PROFILE_ID: {"extends": ":workspace"}},
    }}
    start = native_permission_params(source)
    resumed = native_permission_params(deepcopy(source))
    assert start == resumed
    assert "extends" not in profile(start)
    assert "default_permissions" not in start["config"]
