"""Linux delivery gates are tested offline; native proof runs only on Linux CI."""
import json
import os
from pathlib import Path
import struct
import subprocess
import tarfile
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import build_native_codex_linux as build
from scripts import prepare_native_codex_linux as prepare


def elf(payload=b""):
    data = bytearray(256)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 3, 62, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HHH", data, 52, 64, 56, 1)
    struct.pack_into("<IIQ", data, 64, 1, 5, 0)
    struct.pack_into("<QQ", data, 96, len(data), len(data))
    return bytes(data) + payload


def passing_report(binary):
    return {"schema": "bello.native-selection-provider-proof.v1", "passed": True, "paid_model_calls": 0,
            "platform": "Linux-6.8.0-x86_64-with-glibc2.35", "binary_sha256": build.sha256(binary),
            "cases": [{"case": name, "passed": True, "error": None, "exact_model_visible_output": True,
                       "focus_and_command_correct": True, "provider_requests": 2, "provider_errors": [],
                       "external_proxy_requests_forwarded": 0} for name in sorted(build.PROOF_CASES)]}


def passing_sandbox(binary):
    return {"schema": build.SANDBOX_SCHEMA, "passed": True, "binary_sha256": build.sha256(binary),
            "bwrap_sha256": build.sha256(binary.parent / "codex-resources/bwrap"),
            "inside_write_succeeded": True, "outside_write_denied": True,
            "new_user_namespace": True, "tampered_bwrap_exit_code": 8,
            "system_bwrap_on_path": False, "paid_model_calls": 0}


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    source, release, cargo = (tmp_path / name for name in ("source", "release", "cargo"))
    (source / "codex-rs/vendor/bubblewrap").mkdir(parents=True)
    for name in ("LICENSE", "NOTICE", "codex-rs/vendor/bubblewrap/COPYING", "codex-rs/Cargo.lock"):
        (source / name).write_text(name)
    dependency = cargo / "registry/src/example/dependency"
    dependency.mkdir(parents=True)
    (dependency / "LICENSE-MIT").write_text("Third party copyright")
    release.mkdir()
    (release / "bwrap").write_bytes(elf(b"bwrap"))
    digest = build.sha256(release / "bwrap")
    (release / "codex").write_bytes(elf(digest.encode()))
    (release / "codex-code-mode-host").write_bytes(elf(b"host"))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: prepare.UPSTREAM_REVISION)
    monkeypatch.setenv("CODEX_BWRAP_SHA256", digest)
    output = tmp_path / "candidate"
    build.snapshot_build(source, build.ROOT / "scripts/native-codex-selection.patch", release, cargo, output)
    return output


def test_snapshot_has_exact_identity_licenses_and_embedded_bwrap(candidate):
    receipt = build.verify_build(candidate)
    assert receipt["proof_status"] == "not-run"
    assert receipt["identity"]["target"] == "x86_64-unknown-linux-gnu"
    info = json.loads((candidate / "BUILD-INFO").read_text())
    assert info["bwrap_sha256"] == build.sha256(candidate / "bin/codex-resources/bwrap")
    assert info["v8_artifact_sha256"] == prepare.V8_HASHES
    assert "Vendored bubblewrap COPYING" in (candidate / "THIRD-PARTY-NOTICES").read_text()


@pytest.mark.parametrize("change", ["extra", "missing", "symlink", "hash", "identity", "patch", "bwrap_embedded"])
def test_candidate_rejects_changed_artifacts(candidate, change):
    receipt = json.loads((candidate / build.RECEIPT).read_text())
    if change == "extra":
        (candidate / "bin/codex-resources/extra").write_text("unwanted")
    elif change == "missing":
        (candidate / "NOTICE").unlink()
    elif change == "symlink":
        (candidate / "LICENSE").unlink()
        (candidate / "LICENSE").symlink_to(candidate / "NOTICE")
    elif change == "hash":
        (candidate / "bin/codex-resources/bwrap").write_bytes(elf(b"changed"))
    elif change == "identity":
        receipt["identity"]["upstream_revision"] = "wrong"
    elif change == "patch":
        (candidate / "native-codex-selection.patch").write_text("wrong")
        receipt["files"]["native-codex-selection.patch"] = build.sha256(candidate / "native-codex-selection.patch")
    else:
        (candidate / "bin/codex").write_bytes(elf(b"no digest"))
        receipt["files"]["bin/codex"] = build.sha256(candidate / "bin/codex")
    (candidate / build.RECEIPT).write_text(json.dumps(receipt))
    with pytest.raises((ValueError, FileNotFoundError)):
        build.verify_build(candidate)


