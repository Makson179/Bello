from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.runtime import codex_toolchains as tools
from supervisor.runtime.codex import CodexBackend
from supervisor.runtime.codex_permissions import PROFILE_ID, native_permission_params


@pytest.fixture
def host(tmp_path, monkeypatch):
    home, work = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    work.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(tools, "_current_python_paths", lambda: ())
    monkeypatch.setattr(tools, "_mac_developer_directory", lambda: None)
    monkeypatch.setattr(tools.sandbox, "_TOOLCHAIN_COMMANDS", ("python3", "git"))
    monkeypatch.setattr(tools.sandbox, "_runtime_root", lambda: None)
    monkeypatch.setattr(tools.sandbox, "_windows_current_python_root", lambda policy: None)
    monkeypatch.setattr(tools.sandbox, "_mac_linked_kegs", lambda path: ())
    monkeypatch.setattr(tools.sandbox, "_mac_public_ssl_files", lambda: ())
    monkeypatch.setattr(tools.sandbox, "_mac_developer_selector_paths", lambda: ())
    monkeypatch.setenv("PATH", "")
    return home, work


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_native_public_ssl_and_apple_dispatcher_paths_are_readonly_exact(host, monkeypatch):
    home, work = host
    config = home / "public-system" / "openssl.cnf"
    config.parent.mkdir()
    config.write_text("openssl_conf = openssl_init\n")
    developer = home / "public-apple-sdk"
    developer.mkdir()
    selector = home / "system-selector"
    selector.write_text("placeholder")
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools.sandbox, "_mac_public_ssl_files", lambda: (config,))
    monkeypatch.setattr(tools.sandbox, "_mac_developer_selector_paths", lambda: (selector, developer))
    result = tools.native_toolchain_read_paths(work)
    assert set(result) == {config, selector, developer}
    permission = native_permission_params({"cwd": str(work)}, runtime_read_paths=result)
    fs = permission["config"]["permissions"][PROFILE_ID]["filesystem"]
    assert all(fs[str(path)] == "read" for path in result)
    assert str(config.parent) not in fs and str(selector.parent) not in fs


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable alias; Windows rejects reparse leaves")
def test_narrow_runtime_and_exact_alias_not_path_parent(host, monkeypatch):
    home, work = host
    runtime = home / ".pyenv" / "versions" / "3.15.0"
    binary = executable(runtime / "bin" / "python3")
    aliases = home / ".local" / "bin"
    aliases.mkdir(parents=True)
    alias = aliases / "python3"
    alias.symlink_to(binary)
    monkeypatch.setenv("PATH", str(aliases))
    result = tools.native_toolchain_read_paths(work)
    assert set(result) == {runtime, binary, alias}
    rules = native_permission_params({"cwd": str(work)}, runtime_read_paths=result)
    filesystem = rules["config"]["permissions"][PROFILE_ID]["filesystem"]
    assert all(filesystem[str(path)] == "read" for path in result)
    assert str(home) not in filesystem and str(aliases) not in filesystem
    assert str(home / ".local") not in filesystem


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH/symlink test; Windows resolver rejects reparse leaves")
def test_workspace_path_and_symlink_target_cannot_add_authority(host, monkeypatch):
    home, work = host
    fake = executable(work / "bin" / "python3")
    outside = home / "outside-bin"
    outside.mkdir()
    (outside / "git").symlink_to(fake)
    alias_dir = home / "linked-bin"
    alias_dir.symlink_to(fake.parent)
    monkeypatch.setenv("PATH", os.pathsep.join((".", "bin", str(fake.parent), str(alias_dir), str(outside))))
    assert tools.native_toolchain_read_paths(work) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX alias test; Windows resolver rejects reparse leaves")
def test_no_broad_containers_or_private_paths_even_through_alias(host, monkeypatch):
    home, work = host
    trusted = home / "python-runtime"
    trusted.mkdir()
    cache_alias = home / ".cache"
    cache_alias.symlink_to(trusted, target_is_directory=True)
    workspace_alias = work / ".git"
    workspace_alias.symlink_to(trusted, target_is_directory=True)
    secret = executable(home / ".codex" / "auth.json")
    bin_dir = home / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(secret)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(tools, "_current_python_paths", lambda: (
        home, home.parent, cache_alias, workspace_alias, secret,
    ))
    assert tools.native_toolchain_read_paths(work) == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX workspace alias; Windows rejects reparse leaves")
