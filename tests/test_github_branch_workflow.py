"""Current-branch workflow provenance: neutral filesystem fixtures, no network."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import textwrap

import pytest

from tests.test_native_codex_windows_workflow import _action, _block, _field, _steps


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "codex/080-production-ready"
TARGETS = ("linux-x64", "windows-x64", "darwin-arm64")


def workflow_job(target):
    platform = target.split("-", 1)[0]
    filename = "native-codex-macos-candidate.yml" if platform == "darwin" else "native-codex-candidate.yml"
    job = "macos-proof" if platform == "darwin" else platform + "-proof"
    text = (ROOT / ".github/workflows" / filename).read_text()
    return text, _block(_block(text, "jobs"), job)


def binding_script(target):
    _, job = workflow_job(target)
    step = next(s for s in _steps(job) if "name: Bind commit, binary," in s)
    assert _field(step, "shell") == "python"
    # The general structural reader deliberately strips nested indentation.
    # Preserve it here to execute the exact embedded Python, not a reimplementation.
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


def test_current_source_binding_scripts_are_identical_and_compile():
    scripts = [binding_script(target) for target in TARGETS]
    assert scripts[0] == scripts[1] == scripts[2]
    ast.parse(scripts[0])


@pytest.mark.parametrize("target", TARGETS)
def test_same_run_package_install_binding_is_mandatory_before_archive_upload(target):
    _, job = workflow_job(target)
    steps = _steps(job)
    names = [_field(step, "name") or "" for step in steps]
    package = next(i for i, s in enumerate(steps) if ".py package " in (_field(s, "run") or ""))
    install = next(i for i, s in enumerate(steps) if ".py install-local " in (_field(s, "run") or ""))
    bind = next(i for i, name in enumerate(names) if name.startswith("Bind commit, binary,"))
    archive = next(i for i, name in enumerate(names) if name.startswith("Preserve qualified archive"))
    assert package < install < bind < archive
    assert all(_field(steps[i], "continue-on-error") is None for i in (package, install, bind))
    assert _field(steps[archive], "if") is None
    downloads = [s for s in steps if _action(s) == "actions/download-artifact"]
    assert len(downloads) == 1
    settings = _block(downloads[0], "with")
    assert _field(settings, "name").startswith("${{ needs.")
    assert all(_field(settings, key) is None for key in ("run-id", "artifact-ids", "github-token", "repository"))
    command = _field(steps[install], "run")
    assert "release-receipts/installed" in command and "--modernbert" in command
    receipts = next(s for s in steps if "source-binding.json" in s and _action(s) == "actions/upload-artifact")
    assert _field(receipts, "if") == "always()"
    paths = _field(_block(receipts, "with"), "path")
    assert "release-receipts/installed/worker/context/*/receipt.json" in paths
    assert not any(token in paths for token in ("**", "rpc.jsonl", "provider-request", "empty-home", "guardian-control"))


@pytest.mark.parametrize("target", TARGETS)
def test_candidate_jobs_check_and_preserve_each_context_receipt(target):
    _, job = workflow_job(target)
    steps = _steps(job)
    commands = [_field(step, "run") or "" for step in steps]
    tests = next(command for command in commands if "python -m pytest" in command)
    assert "tests/test_native_context_release_binding.py" in tests.split()
    proof = next(i for i, command in enumerate(commands)
                 if "--output-dir candidate-proof" in command)
    assert commands.index(tests) < proof
    receipts = next(step for step in steps if "candidate-proof/qualification.json" in step
                    and _action(step) == "actions/upload-artifact")
    assert _field(receipts, "if") == "always()"
    assert "candidate-proof/worker/context/*/receipt.json" in _field(_block(receipts, "with"), "path")


def test_candidate_and_package_jobs_are_nonpublishing_and_references_exist():
    for name in ("native-codex-candidate.yml", "native-codex-macos-candidate.yml", "release-package-validation.yml"):
        text = (ROOT / ".github/workflows" / name).read_text()
        assert BRANCH in _field(_block(_block(text, "on"), "push"), "branches")
        assert _field(_block(text, "permissions"), "contents") == "read"
        assert not re.search(r"\b(?:gh release|twine upload|npm publish|git push)\b|secrets\.", text)
        for path in re.findall(r"\b(?:tests/test_[\w]+\.py|scripts/[\w]+\.py)\b", text):
            assert (ROOT / path).is_file(), (name, path)


def test_legacy_helpers_cannot_run_on_current_branch_and_https_is_manual_only():
    for name, job in (("native-codex-history-smoke.yml", "windows-history"),
                      ("native-codex-published-async.yml", "published-async"),
                      ("native-codex-published-linux.yml", "published-helper"),
                      ("windows-modernbert-smoke.yml", "real-model"),
                      ("native-codex-windows-repackage.yml", "repackage")):
        text = (ROOT / ".github/workflows" / name).read_text()
        assert f"github.ref != 'refs/heads/{BRANCH}'" in _field(_block(_block(text, "jobs"), job), "if")
    historical = (ROOT / ".github/workflows/native-codex-release-qualification.yml").read_text()
    assert "push:" not in _block(historical, "on")
    assert _field(_block(_block(historical, "jobs"), "package-install"), "if") == "github.ref == 'refs/heads/codex/072-recovery-validation'"
    published = (ROOT / ".github/workflows/native-codex-release-download.yml").read_text()
    assert set(re.findall(r"^  ([a-z_]+):", _block(published, "on"), re.M)) == {"workflow_dispatch"}
    assert _field(_block(_block(published, "jobs"), "published-install"), "if") is None


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def fixture(root, target, mutation):
    """Neutral receipt-shaped data; this tests binding, not actual native proofs."""
    binary_path = "bin/codex.exe" if target == "windows-x64" else "bin/codex"
    candidate, proof, release, installed = [root / name for name in ("native-candidate", "candidate-proof", "release", "installed")]
    binary = candidate / binary_path
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"neutral candidate bytes; never executed")
    files = {binary_path: digest(binary)}
    identity = {"build_key": "a" * 64}
    receipt = candidate / "native-build-receipt.json"
    write_json(receipt, {"files": files, "identity": identity})
    counts = {"selection": 9, "async": 15, "history": 1, "context": 8, "modernbert": 6}
    if target == "linux-x64":
        counts["sandbox"] = 1
    if mutation == "context-count": counts["context"] = 7
    if mutation == "async-count": counts["async"] = 14
    if mutation == "missing-proof": del counts["history"]
    proofs = {name: {"passed": True, "cases": count, "sha256": "b" * 64} for name, count in counts.items()}
    common = {"passed": True, "published": False, "target": target,
              "binary_sha256": files[binary_path], "build_receipt_sha256": digest(receipt),
              "build_identity": identity, "candidate_files": files, "proofs": proofs}
    worker = proof / "worker/qualification.json"
    write_json(worker, common)
    aggregate = {**common, "worker_report_sha256": digest(worker)}
    if mutation == "qualification-binary": aggregate["binary_sha256"] = "c" * 64
    if mutation == "qualification-worker": aggregate["worker_report_sha256"] = "c" * 64
    aggregate_path = proof / "qualification.json"
    write_json(aggregate_path, aggregate)
    release.mkdir()
    archive = release / "candidate.tar.gz"
    archive.write_bytes(b"neutral archive bytes; never unpacked")
    checks = {"published": False, "target": target, "archive": archive.name,
              "archive_sha256": digest(archive), "manifest_sha256": "d" * 64,
              "build_receipt_sha256": digest(receipt), "qualification_sha256": digest(aggregate_path),
              "build_identity": identity, "files": files}
    if mutation == "archive-bytes": archive.write_bytes(b"substituted archive")
    if mutation == "package-qualification": checks["qualification_sha256"] = "c" * 64
    write_json(release / "checksums.json", checks)
    result = {"passed": True, "published": False, "target": target,
              "archive_sha256": checks["archive_sha256"], "manifest_sha256": checks["manifest_sha256"],
              "binary_sha256": files[binary_path], "distribution_source": "local-archive",
              "cache_files": {binary_path: {"sha256": files[binary_path]}}, "proofs": proofs}
    if mutation == "installed-cache": result["cache_files"][binary_path]["sha256"] = "c" * 64
    if mutation == "installed-distribution": result["distribution_source"] = "production-https"
    installed_worker = installed / "worker/report.json"
    write_json(installed_worker, result)
    final = {**result, "worker_report_sha256": digest(installed_worker)}
    if mutation == "installed-worker": final["worker_report_sha256"] = "c" * 64
    if mutation == "installed-failed": final["passed"] = False
    if mutation == "installed-count":
        final["proofs"] = {**proofs, "context": {**proofs["context"], "cases": 7}}
    write_json(installed / "installed-proof.json", final)
    if mutation == "candidate-bytes": binary.write_bytes(b"substituted executable")
    env = {"BELLO_TARGET": target, "GITHUB_SHA": "e" * 40, "GITHUB_RUN_ID": "12345",
           "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REPOSITORY": "neutral/repository",
           "GITHUB_REF": "refs/heads/" + BRANCH, "GITHUB_JOB": "fixture",
           "RUNNER_OS": "fixture", "BELLO_RELEASE_DIR": str(release),
           "BELLO_INSTALLED_DIR": str(installed), "BELLO_BINDING_FILE": str(root / "binding.json")}
    env["BELLO_CANDIDATE_ARTIFACT"] = f"native-codex-candidate-0161-{target}-{env['GITHUB_SHA']}-12345-2"
    if mutation == "wrong-run": env["GITHUB_RUN_ID"] = "54321"
    if mutation == "wrong-attempt": env["GITHUB_RUN_ATTEMPT"] = "3"
    if mutation == "wrong-commit": env["GITHUB_SHA"] = "f" * 40
    return env


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("mutation", [None, "candidate-bytes", "qualification-binary", "qualification-worker",
    "archive-bytes", "package-qualification", "installed-cache", "installed-distribution", "installed-worker",
    "installed-failed", "installed-count", "context-count", "async-count", "missing-proof",
    "wrong-run", "wrong-attempt", "wrong-commit"])
def test_actual_embedded_binding_accepts_only_matching_current_evidence(tmp_path, monkeypatch, target, mutation):
    env = fixture(tmp_path, target, mutation)
    monkeypatch.chdir(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    execute = lambda: exec(compile(binding_script(target), "<workflow-binding>", "exec"), {})
    if mutation is None:
        execute()
        result = json.loads((tmp_path / "binding.json").read_text())
        assert result["source_sha"] == env["GITHUB_SHA"] and result["run_attempt"] == 2
        assert result["proof_cases"]["context"] == 8 and result["proof_cases"]["async"] == 15
        assert result["published"] is False
    else:
        with pytest.raises(SystemExit, match="Exact source binding rejected"):
            execute()
        assert not (tmp_path / "binding.json").exists()
