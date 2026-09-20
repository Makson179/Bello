from pathlib import Path
import json

import pytest

from scripts import verify_native_codex_history as smoke


@pytest.mark.asyncio
@pytest.mark.parametrize("parseable", [True, False])
async def test_persistent_fixture_uses_fake_provider_metadata_and_exact_history(tmp_path, monkeypatch, parseable):
    output = tmp_path / "proof"
    output.mkdir()
    path = output / "case/empty-home/sessions/owned.jsonl"
    calls = []
    class Session:
        async def request(self, method, params):
            calls.append(method)
            if method == "thread/start":
                assert params["modelProvider"] == "bello_fixture"
                assert params["model"] == "gpt-5.6-luna" and params["ephemeral"] is False
            else:
                assert method == "thread/read" and params == {"threadId": "owned", "includeTurns": True}
            return {"thread": {"id": "owned", "path": str(path)}}
        async def complete(self, thread_id, *, timeout):
            assert thread_id == "owned"
    monkeypatch.setattr(smoke.native, "NativeSession", Session)
    async def fixture(binary, case, directory):
        session = smoke.native.NativeSession()
        await session.request("thread/start", {"modelProvider": "bello_fixture", "ephemeral": True})
        await session.complete("owned")
        path.parent.mkdir(parents=True)
        value = ("Process exited with code 7\nOutput:\n" + smoke.native.SELECTED) if parseable else "unsupported"
        records = [{"type": "session_meta", "payload": {"id": "owned"}},
                   {"type": "response_item", "payload": {"type": "function_call_output", "output": value}}]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        return {"passed": True}
    monkeypatch.setattr(smoke.native, "run_case", fixture)
    report = await smoke.verify(tmp_path / "codex", output)
    assert report["passed"] is parseable and report["exact_selected_history_match"] is parseable
    assert report["provider_passed"] is True and report["paid_model_calls"] == 0
    assert calls == ["thread/start", "thread/read"]
    assert smoke.native.NativeSession is Session


def test_windows_history_workflow_has_no_auth_optional_model_or_rust_build():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/native-codex-history-smoke.yml").read_text()
    assert "windows-2025" in workflow and "workflow_dispatch:" in workflow
    assert "persist-credentials: false" in workflow and "--runtime-root" in workflow
    assert "empty-home/sessions/**/*.jsonl" in workflow
    for forbidden in ("secrets.", "cargo ", "rust-toolchain", "torch", "huggingface", "run_live"):
        assert forbidden not in workflow
