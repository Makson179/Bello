"""Portable, credential-free contracts for the post-publication wheel proof."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import textwrap
from types import SimpleNamespace
import zipfile
from zipfile import ZipFile, ZipInfo

import pytest

from tests.test_native_codex_windows_workflow import _action, _block, _field, _steps

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts/installed_wheel_default_smoke"


def load(name):
    spec = importlib.util.spec_from_file_location("wheel_smoke_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare, verify = load("prepare"), load("verify")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def qualified(tmp_path):
    """All hashes/data here are synthetic; no native process or network exists."""
    wheel = tmp_path / "synthetic.whl"
    wheel.write_bytes(b"neutral synthetic wheel identity")
    runtime = tmp_path / "runtime"
    directory = runtime / "native-codex" / ("a" * 64)
    directory.mkdir(parents=True)
    provenance = {"build_receipt_sha256": "b" * 64, "qualification_sha256": "c" * 64,
                  "worker_sha256": "d" * 64}
    write_json(directory / "BUILD-INFO", {"native_build_identity": {"target": "linux-x64"},
                                         "release_provenance": provenance})
    (directory / "bin").mkdir()
    (directory / "bin/codex").write_bytes(b"neutral non-executable fixture")
    files = {name: prepare.digest(directory / name) for name in ("BUILD-INFO", "bin/codex")}
    write_json(directory / "selection-manifest.json", {"files": files})
    bundle = {"url": "https://github.com/Makson179/Bello/releases/download/synthetic-helper/test.tar.gz",
              "archive_sha256": "a" * 64, "manifest_sha256": prepare.digest(directory / "selection-manifest.json")}
    helper = tmp_path / "audited-helper.json"
    write_json(helper, {"schema": "bello.qualified-helper-release.v1", "helper_source_sha": "1" * 40,
                        "targets": {"linux-x64": {"bundle": bundle, "files": files, "release_provenance": provenance}}})
    proof = tmp_path / "published-proof"
    common = {"schema": "bello.native-codex-local-install.v1", "target": "linux-x64",
              "passed": True, "published": True, "phase": "complete", "distribution_source": "production-https",
              "archive_sha256": bundle["archive_sha256"], "manifest_sha256": bundle["manifest_sha256"],
              "cache_unchanged": True, "paid_model_calls": 0, "external_provider_requests_forwarded": 0}
    child = {**common, "local_archive_transfers": 0, "cold_cache": True, "cache_hit": True,
             "post_proof_cache_reusable": True, "installed_directory": str(directory),
             "binary_sha256": files["bin/codex"],
             "cache_files": {name: {"sha256": checksum} for name, checksum in
                             {**files, "selection-manifest.json": bundle["manifest_sha256"]}.items()}}
    write_json(proof / "worker/report.json", child)
    parent = {**common, "candidate_unchanged": True, "worker_report": "worker/report.json",
              "worker_report_sha256": prepare.digest(proof / "worker/report.json")}
    write_json(proof / "installed-proof.json", parent)
    return SimpleNamespace(wheel=wheel, helper=helper, runtime=runtime, directory=directory, proof=proof,
                           parent=parent, child=child, files=files, bundle=bundle)


def make_ledger(fixture):
    return prepare.qualified_ledger(fixture.wheel, fixture.helper, "linux-x64", "2" * 40,
                                    fixture.proof, fixture.runtime)


def update_child(fixture, **updates):
    fixture.child.update(updates)
    write_json(fixture.proof / "worker/report.json", fixture.child)
    fixture.parent["worker_report_sha256"] = prepare.digest(fixture.proof / "worker/report.json")
    write_json(fixture.proof / "installed-proof.json", fixture.parent)


def test_qualified_ledger_binds_distinct_source_wheel_and_provenance(qualified):
    result = make_ledger(qualified)
    assert result["product_source_sha"] == "2" * 40
    assert result["helper_source_sha"] == "1" * 40
    assert result["wheel_sha256"] == prepare.digest(qualified.wheel)
    assert result["files"] == qualified.files
    assert result["qualification_binding"]["build_info_sha256"] == qualified.files["BUILD-INFO"]
    assert result["qualification_binding"]["helper_release_ledger_sha256"] == prepare.digest(qualified.helper)


@pytest.mark.parametrize("field,value", [("passed", False), ("published", False), ("phase", "install"),
                                       ("distribution_source", "local-archive"), ("paid_model_calls", 1),
                                       ("external_provider_requests_forwarded", True)])
def test_nonqualifying_parent_cannot_generate_wheel_ledger(qualified, field, value):
    qualified.parent[field] = value
    write_json(qualified.proof / "installed-proof.json", qualified.parent)
    with pytest.raises(ValueError, match="not PASS"):
        make_ledger(qualified)


def test_worker_hash_binding_is_required(qualified):
    write_json(qualified.proof / "worker/report.json", {**qualified.child, "unbound": True})
    with pytest.raises(ValueError, match="worker binding"):
        make_ledger(qualified)


@pytest.mark.parametrize("updates", [{"local_archive_transfers": 1}, {"local_archive_transfers": False},
                                    {"post_proof_cache_reusable": False}])
def test_local_transfer_and_unproven_reuse_are_rejected(qualified, updates):
    update_child(qualified, **updates)
    with pytest.raises(ValueError, match="local transfer|reuse"):
        make_ledger(qualified)


def test_payload_tamper_is_rejected(qualified):
    (qualified.directory / "bin/codex").write_bytes(b"changed neutral fixture")
    with pytest.raises(ValueError, match="Cache file identity"):
        make_ledger(qualified)


def test_manifest_tamper_is_rejected(qualified):
    write_json(qualified.directory / "selection-manifest.json", {"files": {}})
    with pytest.raises(ValueError, match="manifest hash"):
        make_ledger(qualified)


def test_build_info_provenance_must_match_audited_ledger(qualified):
    data = prepare.read_json(qualified.helper)
    data["targets"]["linux-x64"]["release_provenance"]["qualification_sha256"] = "e" * 64
    write_json(qualified.helper, data)
    with pytest.raises(ValueError, match="BUILD-INFO qualification"):
        make_ledger(qualified)


def test_helper_source_identity_cannot_be_omitted_or_guessed_from_product(qualified):
    data = prepare.read_json(qualified.helper)
    data.pop("helper_source_sha")
    write_json(qualified.helper, data)
    with pytest.raises(ValueError, match="source identity"):
        make_ledger(qualified)


def test_unexpected_installed_directory_is_rejected(qualified):
    update_child(qualified, installed_directory=str(qualified.runtime / "different"))
    with pytest.raises(ValueError, match="Unexpected published-proof cache"):
        make_ledger(qualified)


def test_duplicate_evidence_fields_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"passed": true, "passed": false}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        prepare.read_json(path)


@pytest.mark.parametrize("override", ["BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST"])
def test_explicit_helper_override_is_rejected(override):
    with pytest.raises(ValueError, match="Explicit helper override"):
        verify.require_default_environment({override: "neutral-forbidden-override"})


def test_isolated_environment_does_not_inherit_provider_credentials(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST"):
        monkeypatch.setenv(name, "neutral-test-only-value")
    environment = verify.clean_environment(tmp_path / "home", tmp_path / "cache")
    assert not any(name in environment for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                                                   "BELLO_CODEX_BINARY", "BELLO_CODEX_SELECTION_MANIFEST"))


def test_imported_modules_must_be_exact_installed_wheel_bytes(tmp_path, monkeypatch):
    installed = tmp_path / "venv-site"
    source = installed / "supervisor/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# neutral package fixture\n")
    monkeypatch.setattr(verify, "sys", SimpleNamespace(modules={"supervisor": SimpleNamespace(__file__=str(source))}))
    expected = {"supervisor/__init__.py": verify.digest(source)}
    assert "supervisor" in verify.imported_modules(installed, expected)
    with pytest.raises(ValueError, match="outside installed wheel"):
        verify.imported_modules(tmp_path / "different-site", expected)
    source.write_bytes(b"# changed fixture\n")
    with pytest.raises(ValueError, match="differs from wheel"):
        verify.imported_modules(installed, expected)


@pytest.mark.parametrize("unsafe", ["../outside", "/outside", "supervisor\\escape"])
def test_unsafe_wheel_members_are_rejected(tmp_path, unsafe):
    wheel = tmp_path / "neutral.whl"
    with ZipFile(wheel, "w") as archive:
        # ZipInfo's constructor normalizes backslashes on Windows. Construct
        # the intentionally invalid archive name after that normalization.
        member = ZipInfo("neutral")
        member.filename = member.orig_filename = unsafe
        archive.writestr(member, b"neutral")
    with ZipFile(wheel) as archive:
        assert archive.infolist()[0].orig_filename == unsafe
    with pytest.raises(ValueError, match="Unsafe wheel member"):
        verify.wheel_payload(wheel)


def test_wheel_original_name_is_checked_before_host_normalization(tmp_path, monkeypatch):
    wheel = tmp_path / "neutral.whl"
    unsafe = "supervisor\\escape"
    with ZipFile(wheel, "w") as archive:
        member = ZipInfo("neutral")
        member.filename = member.orig_filename = unsafe
        archive.writestr(member, b"neutral")
        archive.writestr("supervisor/runtime/native_codex_install.py", b"# neutral fixture\n")
    # Exercise Windows ZipInfo normalization without changing the host's os
    # module or pathlib. The archive bytes and original filename stay intact.
    monkeypatch.setattr(zipfile, "os", SimpleNamespace(**(vars(zipfile.os) | {"sep": "\\"})))
    with ZipFile(wheel) as archive:
        member = archive.infolist()[0]
        assert member.orig_filename == unsafe
        assert member.filename == "supervisor/escape"
    with pytest.raises(ValueError, match="Unsafe wheel member"):
        verify.wheel_payload(wheel)


def test_wheel_data_layout_is_checked_without_importing_product(tmp_path):
    wheel = tmp_path / "neutral.whl"
    payload = b"# neutral installer fixture\n"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("bello-0.8.0.data/platlib/supervisor/runtime/native_codex_install.py", payload)
    assert verify.wheel_payload(wheel) == {"supervisor/runtime/native_codex_install.py": hashlib.sha256(payload).hexdigest()}


def test_workflow_runs_actual_wheel_only_after_native_qualification():
    text = (ROOT / ".github/workflows/native-codex-release-download.yml").read_text()
    inputs = _block(_block(_block(text, "on"), "workflow_dispatch"), "inputs")
    assert _field(_block(inputs, "helper_release_ledger"), "required") == "true"
    steps = _steps(_block(_block(text, "jobs"), "published-install"))
    native = next(i for i, step in enumerate(steps) if "install-published" in (_field(step, "run") or ""))
    wheel = next(i for i, step in enumerate(steps) if "scripts/installed_wheel_default_smoke/prepare.py" in (_field(step, "run") or ""))
    assert native < wheel
    assert _field(steps[wheel], "if") is None and _field(steps[wheel], "continue-on-error") is None
    command = _field(steps[wheel], "run")
    assert '[sys.executable, "-m", "build", "--wheel"' in command
    assert '["git", "rev-parse", "HEAD"]' in command and '["git", "diff", "--quiet", "HEAD", "--"]' in command
    assert '"-I", "-B"' in command and 'subprocess.run(prepared["driver_command"], cwd=output / "work", check=True)' in command
    assert 'os.environ["LOCALAPPDATA"] if os.name == "nt" else os.environ["RUNNER_TEMP"]' in command
    assert '"--helper-ledger"' in command and '"--published-proof"' in command and '"--published-runtime-root"' in command
    assert "pip install -e" not in command and "BELLO_CODEX_BINARY" not in command
    assert "helper_source_sha" not in command  # Never invent this identity from github.sha.
    # _field intentionally strips each line for string assertions; parse the
    # original YAML literal body so Python block indentation stays significant.
    python = re.search(r"^([ ]*)python - <<'PY'\n(.*?)^\1PY\s*$", steps[wheel], re.S | re.M)
    assert python is not None
    ast.parse(textwrap.dedent(python.group(2)))
    for path in (SCRIPTS / "prepare.py", SCRIPTS / "verify.py"):
        ast.parse(path.read_text())
    assert not any(_action(step) == "actions/download-artifact" for step in steps)
