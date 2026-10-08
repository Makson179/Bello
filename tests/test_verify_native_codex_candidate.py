"""Qualification orchestration tests; mocks do not claim native/ML execution."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import verify_native_codex_candidate as proof


def case(name):
    return {"case": name, "passed": True, "provider_errors": [], "provider_requests": 2,
            "external_proxy_requests_forwarded": 0, "error": None,
            "exact_model_visible_output": True, "focus_and_command_correct": True,
            "windows_filesystem_sandbox_enforced": True,
            "real_model_selection": True, "model_bypassed": True}


def result(name, binary):
    base = {"passed": True, "paid_model_calls": 0}
    if name == "selection":
        return {**base, "schema": "bello.native-selection-provider-proof.v1", "binary_sha256": proof.sha256(binary),
                "cases": [case(name) for name in sorted(proof.SELECTION_CASES)]}
    if name == "async":
        return {**base, "schema": "bello.native-async-smoke.v1", "on_off_native_instructions_identical": True,
                "results": [case(name) for name in sorted(proof.ASYNC_CASES)]}
    if name == "history":
        return {**base, "schema": "bello.native-persistent-history-smoke.v1", "provider_passed": True,
                "exact_selected_history_match": True, "live_coder_task": False}
    if name == "modernbert":
        return {**base, "schema": "bello.candidate-real-modernbert-proof.v1", "binary_sha256": proof.sha256(binary),
                "device": "cpu", "model_files_unchanged": True,
                "cases": [case(name) for name in sorted(proof.MODERNBERT_CASES)]}
    from scripts.build_native_codex_linux import SANDBOX_SCHEMA
    return {**base, "schema": SANDBOX_SCHEMA, "binary_sha256": proof.sha256(binary),
        "bwrap_sha256": proof.sha256(binary.parent / "codex-resources/bwrap"),
        "inside_write_succeeded": True, "outside_write_denied": True, "new_user_namespace": True,
        "tampered_bwrap_exit_code": 8, "system_bwrap_on_path": False}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    files = ("bin/codex", "bin/codex.exe", "bin/codex-code-mode-host", "bin/codex-resources/bwrap")
    for name in files:
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic unit-test payload: " + name.encode())
    payload = {name: proof.sha256(candidate / name) for name in files}
    receipt = {"schema": "mock-build-unit-test", "proof_status": "not-run", "identity": {"build_key": "mock-only"}, "files": payload}
    proof.write_report(candidate / "native-build-receipt.json", receipt)
    def verify(target, directory):
        assert target in {"linux-x64", "windows-x64"} and directory == candidate
        if any(proof.sha256(candidate / name) != digest for name, digest in payload.items()):
            raise ValueError("Fixture candidate changed")
        return deepcopy(receipt)
    calls = []
    async def run(name, binary, output):
        calls.append(name)
        assert binary in {candidate / "bin/codex", candidate / "bin/codex.exe"}
        assert "OPENAI_API_KEY" not in os.environ and "HF_TOKEN" not in os.environ
        output.mkdir(parents=True)
        report = result(name, binary)
        proof.write_report(output / "report.json", report)
        if name == "history":
            (output / "case").mkdir()
            proof.write_report(output / "case/result.json", case("persistent_direct"))
        return report
    monkeypatch.setattr(proof, "require_platform", lambda _: None)
    monkeypatch.setattr(proof, "verify_build", verify)
    monkeypatch.setattr(proof, "proof_inputs", lambda: {"fixture-script": "a" * 64})
    monkeypatch.setattr(proof, "native_version", lambda binary: {"version": "0.161.0", "binary_sha256": proof.sha256(binary), "exit_code": 0})
    monkeypatch.setattr(proof, "run_proof", run)
    return SimpleNamespace(candidate=candidate, calls=calls, run=run, receipt=receipt)


@pytest.mark.parametrize("target,modernbert", [("linux-x64", True), ("windows-x64", True), ("linux-x64", False)])
async def test_exact_candidate_all_requested_proofs_and_receipts(harness, tmp_path, target, modernbert, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-secret-do-not-forward")
    monkeypatch.setenv("HF_TOKEN", "unit-secret-do-not-forward")
    before = dict(os.environ)
    report = await proof.qualify(harness.candidate, target, tmp_path / "proof", modernbert=modernbert)
    assert report["passed"] and report["candidate_unchanged"]
    assert harness.calls == ["selection", "async", "history"] + (["sandbox"] if target == "linux-x64" else []) + (["modernbert"] if modernbert else [])
    assert report["proofs"]["async"]["cases"] == 14
    assert report["proofs"]["selection"]["cases"] == 9
    assert os.environ == before
    assert "unit-secret" not in json.dumps(report)


@pytest.mark.parametrize("corruption", ["false", "missing-case", "duplicate-case", "paid", "paid-bool", "forwarded", "changed-binary", "changed-companion", "disk-mismatch", "exception"])
async def test_any_incomplete_unsafe_or_changed_proof_fails_closed(harness, tmp_path, monkeypatch, corruption):
    async def broken(name, binary, output):
        report = await harness.run(name, binary, output)
        if corruption == "false": report["passed"] = False
        elif corruption == "missing-case": report["cases"].pop()
        elif corruption == "duplicate-case": report["cases"][-1] = report["cases"][0]
        elif corruption == "paid": report["paid_model_calls"] = 1
        elif corruption == "paid-bool": report["paid_model_calls"] = False
        elif corruption == "forwarded": report["cases"][0]["external_proxy_requests_forwarded"] = 1
        elif corruption == "changed-binary": binary.write_bytes(b"changed")
        elif corruption == "changed-companion": (harness.candidate / "bin/codex-code-mode-host").write_bytes(b"changed")
        elif corruption == "disk-mismatch": return {**report, "extra": True}
        else: raise RuntimeError("unit-secret-must-not-leak")
        proof.write_report(output / "report.json", report)
        return report
    monkeypatch.setattr(proof, "run_proof", broken)
    report = await proof.qualify(harness.candidate, "linux-x64", tmp_path / "proof", modernbert=True)
    assert report["passed"] is False and harness.calls == ["selection"]
    assert "unit-secret" not in json.dumps(report)
    assert (tmp_path / "proof/qualification.json").is_file()


@pytest.mark.parametrize("stdout,code", [("codex-cli 0.155.1\n", 0), ("codex-cli 0.161.0\n", 1), ("codex-cli 0.161.0 other\n", 0)])
def test_actual_version_not_declared_receipt_is_required(tmp_path, monkeypatch, stdout, code):
    monkeypatch.setattr(proof.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=code))
    with pytest.raises(ValueError, match="exactly native Codex"):
        proof.native_version(tmp_path / "codex")


def test_real_version_probe_has_no_paid_request_or_inherited_proxy(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_bytes(b"fixture")
    def run(command, **kwargs):
        assert command == [str(binary), "--version"]
        assert kwargs["env"]["HTTP_PROXY"] == "http://127.0.0.1:9"
        return SimpleNamespace(stdout="codex-cli 0.161.0\n", returncode=0)
    monkeypatch.setattr(proof.subprocess, "run", run)
    assert proof.native_version(binary)["version"] == "0.161.0"


@pytest.mark.parametrize("failure", [None, "fenced", "scope", "exit", "stopped", "missing", "proof-tamper", "case-tamper", "exception"])
def test_worker_success_cannot_hide_guardian_failure(harness, tmp_path, monkeypatch, failure):
    from supervisor import watchdog
    def run_guarded(command, env, *, cwd):
        if failure == "exception":
            raise RuntimeError("unit-secret-must-not-leak")
        assert "--proof-worker" in command
        worker = Path(command[command.index("--output-dir") + 1])
        asyncio.run(proof.qualify(harness.candidate, "linux-x64", worker, modernbert=True))
        receipt = {"exit_code": 0, "fenced": True, "scope": "tree", "owner_pid": 1234, "stopped": False}
        if failure == "fenced": receipt["fenced"] = False
        if failure == "scope": receipt["scope"] = "groups"
        if failure == "exit": receipt["exit_code"] = 1
        if failure == "stopped": receipt["stopped"] = True
        if failure == "proof-tamper":
            path = worker / "selection/report.json"
            report = proof.read_report(path)
            report["passed"] = False
            proof.write_report(path, report)
        if failure == "case-tamper":
            path = worker / "history/case/result.json"
            report = proof.read_report(path)
            report["extra"] = "changed after worker success"
            proof.write_report(path, report)
        return receipt
    monkeypatch.setattr(watchdog, "_run_guarded", run_guarded)
    def watch(command, **kwargs):
        assert kwargs["maximum_restarts"] == 0 and kwargs["required_scope"] == "tree"
        assert kwargs["disposition"](kwargs["project_root"])["eligible"] is False
        if failure != "missing": watchdog._run_guarded(command, dict(os.environ), cwd=kwargs["project_root"])
        return 0
    monkeypatch.setattr(watchdog, "watch_command", watch)
    report = proof.guarded_qualification(harness.candidate, "linux-x64", tmp_path / "proof", modernbert=True)
    assert report["passed"] is (failure is None)
    assert watchdog._run_guarded is run_guarded
    assert "unit-secret" not in json.dumps(report)


def test_platform_mismatch_never_starts_candidate(monkeypatch):
    monkeypatch.setattr(proof.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="native x64"):
        proof.require_platform("linux-x64")


@pytest.mark.parametrize("architecture,native_architecture,accepted", [
    ("AMD64", "", True), ("x86", "AMD64", True),
    ("ARM64", "", False), ("x86", "", False), ("", "", False),
])
def test_isolated_fresh_worker_retains_python311_windows_architecture(
    tmp_path, monkeypatch, architecture, native_architecture, accepted,
):
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", architecture)
    monkeypatch.setenv("PROCESSOR_ARCHITEW6432", native_architecture)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-forward")
    # CPython 3.11's Windows machine detector uses only these two OS fields.
    # A fresh interpreter avoids platform.uname's parent-process cache masking
    # the exact regression that stopped the native Windows 2022 proof worker.
    code = """