def test_executable_mode_recovery_happens_only_after_all_hashes_valid(candidate):
    for name in build.EXECUTABLES:
        (candidate / name).chmod(0o644)
    (candidate / "NOTICE").write_text("corruption")
    with pytest.raises(ValueError):
        build.verify_build(candidate, restore_executable_modes=True)
    assert (candidate / "bin/codex").stat().st_mode & 0o111 == 0
    (candidate / "NOTICE").write_text("NOTICE")
    build.verify_build(candidate, restore_executable_modes=True)
    assert all((candidate / name).stat().st_mode & 0o111 == 0o111 for name in build.EXECUTABLES)


@pytest.mark.parametrize("kind", ["magic", "arch", "type", "phoff", "segment", "executable"])
def test_elf_validation_rejects_invalid_images(tmp_path, kind):
    data = bytearray(elf())
    if kind == "magic": data[0] = 0
    elif kind == "arch": struct.pack_into("<H", data, 18, 183)
    elif kind == "type": struct.pack_into("<H", data, 16, 1)
    elif kind == "phoff": struct.pack_into("<Q", data, 32, 4096)
    elif kind == "segment": struct.pack_into("<Q", data, 96, 4096)
    else: struct.pack_into("<I", data, 68, 4)
    binary = tmp_path / "binary"
    binary.write_bytes(data)
    with pytest.raises(ValueError, match="ELF"):
        build.validate_elf(binary)


@pytest.mark.parametrize("change", ["hash", "platform", "paid", "failed", "missing", "duplicate", "output", "focus", "error", "requests"])
def test_proof_rejects_incomplete_or_unrelated_evidence(candidate, change):
    binary = candidate / "bin/codex"
    report = passing_report(binary)
    if change == "hash": report["binary_sha256"] = "0" * 64
    elif change == "platform": report["platform"] = "Darwin"
    elif change == "paid": report["paid_model_calls"] = 1
    elif change == "failed": report["passed"] = False
    elif change == "missing": report["cases"].pop()
    elif change == "duplicate": report["cases"][0] = report["cases"][1]
    elif change == "output": report["cases"][0]["exact_model_visible_output"] = False
    elif change == "focus": report["cases"][0]["focus_and_command_correct"] = False
    elif change == "error": report["cases"][0].pop("error")
    else: report["cases"][0]["provider_requests"] = 3
    with pytest.raises(ValueError):
        build.validate_proof(report, binary)


@pytest.mark.parametrize("field", ["binary_sha256", "bwrap_sha256", "outside_write_denied", "inside_write_succeeded",
                                   "new_user_namespace", "tampered_bwrap_exit_code", "system_bwrap_on_path"])
def test_package_refuses_weakened_sandbox_evidence(candidate, tmp_path, field):
    binary = candidate / "bin/codex"
    proof, sandbox = tmp_path / "proof.json", tmp_path / "sandbox.json"
    proof.write_text(json.dumps(passing_report(binary)))
    report = passing_sandbox(binary)
    report[field] = "invalid"
    sandbox.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="sandbox proof"):
        build.package_candidate(candidate, proof, sandbox, tmp_path / "artifact")
    assert not (tmp_path / "artifact").exists()


@pytest.fixture
def artifact(candidate, tmp_path):
    proof, sandbox = tmp_path / "proof.json", tmp_path / "sandbox.json"
    proof.write_text(json.dumps(passing_report(candidate / "bin/codex")))
    sandbox.write_text(json.dumps(passing_sandbox(candidate / "bin/codex")))
    output = tmp_path / "artifact"
    build.package_candidate(candidate, proof, sandbox, output)
    return output


