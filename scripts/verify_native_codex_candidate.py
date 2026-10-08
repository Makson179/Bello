#!/usr/bin/env python3
"""Qualify an explicit unpublished native candidate using credential-free proofs.

No installer or published native pin is consulted. Only --modernbert may fetch
the public, pinned selector weights; all provider requests remain synthetic and
loopback-only. Passing this script does not publish or promote the candidate.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "bello.native-codex-candidate-qualification.v1"
SELECTION_CASES = {"direct_off", "direct_on", "code_off", "code_on", "poll_off", "poll_on",
                   "missing_focus", "task_protected", "help_protected"}
ASYNC_CASES = {"direct_off", "direct_on", "code_off", "code_on", "steer", "interrupt",
               "code_interrupt", "child_wait", "async_and_selection_direct", "async_and_selection_code",
               "direct_on_repeat_2", "steer_repeat_2", "direct_on_repeat_3", "steer_repeat_3"}
MODERNBERT_CASES = {"short_cold", "short_warm", "long_cold", "long_warm", "task_protected", "help_protected"}
PROOF_INPUTS = (
    "scripts/verify_native_codex_candidate.py", "scripts/build_native_codex_candidate.py",
    "scripts/prepare_native_codex_candidate.py", "scripts/prepare_native_codex_windows.py",
    "scripts/native-codex-0.161.0.patch", "scripts/verify_native_codex_selection.py",
    "scripts/verify_native_codex_async.py", "scripts/verify_native_codex_history.py",
    "scripts/verify_windows_modernbert.py", "scripts/verify_windows_modernbert_live.py",
    "scripts/build_native_codex_linux.py", "supervisor/runtime/codex_distiller.py",
    "supervisor/runtime/distiller.py", "supervisor/runtime/distiller_worker.py",
    "supervisor/runtime/distiller_bundle.py", "supervisor/runtime/distiller_download.py",
    "supervisor/runtime/distiller_policy.py", "supervisor/process_fence.py",
    "supervisor/watchdog.py", "supervisor/appserver.py",
)


def sha256(path: Path) -> str:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise ValueError("Expected an unshared regular proof file")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_report(path: Path) -> dict:
    sha256(path)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate proof field")
            result[key] = value
        return result
    report = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(report, dict):
        raise ValueError("Proof report must be an object")
    return report


def proof_inputs() -> dict[str, str]:
    return {name: sha256(ROOT / name) for name in PROOF_INPUTS}


def case_results(output: Path) -> dict[str, str]:
    return {str(path.relative_to(output)).replace(os.sep, "/"): sha256(path)
            for path in sorted(output.rglob("result.json"))}


def native_version(binary: Path) -> dict:
    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = "http://127.0.0.1:9"
    result = subprocess.run([str(binary), "--version"], env=env, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or result.stdout.strip() != "codex-cli 0.161.0":
        raise ValueError("Supplied executable is not exactly native Codex 0.161.0")
    return {"version": "0.161.0", "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
            "binary_sha256": sha256(binary), "exit_code": result.returncode}


def verify_build(target: str, candidate: Path) -> dict:
    from scripts.build_native_codex_candidate import verify_build as verify
    return verify(target, candidate, restore_executable_modes=False)


def require_platform(target: str) -> None:
    expected = {"linux-x64": "Linux", "windows-x64": "Windows"}.get(target)
    if platform.system() != expected or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("Qualification requires the candidate's native x64 platform")


@contextmanager
def isolated_environment(output: Path):
    original = dict(os.environ)
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "USERNAME",
               "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432"}
    # Fresh Python 3.11 workers on Windows determine platform.machine() from
    # these nonsecret OS architecture fields; newer Python can also use WMI.
    clean = {key: value for key, value in original.items() if key.upper() in allowed}
    home, temporary = output / "empty-home", output / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    clean.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home),
        APPDATA=str(home / "config"), LOCALAPPDATA=str(home / "data"),
        XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
        XDG_CACHE_HOME=str(home / "cache"), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
        BELLO_RUNTIME_DIR=str(output / "private-runtime"), HF_HOME=str(output / "public-model-cache"),
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_TELEMETRY="1",
        TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="", PYTHONDONTWRITEBYTECODE="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _zero(value: Any) -> bool:
    return type(value) is int and value == 0


def validate_cases(report: dict, field: str, expected: set[str]) -> None:
    if report.get("passed") is not True or not _zero(report.get("paid_model_calls")):
        raise ValueError("Proof is not passing and credential-free")
    cases = report.get(field)
    if (not isinstance(cases, list) or len(cases) != len(expected)
            or any(not isinstance(case, dict) for case in cases)
            or {case.get("case") for case in cases} != expected):
        raise ValueError("Proof does not contain every distinct required case")
    for case in cases:
        if (case.get("passed") is not True or case.get("provider_errors") != []
                or not _zero(case.get("external_proxy_requests_forwarded"))
                or type(case.get("provider_requests")) is not int or case["provider_requests"] <= 0
                or case.get("error") is not None or case.get("failure") is not None):
            raise ValueError("Synthetic provider case is incomplete or unsafe")


def validate_proof(name: str, report: dict, output: Path, binary: Path, target: str) -> None:
    schemas = {"selection": "bello.native-selection-provider-proof.v1",
        "async": "bello.native-async-smoke.v1", "history": "bello.native-persistent-history-smoke.v1",
        "modernbert": "bello.candidate-real-modernbert-proof.v1"}
    if name == "sandbox":
        from scripts.build_native_codex_linux import validate_sandbox_proof
        validate_sandbox_proof(report, binary)
        return
    if report.get("schema") != schemas[name]:
        raise ValueError("Unexpected proof schema")
    if name == "selection":
        validate_cases(report, "cases", SELECTION_CASES)
        if report.get("binary_sha256") != sha256(binary):
            raise ValueError("Selection proof names a different binary")
        for case in report["cases"]:
            if case.get("exact_model_visible_output") is not True or case.get("focus_and_command_correct") is not True:
                raise ValueError("Selection provider-boundary evidence is incomplete")
    elif name == "async":
        validate_cases(report, "results", ASYNC_CASES)
        if report.get("on_off_native_instructions_identical") is not True:
            raise ValueError("Native instruction preservation was not proven")
    elif name == "history":
        if (report.get("passed") is not True or not _zero(report.get("paid_model_calls"))
                or report.get("provider_passed") is not True or report.get("exact_selected_history_match") is not True
                or report.get("live_coder_task") is not False):
            raise ValueError("Persistent history proof is incomplete")
        case = read_report(output / "case/result.json")
        validate_cases({"passed": True, "paid_model_calls": 0, "cases": [case]}, "cases", {"persistent_direct"})
    else:
        validate_cases(report, "cases", MODERNBERT_CASES)
        if (report.get("binary_sha256") != sha256(binary) or report.get("device") != "cpu"
                or report.get("model_files_unchanged") is not True):
            raise ValueError("Real CPU model identity was not proven")
        for case in report["cases"]:
            if case["case"].endswith("_protected"):
                if case.get("model_bypassed") is not True:
                    raise ValueError("Protected output was not proven to bypass inference")
            elif case.get("real_model_selection") is not True:
                raise ValueError("Real model inference was not proven")
    if target == "windows-x64":
        for case in report.get("cases", report.get("results", [])):
            # Every selection-derived case carries the native Windows ACL
            # proof; async-only cases have their own execution assertions.
            if "exact_model_visible_output" in case and case.get("windows_filesystem_sandbox_enforced") is not True:
                raise ValueError("Native Windows selection sandbox was not proven")


async def modernbert_proof(binary: Path, output: Path) -> dict:
    # Optional ML imports happen only here, after the environment was replaced.
    from scripts.verify_windows_modernbert import exercise
    from supervisor.runtime.distiller_bundle import validate_bundle
    from supervisor.runtime.distiller_download import ensure_default_bundle, MODEL_FILES, MODEL_REPOSITORY, MODEL_REVISION
    started = time.monotonic()
    bundle = ensure_default_bundle()  # Public pinned snapshot; downloader uses token=False.
    download_seconds = time.monotonic() - started
    manifest = validate_bundle(bundle)
    def asset_hashes():
        hashes = {}
        for name in MODEL_FILES:
            # HF snapshots use links into their public content-addressed cache.
            with (bundle / name).open("rb") as stream:
                hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        return hashes
    before = asset_hashes()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(str(bundle), local_files_only=True, use_fast=True, trust_remote_code=False)
    output.mkdir(parents=True, exist_ok=False)
    cases = await exercise(binary, bundle, output, tokenizer)
    after = asset_hashes()
    report = {"schema": "bello.candidate-real-modernbert-proof.v1", "passed": all(case["passed"] for case in cases) and before == after,
        "paid_model_calls": 0, "device": "cpu", "binary_sha256": sha256(binary), "cases": cases,
        "model_repository": MODEL_REPOSITORY, "model_revision": MODEL_REVISION, "recipe": manifest["recipe"],
        "model_files_sha256": before, "model_files_unchanged": before == after, "model_download_seconds": download_seconds,
        "torch": importlib.metadata.version("torch"), "transformers": importlib.metadata.version("transformers"),
        "not_covered": ["live coder quality", "recall", "billing savings", "GPU inference"]}
    write_report(output / "report.json", report)
    return report


async def run_proof(name: str, binary: Path, output: Path) -> dict:
    if name == "selection":
        from scripts.verify_native_codex_selection import main_async
        await main_async(argparse.Namespace(codex=binary, output_dir=output, cases=None))
        return read_report(output / "report.json")
    if name == "async":
        from scripts.verify_native_codex_async import verify
        return await verify(binary, output, concurrency_repeats=3)
    if name == "history":
        from scripts.verify_native_codex_history import verify
        output.mkdir(parents=True, exist_ok=False)
        return await verify(binary, output)
    if name == "sandbox":
        from scripts.build_native_codex_linux import sandbox_proof
        return sandbox_proof(binary, output)
    return await modernbert_proof(binary, output)


async def qualify(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    require_platform(target)
    candidate, output = candidate.resolve(strict=True), output.resolve()
    if output == candidate or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError("Candidate and proof directories must be disjoint")
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": SCHEMA, "passed": False, "published": False, "target": target,
        "paid_model_calls": 0, "external_provider_requests_forwarded": 0, "modernbert_requested": modernbert,
        "proofs": {}, "phase": "worker_metadata",
        "scope": "proof-worker-only; successful guardian finalization is also required"}
    before = None
    try:
        report.update(platform=platform.platform(), python=platform.python_version(), phase="build_before")
        before = verify_build(target, candidate)
        if before.get("proof_status") != "not-run":
            raise ValueError("Build receipt must remain separate from qualification")
        scripts = proof_inputs()
        receipt_hash = sha256(candidate / "native-build-receipt.json")
        binary = candidate / "bin" / ("codex.exe" if target == "windows-x64" else "codex")
        report.update(build_receipt_sha256=receipt_hash, build_identity=before["identity"],
                      candidate_files=before["files"], proof_inputs=scripts, binary_sha256=sha256(binary))
        names = ["selection", "async", "history"] + (["sandbox"] if target == "linux-x64" else [])
        if modernbert:
            names.append("modernbert")
        with isolated_environment(output):
            report["phase"] = "native_version"
            report["native_version"] = native_version(binary)
            for name in names:
                report["phase"] = name
                if verify_build(target, candidate) != before or proof_inputs() != scripts:
                    raise ValueError("Candidate or proof implementation changed before execution")
                result = await run_proof(name, binary, output / name)
                disk = read_report(output / name / "report.json")
                if result != disk:
                    raise ValueError("Proof result and durable report disagree")
                validate_proof(name, disk, output / name, binary, target)
                if verify_build(target, candidate) != before or proof_inputs() != scripts:
                    raise ValueError("Candidate or proof implementation changed during execution")
                report["proofs"][name] = {"passed": True, "report": name + "/report.json",
                    "sha256": sha256(output / name / "report.json"),
                    "case_results": case_results(output / name),
                    "cases": len(disk.get("cases", disk.get("results", [disk])))}
        if any(sha256(output / item["report"]) != item["sha256"] for item in report["proofs"].values()):
            raise ValueError("An earlier proof report changed during qualification")
        if any(case_results(output / name) != item["case_results"] for name, item in report["proofs"].items()):
            raise ValueError("Case artifacts changed during qualification")
        if sha256(candidate / "native-build-receipt.json") != receipt_hash:
            raise ValueError("Build receipt bytes changed during qualification")
        report.update(passed=True, phase="complete", candidate_unchanged=True,
                      qualification="native-and-real-modernbert" if modernbert else "native-only; real ModernBERT not requested")
    except Exception as exc:
        # Detailed native artifacts contain synthetic diagnostics only. Never
        # serialize arbitrary exception text or the launching environment.
        report["error_type"] = type(exc).__name__
        report["failure_code"] = report["phase"] + "_failed"
    finally:
        if before is not None:
            try:
                unchanged = verify_build(target, candidate) == before
            except Exception:
                unchanged = False
            report["candidate_unchanged"] = unchanged
            report["passed"] = report["passed"] and unchanged
        write_report(output / "qualification.json", report)
    return report


def _proof_worker(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    """Keep early startup failures bounded and durable, without weakening proof."""
    phase = "worker_authentication"
    try:
        from supervisor.process_fence import configure_worker, is_guarded_worker
        configure_worker()
        if not is_guarded_worker():
            raise RuntimeError("Qualification worker requires an authenticated production guardian")
        phase = "worker_preflight"
        return asyncio.run(qualify(candidate, target, output, modernbert=modernbert))
    except Exception as exc:
        report = {"schema": SCHEMA, "passed": False, "published": False, "target": target,
                  "paid_model_calls": 0, "proofs": {}, "phase": phase,
                  "error_type": type(exc).__name__, "failure_code": phase + "_failed"}
        try:
            try:
                output.lstat()
            except FileNotFoundError:
                pass
            else:
                return report
            destination, source = output.resolve(), candidate.resolve()
            if (destination == source or destination.is_relative_to(source)
                    or source.is_relative_to(destination)):
                return report
            # Startup diagnostics may create only a fresh output directory.
            # Never replace an earlier proof, follow an existing output alias,
            # or write into the candidate when preflight rejected its layout.
            destination.mkdir(parents=True, exist_ok=False)
            with (destination / "qualification.json").open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        except (OSError, ValueError):
            # Failure to persist diagnostics is still a failed worker. The
            # parent requires both a passing report and proven tree cleanup.
            pass
        return report


def guarded_qualification(candidate: Path, target: str, output: Path, *, modernbert: bool) -> dict:
    """Use the unchanged production guardian, observing its actual final receipt."""
    from supervisor import watchdog
    require_platform(target)
    candidate, output = candidate.resolve(strict=True), output.resolve()
    if output == candidate or output.is_relative_to(candidate) or candidate.is_relative_to(output):
        raise ValueError("Candidate and proof directories must be disjoint")
    output.mkdir(parents=True, exist_ok=False)
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
        return result  # No alteration to guardian behavior or cleanup authority.
    report = {"schema": SCHEMA, "passed": False, "published": False, "target": target,
              "paid_model_calls": 0, "phase": "guardian", "proofs": {}, "guardian": {"receipts": receipts}}
    try:
        before = verify_build(target, candidate)
        watchdog._run_guarded = observe
        with isolated_environment(output):
            os.environ["PYTHONPATH"] = str(ROOT)
            code = watchdog.watch_command(command, project_root=control, maximum_restarts=0, backoff=(),
                required_scope="tree", disposition=lambda _: {"eligible": False, "reason": "qualification is never retried"},
                report=lambda _: None)
        report["guardian"]["watchdog_exit_code"] = code
        child = read_report(worker / "qualification.json")
        report.update(worker_report="worker/qualification.json", worker_report_sha256=sha256(worker / "qualification.json"),
                      worker_passed=child.get("passed") is True, candidate_unchanged=verify_build(target, candidate) == before)
        valid_guardian = (code == 0 and len(receipts) == 1 and receipts[0]["exit_code"] == 0
                         and receipts[0]["fenced"] is True and receipts[0]["scope"] == "tree"
                         and receipts[0]["stopped"] is False and type(receipts[0]["owner_pid"]) is int)
        if (not valid_guardian or child.get("passed") is not True or not report["candidate_unchanged"]
                or child.get("build_identity") != before["identity"] or child.get("candidate_files") != before["files"]
                or child.get("proof_inputs") != proof_inputs()
                or child.get("build_receipt_sha256") != sha256(candidate / "native-build-receipt.json")):
            raise ValueError("Worker proof and guardian finalization must both pass unchanged")
        expected = {"selection", "async", "history"} | ({"sandbox"} if target == "linux-x64" else set()) | ({"modernbert"} if modernbert else set())
        if set(child.get("proofs", {})) != expected or child.get("native_version", {}).get("version") != "0.161.0":
            raise ValueError("Worker did not prove the full requested candidate suite")
        binary = candidate / "bin" / ("codex.exe" if target == "windows-x64" else "codex")
        for name in expected:
            saved = child["proofs"][name]
            path = worker / name / "report.json"
            if saved.get("report") != name + "/report.json" or saved.get("sha256") != sha256(path):
                raise ValueError("Worker proof changed before guardian finalization")
            if saved.get("case_results") != case_results(path.parent):
                raise ValueError("Worker case artifacts changed before guardian finalization")
            validate_proof(name, read_report(path), path.parent, binary, target)
        report.update(passed=True, phase="complete", scope="native proof worker under production process-tree guardian",
                      binary_sha256=child["binary_sha256"], build_identity=child["build_identity"],
                      proof_inputs=child["proof_inputs"], candidate_files=child["candidate_files"],
                      build_receipt_sha256=child["build_receipt_sha256"], native_version=child["native_version"],
                      modernbert_requested=modernbert, external_provider_requests_forwarded=0,
                      proofs={name: {**item, "report": "worker/" + item["report"]} for name, item in child["proofs"].items()})
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["failure_code"] = "guardian_or_worker_finalization_failed"
    finally:
        watchdog._run_guarded = original
        write_report(output / "qualification.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--target", choices=("linux-x64", "windows-x64"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--modernbert", action="store_true", help="Required CI gate: six actual pinned CPU model cases")
    parser.add_argument("--proof-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.proof_worker:
        report = _proof_worker(args.candidate, args.target, args.output_dir, modernbert=args.modernbert)
    else:
        report = guarded_qualification(args.candidate, args.target, args.output_dir, modernbert=args.modernbert)
    print(json.dumps({"passed": report["passed"], "phase": report["phase"], "proofs": list(report["proofs"])}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
