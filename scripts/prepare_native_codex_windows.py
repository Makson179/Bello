#!/usr/bin/env python3
"""Stdlib-only preparation and identity of the pinned Windows native build."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_REVISION = "be2951ea34f0d295ed0becf97079f92fa5f6950e"
VERSION = "0.155.1"
TARGET = "x86_64-pc-windows-msvc"
RUST_VERSION = "1.95.0"
V8_RELEASE = "rusty-v8-v150.4.0"
BUILD_PROFILE = {"release": True, "lto": False, "debug": 0, "codegen_units": 16}
BINARIES = ("codex", "codex-code-mode-host", "codex-command-runner", "codex-windows-sandbox-setup")
V8_HASHES = {
    f"rusty_v8_ptrcomp_sandbox_release_{TARGET}.lib.gz":
        "732ec5da4243aa166799780c8519a5eea6f32f6e47657a323342794dc3c239d6",
    f"src_binding_ptrcomp_sandbox_release_{TARGET}.rs":
        "dabf78ba1faac127660db9862b1d0354175c71b8db2d4fcb5bacbd9c93576b16",
}
NATIVE_INPUTS = (
    "scripts/native-codex-selection.patch",
    "scripts/prepare_native_codex_windows.py",
    ".github/workflows/native-codex-windows-build.yml",
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def native_inputs(root: Path | None = None) -> dict[str, str]:
    root = ROOT if root is None else root
    return {name: normalized_sha256(root / name) for name in NATIVE_INPUTS}


def build_key(root: Path | None = None) -> str:
    # Hash only named native inputs, never the proof, runtime, or packager.
    encoded = json.dumps(native_inputs(root), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_workspace_versions(text: str) -> str:
    """Repair upstream tag's source-less versions without resolving dependencies."""
    sections = text.split("[[package]]")
    for index, section in enumerate(sections[1:], 1):
        package = tomllib.loads("[[package]]" + section)["package"][0]
        if "source" not in package and package.get("version") == "0.0.0":
            sections[index] = re.sub(r'(?m)^version = "0\.0\.0"$',
                                     f'version = "{VERSION}"', section, count=1)
    return "[[package]]".join(sections)


def prepare(source: Path, patch: Path, *, expected_revision: str = UPSTREAM_REVISION) -> None:
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != expected_revision:
        raise ValueError("Native Codex source is not the pinned upstream revision")
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise ValueError("Preparation requires a fresh upstream checkout")
    # Git for Windows can check the patch out with CRLF, which breaks bare
    # context blanks. Normalize transport bytes, not the distributed file.
    patch_bytes = patch.read_bytes().replace(b"\r\n", b"\n")
    subprocess.run(["git", "-C", str(source), "apply", "--check", "-"], input=patch_bytes, check=True)
    subprocess.run(["git", "-C", str(source), "apply", "-"], input=patch_bytes, check=True)
    lock = source / "codex-rs" / "Cargo.lock"
    lock.write_text(normalize_workspace_versions(lock.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")


def verify_v8(directory: Path, *, expected_hashes: dict[str, str] | None = None) -> None:
    for name, expected in (V8_HASHES if expected_hashes is None else expected_hashes).items():
        if sha256(directory / name) != expected:
            raise ValueError(f"Pinned V8 checksum mismatch: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build-key")
    stage = sub.add_parser("prepare")
    stage.add_argument("--source", type=Path, required=True)
    stage.add_argument("--patch", type=Path, required=True)
    sub.add_parser("verify-v8").add_argument("--directory", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "build-key":
        print(build_key())
    else:
        {"prepare": prepare, "verify-v8": verify_v8}[command](**args)


if __name__ == "__main__":
    main()
