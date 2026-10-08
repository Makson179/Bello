#!/usr/bin/env python3
"""Package qualified 0.161.0 candidates and prove fresh private-cache installs.

This does not publish releases, change pins, or use provider credentials. Build
and qualification receipts are locally supplied evidence, not signed authority;
the release operator must separately authenticate their CI provenance.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION = "0.161.0"
SCHEMA = "bello.native-codex-release-package.v1"
INSTALL_SCHEMA = "bello.native-codex-local-install.v1"
TARGETS = {
    "linux-x64": ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
    "windows-x64": ("Windows", "x86_64", "x86_64-pc-windows-msvc"),
    "darwin-arm64": ("Darwin", "arm64", "aarch64-apple-darwin"),
}
METADATA = {"LICENSE", "NOTICE", "THIRD-PARTY-NOTICES", "native-codex-selection.patch", "BUILD-INFO"}
MAX_ARCHIVE = 1024 ** 3
MAX_UNPACKED = 2 * MAX_ARCHIVE
MAX_JSON = 8 * 1024 ** 2


def profile(target: str) -> tuple[str, str, str]:
    if target not in TARGETS:
        raise ValueError("Unsupported release target")
    return TARGETS[target]


def executables(target: str) -> set[str]:
    profile(target)
    if target == "windows-x64":
        return {f"bin/{name}.exe" for name in ("codex", "codex-code-mode-host", "codex-command-runner", "codex-windows-sandbox-setup")}
    return {"bin/codex", "bin/codex-code-mode-host"} | ({"bin/codex-resources/bwrap"} if target == "linux-x64" else set())


def payload(target: str) -> set[str]:
    return METADATA | executables(target) | {"selection-manifest.json"}


def regular(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise ValueError("Release inputs require ordinary, unshared files and directories")


def sha256(path: Path) -> str:
    regular(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _decode(raw: bytes) -> dict:
    if len(raw) > MAX_JSON:
        raise ValueError("Release evidence exceeds its bounded size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate release evidence field")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("Nonfinite release evidence")
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("Release evidence must be an object")
    return value


def read_json(path: Path) -> dict:
    regular(path)
    if path.stat().st_size > MAX_JSON:
        raise ValueError("Release evidence exceeds its bounded size")
    return _decode(path.read_bytes())


def write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def zero(value) -> bool:
    return type(value) is int and value == 0


def _modules(target: str):
    profile(target)
    from scripts import verify_native_codex_candidate as common
    if target == "darwin-arm64":
        from scripts import build_native_codex_macos_candidate as build
        from scripts import verify_native_codex_macos_candidate as proof
    else:
        from scripts import build_native_codex_candidate as build
        proof = common
    return build, proof, common


def installer_implementation() -> str:
    """Avoid the archive-hash -> published-pin -> archive-hash circularity.

    AST locates only literal URL/hash spans in two strictly static dictionaries,
    never executable expressions or a serialized AST (whose schema differs
    between Python versions). Keys, constructors, call syntax and every other
    byte remain bound. Installed proofs record the full raw source separately.
    """
    path = ROOT / "supervisor/runtime/native_codex_install.py"
    regular(path)
    raw = path.read_bytes()
    tree = ast.parse(raw.decode("utf-8"))
    # CPython AST column offsets are UTF-8 byte offsets, not character offsets.
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    replaced = []
    spans = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in {"BUNDLES", "ASYNC_BUNDLES"}:
            replaced.append(node.target.id)
            value = node.value
            if not isinstance(value, ast.Dict):
                raise ValueError("Installer pins must remain explicit dictionaries")
            keys = set()
            for key, call in zip(value.keys, value.values):
                if (not isinstance(key, ast.Tuple) or len(key.elts) != 2
                        or any(not isinstance(item, ast.Constant) or type(item.value) is not str for item in key.elts)):
                    raise ValueError("Installer platform keys must be static string tuples")
                platform_key = tuple(item.value for item in key.elts)
                if platform_key in keys or platform_key not in {entry[:2] for entry in TARGETS.values()}:
                    raise ValueError("Duplicate or unsupported installer platform key")
                keys.add(platform_key)
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "NativeBundle":
                    raise ValueError("Installer pins require the unchanged NativeBundle constructor")
                if len(call.args) == 3 and not call.keywords:
                    literals = call.args
                elif not call.args and len(call.keywords) == 3 and {item.arg for item in call.keywords} == {"url", "archive_sha256", "manifest_sha256"}:
                    literals = [item.value for item in call.keywords]
                else:
                    raise ValueError("Installer pin constructors require exactly three named or positional fields")
                if any(not isinstance(item, ast.Constant) or type(item.value) is not str for item in literals):
                    raise ValueError("Installer pin fields must be inert literal strings")
                for literal in literals:
                    spans.append((starts[literal.lineno - 1] + literal.col_offset,
                                  starts[literal.end_lineno - 1] + literal.end_col_offset))
            if keys != {entry[:2] for entry in TARGETS.values()}:
                raise ValueError("Installer pin tables must cover exactly the supported release platforms")
    if sorted(replaced) != ["ASYNC_BUNDLES", "BUNDLES"]:
        raise ValueError("Unexpected installer pin declarations")
    for start, end in sorted(spans, reverse=True):
        raw = raw[:start] + b"'__BELLO_RELEASE_PIN_LITERAL__'" + raw[end:]
    return hashlib.sha256(raw).hexdigest()


def packaging_inputs(target: str) -> dict[str, str]:
    names = {"scripts/release_native_codex_candidate.py",
             "supervisor/runtime/native_codex_layout.py", "supervisor/runtime/codex_distiller.py",
             "supervisor/filesystem_safety.py", "supervisor/state.py"}
    if target == "darwin-arm64":
        names |= {"scripts/build_native_codex_macos_candidate.py", "scripts/verify_native_codex_macos_candidate.py"}
    return {**{name: sha256(ROOT / name) for name in sorted(names)},
            "supervisor/runtime/native_codex_install.py#source-with-static-pin-literals-masked": installer_implementation()}


def _root(path: Path) -> Path:
    regular(path, directory=True)
    return path.resolve(strict=True)


def _new(path: Path, *inputs: Path) -> Path:
    if os.path.lexists(path):
        raise ValueError("Release output must be a new path")
    result = path.resolve()
    for source in (*inputs, ROOT):
        source = source.resolve()
        if result == source or result.is_relative_to(source) or source.is_relative_to(result):
            raise ValueError("Release output and inputs must be disjoint")
    return result


def _file(root: Path, name: str) -> Path:
    # Callers pass fixed paths or a previously validated exact inventory.
    path = root / name
    for parent in path.relative_to(root).parents:
        regular(root / parent, directory=True)
    regular(path)
    return path


def _guardian(value: dict, target: str) -> None:
    required = "groups" if target == "darwin-arm64" else "tree"
    rows = value.get("receipts")
    if not zero(value.get("watchdog_exit_code")) or not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Exactly one successful guardian receipt is required")
    row = rows[0]
    if (not isinstance(row, dict) or not zero(row.get("exit_code")) or row.get("fenced") is not True
            or row.get("scope") != required or row.get("stopped") is not False
            or type(row.get("owner_pid")) is not int or row["owner_pid"] <= 0):
        raise ValueError("Guardian cleanup was not proven at the platform's required scope")


def proof_names(target: str) -> set[str]:
    return {"selection", "async", "history", "modernbert"} | ({"sandbox"} if target == "linux-x64" else set())


def _case_paths(name: str, common) -> set[str]:
    cases = {"selection": common.SELECTION_CASES, "modernbert": common.MODERNBERT_CASES,
             "async": {"async_and_selection_direct", "async_and_selection_code"},
             "history": {"case"}, "sandbox": set()}[name]
    return {f"{case}/result.json" for case in cases}


def _case_hashes(directory: Path, name: str, common) -> dict[str, str]:
    expected = _case_paths(name, common)
    found = set()
    for parent, directories, files in os.walk(directory, followlinks=False):
        # No proof directory alias may hide or substitute a saved result.
        for child in directories:
            regular(Path(parent) / child, directory=True)
        if "result.json" in files:
            found.add((Path(parent) / "result.json").relative_to(directory).as_posix())
    if found != expected:
        raise ValueError("Proof case-file inventory is incomplete or unexpected")
    return {name: sha256(_file(directory, name)) for name in sorted(expected)}


def validate_suite(proofs: dict, root: Path, binary: Path, target: str) -> dict[str, str]:
    _, _, common = _modules(target)
    expected = proof_names(target)
    if not isinstance(proofs, dict) or set(proofs) != expected:
        raise ValueError("Every release proof, including real CPU ModernBERT, is required")
    counts = {"selection": 9, "async": 14, "history": 1, "modernbert": 6, "sandbox": 1}
    hashes = {}
    for name in sorted(expected):
        saved = proofs[name]
        report_path = _file(root, f"{name}/report.json")
        if (not isinstance(saved, dict) or saved.get("passed") is not True
                or saved.get("report") != f"{name}/report.json"
                or type(saved.get("cases")) is not int or saved["cases"] != counts[name]
                or saved.get("sha256") != sha256(report_path)
                or saved.get("case_results") != _case_hashes(report_path.parent, name, common)):
            raise ValueError("Proof report, case files, or counts differ from qualification")
        report = read_json(report_path)
        common.validate_proof(name, report, report_path.parent, binary, target)
        if not zero(report.get("paid_model_calls")):
            raise ValueError("Release proofs must not make paid model calls")
        if name in {"selection", "modernbert"}:
            rows = {row["case"]: row for row in report["cases"]}
            for relative in _case_paths(name, common):
                if read_json(_file(report_path.parent, relative)) != rows[relative.split("/")[0]]:
                    raise ValueError("Individual case report differs from its aggregate")
        elif name == "async":
            rows = {row["case"]: row for row in report["results"]}
            for label in ("async_and_selection_direct", "async_and_selection_code"):
                case = read_json(_file(report_path.parent, f"{label}/result.json"))
                if rows[label] != {**case, "case": label, "async_tools": True}:
                    raise ValueError("Async selection case differs from its aggregate")
        hashes[f"{name}/report.json"] = sha256(report_path)
        hashes.update({f"{name}/{path}": digest for path, digest in saved["case_results"].items()})
    return hashes


def validate_qualification(target: str, candidate: Path, qualification: Path) -> dict:
    build, proof, common = _modules(target)
    candidate, qualification = _root(candidate), _root(qualification)
    before = build.verify_build(target, candidate, restore_executable_modes=False)
    aggregate_path = _file(qualification, "qualification.json")
    worker_path = _file(qualification, "worker/qualification.json")
    aggregate, worker = read_json(aggregate_path), read_json(worker_path)
    binary = _file(candidate, "bin/codex.exe" if target == "windows-x64" else "bin/codex")
    receipt_hash = sha256(_file(candidate, "native-build-receipt.json"))
    scripts = proof.proof_inputs()
    for report in (aggregate, worker):
        if (report.get("schema") != proof.SCHEMA or report.get("target") != target
                or report.get("passed") is not True or report.get("published") is not False
                or report.get("phase") != "complete" or report.get("candidate_unchanged") is not True
                or report.get("modernbert_requested") is not True
                or not zero(report.get("paid_model_calls"))
                or not zero(report.get("external_provider_requests_forwarded"))
                or report.get("build_identity") != before["identity"]
                or report.get("candidate_files") != before["files"]
                or report.get("proof_inputs") != scripts or report.get("build_receipt_sha256") != receipt_hash
                or report.get("binary_sha256") != sha256(binary)):
            raise ValueError("Qualification is not passing, current, and bound to this exact build")
        if target == "darwin-arm64" and report.get("full_tree_recovery_proven") is not False:
            raise ValueError("macOS qualification must explicitly disclaim full-tree recovery")
        version = report.get("native_version", {})
        if (version.get("version") != VERSION or not zero(version.get("exit_code"))
                or version.get("binary_sha256") != sha256(binary)
                or not re.fullmatch(r"[0-9a-f]{64}", str(version.get("stdout_sha256", "")))):
            raise ValueError("Actual native version proof is missing")
    if (before.get("proof_status") != "not-run" or aggregate.get("worker_passed") is not True
            or aggregate.get("worker_report") != "worker/qualification.json"
            or aggregate.get("worker_report_sha256") != sha256(worker_path)):
        raise ValueError("Aggregate qualification is not bound to its worker")
    _guardian(aggregate.get("guardian", {}), target)
    hashes = validate_suite(worker.get("proofs"), qualification / "worker", binary, target)
    expected_aggregate = {name: {**item, "report": "worker/" + item["report"]} for name, item in worker["proofs"].items()}
    if aggregate.get("proofs") != expected_aggregate:
        raise ValueError("Aggregate proof references differ from its worker")
    if build.verify_build(target, candidate, restore_executable_modes=False) != before or proof.proof_inputs() != scripts:
        raise ValueError("Build or proof source changed during validation")
    return {"build": before, "build_receipt_sha256": receipt_hash,
            "qualification_sha256": sha256(aggregate_path), "worker_sha256": sha256(worker_path),
            "proof_files": hashes, "proof_inputs": scripts, "guardian_scope": "groups" if target == "darwin-arm64" else "tree"}


def _archive(bundle: Path, destination: Path, target: str) -> None:
    size = sum(_file(bundle, name).stat().st_size for name in payload(target))
    if size > MAX_UNPACKED:
        raise ValueError("Release payload exceeds installer size limit")
    with destination.open("xb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name in sorted(payload(target)):
                path = _file(bundle, name)
                info = tarfile.TarInfo(name)
                info.size = path.stat().st_size
                info.mode = 0o755 if name in executables(target) else 0o644
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
    if destination.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Release archive exceeds installer download size limit")


def package(target: str, candidate: Path, qualification: Path, output: Path) -> Path:
    candidate, qualification = _root(candidate), _root(qualification)
    output = _new(output, candidate, qualification)
    evidence = validate_qualification(target, candidate, qualification)
    inputs = packaging_inputs(target)
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / "bundle"
    bundle.mkdir()
    for name in sorted(payload(target) - {"selection-manifest.json"}):
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = _file(candidate, name)
        shutil.copyfile(source, path)
        if sha256(path) != evidence["build"]["files"][name]:
            raise ValueError("Candidate changed while copying release payload")
        path.chmod(0o755 if name in executables(target) else 0o644)
    info = read_json(bundle / "BUILD-INFO")
    info["release_provenance"] = {key: value for key, value in evidence.items() if key != "build"}
    info["release_provenance"]["packaging_inputs"] = inputs
    info["release_provenance"]["executable_modes"] = "known executable allowlist normalized to 0755; candidate bytes unchanged"
    # This one new output file is deliberately rewritten before it is hashed.
    (bundle / "BUILD-INFO").unlink()
    write_json(bundle / "BUILD-INFO", info)
    (bundle / "BUILD-INFO").chmod(0o644)
    files = {name: sha256(_file(bundle, name)) for name in sorted(payload(target) - {"selection-manifest.json"})}
    binary = "bin/codex.exe" if target == "windows-x64" else "bin/codex"
    manifest = {"version": VERSION, "feature": "bello_native_selection", "protocol": 1,
                "transport_timeout_seconds": 315, "binary_sha256": files[binary], "files": files}
    if target == "windows-x64":
        manifest["transports"] = ["tcp-hmac-v1"]
    write_json(bundle / "selection-manifest.json", manifest)
    (bundle / "selection-manifest.json").chmod(0o644)
    archive = output / f"bello-native-codex-{VERSION}-{profile(target)[2]}.tar.gz"
    _archive(bundle, archive, target)
    if validate_qualification(target, candidate, qualification) != evidence or packaging_inputs(target) != inputs:
        raise ValueError("Release inputs changed during packaging")
    write_json(output / "checksums.json", {"schema": SCHEMA, "target": target, "version": VERSION,
        "archive": archive.name, "archive_sha256": sha256(archive), "archive_size": archive.stat().st_size,
        "manifest_sha256": sha256(bundle / "selection-manifest.json"), "files": files,
        "build_identity": evidence["build"]["identity"], "build_receipt_sha256": evidence["build_receipt_sha256"],
        "qualification_sha256": evidence["qualification_sha256"], "packaging_inputs": inputs, "published": False})
    return archive


def verify_artifact(target: str, artifact: Path) -> tuple[dict, Path]:
    artifact = _root(artifact)
    checks = read_json(_file(artifact, "checksums.json"))
    name = f"bello-native-codex-{VERSION}-{profile(target)[2]}.tar.gz"
    archive = _file(artifact, name)
    if (checks.get("schema") != SCHEMA or checks.get("target") != target or checks.get("version") != VERSION
            or checks.get("archive") != name or checks.get("published") is not False
            or type(checks.get("archive_size")) is not int or checks["archive_size"] != archive.stat().st_size
            or not 0 < archive.stat().st_size <= MAX_ARCHIVE or checks.get("archive_sha256") != sha256(archive)
            or checks.get("packaging_inputs") != packaging_inputs(target)):
        raise ValueError("Artifact identity, source, or archive checksum mismatch")
    build, proof, _ = _modules(target)
    manifest, info = None, None
    hashes, total = {}, 0
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream:
            if (member.name not in payload(target) or member.name in hashes or not member.isfile()
                    or member.size < 1 or member.size > MAX_UNPACKED
                    or member.mode != (0o755 if member.name in executables(target) else 0o644)
                    or member.pax_headers or member.uid or member.gid or member.mtime or member.uname or member.gname):
                raise ValueError("Unsafe or unexpected release archive entry")
            total += member.size
            if total > MAX_UNPACKED:
                raise ValueError("Release archive exceeds unpacked size limit")
            content = stream.extractfile(member)
            if member.name in {"BUILD-INFO", "selection-manifest.json"}:
                if member.size > MAX_JSON:
                    raise ValueError("Release metadata too large")
                raw = content.read()
                value = _decode(raw)
                if member.name == "BUILD-INFO": info = value
                else: manifest = value
                hashes[member.name] = hashlib.sha256(raw).hexdigest()
            else:
                hashes[member.name] = hashlib.file_digest(content, "sha256").hexdigest()
    if set(hashes) != payload(target) or not isinstance(manifest, dict) or not isinstance(info, dict):
        raise ValueError("Release archive inventory is incomplete")
    files = {name: digest for name, digest in hashes.items() if name != "selection-manifest.json"}
    binary = "bin/codex.exe" if target == "windows-x64" else "bin/codex"
    expected_manifest = {"version": VERSION, "feature": "bello_native_selection", "protocol": 1,
        "transport_timeout_seconds": 315, "binary_sha256": files[binary], "files": files}
    if target == "windows-x64": expected_manifest["transports"] = ["tcp-hmac-v1"]
    provenance = info.get("release_provenance", {})
    if (not isinstance(provenance, dict)
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get(name, "")))
                   for name in ("build_receipt_sha256", "qualification_sha256", "worker_sha256"))):
        raise ValueError("Release provenance lacks exact build and qualification hashes")
    if (manifest != expected_manifest or checks.get("files") != files
            or checks.get("build_identity") != build.identity(target)
            or info.get("native_build_identity") != checks.get("build_identity")
            or checks.get("manifest_sha256") != hashes["selection-manifest.json"]
            or provenance.get("packaging_inputs") != packaging_inputs(target)
            or provenance.get("proof_inputs") != proof.proof_inputs()
            or provenance.get("build_receipt_sha256") != checks.get("build_receipt_sha256")
            or provenance.get("qualification_sha256") != checks.get("qualification_sha256")):
        raise ValueError("Release manifest or build/proof provenance mismatch")
    return checks, archive


def require_platform(target: str) -> None:
    system, machine, _ = profile(target)
    actual = platform.machine().lower()
    if platform.system() != system or actual not in ({"amd64", "x86_64"} if machine == "x86_64" else {"arm64", "aarch64"}):
        raise ValueError("Installed release proof requires its actual native platform")


@contextmanager
def local_bundle(installer, target: str, checks: dict, archive: Path):
    """Replace only the public bundle choice and byte-transfer source."""
    bundle = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/local-unpublished/" + archive.name,
                                    checks["archive_sha256"], checks["manifest_sha256"])
    original = installer._download, dict(installer.BUNDLES), dict(installer.ASYNC_BUNDLES)
    transfers = []
    def copy(spec, destination):
        if spec != bundle or transfers:
            raise ValueError("Unexpected second local archive transfer")
        if sha256(archive) != bundle.archive_sha256:
            raise ValueError("Local release archive changed before install")
        with archive.open("rb") as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
        if sha256(destination) != bundle.archive_sha256:
            raise ValueError("Local transfer checksum mismatch")
        transfers.append(True)
    try:
        installer._download = copy
        key = profile(target)[:2]
        installer.BUNDLES.clear()
        installer.BUNDLES[key] = bundle
        installer.ASYNC_BUNDLES.clear()
        installer.ASYNC_BUNDLES[key] = bundle
        yield bundle, transfers
    finally:
        installer._download = original[0]
        installer.BUNDLES.clear()
        installer.BUNDLES.update(original[1])
        installer.ASYNC_BUNDLES.clear()
        installer.ASYNC_BUNDLES.update(original[2])


def _cache(directory: Path, target: str) -> dict:
    return {name: {"sha256": sha256(_file(directory, name)), "size": (directory / name).stat().st_size,
                   "mode": stat.S_IMODE((directory / name).stat().st_mode)} for name in sorted(payload(target))}


def published_bundle(target: str) -> dict:
    from supervisor.runtime import native_codex_install as installer
    key = profile(target)[:2]
    bundle = installer.BUNDLES.get(key)
    if bundle is None or installer.ASYNC_BUNDLES.get(key) != bundle:
        raise ValueError("Selection and async must pin the same published release")
    expected = f"bello-native-codex-{VERSION}-{profile(target)[2]}.tar.gz"
    if (not bundle.url.startswith("https://github.com/Makson179/Bello/releases/download/")
            or bundle.url.rsplit("/", 1)[-1] != expected
            or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (bundle.archive_sha256, bundle.manifest_sha256))):
        raise ValueError("Published proof requires real pinned 0.161.0 archive hashes")
    return {"archive_sha256": bundle.archive_sha256, "manifest_sha256": bundle.manifest_sha256, "url": bundle.url}


@contextmanager
def _distribution(installer, target: str, checks: dict, archive: Path | None):
    if archive is None:
        # Production downloader and both production pin tables remain untouched.
        if published_bundle(target) != checks:
            raise ValueError("Published pins changed")
        yield installer.BUNDLES[profile(target)[:2]], []
    else:
        with local_bundle(installer, target, checks, archive) as value:
            yield value


async def _install_worker(target: str, artifact: Path | None, runtime_root: Path, output: Path) -> dict:
    from supervisor.process_fence import configure_worker, is_guarded_worker
    configure_worker()
    if not is_guarded_worker():
        raise RuntimeError("Installed proof worker requires its authenticated production guardian")
    require_platform(target)
    _, proof, common = _modules(target)
    artifact = _root(artifact) if artifact is not None else None
    inputs = (artifact,) if artifact else ()
    runtime_root = _new(runtime_root, *inputs, output)
    output = _new(output, *inputs, runtime_root)
    checks, archive = verify_artifact(target, artifact) if artifact else (published_bundle(target), None)
    output.mkdir(parents=True, exist_ok=False)
    result = {"schema": INSTALL_SCHEMA, "passed": False, "published": artifact is None, "target": target,
              "paid_model_calls": 0, "external_provider_requests_forwarded": 0, "phase": "install", "proofs": {},
              "archive_sha256": checks["archive_sha256"], "manifest_sha256": checks["manifest_sha256"],
              "packaging_inputs": packaging_inputs(target), "proof_inputs": proof.proof_inputs(),
              "installer_source_sha256": sha256(ROOT / "supervisor/runtime/native_codex_install.py")}
    try:
        with common.isolated_environment(output):
            os.environ["BELLO_RUNTIME_DIR"] = str(runtime_root)
            from supervisor.runtime import native_codex_install as installer
            from supervisor.runtime.codex_distiller import validate_native_selection
            with _distribution(installer, target, checks, archive) as (bundle, transfers):
                command, manifest = installer.ensure_native_selection()
                if manifest is None:
                    raise ValueError("Installed manifest missing")
                binary = Path(command[0])
                before = _cache(manifest.parent, target)
                def unchanged():
                    if (installer.ensure_native_selection() != (command, manifest)
                            or installer.ensure_native_async() != (command, manifest)
                            or len(transfers) != (1 if artifact else 0) or _cache(manifest.parent, target) != before):
                        raise ValueError("Private installed cache changed or was not reused")
                unchanged()
                await validate_native_selection(command, manifest)
                result.update(cold_cache=True, cache_hit=True, local_archive_transfers=1 if artifact else 0,
                    distribution_source="local-archive" if artifact else "production-https",
                    binary_sha256=sha256(binary), native_version=common.native_version(binary), cache_files=before)
                for name in sorted(proof_names(target)):
                    result["phase"] = name
                    unchanged()
                    report = await common.run_proof(name, binary, output / name)
                    if report != read_json(output / name / "report.json"):
                        raise ValueError("Installed proof and durable report differ")
                    common.validate_proof(name, report, output / name, binary, target)
                    unchanged()
                    result["proofs"][name] = {"passed": True, "report": f"{name}/report.json",
                        "sha256": sha256(output / name / "report.json"), "case_results": _case_hashes(output / name, name, common),
                        "cases": len(report.get("cases", report.get("results", [report])))}
                validate_suite(result["proofs"], output, binary, target)
                unchanged()
                final_checks = verify_artifact(target, artifact)[0] if artifact else published_bundle(target)
                if (final_checks != checks or proof.proof_inputs() != result["proof_inputs"]
                        or sha256(ROOT / "supervisor/runtime/native_codex_install.py") != result["installer_source_sha256"]):
                    raise ValueError("Release or proof source changed during installed verification")
                result.update(passed=True, phase="complete", cache_unchanged=True,
                    post_proof_cache_reusable=True, private_cache_validation=True, offline_feature_validation=True,
                    installed_directory=str(manifest.parent))
    except Exception as exc:
        result.update(error_type=type(exc).__name__, failure_code=result["phase"] + "_failed")
    write_json(output / "report.json", result)
    return result


def install_release(target: str, artifact: Path | None, runtime_root: Path, output: Path, *, modernbert: bool) -> dict:
    from supervisor import watchdog
    _, proof, common = _modules(target)
    require_platform(target)
    if not modernbert:
        raise ValueError("Release installation requires --modernbert")
    artifact = _root(artifact) if artifact is not None else None
    inputs = (artifact,) if artifact else ()
    runtime_root = _new(runtime_root, *inputs, output)
    output = _new(output, *inputs, runtime_root)
    checks = verify_artifact(target, artifact)[0] if artifact else published_bundle(target)
    installer_source = sha256(ROOT / "supervisor/runtime/native_codex_install.py")
    output.mkdir(parents=True, exist_ok=False)
    control = output / "guardian-control"
    control.mkdir(mode=0o700)
    worker = output / "worker"
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "install-local" if artifact else "install-published", "--install-worker",
               "--target", target, "--runtime-root", str(runtime_root),
               "--output", str(worker), "--modernbert"]
    if artifact:
        command += ["--artifact", str(artifact)]
    receipts = []
    original = watchdog._run_guarded
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        receipts.append({key: result.get(key) for key in ("exit_code", "fenced", "scope", "owner_pid", "stopped")})
        return result
    result = {"schema": INSTALL_SCHEMA, "passed": False, "published": artifact is None, "target": target,
              "paid_model_calls": 0, "external_provider_requests_forwarded": 0, "phase": "guardian",
              "guardian": {"receipts": receipts}, "archive_sha256": checks["archive_sha256"],
              "manifest_sha256": checks["manifest_sha256"], "installer_source_sha256": installer_source}
    try:
        watchdog._run_guarded = observe
        with common.isolated_environment(output):
            os.environ["PYTHONPATH"] = str(ROOT)
            code = watchdog.watch_command(command, project_root=control, maximum_restarts=0, backoff=(),
                required_scope="groups" if target == "darwin-arm64" else "tree",
                disposition=lambda _: {"eligible": False, "reason": "release qualification is never retried"}, report=lambda _: None)
        result["guardian"]["watchdog_exit_code"] = code
        _guardian(result["guardian"], target)
        child = read_json(_file(worker, "report.json"))
        if (child.get("schema") != INSTALL_SCHEMA or child.get("target") != target or child.get("passed") is not True
                or child.get("published") is not (artifact is None) or child.get("phase") != "complete"
                or not zero(child.get("paid_model_calls")) or not zero(child.get("external_provider_requests_forwarded"))
                or child.get("archive_sha256") != checks["archive_sha256"]
                or child.get("manifest_sha256") != checks["manifest_sha256"]
                or child.get("proof_inputs") != proof.proof_inputs() or child.get("packaging_inputs") != packaging_inputs(target)
                or type(child.get("local_archive_transfers")) is not int
                or child["local_archive_transfers"] != (1 if artifact else 0)
                or child.get("distribution_source") != ("local-archive" if artifact else "production-https")
                or child.get("installer_source_sha256") != installer_source
                or any(child.get(name) is not True for name in ("cold_cache", "cache_hit", "cache_unchanged", "post_proof_cache_reusable",
                    "private_cache_validation", "offline_feature_validation"))):
            raise ValueError("Installed worker did not prove the complete cold/reused private cache")
        directory = Path(child["installed_directory"])
        expected_directory = runtime_root / "native-codex" / checks["archive_sha256"]
        if directory != expected_directory:
            raise ValueError("Installed proof references an unexpected cache")
        binary = directory / ("bin/codex.exe" if target == "windows-x64" else "bin/codex")
        version = child.get("native_version", {})
        if (not isinstance(version, dict) or version.get("version") != VERSION
                or not zero(version.get("exit_code")) or version.get("binary_sha256") != sha256(binary)):
            raise ValueError("Installed worker native version is not the exact release binary")
        from supervisor.runtime import native_codex_install as installer
        spec = installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/local-unpublished/unused", checks["archive_sha256"], checks["manifest_sha256"])
        installer._verify(directory, spec, system=profile(target)[0])
        if _cache(directory, target) != child.get("cache_files") or sha256(binary) != child.get("binary_sha256"):
            raise ValueError("Installed bytes changed after guardian completion")
        validate_suite(child.get("proofs"), worker, binary, target)
        final_checks = verify_artifact(target, artifact)[0] if artifact else published_bundle(target)
        if final_checks != checks or sha256(ROOT / "supervisor/runtime/native_codex_install.py") != installer_source:
            raise ValueError("Release archive changed after guardian completion")
        result.update(passed=True, phase="complete", worker_report="worker/report.json", worker_report_sha256=sha256(worker / "report.json"),
            candidate_unchanged=True, cache_unchanged=True, proofs=child["proofs"],
            guardian_scope="groups" if target == "darwin-arm64" else "tree",
            distribution_source="local-archive" if artifact else "production-https",
            not_covered=(["public HTTPS download"] if artifact else []) + ["live model quality", "arbitrary external writable roots"]
                + (["full process-tree cleanup or whole-controller recovery on macOS"] if target == "darwin-arm64" else []))
    except Exception as exc:
        result.update(error_type=type(exc).__name__, failure_code="guardian_or_installed_proof_failed")
    finally:
        watchdog._run_guarded = original
        write_json(output / "installed-proof.json", result)
    return result


def install_local(target: str, artifact: Path, runtime_root: Path, output: Path, *, modernbert: bool) -> dict:
    return install_release(target, artifact, runtime_root, output, modernbert=modernbert)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    packing = sub.add_parser("package")
    installing = sub.add_parser("install-local")
    published = sub.add_parser("install-published")
    for command in (packing, installing, published):
        command.add_argument("--target", choices=TARGETS, required=True)
        command.add_argument("--output", type=Path, required=True)
    packing.add_argument("--candidate", type=Path, required=True)
    packing.add_argument("--qualification", type=Path, required=True)
    installing.add_argument("--artifact", type=Path, required=True)
    for command in (installing, published):
        command.add_argument("--runtime-root", type=Path, required=True)
        command.add_argument("--modernbert", action="store_true")
        command.add_argument("--install-worker", action="store_true", help=argparse.SUPPRESS)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "package":
            archive = package(**args)
            print(json.dumps({"passed": True, "archive": archive.name, "archive_sha256": sha256(archive)}))
            return 0
        worker = args.pop("install_worker")
        if command == "install-published":
            args["artifact"] = None
        if worker:
            if not args.pop("modernbert"):
                raise ValueError("Real ModernBERT is required")
            result = asyncio.run(_install_worker(**args))
        else:
            result = install_release(**args)
        print(json.dumps({"passed": result["passed"], "phase": result["phase"]}))
        return 0 if result["passed"] else 1
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