def test_archive_manifest_covers_bwrap_and_exact_payload(artifact):
    checksums = json.loads((artifact / "checksums.json").read_text())
    archive = artifact / checksums["archive"]
    assert checksums["published"] is False
    assert build.sha256(archive) == checksums["archive_sha256"]
    with tarfile.open(archive) as stream:
        entries = stream.getmembers()
        assert {entry.name for entry in entries} == build.PAYLOAD | {"selection-manifest.json"}
        assert all(entry.isfile() and entry.mode == (0o755 if entry.name in build.EXECUTABLES else 0o644) for entry in entries)
        manifest = json.load(stream.extractfile("selection-manifest.json"))
        assert set(manifest["files"]) == build.PAYLOAD
        assert manifest["binary_sha256"] == build.sha256(artifact / "bundle/bin/codex")
        assert manifest["files"]["bin/codex-resources/bwrap"] == build.sha256(artifact / "bundle/bin/codex-resources/bwrap")


def test_real_installer_cache_used_before_and_after_proofs(artifact, tmp_path, monkeypatch):
    from supervisor.runtime import codex_distiller, native_codex_install as installer
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build.platform, "machine", lambda: "x86_64")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("BELLO_CODEX_BINARY", "must-not-bypass-cache")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-secret")
    expected_hash = build.sha256(artifact / "bundle/bin/codex")
    validation = AsyncMock(return_value={"binary_sha256": expected_hash})
    monkeypatch.setattr(codex_distiller, "validate_native_selection", validation)
    original_pins = dict(installer.BUNDLES)
    ensure = Mock(wraps=installer.ensure_native_selection)
    monkeypatch.setattr(installer, "ensure_native_selection", ensure)
    proof_output = tmp_path / "installed-proof"
    def provider_proof(command, *, check):
        assert check and ensure.call_count == 2
        assert "OPENAI_API_KEY" not in os.environ and "BELLO_CODEX_BINARY" not in os.environ
        proof_output.mkdir()
        (proof_output / "report.json").write_text(json.dumps(passing_report(Path(command[3]))))
    def sandbox(binary, output):
        assert ensure.call_count == 2
        output.mkdir()
        (output / "report.json").write_text(json.dumps(passing_sandbox(binary)))
    monkeypatch.setattr(subprocess, "run", provider_proof)
    monkeypatch.setattr(build, "sandbox_proof", sandbox)
    build.install_local(artifact, tmp_path / "private-cache", proof_output)
    receipt = json.loads((tmp_path / "private-cache/installed-cache.json").read_text())
    assert receipt["passed"] and receipt["post_proof_cache_reusable"] and receipt["local_archive_transfers"] == 1
    assert ensure.call_count == 3 and installer.BUNDLES == original_pins
    validation.assert_awaited_once()
    assert os.environ["OPENAI_API_KEY"] == "synthetic-parent-secret"
    with pytest.raises(ValueError, match="new child"):
        build.install_local(artifact, tmp_path, proof_output)


def test_v8_checksums_are_exact_and_corruption_rejected(tmp_path, monkeypatch):
    assert set(prepare.V8_HASHES) == {
        "librusty_v8_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.a.gz",
        "src_binding_ptrcomp_sandbox_release_x86_64-unknown-linux-gnu.rs"}
    fixture = tmp_path / "test.a.gz"
    fixture.write_bytes(b"known")
    monkeypatch.setattr(prepare, "V8_HASHES", {fixture.name: build.sha256(fixture)})
    prepare.verify_v8(tmp_path)
    fixture.write_bytes(b"wrong")
    with pytest.raises(ValueError, match="V8 checksum"):
        prepare.verify_v8(tmp_path)


def test_native_build_key_excludes_packaging_and_proof_only_sources():
    assert "scripts/native-codex-selection.patch" in prepare.NATIVE_INPUTS
    assert ".github/workflows/native-codex-linux-build.yml" in prepare.NATIVE_INPUTS
    assert all("build_native_codex_linux.py" not in name and "verify_native" not in name for name in prepare.NATIVE_INPUTS)
    assert len(prepare.build_key()) == 64
