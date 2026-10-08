"""Offline checks for the explicit SDK 0.2.164 / Claude CLI 2.1.293 pair.

These tests use fixture bytes and SDK metadata, never execute a CLI or make a
model request. Historical bundle-first 0.2.161 / 2.1.284 fallback coverage stays
in test_claude_cli_windows.py; it is not evidence about the current SDK's wheels.
"""

from dataclasses import replace
import hashlib
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from supervisor.runtime import claude_cli, native_codex_install


# Anthropic's official 2.1.293 manifest, checked 2026-10-07. Pin the full
# platform/size/hash tuple so changing only a version cannot pass this test.
OFFICIAL_PLATFORM_PINS = {
    ("Darwin", "arm64"): (
        "darwin-arm64", "claude", 236_330_608,
        "4e21122a227857da1178aca3299700c1fd7f2b77c93f12e73c2c76db796a105e",
    ),
    ("Darwin", "x86_64"): (
        "darwin-x64", "claude", 244_819_072,
        "267af22d4eb187b8d65d1592e6fabf57b1df6c254913d5a6c5d8b956a02cd002",
    ),
    ("Linux", "arm64"): (
        "linux-arm64", "claude", 252_108_792,
        "a43629e888f0a7d96c5e8de62abf44852433a7ff2481574688db3e5b6399491f",
    ),
    ("Linux", "x86_64"): (
        "linux-x64", "claude", 252_755_128,
        "8968405e26db478af44eabc4635ab5ca557057b702a54460a59c13e1b253e978",
    ),
    ("Windows", "x86_64"): (
        "win32-x64", "claude.exe", 256_155_808,
        "8693c4a02dde7441d0066ede68af8ddfc408bb982d77e12b506286268224e6fa",
    ),
}
PAYLOAD = b"official managed Claude CLI fixture, not executable code\n"


@pytest.mark.parametrize("key,expected", OFFICIAL_PLATFORM_PINS.items())
def test_explicit_pair_matches_every_official_platform_manifest_entry(key, expected, monkeypatch):
    monkeypatch.setattr(native_codex_install.platform, "system", lambda: key[0])
    monkeypatch.setattr(native_codex_install.platform, "machine", lambda: key[1])
    release = claude_cli.managed_release()
    assert release is not None
    assert (release.platform, release.binary, release.size, release.sha256) == expected
    assert (release.sdk_version, release.sdk_bundled_cli_version, release.cli_version) == (
        "0.2.164", "2.1.292", "2.1.293")
    assert release.url == (
        f"https://downloads.claude.ai/claude-code-releases/2.1.293/{expected[0]}/{expected[1]}")


def test_explicit_pair_has_no_unpinned_platform_or_windows_arm_release(monkeypatch):
    assert set(claude_cli.MANAGED_RELEASES) == set(OFFICIAL_PLATFORM_PINS)
    monkeypatch.setattr(native_codex_install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(native_codex_install.platform, "machine", lambda: "ARM64")
    assert claude_cli.managed_release() is None


@pytest.fixture(params=list(OFFICIAL_PLATFORM_PINS), ids=lambda key: "-".join(key))
def paired(request, tmp_path, monkeypatch):
    key = request.param
    release = replace(claude_cli.MANAGED_RELEASES[key], size=len(PAYLOAD),
                      sha256=hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(claude_cli, "managed_release", lambda: release)
    declared = ModuleType("claude_agent_sdk._cli_version")
    declared.__cli_version__ = "2.1.292"
    monkeypatch.setitem(sys.modules, "claude_agent_sdk._cli_version", declared)
    versions = {claude_cli.SDK_DISTRIBUTION: "0.2.164"}
    monkeypatch.setattr(claude_cli.metadata, "version", lambda name: versions[name])
    bundle = tmp_path / "sdk-bundle" / release.binary
    bundle.parent.mkdir()
    bundle.write_bytes(b"SDK bundle 2.1.292 must not be selected")
    bundle_calls = []

    def bundled():
        bundle_calls.append(True)
        return bundle

    downloads = []

    def download(spec, destination):
        assert spec == release
        downloads.append(destination)
        destination.write_bytes(PAYLOAD)

    monkeypatch.setattr(claude_cli, "_download", download)
    return SimpleNamespace(release=release, declared=declared, versions=versions,
                           bundle=bundle, bundled=bundled, bundle_calls=bundle_calls,
                           downloads=downloads, root=tmp_path / "runtime")


def test_override_prefers_managed_cli_even_when_sdk_bundle_exists(paired):
    selected = claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    assert selected.source == "managed-download"
    assert selected.cli_version == "2.1.293"
    assert selected.path == paired.root / "claude-code" / paired.release.sha256 / paired.release.binary
    assert selected.path.read_bytes() == PAYLOAD
    assert selected.path != paired.bundle and paired.bundle.exists()
    assert len(paired.downloads) == 1 and paired.bundle_calls == []
    assert paired.declared.__cli_version__ == "2.1.292"


def test_readiness_is_offline_and_requires_managed_preparation_despite_bundle(paired):
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(bundled=paired.bundled)
    assert caught.value.kind == "not-prepared"
    assert "2.1.293" in str(caught.value)
    assert claude_cli.INSTALL_COMMAND in str(caught.value)
    assert not paired.root.exists()
    assert paired.downloads == paired.bundle_calls == []


def test_prepared_cli_is_verified_offline_and_reused_without_download(paired, monkeypatch):
    first = claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    monkeypatch.setattr(claude_cli, "_download", lambda *_: pytest.fail("prepared cache must remain offline"))
    for prepare in (False, True):
        assert claude_cli.resolve_official_cli(prepare=prepare, bundled=paired.bundled) == first
    assert len(paired.downloads) == 1 and paired.bundle_calls == []


@pytest.mark.parametrize("installed,declared", [
    ("0.2.163", "2.1.292"), ("0.2.165", "2.1.292"),
    ("0.2.164", "2.1.291"), ("0.2.164", "2.1.293"),
    ("0.2.164", None), ("0.2.164", ""), ("0.2.164", 292),
])
def test_mismatched_or_unknown_sdk_metadata_fails_before_cache_or_bundle(paired, installed, declared):
    paired.versions[claude_cli.SDK_DISTRIBUTION] = installed
    paired.declared.__cli_version__ = declared
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare, bundled=paired.bundled)
        assert caught.value.kind == "sdk-mismatch"
        assert "bello update" in str(caught.value)
    assert not paired.root.exists()
    assert paired.downloads == paired.bundle_calls == []


def test_missing_sdk_metadata_declaration_is_not_an_accepted_override(paired):
    del paired.declared.__cli_version__
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    assert caught.value.kind == "sdk-mismatch"
    assert paired.downloads == paired.bundle_calls == []


def test_missing_sdk_distribution_fails_closed_even_when_bundle_exists(paired, monkeypatch):
    def missing(name):
        raise claude_cli.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(claude_cli.metadata, "version", missing)
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare, bundled=paired.bundled)
        assert caught.value.kind == "missing-sdk"
    assert not paired.root.exists()
    assert paired.downloads == paired.bundle_calls == []