import json, os
from scripts import verify_native_codex_candidate as proof
proof.platform.system = lambda: 'Windows'
proof.platform.machine = lambda: (os.environ.get('PROCESSOR_ARCHITEW6432', '')
                                 or os.environ.get('PROCESSOR_ARCHITECTURE', ''))
try:
    proof.require_platform('windows-x64')
    accepted = True
except ValueError:
    accepted = False
print(json.dumps({'accepted': accepted, 'credential_absent': 'OPENAI_API_KEY' not in os.environ}))
"""
    with proof.isolated_environment(tmp_path):
        child = subprocess.run([sys.executable, "-B", "-c", code], cwd=proof.ROOT,
                               capture_output=True, text=True, timeout=20)
    assert child.returncode == 0
    assert json.loads(child.stdout) == {"accepted": accepted, "credential_absent": True}


@pytest.mark.parametrize("failure", ["authentication", "platform", "candidate", "metadata"])
def test_worker_startup_failure_writes_only_bounded_safe_diagnostic(
    tmp_path, monkeypatch, failure,
):
    from supervisor import process_fence
    candidate, output = tmp_path / "candidate", tmp_path / "proof"
    if failure != "candidate":
        candidate.mkdir()
    def fail(*args, **kwargs):
        raise ValueError("fixture-secret and arbitrary command must not appear")
    monkeypatch.setattr(process_fence, "configure_worker", fail if failure == "authentication" else lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: True)
    monkeypatch.setattr(proof, "require_platform", fail if failure == "platform" else lambda _: None)
    if failure == "metadata":
        monkeypatch.setattr(proof.platform, "platform", fail)
    report = proof._proof_worker(candidate, "windows-x64", output, modernbert=False)
    assert report["passed"] is False and report["proofs"] == {}
    expected = {"authentication": "worker_authentication", "platform": "worker_preflight",
                "candidate": "worker_preflight", "metadata": "worker_metadata"}[failure]
    assert report["phase"] == expected
    assert report["error_type"] == ("FileNotFoundError" if failure == "candidate" else "ValueError")
    assert report["failure_code"] == expected + "_failed"
    assert proof.read_report(output / "qualification.json") == report
    assert "fixture-secret" not in json.dumps(report)
    assert "arbitrary command" not in json.dumps(report)


@pytest.mark.parametrize("existing", ["report", "candidate-overlap"])
def test_worker_startup_diagnostics_never_overwrite_existing_or_candidate_files(
    tmp_path, monkeypatch, existing,
):
    from supervisor import process_fence
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output = candidate if existing == "candidate-overlap" else tmp_path / "proof"
    output.mkdir(exist_ok=True)
    path = output / "qualification.json"
    path.write_bytes(b"preserved exact previous bytes")
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: False)
    report = proof._proof_worker(candidate, "windows-x64", output, modernbert=False)
    assert not report["passed"]
    assert path.read_bytes() == b"preserved exact previous bytes"


def test_worker_startup_diagnostics_reject_dangling_output_link(tmp_path, monkeypatch):
    from supervisor import process_fence
    candidate, outside, output = tmp_path / "candidate", tmp_path / "outside", tmp_path / "proof"
    candidate.mkdir()
    try:
        output.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink privilege unavailable")
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: False)
    report = proof._proof_worker(candidate, "windows-x64", output, modernbert=False)
    assert not report["passed"] and not outside.exists()


def test_ambiguous_json_report_is_rejected(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"passed":false,"passed":true}')
    with pytest.raises(ValueError, match="Duplicate"):
        proof.read_report(path)


async def test_later_proof_cannot_rewrite_earlier_case_artifacts(harness, tmp_path, monkeypatch):
    async def corrupt(name, binary, output):
        report = await harness.run(name, binary, output)
        if name == "modernbert":
            path = output.parent / "history/case/result.json"
            previous = proof.read_report(path)
            previous["extra"] = "changed during a later proof"
            proof.write_report(path, previous)
        return report
    monkeypatch.setattr(proof, "run_proof", corrupt)
    report = await proof.qualify(harness.candidate, "linux-x64", tmp_path / "proof", modernbert=True)
    assert report["passed"] is False
