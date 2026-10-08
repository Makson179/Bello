#!/usr/bin/env python3
"""Qualify a native arm64 Mac candidate without claiming full-tree recovery.

The production guardian must report normal completion and owned-group cleanup.
Detached descendant containment and crash recovery are explicitly NOT proven.
Only public pinned ModernBERT assets can use the network; providers are local
synthetic fixtures. This does not mutate any installer or published pins.
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import build_native_codex_macos_candidate as build
from scripts import verify_native_codex_candidate as common

SCHEMA = "bello.native-codex-macos-candidate-qualification.v1"
TARGET = build.TARGET
PROOF_INPUTS = (*common.PROOF_INPUTS,
                "scripts/build_native_codex_macos_candidate.py",
                "scripts/verify_native_codex_macos_candidate.py",
                ".github/workflows/native-codex-macos-candidate.yml")
LIMITATION = "normal completion and owned process groups only; detached descendant containment and crash recovery not proven"


def proof_inputs() -> dict:
    return {name: common.sha256(ROOT / name) for name in PROOF_INPUTS}


def names(modernbert: bool) -> set[str]:
    return {"selection", "async", "history"} | ({"modernbert"} if modernbert else set())


def fresh_output(candidate: Path, output: Path) -> tuple[Path, Path]:
    candidate, output = candidate.resolve(strict=True), output.resolve()
    if output == candidate or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError("Candidate and proof directories must be disjoint")
    output.mkdir(parents=True, exist_ok=False)
    return candidate, output


def report_base(target: str, phase: str) -> dict:
    return {"schema": SCHEMA, "passed": False, "published": False, "target": target,
            "paid_model_calls": 0, "external_provider_requests_forwarded": 0,
            "proofs": {}, "phase": phase, "scope": LIMITATION,
            "full_tree_recovery_proven": False}


def validate_saved_proofs(report: dict, output: Path, binary: Path, *, modernbert: bool) -> None:
    if set(report.get("proofs", {})) != names(modernbert):
        raise ValueError("Every requested native proof must be present")
    for name, saved in report["proofs"].items():
        path = output / name / "report.json"
        if (saved.get("report") != name + "/report.json"
                or saved.get("sha256") != common.sha256(path)
                or saved.get("case_results") != common.case_results(path.parent)):
            raise ValueError("Native proof bytes changed")
        common.validate_proof(name, common.read_report(path), path.parent, binary, TARGET)


async def qualify(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    build.require_platform(target)
    candidate, output = fresh_output(candidate, output)
    report = report_base(target, "build_before")
    report.update(modernbert_requested=modernbert, platform=platform.platform(), python=platform.python_version())
    before = None
    try:
        before = build.verify_build(target, candidate)
        scripts = proof_inputs()
        receipt_hash = common.sha256(candidate / build.RECEIPT)
        binary = candidate / "bin/codex"
        report.update(build_receipt_sha256=receipt_hash, build_identity=before["identity"],
                      candidate_files=before["files"], proof_inputs=scripts,
                      binary_sha256=common.sha256(binary))
        ordered = ["selection", "async", "history"] + (["modernbert"] if modernbert else [])
        with common.isolated_environment(output):
            report["phase"] = "native_version"
            report["native_version"] = common.native_version(binary)
            for name in ordered:
                report["phase"] = name
                if build.verify_build(target, candidate) != before or proof_inputs() != scripts:
                    raise ValueError("Candidate or proof inputs changed before execution")
                result = await common.run_proof(name, binary, output / name)
                disk = common.read_report(output / name / "report.json")
                if result != disk:
                    raise ValueError("Native proof differs from its durable report")
                common.validate_proof(name, disk, output / name, binary, target)
                if build.verify_build(target, candidate) != before or proof_inputs() != scripts:
                    raise ValueError("Candidate or proof inputs changed during execution")
                report["proofs"][name] = {"passed": True, "report": name + "/report.json",
                    "sha256": common.sha256(output / name / "report.json"),
                    "case_results": common.case_results(output / name),
                    "cases": len(disk.get("cases", disk.get("results", [disk])))}
        validate_saved_proofs(report, output, binary, modernbert=modernbert)
        if common.sha256(candidate / build.RECEIPT) != receipt_hash:
            raise ValueError("Build receipt changed during qualification")
        report.update(passed=True, phase="complete", candidate_unchanged=True,
                      qualification="native-and-real-modernbert" if modernbert else "native-only")
    except Exception as exc:
        report.update(error_type=type(exc).__name__, failure_code=report["phase"] + "_failed")
    finally:
        if before is not None:
            try:
                unchanged = build.verify_build(target, candidate) == before
            except Exception:
                unchanged = False
            report.update(candidate_unchanged=unchanged, passed=report["passed"] and unchanged)
        common.write_report(output / "qualification.json", report)
    return report


def _proof_worker(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    phase = "worker_authentication"
    try:
        from supervisor.process_fence import configure_worker, is_guarded_worker
        configure_worker()
        if not is_guarded_worker():
            raise RuntimeError("Native proof requires an authenticated guardian")
        phase = "worker_preflight"
        return asyncio.run(qualify(candidate, target, output, modernbert=modernbert))
    except Exception as exc:
        report = report_base(target, phase)
        report.update(error_type=type(exc).__name__, failure_code=phase + "_failed")
        try:
            try:
                output.lstat()
            except FileNotFoundError:
                pass
            else:
                return report
            _, destination = fresh_output(candidate, output)
            with (destination / "qualification.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError):
            pass
        return report


def guarded_qualification(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    from supervisor import watchdog
    build.require_platform(target)
    candidate, output = fresh_output(candidate, output)
    control = output / "guardian-control"
    control.mkdir(mode=0o700)
    worker = output / "worker"
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--proof-worker",
               "--candidate", str(candidate), "--target", target, "--output-dir", str(worker)]
    if modernbert:
        command.append("--modernbert")
    receipts = []
    original = watchdog._run_guarded

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        receipts.append({key: result.get(key) for key in ("exit_code", "fenced", "scope", "owner_pid", "stopped")})
        return result  # The production guardian and its result remain unchanged.

    report = report_base(target, "guardian")
    report["guardian"] = {"receipts": receipts}
    try:
        before = build.verify_build(target, candidate)
        watchdog._run_guarded = observe
        with common.isolated_environment(output):
            os.environ["PYTHONPATH"] = str(ROOT)
            code = watchdog.watch_command(command, project_root=control, maximum_restarts=0, backoff=(),
                required_scope="groups", disposition=lambda _: {"eligible": False, "reason": "qualification is never retried"},
                report=lambda _: None)
        report["guardian"]["watchdog_exit_code"] = code
        child = common.read_report(worker / "qualification.json")
        report.update(worker_report="worker/qualification.json", worker_report_sha256=common.sha256(worker / "qualification.json"),
                      worker_passed=child.get("passed") is True,
                      candidate_unchanged=build.verify_build(target, candidate) == before)
        valid_guardian = (type(code) is int and code == 0 and len(receipts) == 1
            and type(receipts[0]["exit_code"]) is int and receipts[0]["exit_code"] == 0
            and receipts[0]["fenced"] is True and receipts[0]["scope"] == "groups"
            and receipts[0]["stopped"] is False and type(receipts[0]["owner_pid"]) is int
            and receipts[0]["owner_pid"] > 0)
        if (not valid_guardian or child.get("passed") is not True or not report["candidate_unchanged"]
                or child.get("schema") != SCHEMA or child.get("target") != TARGET
                or child.get("full_tree_recovery_proven") is not False
                or child.get("candidate_unchanged") is not True
                or child.get("modernbert_requested") is not modernbert
                or not common._zero(child.get("paid_model_calls"))
                or not common._zero(child.get("external_provider_requests_forwarded"))
                or child.get("build_identity") != before["identity"] or child.get("candidate_files") != before["files"]
                or child.get("proof_inputs") != proof_inputs()
                or child.get("build_receipt_sha256") != common.sha256(candidate / build.RECEIPT)
                or child.get("native_version", {}).get("version") != "0.161.0"
                or child.get("native_version", {}).get("binary_sha256") != before["files"]["bin/codex"]
                or not common._zero(child.get("native_version", {}).get("exit_code"))
                or child.get("binary_sha256") != before["files"]["bin/codex"]):
            raise ValueError("Native worker proof and group finalization must both pass unchanged")
        validate_saved_proofs(child, worker, candidate / "bin/codex", modernbert=modernbert)
        report.update(passed=True, phase="complete", binary_sha256=child["binary_sha256"],
                      build_identity=child["build_identity"], proof_inputs=child["proof_inputs"],
                      candidate_files=child["candidate_files"], build_receipt_sha256=child["build_receipt_sha256"],
                      native_version=child["native_version"], modernbert_requested=modernbert,
                      proofs={name: {**item, "report": "worker/" + item["report"]} for name, item in child["proofs"].items()})
    except Exception as exc:
        report.update(error_type=type(exc).__name__, failure_code="guardian_or_worker_finalization_failed")
    finally:
        watchdog._run_guarded = original
        common.write_report(output / "qualification.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", choices=(TARGET,), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--modernbert", action="store_true", help="Four actual CPU inference cases and two protected-input bypass controls")
    parser.add_argument("--proof-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    function = _proof_worker if args.proof_worker else guarded_qualification
    report = function(args.candidate, args.target, args.output_dir, modernbert=args.modernbert)
    print(json.dumps({"passed": report["passed"], "phase": report["phase"], "proofs": list(report["proofs"])}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