def test_workspace_symlink_alias_does_not_grant_controlled_executable(host, monkeypatch):
    home, work = host
    trusted = executable(home / "python-runtime" / "bin" / "python3")
    (work / "bin").mkdir()
    (work / "bin" / "python3").symlink_to(trusted)
    alias = home / "workspace-alias"
    alias.symlink_to(work, target_is_directory=True)
    monkeypatch.setattr(tools, "_current_python_paths", lambda: (alias / "bin" / "python3",))
    assert tools.native_toolchain_read_paths(alias) == ()


@pytest.mark.parametrize("workspace", [Path("relative"), Path("/missing-bello-workspace")])
def test_invalid_workspace_does_not_discover(workspace, monkeypatch):
    def unexpected(policy):
        pytest.fail("must not discover tools for an invalid workspace")
    monkeypatch.setattr(tools.sandbox, "_discover_toolchain", unexpected)
    assert tools.native_toolchain_read_paths(workspace) == ()


def test_current_venv_keeps_base_and_lexical_interpreter(tmp_path, monkeypatch):
    base, venv = tmp_path / "python", tmp_path / "venv"
    binary = executable(base / "bin" / "python3")
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = ../python/bin\n")
    alias = venv / "bin" / "python"
    executable(alias)  # Also covers a Windows venv's ordinary redirector file.
    monkeypatch.setattr(tools.sys, "prefix", str(venv))
    monkeypatch.setattr(tools.sys, "base_prefix", str(base))
    monkeypatch.setattr(tools.sys, "executable", str(alias))
    assert tools._current_python_paths() == (alias, base, venv)


def test_windows_unknown_tool_does_not_grant_generic_path_directory(host, monkeypatch):
    home, work = host
    binary = executable(home / "tools" / "git.exe")
    version = home / ".pyenv" / "versions" / "3.15.0"
    python = executable(version / "python.exe")
    monkeypatch.setattr(tools, "_IS_WINDOWS", True)
    monkeypatch.setattr(tools.sandbox, "_discover_toolchain", lambda policy: tools.sandbox._Toolchain(
        (("git", binary), ("python", python)), (binary.parent, version),
    ))
    result = tools.native_toolchain_read_paths(work)
    assert binary in result and python in result and version in result
    assert binary.parent not in result


@pytest.mark.parametrize("selection", ["clt", "xcode", "private", "relative", "failed"])
def test_apple_developer_selection_is_bounded(tmp_path, monkeypatch, selection):
    clt = tmp_path / "Library" / "Developer" / "CommandLineTools"
    applications = tmp_path / "Applications"
    xcode = applications / "Xcode-beta.app" / "Contents" / "Developer"
    private = tmp_path / "unrelated-private-directory"
    for path in (clt, xcode, private):
        path.mkdir(parents=True)
    selector = executable(tmp_path / "usr" / "bin" / "xcode-select")
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools, "_XCODE_SELECT", selector)
    monkeypatch.setattr(tools, "_COMMAND_LINE_TOOLS", clt)
    monkeypatch.setattr(tools, "_APPLICATIONS", applications)
    selected = {"clt": clt, "xcode": xcode, "private": private,
                "relative": Path("relative"), "failed": clt}[selection]
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=int(selection == "failed"), stdout=str(selected) + "\n")
    monkeypatch.setattr(tools.subprocess, "run", run)
    assert tools._mac_developer_directory() == (selected if selection in {"clt", "xcode"} else None)
    assert calls[0][0] == [str(selector), "-p"]
    assert calls[0][1]["env"]["PATH"] == "/usr/bin:/bin"


def test_non_macos_never_queries_xcode(monkeypatch):
    monkeypatch.setattr(tools, "_IS_MACOS", False)
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: pytest.fail("not macOS"))
    assert tools._mac_developer_directory() is None


def test_backend_discovers_once_per_cwd_and_skips_full_access(tmp_path, monkeypatch):
    calls = []
    dependency = tmp_path / "runtime"
    def discover(path):
        calls.append(path)
        return (dependency,)
    monkeypatch.setattr("supervisor.runtime.codex.native_toolchain_read_paths", discover)
    backend = CodexBackend(state_dir=tmp_path / "state", emit=lambda raw: None)
    params = {"cwd": str(tmp_path / "work"), "model": "gpt-6-astra"}
    first = backend._thread_params(params)
    assert backend._thread_params(params) == first
    filesystem = first["config"]["permissions"][PROFILE_ID]["filesystem"]
    assert filesystem[str(dependency)] == "read"
    backend._thread_params({**params, "sandbox": "read-only"})
    assert calls == [Path(params["cwd"])]
    backend._thread_params({**params, "cwd": str(tmp_path / "other")})
    assert len(calls) == 2
    backend._thread_params({**params, "cwd": str(tmp_path / "full"), "sandbox": "danger-full-access"})
    assert len(calls) == 2
