#!/usr/bin/env python3
"""Capture, verify and package the pinned Windows native helper; never publish it.

An unproved build candidate survives Python/proof fixes without recompilation.
Packaging still requires all nine real zero-paid-call provider-boundary cases.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
from unittest.mock import patch as replace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_windows as native_prepare

UPSTREAM_REVISION = native_prepare.UPSTREAM_REVISION
VERSION = native_prepare.VERSION
TARGET = native_prepare.TARGET
BINARIES = native_prepare.BINARIES
V8_HASHES = native_prepare.V8_HASHES
normalize_workspace_versions = native_prepare.normalize_workspace_versions
sha256 = native_prepare.sha256
PROOF_CASES = frozenset({"direct_off", "direct_on", "code_off", "code_on", "poll_off", "poll_on",
                         "missing_focus", "task_protected", "help_protected"})
_PAYLOAD_FILES = frozenset({*(f"bin/{name}.exe" for name in BINARIES),
    "LICENSE", "NOTICE", "native-codex-selection.patch", "THIRD-PARTY-NOTICES", "BUILD-INFO"})
_RECEIPT_NAME = "native-build-receipt.json"
_RECEIPT_SCHEMA = "bello.native-codex-windows-build.v1"


def prepare(source: Path, patch: Path) -> None:
    # Compatibility for existing callers; the build workflow uses the small
    # stdlib-only preparation script directly.
    native_prepare.prepare(source, patch, expected_revision=UPSTREAM_REVISION)


def verify_v8(directory: Path) -> None:
    native_prepare.verify_v8(directory, expected_hashes=V8_HASHES)


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
                or case.get("windows_filesystem_sandbox_enforced") is not True
                or case.get("provider_requests") != 2 or case.get("provider_errors") != []
                or case.get("external_proxy_requests_forwarded") != 0):
            raise ValueError(f"Incomplete native provider proof: {case.get('case')}")
        # Native Windows uses ACL-backed private-file isolation. Public files
        # may already be readable by the sandbox account; do not claim a
        # universal default-deny read boundary from this narrower proof.
        probe = case.get("windows_filesystem_probe")
        private_hashes = case.get("windows_private_fixture_sha256")
        if (case.get("windows_filesystem_contract") != "native-acl-private-file-isolation-v1"
                or case.get("windows_arbitrary_public_path_read_confinement") != "not-covered"
                or not isinstance(probe, dict)
                or probe.get("schema") != "bello.windows-native-acl-probe.v1"
                or probe.get("inside_write_succeeded") is not True
                or type(probe.get("outside_public_read_succeeded")) is not bool
                or probe.get("outside_write_succeeded") is not False
                or probe.get("outside_private_read_succeeded") is not False
                or case.get("windows_filesystem_probe_error", "missing") is not None
                or not isinstance(private_hashes, dict)
                or not isinstance(private_hashes.get("before"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", private_hashes["before"])
                or private_hashes.get("after") != private_hashes["before"]):
            raise ValueError(f"Incomplete Windows filesystem proof: {case.get('case')}")


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


def _regular(path: Path, *, directory: bool = False) -> None:
    status = path.lstat()
    if (stat.S_ISLNK(status.st_mode)
            or getattr(status, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not (stat.S_ISDIR(status.st_mode) if directory else stat.S_ISREG(status.st_mode))):
        raise ValueError(f"Native build requires a regular {'directory' if directory else 'file'}: {path.name}")


def validate_pe(path: Path) -> None:
    """Validate the x64 executable header, not merely the DOS 'MZ' prefix."""
    _regular(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError(f"Invalid Windows PE executable: {path.name}")
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset < 64 or offset + 24 > size:
            raise ValueError(f"Invalid Windows PE header offset: {path.name}")
        stream.seek(offset)
        header = stream.read(24)
        machine, sections = struct.unpack_from("<HH", header, 4)
        optional_size, characteristics = struct.unpack_from("<HH", header, 20)
        optional = stream.read(optional_size)
        if (header[:4] != b"PE\x00\x00" or machine != 0x8664 or not 0 < sections <= 96
                or optional_size < 112 or len(optional) != optional_size or optional[:2] != b"\x0b\x02"
                or not characteristics & 0x0002 or characteristics & 0x2000
                or offset + 24 + optional_size + sections * 40 > size):
            raise ValueError(f"Invalid Windows x64 PE executable: {path.name}")
        for _ in range(sections):
            section = stream.read(40)
            raw_size, raw_offset = struct.unpack_from("<II", section, 16)
            if raw_size and (not raw_offset or raw_offset + raw_size > size):
                raise ValueError(f"Invalid Windows PE section bounds: {path.name}")


def native_build_identity() -> dict:
    return {"build_key": native_prepare.build_key(ROOT), "inputs": native_prepare.native_inputs(ROOT),
            "upstream_revision": UPSTREAM_REVISION, "version": VERSION, "target": TARGET}


def _build_info(patch: Path, cargo_lock_sha256: str) -> dict:
    return {"upstream_repository": "https://github.com/openai/codex",
            "upstream_revision": UPSTREAM_REVISION, "upstream_tag": f"rust-v{VERSION}",
            "target": TARGET, "rust_version": native_prepare.RUST_VERSION, "patch_sha256": sha256(patch),
            "cargo_lock_sha256": cargo_lock_sha256,
            "cargo_lock_adjustment": f"Only source-less 0.0.0 workspace versions normalized to {VERSION}",
            "v8_release": native_prepare.V8_RELEASE, "v8_artifact_sha256": V8_HASHES,
            "profile": native_prepare.BUILD_PROFILE, "signed": False}


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def snapshot_build(source: Path, patch: Path, release: Path, cargo_home: Path, output: Path) -> Path:
    """Capture compiled files now; this receipt deliberately makes no proof claim."""
    identity = native_build_identity()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError("Native Codex source is not the pinned upstream revision")
    _regular(patch)
    if native_prepare.normalized_sha256(patch) != identity["inputs"]["scripts/native-codex-selection.patch"]:
        raise ValueError("Snapshot patch differs from the current native build inputs")
    _regular(release, directory=True)
    for name in BINARIES:
        validate_pe(release / f"{name}.exe")
    for name in ("LICENSE", "NOTICE", "codex-rs/Cargo.lock"):
        _regular(source / name)
    notices = dependency_notices(cargo_home)
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin").mkdir()
    for name in BINARIES:
        shutil.copyfile(release / f"{name}.exe", output / "bin" / f"{name}.exe")
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(source / name, output / name)
    shutil.copyfile(patch, output / "native-codex-selection.patch")
    (output / "THIRD-PARTY-NOTICES").write_text(notices, encoding="utf-8", newline="\n")
    info = _build_info(output / "native-codex-selection.patch", sha256(source / "codex-rs/Cargo.lock"))
    info["native_build_identity"] = identity
    _write_json(output / "BUILD-INFO", info)
    receipt = {"schema": _RECEIPT_SCHEMA, "identity": identity, "proof_status": "not-run",
               "files": {name: sha256(output / name) for name in sorted(_PAYLOAD_FILES)}}
    _write_json(output / _RECEIPT_NAME, receipt)
    verify_build(output)
    print(json.dumps({"candidate": str(output), "build_key": identity["build_key"], "proof_status": "not-run"}), flush=True)
    return output


def verify_build(candidate: Path) -> dict:
    """Reject changed native inputs, extra files, links, bad PE headers or hashes."""
    _regular(candidate, directory=True)
    found = set()
    for path in candidate.iterdir():
        if path.name == "bin":
            _regular(path, directory=True)
            for executable in path.iterdir():
                _regular(executable)
                found.add("bin/" + executable.name)
        else:
            _regular(path)
            found.add(path.name)
    if found != _PAYLOAD_FILES | {_RECEIPT_NAME}:
        raise ValueError("Native build candidate must contain exactly its expected files")
    receipt = json.loads((candidate / _RECEIPT_NAME).read_text(encoding="utf-8"))
    expected_identity = native_build_identity()
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "identity", "proof_status", "files"}
            or receipt["schema"] != _RECEIPT_SCHEMA or receipt["proof_status"] != "not-run"
            or receipt["identity"] != expected_identity):
        raise ValueError("Native build identity does not match the current native inputs")
    hashes = receipt["files"]
    if not isinstance(hashes, dict) or set(hashes) != _PAYLOAD_FILES:
        raise ValueError("Native build receipt must hash every expected payload file")
    for name, expected in hashes.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or sha256(candidate / name) != expected:
            raise ValueError(f"Native build candidate checksum mismatch: {name}")
    for name in BINARIES:
        validate_pe(candidate / "bin" / f"{name}.exe")
    patch = candidate / "native-codex-selection.patch"
    if native_prepare.normalized_sha256(patch) != expected_identity["inputs"]["scripts/native-codex-selection.patch"]:
        raise ValueError("Native candidate patch differs from the current native build inputs")
    info = json.loads((candidate / "BUILD-INFO").read_text(encoding="utf-8"))
    lock_hash = info.get("cargo_lock_sha256") if isinstance(info, dict) else None
    if not isinstance(lock_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", lock_hash):
        raise ValueError("Invalid native build lockfile identity")
    expected_info = {**_build_info(patch, lock_hash), "native_build_identity": expected_identity}
    if info != expected_info:
        raise ValueError("Native BUILD-INFO does not match its build identity")
    return receipt


def package_candidate(candidate: Path, proof: Path, output: Path) -> Path:
    receipt = verify_build(candidate)
    validate_proof(json.loads(proof.read_text(encoding="utf-8")), candidate / "bin/codex.exe")
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "bundle"
    (bundle / "bin").mkdir(parents=True)
    for name in sorted(_PAYLOAD_FILES):
        shutil.copyfile(candidate / name, bundle / name)
        if sha256(bundle / name) != receipt["files"][name]:
            raise ValueError(f"Native candidate changed while packaging: {name}")
    info = json.loads((bundle / "BUILD-INFO").read_text(encoding="utf-8"))
    info["provider_proof_sha256"] = sha256(proof)
    _write_json(bundle / "BUILD-INFO", info)
    return _archive_bundle(bundle, proof, output)


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
    build_info = {**_build_info(patch, sha256(source / "codex-rs/Cargo.lock")), "provider_proof_sha256": sha256(proof)}
    (bundle / "BUILD-INFO").write_text(json.dumps(build_info, indent=2) + "\n", encoding="utf-8")
    return _archive_bundle(bundle, proof, output)


def _archive_bundle(bundle: Path, proof: Path, output: Path) -> Path:
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
    # RUNNER_TEMP can be a shared directory whose ACL is intentionally rejected
    # by the private-cache installer. A real per-user LOCALAPPDATA is another
    # fixture anchor; no parent ACL is modified or bypassed by this allowance.
    anchors = [Path(value).resolve(strict=True) for name in ("RUNNER_TEMP", "LOCALAPPDATA")
               if (value := os.environ.get(name))]
    runtime_root = runtime_root.resolve()
    if (runtime_root.exists() or not any(
            runtime_root != anchor and runtime_root.is_relative_to(anchor) for anchor in anchors)):
        raise ValueError("Installer proof requires a new child directory of RUNNER_TEMP or LOCALAPPDATA")
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
                   "post_proof_cache_reusable": False,
                   "offline_feature_validation": True, "published_pin": False}
        receipt_path = runtime_root / "installed-cache.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_native_codex_selection.py"),
                        "--codex", command[0], "--output-dir", str(proof_output)], check=True)
        report_path = proof_output / "report.json"
        validate_proof(json.loads(report_path.read_text(encoding="utf-8")), Path(command[0]))
        # Windows sandbox setup may change read ACLs on runtime executables.
        # Re-enter the actual installer after execution: the next Bello launch
        # must accept the same protected cache, with no repair or new transfer.
        if installer.ensure_native_selection() != (command, manifest) or len(copies) != 1:
            raise ValueError("Native installed cache was not reusable after provider proof")
        receipt.update({"passed": True, "post_proof_cache_reusable": True,
                        "provider_proof_sha256": sha256(report_path)})
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
    snapshot = sub.add_parser("snapshot-build")
    for name in ("source", "patch", "release", "cargo-home", "output"):
        snapshot.add_argument(f"--{name}", type=Path, required=True)
    sub.add_parser("verify-build").add_argument("--candidate", type=Path, required=True)
    candidate = sub.add_parser("package-candidate")
    for name in ("candidate", "proof", "output"):
        candidate.add_argument(f"--{name}", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    {"prepare": prepare, "package": package, "verify-v8": verify_v8,
     "install-local": install_local, "snapshot-build": snapshot_build,
     "verify-build": verify_build, "package-candidate": package_candidate}[command](**args)


if __name__ == "__main__":
    main()
