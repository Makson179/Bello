"""Offline checks for the artifact gate; no native build or model request."""

import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tarfile
import tomllib
from unittest.mock import AsyncMock, Mock

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
    ensure = Mock(wraps=installer.ensure_native_selection)
    monkeypatch.setattr(installer, "ensure_native_selection", ensure)

    def fake_provider_proof(command, *, check):
        assert ensure.call_count == 2  # The final cache check must follow execution.
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
    assert receipt["post_proof_cache_reusable"] is True and ensure.call_count == 3
    assert receipt["local_archive_transfers"] == 1 and len(calls) == 1
    validation.assert_awaited_once()
    assert installer.BUNDLES == original_bundles
    assert build.os.environ["BELLO_CODEX_BINARY"] == "must-not-bypass-the-installer"
    with pytest.raises(ValueError, match="new child directory"):
        build.install_local(packaged_artifact["output"], tmp_path / "private-cache", proof_output)


@pytest.mark.parametrize("change", ["payload", "acl"])
def test_installed_proof_rejects_cache_changes_after_successful_execution(packaged_artifact, tmp_path, monkeypatch, change):
    from supervisor.runtime import codex_distiller, native_codex_install as installer

    monkeypatch.setattr(build.platform, "system", lambda: "Windows")
    monkeypatch.setattr(build.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    expected_hash = build.sha256(packaged_artifact["release"] / "codex.exe")
    monkeypatch.setattr(codex_distiller, "validate_native_selection",
                        AsyncMock(return_value={"binary_sha256": expected_hash}))
    proof_output = tmp_path / "proof"

    def proof_then_change_cache(command, *, check):
        assert check is True
        installed = Path(command[3])
        proof_output.mkdir()
        (proof_output / "report.json").write_text(json.dumps(passing_report(installed)))
        if change == "payload":
            (installed.parent.parent / "NOTICE").write_text("changed by a child process")
        else:
            # Exercise the installer's unchanged ACL validation path after the
            # proof, without pretending a POSIX host can mutate Windows ACLs.
            owned = installer._owned

            def reject_changed_acl(path, **kwargs):
                if path == installed:
                    raise ValueError("Native Codex cache must not grant access to other Windows accounts")
                return owned(path, **kwargs)

            monkeypatch.setattr(installer, "_owned", reject_changed_acl)

    monkeypatch.setattr(subprocess, "run", proof_then_change_cache)
    with pytest.raises(ValueError, match="checksum mismatch|must not grant access"):
        build.install_local(packaged_artifact["output"], tmp_path / "private-cache", proof_output)
    receipt = json.loads((tmp_path / "private-cache/installed-cache.json").read_text())
    assert receipt["passed"] is False and receipt["post_proof_cache_reusable"] is False
    assert json.loads((proof_output / "report.json").read_text())["passed"] is True


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


def synthetic_pe(name: str) -> bytes:
    data = bytearray(1024)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664, 1)
    struct.pack_into("<HH", data, 148, 240, 0x0022)
    struct.pack_into("<H", data, 152, 0x020b)
    struct.pack_into("<II", data, 128 + 24 + 240 + 16, 512, 512)
    data[512:512 + len(name)] = name.encode()
    return bytes(data)


