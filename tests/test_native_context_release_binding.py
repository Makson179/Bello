"""Offline qualification contracts, not evidence of native/model execution."""
from copy import deepcopy

import pytest

from scripts import release_native_codex_candidate as release
from scripts import verify_native_codex_candidate as proof
from scripts import verify_native_codex_macos_candidate as mac
from tests.test_release_native_codex_candidate import fixture, put, refresh
from tests.test_verify_native_codex_candidate import result


@pytest.mark.parametrize("target", release.TARGETS)
def test_context_is_required_through_qualification_packaging_and_install(target):
    assert "context" in release.proof_names(target)
    assert len(release._case_paths("context", proof)) == 8
    assert "scripts/verify_native_codex_context.py" in proof.PROOF_INPUTS
    assert "scripts/verify_native_codex_context.py" in mac.PROOF_INPUTS
    assert "context" in mac.names(False)


@pytest.mark.parametrize("mutation", [None, "missing", "duplicate", "wrong-binary", "changed-binary",
    "one-summary", "off-summary", "false-check", "missing-check", "paid", "paid-bool", "external",
    "provider-error", "not-resumed", "masked-failure", "wrong-mode", "different-receipt", "missing-receipt"])
def test_context_requires_eight_actual_bound_success_or_rollback_cases(tmp_path, mutation):
    binary = tmp_path / "codex"
    binary.write_bytes(b"synthetic")
    report = result("context", binary)
    rows = {row["case"]: row for row in report["results"]}
    for label, row in rows.items():
        put(tmp_path / label / "receipt.json", row)
    if mutation == "missing": report["results"].pop()
    elif mutation == "duplicate": report["results"][-1] = deepcopy(report["results"][0])
    elif mutation == "wrong-binary": report["binary_sha256"] = "0" * 64
    elif mutation == "changed-binary": report["binary_unchanged"] = False
    elif mutation == "one-summary": rows["pending"]["summary_requests"] = 1
    elif mutation == "off-summary": rows["off"]["summary_requests"] = 1
    elif mutation == "false-check": rows["on"]["checks"]["call_result_pairs_intact"] = False
    elif mutation == "missing-check": rows["on"]["checks"].pop("prefix_unchanged")
    elif mutation == "paid": rows["on"]["paid_model_calls"] = 1
    elif mutation == "paid-bool": rows["on"]["paid_model_calls"] = False
    elif mutation == "external": rows["on"]["blocked_external_requests"] = 1
    elif mutation == "provider-error": rows["on"]["provider_errors"] = 1
    elif mutation == "not-resumed": rows["on"]["resumed"] = False
    elif mutation == "masked-failure": rows["empty"]["turn_status"] = "completed"
    elif mutation == "wrong-mode": rows["pending"]["pending_async"] = False
    elif mutation == "different-receipt": put(tmp_path / "on/receipt.json", {**rows["on"], "extra": "changed"})
    elif mutation == "missing-receipt": (tmp_path / "on/receipt.json").unlink()
    if mutation is None:
        proof.validate_proof("context", report, tmp_path, binary, "linux-x64")
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            proof.validate_proof("context", report, tmp_path, binary, "linux-x64")


@pytest.mark.parametrize("target", release.TARGETS)
@pytest.mark.parametrize("mutation", ["missing-suite", "short-suite", "wrong-count", "late-case-change"])
def test_package_cannot_accept_old_or_partial_context_evidence(fixture, target, mutation):
    f = fixture(target)
    if mutation == "missing-suite":
        del f.worker["proofs"]["context"]
        refresh(f)
    elif mutation == "wrong-count":
        f.worker["proofs"]["context"]["cases"] = 7
        refresh(f)
    elif mutation == "short-suite":
        path = f.qualification / "worker/context/report.json"
        report = release.read_json(path)
        report["results"].pop()
        put(path, report)
        refresh(f, "context")
    else:
        path = f.qualification / "worker/context/on/receipt.json"
        put(path, {**release.read_json(path), "changed": True})
        refresh(f, "context")
    with pytest.raises(ValueError):
        release.validate_qualification(target, f.candidate, f.qualification)
