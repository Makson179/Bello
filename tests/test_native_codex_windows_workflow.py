"""Structural contracts for the native build/proof split, without a YAML dependency.

The small readers below inspect indentation-delimited mappings/step records only;
they do not interpret YAML or execute expressions. Assertions use action IDs,
commands and data dependencies rather than presentation names or whole-file text.
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROOF = ROOT / ".github/workflows/native-codex-windows.yml"
BUILD = ROOT / ".github/workflows/native-codex-windows-build.yml"


def _block(text: str, key: str) -> str:
    lines = text.splitlines()
    matches = [(index, len(match[1])) for index, line in enumerate(lines)
               if (match := re.fullmatch(rf"(\s*){re.escape(key)}:\s*(?:#.*)?", line))]
    assert matches, f"missing mapping: {key}"
    shallowest = min(indent for _, indent in matches)
    roots = [(index, indent) for index, indent in matches if indent == shallowest]
    assert len(roots) == 1, f"ambiguous mapping: {key}"
    start, indent = roots[0]
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            break
        end += 1
    return "\n".join(lines[start + 1:end])


def _field(text: str, key: str) -> str | None:
    # A step's first field follows '- '; subsequent fields are indented equally.
    lines = [re.sub(r"^(\s*)- ", r"\1  ", line) for line in text.splitlines()]
    meaningful = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not meaningful:
        return None
    indent = min(len(line) - len(line.lstrip()) for line in meaningful)
    for index, line in enumerate(lines):
        match = re.fullmatch(rf"\s{{{indent}}}{re.escape(key)}:\s*(.*)", line)
        if not match:
            continue
        value = match[1].split(" #", 1)[0].strip()
        if value in {"|", "|-", ">", ">-"}:
            body = []
            for child in lines[index + 1:]:
                if child.strip() and len(child) - len(child.lstrip()) <= indent:
                    break
                body.append(child.strip())
            return "\n".join(body)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def _steps(job: str) -> list[str]:
    text = _block(job, "steps")
    entries = list(re.finditer(r"(?m)^([ \t]*)- \S", text))
    assert entries, "job has no steps"
    indent = min(len(match[1]) for match in entries)
    starts = [match.start() for match in entries if len(match[1]) == indent] + [len(text)]
    return [text[start:end] for start, end in zip(starts, starts[1:])]


def _action(step: str) -> str:
    return (_field(step, "uses") or "").split("@", 1)[0]


def _miss_required(step: str) -> bool:
    condition = (_field(step, "if") or "").removeprefix("${{").removesuffix("}}").strip()
    return "||" not in condition and any(re.fullmatch(
        r"steps\.candidate\.outputs\.cache-hit\s*!=\s*(['\"])true\1", term.strip())
        for term in condition.split("&&"))


@pytest.fixture
def workflows():
    proof = PROOF.read_text(encoding="utf-8")
    build = BUILD.read_text(encoding="utf-8")
    return proof, build, _block(_block(proof, "jobs"), "native-proof"), _block(_block(build, "jobs"), "build")


def test_proof_job_never_installs_rust_checks_out_upstream_or_compiles(workflows):
    _, _, proof, _ = workflows
    for step in _steps(proof):
        action, command = _action(step), _field(step, "run") or ""
        assert "rust-toolchain" not in action and "setup-msvc" not in action
        assert not re.search(r"\bcargo(?:\.exe)?\s+(?:build|check|test|install|fetch|update)\b", command)
        assert "RUSTY_V8_ARCHIVE" not in command and "CARGO_TARGET_DIR" not in command
        if action == "actions/checkout":
            assert _field(_block(step, "with"), "repository") is None
        assert not re.search(r"prepare_native_codex_windows\.py\s+(?:prepare|verify-v8)\b", command)


def test_ready_candidate_cache_is_exact_and_checked_before_reuse(workflows):
    _, _, _, build = workflows
    steps = _steps(build)
    candidate = next(step for step in steps if _field(step, "id") == "candidate")
    assert _action(candidate) == "actions/cache/restore"
    settings = _block(candidate, "with")
    assert _field(settings, "path") == "native-candidate"
    assert re.search(r"\$\{\{\s*steps\.identity\.outputs\.key\s*\}\}$", _field(settings, "key") or "")
    assert _field(settings, "restore-keys") is None
    assert _field(settings, "fail-on-cache-miss") in {None, "false"}
    verification = [step for step in steps if " verify-build " in (_field(step, "run") or "")]
    assert any(re.fullmatch(r"steps\.candidate\.outputs\.cache-hit\s*==\s*(['\"])true\1",
                           _field(step, "if") or "") for step in verification)
    assert any(_field(step, "if") is None for step in verification)


def test_all_expensive_native_preparation_and_snapshot_steps_require_cache_miss(workflows):
    _, _, _, build = workflows
    exercised = set()
    for step in _steps(build):
        action, command = _action(step), _field(step, "run") or ""
        categories = set()
        if action == "actions/checkout" and _field(_block(step, "with"), "repository") == "openai/codex":
            categories.add("upstream")
        if "rust-toolchain" in action:
            categories.add("rust")
        if "setup-msvc" in action:
            categories.add("msvc")
        if re.search(r"\bcargo\s+build\b", command):
            categories.add("compile")
        for marker, category in ((" prepare ", "source"), (" verify-v8 ", "v8"),
                                 (" snapshot-build ", "snapshot"), ("CARGO_TARGET_DIR=", "paths")):
            if marker in command:
                categories.add(category)
        if categories:
            assert _miss_required(step), f"unguarded native work: {categories}"
            exercised |= categories
    assert exercised == {"upstream", "rust", "msvc", "compile", "source", "v8", "snapshot", "paths"}


def test_candidate_artifact_is_saved_by_build_then_downloaded_by_dependent_proof(workflows):
    proof_file, build_file, proof, build = workflows
    build_call = _block(_block(proof_file, "jobs"), "native-build")
    assert _field(build_call, "uses") == "./.github/workflows/native-codex-windows-build.yml"
    assert _field(proof, "needs") in {"native-build", "[native-build]"}
    output = _block(_block(_block(build_file, "on"), "workflow_call"), "outputs")
    assert _field(_block(output, "artifact-name"), "value") == "${{ jobs.build.outputs.artifact-name }}"
    assert _field(_block(build, "outputs"), "artifact-name") == "${{ steps.identity.outputs.artifact-name }}"
    build_steps = _steps(build)
    uploaded = next(step for step in build_steps if _action(step) == "actions/upload-artifact")
    assert _field(uploaded, "if") is None and _field(uploaded, "continue-on-error") != "true"
    assert _field(_block(uploaded, "with"), "name") == "${{ steps.identity.outputs.artifact-name }}"
    assert any(" verify-build " in (_field(step, "run") or "") and _field(step, "if") is None
               for step in build_steps[:build_steps.index(uploaded)])
    proof_steps = _steps(proof)
    downloaded = next(step for step in proof_steps if _action(step) == "actions/download-artifact")
    assert _field(_block(downloaded, "with"), "name") == "${{ needs.native-build.outputs.artifact-name }}"
    assert _field(_block(downloaded, "with"), "path") == "native-candidate"
    commands_after_download = [_field(step, "run") or "" for step in proof_steps[proof_steps.index(downloaded) + 1:]]
    verify = next(index for index, command in enumerate(commands_after_download) if " verify-build " in command)
    execute = next(index for index, command in enumerate(commands_after_download) if "verify_native_codex_selection.py" in command)
    assert verify < execute


def test_proof_failures_and_python_changes_do_not_cancel_or_gate_the_native_build(workflows):
    proof, build_file, _, build = workflows
    assert _field(_block(proof, "concurrency"), "cancel-in-progress") == "false"
    assert not re.search(r"(?m)^\s*cancel-in-progress:\s*true\b", build_file)
    assert _field(build, "needs") is None
    assert not any(re.search(r"\bpytest\b|verify_native_codex_selection\.py", _field(step, "run") or "")
                   for step in _steps(build))
    saves = [step for step in _steps(build) if _action(step) == "actions/cache/save"
             and _field(_block(step, "with"), "path") == "native-candidate"]
    assert len(saves) == 1 and _miss_required(saves[0])
