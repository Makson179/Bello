from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
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
    monkeypatch.setattr(tools, "_mac_cryptex_alias_directory", lambda: None)
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


@pytest.mark.parametrize(("mode", "uid", "accepted"), [
    (stat.S_IFDIR | 0o755, 0, True),
    (stat.S_IFDIR | 0o555, 0, True),
    (stat.S_IFDIR | 0o755, 501, False),
    (stat.S_IFDIR | 0o775, 0, False),
    (stat.S_IFDIR | 0o757, 0, False),
    (stat.S_IFLNK | 0o755, 0, False),
    (stat.S_IFREG | 0o755, 0, False),
], ids=["root-owned", "readonly", "user-owned", "group-writable", "other-writable", "symlink", "file"])
def test_mac_cryptex_directory_requires_safe_system_metadata(tmp_path, monkeypatch, mode, uid, accepted):
    directory = tmp_path / "System" / "Cryptexes"
    directory.mkdir(parents=True)
    original_lstat = Path.lstat
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools, "_MAC_CRYPTEX_ALIASES", directory)
    monkeypatch.setattr(Path, "lstat", lambda self, *a, **kw:
                        SimpleNamespace(st_mode=mode, st_uid=uid) if self == directory
                        else original_lstat(self, *a, **kw))
    assert tools._mac_cryptex_alias_directory() == (directory if accepted else None)


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError(), OSError("unavailable")])
def test_mac_cryptex_missing_or_uninspectable_is_not_granted(monkeypatch, error):
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    def unavailable(self):
        raise error
    monkeypatch.setattr(Path, "lstat", unavailable)
    assert tools._mac_cryptex_alias_directory() is None


def test_non_macos_does_not_inspect_cryptex(monkeypatch):
    monkeypatch.setattr(tools, "_IS_MACOS", False)
    monkeypatch.setattr(Path, "lstat", lambda self: pytest.fail("must not inspect macOS paths"))
    assert tools._mac_cryptex_alias_directory() is None


def test_mac_cryptex_rejects_redirected_ancestor(tmp_path, monkeypatch):
    directory = tmp_path / "System" / "Cryptexes"
    directory.mkdir(parents=True)
    original_lstat, original_resolve = Path.lstat, Path.resolve
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools, "_MAC_CRYPTEX_ALIASES", directory)
    monkeypatch.setattr(Path, "lstat", lambda self, *a, **kw:
                        SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
                        if self == directory else original_lstat(self, *a, **kw))
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **kw:
                        tmp_path / "redirected" if self == directory
                        else original_resolve(self, *a, **kw))
    assert tools._mac_cryptex_alias_directory() is None


def test_mac_cryptex_grant_is_exact_readonly_and_not_home_or_preboot(host, monkeypatch):
    home, work = host
    directory = home / "System" / "Cryptexes"
    directory.mkdir(parents=True)
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools, "_mac_cryptex_alias_directory", lambda: directory)
    result = tools.native_toolchain_read_paths(work)
    assert result == (directory,)
    permission = native_permission_params({"cwd": str(work)}, runtime_read_paths=result)
    fs = permission["config"]["permissions"][PROFILE_ID]["filesystem"]
    assert fs[str(directory)] == "read"
    assert all(str(path) not in fs for path in (
        directory.parent, home, home / ".codex", Path("/System"),
        Path("/System/Volumes/Preboot"), Path("/System/Volumes/Preboot/Cryptexes"),
    ))


def test_mac_cryptex_discovery_cannot_override_workspace_scope(host, monkeypatch):
    _, work = host
    monkeypatch.setattr(tools, "_IS_MACOS", True)
    monkeypatch.setattr(tools, "_mac_cryptex_alias_directory", lambda: work)
    assert tools.native_toolchain_read_paths(work) == ()


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
    expected = {runtime, alias} if tools._IS_LINUX else {runtime, binary, alias}
    assert set(result) == expected
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX virtualenv symlink layout")
@pytest.mark.parametrize("platform", ["linux", "macos"])
def test_venv_descendants_are_redundant_only_on_linux(host, monkeypatch, platform):
    home, work = host
    venv = home / "venv"
    binary = executable(home / "base-python" / "bin" / "python3")
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = ../base-python/bin\n")
    python3 = venv / "bin" / "python3"
    python3.symlink_to(binary)
    python = venv / "bin" / "python"
    python.symlink_to("python3")
    pip = executable(venv / "bin" / "pip")
    monkeypatch.setattr(tools, "_IS_LINUX", platform == "linux")
    monkeypatch.setattr(tools, "_IS_MACOS", platform == "macos")
    monkeypatch.setattr(tools, "_IS_WINDOWS", False)
    monkeypatch.setattr(tools.sandbox, "_discover_toolchain", lambda policy: tools.sandbox._Toolchain(
        (("pip", pip),), (venv / "bin", venv),
    ))
    # The external canonical target has no directory grant and must survive.
    monkeypatch.setattr(tools, "_current_python_paths", lambda: (python, python3, venv))
    result = tools.native_toolchain_read_paths(work)
    assert binary in result
    if platform == "linux":
        assert set(result) == {venv, binary}
    else:
        assert set(result) == {venv, venv / "bin", pip, python, python3, binary}
    filesystem = native_permission_params({"cwd": str(work)}, runtime_read_paths=result)[
        "config"]["permissions"][PROFILE_ID]["filesystem"]
    assert all(filesystem[str(path)] == "read" for path in result)
    assert str(home) not in filesystem
    assert str(binary.parent) not in filesystem


