from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import supervisor.appserver as appserver_module
from supervisor.appserver import (
    AppServerClient,
    AppServerError,
    AppServerProtocolError,
    AppServerTimeoutError,
    _app_server_environment,
    _codex_home_from_environment,
    _create_isolated_codex_home,
)


def test_appserver_environment_drops_parent_codex_execution_context() -> None:
    source = {
        "PATH": "/usr/bin",
        "CODEX_HOME": "/tmp/codex-home",
        "CODEX_PERMISSION_PROFILE": ":danger-full-access",
        "CODEX_SANDBOX": "seatbelt",
        "CODEX_SANDBOX_NETWORK_DISABLED": "1",
        "CODEX_NETWORK_PROXY_ACTIVE": "1",
        "CODEX_THREAD_ID": "parent-thread",
    }

    result = _app_server_environment(source)

    assert result == {"PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex-home"}


async def test_transport_restart_reuses_isolated_codex_home(tmp_path: Path) -> None:
    client = AppServerClient()
    client._isolated_codex_home = tmp_path
    calls: list[tuple[str, bool]] = []

    async def fake_stop(*, preserve_isolated_codex_home: bool = False) -> None:
        calls.append(("stop", preserve_isolated_codex_home))

    async def fake_start(*, reuse_isolated_codex_home: bool = False) -> None:
        calls.append(("start", reuse_isolated_codex_home))

    client.stop = fake_stop  # type: ignore[method-assign]
    client.start = fake_start  # type: ignore[method-assign]

    await client.restart()

    assert calls == [("stop", True), ("start", True)]
    assert client._isolated_codex_home == tmp_path


def test_windows_default_codex_home_uses_userprofile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "windows-profile"
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    result = _codex_home_from_environment(
        {"USERPROFILE": str(profile), "HOME": str(tmp_path / "posix-home")}
    )

    assert result == (profile / ".codex").resolve(strict=False)


def test_windows_default_codex_home_falls_back_to_home_drive_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "drive-profile"
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    result = _codex_home_from_environment(
        {
            "HOMEDRIVE": str(tmp_path),
            "HOMEPATH": "/drive-profile",
            "HOME": str(tmp_path / "git-bash-home"),
        }
    )

    assert result == (profile / ".codex").resolve(strict=False)


def test_windows_default_codex_home_falls_back_to_path_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "path-home"
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: profile))

    result = _codex_home_from_environment({"HOME": str(tmp_path / "git-bash-home")})

    assert result == (profile / ".codex").resolve(strict=False)


def test_posix_default_codex_home_preserves_home_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "posix-home"
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", False)

    result = _codex_home_from_environment(
        {"HOME": str(home), "USERPROFILE": str(tmp_path / "windows-profile")}
    )

    assert result == (home / ".codex").resolve(strict=False)


