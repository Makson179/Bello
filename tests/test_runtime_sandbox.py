from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from supervisor.runtime import sandbox
from supervisor.runtime import windows_sandbox
from supervisor.runtime.sandbox import (
    SandboxPolicy,
    SandboxPolicyError,
    SandboxRunner,
    SandboxUnavailableError,
)


def test_policy_canonicalizes_and_deduplicates_readable_roots(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    dependency = tmp_path / "runtime"
    root.mkdir()
    dependency.mkdir()

    policy = SandboxPolicy(root / ".", mode="read-only", readable_roots=(dependency, dependency / "."))

    assert policy.root == root.resolve()
    assert policy.readable_roots == (dependency.resolve(),)


@pytest.mark.parametrize("mode", ["invalid", "workspace_write", ""])
def test_policy_rejects_unknown_modes(tmp_path: Path, mode: str) -> None:
    with pytest.raises(SandboxPolicyError, match="unsupported sandbox mode"):
        SandboxPolicy(tmp_path, mode=mode)  # type: ignore[arg-type]


def test_policy_rejects_nonexistent_or_overbroad_authority(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(SandboxPolicyError, match="existing"):
        SandboxPolicy(tmp_path / "missing")
    with pytest.raises(SandboxPolicyError, match="filesystem root"):
        SandboxPolicy(Path("/"), mode="read-only")
    with pytest.raises(SandboxPolicyError, match="account home"):
        SandboxPolicy(root, readable_roots=(Path.home(),))


def test_policy_rejects_readonly_carveout_in_writable_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    dependency = root / "vendor"
    dependency.mkdir(parents=True)

    with pytest.raises(SandboxPolicyError, match="cannot overlap"):
        SandboxPolicy(root, mode="workspace-write", readable_roots=(dependency,))

    policy = SandboxPolicy(root, mode="read-only", readable_roots=(dependency,))
    assert dependency.resolve() in policy.readable_roots


def test_policy_rejects_bad_cwd_and_run_arguments(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    runner = SandboxRunner(SandboxPolicy(root))

    with pytest.raises(SandboxPolicyError, match="outside"):
        runner._cwd(sibling)
    with pytest.raises(SandboxPolicyError, match="existing"):
        runner._cwd(root / "missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("command, timeout", [("", 1), (" \n", 1), ("x\x00y", 1), ("true", 0), ("true", -1)])
async def test_run_rejects_invalid_command_or_timeout(tmp_path: Path, command: str, timeout: float) -> None:
    runner = SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access"))
    with pytest.raises(SandboxPolicyError):
        await runner.run(command, tmp_path, timeout)


def test_macos_profile_uses_parameters_not_interpolated_paths(tmp_path: Path) -> None:
    injected = tmp_path / 'workspace )\n(allow network*)\n("quoted"'
    scratch = tmp_path / "scratch"
    injected.mkdir()
    scratch.mkdir()
    (scratch / "home").mkdir()
    (scratch / "tmp").mkdir()
    policy = SandboxPolicy(injected, mode="workspace-write", network_access=False)

    profile, parameters = sandbox._mac_profile(policy, scratch)

    assert str(injected.resolve()) not in profile
    assert any(value.endswith(f"={injected.resolve()}") for value in parameters)
    assert profile.startswith("(version 1)\n(deny default)")
    assert "(deny network*)" in profile
    assert "(allow network*)" not in profile
    assert "system.sb" not in profile
    assert "mach-lookup" not in profile
    assert "cfprefsd" not in profile
    assert "file-write-unlink" in profile
    assert ".supervisor" not in profile
    assert any(".supervisor" in parameter for parameter in parameters)


def test_macos_network_authority_is_explicit(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    profile, _ = sandbox._mac_profile(
        SandboxPolicy(tmp_path, mode="read-only", network_access=True), scratch
    )
    assert "(allow network*)" in profile
    assert "(deny network*)" not in profile


def test_macos_invocation_scrubs_host_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir()
    monkeypatch.setattr(sandbox, "_trusted_launcher", lambda *_: Path("/usr/bin/sandbox-exec"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/evil.dylib")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")

    invocation = sandbox._mac_invocation(
        SandboxPolicy(tmp_path, mode="read-only"), tmp_path, ("/bin/sh", "-c", "true"), scratch
    )

    assert invocation.argv[0] == "/usr/bin/sandbox-exec"
    assert invocation.argv[-4:] == ("--", "/bin/sh", "-c", "true")
    assert invocation.env is not None
    assert invocation.env["HOME"] == str(scratch / "home")
    assert invocation.env["TMPDIR"] == str(scratch / "tmp")
    assert not ({"OPENAI_API_KEY", "DYLD_INSERT_LIBRARIES", "SSH_AUTH_SOCK", "HTTP_PROXY"}
                & invocation.env.keys())


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_toolchain_discovery_uses_exact_under_home_version_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "account"
    node_root = home / ".nvm" / "versions" / "node" / "v24.8.0"
    python_root = home / ".pyenv" / "versions" / "3.13.7"
    node = _make_executable(node_root / "bin" / "node")
    python = _make_executable(python_root / "bin" / "python3")
    malicious = _make_executable(workspace / "local-bin" / "node")
    monkeypatch.setattr(sandbox, "_real_home", lambda: home.resolve())
    monkeypatch.setattr(sandbox.sys, "base_prefix", str(python_root))
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(malicious.parent), str(node.parent), str(python.parent))),
    )

    toolchain = sandbox._discover_toolchain(SandboxPolicy(workspace))
    shims = dict(toolchain.shims)

    assert shims["node"] == node.resolve()
    assert shims["python3"] == python.resolve()
    assert malicious.resolve() not in shims.values()
    assert node_root.resolve() in toolchain.readable_roots
    assert python_root.resolve() in toolchain.readable_roots
    assert home.resolve() not in toolchain.readable_roots
    assert all(not root.is_relative_to(malicious.parent) for root in toolchain.readable_roots)


def test_toolchain_shims_precede_system_path_without_exposing_host_bin_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    (scratch / "home").mkdir(parents=True)
    (scratch / "tmp").mkdir()
    version = tmp_path / "host" / ".nvm" / "versions" / "node" / "v24.8.0"
    node = _make_executable(version / "bin" / "node")
    unrelated = _make_executable(version.parent.parent.parent / "secret-bin" / "unrelated")
    monkeypatch.setattr(sandbox, "_real_home", lambda: (tmp_path / "account").resolve())
    monkeypatch.setattr(sandbox.sys, "base_prefix", str(workspace))
    monkeypatch.setenv("PATH", str(node.parent))
    monkeypatch.setattr(sandbox, "_trusted_launcher", lambda *_: Path("/usr/bin/sandbox-exec"))

    policy = SandboxPolicy(workspace)
    toolchain = sandbox._discover_toolchain(policy)
    sandbox._stage_tool_shims(scratch, toolchain)
    invocation = sandbox._mac_invocation(
        policy, workspace, ("/bin/sh", "-c", "node -p 42"), scratch, toolchain
    )

    assert invocation.env is not None
    assert invocation.env["PATH"].split(os.pathsep)[0] == str(scratch / "bin")
    assert (scratch / "bin" / "node").resolve() == node.resolve()
    assert str(node.parent) not in invocation.env["PATH"].split(os.pathsep)
    assert unrelated.parent not in toolchain.readable_roots
    assert version.resolve() in toolchain.readable_roots


def test_macos_homebrew_dependency_discovery_never_grants_prefix_or_private_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = tmp_path / "opt" / "homebrew"
    keg = prefix / "Cellar" / "openssl@3" / "3.6.0"
    library = _make_executable(keg / "lib" / "libssl.3.dylib")
    opt = prefix / "opt" / "openssl@3"
    opt.parent.mkdir(parents=True)
    opt.symlink_to(keg, target_is_directory=True)
    config = prefix / "etc" / "openssl@3" / "openssl.cnf"
    config.parent.mkdir(parents=True)
    config.write_text("openssl_conf = openssl_init\n", encoding="utf-8")
    private_key = config.parent / "private" / "account.key"
    private_key.parent.mkdir()
    private_key.write_text("SECRET", encoding="utf-8")
    executable = _make_executable(prefix / "Cellar" / "node" / "24.8.0" / "bin" / "node")
    output = f"{executable}:\n\t{opt / 'lib' / library.name} (compatibility version 3.0.0)\n"
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_MAC_OTOOL", _make_executable(tmp_path / "otool"))
    monkeypatch.setattr(
        sandbox.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )

    roots = sandbox._mac_linked_kegs(executable)

    assert opt in roots
    assert keg.resolve() in roots
    assert config in roots
    assert prefix not in roots
    assert config.parent not in roots
    assert private_key not in roots


def _argument_pairs(argv: tuple[str, ...], option: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, value in enumerate(argv[:-2]):
        if value == option:
            pairs.append((argv[index + 1], argv[index + 2]))
    return pairs


def test_linux_invocation_has_fail_closed_namespace_and_mount_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    dependency = tmp_path / "dependency"
    root.mkdir()
    dependency.mkdir()
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(
        sandbox,
        "_linux_system_mounts",
        lambda: ((Path("/usr/bin"), Path("/usr/bin")), (Path("/usr/lib"), Path("/usr/lib"))),
    )
    invocation = sandbox._linux_invocation(
        SandboxPolicy(root, mode="workspace-write", readable_roots=(dependency,)),
        root,
        ("/bin/sh", "-c", "printf ok"),
    )
    argv = invocation.argv

    for required in (
        "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
        "--unshare-ipc", "--unshare-uts", "--unshare-net", "--cap-drop", "--clearenv",
        "--proc", "--dev", "--tmpfs", "--chdir",
    ):
        assert required in argv
    assert "--unshare-all" not in argv
    assert not any(value.endswith("-try") for value in argv)
    assert (str(dependency), str(dependency)) in _argument_pairs(argv, "--ro-bind")
    assert (str(root), str(root)) in _argument_pairs(argv, "--bind")
    assert ("/", "/") not in _argument_pairs(argv, "--ro-bind")
    assert ("/dev", "/dev") not in _argument_pairs(argv, "--dev-bind")
    assert "/run" not in argv
    assert "/home" not in {source for source, _ in _argument_pairs(argv, "--ro-bind")}
    assert argv[-4:] == ("--", "/bin/sh", "-c", "printf ok")
    assert invocation.env is not None
    assert invocation.env.keys() == {
        "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    }


def test_linux_readonly_and_network_enabled_are_reflected_in_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(sandbox, "_linux_system_mounts", lambda: ())
    invocation = sandbox._linux_invocation(
        SandboxPolicy(tmp_path, mode="read-only", network_access=True),
        tmp_path,
        ("/bin/sh", "-c", "true"),
    )
    assert (str(tmp_path), str(tmp_path)) in _argument_pairs(invocation.argv, "--ro-bind")
    assert "--unshare-net" not in invocation.argv
    assert "--share-net" not in invocation.argv


def test_linux_toolchain_mounts_only_exact_runtime_and_creates_empty_parents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    runtime = tmp_path / "account" / ".nvm" / "versions" / "node" / "v24.8.0"
    workspace.mkdir()
    scratch.mkdir()
    node = _make_executable(runtime / "bin" / "node")
    sandbox._stage_tool_shims(scratch, sandbox._Toolchain((("node", node),), (runtime,)))
    monkeypatch.setattr(sandbox, "_linux_launcher", lambda: Path("/usr/bin/bwrap"))
    monkeypatch.setattr(sandbox, "_linux_system_mounts", lambda: ())

    invocation = sandbox._linux_invocation(
        SandboxPolicy(workspace),
        workspace,
        ("/bin/sh", "-c", "node -p 42"),
        scratch,
        sandbox._Toolchain((("node", node),), (runtime,)),
    )

    assert (str(runtime), str(runtime)) in _argument_pairs(invocation.argv, "--ro-bind")
    assert (str(scratch), "/opt/bello-tools") in _argument_pairs(
        invocation.argv, "--ro-bind"
    )
    assert str(runtime.parent) in invocation.argv
    assert invocation.env is not None
    assert invocation.env["PATH"].split(os.pathsep)[0] == "/opt/bello-tools/bin"
    assert str(runtime.parent.parent.parent.parent) not in {
        source for source, _ in _argument_pairs(invocation.argv, "--ro-bind")
    }


@pytest.mark.asyncio
async def test_restricted_windows_without_native_helper_refuses_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called = False

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("restricted command escaped to raw subprocess")

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)

    with pytest.raises(SandboxUnavailableError, match="failed closed"):
        await SandboxRunner(SandboxPolicy(tmp_path, mode="read-only")).run("echo unsafe", tmp_path, 1)
    assert not called


@pytest.mark.asyncio
async def test_windows_runner_delegates_exact_policy_and_preserves_result_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    dependency = tmp_path / "dependency"
    runtime = tmp_path / "runtime"
    for path in (root, dependency, runtime):
        path.mkdir()
    observed: dict[str, object] = {}
    deltas: list[str] = []

    async def output(chunk: str) -> None:
        deltas.append(chunk)

    async def restricted(**kwargs):
        observed.update(kwargs)
        await kwargs["on_output"]("streamed")
        return windows_sandbox.WindowsSandboxOutcome(
            "streamed", 130, timed_out=False, cancelled=True
        )

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.setattr(
        sandbox,
        "_discover_toolchain",
        lambda _policy: sandbox._Toolchain(readable_roots=(runtime,)),
    )
    monkeypatch.setattr(windows_sandbox, "run_restricted", restricted)
    cancel = asyncio.Event()
    runner = SandboxRunner(SandboxPolicy(
        root,
        mode="workspace-write",
        readable_roots=(dependency,),
        network_access=False,
    ))

    result = await runner.run("echo managed", root, 3, output, cancel_event=cancel)

    assert result.output == "streamed"
    assert result.exit_code == 130
    assert result.cancelled
    assert deltas == ["streamed"]
    assert observed["root"] == root.resolve()
    assert observed["cwd"] == root.resolve()
    assert observed["mode"] == "workspace-write"
    assert observed["readable_roots"] == (dependency.resolve(), runtime.resolve())
    assert observed["network_access"] is False
    assert observed["cancel_event"] is cancel
    assert observed["max_output_chars"] == sandbox._MAX_OUTPUT_CHARS
    assert any(".supervisor" in str(path) for path in observed["private_paths"])


@pytest.mark.asyncio
async def test_windows_backend_protocol_error_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def rejected(**_kwargs):
        raise windows_sandbox.WindowsSandboxBackendError("invalid native response")

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    monkeypatch.setattr(sandbox, "_discover_toolchain", lambda _policy: sandbox._Toolchain())
    monkeypatch.setattr(windows_sandbox, "run_restricted", rejected)

    with pytest.raises(SandboxUnavailableError, match="failed closed.*invalid native response"):
        await SandboxRunner(SandboxPolicy(tmp_path)).run("echo unsafe", tmp_path, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows AppContainer integration")
@pytest.mark.asyncio
async def test_native_windows_runner_enforces_workspace_and_private_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    private = root / ".supervisor"
    private.mkdir()
    secret = "BELLO_WINDOWS_PRIVATE_SENTINEL"
    (private / "controller-state.txt").write_text(secret, encoding="utf-8")
    sibling = tmp_path / "outside.txt"
    sibling.write_text("BELLO_WINDOWS_OUTSIDE_SENTINEL", encoding="utf-8")

    # Native toolchain grants have their own standard-user gate. Keep this
    # integration focused on SandboxRunner -> bridge -> packaged helper so it
    # never rewrites large machine-wide toolchain ACLs as a test side effect.
    monkeypatch.setattr(sandbox, "_discover_toolchain", lambda _policy: sandbox._Toolchain())
    runner = SandboxRunner(SandboxPolicy(root, mode="workspace-write", network_access=False))
    try:
        write = await runner.run("echo WINDOWS_NATIVE_OK>allowed.txt", root, 30)
    except SandboxUnavailableError as exc:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))

    assert write.exit_code == 0, write.output
    assert (root / "allowed.txt").read_text(encoding="utf-8").strip() == "WINDOWS_NATIVE_OK"
    for forbidden in (private / "controller-state.txt", sibling):
        denied = await runner.run(f'type "{forbidden}"', root, 30)
        assert denied.exit_code != 0
        assert secret not in denied.output
        assert "BELLO_WINDOWS_OUTSIDE_SENTINEL" not in denied.output

    readonly = SandboxRunner(SandboxPolicy(root, mode="read-only", network_access=False))
    denied_write = await readonly.run("echo denied>read-only-write.txt", root, 30)
    assert denied_write.exit_code != 0
    assert not (root / "read-only-write.txt").exists()


@pytest.mark.asyncio
async def test_backend_preflight_failure_never_retries_unsandboxed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    actual_spawn_called = False
    dummy = sandbox._Invocation(("/usr/bin/sandbox-exec", "--", "/bin/true"), {}, tmp_path, "test")

    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_mac_invocation", lambda *_args: dummy)

    async def unavailable(_invocation):
        raise SandboxUnavailableError("preflight denied")

    async def forbidden_spawn(*_args, **_kwargs):
        nonlocal actual_spawn_called
        actual_spawn_called = True
        raise AssertionError("raw retry")

    monkeypatch.setattr(sandbox, "_probe_backend", unavailable)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)

    with pytest.raises(SandboxUnavailableError, match="preflight denied"):
        await SandboxRunner(SandboxPolicy(tmp_path)).run("echo unsafe", tmp_path, 1)
    assert not actual_spawn_called


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_danger_runner_streams_combined_output(tmp_path: Path) -> None:
    deltas: list[str] = []

    async def output(chunk: str) -> None:
        deltas.append(chunk)

    result = await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
        "printf 'hello'; printf ' error' >&2", tmp_path, 2, on_output=output
    )

    assert result.exit_code == 0
    assert result.output == "hello error"
    assert "".join(deltas) == result.output
    assert not result.timed_out
    assert not result.cancelled


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell command fixture")
@pytest.mark.asyncio
async def test_stream_decoder_preserves_split_utf8_and_output_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interpreter = shlex.quote(str(Path(sys.executable).resolve()))
    split = "import os,time; os.write(1,b'\\xe2'); time.sleep(.05); os.write(1,b'\\x82\\xac')"
    decoded = await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
        f"{interpreter} -I -c {shlex.quote(split)}", tmp_path, 2
    )
    assert decoded.output == "€"

    monkeypatch.setattr(sandbox, "_MAX_OUTPUT_CHARS", 5)
    deltas: list[str] = []

    async def output(chunk: str) -> None:
        deltas.append(chunk)

    capped = await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
        "printf abcdefgh", tmp_path, 2, on_output=output
    )
    assert capped.output == "abcde" + sandbox._OUTPUT_TRUNCATED
    assert "".join(deltas) == capped.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_timeout_kills_background_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    command = f"(sleep 0.4; printf escaped > {shlex.quote(str(marker))}) & wait"

    result = await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
        command, tmp_path, 0.05
    )
    await asyncio.sleep(0.45)

    assert result.exit_code == 124
    assert result.timed_out
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_cancel_event_kills_background_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    cancel = asyncio.Event()
    runner = SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access"))
    task = asyncio.create_task(runner.run(
        f"(sleep 0.4; printf escaped > {shlex.quote(str(marker))}) & wait",
        tmp_path,
        2,
        cancel_event=cancel,
    ))
    await asyncio.sleep(0.05)
    cancel.set()
    result = await task
    await asyncio.sleep(0.45)

    assert result.exit_code == 130
    assert result.cancelled
    assert not marker.exists()