@pytest.fixture
def native_inputs_root(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for name in build.native_prepare.NATIVE_INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((name + "\nfixture native input\n").encode())
    monkeypatch.setattr(build, "ROOT", root)
    return root


@pytest.mark.parametrize("name", build.native_prepare.NATIVE_INPUTS)
def test_native_build_key_changes_for_each_native_input_but_not_line_endings(native_inputs_root, name):
    prep = build.native_prepare
    before = prep.build_key(native_inputs_root)
    path = native_inputs_root / name
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert prep.build_key(native_inputs_root) == before
    path.write_bytes(path.read_bytes() + b"different\r\n")
    assert prep.build_key(native_inputs_root) != before


def test_native_key_ignores_python_runtime_proof_packaging_and_main_workflow(native_inputs_root):
    prep = build.native_prepare
    before = prep.build_key(native_inputs_root)
    for name in ("supervisor/runtime/codex.py", "scripts/verify_native_codex_selection.py",
                 "scripts/build_native_codex_windows.py", ".github/workflows/native-codex-windows.yml",
                 "pyproject.toml", "README.md"):
        path = native_inputs_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Python or proof changes must not recompile Rust\n")
    assert prep.build_key(native_inputs_root) == before
    assert set(prep.native_inputs(native_inputs_root)) == {
        "scripts/native-codex-selection.patch", "scripts/prepare_native_codex_windows.py",
        ".github/workflows/native-codex-windows-build.yml"}


@pytest.fixture
def native_candidate(tmp_path, native_inputs_root, monkeypatch):
    source, release, cargo = [tmp_path / name for name in ("native-source", "native-release", "native-cargo")]
    (source / "codex-rs").mkdir(parents=True)
    (source / "codex-rs/Cargo.lock").write_text("pinned normalized lockfile\n")
    for name in ("LICENSE", "NOTICE"):
        (source / name).write_text(name + "\n")
    dependency = cargo / "registry/src/example/dep-1"
    dependency.mkdir(parents=True)
    (dependency / "LICENSE").write_text("fixture attribution\n")
    release.mkdir()
    for name in build.BINARIES:
        (release / f"{name}.exe").write_bytes(synthetic_pe(name))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: build.UPSTREAM_REVISION + "\n")
    patch = native_inputs_root / "scripts/native-codex-selection.patch"
    candidate = tmp_path / "candidate"
    build.snapshot_build(source, patch, release, cargo, candidate)
    return {"candidate": candidate, "source": source, "release": release, "patch": patch,
            "cargo_home": cargo, "root": native_inputs_root}


def test_native_snapshot_binds_exact_files_but_does_not_claim_proof(native_candidate):
    candidate = native_candidate["candidate"]
    receipt = build.verify_build(candidate)
    assert receipt["proof_status"] == "not-run" and "passed" not in receipt
    expected = {*(f"bin/{name}.exe" for name in build.BINARIES), "LICENSE", "NOTICE",
                "native-codex-selection.patch", "THIRD-PARTY-NOTICES", "BUILD-INFO"}
    assert set(receipt["files"]) == expected
    assert {p.relative_to(candidate).as_posix() for p in candidate.rglob("*") if p.is_file()} == (
        expected | {"native-build-receipt.json"})
    for name, digest in receipt["files"].items():
        assert build.sha256(candidate / name) == digest
    assert receipt["identity"]["build_key"] == build.native_prepare.build_key(native_candidate["root"])
    info = json.loads((candidate / "BUILD-INFO").read_text())
    assert info["native_build_identity"] == receipt["identity"]
    assert "provider_proof_sha256" not in info


@pytest.mark.parametrize("name", sorted(build._PAYLOAD_FILES))
def test_native_candidate_corruption_is_refused(native_candidate, name):
    candidate = native_candidate["candidate"]
    path = candidate / name
    path.write_bytes(path.read_bytes() + b"corrupted")
    with pytest.raises(ValueError, match="checksum mismatch"):
        build.verify_build(candidate)


@pytest.mark.parametrize("name", build.native_prepare.NATIVE_INPUTS)
def test_native_candidate_refuses_changed_current_native_inputs(native_candidate, name, tmp_path):
    candidate = native_candidate["candidate"]
    path = native_candidate["root"] / name
    path.write_bytes(path.read_bytes() + b"changed native build")
    with pytest.raises(ValueError, match="identity"):
        build.verify_build(candidate)
    with pytest.raises(ValueError, match="identity"):
        build.package_candidate(candidate, tmp_path / "no-proof.json", tmp_path / "no-package")
    assert not (tmp_path / "no-package").exists()


@pytest.mark.parametrize("change", ["extra", "directory", "missing", "identity", "proof_claim", "bad_hash_list"])
def test_native_candidate_refuses_unexpected_contents_or_receipt(native_candidate, change):
    candidate = native_candidate["candidate"]
    receipt_path = candidate / "native-build-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    if change == "extra":
        (candidate / "unlisted.txt").write_text("unlisted")
    elif change == "directory":
        (candidate / "unlisted").mkdir()
    elif change == "missing":
        (candidate / "NOTICE").unlink()
    elif change == "identity":
        receipt["identity"]["target"] = "aarch64-pc-windows-msvc"
    elif change == "proof_claim":
        receipt["proof_status"] = "passed"
    else:
        receipt["files"].pop("NOTICE")
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        build.verify_build(candidate)


