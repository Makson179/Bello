#!/usr/bin/env python3
"""Capture and prove a Linux native candidate; never publish or invent a pin."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
from unittest.mock import patch as replace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import prepare_native_codex_linux as native
from scripts.build_native_codex_windows import _regular, _write_json, dependency_notices, PROOF_CASES

sha256 = native.sha256
VERSION, TARGET = native.VERSION, native.TARGET
EXECUTABLES = {"bin/codex": "codex", "bin/codex-code-mode-host": "codex-code-mode-host",
               "bin/codex-resources/bwrap": "bwrap"}
PAYLOAD = frozenset({*EXECUTABLES, "LICENSE", "NOTICE", "native-codex-selection.patch",
                     "THIRD-PARTY-NOTICES", "BUILD-INFO"})
RECEIPT = "native-build-receipt.json"
SCHEMA = "bello.native-codex-linux-build.v1"
SANDBOX_SCHEMA = "bello.native-codex-linux-sandbox-proof.v1"


def validate_elf(path: Path) -> None:
    """Require a bounded 64-bit little-endian x86_64 ELF executable image."""
    _regular(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(64)
        if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
                or struct.unpack_from("<HHI", header, 16) not in {(2, 62, 1), (3, 62, 1)}):
            raise ValueError(f"Invalid Linux x86_64 ELF executable: {path.name}")
        phoff = struct.unpack_from("<Q", header, 32)[0]
        ehsize, phsize, phnum = struct.unpack_from("<HHH", header, 52)
        if ehsize != 64 or phsize != 56 or not 0 < phnum <= 1024 or phoff < 64 or phoff + phsize * phnum > size:
            raise ValueError(f"Invalid ELF program header bounds: {path.name}")
        stream.seek(phoff)
        executable = False
        for _ in range(phnum):
            entry = stream.read(phsize)
            kind, flags, offset = struct.unpack_from("<IIQ", entry)
            filesz, memsz = struct.unpack_from("<QQ", entry, 32)
            if offset + filesz > size or filesz > memsz:
                raise ValueError(f"Invalid ELF segment bounds: {path.name}")
            executable |= kind == 1 and bool(flags & 1)
        if not executable:
            raise ValueError(f"ELF has no executable load segment: {path.name}")


def _contains(path: Path, needle: bytes) -> bool:
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            chunk = tail + chunk
            if needle in chunk:
                return True
            tail = chunk[-len(needle):]
    return False


def _embedded_digest(path: Path, digest: str) -> bool:
    # Release optimization may retain the source hex or fold it to raw bytes;
    # actual enforcement is additionally required by the tampered-bwrap proof.
    return _contains(path, digest.encode()) or _contains(path, bytes.fromhex(digest))


def identity() -> dict:
    return {"build_key": native.build_key(ROOT), "inputs": native.native_inputs(ROOT),
            "upstream_revision": native.UPSTREAM_REVISION, "version": VERSION, "target": TARGET}


def _info(patch: Path, lock_hash: str, bwrap_hash: str) -> dict:
    return {"upstream_repository": "https://github.com/openai/codex",
            "upstream_revision": native.UPSTREAM_REVISION, "upstream_tag": f"rust-v{VERSION}",
            "target": TARGET, "rust_version": native.RUST_VERSION, "patch_sha256": sha256(patch),
            "cargo_lock_sha256": lock_hash,
            "cargo_lock_adjustment": f"Only source-less 0.0.0 workspace versions normalized to {VERSION}",
            "v8_release": native.V8_RELEASE, "v8_artifact_sha256": native.V8_HASHES,
            "bwrap_sha256": bwrap_hash, "profile": native.BUILD_PROFILE,
            "build_os": "ubuntu-22.04", "signed": False, "native_build_identity": identity()}


def _tree(root: Path) -> set[str]:
    _regular(root, directory=True)
    found = set()
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if name in {"bin", "bin/codex-resources"}:
            _regular(path, directory=True)
        else:
            _regular(path)
            found.add(name)
    return found


def snapshot_build(source: Path, patch: Path, release: Path, cargo_home: Path, output: Path) -> Path:
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != native.UPSTREAM_REVISION:
        raise ValueError("Native Codex source is not the pinned upstream revision")
    _regular(patch)
    if native.normalized_sha256(patch) != identity()["inputs"]["scripts/native-codex-selection.patch"]:
        raise ValueError("Snapshot patch differs from exact build inputs")
    _regular(release, directory=True)
    for name in EXECUTABLES.values():
        validate_elf(release / name)
    bwrap_hash = sha256(release / "bwrap")
    if os.environ.get("CODEX_BWRAP_SHA256") != bwrap_hash or not _embedded_digest(release / "codex", bwrap_hash):
        raise ValueError("Final bundled bwrap digest must be embedded into Codex before packaging")
    for name in ("LICENSE", "NOTICE", "codex-rs/Cargo.lock", "codex-rs/vendor/bubblewrap/COPYING"):
        _regular(source / name)
    notices = dependency_notices(cargo_home)
    notices += "\nVendored bubblewrap COPYING\n" + (source / "codex-rs/vendor/bubblewrap/COPYING").read_text()
    output.mkdir(parents=True, exist_ok=False)
    (output / "bin/codex-resources").mkdir(parents=True)
    for target, name in EXECUTABLES.items():
        shutil.copyfile(release / name, output / target)
        (output / target).chmod(0o755)
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(source / name, output / name)
    shutil.copyfile(patch, output / "native-codex-selection.patch")
    (output / "THIRD-PARTY-NOTICES").write_text(notices, encoding="utf-8")
    _write_json(output / "BUILD-INFO", _info(patch, sha256(source / "codex-rs/Cargo.lock"), bwrap_hash))
    _write_json(output / RECEIPT, {"schema": SCHEMA, "identity": identity(), "proof_status": "not-run",
                                 "files": {name: sha256(output / name) for name in sorted(PAYLOAD)}})
    verify_build(output)
    return output


def verify_build(candidate: Path, restore_executable_modes: bool = False) -> dict:
    if _tree(candidate) != PAYLOAD | {RECEIPT}:
        raise ValueError("Candidate must contain exactly the expected regular files")
    receipt = json.loads((candidate / RECEIPT).read_text())
    if (not isinstance(receipt, dict) or set(receipt) != {"schema", "identity", "proof_status", "files"}
            or receipt["schema"] != SCHEMA or receipt["identity"] != identity()
            or receipt["proof_status"] != "not-run"):
        raise ValueError("Candidate native build identity mismatch")
    hashes = receipt["files"]
    if not isinstance(hashes, dict) or set(hashes) != PAYLOAD:
        raise ValueError("Every candidate payload file must be hashed")
    for name, digest in hashes.items():
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest) or sha256(candidate / name) != digest:
            raise ValueError(f"Candidate checksum mismatch: {name}")
    for name in EXECUTABLES:
        validate_elf(candidate / name)
    patch = candidate / "native-codex-selection.patch"
    if native.normalized_sha256(patch) != identity()["inputs"]["scripts/native-codex-selection.patch"]:
        raise ValueError("Candidate patch differs from exact native inputs")
    info = json.loads((candidate / "BUILD-INFO").read_text())
    lock_hash = info.get("cargo_lock_sha256") if isinstance(info, dict) else None
    bwrap_hash = hashes["bin/codex-resources/bwrap"]
    if (not isinstance(lock_hash, str) or not re.fullmatch("[0-9a-f]{64}", lock_hash)
            or info != _info(patch, lock_hash, bwrap_hash)
            or not _embedded_digest(candidate / "bin/codex", bwrap_hash)):
        raise ValueError("Candidate BUILD-INFO or embedded bwrap digest mismatch")
    # GitHub artifact transport drops mode bits. Restore only known verified
    # executable files, after checking the entire candidate; never chmod links.
    if restore_executable_modes:
        for name in EXECUTABLES:
            (candidate / name).chmod(0o755)
    return receipt


def validate_proof(report: dict, binary: Path) -> None:
    if (report.get("schema") != "bello.native-selection-provider-proof.v1"
            or report.get("passed") is not True or type(report.get("paid_model_calls")) is not int
            or report["paid_model_calls"] != 0 or report.get("binary_sha256") != sha256(binary)
            or not str(report.get("platform", "")).startswith("Linux")):
        raise ValueError("A passing Linux proof for this exact executable is required")
    cases = report.get("cases")
    if (not isinstance(cases, list) or len(cases) != 9 or not all(isinstance(case, dict) for case in cases)
            or {case.get("case") for case in cases} != PROOF_CASES):
        raise ValueError("All nine distinct native provider-boundary cases are required")
    for case in cases:
        if (case.get("passed") is not True or case.get("exact_model_visible_output") is not True
                or case.get("focus_and_command_correct") is not True or case.get("error", "missing") is not None
                or type(case.get("provider_requests")) is not int or case["provider_requests"] != 2
                or case.get("provider_errors") != [] or case.get("external_proxy_requests_forwarded") != 0):
            raise ValueError(f"Incomplete Linux provider-boundary proof: {case.get('case')}")


def validate_sandbox_proof(report: dict, binary: Path) -> None:
    expected = {"schema": SANDBOX_SCHEMA, "passed": True, "binary_sha256": sha256(binary),
                "bwrap_sha256": sha256(binary.parent / "codex-resources/bwrap"),
                "inside_write_succeeded": True, "outside_write_denied": True,
                "new_user_namespace": True, "tampered_bwrap_exit_code": 8,
                "system_bwrap_on_path": False, "paid_model_calls": 0}
    if report != expected:
        raise ValueError("Exact bundled Linux sandbox proof is required")


def sandbox_proof(binary: Path, output: Path) -> dict:
    """Exercise real sandbox and digest denial using only new synthetic files."""
    if platform.system() != "Linux" or platform.machine().lower() != "x86_64":
        raise ValueError("Native sandbox proof requires Linux x86_64")
    binary = binary.resolve(strict=True)
    bwrap = binary.parent / "codex-resources/bwrap"
    validate_elf(binary)
    validate_elf(bwrap)
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    work, home, outside = (output / name for name in ("work", "empty-home", "outside"))
    for directory in (work, home, outside):
        directory.mkdir(mode=0o700)
    (home / "tmp").mkdir(mode=0o700)
    outside_file = outside / "unchanged.txt"
    outside_file.write_text("synthetic unchanged canary")
    parent_namespace = os.readlink("/proc/self/ns/user")
    script = (
        "import errno,json,os,pathlib; "
        "pathlib.Path('inside.txt').write_text('allowed'); "
        "result={'inside_write_succeeded':True, 'outside_write_denied':False, "
        f"'new_user_namespace':os.readlink('/proc/self/ns/user')!={parent_namespace!r}" + "};\n"
        "try:\n"
        f" pathlib.Path({str(outside_file)!r}).write_text('forbidden')\n"
        "except OSError as error:\n"
        " if error.errno not in (errno.EACCES,errno.EPERM,errno.EROFS): raise\n"
        " result['outside_write_denied']=True\n"
        "print(json.dumps(result))\n"
    )
    # Deliberately no system directories in PATH: only bundled bwrap may work.
    env = {"HOME": str(home), "CODEX_HOME": str(home), "PATH": str(binary.parent),
           "TMPDIR": str(home / "tmp"), "SHELL": "/bin/bash", "RUST_LOG": "warn",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    if shutil.which("bwrap", path=env["PATH"]) is not None:
        raise ValueError("Sandbox proof PATH unexpectedly contains system bwrap")
    flags = ["-c", 'sandbox_mode="workspace-write"', "-c", "features.use_legacy_landlock=false",
             "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
             "-c", "sandbox_workspace_write.exclude_slash_tmp=true", "sandbox", "--", sys.executable, "-c", script]
    result = subprocess.run([str(binary), *flags], cwd=work, env=env, text=True, capture_output=True, timeout=60)
    (output / "native-stdout.txt").write_text(result.stdout)
    (output / "native-stderr.txt").write_text(result.stderr)
    if result.returncode != 0:
        raise ValueError(f"Native Linux sandbox failed with exit code {result.returncode}")
    observation = json.loads(result.stdout.strip())
    if (observation != {"inside_write_succeeded": True, "outside_write_denied": True, "new_user_namespace": True}
            or outside_file.read_text() != "synthetic unchanged canary" or (work / "inside.txt").read_text() != "allowed"):
        raise ValueError("Native Linux filesystem/namespace isolation failed")
    # Only tamper a disposable copy; the verified candidate/cache is unchanged.
    copied = output / "tampered/bin/codex"
    (copied.parent / "codex-resources").mkdir(parents=True)
    shutil.copy2(binary, copied)
    tampered = copied.parent / "codex-resources/bwrap"
    shutil.copy2(bwrap, tampered)
    with tampered.open("ab") as stream:
        stream.write(b"synthetic digest tamper")
    env["PATH"] = str(copied.parent)
    denied = subprocess.run([str(copied), *flags], cwd=work, env=env, text=True, capture_output=True, timeout=60)
    (output / "tampered-stderr.txt").write_text(denied.stderr)
    report = {"schema": SANDBOX_SCHEMA, "passed": True, "binary_sha256": sha256(binary),
              "bwrap_sha256": sha256(bwrap), **observation, "tampered_bwrap_exit_code": denied.returncode,
              "system_bwrap_on_path": False, "paid_model_calls": 0}
    validate_sandbox_proof(report, binary)
    _write_json(output / "report.json", report)
    return report


def package_candidate(candidate: Path, proof: Path, sandbox_proof: Path, output: Path) -> Path:
    receipt = verify_build(candidate)
    binary = candidate / "bin/codex"
    validate_proof(json.loads(proof.read_text()), binary)
    validate_sandbox_proof(json.loads(sandbox_proof.read_text()), binary)
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "bundle"
    (bundle / "bin/codex-resources").mkdir(parents=True)
    for name in sorted(PAYLOAD):
        shutil.copyfile(candidate / name, bundle / name)
        if sha256(bundle / name) != receipt["files"][name]:
            raise ValueError(f"Candidate changed while packaging: {name}")
        (bundle / name).chmod(0o755 if name in EXECUTABLES else 0o644)
    info = json.loads((bundle / "BUILD-INFO").read_text())
    info.update({"provider_proof_sha256": sha256(proof), "sandbox_proof_sha256": sha256(sandbox_proof)})
    _write_json(bundle / "BUILD-INFO", info)
    files = {name: sha256(bundle / name) for name in sorted(PAYLOAD)}
    manifest = {"binary_sha256": files["bin/codex"], "files": files, "version": VERSION,
                "feature": "bello_native_selection", "protocol": 1, "transport_timeout_seconds": 315}
    manifest_path = bundle / "selection-manifest.json"
    _write_json(manifest_path, manifest)
    archive = output / f"bello-native-codex-{VERSION}-{TARGET}.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name in sorted(PAYLOAD | {"selection-manifest.json"}):
            path = bundle / name
            info = stream.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if name in EXECUTABLES else 0o644
            with path.open("rb") as content:
                stream.addfile(info, content)
    _write_json(output / "checksums.json", {"archive": archive.name, "archive_sha256": sha256(archive),
        "manifest_sha256": sha256(manifest_path), "proof_sha256": sha256(proof),
        "sandbox_proof_sha256": sha256(sandbox_proof), "published": False})
    return archive


def install_local(artifact: Path, runtime_root: Path, proof_output: Path) -> None:
    from supervisor.runtime import native_codex_install as installer
    from supervisor.runtime.codex_distiller import validate_native_selection

    if platform.system() != "Linux" or platform.machine().lower() != "x86_64":
        raise ValueError("Installed-artifact proof requires Linux x86_64")
    anchor = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    runtime_root = runtime_root.resolve()
    if runtime_root.exists() or runtime_root == anchor or not runtime_root.is_relative_to(anchor):
        raise ValueError("Installer proof requires a new child directory of RUNNER_TEMP")
    archive = artifact / f"bello-native-codex-{VERSION}-{TARGET}.tar.gz"
    checksums = json.loads((artifact / "checksums.json").read_text())
    if checksums.get("archive") != archive.name or checksums.get("archive_sha256") != sha256(archive):
        raise ValueError("Local archive checksum mismatch")
    bundle = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/ci-local-only/" + archive.name,
                                    checksums["archive_sha256"], checksums["manifest_sha256"])
    copies = []

    def local_download(spec, destination):
        if spec != bundle or copies:
            raise ValueError("Installed cache requested an unexpected second transfer")
        with archive.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
        if sha256(destination) != bundle.archive_sha256:
            raise ValueError("Copied archive checksum mismatch")
        copies.append(destination)

    environment = dict(os.environ)
    for name in ("BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
        environment.pop(name, None)
    environment.update({"BELLO_RUNTIME_DIR": str(runtime_root), "HOME": str(runtime_root / "empty-home"),
                        "CODEX_HOME": str(runtime_root / "empty-home")})
    with replace.dict(os.environ, environment, clear=True), replace.dict(
            installer.BUNDLES, {("Linux", "x86_64"): bundle}, clear=True), replace.object(installer, "_download", local_download):
        command, manifest = installer.ensure_native_selection()
        if installer.ensure_native_selection() != (command, manifest) or len(copies) != 1:
            raise ValueError("Native cache was not reused unchanged")
        installer._private_directory(runtime_root / "empty-home")
        capability = asyncio.run(validate_native_selection(command, manifest))
        receipt = {"schema": "bello.native-selection-installed-proof.v1", "passed": False,
            "archive_sha256": bundle.archive_sha256, "manifest_sha256": bundle.manifest_sha256,
            "binary_sha256": capability["binary_sha256"], "binary": command[0], "local_archive_transfers": 1,
            "cache_hit": True, "post_proof_cache_reusable": False, "offline_feature_validation": True, "published_pin": False}
        _write_json(runtime_root / "installed-cache.json", receipt)
        subprocess.run([sys.executable, str(ROOT / "scripts/verify_native_codex_selection.py"),
                        "--codex", command[0], "--output-dir", str(proof_output)], check=True)
        report = proof_output / "report.json"
        validate_proof(json.loads(report.read_text()), Path(command[0]))
        sandbox_output = proof_output.with_name(proof_output.name + "-sandbox")
        sandbox_proof(Path(command[0]), sandbox_output)
        if installer.ensure_native_selection() != (command, manifest) or len(copies) != 1:
            raise ValueError("Native cache was not reusable after actual execution")
        receipt.update({"passed": True, "post_proof_cache_reusable": True,
                        "provider_proof_sha256": sha256(report),
                        "sandbox_proof_sha256": sha256(sandbox_output / "report.json")})
        _write_json(runtime_root / "installed-cache.json", receipt)
        print(json.dumps(receipt), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {"snapshot-build": (snapshot_build, ("source", "patch", "release", "cargo-home", "output")),
                "verify-build": (verify_build, ("candidate",)),
                "sandbox-proof": (sandbox_proof, ("binary", "output")),
                "package-candidate": (package_candidate, ("candidate", "proof", "sandbox-proof", "output")),
                "install-local": (install_local, ("artifact", "runtime-root", "proof-output"))}
    for name, (_, arguments) in commands.items():
        command = sub.add_parser(name)
        for argument in arguments:
            command.add_argument("--" + argument, type=Path, required=True)
        if name == "verify-build":
            command.add_argument("--restore-executable-modes", action="store_true")
    args = vars(parser.parse_args())
    commands[args.pop("command")][0](**args)


if __name__ == "__main__":
    main()
