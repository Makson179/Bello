#!/usr/bin/env python3
"""Pinned Linux x86_64 build inputs; no compilation occurs in this helper."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_windows as common

UPSTREAM_REVISION = common.UPSTREAM_REVISION
VERSION = common.VERSION
RUST_VERSION = common.RUST_VERSION
V8_RELEASE = common.V8_RELEASE
BUILD_PROFILE = common.BUILD_PROFILE
TARGET = "x86_64-unknown-linux-gnu"
BINARIES = ("codex", "codex-code-mode-host", "bwrap")
# Official rusty-v8-v150.4.0 / rusty_v8_ptrcomp_sandbox_release_<target>.sha256.
V8_HASHES = {
    f"librusty_v8_ptrcomp_sandbox_release_{TARGET}.a.gz":
        "a35c75d1f26e6a983885a45b33490a4ebe54f05050568b32b89cfb421b30b583",
    f"src_binding_ptrcomp_sandbox_release_{TARGET}.rs":
        "7727826ae479bdb645e807239fb12d1f8e2e23de7a6cf16f5ee592690d1d8506",
}
NATIVE_INPUTS = (
    "scripts/native-codex-selection.patch",
    "scripts/prepare_native_codex_linux.py",
    "scripts/prepare_native_codex_windows.py",  # Shared source/lock preparation.
    ".github/workflows/native-codex-linux-build.yml",
)
sha256 = common.sha256
normalized_sha256 = common.normalized_sha256


def native_inputs(root: Path = ROOT) -> dict[str, str]:
    return {name: normalized_sha256(root / name) for name in NATIVE_INPUTS}


def build_key(root: Path = ROOT) -> str:
    payload = json.dumps(native_inputs(root), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def prepare(source: Path, patch: Path) -> None:
    common.prepare(source, patch, expected_revision=UPSTREAM_REVISION)


def verify_v8(directory: Path) -> None:
    common.verify_v8(directory, expected_hashes=V8_HASHES)


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
