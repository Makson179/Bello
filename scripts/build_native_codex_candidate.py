#!/usr/bin/env python3
"""Capture and verify unpublished 0.161.0 builds, using only the standard library.

These receipts describe compiled inputs, not proof success or an installable
release. They never change the legacy build profiles or published runtime pins.
"""
# ruff: noqa: E402 -- Direct CLI execution must add the repository before imports.

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_candidate as source_candidate
from scripts import prepare_native_codex_linux as linux_inputs
from scripts import prepare_native_codex_windows as windows_inputs
from scripts.build_native_codex_linux import _embedded_digest, validate_elf
from scripts.build_native_codex_windows import dependency_notices, validate_pe

SCHEMA = "bello.native-codex-candidate-build.v1"
RECEIPT = "native-build-receipt.json"
RECIPE_INPUTS = (
    "scripts/build_native_codex_candidate.py",
    "scripts/build_native_codex_linux.py",
    "scripts/build_native_codex_windows.py",
    "scripts/prepare_native_codex_linux.py",
    ".github/workflows/native-codex-candidate.yml",
)
TARGETS = {
    "linux-x64": {
        "rust_target": "x86_64-unknown-linux-gnu",
        "runner": "ubuntu-22.04",
        "executables": {
            "bin/codex": "codex",
            "bin/codex-code-mode-host": "codex-code-mode-host",
            "bin/codex-resources/bwrap": "bwrap",
        },
    },
    "windows-x64": {
        "rust_target": "x86_64-pc-windows-msvc",
        "runner": "windows-2025",
        "executables": {
            f"bin/{name}.exe": f"{name}.exe"
            for name in (
                "codex",
                "codex-code-mode-host",
                "codex-command-runner",
                "codex-windows-sandbox-setup",
            )
        },
    },
}
METADATA = frozenset(
    {
        "LICENSE",
        "NOTICE",
        "THIRD-PARTY-NOTICES",
        "Cargo.lock",
        "native-codex-selection.patch",
        "BUILD-INFO",
    }
)
sha256 = windows_inputs.sha256


def _profile(target: str) -> dict:
    if target not in TARGETS:
        raise ValueError("Candidate target must be linux-x64 or windows-x64")
    return TARGETS[target]


def _regular(path: Path, *, directory: bool = False, single_link: bool = True) -> None:
    status = path.lstat()
    if (
        stat.S_ISLNK(status.st_mode)
        or getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not (
            stat.S_ISDIR(status.st_mode) if directory else stat.S_ISREG(status.st_mode)
        )
        or (not directory and single_link and status.st_nlink != 1)
    ):
        raise ValueError(
            f"Candidate requires an ordinary {'directory' if directory else 'file'}: {path.name}"
        )


def _json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _read_json(path: Path) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate candidate receipt key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Invalid candidate JSON constant")

    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Candidate metadata exceeds its size limit")
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=pairs,
        parse_constant=constant,
    )


def _write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def identity(target: str, root: Path = ROOT) -> dict:
    """Include every preparation/build dependency, not mutable proof results."""
    profile = _profile(target)
    for name in (*source_candidate.INPUTS, *RECIPE_INPUTS):
        _regular(root / name)
    payload = {
        "schema": SCHEMA,
        "version": source_candidate.VERSION,
        "upstream_revision": source_candidate.UPSTREAM_REVISION,
        "source_identity": source_candidate.identity(root),
        "target": target,
        "rust_target": profile["rust_target"],
        "runner": profile["runner"],
        "rust_version": source_candidate.RUST_VERSION,
        "v8_release": source_candidate.V8_RELEASE,
        "v8_artifact_sha256": dict(
            linux_inputs.V8_HASHES
            if target == "linux-x64"
            else windows_inputs.V8_HASHES
        ),
        "profile": {
            "release": True,
            "lto": False,
            "debug": 0,
            "codegen_units": 16,
            "locked": True,
            "jobs": 3,
            "libsqlite3_flags": "SQLITE_DISABLE_INTRINSIC"
            if target == "windows-x64"
            else None,
        },
        "executables": dict(profile["executables"]),
        "recipe_inputs": {
            name: windows_inputs.normalized_sha256(root / name)
            for name in RECIPE_INPUTS
        },
        "published": False,
    }
    return {**payload, "build_key": hashlib.sha256(_json(payload)).hexdigest()}


def _git(source: Path, *args: str, env=None, data=None) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(source), *args], env=env, input=data
    )


