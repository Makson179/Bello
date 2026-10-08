#!/usr/bin/env python3
"""Prepare the explicit, unpublished Codex 0.161.0 source candidate only.

This helper neither downloads nor compiles anything. Its source-input identity
is not a binary manifest, proof receipt, or automatic-install pin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_windows as common

UPSTREAM_REVISION = "979011409de0a60b52f179721948e65531d26144"
VERSION = "0.161.0"
RUST_VERSION = "1.95.0"
V8_RELEASE = "rusty-v8-v150.4.0"
PATCH = "scripts/native-codex-0.161.0.patch"
INPUTS = (PATCH, "scripts/prepare_native_codex_candidate.py",
          "scripts/prepare_native_codex_windows.py")
SCHEMA = "bello.native-codex-source-candidate.v1"
ROOT_ROUNDTRIP_SOURCE_MARKERS = {
    "codex-rs/utils/path-uri/src/lib.rs": (
        "let url = with_canonical_windows_drive_root(url);",
        "fn with_canonical_windows_drive_root(mut url: Url) -> Url",
        "file_url_for_native_conversion(&self.0)",
    ),
    "codex-rs/protocol/src/permissions.rs": (
        "if path_uri_from_raw(raw_path.clone()).as_ref() == Ok(&path)",
        'Err("permission path cannot be represented losslessly".to_string())',
        "fn bello_permission_path_roundtrip_config_drive_roots()",
        "fn bello_permission_path_roundtrip_profile_preserves_read_write_and_deny()",
        "fn bello_permission_path_roundtrip_keeps_lossy_inputs_rejected()",
        "fn bello_permission_path_roundtrip_native_windows_root()",
    ),
}


def verify_root_roundtrip_source(source: Path) -> None:
    """Refuse missing repair/tests; exact source provenance is checked separately."""
    for name, markers in ROOT_ROUNDTRIP_SOURCE_MARKERS.items():
        path = source / name
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0) & 0x400):
            raise ValueError("Candidate root-roundtrip source must be a regular file")
        contents = path.read_text(encoding="utf-8")
        if any(marker not in contents for marker in markers):
            raise ValueError("Candidate root-roundtrip source contract is incomplete")


def identity(root: Path = ROOT) -> dict:
    """Hash the named source preparation inputs, never claim a built artifact."""
    inputs = {}
    for name in INPUTS:
        path = root / name
        status = path.lstat()
        if (not stat.S_ISREG(status.st_mode)
                or getattr(status, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError(f"Candidate input must be a regular file: {name}")
        inputs[name] = common.normalized_sha256(path)
    payload = {"schema": SCHEMA, "version": VERSION,
               "upstream_revision": UPSTREAM_REVISION,
               "rust_version": RUST_VERSION, "v8_release": V8_RELEASE,
               "inputs": inputs, "published": False,
               "qualification": "source-only; native build and proofs required"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "source_key": hashlib.sha256(encoded).hexdigest()}


def prepare(source: Path, *, root: Path = ROOT) -> dict:
    before = identity(root)
    # The git revision is verified by the shared helper. An archive imported
    # into a new local commit is deliberately not accepted as the official SHA.
    common.prepare(source, root / PATCH, expected_revision=UPSTREAM_REVISION, version=VERSION)
    if identity(root) != before:
        raise ValueError("Candidate preparation inputs changed during application")
    verify_root_roundtrip_source(source)
    return before


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity")
    stage = sub.add_parser("prepare")
    stage.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    result = identity() if args.command == "identity" else prepare(args.source)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
