"""The official Claude Code CLI on native Windows, entirely offline.

The historical SDK 0.2.161 / CLI 2.1.284 fixtures exercise the same-build
fallback from a pure-Python sdist (no ``_bundled/claude.exe``). Current explicit
SDK/CLI pairing is also covered by test_claude_haiku55_runtime. These tests use
fixture bytes instead of the 246 MB binary, and never execute a CLI, log in,
or contact the network (``urlopen`` is replaced wherever a download path runs).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import urllib.error

from claude_agent_sdk import ClaudeAgentOptions
from click.testing import CliRunner
import pytest

from supervisor import doctor
from supervisor.appserver import AppServerError
from supervisor.main import cli
from supervisor.runtime import claude_cli, native_codex_install
from supervisor.runtime.claude import ClaudeBackend, _DIRECT_CREDENTIAL_ENV, _PROVIDER_SWITCH_ENV
from supervisor.runtime.client import RuntimeClient


PAYLOAD = b"MZ official claude.exe fixture bytes"
AUTH = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
_REAL_DOWNLOAD = claude_cli._download


def _release(payload: bytes = PAYLOAD) -> claude_cli.OfficialCliRelease:
    return claude_cli.OfficialCliRelease(
        cli_version="2.1.284", sdk_version="0.2.161", platform="win32-x64", binary="claude.exe",
        size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
    )


class Platform(SimpleNamespace):
    pass


@pytest.fixture
def windows(tmp_path, monkeypatch):
    """Native Windows x64 with claude-agent-sdk 0.2.161 installed from its sdist."""
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(native_codex_install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(native_codex_install.platform, "machine", lambda: "AMD64")
    release = _release()
    monkeypatch.setattr(claude_cli, "MANAGED_RELEASES", {("Windows", "x86_64"): release})
    package = tmp_path / "site-packages" / "claude_agent_sdk"
    (package / "_bundled").mkdir(parents=True)
    (package / "_bundled" / ".gitignore").write_text("*\n")  # all an sdist-built wheel contains
    sdk = ModuleType("claude_agent_sdk")
    sdk.__file__ = str(package / "__init__.py")
    sdk.ClaudeAgentOptions = ClaudeAgentOptions  # the sdist has the same Python API
    declared = ModuleType("claude_agent_sdk._cli_version")
    declared.__cli_version__ = "2.1.284"
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._cli_version", declared)
    versions = {"claude-agent-sdk": "0.2.161"}
    real_version = claude_cli.metadata.version

    def version(name):
        return versions[name] if name in versions else real_version(name)

    monkeypatch.setattr(claude_cli.metadata, "version", version)
    downloads: list[Path] = []

    def download(spec, destination):
        assert spec == release
        downloads.append(destination)
        destination.write_bytes(PAYLOAD)

    monkeypatch.setattr(claude_cli, "_download", download)
    return Platform(release=release, downloads=downloads, versions=versions, declared=declared,
                    root=tmp_path / "runtime" / "claude-code", package=package)


def _cache_dir(state) -> Path:
    return state.root / state.release.sha256


# --- The readiness contract ------------------------------------------------------------


def test_verification_alone_never_downloads_and_names_the_setup_command(windows):
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli()
    assert caught.value.kind == "not-prepared"
    assert "bello runtime install claude-code" in str(caught.value)
    assert "2.1.284" in str(caught.value)
    assert windows.downloads == []


def test_prepare_installs_once_into_the_private_cache_and_reuses_it(windows):
    first = claude_cli.resolve_official_cli(prepare=True)
    assert first.source == "managed-download" and first.cli_version == "2.1.284"
    assert first.path == _cache_dir(windows) / "claude.exe"
    assert first.path.read_bytes() == PAYLOAD
    assert len(windows.downloads) == 1
    # Downloaded into private staging, then atomically moved into place.
    assert windows.downloads[0].parent.parent.name.startswith(".download-")
    if os.name != "nt":
        for path in (windows.root.parent, windows.root, first.path.parent):
            assert path.stat().st_mode & 0o077 == 0
        assert first.path.stat().st_mode & 0o777 == 0o700
    assert claude_cli.resolve_official_cli(prepare=True) == first
    assert claude_cli.resolve_official_cli() == first
    assert ClaudeBackend._official_cli_path() == first.path
    assert len(windows.downloads) == 1
    assert sorted(path.name for path in windows.root.iterdir()) == sorted(
        [windows.release.sha256, f".{windows.release.sha256}.lock"])


def test_concurrent_preparation_downloads_once(windows):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: claude_cli.resolve_official_cli(prepare=True), range(4)))
    assert len({result.path for result in results}) == 1
    assert len(windows.downloads) == 1


def test_sdk_bundle_takes_precedence_and_never_touches_the_download_cache(windows, tmp_path):
    bundled = windows.package / "_bundled" / "claude.exe"
    bundled.write_bytes(b"bundled by an official SDK wheel")
    result = claude_cli.resolve_official_cli(prepare=True, bundled=lambda: bundled)
    assert result.source == "sdk-bundle" and result.path == bundled
    assert windows.downloads == []
    assert not (tmp_path / "runtime").exists()


def test_other_platforms_have_no_download_fallback(windows, monkeypatch):
    monkeypatch.setattr(native_codex_install.platform, "system", lambda: "Linux")
    with pytest.raises(claude_cli.BundledCliMissing, match="bundled with claude-agent-sdk is missing"):
        claude_cli.resolve_official_cli(prepare=True)
    assert windows.downloads == []


@pytest.mark.parametrize("sdk_version,declared", [("0.2.162", "2.1.285"), ("0.2.159", "2.1.281"), ("0.2.161", "2.1.285")])
def test_a_different_sdk_release_is_never_paired_with_the_pinned_cli(windows, sdk_version, declared):
    windows.versions["claude-agent-sdk"] = sdk_version
    windows.declared.__cli_version__ = declared
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare)
        assert caught.value.kind == "sdk-mismatch"
        assert "bello update" in str(caught.value)
    assert windows.downloads == []


def test_missing_sdk_distribution_is_reported_as_missing_support(windows, monkeypatch):
    def missing(name):
        raise claude_cli.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(claude_cli.metadata, "version", missing)
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    assert caught.value.kind == "missing-sdk"
    assert windows.downloads == []


def test_path_and_checkout_executables_are_never_used(windows, tmp_path, monkeypatch):
    for directory in (tmp_path / "on-path", tmp_path / "checkout"):
        directory.mkdir()
        (directory / "claude.exe").write_bytes(PAYLOAD)
        (directory / "claude").write_bytes(PAYLOAD)
    monkeypatch.setenv("PATH", str(tmp_path / "on-path"))
    monkeypatch.chdir(tmp_path / "checkout")
    with pytest.raises(claude_cli.ClaudeCliError, match="not prepared"):
        claude_cli.resolve_official_cli()
    prepared = claude_cli.resolve_official_cli(prepare=True)
    assert prepared.path.is_relative_to(windows.root)


def test_tampered_cache_fails_closed_and_is_never_repaired_or_replaced(windows):
    installed = claude_cli.resolve_official_cli(prepare=True).path
    tampered = b"MZ tampered bytes with same size!!!!"[: len(PAYLOAD)]
    assert len(tampered) == len(PAYLOAD)
    installed.write_bytes(tampered)
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare)
        assert caught.value.kind == "invalid-cache"
        assert "checksum" in str(caught.value)
        assert str(installed.parent) in str(caught.value)
    assert installed.read_bytes() == tampered
    assert len(windows.downloads) == 1


def _identity(path: Path) -> tuple[int, int, int, int]:
    metadata = path.stat()
    return metadata.st_size, metadata.st_mtime_ns, metadata.st_ino, metadata.st_dev


def _rewrite_keeping_metadata(path: Path, data: bytes, *, offset: int = 0) -> None:
    """Change bytes in place, then restore the timestamps.

    Size, mtime, inode and device all stay identical, deterministically: this
    covers coarse filesystem clocks (NTFS updates at timer-tick granularity)
    and deliberately restored timestamps without sleeping or timing races.
    """
    before = path.stat()
    with path.open("r+b") as stream:
        stream.seek(offset)
        stream.write(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _identity(path) == (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev)


@pytest.mark.parametrize("offset,data", [(0, PAYLOAD.replace(b"official", b"tampered")),
                                         (len(PAYLOAD) - 1, b"?")], ids=["rewrite", "last-byte"])
def test_tampering_with_unchanged_size_and_restored_mtime_fails_closed(windows, offset, data):
    installed = claude_cli.resolve_official_cli(prepare=True).path
    assert claude_cli.resolve_official_cli().path == installed  # verified more than once beforehand
    _rewrite_keeping_metadata(installed, data, offset=offset)
    tampered = installed.read_bytes()
    assert tampered != PAYLOAD and len(tampered) == len(PAYLOAD)
    for prepare in (False, True, False):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare)
        assert caught.value.kind == "invalid-cache"
        assert "checksum does not match the pinned official build" in str(caught.value)
        assert str(installed.parent) in str(caught.value) and "never repaired or replaced" in str(caught.value)
    assert installed.read_bytes() == tampered  # never repaired, replaced or re-downloaded
    assert len(windows.downloads) == 1


def test_every_readiness_check_hashes_the_cached_bytes(windows, monkeypatch):
    installed = claude_cli.resolve_official_cli(prepare=True).path
    hashed = []
    real_sha256 = native_codex_install._sha256
    monkeypatch.setattr(native_codex_install, "_sha256", lambda path: hashed.append(path) or real_sha256(path))
    for prepare in (False, False, True):
        assert claude_cli.resolve_official_cli(prepare=prepare).path == installed
    # No metadata-only shortcut: size, mtime, inode and device do not prove the bytes.
    assert hashed == [installed] * 3
    assert len(windows.downloads) == 1


@pytest.mark.parametrize("change", ["extra-file", "wrong-size"])
def test_unexpected_cache_contents_are_rejected(windows, change):
    installed = claude_cli.resolve_official_cli(prepare=True).path
    if change == "extra-file":
        (installed.parent / "claude.cmd").write_text("@echo off")
    else:
        installed.write_bytes(PAYLOAD + b"!")
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli()
    assert caught.value.kind == "invalid-cache"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows ACLs are checked in native CI")
def test_cache_writable_by_others_is_rejected(windows):
    installed = claude_cli.resolve_official_cli(prepare=True).path
    installed.parent.chmod(0o777)
    try:
        with pytest.raises(claude_cli.ClaudeCliError, match="not writable by others"):
            claude_cli.resolve_official_cli()
    finally:
        installed.parent.chmod(0o700)


# --- Where the private cache may live -------------------------------------------------

_USER_SID = "S-1-5-21-123-456-789-1001"
_PRIVATE_SDDL = f"O:{_USER_SID}D:P(A;OICI;FA;;;{_USER_SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
# Plausible (not captured) runner work/temp directories that inherit a volume
# root's grants: Users may create folders/files, or Authenticated Users modify.
_SHARED_PARENTS = {
    "users-create": ("O:BAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIIOID;GA;;;CO)"
                     "(A;OICIID;0x1200a9;;;BU)(A;CIID;LC;;;BU)(A;CIID;DC;;;BU)", "(A;CIID;LC;;;BU)"),
    "authenticated-modify": ("O:BAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)"
                             "(A;OICIID;0x1301bf;;;AU)", "(A;OICIID;0x1301bf;;;AU)"),
}


def _default_home(tmp_path, monkeypatch) -> Path:
    """Unset BELLO_RUNTIME_DIR so Bello resolves ~/.bello/runtime in a fixture profile."""
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.delenv("BELLO_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))  # Path.home() on Windows
    assert claude_cli._runtime_base() == profile / ".bello" / "runtime"
    return profile


@pytest.fixture
def windows_acls(windows, monkeypatch):
    """The installer's Windows branch with SDDL fixtures instead of OS ACL calls.

    The real validator judges every descriptor. Native Windows CI additionally
    runs the ``real_windows`` tests below and prepares the real default cache.
    """
    descriptors: dict[Path, str] = {}
    calls: list[tuple[Path, bool, bool]] = []

    def acl(path, *, create=False, parent=False, verified_public_executable=False):
        calls.append((path, create, parent))
        if create and not path.exists():  # CreateDirectoryW; an existing entry is only inspected
            path.mkdir(mode=0o700)
        native_codex_install._validate_windows_security_descriptor(
            descriptors.get(path, _PRIVATE_SDDL), _USER_SID, parent=parent, path=path)

    monkeypatch.setattr(native_codex_install, "_IS_WINDOWS", True)
    monkeypatch.setattr(native_codex_install, "_windows_private_acl", acl)
    return SimpleNamespace(descriptors=descriptors, calls=calls)


def test_default_per_user_runtime_directory_is_created_private(windows, windows_acls, tmp_path, monkeypatch):
    profile = _default_home(tmp_path, monkeypatch)
    prepared = claude_cli.resolve_official_cli(prepare=True)
    runtime = profile / ".bello" / "runtime"
    assert prepared.path == runtime / "claude-code" / windows.release.sha256 / "claude.exe"
    assert prepared.path.read_bytes() == PAYLOAD and len(windows.downloads) == 1
    # Missing ancestors are created privately, outermost first; the profile is
    # only inspected as a containing directory and never created or changed.
    created = [path for path, create, _ in windows_acls.calls if create]
    assert created == [runtime.parent, runtime, runtime / "claude-code"]
    assert {parent for path, _, parent in windows_acls.calls if path == profile} == {True}
    assert claude_cli.resolve_official_cli() == prepared


@pytest.mark.parametrize("shape", sorted(_SHARED_PARENTS))
def test_shared_containing_directory_is_refused_with_its_path_and_ace(windows, windows_acls, tmp_path,
                                                                      monkeypatch, shape):
    descriptor, rejected_ace = _SHARED_PARENTS[shape]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    windows_acls.descriptors[runner_temp] = descriptor
    base = runner_temp / "bello-runtime"
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(base))
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    message = str(caught.value)
    assert caught.value.kind == "invalid-cache"
    assert f"private runtime directory {base} failed verification" in message
    assert f"other Windows accounts: ACE {rejected_ace} on containing directory {runner_temp}" in message
    assert "was not installed" in message and "~/.bello/runtime" in message
    assert str(base / "claude-code") not in message  # no advice to delete a cache that never existed
    assert list(runner_temp.iterdir()) == [] and windows.downloads == []
    assert windows_acls.descriptors[runner_temp] == descriptor  # never repaired
    with pytest.raises(claude_cli.ClaudeCliError) as readiness:
        claude_cli.resolve_official_cli()
    assert readiness.value.kind == "not-prepared"


def _set_real_dacl(path: Path, sddl: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, pointer]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    descriptor = pointer()
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None)
    try:  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        assert advapi.SetFileSecurityW(str(path), 0x80000004, descriptor)
    finally:
        kernel.LocalFree(descriptor)


real_windows = pytest.mark.skipif(os.name != "nt", reason="real Windows ACL APIs")


@real_windows
def test_real_windows_default_runtime_directory_has_private_acls(windows, tmp_path, monkeypatch):
    profile = _default_home(tmp_path, monkeypatch)
    prepared = claude_cli.resolve_official_cli(prepare=True).path
    runtime = profile / ".bello" / "runtime"
    assert prepared == runtime / "claude-code" / windows.release.sha256 / "claude.exe"
    native_codex_install._windows_private_acl(profile, parent=True)
    for path in (runtime.parent, runtime, runtime / "claude-code", prepared.parent, prepared):
        native_codex_install._windows_private_acl(path)
    assert claude_cli.resolve_official_cli(prepare=True).path == prepared
    assert len(windows.downloads) == 1


@real_windows
def test_real_windows_shared_containing_directory_is_refused_unchanged(windows, tmp_path, monkeypatch):
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    # Volume-root style grants: Users read, create folders here, create files below.
    _set_real_dacl(runner_temp, "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
                                "(A;OICI;0x1200a9;;;BU)(A;CI;LC;;;BU)(A;CIIO;DC;;;BU)")
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(runner_temp / "bello-runtime"))
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    assert caught.value.kind == "invalid-cache"
    assert re.search(rf"ACE \(A;CI;(?:LC|0x4);;;BU\) on containing directory {re.escape(str(runner_temp))}",
                     str(caught.value))
    assert list(runner_temp.iterdir()) == [] and windows.downloads == []
    with pytest.raises(ValueError, match="other Windows accounts"):
        native_codex_install._windows_private_acl(runner_temp, parent=True)  # left unchanged


def test_failed_download_installs_nothing_and_leaves_no_staging(windows, monkeypatch):
    def truncated(spec, destination):
        destination.write_bytes(PAYLOAD[:-1] + b"?")

    monkeypatch.setattr(claude_cli, "_download", truncated)
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    assert caught.value.kind == "download"
    assert "checksum" in str(caught.value) and "no other executable" in str(caught.value)
    assert [path.name for path in windows.root.iterdir()] == [f".{windows.release.sha256}.lock"]


# --- The real downloader, with a fake network ------------------------------------------


class Response(io.BytesIO):
    def __init__(self, body: bytes, *, url: str, length: str | None = "auto"):
        super().__init__(body)
        self._url = url
        self.headers = {} if length is None else {"Content-Length": str(len(body)) if length == "auto" else length}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@pytest.fixture
def network(windows, monkeypatch):
    # Restore the real downloader; only the network below it is a fixture.
    monkeypatch.setattr(claude_cli, "_download", _REAL_DOWNLOAD)
    requests = []

    def install(response_factory):
        def fake_urlopen(request, timeout):
            requests.append((request.full_url, dict(request.header_items()), timeout))
            return response_factory(request)
        monkeypatch.setattr(claude_cli, "urlopen", fake_urlopen)
        return requests

    return install


def test_download_fetches_only_the_pinned_official_url(windows, network):
    requests = network(lambda request: Response(PAYLOAD, url=request.full_url))
    installed = claude_cli.resolve_official_cli(prepare=True)
    assert installed.path.read_bytes() == PAYLOAD
    (url, headers, timeout), = requests
    assert url == "https://downloads.claude.ai/claude-code-releases/2.1.284/win32-x64/claude.exe"
    assert headers.get("User-agent") == "Bello-claude-code-installer"
    assert "Authorization" not in headers and "Cookie" not in headers
    assert timeout == 60


@pytest.mark.parametrize("body,url,length,message", [
    (b"X" * len(PAYLOAD), None, "auto", "checksum"),
    (PAYLOAD + b"extra", None, None, "exceeds its pinned size"),
    (PAYLOAD, None, "999", "pinned size"),
    (PAYLOAD, "http://downloads.claude.ai/claude-code-releases/2.1.284/win32-x64/claude.exe", "auto", "insecure"),
], ids=["same-size-other-bytes", "oversized-stream", "declared-size-mismatch", "https-downgrade-redirect"])
def test_download_rejects_any_difference_from_the_pinned_build(windows, network, body, url, length, message):
    network(lambda request: Response(body, url=url or request.full_url, length=length))
    with pytest.raises(claude_cli.ClaudeCliError, match=message) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    assert caught.value.kind == "download"
    assert not _cache_dir(windows).exists()


@pytest.mark.parametrize("error,expected", [
    (urllib.error.HTTPError("https://downloads.claude.ai/x", 404, "Not Found", {}, None), "HTTP 404"),
    (urllib.error.URLError("temporary failure in name resolution"), "downloads.claude.ai"),
    (TimeoutError(), "timed out"),
], ids=["http-404", "unreachable", "timeout"])
def test_network_failures_are_actionable_and_install_nothing(windows, network, error, expected):
    def fail(_request):
        raise error

    network(fail)
    with pytest.raises(claude_cli.ClaudeCliError, match=expected) as caught:
        claude_cli.resolve_official_cli(prepare=True)
    assert caught.value.kind == "download"
    assert "no other executable" in str(caught.value) or "nothing was installed" in str(caught.value)
    assert not _cache_dir(windows).exists()


# --- Every entry point shares the same contract -----------------------------------------


def _clear_provider_environment(monkeypatch):
    for name in (*_DIRECT_CREDENTIAL_ENV, *_PROVIDER_SWITCH_ENV):
        monkeypatch.delenv(name, raising=False)


async def test_production_backend_startup_uses_only_the_verified_managed_cli(windows, tmp_path, monkeypatch):
    _clear_provider_environment(monkeypatch)
    probes = []

    def probe():
        probes.append(True)
        return dict(AUTH)

    async def emit(_event):
        pass

    unprepared = ClaudeBackend(tmp_path / "state-a", emit, tool_handler=lambda _r: None, auth_probe=probe)
    with pytest.raises(AppServerError, match="bello runtime install claude-code"):
        await unprepared.request("initialize", {})
    assert probes == [] and windows.downloads == []

    prepared = claude_cli.resolve_official_cli(prepare=True)
    backend = ClaudeBackend(tmp_path / "state-b", emit, tool_handler=lambda _r: None, auth_probe=probe)
    result = await backend.request("initialize", {})
    assert result["billingRoute"] == "subscription"
    assert backend._cli_path == prepared.path
    assert backend._metadata_options().cli_path == str(prepared.path)


def test_production_mode_still_rejects_any_caller_supplied_cli_path(tmp_path):
    with pytest.raises(AppServerError, match="does not accept another path"):
        ClaudeBackend(tmp_path / "state", lambda _e: None, tool_handler=lambda _r: None,
                      cli_path=tmp_path / "claude.exe")


def test_runtime_install_claude_code_prepares_once(windows):
    runner = CliRunner()
    first = runner.invoke(cli, ["runtime", "install", "claude-code"])
    assert first.exit_code == 0, first.output
    assert "Claude Code CLI 2.1.284" in first.output
    assert str(_cache_dir(windows) / "claude.exe") in first.output
    second = runner.invoke(cli, ["runtime", "install", "claude-code"])
    assert second.exit_code == 0, second.output
    assert len(windows.downloads) == 1


def test_runtime_install_claude_code_reports_failure_without_fallback(windows, monkeypatch):
    def offline(spec, destination):
        raise claude_cli.ClaudeCliError("could not reach downloads.claude.ai", kind="download")

    monkeypatch.setattr(claude_cli, "_download", offline)
    result = CliRunner().invoke(cli, ["runtime", "install", "claude-code"])
    assert result.exit_code != 0
    assert "could not reach downloads.claude.ai" in result.output


def test_runtime_install_all_keeps_claude_result_when_pi_is_unavailable(windows, monkeypatch):
    monkeypatch.setattr("supervisor.update_check._claude_is_installed", lambda: True)

    def no_node():
        raise RuntimeError("Pi requires Node.js >= 22.19.0")

    monkeypatch.setattr("supervisor.runtime.install.install_worker", no_node)
    result = CliRunner().invoke(cli, ["runtime", "install"])
    assert result.exit_code != 0
    assert "Claude Code: official Claude Code CLI 2.1.284" in result.output
    assert "Pi: Pi requires Node.js" in result.output
    assert len(windows.downloads) == 1


def test_login_uses_the_same_prepared_official_cli(windows, monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.run", lambda command, **_kw: calls.append(command) or SimpleNamespace(returncode=0))
    result = CliRunner().invoke(cli, ["runtime", "login", "claude-code"])
    assert result.exit_code == 0, result.output
    assert calls == [[str(_cache_dir(windows) / "claude.exe"), "auth", "login"]]


def test_doctor_reports_readiness_without_downloading(windows, monkeypatch):
    monkeypatch.setattr(claude_cli, "_download", lambda *_: pytest.fail("doctor must never download"))
    before = doctor._claude_dependency_result(ClaudeBackend, AppServerError)
    assert before.level == "warn"
    assert "not ready" in before.message
    assert "bello runtime install claude-code" in before.detail
    assert "standalone claude on PATH does not replace" in before.detail

    monkeypatch.setattr(claude_cli, "_download", lambda spec, destination: destination.write_bytes(PAYLOAD))
    claude_cli.resolve_official_cli(prepare=True)
    monkeypatch.setattr(claude_cli, "_download", lambda *_: pytest.fail("doctor must never download"))
    after = doctor._claude_dependency_result(ClaudeBackend, AppServerError)
    assert after.level == "ok"
    assert "Claude Code CLI 2.1.284" in after.message and "Bello-verified download" in after.message
    assert str(_cache_dir(windows) / "claude.exe") in after.message
    assert "pinned SHA-256" in after.detail


def test_doctor_reports_sdk_mismatch_as_not_ready(windows):
    windows.versions["claude-agent-sdk"] = "0.2.162"
    result = doctor._claude_dependency_result(ClaudeBackend, AppServerError)
    assert result.level == "warn"
    assert "bello update" in result.detail


ROOT = Path(__file__).resolve().parents[1]


def test_windows_ci_proves_each_readiness_phase_without_credentials():
    workflow = (ROOT / ".github" / "workflows" / "claude-windows.yml").read_text(encoding="utf-8")
    phases = [workflow.index(f"scripts/verify_claude_cli_windows.py {phase}")
              for phase in ("sdk", "before", "after", "metadata")]
    assert phases == sorted(phases)
    install = workflow.index("bello runtime install claude-code")
    assert phases[1] < install < phases[2]
    # The real default per-user cache is prepared: no runtime-directory override.
    assert "BELLO_RUNTIME_DIR=" not in workflow and "BELLO_RUNTIME_DIR:" not in workflow
    informational = workflow.index("scripts/verify_claude_cli_windows.py runner-temp")
    assert phases[1] < informational < install
    assert workflow[informational:].splitlines()[1].strip() == "continue-on-error: true"
    assert 'python -m pip install ".[test,claude]"' in workflow
    for forbidden in ("secrets.", "ANTHROPIC_", "OAUTH", "api_key"):
        assert forbidden not in workflow
    assert "tests/test_claude_cli_windows.py" in workflow and "windows-2022" in workflow and "windows-2025" in workflow


def _verifier():
    import importlib.util

    spec = importlib.util.spec_from_file_location("verify_claude_cli_windows",
                                                  ROOT / "scripts" / "verify_claude_cli_windows.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    return verifier


def test_windows_verifier_proves_only_the_default_runtime_directory(tmp_path, monkeypatch, capsys):
    verifier = _verifier()
    profile = _default_home(tmp_path, monkeypatch)
    assert verifier.default_runtime() == profile / ".bello" / "runtime"
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runner-temp" / "bello-runtime"))
    monkeypatch.setattr(verifier.sys, "argv", ["verify", "before"])
    monkeypatch.setattr(verifier.sys, "platform", "win32")
    with pytest.raises(SystemExit) as caught:
        verifier.main()
    assert caught.value.code == 1
    assert "unset BELLO_RUNTIME_DIR" in capsys.readouterr().out


def test_runner_temp_phase_reports_the_refusal_without_failing_or_writing(tmp_path, monkeypatch, capsys):
    verifier = _verifier()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    descriptor, rejected_ace = _SHARED_PARENTS["users-create"]
    checked = []

    def acl(path, *, parent=False, **_kwargs):
        checked.append((path, parent))
        native_codex_install._validate_windows_security_descriptor(descriptor, _USER_SID, parent=parent, path=path)

    monkeypatch.setattr(native_codex_install, "_windows_private_acl", acl)
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    verifier.check_runner_temp()
    assert checked == [(runner_temp, True)]
    assert f"ACE {rejected_ace} on containing directory {runner_temp}" in capsys.readouterr().out
    assert list(runner_temp.iterdir()) == []


def test_windows_verifier_refuses_other_platforms(monkeypatch, capsys):
    verifier = _verifier()
    monkeypatch.setattr(verifier.sys, "platform", "linux")
    monkeypatch.setattr(verifier.sys, "argv", ["verify", "sdk"])
    with pytest.raises(SystemExit) as caught:
        verifier.main()
    assert caught.value.code == 1
    assert "native Windows" in capsys.readouterr().out


def test_windows_verifier_accepts_explicit_pair_with_an_older_sdk_bundle(windows, monkeypatch, capsys):
    from dataclasses import replace

    verifier = _verifier()
    # Historical fixture versions, with an explicit newer managed CLI.
    release = replace(windows.release, cli_version="2.1.293", sdk_bundled_cli_version="2.1.284")
    monkeypatch.setattr(claude_cli, "MANAGED_RELEASES", {("Windows", "x86_64"): release})
    (windows.package / "_bundled" / "claude.exe").write_bytes(b"older SDK bundle")
    verifier.check_sdk()
    output = capsys.readouterr().out
    assert "bundled executable present: True" in output
    assert "explicitly paired managed CLI: 2.1.293" in output


@pytest.mark.parametrize("defect", [None, "missing-haiku", "wrong-alias", "wrong-efforts"])
def test_windows_metadata_proof_requires_haiku55_and_sonnet55(tmp_path, monkeypatch, capsys, defect):
    verifier = _verifier()
    for name in (*_DIRECT_CREDENTIAL_ENV, *_PROVIDER_SWITCH_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "signed-out"))
    entries = []
    for family in ("sonnet", "haiku"):
        identifier = f"claude-{family}-5-5"
        entries.extend([
            {"id": identifier, "supportedEfforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": family, "resolvedModel": identifier},
        ])
    if defect == "missing-haiku":
        entries = entries[:2]
    elif defect == "wrong-alias":
        entries[-1]["resolvedModel"] = "claude-haiku-4-5-20251001"
    elif defect == "wrong-efforts":
        entries[-2]["supportedEfforts"] = []

    async def catalog(self):
        return entries

    monkeypatch.setattr(ClaudeBackend, "_read_model_catalog", catalog)
    if defect:
        with pytest.raises(SystemExit) as caught:
            verifier.check_metadata()
        assert caught.value.code == 1
    else:
        verifier.check_metadata()
        assert "alias haiku -> claude-haiku-5-5" in capsys.readouterr().out


async def test_run_preparation_only_prepares_selected_claude_engine(windows, tmp_path):
    client = RuntimeClient(cwd=tmp_path)
    await client.prepare_engines(("openai-codex/gpt-5.6-sol", "anthropic/claude-sonnet-5-5"))
    assert windows.downloads == []  # Pi API route and Codex never need the Claude CLI
    await client.prepare_engines(("claude-code/claude-sonnet-5-5",))
    assert len(windows.downloads) == 1
    injected = RuntimeClient(cwd=tmp_path, backends={"claude-code": object()})
    await injected.prepare_engines(("claude-code/claude-sonnet-5-5",))
    assert len(windows.downloads) == 1