def test_sdk_drift_is_rejected_even_after_managed_cli_is_prepared(paired):
    first = claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    paired.declared.__cli_version__ = "2.1.293"
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare, bundled=paired.bundled)
        assert caught.value.kind == "sdk-mismatch"
    assert first.path.read_bytes() == PAYLOAD
    assert len(paired.downloads) == 1 and paired.bundle_calls == []


def test_tamper_with_unchanged_size_and_timestamp_never_falls_back_or_repairs(paired):
    selected = claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    before = selected.path.stat()
    changed = b"X" + PAYLOAD[1:]
    selected.path.write_bytes(changed)
    os.utime(selected.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    for prepare in (False, True):
        with pytest.raises(claude_cli.ClaudeCliError) as caught:
            claude_cli.resolve_official_cli(prepare=prepare, bundled=paired.bundled)
        assert caught.value.kind == "invalid-cache"
        assert "checksum" in str(caught.value)
    assert selected.path.read_bytes() == changed
    assert len(paired.downloads) == 1 and paired.bundle_calls == []


def test_failed_managed_download_cannot_fall_back_to_present_sdk_bundle(paired, monkeypatch):
    def offline(*_args):
        raise claude_cli.ClaudeCliError("fixture download unavailable", kind="download")

    monkeypatch.setattr(claude_cli, "_download", offline)
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    assert caught.value.kind == "download"
    assert paired.bundle_calls == []
    assert not claude_cli.managed_cli_path(paired.release).exists()


def test_override_ignores_path_checkout_and_arbitrary_executable_environment(paired, tmp_path, monkeypatch):
    arbitrary = tmp_path / "arbitrary"
    arbitrary.mkdir()
    (arbitrary / "claude").write_bytes(PAYLOAD)
    (arbitrary / "claude.exe").write_bytes(PAYLOAD)
    monkeypatch.chdir(arbitrary)
    monkeypatch.setenv("PATH", str(arbitrary))
    monkeypatch.setenv("BELLO_CLAUDE_BINARY", str(arbitrary / "claude"))
    monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(arbitrary / "claude"))
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.resolve_official_cli(bundled=paired.bundled)
    assert caught.value.kind == "not-prepared"
    assert paired.bundle_calls == paired.downloads == []


def test_legacy_release_without_override_still_prefers_sdk_bundle(paired, monkeypatch):
    legacy = replace(paired.release, cli_version="2.1.284", sdk_version="0.2.161",
                     sdk_bundled_cli_version=None)
    monkeypatch.setattr(claude_cli, "managed_release", lambda: legacy)
    paired.declared.__cli_version__ = "2.1.284"
    paired.versions[claude_cli.SDK_DISTRIBUTION] = "0.2.161"
    selected = claude_cli.resolve_official_cli(prepare=True, bundled=paired.bundled)
    assert selected.source == "sdk-bundle" and selected.path == paired.bundle
    assert selected.cli_version == "2.1.284"
    assert paired.bundle_calls == [True] and paired.downloads == []
    assert not paired.root.exists()


@pytest.mark.parametrize("declared", [None, "2.1.284"])
def test_legacy_same_build_fallback_keeps_its_original_metadata_contract(paired, declared):
    legacy = replace(paired.release, cli_version="2.1.284", sdk_version="0.2.161",
                     sdk_bundled_cli_version=None)
    paired.declared.__cli_version__ = declared
    paired.versions[claude_cli.SDK_DISTRIBUTION] = "0.2.161"
    claude_cli.check_sdk_pairing(legacy)


def test_optional_override_does_not_change_legacy_release_constructor():
    legacy = claude_cli.OfficialCliRelease("2.1.284", "0.2.161", "win32-x64", "claude.exe",
                                         len(PAYLOAD), hashlib.sha256(PAYLOAD).hexdigest())
    assert legacy.sdk_bundled_cli_version is None
