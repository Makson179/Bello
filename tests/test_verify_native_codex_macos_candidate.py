"""Synthetic orchestration tests; they do not claim native or CPU inference."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_native_codex_macos_candidate as proof
from tests.test_verify_native_codex_candidate import case, result


@pytest.fixture
def harness(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"
    (candidate / "bin").mkdir(parents=True)
    for name in ("codex", "codex-code-mode-host"):
        (candidate / "bin" / name).write_bytes(b"synthetic " + name.encode())
    files = {"bin/" + name: proof.common.sha256(candidate / "bin" / name)
             for name in ("codex", "codex-code-mode-host")}
    receipt = {"schema": "synthetic-only", "identity": {"build_key": "fixture"},
               "files": files, "proof_status": "not-run"}
    proof.common.write_report(candidate / proof.build.RECEIPT, receipt)
    def verify(target, directory):
        assert target == proof.TARGET and directory == candidate
        if any(proof.common.sha256(candidate / name) != digest for name, digest in files.items()):
            raise ValueError("Fixture bytes changed")
        return deepcopy(receipt)
    calls = []
    async def run(name, binary, output):
        calls.append(name)
        assert "OPENAI_API_KEY" not in os.environ and "HF_TOKEN" not in os.environ
        output.mkdir(parents=True)
        report = result(name, binary)
        proof.common.write_report(output / "report.json", report)
        if name == "history":
            (output / "case").mkdir()
            proof.common.write_report(output / "case/result.json", case("persistent_direct"))
        return report
    monkeypatch.setattr(proof.build, "require_platform", lambda *args: None)
    monkeypatch.setattr(proof.build, "verify_build", verify)
    monkeypatch.setattr(proof, "proof_inputs", lambda: {"synthetic-input": "a" * 64})
    monkeypatch.setattr(proof.common, "native_version", lambda binary: {"version": "0.161.0", "binary_sha256": proof.common.sha256(binary), "exit_code": 0})
    monkeypatch.setattr(proof.common, "run_proof", run)
    return SimpleNamespace(candidate=candidate, calls=calls, run=run)


@pytest.mark.parametrize("modernbert", [False, True])
async def test_complete_worker_is_not_a_guardian_or_full_tree_claim(harness, tmp_path, monkeypatch, modernbert):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-do-not-forward")
    monkeypatch.setenv("HF_TOKEN", "secret-do-not-forward")
    before = dict(os.environ)
    report = await proof.qualify(harness.candidate, proof.TARGET, tmp_path / "proof", modernbert=modernbert)
    assert report["passed"] and report["candidate_unchanged"]
    assert report["full_tree_recovery_proven"] is False and "not proven" in report["scope"]
    assert report["proofs"]["selection"]["cases"] == 9
    assert report["proofs"]["async"]["cases"] == 14
    assert report["proofs"]["history"]["cases"] == 1
    if modernbert:
        assert report["proofs"]["modernbert"]["cases"] == 6
    assert harness.calls == ["selection", "async", "history"] + (["modernbert"] if modernbert else [])
    assert dict(os.environ) == before and "secret-do-not-forward" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["false", "missing", "paid", "forwarded", "changed-companion", "disk-mismatch", "exception", "later-tamper"])
async def test_incomplete_changed_or_unsafe_native_proof_fails(harness, tmp_path, monkeypatch, failure):
    async def broken(name, binary, output):
        report = await harness.run(name, binary, output)
        if failure == "later-tamper":
            if name == "modernbert":
                (output.parent / "history/case/result.json").write_bytes(b"{}")
            return report
        if failure == "false": report["passed"] = False
        elif failure == "missing": report["cases"].pop()
        elif failure == "paid": report["paid_model_calls"] = 1
        elif failure == "forwarded": report["cases"][0]["external_proxy_requests_forwarded"] = 1
        elif failure == "changed-companion": (binary.parent / "codex-code-mode-host").write_bytes(b"changed")
        elif failure == "disk-mismatch": return {**report, "extra": 1}
        else: raise RuntimeError("secret-exception")
        proof.common.write_report(output / "report.json", report)
        return report
    monkeypatch.setattr(proof.common, "run_proof", broken)
    report = await proof.qualify(harness.candidate, proof.TARGET, tmp_path / "proof", modernbert=True)
    assert report["passed"] is False and "secret-exception" not in json.dumps(report)


@pytest.mark.parametrize("failure", [None, "fenced", "tree", "exit", "stopped", "pid", "missing", "case-tamper", "proof-tamper", "worker-claim", "exception"])
def test_parent_requires_actual_normal_group_cleanup_and_unchanged_proofs(harness, tmp_path, monkeypatch, failure):
    from supervisor import watchdog
    def run_guarded(command, env, *, cwd):
        if failure == "exception":
            raise RuntimeError("secret-exception")
        worker = Path(command[command.index("--output-dir") + 1])
        asyncio.run(proof.qualify(harness.candidate, proof.TARGET, worker, modernbert=True))
        receipt = {"exit_code": 0, "fenced": True, "scope": "groups", "owner_pid": 1234, "stopped": False}
        if failure == "fenced": receipt["fenced"] = False
        if failure == "tree": receipt["scope"] = "tree"
        if failure == "exit": receipt["exit_code"] = 1
        if failure == "stopped": receipt["stopped"] = True
        if failure == "pid": receipt["owner_pid"] = True
        if failure == "case-tamper": (worker / "history/case/result.json").write_bytes(b"{}")
        if failure == "proof-tamper": (worker / "selection/report.json").write_bytes(b"{}")
        if failure == "worker-claim":
            report = proof.common.read_report(worker / "qualification.json")
            report["full_tree_recovery_proven"] = True
            proof.common.write_report(worker / "qualification.json", report)
        return receipt
    monkeypatch.setattr(watchdog, "_run_guarded", run_guarded)
    def watch(command, **kwargs):
        assert kwargs["maximum_restarts"] == 0 and kwargs["backoff"] == ()
        assert kwargs["required_scope"] == "groups"
        assert kwargs["disposition"](kwargs["project_root"])["eligible"] is False
        if failure != "missing":
            watchdog._run_guarded(command, dict(os.environ), cwd=kwargs["project_root"])
        return 0
    monkeypatch.setattr(watchdog, "watch_command", watch)
    report = proof.guarded_qualification(harness.candidate, proof.TARGET, tmp_path / "proof", modernbert=True)
    assert report["passed"] is (failure is None)
    assert report["full_tree_recovery_proven"] is False
    assert watchdog._run_guarded is run_guarded
    assert "secret-exception" not in json.dumps(report)


def test_unauthenticated_worker_has_bounded_nonoverwriting_diagnostic(tmp_path, monkeypatch):
    from supervisor import process_fence
    monkeypatch.setattr(process_fence, "configure_worker", lambda: None)
    monkeypatch.setattr(process_fence, "is_guarded_worker", lambda: False)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    output = tmp_path / "proof"
    report = proof._proof_worker(candidate, proof.TARGET, output, modernbert=True)
    assert not report["passed"] and report["phase"] == "worker_authentication"
    assert report["error_type"] == "RuntimeError"
    before = (output / "qualification.json").read_bytes()
    proof._proof_worker(candidate, proof.TARGET, output, modernbert=True)
    assert (output / "qualification.json").read_bytes() == before


def test_alias_output_cannot_write_into_candidate(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for output in (candidate, candidate / "proof", tmp_path):
        with pytest.raises(ValueError, match="disjoint"):
            proof.fresh_output(candidate, output)


def test_proof_input_graph_includes_all_shared_and_mac_authority():
    assert set(proof.common.PROOF_INPUTS) < set(proof.PROOF_INPUTS)
    hashes = proof.proof_inputs()
    assert len(hashes) == len(proof.PROOF_INPUTS)
    assert "supervisor/process_fence.py" in hashes and "supervisor/watchdog.py" in hashes
    assert hashes["scripts/verify_native_codex_macos_candidate.py"] == proof.common.sha256(Path(proof.__file__))
