#!/usr/bin/env python3
"""Capture the separately qualified, unpublished macOS arm64 0.161.0 build.

This recipe is independent of the frozen Linux/Windows candidate identity.
Signatures are checked locally; neither Developer ID signing nor notarization
is claimed. All capture and verification operations require native Darwin.
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import build_native_codex_candidate as common
from scripts import prepare_native_codex_candidate as source_candidate
from scripts import prepare_native_codex_windows as inputs
from scripts.build_native_codex_windows import dependency_notices

SCHEMA = "bello.native-codex-macos-candidate-build.v1"
TARGET = "darwin-arm64"
RUST_TARGET = "aarch64-apple-darwin"
RECEIPT = common.RECEIPT
METADATA = common.METADATA
EXECUTABLES = {"bin/codex": "codex", "bin/codex-code-mode-host": "codex-code-mode-host"}
V8_HASHES = {
    "librusty_v8_ptrcomp_sandbox_release_aarch64-apple-darwin.a.gz":
        "00adbb48798848c77550441c68673a5e8529b8e1b73eabcdee232cb39b40f4a1",
    "src_binding_ptrcomp_sandbox_release_aarch64-apple-darwin.rs":
        "ca5adf0cf89c9a70ad460ae73648b2fe89b74aa113b3cb7f757b6a02b758394f",
}
RECIPE_INPUTS = (
    "scripts/build_native_codex_macos_candidate.py",
    "scripts/build_native_codex_candidate.py",
    "scripts/build_native_codex_linux.py",
    "scripts/build_native_codex_windows.py",
    "scripts/prepare_native_codex_linux.py",
    ".github/workflows/native-codex-macos-candidate.yml",
)
sha256 = inputs.sha256


def require_platform(target: str = TARGET) -> None:
    if target != TARGET or platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise ValueError("Native macOS arm64 is required")


def identity(target: str = TARGET, root: Path = ROOT) -> dict:
    if target != TARGET:
        raise ValueError("Only darwin-arm64 is supported by this separate recipe")
    for name in (*source_candidate.INPUTS, *RECIPE_INPUTS):
        common._regular(root / name)
    payload = {
        "schema": SCHEMA, "version": source_candidate.VERSION,
        "upstream_revision": source_candidate.UPSTREAM_REVISION,
        "source_identity": source_candidate.identity(root), "target": target,
        "rust_target": RUST_TARGET, "runner": "macos-15",
        "rust_version": source_candidate.RUST_VERSION, "v8_release": source_candidate.V8_RELEASE,
        "v8_artifact_sha256": V8_HASHES,
        "profile": {"release": True, "lto": False, "debug": 0,
                    "codegen_units": 16, "locked": True, "jobs": 3},
        "executables": EXECUTABLES,
        "recipe_inputs": {name: inputs.normalized_sha256(root / name) for name in RECIPE_INPUTS},
        "published": False,
    }
    return {**payload, "build_key": hashlib.sha256(common._json(payload)).hexdigest()}


def verify_v8(directory: Path) -> dict:
    common._regular(directory, directory=True)
    for name, expected in V8_HASHES.items():
        common._regular(directory / name)
        if sha256(directory / name) != expected:
            raise ValueError("Pinned macOS V8 artifact checksum mismatch")
    return dict(V8_HASHES)


def validate_macho(path: Path) -> None:
    """Require a bounded, thin arm64 executable with an embedded signature."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError("Truncated Mach-O header")
        magic, cpu, _, filetype, count, command_bytes, _, _ = struct.unpack("<8I", header)
        if (magic != 0xFEEDFACF or cpu != 0x0100000C or filetype != 2
                or not 1 <= count <= 4096 or not count * 8 <= command_bytes <= 1024 * 1024
                or 32 + command_bytes > size):
            raise ValueError("Expected a thin arm64 Mach-O executable")
        commands = stream.read(command_bytes)
    offset, signatures = 0, 0
    for _ in range(count):
        if offset + 8 > len(commands):
            raise ValueError("Truncated Mach-O load command")
        command, length = struct.unpack_from("<II", commands, offset)
        if length < 8 or length % 8 or offset + length > len(commands):
            raise ValueError("Invalid Mach-O load command length")
        if command == 0x1D:  # LC_CODE_SIGNATURE, not an assertion of trusted identity.
            if length != 16:
                raise ValueError("Invalid Mach-O signature command")
            start, length_bytes = struct.unpack_from("<II", commands, offset + 8)
            if start < 32 + command_bytes or not length_bytes or start + length_bytes > size:
                raise ValueError("Mach-O signature is outside its file")
            signatures += 1
        offset += length
    if offset != len(commands) or signatures != 1:
        raise ValueError("Mach-O requires exactly one bounded embedded signature")


def verify_signature(path: Path) -> None:
    require_platform()
    result = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)],
                            capture_output=True, timeout=30, check=False)
    if result.returncode != 0:
        raise ValueError("Native macOS signature verification failed")


def verify_build_environment() -> None:
    require_platform()
    expected = {"CARGO_BUILD_JOBS": "3", "CARGO_PROFILE_RELEASE_DEBUG": "0",
                "CARGO_PROFILE_RELEASE_LTO": "false", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "16"}
    if any(os.environ.get(name) != value for name, value in expected.items()):
        raise ValueError("Build profile differs from the pinned macOS recipe")
    for tool in ("rustc", "cargo"):
        result = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=30, check=True)
        if not result.stdout.startswith(f"{tool} {source_candidate.RUST_VERSION} "):
            raise ValueError("Native build toolchain version mismatch")
    for variable, name in zip(("RUSTY_V8_ARCHIVE", "RUSTY_V8_SRC_BINDING_PATH"), V8_HASHES):
        path = Path(os.environ.get(variable, ""))
        common._regular(path)
        if sha256(path) != V8_HASHES[name]:
            raise ValueError("Native build did not select the pinned V8 inputs")