@pytest.mark.asyncio
async def test_pre_cancelled_event_never_spawns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cancel = asyncio.Event()
    cancel.set()

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("spawned despite pre-cancellation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    result = await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
        "echo no", tmp_path, 1, cancel_event=cancel
    )
    assert result.exit_code == 130
    assert result.cancelled


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_task_cancellation_kills_background_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "late"
    task = asyncio.create_task(
        SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
            f"(sleep 0.4; printf escaped > {shlex.quote(str(marker))}) & wait", tmp_path, 2
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.45)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_callback_failure_kills_command_tree(tmp_path: Path) -> None:
    marker = tmp_path / "late"

    async def fail(_chunk: str) -> None:
        raise ValueError("consumer failed")

    command = f"printf now; (sleep 0.4; printf escaped > {shlex.quote(str(marker))}) & wait"
    with pytest.raises(ValueError, match="consumer failed"):
        await SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
            command, tmp_path, 2, on_output=fail
        )
    await asyncio.sleep(0.45)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_callback_oserror_is_not_misreported_as_backend_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fail(_chunk: str) -> None:
        raise OSError("consumer pipe failed")

    async def available(_invocation) -> None:
        return None

    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "_probe_backend", available)
    monkeypatch.setattr(
        sandbox,
        "_mac_invocation",
        lambda _policy, cwd, command, _scratch, _toolchain: sandbox._Invocation(
            command, None, cwd, "test"
        ),
    )
    with pytest.raises(OSError, match="consumer pipe failed"):
        await SandboxRunner(SandboxPolicy(tmp_path, mode="workspace-write")).run(
            "printf now", tmp_path, 2, on_output=fail
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_repeated_task_cancellation_cannot_interrupt_tree_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "late"
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    terminate = sandbox._terminate_process_tree

    async def delayed_cleanup(process, *, descendants_only=False):
        cleanup_started.set()
        await release_cleanup.wait()
        await terminate(process, descendants_only=descendants_only)

    monkeypatch.setattr(sandbox, "_terminate_process_tree", delayed_cleanup)
    task = asyncio.create_task(
        SandboxRunner(SandboxPolicy(tmp_path, mode="danger-full-access")).run(
            f"(sleep 0.4; printf escaped > {shlex.quote(str(marker))}) & wait", tmp_path, 2
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    task.cancel()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.45)
    assert not marker.exists()


def _native_backend_expected() -> bool:
    if sys.platform == "darwin":
        return Path("/usr/bin/sandbox-exec").exists()
    if sys.platform.startswith("linux"):
        return any(Path(path).exists() for path in ("/usr/bin/bwrap", "/bin/bwrap"))
    return False


@pytest.mark.skipif(not _native_backend_expected(), reason="no supported native sandbox backend installed")
@pytest.mark.asyncio
async def test_native_restricted_filesystem_and_network_enforcement(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sibling = tmp_path / "sibling-secret"
    dependency = tmp_path / "approved-dependency"
    root.mkdir()
    sibling.write_text("outside", encoding="utf-8")
    dependency.mkdir()
    dependency_file = dependency / "public.txt"
    dependency_file.write_text("approved", encoding="utf-8")
    (root / ".supervisor").mkdir()
    private = root / ".supervisor" / "controller-state"
    private.write_text("private", encoding="utf-8")
    link = root / "escape-link"
    link.symlink_to(sibling)
    runner = SandboxRunner(SandboxPolicy(
        root,
        mode="workspace-write",
        network_access=False,
        readable_roots=(dependency,),
    ))

    try:
        write = await runner.run("printf allowed > allowed.txt", root, 5)
    except SandboxUnavailableError as exc:
        # Codex's outer macOS sandbox intentionally rejects nested sandbox_apply.
        # The release gate runs this same test with an approval scoped to this
        # test file; a genuinely unavailable system backend remains fail-closed.
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))
    assert write.exit_code == 0, write.output
    assert (root / "allowed.txt").read_text(encoding="utf-8") == "allowed"

    allowed_read = await runner.run(f"/bin/cat {shlex.quote(str(dependency_file))}", root, 5)
    assert allowed_read.exit_code == 0
    assert allowed_read.output == "approved"
    denied_dependency_write = await runner.run(
        f"printf denied > {shlex.quote(str(dependency / 'changed'))}", root, 5
    )
    assert denied_dependency_write.exit_code != 0
    assert not (dependency / "changed").exists()

    for forbidden in (sibling, link, private):
        result = await runner.run(f"/bin/cat {shlex.quote(str(forbidden))}", root, 5)
        assert result.exit_code != 0, f"sandbox disclosed {forbidden}: {result.output}"

    readonly = SandboxRunner(SandboxPolicy(root, mode="read-only", network_access=False))
    denied_write = await readonly.run("printf denied > should-not-exist", root, 5)
    assert denied_write.exit_code != 0
    assert not (root / "should-not-exist").exists()

    accepted = asyncio.Event()

    async def client(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.set()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    script = (
        "import socket; s=socket.socket(); s.settimeout(.4); "
        f"\ntry: s.connect(('127.0.0.1',{port})); print('CONNECTED')"
        "\nexcept OSError: print('blocked')"
    )
    try:
        network = await runner.run(
            f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(script)}", root, 5
        )
    finally:
        server.close()
        await server.wait_closed()
    assert network.exit_code == 0, network.output
    assert "blocked" in network.output
    assert "CONNECTED" not in network.output
    assert not accepted.is_set()


@pytest.mark.skipif(not _native_backend_expected(), reason="no supported native sandbox backend installed")
@pytest.mark.asyncio
async def test_native_private_namespace_cannot_be_aliased_to_expose_state(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    private = root / ".codex" / "bello-run"
    private.mkdir(parents=True)
    secret = "BELLO_PRIVATE_RENAME_SENTINEL"
    (private / "state.json").write_text(secret, encoding="utf-8")
    runner = SandboxRunner(SandboxPolicy(root, mode="workspace-write", network_access=False))

    try:
        result = await runner.run(
            "mv .codex renamed-codex 2>/dev/null || true; "
            "cat renamed-codex/bello-run/state.json 2>/dev/null || true; "
            "ln .codex/bello-run/state.json exposed-state 2>/dev/null || true; "
            "cat exposed-state 2>/dev/null || true",
            root,
            5,
        )
    except SandboxUnavailableError as exc:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))

    assert secret not in result.output
    assert (root / ".codex").is_dir(), "private-state parent was renamed inside the sandbox"
    assert not (root / "renamed-codex").exists()
    assert not (root / "exposed-state").exists(), "private-state file was hard-linked outside its deny"