@pytest.mark.skipif(os.name == "nt", reason="POSIX system-tool symlink layout")
@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_native_minimal_covered_aliases_are_redundant_only_on_linux(host, monkeypatch, platform):
    home, work = host
    system = home.parent / "system"
    usr, etc = system / "usr", system / "etc"
    compiler = executable(usr / "bin" / "gcc-real")
    gcc = usr / "bin" / "gcc"
    gcc.symlink_to("gcc-real")
    alternatives = etc / "alternatives" / "cc"
    alternatives.parent.mkdir(parents=True)
    alternatives.symlink_to(gcc)
    cc = usr / "bin" / "cc"
    cc.symlink_to(alternatives)
    # /usr/local/bin/node commonly points outside :minimal to a user toolchain.
    node = executable(home / "node-runtime" / "bin" / "node")
    node_alias = usr / "local" / "bin" / "node"
    node_alias.parent.mkdir(parents=True)
    node_alias.symlink_to(node)
    monkeypatch.setattr(tools, "_IS_LINUX", platform == "linux")
    monkeypatch.setattr(tools, "_IS_MACOS", platform == "macos")
    monkeypatch.setattr(tools, "_IS_WINDOWS", platform == "windows")
    monkeypatch.setattr(tools, "_LINUX_NATIVE_MINIMAL_READ_ROOTS", (usr, etc))
    monkeypatch.setattr(tools.sandbox, "_discover_toolchain", lambda policy: tools.sandbox._Toolchain((), ()))
    monkeypatch.setattr(tools.sandbox, "_TOOLCHAIN_COMMANDS", ("cc", "node"))
    monkeypatch.setattr(tools, "resolve_trusted_executable", lambda name, **kwargs:
                        str(cc if name == "cc" else node_alias))
    result = tools.native_toolchain_read_paths(work)
    if platform == "linux":
        assert result == (node,)
    else:
        assert set(result) == {cc, compiler, node_alias, node}
    filesystem = native_permission_params({"cwd": str(work)}, runtime_read_paths=result)[
        "config"]["permissions"][PROFILE_ID]["filesystem"]
    assert filesystem[":minimal"] == "read"
    assert str(usr) not in filesystem and str(etc) not in filesystem
    assert str(home) not in filesystem and str(node.parent) not in filesystem
    assert filesystem[str(node)] == "read"


def test_linux_implicit_root_itself_is_not_emitted_and_only_directories_cover(host, monkeypatch):
    home, work = host
    system = home.parent / "system"
    system.mkdir()
    external = executable(home / "external" / "tool")
    monkeypatch.setattr(tools, "_IS_LINUX", True)
    monkeypatch.setattr(tools, "_IS_MACOS", False)
    monkeypatch.setattr(tools, "_IS_WINDOWS", False)
    monkeypatch.setattr(tools, "_LINUX_NATIVE_MINIMAL_READ_ROOTS", (
        system, external, home.parent / "missing",
    ))
    monkeypatch.setattr(tools.sandbox, "_discover_toolchain", lambda policy: tools.sandbox._Toolchain(
        (("external", external),), (system,),
    ))
    assert tools.native_toolchain_read_paths(work) == (external,)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux bubblewrap regression")
def test_linux_minimal_grants_execute_absolute_alternatives_symlink(host, monkeypatch):
    bwrap = Path(os.environ.get("BELLO_TEST_NATIVE_BWRAP", "/usr/bin/bwrap"))
    if not bwrap.is_file():
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail("required native bubblewrap executable is unavailable")
        pytest.skip("bubblewrap unavailable in this environment")
    if not Path("/usr/bin/cc").is_symlink():
        pytest.skip("host has no cc alternatives symlink")
    _, work = host
    monkeypatch.setattr(tools.sandbox, "_TOOLCHAIN_COMMANDS", ("cc",))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    grants = tools.native_toolchain_read_paths(work)
    assert Path("/usr/bin/cc") not in grants
    base = [str(bwrap), "--die-with-parent", "--unshare-all", "--tmpfs", "/", "--dev", "/dev"]
    for path in sorted(tools._LINUX_NATIVE_MINIMAL_READ_ROOTS):
        if path.is_dir():
            base.extend(("--ro-bind", str(path), str(path)))
    probe = subprocess.run([*base, "/usr/bin/true"], capture_output=True, text=True, timeout=10)
    if probe.returncode:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail(probe.stderr)
        pytest.skip(f"bubblewrap unavailable in this environment: {probe.stderr}")
    for path in grants:
        base.extend(("--ro-bind", str(path), str(path)))
    completed = subprocess.run([*base, "/usr/bin/cc", "--version"],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()


@pytest.mark.skipif(not sys.platform.startswith("linux") or not Path("/usr/bin/bwrap").exists(),
                    reason="Linux bubblewrap regression")
def test_linux_native_grants_execute_symlinked_venv_python(host, monkeypatch):
    home, work = host
    venv = home / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    python3 = venv / "bin" / "python3"
    python3.symlink_to("/usr/bin/python3")
    python = venv / "bin" / "python"
    python.symlink_to("python3")
    monkeypatch.setattr(tools, "_current_python_paths", lambda: (python, Path("/usr"), venv))
    monkeypatch.setenv("PATH", str(venv / "bin"))
    grants = tools.native_toolchain_read_paths(work)
    base = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-all"]
    for path in (Path("/usr"), Path("/lib"), Path("/lib64")):
        if path.is_dir():
            base.extend(("--ro-bind", str(path), str(path)))
    probe = subprocess.run([*base, "/usr/bin/true"], capture_output=True, text=True, timeout=10)
    if probe.returncode:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail(probe.stderr)
        pytest.skip(f"bubblewrap unavailable in this environment: {probe.stderr}")
    command = list(base)
    for path in grants:
        if path != Path("/usr"):
            command.extend(("--ro-bind", str(path), str(path)))
    completed = subprocess.run(
        [*command, str(python), "-I", "-c", "print('venv-ok')"],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "venv-ok"


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
