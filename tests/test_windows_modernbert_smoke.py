from __future__ import annotations

from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import verify_windows_modernbert as smoke


class CharacterTokenizer:
    def __call__(self, text, **kwargs):
        return {"input_ids": list(text.encode("utf-8"))}


def test_synthetic_short_and_long_sizes_are_mechanical_and_below_native_budget():
    logs = smoke.synthetic_logs(CharacterTokenizer())
    assert len(logs["short"]) < 2000
    assert 9500 <= len(logs["long"]) < 35000
    assert all("AssertionError: expected 3, received 4" in text for text in logs.values())


@pytest.mark.asyncio
async def test_observation_requires_successful_real_worker_exchange_and_preserves_output(tmp_path, monkeypatch):
    selected = "FAILED expected 3, received 4\n"
    monkeypatch.setattr(smoke.LogDistiller, "_exchange", AsyncMock(return_value=selected))
    async def base_distill(self, text, focus, command):
        self._process = SimpleNamespace(pid=123)
        return await self._exchange({"id": 1, "log": text, "focus": focus, "command": command})
    monkeypatch.setattr(smoke.LogDistiller, "distill", base_distill)
    monkeypatch.setattr(smoke, "make_entry", lambda *args: ([{}], []))
    selector = smoke.ObservedDistiller(tmp_path, CharacterTokenizer())
    raw = "PASS ignored\n" * 20 + selected
    assert await selector.distill(raw, "focus", "command") == selected
    assert await selector.distill(raw, "focus", "command") == selected
    first, second = selector.measurements
    assert first["cold"] is True and second["cold"] is False
    assert first["worker_pid"] == second["worker_pid"] == 123
    assert first["worker_response_ok"] and first["returned_worker_output"] and first["strictly_reduced"]
    assert first["original_bytes"] == len(raw) and first["selected_bytes"] == len(selected)
    assert first["selected_sha256"] == smoke.digest(selected)


@pytest.mark.asyncio
async def test_fail_open_original_is_not_counted_as_model_success(tmp_path, monkeypatch):
    async def fallback(self, text, focus, command):
        return text
    monkeypatch.setattr(smoke.LogDistiller, "distill", fallback)
    monkeypatch.setattr(smoke, "make_entry", lambda *args: ([{}], []))
    selector = smoke.ObservedDistiller(tmp_path, CharacterTokenizer())
    assert await selector.distill("original", "focus", "command") == "original"
    observed = selector.measurements[0]
    assert observed["worker_response_ok"] is False
    assert observed["returned_worker_output"] is False
    assert observed["strictly_reduced"] is False


def test_workflow_uses_published_downloads_without_training_or_auth_by_default():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/windows-modernbert-smoke.yml").read_text()
    assert "verify_windows_modernbert.py" in workflow
    assert "--native-regressions" in workflow
    assert "windows-modernbert-proof/native-regressions/report.json" in workflow
    assert "windows-2025" in workflow
    assert "https://download.pytorch.org/whl/cpu" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "windows-modernbert-proof/*/provider-request-*.json" in workflow
    assert "native-codex-windows-build" not in workflow


def test_fixture_extension_does_not_change_existing_native_defaults():
    import inspect
    signature = inspect.signature(smoke.native.run_case)
    assert signature.parameters["selector"].default is None
    assert signature.parameters["raw_output"].default == smoke.native.RAW
    assert signature.parameters["turn_timeout"].default == 60


def test_live_workflow_is_opt_in_and_only_exports_sanitized_receipt():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/windows-modernbert-smoke.yml").read_text()
    assert "run_live:" in workflow and "default: false" in workflow
    assert "github.event_name == 'workflow_dispatch' && inputs.run_live" in workflow
    assert "always() && github.event_name == 'workflow_dispatch' && inputs.run_live" in workflow
    assert "secrets.BELLO_WINDOWS_SMOKE_AUTH_20260919" in workflow
    assert "Join-Path $env:RUNNER_TEMP" in workflow
    artifacts = workflow.split("path: |", 1)[1]
    assert "windows-modernbert-live/report.json" in artifacts
    assert "auth.json" not in artifacts and "RUNNER_TEMP" not in artifacts and "live/*" not in artifacts


def test_live_solution_check_does_not_execute_generated_code(tmp_path):
    from scripts import verify_windows_modernbert_live as live
    (tmp_path / "check.py").write_text(live.CHECKER)
    (tmp_path / "solution.py").write_text(live.CORRECT)
    assert live.solution_passes(tmp_path)
    (tmp_path / "solution.py").write_text(live.CORRECT + "\nraise RuntimeError('not executed')\n")
    assert not live.solution_passes(tmp_path)
    (tmp_path / "solution.py").write_text(live.CORRECT)
    (tmp_path / "check.py").write_text("print('SMOKE_TESTS_PASSED')")
    assert not live.solution_passes(tmp_path)


def test_live_history_check_uses_only_exact_owned_thread_and_model_facing_packets(tmp_path):
    from scripts import verify_windows_modernbert_live as live
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    payload = {"type": "function_call_output", "call_id": "actual-native-call", "output":
               "Chunk ID: fixture\nProcess exited with code 1\nOutput:\nselected diagnostic"}
    (sessions / "rollout-owned.jsonl").write_text(json.dumps({"type": "response_item", "payload": payload}) + "\n")
    payload["output"] = payload["output"].replace("selected diagnostic", "unrelated")
    (sessions / "rollout-other.jsonl").write_text(json.dumps({"type": "response_item", "payload": payload}) + "\n")
    assert live.selected_history_hashes(tmp_path, "owned") == {live.digest("selected diagnostic")}