@pytest.mark.skipif(not _native_backend_expected(), reason="no supported native sandbox backend installed")
@pytest.mark.asyncio
async def test_native_restricted_versioned_node_npm_and_python_are_available(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    policy = SandboxPolicy(root, mode="workspace-write", network_access=False)
    discovered = dict(sandbox._discover_toolchain(policy).shims)
    missing = sorted({"node", "npm", "python3"} - discovered.keys())
    if missing:
        message = f"native toolchain proof requires host tools: {', '.join(missing)}"
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            pytest.fail(message)
        pytest.skip(message)
    node_script = 'process.stdout.write("NODE_SANDBOX_OK\\n")'
    python_script = 'print("PYTHON_SANDBOX_OK")'
    command = "\n".join((
        "set -e",
        f"node -e {shlex.quote(node_script)}",
        "npm --version",
        "printf 'NPM_SANDBOX_OK\\n'",
        f"python3 -I -c {shlex.quote(python_script)}",
    ))

    try:
        result = await SandboxRunner(policy).run(command, root, 20)
    except SandboxUnavailableError as exc:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))

    assert result.exit_code == 0, result.output
    assert "NODE_SANDBOX_OK" in result.output
    assert "NPM_SANDBOX_OK" in result.output
    assert "PYTHON_SANDBOX_OK" in result.output


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process-group escape probe")
@pytest.mark.xfail(
    sys.platform == "darwin",
    strict=True,
    raises=AssertionError,
    reason=(
        "known macOS Seatbelt/process-group limit: a setsid double-fork remains sandboxed but "
        "cannot be reaped by PGID; verified against openai/codex "
        "1e66885a16161048215a3782ecdd1739aab0aabf"
    ),
)
@pytest.mark.asyncio
async def test_native_macos_detached_descendant_is_terminated(tmp_path: Path) -> None:
    # Reference implementation checked at the exact source revision above:
    # https://github.com/openai/codex/blob/1e66885a16161048215a3782ecdd1739aab0aabf/codex-rs/sandboxing/src/seatbelt_base_policy.sbpl
    # https://github.com/openai/codex/blob/1e66885a16161048215a3782ecdd1739aab0aabf/codex-rs/core/src/spawn.rs
    # Its Seatbelt policy permits process-fork, while parent-death signaling is
    # Linux-only. Strict xfail makes a future OS/backend fix visible as XPASS.
    root = tmp_path / "workspace"
    root.mkdir()
    marker = root / "detached-survived"
    child = """\
import os
import time
from pathlib import Path

if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
time.sleep(.35)
Path(%r).write_text("survived", encoding="utf-8")
""" % str(marker)
    runner = SandboxRunner(SandboxPolicy(root, mode="workspace-write", network_access=False))

    try:
        await runner.run(
            f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(child)}", root, 2
        )
    except SandboxUnavailableError as exc:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))
    await asyncio.sleep(0.45)

    assert not marker.exists(), "a double-forked setsid child escaped process-group cleanup"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS detached Seatbelt inheritance probe")
@pytest.mark.asyncio
async def test_native_macos_detached_descendant_remains_filesystem_confined(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    inside = root / "detached-ran"
    outside = tmp_path / "detached-escape"
    child = """\
import os
import time
from pathlib import Path

if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
time.sleep(.25)
try:
    Path(%r).write_text("escaped", encoding="utf-8")
except OSError:
    pass
Path(%r).write_text("ran", encoding="utf-8")
""" % (str(outside), str(inside))
    runner = SandboxRunner(SandboxPolicy(root, mode="workspace-write", network_access=False))

    try:
        await runner.run(
            f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(child)}", root, 2
        )
    except SandboxUnavailableError as exc:
        if os.environ.get("BELLO_REQUIRE_NATIVE_SANDBOX") == "1":
            raise
        pytest.skip(str(exc))
    await asyncio.sleep(0.4)

    assert inside.exists(), "detached-child probe did not execute"
    assert not outside.exists(), "a detached child escaped the inherited Seatbelt profile"
