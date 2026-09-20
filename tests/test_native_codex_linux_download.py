"""Offline tests for the separate production-HTTPS Linux smoke recipe."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import verify_native_codex_linux_download as smoke


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    runtime, output = tmp_path / "fresh-runtime", tmp_path / "proof"
    env = {"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "not-inherited",
           "BELLO_CODEX_BINARY": "not-used", "GITHUB_TOKEN": "not-inherited"}
    monkeypatch.setattr(smoke, "os", SimpleNamespace(environ=env, path=os.path, devnull=os.devnull))
    monkeypatch.setattr(smoke.platform, "system", lambda: "Linux")
    monkeypatch.setattr(smoke.platform, "machine", lambda: "x86_64")
    manifest_data = b'{"synthetic":"unit fixture"}'
    bundle = smoke.installer.NativeBundle("https://github.com/example/published.tar.gz", "a" * 64,
                                         hashlib.sha256(manifest_data).hexdigest())
    monkeypatch.setattr(smoke.installer, "BUNDLES", {("Linux", "x86_64"): bundle})
    binary = runtime / "cache/bundle/bin/codex"
    manifest = binary.parent.parent / "selection-manifest.json"
    calls = []
    def ensure():
        calls.append("ensure")
        assert not {"OPENAI_API_KEY", "GITHUB_TOKEN", "BELLO_CODEX_BINARY"} & env.keys()
        assert env["HOME"] == env["CODEX_HOME"] == str(runtime / "empty-home")
        if not manifest.exists():
            (binary.parent / "codex-resources").mkdir(parents=True)
            binary.write_bytes(b"synthetic codex")
            (binary.parent / "codex-resources/bwrap").write_bytes(b"synthetic bwrap")
            manifest.write_bytes(manifest_data)
        return [str(binary)], manifest
    monkeypatch.setattr(smoke.installer, "ensure_native_selection", ensure)
    async def capability(command, path):
        assert command == [str(binary)] and path == manifest
        return {"binary_sha256": smoke.linux.sha256(binary)}
    monkeypatch.setattr(smoke, "validate_native_selection", AsyncMock(side_effect=capability))
    state = SimpleNamespace(runtime=runtime, output=output, calls=calls, binary=binary,
                            provider_bad=False, cache_mutation=False)
    async def native(args):
        calls.append("provider")
        assert args.codex == binary and args.cases is None
        args.output_dir.mkdir()
        proof = {"schema": "bello.native-selection-provider-proof.v1", "passed": True,
                 "paid_model_calls": 0, "binary_sha256": smoke.linux.sha256(binary), "platform": "Linux",
                 "cases": [{"case": name, "passed": True, "exact_model_visible_output": True,
                            "focus_and_command_correct": True, "error": None, "provider_requests": 2,
                            "provider_errors": [], "external_proxy_requests_forwarded": 0}
                           for name in smoke.linux.PROOF_CASES]}
        if state.provider_bad:
            proof["cases"].pop()
        (args.output_dir / "report.json").write_text(json.dumps(proof))
        return 0
    monkeypatch.setattr(smoke.native, "main_async", native)
    def sandbox(path, destination):
        calls.append("sandbox")
        assert path == binary
        destination.mkdir()
        report = {"schema": smoke.linux.SANDBOX_SCHEMA, "passed": True,
                  "binary_sha256": smoke.linux.sha256(binary),
                  "bwrap_sha256": smoke.linux.sha256(binary.parent / "codex-resources/bwrap"),
                  "inside_write_succeeded": True, "outside_write_denied": True, "new_user_namespace": True,
                  "tampered_bwrap_exit_code": 8, "system_bwrap_on_path": False, "paid_model_calls": 0}
        (destination / "report.json").write_text(json.dumps(report))
        if state.cache_mutation:
            os.utime(binary, ns=(binary.stat().st_atime_ns, binary.stat().st_mtime_ns + 1_000_000))
        return report
    monkeypatch.setattr(smoke.linux, "sandbox_proof", sandbox)
    return state


@pytest.mark.asyncio
async def test_published_recipe_requires_full_proofs_and_cache_reuse(fixture):
    result = await smoke.verify(fixture.runtime, fixture.output)
    assert result["passed"] is True and result["provider_cases_passed"] == 9
    assert result["published_pin"] and result["cold_cache"] and result["post_proof_cache_unchanged"]
    assert result["bundled_sandbox_passed"] and result["paid_model_calls"] == 0
    assert fixture.calls == ["ensure", "ensure", "provider", "sandbox", "ensure"]
    assert json.loads((fixture.output / "report.json").read_text()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider_bad", "cache_mutation"])
async def test_missing_case_or_mutated_cache_cannot_pass(fixture, failure):
    setattr(fixture, failure, True)
    result = await smoke.verify(fixture.runtime, fixture.output)
    assert result["passed"] is False and result["error_type"] == "ValueError"
    assert result["phase"] == ("nine_provider_cases" if failure == "provider_bad" else "post_proof_cache_reuse")


@pytest.mark.asyncio
async def test_missing_linux_pin_is_explicit_failure_not_skipped_green(fixture, monkeypatch):
    monkeypatch.setattr(smoke.installer, "BUNDLES", {})
    result = await smoke.verify(fixture.runtime, fixture.output)
    assert result["passed"] is False and result["published_pin"] is False
    assert result["phase"] == "preconditions" and fixture.calls == []


@pytest.mark.asyncio
async def test_existing_runtime_cannot_be_presented_as_cold(fixture):
    fixture.runtime.mkdir()
    result = await smoke.verify(fixture.runtime, fixture.output)
    assert result["passed"] is False and result["phase"] == "preconditions"
    assert fixture.calls == []


def test_scoped_workflow_uses_real_production_download_without_build_or_auth():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/native-codex-published-linux.yml").read_text()
    assert "workflow_dispatch:" in workflow and "branches: ['codex/release-0.6.0-readiness']" in workflow
    assert "ubuntu-22.04" in workflow and "persist-credentials: false" in workflow
    assert "verify_native_codex_linux_download.py" in workflow
    assert "linux-published-proof/provider/*/provider-request-*.json" in workflow
    assert "linux-published-proof/sandbox/*.json" in workflow
    for prohibited in ("cargo ", "rust-toolchain", "native-codex-linux-build", "secrets.", "pip install -e"):
        assert prohibited not in workflow
    source = inspect.getsource(smoke)
    assert "installer.ensure_native_selection()" in source and "validate_native_selection" in source
    assert "native.main_async" in source and "linux.sandbox_proof" in source
    assert "replace." not in source and "installer._download" not in source