def test_native_candidate_rejects_symlink_even_when_target_hash_matches(native_candidate):
    candidate = native_candidate["candidate"]
    (candidate / "NOTICE").unlink()
    try:
        (candidate / "NOTICE").symlink_to(native_candidate["source"] / "NOTICE")
    except OSError as error:
        pytest.skip(f"Host cannot create a fixture symlink: {error}")
    with pytest.raises(ValueError, match="regular file"):
        build.verify_build(candidate)


@pytest.mark.parametrize("change", ["dos_only", "bad_offset", "bad_signature", "wrong_machine", "pe32", "dll", "section_bounds"])
def test_native_candidate_pe_headers_are_checked_even_with_updated_receipt(native_candidate, change):
    candidate = native_candidate["candidate"]
    binary = candidate / "bin/codex.exe"
    data = bytearray(binary.read_bytes())
    if change == "dos_only":
        data = bytearray(b"MZnot-a-PE")
    elif change == "bad_offset":
        struct.pack_into("<I", data, 60, len(data) + 1)
    elif change == "bad_signature":
        data[128:132] = b"NOPE"
    elif change == "wrong_machine":
        struct.pack_into("<H", data, 132, 0xaa64)
    elif change == "pe32":
        struct.pack_into("<H", data, 152, 0x010b)
    elif change == "dll":
        struct.pack_into("<H", data, 150, 0x2022)
    else:
        struct.pack_into("<I", data, 128 + 24 + 240 + 16, len(data) + 1)
    binary.write_bytes(data)
    receipt_path = candidate / "native-build-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["files"]["bin/codex.exe"] = build.sha256(binary)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="PE"):
        build.verify_build(candidate)


def test_snapshot_requires_current_patch_and_pinned_revision(native_candidate, tmp_path, monkeypatch):
    args = {name: native_candidate[name] for name in ("source", "patch", "release", "cargo_home")}
    args["output"] = tmp_path / "no-snapshot"
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "wrong-revision\n")
    with pytest.raises(ValueError, match="pinned upstream"):
        build.snapshot_build(**args)
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: build.UPSTREAM_REVISION + "\n")
    args["patch"] = tmp_path / "different.patch"
    args["patch"].write_text("not the current patch")
    with pytest.raises(ValueError, match="current native build inputs"):
        build.snapshot_build(**args)
    assert not args["output"].exists()


@pytest.mark.parametrize("change", ["missing", "incomplete", "failed", "sandbox", "binary"])
def test_candidate_packaging_refuses_missing_failed_or_unrelated_proof(native_candidate, tmp_path, change):
    candidate = native_candidate["candidate"]
    proof = tmp_path / "candidate-proof.json"
    report = passing_report(candidate / "bin/codex.exe")
    if change == "incomplete":
        report["cases"].pop()
    elif change == "failed":
        report["passed"] = False
    elif change == "sandbox":
        report["cases"][0]["windows_filesystem_sandbox_enforced"] = False
    elif change == "binary":
        report["binary_sha256"] = "0" * 64
    if change != "missing":
        proof.write_text(json.dumps(report))
    output = tmp_path / "no-package"
    with pytest.raises((ValueError, FileNotFoundError)):
        build.package_candidate(candidate, proof, output)
    assert not output.exists()


def test_candidate_can_be_reproved_after_python_changes_without_native_rebuild(native_candidate, tmp_path):
    candidate, root = native_candidate["candidate"], native_candidate["root"]
    original = build.verify_build(candidate)
    for name in ("scripts/build_native_codex_windows.py", "scripts/verify_native_codex_selection.py",
                 ".github/workflows/native-codex-windows.yml", "supervisor/runtime/codex.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Updated Python-only behavior\n")
    assert build.verify_build(candidate) == original
    proof = tmp_path / "new-proof.json"
    proof.write_text(json.dumps(passing_report(candidate / "bin/codex.exe")))
    output = tmp_path / "candidate-package"
    archive = build.package_candidate(candidate, proof, output)
    with tarfile.open(archive) as stream:
        names = set(stream.getnames())
        assert names == build._PAYLOAD_FILES | {"selection-manifest.json"}
        assert "native-build-receipt.json" not in names
        info = json.load(stream.extractfile("BUILD-INFO"))
        assert info["native_build_identity"] == original["identity"]
        assert info["provider_proof_sha256"] == build.sha256(proof)
        manifest = json.load(stream.extractfile("selection-manifest.json"))
        assert manifest["binary_sha256"] == original["files"]["bin/codex.exe"]
    assert build.verify_build(candidate) == original  # Packaging never rewrites the candidate.
