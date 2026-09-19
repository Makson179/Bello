"""Offline checks for the artifact gate; no native build or model request."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
import tomllib

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


@pytest.mark.parametrize("change", ["hash", "platform", "paid", "failed", "missing", "duplicate", "bad_case"])
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


def test_package_binds_four_executables_licenses_manifest_and_proof(tmp_path):
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
        build.package(source, patch, release, proof, cargo, output)


def test_missing_notices_cannot_create_bundle(tmp_path):
    with pytest.raises(ValueError, match="license notices"):
        build.dependency_notices(tmp_path)
