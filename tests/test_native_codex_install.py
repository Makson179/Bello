"""The optional native helper installs without auth, paid turns, or global changes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from supervisor.runtime import native_codex_install as install
from supervisor.runtime.client import RuntimeClient


@pytest.fixture
def release(tmp_path, monkeypatch, request):
    monkeypatch.delenv("BELLO_CODEX_BINARY", raising=False)
    monkeypatch.delenv("BELLO_CODEX_SELECTION_MANIFEST", raising=False)
    monkeypatch.setenv("BELLO_RUNTIME_DIR", str(tmp_path / "runtime"))
    system = getattr(request, "param", "Darwin")
    machine = "AMD64" if system == "Windows" else "arm64"
    executable = "bin/codex.exe" if system == "Windows" else "bin/codex"
    monkeypatch.setattr(install.platform, "system", lambda: system)
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    contents = {name: f"fixture {name}".encode() for name in install._bundle_files(system)
                if name != "selection-manifest.json"}
    files = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
    contents["selection-manifest.json"] = json.dumps({
        "binary_sha256": files[executable], "files": files,
        "feature": "bello_native_selection", "protocol": 1,
        "version": "0.153.4", "transport_timeout_seconds": 315,
        **({"transports": ["tcp-hmac-v1"]} if system == "Windows" else {}),
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
    monkeypatch.setattr(install, "BUNDLES", {(system, "x86_64" if system == "Windows" else machine): bundle})
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


@pytest.mark.parametrize("release", ["Windows"], indirect=True)
def test_windows_fixture_installs_exe_and_all_native_helpers_then_reuses_cache(release):
    bundle, _, calls = release
    command, manifest = install.ensure_native_selection()
    assert Path(command[0]).name == "codex.exe"
    assert command[1:] == ["app-server", "--listen", "stdio://"]
    assert {path.name for path in (manifest.parent / "bin").iterdir()} == {
        "codex.exe", "codex-code-mode-host.exe", "codex-command-runner.exe",
        "codex-windows-sandbox-setup.exe",
    }
    install._verify(manifest.parent, bundle, system="Windows")
    assert install.ensure_native_selection() == (command, manifest)
    assert len(calls) == 1


@pytest.mark.parametrize("release", ["Windows"], indirect=True)
@pytest.mark.parametrize("transport", [None, [], ["unix"], "tcp-hmac-v1"])
def test_windows_bundle_requires_authenticated_tcp_transport(release, transport):
    bundle, _, _ = release
    _, manifest = install.ensure_native_selection()
    value = json.loads(manifest.read_text())
    value["transports"] = transport
    manifest.write_text(json.dumps(value))
    changed_bundle = replace(bundle, manifest_sha256=install._sha256(manifest))
    with pytest.raises(ValueError, match="requires the tcp-hmac-v1"):
        install._verify(manifest.parent, changed_bundle, system="Windows")


@pytest.mark.parametrize("release", ["Windows"], indirect=True)
def test_windows_bundle_rejects_posix_layout_and_missing_sandbox_helper(release, tmp_path):
    bundle, archive, _ = release
    destination = tmp_path / "incomplete-windows"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="Unexpected entry"):
        install._unpack(archive, destination, system="Darwin")
    _, manifest = install.ensure_native_selection()
    (manifest.parent / "bin/codex-command-runner.exe").unlink()
    with pytest.raises(ValueError, match="unexpected bundled files"):
        install._verify(manifest.parent, bundle, system="Windows")


@pytest.mark.parametrize("release", ["Windows", "Darwin"], indirect=True)
@pytest.mark.parametrize("name", ["bin/injected.dll", "untracked.json"])
def test_cache_rejects_unmanifested_adjacent_files(release, name):
    _, manifest = install.ensure_native_selection()
    (manifest.parent / name).write_bytes(b"not in the pinned archive")
    with pytest.raises(ValueError, match="unexpected bundled files"):
        install.ensure_native_selection()
    assert len(release[2]) == 1


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


@pytest.mark.parametrize("entry_name,kind", [
    ("bin/codex", tarfile.REGTYPE), ("bin/CODEX.EXE", tarfile.REGTYPE),
    ("bin/codex.exe.", tarfile.REGTYPE), ("bin/codex.exe:stream", tarfile.REGTYPE),
    ("bin\\codex.exe", tarfile.REGTYPE), ("C:/escape", tarfile.REGTYPE),
    ("//server/share", tarfile.REGTYPE), ("bin/../codex.exe", tarfile.REGTYPE),
    ("bin/codex.exe", tarfile.SYMTYPE), ("bin/codex.exe", tarfile.LNKTYPE),
    ("bin/codex.exe", tarfile.FIFOTYPE),
])
def test_windows_archive_rejects_aliases_streams_links_and_traversal(tmp_path, entry_name, kind):
    archive = tmp_path / "unsafe-windows.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        entry = tarfile.TarInfo(entry_name)
        entry.type = kind
        entry.linkname = "../../escape"
        stream.addfile(entry)
    destination = tmp_path / "unpacked"
    destination.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="Unexpected entry"):
        install._unpack(archive, destination, system="Windows")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("leaf", ["base", "root", "bundle", "bin", "executable", "manifest", "lock"])
def test_cache_rejects_opaque_windows_reparse_points_without_touching_target(release, monkeypatch, leaf):
    command, manifest = install.ensure_native_selection()
    bundle = release[0]
    target = {
        "base": manifest.parent.parent.parent, "root": manifest.parent.parent,
        "bundle": manifest.parent, "bin": manifest.parent / "bin",
        "executable": Path(command[0]), "manifest": manifest,
        "lock": manifest.parent.parent / f".{bundle.archive_sha256}.lock",
    }[leaf]
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        metadata = original(path, *args, **kwargs)
        if path != target:
            return metadata
        # Junctions need not be reported as S_IFLNK; use the native attribute bit.
        return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400,
                               st_uid=getattr(metadata, "st_uid", 0), st_size=metadata.st_size)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="real |invalid|checksum|reparse"):
        install.ensure_native_selection()
    assert len(release[2]) == 1


def test_windows_cache_rejects_reparse_ancestor_before_creating_child(tmp_path, monkeypatch):
    ancestor = tmp_path / "junction"
    ancestor.mkdir()
    destination = ancestor / "new-cache"
    monkeypatch.setattr(install, "_IS_WINDOWS", True)
    monkeypatch.setattr(install, "is_link_or_reparse", lambda path, **kw: path == ancestor)
    monkeypatch.setattr(install, "_windows_private_acl", lambda *a, **kw: pytest.fail("must not create cache"))
    with pytest.raises(ValueError, match="traverse Windows reparse"):
        install._private_directory(destination, parents=True)
    assert not destination.exists()


_USER_SID = "S-1-5-21-123-456-789-1001"


@pytest.mark.parametrize("dacl", [
    f"P(A;OICI;FA;;;{_USER_SID})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
    f"AI(A;ID;0x1f01ff;;;{_USER_SID})(A;ID;FA;;;S-1-5-18)",
])
def test_windows_cache_accepts_only_private_current_user_system_admin_acl(dacl):
    install._validate_windows_security_descriptor(f"O:{_USER_SID}D:{dacl}", _USER_SID)


@pytest.mark.parametrize("descriptor", [
    f"O:{_USER_SID}D:NO_ACCESS_CONTROL", f"O:{_USER_SID}D:P",
    f"O:{_USER_SID}D:P(A;OICI;FA;;;WD)",
    f"O:{_USER_SID}D:P(A;OICI;FR;;;BU)",
    f"O:{_USER_SID}D:P(A;OICI;FA;;;S-1-5-21-123-456-789-1002)",
    f"O:WDD:P(A;OICI;FA;;;{_USER_SID})",
    f"O:{_USER_SID}D:P(A;OICI;FA;;;SY)junk",
    f"O:{_USER_SID}D:P(XA;OICI;FA;;;SY;(@User.foo == 1))",
    f"O:{_USER_SID}D:P(OA;OICI;FA;guid;;SY)",
])
def test_windows_cache_rejects_public_null_foreign_and_unknown_dacls(descriptor):
    with pytest.raises(ValueError, match="Windows"):
        install._validate_windows_security_descriptor(descriptor, _USER_SID)


@pytest.mark.skipif(not install._IS_WINDOWS, reason="Native Windows DACL APIs")
def test_windows_private_directory_has_real_private_dacl_and_is_reusable(tmp_path):
    directory = tmp_path / "native-private-cache"
    install._private_directory(directory)
    install._windows_private_acl(directory)
    install._private_directory(directory)  # Existing cache is validated, not repaired.
    child = directory / "helper.exe"
    child.write_bytes(b"synthetic binary")
    install._windows_private_acl(child)  # Restrictive directory ACL is inherited.


@pytest.mark.skipif(not install._IS_WINDOWS, reason="Native Windows DACL APIs")
def test_windows_existing_public_acl_is_rejected_without_repair(tmp_path):
    import ctypes
    from ctypes import wintypes

    directory = tmp_path / "public-cache"
    install._private_directory(directory)
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
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:P(A;OICI;FA;;;WD)", 1, ctypes.byref(descriptor), None)
    try:
        assert advapi.SetFileSecurityW(str(directory), 0x80000004, descriptor)
    finally:
        kernel.LocalFree(descriptor)
    with pytest.raises(ValueError, match="other Windows accounts"):
        install._private_directory(directory)
    with pytest.raises(ValueError, match="other Windows accounts"):
        install._windows_private_acl(directory)  # The rejected ACL remains unmodified.


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


@pytest.mark.parametrize("relative_manifest", [False, True])
@pytest.mark.parametrize("binary_name", ["custom-codex", "custom-codex.exe"])
def test_explicit_host_build_does_not_download_or_probe_platform(tmp_path, monkeypatch, relative_manifest, binary_name):
    binary = tmp_path / "host" / binary_name
    manifest = tmp_path / "host" / "manifest.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BELLO_CODEX_BINARY", str(binary))
    monkeypatch.setenv("BELLO_CODEX_SELECTION_MANIFEST", str(
        manifest.relative_to(tmp_path) if relative_manifest else manifest))
    monkeypatch.setattr(install.platform, "system", lambda: pytest.fail("explicit build"))
    monkeypatch.setattr(install.platform, "machine", lambda: pytest.fail("explicit build"))
    monkeypatch.setattr(install, "_download", lambda *a: pytest.fail("explicit build"))
    assert install.ensure_native_selection() == (
        [str(binary), "app-server", "--listen", "stdio://"], manifest)


def test_unsupported_platform_fails_explicitly(release, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Unsupported")
    with pytest.raises(RuntimeError, match="No verified.*Unsupported/arm64"):
        install.ensure_native_selection()
    assert release[2] == []


def test_windows_without_verified_bundle_requests_explicit_build_without_downloading(release, monkeypatch):
    monkeypatch.setattr(install.platform, "system", lambda: "Windows")
    monkeypatch.setattr(install.platform, "machine", lambda: "AMD64")
    with pytest.raises(RuntimeError, match="No verified.*Windows/x86_64.*BELLO_CODEX_BINARY"):
        install.ensure_native_selection()
    assert release[2] == []


def test_no_unverified_windows_bundle_is_published():
    # Pinning a real Windows release is a separate action after native boundary proof.
    assert not any(system == "Windows" for system, _ in install.BUNDLES)


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
