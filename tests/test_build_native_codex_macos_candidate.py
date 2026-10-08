"""Portable synthetic format/receipt tests, not native compilation claims."""
from __future__ import annotations

import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from scripts import build_native_codex_macos_candidate as build
from scripts import prepare_native_codex_candidate as prepare
from tests.test_build_native_codex_candidate import cargo_home, prepared_source  # noqa: F401


def macho():
    return struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 16, 0, 0) + struct.pack("<4I", 0x1D, 16, 48, 16) + b"synthetic-sign!!" + b"!"


@pytest.fixture
def recipe(tmp_path):
    root = tmp_path / "recipe"
    for name in (*prepare.INPUTS, *build.RECIPE_INPUTS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("fixture " + name + "\n").encode())
    (root / prepare.PATCH).write_bytes(
        b"diff --git a/changed.txt b/changed.txt\n--- a/changed.txt\n+++ b/changed.txt\n"
        b"@@ -1 +1 @@\n-before\n+after\n")
    return root


@pytest.fixture
def captured(tmp_path, recipe, prepared_source, cargo_home, monkeypatch):
    release = tmp_path / "release"
    release.mkdir()
    for name in build.EXECUTABLES.values():
        (release / name).write_bytes(macho())
    monkeypatch.setattr(build, "require_platform", lambda *a: None)
    monkeypatch.setattr(build, "verify_signature", lambda path: None)
    monkeypatch.setattr(build, "verify_build_environment", lambda: None)
    candidate = build.snapshot_build(build.TARGET, prepared_source, release, cargo_home,
                                     tmp_path / "candidate", root=recipe)
    return candidate


def test_capture_exact_inventory_and_no_install_or_publication_claim(captured, recipe):
    receipt = build.verify_build(build.TARGET, captured, root=recipe)
    assert receipt["schema"] == build.SCHEMA
    assert receipt["proof_status"] == "not-run"
    assert receipt["identity"]["target"] == "darwin-arm64"
    assert receipt["identity"]["published"] is False
    assert set(receipt["files"]) == set(build.EXECUTABLES) | build.METADATA
    info = build.common._read_json(captured / "BUILD-INFO")
    assert info["developer_id_verified"] is False and info["notarized"] is False
    assert info["signature_verification"] == "native-codesign-verify-strict"
    assert "selection-manifest.json" not in receipt["files"]


@pytest.mark.parametrize("name", [*build.EXECUTABLES, *sorted(build.METADATA)])
def test_every_distributed_byte_is_bound(captured, recipe, name):
    with (captured / name).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError):
        build.verify_build(build.TARGET, captured, root=recipe)


@pytest.mark.parametrize("mutation", ["extra", "missing", "hardlink", "symlink", "duplicate-key"])
def test_ambiguous_inventory_and_receipt_rejected(captured, recipe, tmp_path, mutation):
    if mutation == "extra":
        (captured / "extra").write_bytes(b"extra")
    elif mutation == "missing":
        (captured / "NOTICE").unlink()
    elif mutation == "hardlink":
        os.link(captured / "LICENSE", tmp_path / "alias")
    elif mutation == "symlink":
        original = captured / "LICENSE"
        external = tmp_path / "external"
        original.rename(external)
        try:
            original.symlink_to(external)
        except OSError:
            pytest.skip("Host does not allow unprivileged symlinks")
    else:
        path = captured / build.RECEIPT
        path.write_bytes(path.read_bytes().replace(b'{', b'{"schema":"duplicate",', 1))
    with pytest.raises((ValueError, FileNotFoundError)):
        build.verify_build(build.TARGET, captured, root=recipe)


def test_restore_modes_only_after_all_bytes_validated(captured, recipe):
    executable = captured / "bin/codex"
    executable.chmod(0o644)
    build.verify_build(build.TARGET, captured, True, root=recipe)
    if os.name != "nt":
        assert executable.stat().st_mode & 0o111 == 0o111
    executable.chmod(0o644)
    (captured / "NOTICE").write_bytes(b"modified")
    with pytest.raises(ValueError):
        build.verify_build(build.TARGET, captured, True, root=recipe)
    if os.name != "nt":
        assert executable.stat().st_mode & 0o111 == 0


