"""The optional native helper installs without auth, paid turns, or global changes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
from unittest.mock import AsyncMock

import pytest

from supervisor.runtime import native_codex_install as install
from supervisor.runtime.client import RuntimeClient


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.delenv("BELLO_CODEX_BINARY", raising=False)
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    contents = {name: f"fixture {name}".encode() for name in install._FILES
                if name != "selection-manifest.json"}
    files = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    contents["selection-manifest.json"] = json.dumps({
        "binary_sha256": files["bin/codex"], "files": files,
        "feature": "bello_native_selection", "protocol": 1,
        "version": "0.153.4", "transport_timeout_seconds": 315,
    }).encode()
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, value in contents.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(value)
            stream.addfile(entry, io.BytesIO(value))
    bundle = install.NativeBundle(
        "https://github.com/Makson179/Bello/releases/download/test/native.tar.gz",
        install._sha256(archive), hashlib.sha256(contents["selection-manifest.json"]).hexdigest(),
    )
    monkeypatch.setattr(install, "BUNDLES", {("Darwin", "arm64"): bundle})
    calls = []
    def download(spec, target):
        assert spec == bundle
        calls.append(target)
        shutil.copyfile(archive, target)
    monkeypatch.setattr(install, "_download", download)
    return bundle, archive, calls


def test_install_cache_and_two_helpers(release):
    bundle, _, calls = release
    command, manifest = install.ensure_native_selection()
    assert command[1:] == ["app-server", "--listen", "stdio://"]
    directory = manifest.parent
    assert directory.name == bundle.archive_sha256
    assert install.os.access(directory / "bin/codex-code-mode-host", install.os.X_OK)
    assert install.os.access(command[0], install.os.X_OK)
    install._verify(directory, bundle)
    assert install.ensure_native_selection() == (command, manifest)
    assert len(calls) == 1


def test_concurrent_clean_installs_download_once(release):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: install.ensure_native_selection(), range(3)))
    assert results[0] == results[1] == results[2]
    assert len(release[2]) == 1


def test_relative_runtime_root_returns_absolute_executable(release, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BELLO_RUNTIME_DIR", "relative-cache")
    command, manifest = install.ensure_native_selection()
    assert Path(command[0]).is_absolute() and manifest.is_absolute()
    assert command[0].startswith(str(tmp_path / "relative-cache"))


@pytest.mark.parametrize("file", ["bin/codex", "bin/codex-code-mode-host", "selection-manifest.json"])
def test_changed_cache_is_not_run_or_silently_replaced(release, file):
    _, manifest = install.ensure_native_selection()
    changed = manifest.parent / file
    changed.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        install.ensure_native_selection()
    assert changed.read_bytes() == b"changed"
    assert len(release[2]) == 1


@pytest.mark.skipif(install.os.name == "nt", reason="Unix ownership/modes")
@pytest.mark.parametrize("path", ["base", "root", "bundle", "bin", "executable"])
def test_writable_existing_cache_rejected_without_chmod(release, path):
    command, manifest = install.ensure_native_selection()
    target = {"base": manifest.parent.parent.parent, "root": manifest.parent.parent,
              "bundle": manifest.parent, "bin": manifest.parent / "bin",
              "executable": Path(command[0])}[path]
    target.chmod(0o777)
    try:
        with pytest.raises(ValueError, match="not writable by others"):
            install.ensure_native_selection()
        assert target.stat().st_mode & 0o022
        assert len(release[2]) == 1
    finally:
        target.chmod(0o700)


@pytest.mark.parametrize("entry_name,kind", [
    ("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
    ("bin/codex", tarfile.SYMTYPE), ("bin/codex", tarfile.LNKTYPE),
    ("bin/codex", tarfile.FIFOTYPE), ("unexpected", tarfile.REGTYPE),
])
def test_archive_cannot_write_outside_fixed_layout(tmp_path, entry_name, kind):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        entry = tarfile.TarInfo(entry_name)
        entry.type = kind
        entry.linkname = "../../escape"
        stream.addfile(entry)
    destination = tmp_path / "unpacked"
    destination.mkdir()
    with pytest.raises(ValueError, match="Unexpected entry"):
        install._unpack(archive, destination)
    assert not (tmp_path / "escape").exists()


def test_download_checksum_rejected_without_publishing_cache(release, monkeypatch):
    original = release[0]
    monkeypatch.setattr(install, "BUNDLES", {("Darwin", "arm64"): install.NativeBundle(
        original.url, original.archive_sha256, "0" * 64)})
    monkeypatch.setattr(install, "_download", lambda spec, target: shutil.copyfile(release[1], target))
    with pytest.raises(ValueError, match="manifest checksum"):
        install.ensure_native_selection()
    cache = Path(install.os.environ["BELLO_RUNTIME_DIR"]) / "native-codex"
    assert not (cache / original.archive_sha256).exists()
    assert not list(cache.glob(".download-*"))


def test_explicit_host_build_does_not_download_or_probe_platform(monkeypatch):
    monkeypatch.setenv("BELLO_CODEX_BINARY", "/host/custom-codex")
    monkeypatch.setenv("BELLO_CODEX_SELECTION_MANIFEST", "/host/manifest.json")
    monkeypatch.setattr(install.platform, "system", lambda: pytest.fail("explicit build"))
    assert install.ensure_native_selection() == (
        ["/host/custom-codex", "app-server", "--listen", "stdio://"], Path("/host/manifest.json"))


def test_unsupported_platform_fails_explicitly(release, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Unsupported")
    with pytest.raises(RuntimeError, match="No verified.*Unsupported/arm64"):
        install.ensure_native_selection()
    assert release[2] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_runtime_prepares_native_before_backend_rpc_only_with_distiller(tmp_path, monkeypatch, enabled):
    events = []
    def prepare():
        events.append("prepare")
        return ["/cache/codex", "app-server"], Path("/cache/selection-manifest.json")
    class Backend:
        def __init__(self, **kwargs):
            events.append("construct")
            self.settings = kwargs
        async def request(self, method, params):
            events.append(method)
            return {}
    monkeypatch.setattr(install, "ensure_native_selection", prepare)
    monkeypatch.setattr("supervisor.runtime.codex.CodexBackend", Backend)
    client = RuntimeClient(cwd=tmp_path)
    client._distiller = object() if enabled else None
    backend = await client._load_engine("codex")
    assert events == (["prepare"] if enabled else []) + ["construct", "initialize"]
    assert backend.settings["command"] == (["/cache/codex", "app-server"] if enabled else None)
    assert backend.settings["selection_manifest"] == (Path("/cache/selection-manifest.json") if enabled else None)
    assert await client._load_engine("codex") is backend


@pytest.mark.asyncio
async def test_other_engine_never_prepares_native(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "ensure_native_selection", lambda: pytest.fail("Claude needs no Codex download"))
    fake = AsyncMock()
    monkeypatch.setattr("supervisor.runtime.claude.ClaudeBackend", lambda **kwargs: fake)
    client = RuntimeClient(cwd=tmp_path)
    client._distiller = object()
    assert await client._load_engine("claude-code") is fake