def test_isolated_codex_home_preserves_configuration_but_not_user_rules(tmp_path: Path) -> None:
    source = tmp_path / "codex-home"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "test"}\n', encoding="utf-8")
    (source / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    (source / "skills").mkdir()
    (source / "rules").mkdir()
    (source / "rules" / "default.rules").write_text(
        'prefix_rule(pattern=["curl"], decision="allow")\n',
        encoding="utf-8",
    )

    isolated = _create_isolated_codex_home(source)
    try:
        assert (isolated / "auth.json").is_symlink() == (not appserver_module._IS_WINDOWS)
        assert (isolated / "config.toml").read_text(encoding="utf-8") == 'model = "gpt-test"\n'
        assert (isolated / "skills").is_symlink() == (not appserver_module._IS_WINDOWS)
        assert (isolated / "rules").is_dir()
        assert not (isolated / "rules").is_symlink()
        assert list((isolated / "rules").iterdir()) == []
        assert (source / "rules" / "default.rules").exists()
    finally:
        shutil.rmtree(isolated)


def test_isolated_codex_home_windows_copy_is_independent_and_needs_no_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex-home"
    source.mkdir()
    auth = source / "auth.json"
    auth.write_text('{"token": "source"}\n', encoding="utf-8")
    auth_alias = source / "auth-alias.json"
    auth_alias.hardlink_to(auth)
    skills = source / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("source skill\n", encoding="utf-8")
    (source / "RULES").mkdir()
    (source / "RULES" / "unsafe.rules").write_text("allow all\n", encoding="utf-8")
    auth.chmod(0o444)

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    isolated = _create_isolated_codex_home(source)
    try:
        assert not (isolated / "auth.json").is_symlink()
        assert not (isolated / "skills").is_symlink()
        assert (isolated / "skills" / "SKILL.md").read_text(encoding="utf-8") == "source skill\n"
        assert list((isolated / "rules").iterdir()) == []
        assert not (isolated / "rules" / "unsafe.rules").exists()

        (isolated / "auth.json").write_text('{"token": "isolated"}\n', encoding="utf-8")
        assert auth.read_text(encoding="utf-8") == '{"token": "source"}\n'
        assert auth_alias.read_text(encoding="utf-8") == '{"token": "source"}\n'
        assert (isolated / "auth.json").stat().st_ino != auth.stat().st_ino
    finally:
        appserver_module._remove_codex_home_tree(isolated)
        auth.chmod(0o644)

    assert not isolated.exists()


def test_isolated_codex_home_windows_skips_reconstructible_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex-home"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "source"}\n', encoding="utf-8")

    package_junction = source / "Packages" / "standalone" / "current"
    package_junction.mkdir(parents=True)
    plugin_cache_junction = (
        source / "Plugins" / "CACHE" / "market" / "bundle" / "latest"
    )
    plugin_cache_junction.mkdir(parents=True)
    plugin_payload = (
        source
        / "Plugins"
        / "CACHE"
        / "market"
        / "bundle"
        / "1.2.3"
        / "SKILL.md"
    )
    plugin_payload.parent.mkdir(parents=True)
    plugin_payload.write_text("plugin skill\n", encoding="utf-8")
    nested_latest = plugin_payload.parent / "assets" / "latest"
    nested_latest.parent.mkdir()
    nested_latest.write_text("persistent payload\n", encoding="utf-8")
    ordinary_latest = (
        source / "Plugins" / "CACHE" / "market" / "ordinary" / "latest" / "SKILL.md"
    )
    ordinary_latest.parent.mkdir(parents=True)
    ordinary_latest.write_text("ordinary version\n", encoding="utf-8")
    plugin_state = source / "Plugins" / "data" / "bello.json"
    plugin_state.parent.mkdir(parents=True)
    plugin_state.write_text('{"enabled": true}\n', encoding="utf-8")
    lock_file = source / "Thread-Writer-Locks" / "active.lock"
    lock_file.parent.mkdir()
    lock_file.write_text("locked\n", encoding="utf-8")
    temp_lock = source / "TMP" / "arg0" / "active.lock"
    temp_lock.parent.mkdir(parents=True)
    temp_lock.write_text("locked\n", encoding="utf-8")
    dot_temp_lock = source / ".TMP" / "plugins.sync.lock"
    dot_temp_lock.parent.mkdir()
    dot_temp_lock.write_text("locked\n", encoding="utf-8")

    real_is_link = appserver_module.is_link_or_reparse
    simulated_reparse_entries = {package_junction, plugin_cache_junction}
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "is_link_or_reparse",
        lambda path, stat_result=None: (
            path in simulated_reparse_entries
            or real_is_link(path, stat_result=stat_result)
        ),
    )

    isolated = _create_isolated_codex_home(source)
    try:
        assert (isolated / "auth.json").read_text(encoding="utf-8") == (
            '{"token": "source"}\n'
        )
        assert (isolated / "Plugins" / "data" / "bello.json").is_file()
        assert (
            isolated
            / "Plugins"
            / "CACHE"
            / "market"
            / "bundle"
            / "1.2.3"
            / "SKILL.md"
        ).is_file()
        assert (
            isolated
            / "Plugins"
            / "CACHE"
            / "market"
            / "bundle"
            / "1.2.3"
            / "assets"
            / "latest"
        ).is_file()
        assert (
            isolated
            / "Plugins"
            / "CACHE"
            / "market"
            / "ordinary"
            / "latest"
            / "SKILL.md"
        ).is_file()
        assert not (isolated / "Packages").exists()
        assert not (
            isolated / "Plugins" / "CACHE" / "market" / "bundle" / "latest"
        ).exists()
        assert not (isolated / "Thread-Writer-Locks").exists()
        assert not (isolated / "TMP").exists()
        assert not (isolated / ".TMP").exists()
    finally:
        appserver_module._remove_codex_home_tree(isolated)


