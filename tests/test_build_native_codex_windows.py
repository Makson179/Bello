"""Offline checks for the artifact gate; no native build or model request."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib
from unittest.mock import AsyncMock

import pytest


spec = importlib.util.spec_from_file_location("windows_native_build",
    Path(__file__).resolve().parents[1] / "scripts" / "build_native_codex_windows.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def passing_report(binary):
    return {"schema": "bello.native-selection-provider-proof.v1", "passed": True,
            "paid_model_calls": 0, "binary_sha256": build.sha256(binary), "platform": "Windows-11",
            "cases": [{"case": name, "passed": True, "error": None,
                       "exact_model_visible_output": True, "focus_and_command_correct": True,
                       "windows_filesystem_sandbox_enforced": True,
                       "provider_requests": 2, "provider_errors": [], "external_proxy_requests_forwarded": 0}
                      for name in sorted(build.PROOF_CASES)]}


def test_lock_normalization_does_not_update_external_dependencies():
    original = ('version = 4\n\n[[package]]\nname = "codex-core"\nversion = "0.0.0"\n'
                'dependencies = ["sha2 0.10.9"]\n\n[[package]]\nname = "external"\n'
                'version = "0.0.0"\nsource = "registry+pinned"\nchecksum = "abc"\n')
    updated = build.normalize_workspace_versions(original)
    packages = tomllib.loads(updated)["package"]
    assert packages[0] == {"name": "codex-core", "version": "0.153.4", "dependencies": ["sha2 0.10.9"]}
    assert packages[1] == tomllib.loads(original)["package"][1]
    assert build.normalize_workspace_versions(updated) == updated


@pytest.mark.parametrize("revision,dirty", [("wrong", ""), (build.UPSTREAM_REVISION, " M Cargo.lock\n")])
def test_prepare_rejects_wrong_or_dirty_source(tmp_path, monkeypatch, revision, dirty):
    answers = iter([revision, dirty])
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: next(answers))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not apply patch"))
    with pytest.raises(ValueError):
        build.prepare(tmp_path, tmp_path / "patch")


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_prepare_accepts_crlf_patch_with_empty_context_on_real_git(tmp_path, monkeypatch, autocrlf):
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args], text=True)
    git("init", "--quiet")
    git("config", "core.autocrlf", autocrlf)
    (source / "codex-rs").mkdir()
    (source / "codex-rs/Cargo.lock").write_text('version = 4\n', newline="\n")
    original = b"before\n\ncontext\n"
    if autocrlf == "true":
        original = original.replace(b"\n", b"\r\n")
    (source / "file.txt").write_bytes(original)
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "fixture")
    monkeypatch.setattr(build, "UPSTREAM_REVISION", git("rev-parse", "HEAD").strip())
    patch = tmp_path / "fixture.patch"
    payload = (b"diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
               b"@@ -1,3 +1,3 @@\n-before\n+after\n\n context\n").replace(b"\n", b"\r\n")
    patch.write_bytes(payload)
    build.prepare(source, patch)
    assert (source / "file.txt").read_text() == "after\n\ncontext\n"
    assert patch.read_bytes() == payload


@pytest.mark.parametrize("change", ["hash", "platform", "paid", "failed", "missing", "duplicate", "bad_case", "sandbox"])
def test_proof_gate_rejects_incomplete_or_unrelated_evidence(tmp_path, change):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"MZsynthetic")
    report = passing_report(binary)
    if change == "hash":
        report["binary_sha256"] = "0" * 64
    elif change == "platform":
        report["platform"] = "Darwin"
    elif change == "paid":
        report["paid_model_calls"] = 1
    elif change == "failed":
        report["passed"] = False
    elif change == "missing":
        report["cases"].pop()
    elif change == "duplicate":
        report["cases"][0] = report["cases"][1]
    elif change == "sandbox":
        report["cases"][0].pop("windows_filesystem_sandbox_enforced")
    else:
        report["cases"][0]["exact_model_visible_output"] = False
    with pytest.raises(ValueError):
        build.validate_proof(report, binary)


def test_v8_hash_mismatch_fails_before_build(tmp_path, monkeypatch):
    library = tmp_path / "fixture.lib.gz"
    library.write_bytes(b"pinned")
    monkeypatch.setattr(build, "V8_HASHES", {library.name: build.sha256(library)})
    build.verify_v8(tmp_path)
    library.write_bytes(b"changed")
    with pytest.raises(ValueError, match="V8 checksum"):
        build.verify_v8(tmp_path)


@pytest.fixture
def packaged_artifact(tmp_path):
    source, release, cargo = [tmp_path / name for name in ("source", "release", "cargo")]
    (source / "codex-rs").mkdir(parents=True)
    (source / "codex-rs" / "Cargo.lock").write_text("lock")
    for name in ("LICENSE", "NOTICE"):
        (source / name).write_text(name)
    dependency = cargo / "registry/src/example/dependency-1"
    dependency.mkdir(parents=True)
    (dependency / "LICENSE-MIT").write_text("Dependency copyright and permission")
    release.mkdir()
    for name in build.BINARIES:
        (release / f"{name}.exe").write_bytes(b"MZ" + name.encode())
    patch, proof = tmp_path / "change.patch", tmp_path / "report.json"
    patch.write_text("patch")
    proof.write_text(json.dumps(passing_report(release / "codex.exe")))
    output = tmp_path / "artifact"
    archive = build.package(source, patch, release, proof, cargo, output)
    return {"output": output, "archive": archive, "source": source, "patch": patch,
            "release": release, "proof": proof, "cargo_home": cargo}


def test_package_binds_four_executables_licenses_manifest_and_proof(packaged_artifact):
    output, archive, release = [packaged_artifact[name] for name in ("output", "archive", "release")]
    with tarfile.open(archive) as stream:
        entries = stream.getmembers()
        assert all(entry.isfile() for entry in entries)
        assert {e.name for e in entries} == {
            *(f"bin/{name}.exe" for name in build.BINARIES), "selection-manifest.json",
            "BUILD-INFO", "THIRD-PARTY-NOTICES", "native-codex-selection.patch", "LICENSE", "NOTICE"}
        manifest = json.load(stream.extractfile("selection-manifest.json"))
        assert manifest["transports"] == ["tcp-hmac-v1"]
        assert manifest["binary_sha256"] == build.sha256(release / "codex.exe")
        for name, digest in manifest["files"].items():
            assert digest == build.sha256(output / "bundle" / name)
    checksums = json.loads((output / "checksums.json").read_text())
    assert checksums["archive_sha256"] == build.sha256(archive)
    assert checksums["published"] is False
    with pytest.raises(FileExistsError):
        build.package(**{name: value for name, value in packaged_artifact.items() if name != "archive"})


def test_installed_proof_uses_real_cache_and_rejects_failed_second_proof(packaged_artifact, tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller, native_codex_install as installer

    monkeypatch.setattr(build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(build.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("BELLO_CODEX_BINARY", "must-not-bypass-the-installer")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-secret")
    expected_hash = build.sha256(packaged_artifact["release"] / "codex.exe")
    validation = AsyncMock(return_value={"binary_sha256": expected_hash})
    monkeypatch.setattr(codex_distiller, "validate_native_selection", validation)
    proof_output = tmp_path / "installed-proof"
    calls = []

    def fake_provider_proof(command, *, check):
        assert check is True and command[2] == "--codex"
        installed = Path(command[3])
        assert installed.is_relative_to(tmp_path / "private-cache")
        assert installed.read_bytes().startswith(b"MZ")
        assert "OPENAI_API_KEY" not in build.os.environ
        assert "BELLO_CODEX_BINARY" not in build.os.environ
        report = passing_report(installed)
        calls.append(command)
        proof_output.mkdir()
        (proof_output / "report.json").write_text(json.dumps(report))

    monkeypatch.setattr(subprocess, "run", fake_provider_proof)
    original_bundles = dict(installer.BUNDLES)
    build.install_local(packaged_artifact["output"], tmp_path / "private-cache", proof_output)
    receipt = json.loads((tmp_path / "private-cache/installed-cache.json").read_text())
    assert receipt["passed"] is True and receipt["cache_hit"] is True
    assert receipt["local_archive_transfers"] == 1 and len(calls) == 1
    validation.assert_awaited_once()
    assert installer.BUNDLES == original_bundles
    assert build.os.environ["BELLO_CODEX_BINARY"] == "must-not-bypass-the-installer"
    with pytest.raises(ValueError, match="new child directory"):
        build.install_local(packaged_artifact["output"], tmp_path / "private-cache", proof_output)


@pytest.mark.parametrize("failure", ["archive", "proof"])
def test_installed_artifact_failures_do_not_produce_success_receipt(packaged_artifact, tmp_path, monkeypatch, failure):
    from supervisor.runtime import codex_distiller

    monkeypatch.setattr(build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(build.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(codex_distiller, "validate_native_selection", AsyncMock(return_value={"binary_sha256": "fake"}))
    def fail_proof(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, "run", fail_proof)
    if failure == "archive":
        packaged_artifact["archive"].write_bytes(b"corrupt")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        build.install_local(packaged_artifact["output"], tmp_path / "private-cache", tmp_path / "proof")
    receipt = tmp_path / "private-cache/installed-cache.json"
    assert not receipt.exists() or json.loads(receipt.read_text())["passed"] is False


def test_missing_notices_cannot_create_bundle(tmp_path):
    with pytest.raises(ValueError, match="license notices"):
        build.dependency_notices(tmp_path)
