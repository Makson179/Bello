from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from supervisor.runtime import native_codex_layout as layout
from supervisor.runtime.codex import CodexBackend


def bundle(tmp_path, monkeypatch, system="Linux"):
    monkeypatch.setattr(layout.platform, "system", lambda: system)
    # These synthetic bundles model another host's layout, not executable discovery.
    monkeypatch.setattr(layout.shutil, "which", lambda command: str(Path(command)) if Path(command).is_file() else None)
    root = tmp_path / "bundle"
    names = ["bin/codex", "bin/codex-code-mode-host"]
    if system == "Windows":
        names = [name + ".exe" for name in names] + [
            "bin/codex-command-runner.exe", "bin/codex-windows-sandbox-setup.exe"]
    elif system == "Linux":
        names.append("bin/codex-resources/bwrap")
    files = {}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("synthetic " + name).encode())
        path.chmod(0o755)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = root / "selection-manifest.json"
    manifest.write_text(json.dumps({"binary_sha256": files[names[0]], "files": files,
        "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315}))
    return root / names[0], manifest, names


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
async def test_complete_declared_package_passes_without_process(tmp_path, monkeypatch, system):
    executable, manifest, _ = bundle(tmp_path, monkeypatch, system)
    await layout.validate_native_package([str(executable)], manifest)


@pytest.mark.parametrize("system,missing", [
    ("Linux", "bin/codex-code-mode-host"), ("Linux", "bin/codex-resources/bwrap"),
    ("Darwin", "bin/codex-code-mode-host"),
    ("Windows", "bin/codex-code-mode-host.exe"),
    ("Windows", "bin/codex-command-runner.exe"),
    ("Windows", "bin/codex-windows-sandbox-setup.exe"),
])
async def test_missing_companion_fails_before_backend_initializes(tmp_path, monkeypatch, system, missing):
    executable, manifest, _ = bundle(tmp_path, monkeypatch, system)
    (manifest.parent / missing).unlink()
    called = []
    async def emit(_):
        pytest.fail("No native events before package validation")
    def forbidden_factory(**options):
        called.append(options)
        pytest.fail("No app-server initialization/auth/model call with missing helper")
    backend = CodexBackend(state_dir=tmp_path / "state", emit=emit,
        command=[str(executable), "app-server"], selection_manifest=manifest,
        client_factory=forbidden_factory)
    with pytest.raises(RuntimeError, match="packaged helper is missing or invalid"):
        await backend.request("initialize", {})
    assert not called and backend._client is None and backend._bridge is None


@pytest.mark.parametrize("mutation", ["missing-entry", "hash", "directory", "not-executable", "symlink", "linked-parent"])
async def test_declared_helper_invalidity_is_rejected(tmp_path, monkeypatch, mutation):
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    path = manifest.parent / "bin/codex-resources/bwrap"
    if mutation == "missing-entry":
        data = json.loads(manifest.read_text())
        del data["files"]["bin/codex-resources/bwrap"]
        manifest.write_text(json.dumps(data))
    elif mutation == "hash":
        path.write_bytes(b"different")
    elif mutation == "directory":
        path.unlink()
        path.mkdir()
    elif mutation == "not-executable":
        path.chmod(0o644)
        original_access = layout.os.access
        monkeypatch.setattr(layout.os, "access", lambda candidate, mode:
            False if Path(candidate) == path and mode == layout.os.X_OK else original_access(candidate, mode))
    elif mutation == "symlink":
        target = tmp_path / "unchanged-target"
        path.rename(target)
        path.symlink_to(target)
    else:
        target = tmp_path / "unchanged-directory"
        path.parent.rename(target)
        path.parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="incomplete|packaged helper is missing or invalid"):
        await layout.validate_native_package([str(executable)], manifest)


async def test_windows_reparse_helper_is_rejected(tmp_path, monkeypatch):
    executable, manifest, _ = bundle(tmp_path, monkeypatch, "Windows")
    target = manifest.parent / "bin/codex-command-runner.exe"
    original = layout.is_link_or_reparse
    monkeypatch.setattr(layout, "is_link_or_reparse", lambda path: path == target or original(path))
    with pytest.raises(RuntimeError, match="packaged helper is missing or invalid"):
        await layout.validate_native_package([str(executable)], manifest)


@pytest.mark.parametrize("capability", [None, {"feature": "bello_native_selection", "protocol": 1},
    {"files": {"custom-code-host": "custom"}, "feature": "bello_native_selection"}])
async def test_custom_host_without_packaged_contract_retains_compatibility(tmp_path, monkeypatch, capability):
    manifest = None
    if capability is not None:
        manifest = tmp_path / "custom.json"
        manifest.write_text(json.dumps(capability))
    monkeypatch.setattr(layout.shutil, "which", lambda *_: pytest.fail("Custom host not probed by layout check"))
    await layout.validate_native_package(["custom-wrapper", "arg"], manifest)


@pytest.mark.parametrize("payload", [None, b"not-json", b"[]", b" " * (65536 + 1)],
                         ids=["missing", "not-json", "non-object", "oversized"])
async def test_no_new_requirement_for_unreadable_selection_off_capability(tmp_path, monkeypatch, payload):
    manifest = tmp_path / "ignored-by-selection-off.json"
    if payload is not None:
        manifest.write_bytes(payload)
    monkeypatch.setattr(layout.shutil, "which", lambda *_: pytest.fail("No packaged contract"))
    await layout.validate_native_package(["custom-wrapper"], manifest)


@pytest.mark.parametrize("name", ["../../private-auth", "/private/file", "bin\\outside", "bin//other"])
async def test_unsafe_manifest_entries_rejected_without_opening_them(tmp_path, monkeypatch, name):
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    data = json.loads(manifest.read_text())
    data["files"][name] = "0"*64
    manifest.write_text(json.dumps(data))
    original = Path.open
    def checked_open(path, *args, **kwargs):
        assert path.is_relative_to(manifest.parent)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", checked_open)
    with pytest.raises(RuntimeError, match="unsafe member path"):
        await layout.validate_native_package([str(executable)], manifest)


@pytest.mark.parametrize("mutation", ["ambiguous", "binary-hash", "helper-hash-type"])
async def test_manifest_identity_and_digest_contract(tmp_path, monkeypatch, mutation):
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    data = json.loads(manifest.read_text())
    if mutation == "ambiguous":
        data["files"]["bin/codex.exe"] = data["binary_sha256"]
    elif mutation == "binary-hash":
        data["binary_sha256"] = "0"*64
    else:
        data["files"]["bin/codex-code-mode-host"] = None
    manifest.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="ambiguous|checksum is inconsistent|incomplete"):
        await layout.validate_native_package([str(executable)], manifest)


async def test_host_launcher_alias_to_complete_bundle_is_allowed(tmp_path, monkeypatch):
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    alias = tmp_path / "codex-on-path"
    alias.symlink_to(executable)
    await layout.validate_native_package([str(alias)], manifest)


async def test_other_launcher_with_identical_bytes_is_not_this_bundle(tmp_path, monkeypatch):
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    copied = tmp_path / "copied-codex"
    copied.write_bytes(executable.read_bytes())
    copied.chmod(0o755)
    with pytest.raises(RuntimeError, match="selected executable layout"):
        await layout.validate_native_package([str(copied)], manifest)


async def test_helper_check_runs_before_standalone_selection_probe(tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller
    executable, manifest, _ = bundle(tmp_path, monkeypatch)
    (manifest.parent / "bin/codex-code-mode-host").unlink()
    async def forbidden(*args, **kwargs):
        pytest.fail("Missing helper cannot reach feature probe")
    monkeypatch.setattr(codex_distiller.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(RuntimeError, match="packaged helper is missing or invalid"):
        await codex_distiller.validate_native_selection([str(executable)], manifest)
