#!/usr/bin/env python3
"""Prepare and package the pinned Windows native helper; never publish it.

The workflow builds the four executables between ``prepare`` and ``package``.
Packaging requires the real nine-case, zero-paid-call provider-boundary proof.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from unittest.mock import patch as replace


UPSTREAM_REVISION = "3d2ee51ca2d5db578f328aa75e20aa22c0197c9a"
ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.153.4"
TARGET = "x86_64-pc-windows-msvc"
BINARIES = ("codex", "codex-code-mode-host", "codex-command-runner", "codex-windows-sandbox-setup")
PROOF_CASES = frozenset({"direct_off", "direct_on", "code_off", "code_on", "poll_off", "poll_on",
                         "missing_focus", "task_protected", "help_protected"})
V8_HASHES = {
    f"rusty_v8_ptrcomp_sandbox_release_{TARGET}.lib.gz":
        "732ec5da4243aa166799780c8519a5eea6f32f6e47657a323342794dc3c239d6",
    f"src_binding_ptrcomp_sandbox_release_{TARGET}.rs":
        "dabf78ba1faac127660db9862b1d0354175c71b8db2d4fcb5bacbd9c93576b16",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalize_workspace_versions(text: str) -> str:
    """Repair upstream tag's source-less versions without resolving dependencies."""
    sections = text.split("[[package]]")
    for index, section in enumerate(sections[1:], 1):
        package = tomllib.loads("[[package]]" + section)["package"][0]
        if "source" not in package and package.get("version") == "0.0.0":
            sections[index] = re.sub(r'(?m)^version = "0\.0\.0"$',
                                     f'version = "{VERSION}"', section, count=1)
    return "[[package]]".join(sections)