@pytest.mark.parametrize("name", [*prepare.INPUTS, *build.RECIPE_INPUTS])
def test_each_build_input_changes_identity(recipe, name):
    before = build.identity(root=recipe)
    path = recipe / name
    path.write_bytes(path.read_bytes() + b"changed")
    assert build.identity(root=recipe)["build_key"] != before["build_key"]


@pytest.mark.parametrize("offset,value", [(0, 0xCAFEBABE), (4, 0x01000007), (12, 6), (16, 0),
                                        (16, 4097), (20, 1048577), (32, 0x19), (36, 8),
                                        (40, 8), (44, 0), (44, 1000)])
def test_macho_rejects_other_architectures_fat_dylibs_and_bad_signature_bounds(tmp_path, offset, value):
    data = bytearray(macho())
    struct.pack_into("<I", data, offset, value)
    path = tmp_path / "native"
    path.write_bytes(data)
    with pytest.raises(ValueError):
        build.validate_macho(path)


def test_macho_valid_fixture_and_truncation(tmp_path):
    path = tmp_path / "native"
    path.write_bytes(macho())
    build.validate_macho(path)
    path.write_bytes(macho()[:31])
    with pytest.raises(ValueError, match="Truncated"):
        build.validate_macho(path)


@pytest.mark.parametrize("system,machine,target", [("Linux", "arm64", build.TARGET),
    ("Darwin", "x86_64", build.TARGET), ("Darwin", "arm64", "linux-x64")])
def test_platform_gate_is_native_arm64_only(monkeypatch, system, machine, target):
    monkeypatch.setattr(build.platform, "system", lambda: system)
    monkeypatch.setattr(build.platform, "machine", lambda: machine)
    with pytest.raises(ValueError, match="Native macOS arm64"):
        build.require_platform(target)


def test_codesign_is_real_native_verification_and_never_leaks_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(build, "require_platform", lambda: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["capture_output"] and kwargs["timeout"] == 30
        return SimpleNamespace(returncode=1, stderr=b"do-not-serialize")
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(ValueError, match="^Native macOS signature verification failed$"):
        build.verify_signature(tmp_path / "binary")
    assert calls == [["/usr/bin/codesign", "--verify", "--strict", str(tmp_path / "binary")]]


@pytest.mark.parametrize("wrong", [None, "rustc", "cargo", "profile", "v8"])
def test_capture_requires_actual_toolchain_profile_and_selected_v8(tmp_path, monkeypatch, wrong):
    monkeypatch.setattr(build, "require_platform", lambda: None)
    hashes = {}
    for variable, name in zip(("RUSTY_V8_ARCHIVE", "RUSTY_V8_SRC_BINDING_PATH"), build.V8_HASHES):
        path = tmp_path / name
        path.write_bytes(b"synthetic " + name.encode())
        hashes[name] = build.sha256(path)
        monkeypatch.setenv(variable, str(path))
    monkeypatch.setattr(build, "V8_HASHES", hashes)
    for variable, value in {"CARGO_BUILD_JOBS": "3", "CARGO_PROFILE_RELEASE_DEBUG": "0",
        "CARGO_PROFILE_RELEASE_LTO": "false", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "16"}.items():
        monkeypatch.setenv(variable, value)
    def run(command, **kwargs):
        version = "1.0.0" if wrong == command[0] else prepare.RUST_VERSION
        return SimpleNamespace(stdout=f"{command[0]} {version} (fixture)\n", returncode=0)
    monkeypatch.setattr(build.subprocess, "run", run)
    if wrong == "profile":
        monkeypatch.setenv("CARGO_PROFILE_RELEASE_LTO", "true")
    if wrong == "v8":
        (tmp_path / next(iter(hashes))).write_bytes(b"bad")
    if wrong:
        with pytest.raises(ValueError):
            build.verify_build_environment()
    else:
        build.verify_build_environment()
        assert build.verify_v8(tmp_path) == hashes


def test_prepared_source_mismatch_blocks_capture(captured, recipe, prepared_source, cargo_home, tmp_path):
    (prepared_source / "changed.txt").write_bytes(b"unexpected source")
    with pytest.raises(ValueError, match="source differs"):
        build.snapshot_build(build.TARGET, prepared_source, tmp_path / "release", cargo_home,
                             tmp_path / "rejected", root=recipe)
    assert not (tmp_path / "rejected").exists()
