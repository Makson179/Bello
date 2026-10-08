"""Candidate-only CI contracts, using stdlib structural readers (not PyYAML)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_native_codex_windows_workflow import _action, _block, _field, _miss_required, _steps


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "codex/072-recovery-validation"
REF = f"refs/heads/{BRANCH}"
WORKFLOW = ROOT / ".github/workflows/native-codex-candidate.yml"
UPSTREAM = "979011409de0a60b52f179721948e65531d26144"


@pytest.fixture
def workflow():
    return WORKFLOW.read_text(encoding="utf-8")


def _jobs(workflow):
    mapping = _block(workflow, "jobs")
    return {name: _block(mapping, name) for name in re.findall(r"^  ([a-z][a-z-]+):$", mapping, re.M)}


def _branches(push):
    value = _field(push, "branches")
    assert value and value.startswith("[") and value.endswith("]")
    return [entry.strip().strip("'\"") for entry in value[1:-1].split(",")]


def test_exact_branch_push_and_guarded_dispatch_never_publish(workflow):
    triggers = _block(workflow, "on")
    assert _branches(_block(triggers, "push")) == [BRANCH]
    assert re.search(r"^  workflow_dispatch:\s*$", triggers, re.M)
    assert not re.search(r"\b(paths|paths-ignore|pull_request|workflow_call):", triggers)
    assert _field(_block(workflow, "permissions"), "contents") == "read"
    assert _field(_block(workflow, "concurrency"), "cancel-in-progress") == "false"
    jobs = _jobs(workflow)
    assert set(jobs) == {"linux-build", "windows-build", "linux-proof", "windows-proof"}
    for job in jobs.values():
        assert _field(job, "if") == f"github.ref == '{REF}'"
        for step in _steps(job):
            if _action(step) == "actions/checkout":
                assert _field(_block(step, "with"), "persist-credentials") == "false"
    assert "secrets." not in workflow and "secrets: inherit" not in workflow
    assert not re.search(r"\b(?:gh release|twine upload|npm publish|git push)\b", workflow)
    assert "danger-full-access" not in workflow


def test_requested_branch_keeps_general_gates_and_excludes_only_legacy_native_proofs():
    for filename in ("tests.yml", "runtime-windows.yml", "claude-windows.yml"):
        workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
        push = _block(_block(workflow, "on"), "push")
        assert BRANCH in _branches(push), filename
        assert "paths:" not in push and "paths-ignore:" not in push
    for platform in ("linux", "windows"):
        workflow = (ROOT / f".github/workflows/native-codex-{platform}.yml").read_text(encoding="utf-8")
        branches = _branches(_block(_block(workflow, "on"), "push"))
        assert branches[:2] == ["mystery", "codex/**"]
        assert branches[-1] == f"!{BRANCH}"
        expected = {f"!{BRANCH}"}
        if platform == "windows":
            expected.add("!codex/release-0.6.0-readiness")
        assert {branch for branch in branches if branch.startswith("!")} == expected
        jobs = _jobs(workflow)
        assert _field(jobs["native-build"], "if") == f"github.ref != '{REF}'"
        assert _field(jobs["native-proof"], "if") == f"github.ref != '{REF}'"
        assert _field(jobs["native-build"], "uses") == f"./.github/workflows/native-codex-{platform}-build.yml"


@pytest.mark.parametrize("platform,runner,target", [
    ("linux", "ubuntu-22.04", "x86_64-unknown-linux-gnu"),
    ("windows", "windows-2025", "x86_64-pc-windows-msvc"),
])
def test_build_recipe_is_exact_candidate_only_and_all_expensive_work_cache_gated(workflow, platform, runner, target):
    job = _jobs(workflow)[f"{platform}-build"]
    assert _field(job, "runs-on") == runner
    assert _field(job, "needs") is None
    steps = _steps(job)
    upstream = [step for step in steps if _action(step) == "actions/checkout"
                and _field(_block(step, "with"), "repository") == "openai/codex"]
    assert len(upstream) == 1
    assert _field(_block(upstream[0], "with"), "ref") == UPSTREAM
    rust = next(step for step in steps if "rust-toolchain" in _action(step))
    assert _field(_block(rust, "with"), "toolchain") == "1.95.0"
    assert _field(_block(rust, "with"), "targets") == target
    assert re.search(r"@[a-f0-9]{40}(?:\s|$)", _field(rust, "uses"))
    for step in steps:
        command = _field(step, "run") or ""
        action = _action(step)
        if (step in upstream or "rust-toolchain" in action or "setup-msvc-env" in action
                or any(marker in command for marker in ("cargo build", " prepare ", " verify-v8 ",
                    " snapshot-build ", "RUSTY_V8_ARCHIVE", "apt-get install", "CARGO_TARGET_DIR="))):
            assert _miss_required(step), step
        assert "verify_native_codex_candidate.py" not in command
        assert "native-codex-selection.patch" not in command
    prepare = next(_field(step, "run") for step in steps if " prepare " in (_field(step, "run") or ""))
    assert prepare == "python scripts/prepare_native_codex_candidate.py prepare --source native-source"
    v8 = next(_field(step, "run") for step in steps if " verify-v8 " in (_field(step, "run") or ""))
    assert "rusty-v8-v150.4.0" in v8
    assert v8.index(" verify-v8 ") < v8.index("RUSTY_V8_ARCHIVE")
    for step in steps:
        if "cargo build" in (_field(step, "run") or ""):
            assert f"--locked --release --timings --target {target}" in _field(step, "run")


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_all_caches_are_exact_separate_and_every_candidate_is_verified_before_save(workflow, platform):
    steps = _steps(_jobs(workflow)[f"{platform}-build"])
    restores = [step for step in steps if _action(step) == "actions/cache/restore"]
    assert len(restores) == 2
    for step in restores:
        settings = _block(step, "with")
        kind = "ready" if _field(step, "id") == "candidate" else "cargo"
        assert _field(settings, "key") == f"native-codex-candidate-0161-{platform}-x64-{kind}-v1-${{{{ steps.identity.outputs.key }}}}"
        assert _field(settings, "restore-keys") is None
    commands = [_field(step, "run") or "" for step in steps]
    verify = f"python scripts/build_native_codex_candidate.py verify-build --target {platform}-x64 --candidate native-candidate"
    checks = [(index, step) for index, step in enumerate(steps) if _field(step, "run") == verify]
    assert len(checks) == 2
    assert _field(checks[0][1], "if") == "steps.candidate.outputs.cache-hit == 'true'"
    assert _field(checks[1][1], "if") is None
    snapshot = next(i for i, command in enumerate(commands) if " snapshot-build " in command)
    assert checks[0][0] < snapshot < checks[1][0]
    saves = [i for i, step in enumerate(steps) if _action(step) == "actions/cache/save"
             and _field(_block(step, "with"), "path") == "native-candidate"]
    uploads = [i for i, step in enumerate(steps) if _action(step) == "actions/upload-artifact"]
    assert len(saves) == len(uploads) == 1 and checks[1][0] < saves[0] < uploads[0]
    upload = _block(steps[uploads[0]], "with")
    assert _field(upload, "path") == "native-candidate/"
    assert _field(upload, "if-no-files-found") == "error"
    assert _field(upload, "overwrite") is None
    identity = next(step for step in steps if _field(step, "id") == "identity")
    assert f"build-key --target {platform}-x64" in _field(identity, "run")
    assert "github.sha" in _field(identity, "run") and "github.run_attempt" in _field(identity, "run")


def test_linux_bwrap_is_final_before_codex_compiles(workflow):
    steps = _steps(_jobs(workflow)["linux-build"])
    commands = [_field(step, "run") or "" for step in steps]
    bwrap = next(i for i, command in enumerate(commands) if "--bin bwrap" in command)
    codex = next(i for i, command in enumerate(commands) if "--bin codex --bin codex-code-mode-host" in command)
    assert bwrap < codex
    command = commands[bwrap]
    assert command.index("strip --strip-debug") < command.index("sha256sum") < command.index("CODEX_BWRAP_SHA256")


def test_native_proof_platform_matrix_is_explicit(workflow):
    jobs = _jobs(workflow)
    linux = jobs["linux-proof"]
    assert _field(linux, "runs-on") == "ubuntu-22.04"
    python = next(step for step in _steps(linux) if _action(step) == "actions/setup-python")
    assert _field(_block(python, "with"), "python-version") == "3.13"
    windows = jobs["windows-proof"]
    assert _field(windows, "runs-on") == "${{ matrix.os }}"
    strategy = _block(windows, "strategy")
    assert _field(strategy, "fail-fast") == "false"
    matrix = _block(_block(strategy, "matrix"), "include")
    assert re.findall(r"- os: (\S+)\s+python: '([^']+)'", matrix) == [
        ("windows-2022", "3.11"), ("windows-2025", "3.14")]


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_proof_verifies_before_execution_runs_real_cpu_models_and_retains_only_bounded_evidence(workflow, platform):
    job = _jobs(workflow)[f"{platform}-proof"]
    assert _field(job, "needs") == f"{platform}-build"
    steps = _steps(job)
    download = next(i for i, step in enumerate(steps) if _action(step) == "actions/download-artifact")
    assert _field(_block(steps[download], "with"), "name") == f"${{{{ needs.{platform}-build.outputs.artifact-name }}}}"
    commands = [_field(step, "run") or "" for step in steps]
    verify = next(i for i, command in enumerate(commands) if " verify-build " in command)
    install = next(i for i, command in enumerate(commands) if "pip install" in command)
    proof = next(i for i, command in enumerate(commands) if "verify_native_codex_candidate.py --candidate" in command)
    assert download < verify < install < proof
    assert ("--restore-executable-modes" in commands[verify]) == (platform == "linux")
    assert '--index-url https://download.pytorch.org/whl/cpu' in commands[install]
    assert '".[log-distiller,test]"' in commands[install]
    assert commands[proof] == f"python scripts/verify_native_codex_candidate.py --candidate native-candidate --target {platform}-x64 --output-dir candidate-proof --modernbert"
    assert _field(steps[proof], "continue-on-error") is None
    for step, command in zip(steps, commands):
        assert not re.search(r"\bcargo\s+(?:build|test|install)\b", command)
        assert "rust-toolchain" not in _action(step) and "RUSTY_V8_ARCHIVE" not in command
        if _action(step) == "actions/checkout":
            assert _field(_block(step, "with"), "repository") is None
    uploads = [step for step in steps if _action(step) == "actions/upload-artifact"]
    assert len(uploads) == 1 and _field(uploads[0], "if") == "always()"
    settings = _block(uploads[0], "with")
    assert _field(settings, "overwrite") is None
    paths = _field(settings, "path").splitlines()
    assert set(paths) == {"native-candidate/native-build-receipt.json", "native-candidate/BUILD-INFO",
        "native-candidate-tests.xml", "candidate-proof/qualification.json",
        "candidate-proof/worker/qualification.json", "candidate-proof/worker/*/report.json",
        "candidate-proof/worker/*/*/result.json"}
    assert all("**" not in path for path in paths)
    if platform == "windows":
        assert "matrix.os" in _field(settings, "name") and "matrix.python" in _field(settings, "name")