def prepare(source: Path, patch: Path) -> None:
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("Native Codex source is not the pinned upstream revision")
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True)
    if dirty.strip():
        raise ValueError("Preparation requires a fresh upstream checkout")
    subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch.resolve())], check=True)
    subprocess.run(["git", "-C", str(source), "apply", str(patch.resolve())], check=True)
    lock = source / "codex-rs" / "Cargo.lock"
    lock.write_text(normalize_workspace_versions(lock.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")


def verify_v8(directory: Path) -> None:
    for name, expected in V8_HASHES.items():
        if sha256(directory / name) != expected:
            raise ValueError(f"Pinned V8 checksum mismatch: {name}")


def validate_proof(report: dict, binary: Path) -> None:
    if (report.get("schema") != "bello.native-selection-provider-proof.v1"
            or report.get("passed") is not True or report.get("paid_model_calls") != 0
            or report.get("binary_sha256") != sha256(binary)
            or not str(report.get("platform", "")).startswith("Windows")):
        raise ValueError("A passing Windows proof for this exact executable is required")
    cases = report.get("cases", [])
    if len(cases) != len(PROOF_CASES) or {case.get("case") for case in cases} != PROOF_CASES:
        raise ValueError("All nine distinct native provider-boundary cases are required")
    for case in cases:
        if (case.get("passed") is not True or case.get("exact_model_visible_output") is not True
                or case.get("focus_and_command_correct") is not True or case.get("error") is not None
                or case.get("provider_requests") != 2 or case.get("provider_errors") != []
                or case.get("external_proxy_requests_forwarded") != 0):
            raise ValueError(f"Incomplete native provider proof: {case.get('case')}")


def dependency_notices(cargo_home: Path) -> str:
    """Retain downloaded dependency notices, including platform/build-only ones."""
    roots = sorted(cargo_home.glob("registry/src/*/*")) + sorted(cargo_home.glob("git/checkouts/*/*"))
    notices = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if (not path.is_file() or path.is_symlink()
                    or not re.match(r"^(LICENSE|LICENCE|NOTICE|COPYING)(?:[._-].*)?$", path.name, re.I)):
                continue
            notices.append(f"\n{'=' * 72}\n{path.relative_to(cargo_home).as_posix()}\n\n"
                           + path.read_text(encoding="utf-8", errors="replace"))
    if not notices:
        raise ValueError("No downloaded third-party license notices found")
    return ("Third-party attribution for the locally rebuilt OpenAI Codex distribution.\n"
            "Conservative superset of downloaded platform, build and test dependency notices.\n"
            "Dependencies retain their respective licenses.\n" + "".join(notices))


def package(source: Path, patch: Path, release: Path, proof: Path, cargo_home: Path, output: Path) -> Path:
    report = json.loads(proof.read_text(encoding="utf-8"))
    validate_proof(report, release / "codex.exe")
    for name in BINARIES:
        path = release / f"{name}.exe"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing regular Windows executable: {path.name}")
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError(f"Not a Windows executable: {path.name}")
    notices = dependency_notices(cargo_home)
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "bundle"
    (bundle / "bin").mkdir(parents=True)
    for name in BINARIES:
        shutil.copyfile(release / f"{name}.exe", bundle / "bin" / f"{name}.exe")
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(source / name, bundle / name)
    shutil.copyfile(patch, bundle / "native-codex-selection.patch")
    (bundle / "THIRD-PARTY-NOTICES").write_text(notices, encoding="utf-8", newline="\n")
    build_info = {"upstream_repository": "https://github.com/openai/codex",
                  "upstream_revision": UPSTREAM_REVISION, "upstream_tag": f"rust-v{VERSION}",
                  "target": TARGET, "rust_version": "1.95.0", "patch_sha256": sha256(patch),
                  "cargo_lock_sha256": sha256(source / "codex-rs" / "Cargo.lock"),
                  "cargo_lock_adjustment": "Only source-less 0.0.0 workspace versions normalized to 0.153.4",
                  "v8_release": "rusty-v8-v150.4.0", "v8_artifact_sha256": V8_HASHES,
                  "profile": {"release": True, "lto": False, "debug": 0, "codegen_units": 16},
                  "provider_proof_sha256": sha256(proof), "signed": False}
    (bundle / "BUILD-INFO").write_text(json.dumps(build_info, indent=2) + "\n", encoding="utf-8")
    files = {path.relative_to(bundle).as_posix(): sha256(path) for path in sorted(bundle.rglob("*"))
             if path.is_file()}
    manifest = {"binary_sha256": files["bin/codex.exe"], "files": files, "version": VERSION,
                "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315,
                "transports": ["tcp-hmac-v1"]}
    manifest_path = bundle / "selection-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    archive = output / f"bello-native-codex-{VERSION}-{TARGET}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                info = stream.gettarinfo(str(path), arcname=path.relative_to(bundle).as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if path.suffix == ".exe" else 0o644
                with path.open("rb") as content:
                    stream.addfile(info, content)
    summary = {"archive": archive.name, "archive_sha256": sha256(archive),
               "manifest_sha256": sha256(manifest_path), "proof_sha256": sha256(proof), "published": False}
    (output / "checksums.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)
    return archive


def install_local(artifact: Path, runtime_root: Path, proof_output: Path) -> None:
    """Exercise the real private-cache installer, without publishing a bundle pin."""
    sys.path.insert(0, str(ROOT))
    from supervisor.runtime import native_codex_install as installer
    from supervisor.runtime.codex_distiller import validate_native_selection

    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("Installed-artifact proof requires Windows x64")
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    runtime_root = runtime_root.resolve()
    if (runtime_root == runner_temp or not runtime_root.is_relative_to(runner_temp)
            or runtime_root.exists()):
        raise ValueError("Installer proof requires a new child directory of RUNNER_TEMP")
    archive = artifact / f"bello-native-codex-{VERSION}-{TARGET}.tar.gz"
    checksums = json.loads((artifact / "checksums.json").read_text(encoding="utf-8"))
    if checksums.get("archive") != archive.name or checksums.get("archive_sha256") != sha256(archive):
        raise ValueError("Local artifact archive checksum mismatch")
    bundle = installer.NativeBundle(
        "https://github.com/Makson179/Bello/releases/download/ci-local-only/" + archive.name,
        checksums["archive_sha256"], checksums["manifest_sha256"])
    copies = []

    def local_download(spec, destination):
        if spec != bundle or copies:
            raise ValueError("Private cache unexpectedly requested another transfer")
        with archive.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        if sha256(destination) != spec.archive_sha256:
            raise ValueError("Copied artifact checksum mismatch")
        copies.append(destination)

    environment = dict(os.environ)
    for name in ("BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST", "OPENAI_API_KEY",
                 "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        environment.pop(name, None)
    environment.update({"BELLO_RUNTIME_DIR": str(runtime_root), "HOME": str(runtime_root / "empty-home"),
                        "USERPROFILE": str(runtime_root / "empty-home"),
                        "CODEX_HOME": str(runtime_root / "empty-home")})
    with replace.dict(os.environ, environment, clear=True), replace.dict(
            installer.BUNDLES, {("Windows", "x86_64"): bundle}, clear=True), replace.object(
            installer, "_download", local_download):
        command, manifest = installer.ensure_native_selection()
        if installer.ensure_native_selection() != (command, manifest) or len(copies) != 1:
            raise ValueError("Native installed cache was not reused unchanged")
        installer._private_directory(runtime_root / "empty-home")
        capability = asyncio.run(validate_native_selection(command, manifest))
        receipt = {"schema": "bello.native-selection-installed-proof.v1", "passed": False,
                   "archive_sha256": bundle.archive_sha256, "manifest_sha256": bundle.manifest_sha256,
                   "binary_sha256": capability["binary_sha256"], "binary": command[0],
                   "local_archive_transfers": len(copies), "cache_hit": True,
                   "offline_feature_validation": True, "published_pin": False}
        receipt_path = runtime_root / "installed-cache.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_native_codex_selection.py"),
                        "--codex", command[0], "--output-dir", str(proof_output)], check=True)
        report_path = proof_output / "report.json"
        validate_proof(json.loads(report_path.read_text(encoding="utf-8")), Path(command[0]))
        receipt.update({"passed": True, "provider_proof_sha256": sha256(report_path)})
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(receipt), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "package"):
        stage = sub.add_parser(command)
        stage.add_argument("--source", type=Path, required=True)
        stage.add_argument("--patch", type=Path, required=True)
        if command == "package":
            for name in ("release", "proof", "cargo-home", "output"):
                stage.add_argument(f"--{name}", type=Path, required=True)
    sub.add_parser("verify-v8").add_argument("--directory", type=Path, required=True)
    installed = sub.add_parser("install-local")
    for name in ("artifact", "runtime-root", "proof-output"):
        installed.add_argument(f"--{name}", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"prepare": prepare, "package": package, "verify-v8": verify_v8,
     "install-local": install_local}[command](**args)


if __name__ == "__main__":
    main()
