"""Linux build/proof separation and sandbox-preserving CI contracts."""
from pathlib import Path
import importlib.util
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("workflow_readers", ROOT / "tests/test_native_codex_windows_workflow.py")
readers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readers)
_action, _block, _field, _miss_required, _steps = (
    readers._action, readers._block, readers._field, readers._miss_required, readers._steps)


@pytest.fixture
def workflows():
    proof = (ROOT / ".github/workflows/native-codex-linux.yml").read_text()
    build = (ROOT / ".github/workflows/native-codex-linux-build.yml").read_text()
    return proof, build, _block(_block(proof, "jobs"), "native-proof"), _block(_block(build, "jobs"), "build")


def test_proof_never_recompiles_or_changes_sandbox(workflows):
    proof_file, _, proof, _ = workflows
    assert _field(proof, "runs-on") == "ubuntu-22.04"
    assert _field(proof, "needs") == "native-build"
    assert _field(_block(proof_file, "concurrency"), "cancel-in-progress") == "false"
    for step in _steps(proof):
        command = _field(step, "run") or ""
        assert "rust-toolchain" not in _action(step)
        assert not re.search(r"\bcargo\s+(build|install|test)\b", command)
        assert "RUSTY_V8_ARCHIVE" not in command
        assert "danger-full-access" not in command and "use_legacy_landlock=true" not in command
        if _action(step) == "actions/checkout":
            assert _field(_block(step, "with"), "repository") is None


def test_ready_cache_exact_and_all_native_work_gated(workflows):
    _, _, _, build = workflows
    assert _field(build, "runs-on") == "ubuntu-22.04"
    steps = _steps(build)
    candidate = next(step for step in steps if _field(step, "id") == "candidate")
    assert _field(_block(candidate, "with"), "restore-keys") is None
    assert "steps.identity.outputs.key" in _field(_block(candidate, "with"), "key")
    for step in steps:
        command = _field(step, "run") or ""
        if ("rust-toolchain" in _action(step) or "cargo build" in command or " prepare " in command
                or " verify-v8 " in command or " snapshot-build " in command
                or "RUSTY_V8_ARCHIVE" in command or "apt-get install" in command):
            assert _miss_required(step)


def test_bwrap_finalized_and_hashed_before_codex_compile(workflows):
    _, _, _, build = workflows
    steps = _steps(build)
    bwrap = next(step for step in steps if "--bin bwrap" in (_field(step, "run") or ""))
    codex = next(step for step in steps if "--bin codex --bin codex-code-mode-host" in (_field(step, "run") or ""))
    assert steps.index(bwrap) < steps.index(codex)
    command = _field(bwrap, "run")
    assert command.index("strip --strip-debug") < command.index("sha256sum") < command.index("CODEX_BWRAP_SHA256")
    assert "--locked" in command and "--locked" in _field(codex, "run")
    assert "GITHUB_ENV" in command


def test_proof_downloads_ready_candidate_then_sandbox_native_package_install(workflows):
    _, _, proof, build = workflows
    assert any(_action(step) == "actions/upload-artifact" for step in _steps(build))
    assert not any("verify_native_codex_selection" in (_field(step, "run") or "") for step in _steps(build))
    steps = _steps(proof)
    download = next(step for step in steps if _action(step) == "actions/download-artifact")
    assert _field(_block(download, "with"), "name") == "${{ needs.native-build.outputs.artifact-name }}"
    commands = [_field(step, "run") or "" for step in steps]
    indices = [next(i for i, cmd in enumerate(commands) if marker in cmd) for marker in (
        " verify-build ", " sandbox-proof ", "verify_native_codex_selection.py --codex", " package-candidate ", " install-local ")]
    assert indices == sorted(indices) and steps.index(download) < indices[0]
    assert "--restore-executable-modes" in commands[indices[0]]
    assert "--sandbox-proof" in commands[indices[3]]
