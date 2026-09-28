"""Offline regressions for actual published async-download qualification."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify_native_codex_async_download as smoke


def test_workflow_is_manual_download_qualification_not_build_or_release():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/native-codex-published-async.yml").read_text()
    assert "workflow_dispatch:" in text and "\n  push:" not in text
    assert "persist-credentials: false" in text and "contents: read" in text
    assert "verify_native_codex_async_download.py" in text
    assert "ubuntu-22.04" in text and "windows-2025" in text
    # Windows RUNNER_TEMP can have a shared writable ACL. Use the same private
    # per-user anchor as the already-qualified installed-cache workflow.
    assert 'if [[ "$RUNNER_OS" == "Windows" ]]; then' in text
    assert 'runtime_parent="$LOCALAPPDATA"' in text
    assert 'runtime_parent="$RUNNER_TEMP"' in text
    assert '--runtime-root "$runtime_parent/bello-async-published-' in text
    assert not any(value in text for value in ("secrets.", "cargo ", "gh release", "upload-release"))


def good_proof():
    return {"schema": "bello.native-async-smoke.v1", "passed": True, "paid_model_calls": 0,
            "on_off_native_instructions_identical": True,
            "results": [{"case": name, "passed": True, "external_proxy_requests_forwarded": 0}
                        for name in sorted(smoke.CASES)]}


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "failed", "forwarded", "paid", "prompt"])
def test_incomplete_or_unsafe_proof_rejected(corruption):
    proof = good_proof()
    if corruption == "missing": proof["results"].pop()
    elif corruption == "duplicate": proof["results"][0] = proof["results"][1]
    elif corruption == "failed": proof["results"][0]["passed"] = False
    elif corruption == "forwarded": proof["results"][0]["external_proxy_requests_forwarded"] = 1
    elif corruption == "paid": proof["paid_model_calls"] = 1
    else: proof["on_off_native_instructions_identical"] = False
    with pytest.raises(ValueError): smoke.validate_proof(proof)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runtime, output = tmp_path / "runtime", tmp_path / "output"
    env = {"PATH": "/usr/bin", "OPENAI_API_KEY": "synthetic-do-not-inherit",
           "ANTHROPIC_API_KEY": "synthetic-do-not-inherit", "HOME": "/original",
           "BELLO_CODEX_BINARY": "/wrong", "HTTPS_PROXY": "http://wrong"}
    original = dict(env)
    monkeypatch.setattr(smoke, "os", SimpleNamespace(environ=env, path=os.path, devnull=os.devnull))
    monkeypatch.setattr(smoke.installer, "_platform_key", lambda: ("Linux", "x86_64"))
    manifest = runtime / "cache/bundle/selection-manifest.json"
    binary = manifest.parent / "bin/codex"
    import hashlib
    bundle = smoke.installer.NativeBundle("https://github.com/Makson179/Bello/releases/download/test/a.tar.gz",
                                         "a" * 64, hashlib.sha256(b"fixture").hexdigest())
    monkeypatch.setattr(smoke.installer, "ASYNC_BUNDLES", {("Linux", "x86_64"): bundle})
    calls = []

    def install():
        assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BELLO_CODEX_BINARY", "HTTPS_PROXY"} & env.keys()
        assert env["CODEX_HOME"] == str(runtime / "empty-home")
        calls.append("install")
        if not manifest.exists():
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture executable")
            manifest.write_bytes(b"fixture")
        return [str(binary), "app-server"], manifest

    state = SimpleNamespace(runtime=runtime, output=output, env=env, original=original,
                            calls=calls, mutation=False, failure=False)

    async def capability(command, path):
        assert command[0] == str(binary) and path == manifest
        return {"binary_sha256": smoke.installer._sha256(binary)}

    async def proof(path, destination, *, concurrency_repeats):
        assert path == binary and concurrency_repeats == 3
        destination.mkdir()
        value = good_proof()
        if state.failure: value["results"].pop()
        if state.mutation: binary.write_bytes(b"changed executable")
        (destination / "report.json").write_text(json.dumps(value))
        return value

    monkeypatch.setattr(smoke.installer, "ensure_native_async", install)
    monkeypatch.setattr(smoke, "validate_native_selection", capability)
    monkeypatch.setattr(smoke, "verify_async", proof)
    return state


@pytest.mark.asyncio
async def test_cold_install_full_proof_cache_reuse_and_environment_restore(setup):
    result = await smoke.verify(setup.runtime, setup.output)
    assert result["passed"] and result["cases_passed"] == 13
    assert result["cold_cache"] and result["cache_unchanged"]
    assert setup.calls == ["install", "install"]
    assert setup.env == setup.original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mutation", "failure", "existing", "no_pin"])
async def test_fail_closed_with_retained_receipt_and_environment(setup, monkeypatch, mode):
    if mode in {"mutation", "failure"}: setattr(setup, mode, True)
    elif mode == "existing": setup.runtime.mkdir()
    else: monkeypatch.setattr(smoke.installer, "ASYNC_BUNDLES", {})
    result = await smoke.verify(setup.runtime, setup.output)
    assert not result["passed"] and result["error_type"] == "ValueError"
    assert setup.env == setup.original
    assert json.loads((setup.output / "report.json").read_text()) == result
    if mode in {"existing", "no_pin"}: assert not setup.calls