def _verify_prepared_source(source: Path, patch: Path) -> None:
    """Compare the worktree with the exact patch+lock repair in a private index.

    Git objects produced for this check go to the temporary object directory;
    neither the source index nor its object database is modified.
    """
    _regular(source, directory=True)
    if (
        _git(source, "rev-parse", "HEAD").decode().strip()
        != source_candidate.UPSTREAM_REVISION
    ):
        raise ValueError("Candidate source is not the pinned upstream revision")
    objects = (
        _git(source, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        .decode()
        .strip()
    )
    # Windows checkout conversion may be set by system/global Git config, not
    # the repository. Preserve only that conversion rule while isolating all
    # unrelated global config from this private-index comparison.
    conversion = subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "config",
            "--type=bool-or-str",
            "--get",
            "core.autocrlf",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    autocrlf = conversion.stdout.strip() if conversion.returncode == 0 else "false"
    if conversion.returncode not in (0, 1) or autocrlf not in {
        "true",
        "false",
        "input",
    }:
        raise ValueError("Candidate source has an unsupported Git newline conversion")
    with tempfile.TemporaryDirectory(prefix="bello-candidate-source-") as temporary:
        private = Path(temporary)
        (private / "objects").mkdir()
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("GIT_")
        }
        env.update(
            {
                "GIT_INDEX_FILE": str(private / "index"),
                "GIT_OBJECT_DIRECTORY": str(private / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
            }
        )
        _git(source, "read-tree", "HEAD", env=env)
        _git(
            source,
            "apply",
            "--cached",
            "--whitespace=nowarn",
            "-",
            env=env,
            data=patch.read_bytes().replace(b"\r\n", b"\n"),
        )
        lock = _git(source, "show", ":codex-rs/Cargo.lock", env=env).decode("utf-8")
        repaired = windows_inputs.normalize_workspace_versions(
            lock, version=source_candidate.VERSION
        ).encode()
        blob = (
            _git(source, "hash-object", "-w", "--stdin", env=env, data=repaired)
            .decode()
            .strip()
        )
        _git(
            source,
            "update-index",
            "--cacheinfo",
            "100644",
            blob,
            "codex-rs/Cargo.lock",
            env=env,
        )
        different = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=" + autocrlf,
                "diff",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                "--",
            ],
            env=env,
            check=False,
        ).returncode
        if different or _git(source, "ls-files", "--others", env=env).strip():
            raise ValueError(
                "Candidate source differs from the exact patch and lockfile recipe"
            )


