#!/usr/bin/env python3
"""Install an explicitly supplied final wheel into a fresh isolated test venv.

Preparation may download declared Python dependencies when the operator runs it.
It never starts Bello, a provider, a model, or the native helper. Run the emitted
driver command separately after the technical helper release and pin promotion.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import venv


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verifier():
    """Load only this stdlib checker, never the checkout's supervisor package."""
    spec = importlib.util.spec_from_file_location("installed_wheel_verify", Path(__file__).with_name("verify.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Expected bounded regular JSON evidence")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate evidence key")
            value[key] = item
        return value
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Evidence must be an object")
    return value


def qualified_ledger(wheel: Path, helper_ledger: Path, target: str, source_sha: str,
                     proof_root: Path, runtime_root: Path) -> dict:
    """Bind the new wheel to an audited release ledger and an actual PASS proof.

    The public ledger's helper_source_sha comes from the reviewed CI/release
    evidence, not from the native upstream revision or the current product SHA.
    No identity or hash is invented when this ledger has not yet been supplied.
    """
    check = verifier()
    published = read_json(helper_ledger)
    check.require(published.get("schema") == "bello.qualified-helper-release.v1", "Wrong helper ledger schema")
    entry = published.get("targets", {}).get(target)
    check.require(isinstance(entry, dict), "Target absent from audited helper release ledger")
    result = {"schema": "bello.installed-wheel-delivery-ledger.v1", "target": target,
              "product_source_sha": source_sha, "helper_source_sha": published.get("helper_source_sha"),
              "wheel_sha256": digest(wheel), "bundle": entry.get("bundle"), "files": entry.get("files")}
    check.validate_ledger(result)
    expected_provenance = entry.get("release_provenance")
    provenance_keys = {"build_receipt_sha256", "qualification_sha256", "worker_sha256"}
    check.require(isinstance(expected_provenance, dict) and set(expected_provenance) == provenance_keys
                  and all(re.fullmatch(r"[a-f0-9]{64}", str(value)) for value in expected_provenance.values()),
                  "Audited helper ledger lacks exact qualification provenance")
    parent = read_json(proof_root / "installed-proof.json")
    child_path = proof_root / "worker/report.json"
    child = read_json(child_path)
    bundle = result["bundle"]
    for row in (parent, child):
        check.require(row.get("schema") == "bello.native-codex-local-install.v1"
                      and row.get("passed") is True and row.get("published") is True
                      and row.get("phase") == "complete" and row.get("target") == target
                      and row.get("distribution_source") == "production-https"
                      and row.get("archive_sha256") == bundle["archive_sha256"]
                      and row.get("manifest_sha256") == bundle["manifest_sha256"]
                      and row.get("cache_unchanged") is True
                      and type(row.get("paid_model_calls")) is int and row["paid_model_calls"] == 0
                      and type(row.get("external_provider_requests_forwarded")) is int
                      and row["external_provider_requests_forwarded"] == 0,
                      "Published native qualification is not PASS for the audited helper")
    check.require(parent.get("candidate_unchanged") is True
                  and parent.get("worker_report") == "worker/report.json"
                  and parent.get("worker_report_sha256") == digest(child_path), "Published worker binding mismatch")
    check.require(type(child.get("local_archive_transfers")) is int and child["local_archive_transfers"] == 0
                  and all(child.get(name) is True for name in ("cold_cache", "cache_hit", "post_proof_cache_reusable")),
                  "Published proof used a local transfer or did not prove reuse")
    directory = runtime_root.resolve() / "native-codex" / bundle["archive_sha256"]
    check.require(Path(child.get("installed_directory", "")).resolve() == directory, "Unexpected published-proof cache")
    manifest_path = directory / "selection-manifest.json"
    check.require(digest(manifest_path) == bundle["manifest_sha256"], "Published manifest hash mismatch")
    manifest = read_json(manifest_path)
    check.require(manifest.get("files") == result["files"], "Audited helper inventory differs from published cache")
    complete_files = {**result["files"], "selection-manifest.json": bundle["manifest_sha256"]}
    check.snapshot(directory, complete_files)
    check.require({name: item.get("sha256") for name, item in child.get("cache_files", {}).items()}
                  == complete_files, "Published worker cache identities differ")
    binary_name = "bin/codex.exe" if target == "windows-x64" else "bin/codex"
    check.require(child.get("binary_sha256") == result["files"].get(binary_name), "Qualified executable hash mismatch")
    info = read_json(directory / "BUILD-INFO")
    check.require(isinstance(info.get("native_build_identity"), dict)
                  and info["native_build_identity"].get("target") == target,
                  "Published BUILD-INFO lacks the native target identity")
    provenance = info.get("release_provenance", {})
    check.require({name: provenance.get(name) for name in provenance_keys} == expected_provenance,
                  "Published BUILD-INFO qualification differs from the audited ledger")
    result["qualification_binding"] = {
        "helper_release_ledger_sha256": digest(helper_ledger),
        "published_proof_sha256": digest(proof_root / "installed-proof.json"),
        "published_worker_sha256": digest(child_path), "build_info_sha256": result["files"]["BUILD-INFO"],
        "release_provenance": expected_provenance,
    }
    return result


def clean_environment(home: Path, scripts: Path) -> dict[str, str]:
    system_names = ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "USERNAME")
    env = {key: os.environ[key] for key in system_names if os.environ.get(key)}
    for name in ("tmp", "config", "data", "cache", "codex"):
        (home / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / "codex"),
               TMPDIR=str(home / "tmp"), TMP=str(home / "tmp"), TEMP=str(home / "tmp"),
               APPDATA=str(home / "config"), LOCALAPPDATA=str(home / "data"),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), PYTHONNOUSERSITE="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    system_path = os.environ.get("PATH", os.defpath)
    env["PATH"] = str(scripts) + os.pathsep + system_path
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    ledgers = parser.add_mutually_exclusive_group(required=True)
    ledgers.add_argument("--ledger", type=Path)
    ledgers.add_argument("--helper-ledger", type=Path)
    parser.add_argument("--target", choices=("linux-x64", "windows-x64", "darwin-arm64"))
    parser.add_argument("--source-sha")
    parser.add_argument("--published-proof", type=Path)
    parser.add_argument("--published-runtime-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependency-wheelhouse", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    ledger = args.ledger.resolve(strict=True) if args.ledger else None
    if ledger:
        expected = read_json(ledger)
    else:
        if not all((args.target, args.source_sha, args.published_proof, args.published_runtime_root)):
            parser.error("--helper-ledger requires target, source-sha, published-proof and published-runtime-root")
        expected = qualified_ledger(wheel, args.helper_ledger.resolve(strict=True), args.target,
                                    args.source_sha, args.published_proof.resolve(strict=True),
                                    args.published_runtime_root.resolve(strict=True))
    verifier().validate_ledger(expected)
    if digest(wheel) != expected["wheel_sha256"]:
        raise ValueError("Supplied wheel differs from the release ledger")
    output = args.output.absolute()
    output.mkdir(parents=False, exist_ok=False)
    if ledger is None:
        ledger = output / "delivery-ledger.json"
        ledger.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    work = output / "work"
    work.mkdir()
    virtual = output / "venv"
    venv.EnvBuilder(with_pip=True).create(virtual)
    scripts = virtual / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    env = clean_environment(output / "preparation-home", scripts)
    command = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install",
               "--no-input", "--no-cache-dir"]
    if args.dependency_wheelhouse:
        command += ["--no-index", "--find-links", str(args.dependency_wheelhouse.resolve(strict=True))]
    command.append(str(wheel))
    with (output / "pip-install.raw.log").open("xb") as log:
        result = subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT,
                                timeout=600, check=False)
    driver = Path(__file__).resolve().with_name("verify.py")
    next_command = [str(python), "-I", "-B", str(driver), "--wheel", str(wheel), "--ledger", str(ledger),
                    "--output", str(output / "delivery-proof")]
    receipt = {"schema": "bello.installed-wheel-preparation.v1", "passed": result.returncode == 0,
               "wheel_sha256": digest(wheel), "ledger_sha256": digest(ledger),
               "product_source_sha": expected["product_source_sha"], "helper_source_sha": expected["helper_source_sha"],
               "qualification_binding": expected.get("qualification_binding"),
               "pip_exit_code": result.returncode, "python": str(python),
               "driver_command": next_command, "driver_started": False,
               "paid_model_calls": 0}
    (output / "preparation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