def test_isolated_codex_home_windows_still_rejects_non_cache_plugin_reparse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex-home"
    plugin_state = source / "plugins" / "data"
    plugin_state.mkdir(parents=True)
    real_is_link = appserver_module.is_link_or_reparse

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "is_link_or_reparse",
        lambda path, stat_result=None: (
            path == plugin_state or real_is_link(path, stat_result=stat_result)
        ),
    )

    with pytest.raises(AppServerError, match="reparse"):
        _create_isolated_codex_home(source)


def test_isolated_codex_home_windows_rejects_reparse_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex-home"
    skills = source / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("skill\n", encoding="utf-8")
    real_is_link = appserver_module.is_link_or_reparse

    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        appserver_module,
        "is_link_or_reparse",
        lambda path, stat_result=None: path == skills
        or real_is_link(path, stat_result=stat_result),
    )

    with pytest.raises(AppServerError, match="reparse"):
        _create_isolated_codex_home(source)


@pytest.mark.skipif(
    os.name != "nt", reason="requires native Windows filesystem semantics"
)
def test_isolated_codex_home_handles_native_codex_junctions_and_locked_files(
    tmp_path: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    source = tmp_path / "codex-home"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "fixture"}\n', encoding="utf-8")
    (source / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    plugin_state = source / "plugins" / "data" / "bello.json"
    plugin_state.parent.mkdir(parents=True)
    plugin_state.write_text('{"enabled": true}\n', encoding="utf-8")

    plugin_version = (
        source / "plugins" / "cache" / "openai-bundled" / "chrome" / "1.2.3"
    )
    plugin_version.mkdir(parents=True)
    (plugin_version / "SKILL.md").write_text("plugin skill\n", encoding="utf-8")
    package_version = source / "packages" / "standalone" / "1.2.3"
    package_version.mkdir(parents=True)

    junctions = (
        (
            source / "packages" / "standalone" / "current",
            package_version,
        ),
        (
            source / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest",
            plugin_version,
        ),
    )
    for junction, target in junctions:
        junction.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/s",
                "/c",
                "mklink",
                "/J",
                str(junction),
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            pytest.skip(
                f"native junction creation unavailable: {result.stderr or result.stdout}"
            )

    lock_paths = (
        source / "thread-writer-locks" / "active.lock",
        source / "tmp" / "arg0" / "active.lock",
        source / ".tmp" / "plugins.sync.lock",
    )
    for lock_path in lock_paths:
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("locked\n", encoding="utf-8")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handles: list[int] = []
    try:
        for lock_path in lock_paths:
            handle = kernel32.CreateFileW(
                str(lock_path),
                0x80000000 | 0x40000000,
                0,
                None,
                3,
                0x80,
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)

        isolated = _create_isolated_codex_home(source)
        try:
            assert (isolated / "auth.json").is_file()
            assert (isolated / "config.toml").is_file()
            assert (isolated / "plugins" / "data" / "bello.json").is_file()
            assert (
                isolated
                / "plugins"
                / "cache"
                / "openai-bundled"
                / "chrome"
                / "1.2.3"
                / "SKILL.md"
            ).is_file()
            assert not (isolated / "packages").exists()
            assert not (
                isolated / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest"
            ).exists()
            assert not (isolated / "thread-writer-locks").exists()
            assert not (isolated / "tmp").exists()
            assert not (isolated / ".tmp").exists()
        finally:
            appserver_module._remove_codex_home_tree(isolated)
    finally:
        for handle in handles:
            kernel32.CloseHandle(handle)


def test_configured_windows_codex_home_root_link_is_rejected_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "real-codex-home"
    source.mkdir()
    alias = tmp_path / "codex-home-link"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    configured = _codex_home_from_environment({"CODEX_HOME": str(alias)})

    assert configured == alias.absolute()
    with pytest.raises(AppServerError, match="cannot be a symlink, junction, or reparse"):
        _create_isolated_codex_home(configured)


def test_isolated_codex_home_windows_rejects_reserved_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "codex-home"
    source.mkdir()
    monkeypatch.setattr(appserver_module, "_IS_WINDOWS", True)

    with pytest.raises(AppServerError, match="reserved Windows device name"):
        appserver_module._validate_windows_codex_home_names(source, ["CON.txt"])


async def test_request_times_out_without_appserver_response() -> None:
    class FakeStdin:
        def write(self, data):
            self.data = data

        async def drain(self):
            return None

    class FakeProcess:
        stdin = FakeStdin()

    client = AppServerClient()
    client.process = FakeProcess()  # type: ignore[assignment]

    with pytest.raises(AppServerTimeoutError) as exc_info:
        await client.request("model/list", {}, timeout=0.01)

    assert "app-server RPC model/list response timed out after 0.01s" in str(exc_info.value)


async def test_thread_list_and_turns_list_forward_query_fields() -> None:
    class RecordingClient(AppServerClient):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[tuple[str, dict, float]] = []

        async def request(self, method, params=None, *, timeout):
            self.requests.append((method, params, timeout))
            return {"data": []}

    client = RecordingClient()

    await client.thread_list(
        {
            "cwd": "/workspace",
            "archived": False,
            "sourceKinds": ["subAgent", "subAgentThreadSpawn"],
            "cursor": "next-page",
        },
        timeout=3.0,
    )
    await client.thread_turns_list(
        "child-1",
        limit=1,
        items_view="summary",
        cursor="older",
        sort_direction="desc",
        timeout=4.0,
    )

    assert client.requests == [
        (
            "thread/list",
            {
                "cwd": "/workspace",
                "archived": False,
                "sourceKinds": ["subAgent", "subAgentThreadSpawn"],
                "cursor": "next-page",
            },
            3.0,
        ),
        (
            "thread/turns/list",
            {
                "threadId": "child-1",
                "limit": 1,
                "itemsView": "summary",
                "cursor": "older",
                "sortDirection": "desc",
            },
            4.0,
        ),
    ]


async def test_reader_reports_oversized_stdout_line_without_hanging() -> None:
    errors: list[BaseException] = []
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b'{"method":"notification","params":{"output":"' + (b"x" * 128) + b'"}}\n')
    reader.feed_eof()

    class FakeProcess:
        stdout = reader

    async def on_transport_error(error: BaseException) -> None:
        errors.append(error)

    client = AppServerClient(transport_error_handler=on_transport_error, stdout_limit=64)
    client.process = FakeProcess()  # type: ignore[assignment]
    pending = asyncio.get_running_loop().create_future()
    client._pending[1] = pending

    await asyncio.wait_for(client._read_loop(), timeout=0.5)

    assert len(errors) == 1
    assert isinstance(errors[0], AppServerProtocolError)
    assert "stdout line exceeded stream limit" in str(errors[0])
    assert pending.done()
    with pytest.raises(AppServerProtocolError):
        pending.result()