def _tree(candidate: Path, payload: set[str]) -> None:
    _regular(candidate, directory=True)
    directories = {
        str(parent)
        for name in payload
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    found = set()
    pending = [candidate]
    while pending:
        for path in pending.pop().iterdir():
            name = path.relative_to(candidate).as_posix()
            if name in directories:
                _regular(path, directory=True)
                pending.append(path)
            else:
                _regular(path)
                found.add(name)
    if found != payload:
        raise ValueError("Candidate must contain exactly the expected payload files")


def _info(build_identity: dict, files: dict[str, str]) -> dict:
    return {
        "schema": "bello.native-codex-candidate-build-info.v1",
        "native_build_identity": build_identity,
        "upstream_repository": "https://github.com/openai/codex",
        "upstream_tag": f"rust-v{source_candidate.VERSION}",
        "patch_sha256": files["native-codex-selection.patch"],
        "cargo_lock_sha256": files["Cargo.lock"],
        "bwrap_sha256": files.get("bin/codex-resources/bwrap"),
        "signed": False,
        "published": False,
        "proof_status": "not-run",
    }


def snapshot_build(
    target: str,
    source: Path,
    release: Path,
    cargo_home: Path,
    output: Path,
    *,
    root: Path = ROOT,
) -> Path:
    build_identity = identity(target, root)
    profile = _profile(target)
    patch = root / source_candidate.PATCH
    _verify_prepared_source(source, patch)
    _regular(release, directory=True)
    validator = validate_elf if target == "linux-x64" else validate_pe
    for name in profile["executables"].values():
        # Cargo can hard-link release binaries to deps; distributed copies below
        # must have a single link and can never alias the mutable Cargo cache.
        _regular(release / name, single_link=False)
        validator(release / name)
    if target == "linux-x64":
        bwrap_hash = sha256(release / "bwrap")
        if os.environ.get("CODEX_BWRAP_SHA256") != bwrap_hash or not _embedded_digest(
            release / "codex", bwrap_hash
        ):
            raise ValueError(
                "Candidate requires its final bundled bwrap digest embedded into Codex"
            )
    for name in ("LICENSE", "NOTICE", "codex-rs/Cargo.lock"):
        _regular(source / name)
    _regular(cargo_home, directory=True)
    notices = dependency_notices(cargo_home)
    if target == "linux-x64":
        copying = source / "codex-rs/vendor/bubblewrap/COPYING"
        _regular(copying)
        notices += "\nVendored bubblewrap COPYING\n" + copying.read_text(
            encoding="utf-8"
        )
    output.mkdir(parents=True, exist_ok=False)
    for destination, name in profile["executables"].items():
        path = output / destination
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(release / name, path)
        path.chmod(0o755)
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(source / name, output / name)
    shutil.copyfile(source / "codex-rs/Cargo.lock", output / "Cargo.lock")
    shutil.copyfile(patch, output / "native-codex-selection.patch")
    (output / "THIRD-PARTY-NOTICES").write_text(notices, encoding="utf-8", newline="\n")
    payload = set(profile["executables"]) | METADATA
    files = {name: sha256(output / name) for name in sorted(payload - {"BUILD-INFO"})}
    _write_json(output / "BUILD-INFO", _info(build_identity, files))
    files["BUILD-INFO"] = sha256(output / "BUILD-INFO")
    if identity(target, root) != build_identity:
        raise ValueError("Candidate inputs changed during build capture")
    _verify_prepared_source(source, patch)
    for original, copied in (
        ("LICENSE", "LICENSE"),
        ("NOTICE", "NOTICE"),
        ("codex-rs/Cargo.lock", "Cargo.lock"),
    ):
        if sha256(source / original) != files[copied]:
            raise ValueError("Candidate source metadata changed during capture")
    _write_json(
        output / RECEIPT,
        {
            "schema": SCHEMA,
            "identity": build_identity,
            "proof_status": "not-run",
            "files": files,
        },
    )
    verify_build(target, output, root=root)
    return output


def verify_build(
    target: str,
    candidate: Path,
    restore_executable_modes: bool = False,
    *,
    root: Path = ROOT,
) -> dict:
    profile = _profile(target)
    payload = set(profile["executables"]) | METADATA
    _tree(candidate, payload | {RECEIPT})
    receipt = _read_json(candidate / RECEIPT)
    expected = identity(target, root)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema", "identity", "proof_status", "files"}
        or receipt["schema"] != SCHEMA
        or receipt["proof_status"] != "not-run"
        or _json(receipt["identity"]) != _json(expected)
    ):
        raise ValueError(
            "Candidate build identity does not match the exact current recipe"
        )
    files = receipt["files"]
    if not isinstance(files, dict) or set(files) != payload:
        raise ValueError("Candidate receipt must hash every payload file")
    for name, digest in files.items():
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or sha256(candidate / name) != digest
        ):
            raise ValueError(f"Candidate payload checksum mismatch: {name}")
    validator = validate_elf if target == "linux-x64" else validate_pe
    for name in profile["executables"]:
        validator(candidate / name)
    if (
        windows_inputs.normalized_sha256(candidate / "native-codex-selection.patch")
        != expected["source_identity"]["inputs"][source_candidate.PATCH]
    ):
        raise ValueError("Candidate patch differs from the prepared source identity")
    if _json(_read_json(candidate / "BUILD-INFO")) != _json(_info(expected, files)):
        raise ValueError("Candidate BUILD-INFO does not match its payload and recipe")
    if target == "linux-x64" and not _embedded_digest(
        candidate / "bin/codex", files["bin/codex-resources/bwrap"]
    ):
        raise ValueError("Candidate Codex does not embed the bundled bwrap digest")
    # Artifact transport loses mode bits. Restore only known regular executable
    # copies after validating the entire inventory, identity and every byte.
    if restore_executable_modes:
        for name in profile["executables"]:
            (candidate / name).chmod(0o755)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    key = sub.add_parser("build-key")
    capture = sub.add_parser("snapshot-build")
    verify = sub.add_parser("verify-build")
    for command in (key, capture, verify):
        command.add_argument("--target", choices=TARGETS, required=True)
    for name in ("source", "release", "cargo-home", "output"):
        capture.add_argument("--" + name, type=Path, required=True)
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--restore-executable-modes", action="store_true")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "build-key":
        print(identity(**args)["build_key"])
    elif command == "snapshot-build":
        result = snapshot_build(**args)
        print(
            json.dumps(
                {
                    "candidate": str(result),
                    "build_key": identity(args["target"])["build_key"],
                    "proof_status": "not-run",
                }
            )
        )
    else:
        print(json.dumps(verify_build(**args), sort_keys=True))


if __name__ == "__main__":
    main()