def _info(build_identity: dict, files: dict) -> dict:
    return {"schema": "bello.native-codex-macos-candidate-build-info.v1",
            "native_build_identity": build_identity,
            "upstream_repository": "https://github.com/openai/codex",
            "upstream_tag": f"rust-v{source_candidate.VERSION}",
            "patch_sha256": files["native-codex-selection.patch"],
            "cargo_lock_sha256": files["Cargo.lock"],
            "signature_verification": "native-codesign-verify-strict",
            "developer_id_verified": False, "notarized": False,
            "published": False, "proof_status": "not-run"}


def snapshot_build(target: str, source: Path, release: Path, cargo_home: Path,
                   output: Path, *, root: Path = ROOT) -> Path:
    require_platform(target)
    verify_build_environment()
    build_identity = identity(target, root)
    patch = root / source_candidate.PATCH
    common._verify_prepared_source(source, patch)
    common._regular(release, directory=True)
    common._regular(cargo_home, directory=True)
    originals = {}
    for name in EXECUTABLES.values():
        common._regular(release / name, single_link=False)
        validate_macho(release / name)
        verify_signature(release / name)
        originals[name] = sha256(release / name)
    for name in ("LICENSE", "NOTICE", "codex-rs/Cargo.lock"):
        common._regular(source / name)
    notices = dependency_notices(cargo_home)
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin").mkdir()
    for destination, name in EXECUTABLES.items():
        shutil.copyfile(release / name, output / destination)
        (output / destination).chmod(0o755)
        if sha256(output / destination) != originals[name]:
            raise ValueError("Executable changed during build capture")
    for original, destination in (("LICENSE", "LICENSE"), ("NOTICE", "NOTICE"),
                                  ("codex-rs/Cargo.lock", "Cargo.lock")):
        shutil.copyfile(source / original, output / destination)
    shutil.copyfile(patch, output / "native-codex-selection.patch")
    (output / "THIRD-PARTY-NOTICES").write_text(notices, encoding="utf-8", newline="\n")
    files = {name: sha256(output / name) for name in sorted(set(EXECUTABLES) | METADATA - {"BUILD-INFO"})}
    common._write_json(output / "BUILD-INFO", _info(build_identity, files))
    files["BUILD-INFO"] = sha256(output / "BUILD-INFO")
    if identity(target, root) != build_identity:
        raise ValueError("Build inputs changed during capture")
    common._verify_prepared_source(source, patch)
    for original, destination in (("LICENSE", "LICENSE"), ("NOTICE", "NOTICE"),
                                  ("codex-rs/Cargo.lock", "Cargo.lock")):
        if sha256(source / original) != files[destination]:
            raise ValueError("Source metadata changed during capture")
    common._write_json(output / RECEIPT, {"schema": SCHEMA, "identity": build_identity,
                                        "proof_status": "not-run", "files": files})
    verify_build(target, output, root=root)
    return output


def verify_build(target: str, candidate: Path, restore_executable_modes: bool = False,
                 *, root: Path = ROOT) -> dict:
    require_platform(target)
    payload = set(EXECUTABLES) | METADATA
    common._tree(candidate, payload | {RECEIPT})
    receipt = common._read_json(candidate / RECEIPT)
    expected = identity(target, root)
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "identity", "proof_status", "files"}
            or receipt["schema"] != SCHEMA or receipt["proof_status"] != "not-run"
            or common._json(receipt["identity"]) != common._json(expected)):
        raise ValueError("macOS candidate identity differs from the exact recipe")
    files = receipt["files"]
    if not isinstance(files, dict) or set(files) != payload:
        raise ValueError("macOS receipt must hash every payload file")
    for name, digest in files.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256(candidate / name) != digest:
            raise ValueError("macOS candidate checksum mismatch")
    if inputs.normalized_sha256(candidate / "native-codex-selection.patch") != expected["source_identity"]["inputs"][source_candidate.PATCH]:
        raise ValueError("macOS candidate patch differs from prepared source")
    if common._json(common._read_json(candidate / "BUILD-INFO")) != common._json(_info(expected, files)):
        raise ValueError("macOS BUILD-INFO differs from its recipe and payload")
    for name in EXECUTABLES:
        validate_macho(candidate / name)
        verify_signature(candidate / name)
    if any(sha256(candidate / name) != digest for name, digest in files.items()):
        raise ValueError("macOS candidate changed during native signature verification")
    if restore_executable_modes:
        for name in EXECUTABLES:
            (candidate / name).chmod(0o755)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("build-key")
    capture = commands.add_parser("snapshot-build")
    verify = commands.add_parser("verify-build")
    v8 = commands.add_parser("verify-v8")
    v8.add_argument("--directory", type=Path, required=True)
    for command in (key, capture, verify):
        command.add_argument("--target", choices=(TARGET,), required=True)
    for name in ("source", "release", "cargo-home", "output"):
        capture.add_argument("--" + name, type=Path, required=True)
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--restore-executable-modes", action="store_true")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "build-key":
        print(identity(**args)["build_key"])
    elif command == "verify-v8":
        print(json.dumps(verify_v8(**args), sort_keys=True))
    elif command == "snapshot-build":
        result = snapshot_build(**args)
        print(json.dumps({"candidate": str(result), "proof_status": "not-run"}))
    else:
        print(json.dumps(verify_build(**args), sort_keys=True))


if __name__ == "__main__":
    main()
