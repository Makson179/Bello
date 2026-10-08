"""Prepublication artifact gate contracts; no publication or provider access."""
import re
from pathlib import Path

from tests.test_native_codex_windows_workflow import _action, _block, _field, _steps


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release-package-validation.yml"


def jobs():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    mapping = _block(workflow, "jobs")
    return workflow, {name: _block(mapping, name) for name in re.findall(r"^  ([a-z][a-z-]+):$", mapping, re.M)}


def test_release_validation_triggers_are_explicit_and_never_publish():
    workflow, mapping = jobs()
    triggers = _block(workflow, "on")
    assert _field(_block(triggers, "push"), "branches") == "[codex/072-recovery-validation]"
    assert _field(_block(triggers, "pull_request"), "branches") == "[main]"
    assert "workflow_dispatch:" in triggers and "release:" not in triggers
    assert _field(_block(workflow, "permissions"), "contents") == "read"
    assert set(mapping) == {"portable", "windows", "clean-install"}
    assert not any(value in workflow for value in ("secrets.", "id-token:", "gh release", "twine upload", "git push",
                                                   "pip install -e", "runtime login", "runtime install", "continue-on-error"))
    for job in mapping.values():
        for step in _steps(job):
            if _action(step) == "actions/checkout":
                assert _field(_block(step, "with"), "persist-credentials") == "false"


def test_portable_build_checks_metadata_and_rebuilds_sdist_before_upload():
    _, mapping = jobs()
    job = mapping["portable"]
    assert _field(job, "runs-on") == "ubuntu-latest"
    steps = _steps(job)
    python = next(step for step in steps if _action(step) == "actions/setup-python")
    assert _field(_block(python, "with"), "python-version") == "3.11"
    command = next(_field(step, "run") for step in steps if "build --sdist" in (_field(step, "run") or ""))
    assert "set -euo pipefail" in command
    assert command.index("build --sdist --wheel") < command.index("twine check") < command.index("pip wheel --no-deps")
    assert command.index("pip wheel --no-deps") < command.index("verify_release_install.py build-check")
    assert "--rebuilt-dir rebuilt" in command and '--source-sha "${{ github.sha }}"' in command


def test_windows_build_uses_published_recipe_and_checks_native_payload():
    _, mapping = jobs()
    steps = _steps(mapping["windows"])
    assert _field(mapping["windows"], "runs-on") == "windows-2022"
    rust = next(step for step in steps if "rust-toolchain" in _action(step))
    assert _field(rust, "uses") == "dtolnay/rust-toolchain@1.98.1"
    assert _field(_block(rust, "with"), "targets") == "x86_64-pc-windows-msvc"
    command = next(_field(step, "run") for step in steps if "build --wheel" in (_field(step, "run") or ""))
    assert command.count("if ($LASTEXITCODE -ne 0)") == 4
    assert command.index("build --wheel") < command.index("twine check") < command.index("verify_release_install.py")
    assert "--windows" in command


def test_every_clean_install_uses_the_requested_matrix_and_exact_head_artifact():
    _, mapping = jobs()
    job = mapping["clean-install"]
    assert _field(job, "needs") == "[portable, windows]"
    assert _field(job, "if") is None
    strategy = _block(job, "strategy")
    assert _field(strategy, "fail-fast") == "false"
    matrix = _block(_block(strategy, "matrix"), "include")
    assert re.findall(r'- os: (\S+)\s+python: "([^"]+)"\s+distribution: (\S+)', matrix) == [
        ("ubuntu-latest", "3.11", "portable"), ("ubuntu-latest", "3.14", "portable"),
        ("macos-15", "3.13", "portable"), ("windows-2022", "3.11", "windows"),
        ("windows-2025", "3.14", "windows"),
    ]
    steps = _steps(job)
    download = next(step for step in steps if _action(step) == "actions/download-artifact")
    assert _field(_block(download, "with"), "name") == "bello-release-${{ matrix.distribution }}-${{ github.sha }}"
    command = next(_field(step, "run") for step in steps if "clean-install" in (_field(step, "run") or ""))
    assert '--work-dir "$RUNNER_TEMP/bello-release-install"' in command
    assert "--wheel-dir incoming/dist --build-report incoming/build-report.json" in command
    assert '--source-sha "${{ github.sha }}"' in command


def test_all_logs_and_receipts_are_preserved_on_failure_with_distinct_names():
    _, mapping = jobs()
    names = []
    for job in mapping.values():
        uploads = [step for step in _steps(job) if _action(step) == "actions/upload-artifact"]
        assert len(uploads) == 1
        upload = uploads[0]
        assert _field(upload, "if") == "always()"
        fields = _block(upload, "with")
        assert _field(fields, "if-no-files-found") == "error"
        name = _field(fields, "name")
        assert "${{ github.sha }}" in name
        names.append(name)
        path = _field(fields, "path")
        assert ".json" in path and ".log" in path
        assert "venv" not in path and "home" not in path
    assert len(set(names)) == 3
