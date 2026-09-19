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
    assert first["worker_exchange_attempted"] is True and first["worker_error_type"] is None
    assert selector.worker_failure_types == []
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
    assert observed["worker_exchange_attempted"] is False and observed["worker_error_type"] is None
    assert selector.worker_failure_types == []


@pytest.mark.asyncio
async def test_real_exchange_exception_is_recorded_without_error_text_or_payload(tmp_path, monkeypatch, caplog):
    error = ValueError("never-export-worker-payload-or-secret")
    exchange = AsyncMock(side_effect=error)
    monkeypatch.setattr(smoke.LogDistiller, "_exchange", exchange)
    monkeypatch.setattr(smoke.LogDistiller, "_finish_cleanup", AsyncMock())
    monkeypatch.setattr(smoke, "make_entry", lambda *args: ([{}], []))
    selector = smoke.ObservedDistiller(tmp_path, CharacterTokenizer())
    assert await selector.distill("original private log", "focus", "command") == "original private log"
    measurement = selector.measurements[0]
    assert measurement["worker_exchange_attempted"] is True
    assert measurement["worker_response_ok"] is False
    assert measurement["worker_error_type"] == "ValueError"
    assert selector.worker_failure_types == ["ValueError"]
    exported = json.dumps(measurement) + json.dumps(selector.worker_failure_types) + caplog.text
    assert "never-export" not in exported and "original private log" not in exported


@pytest.mark.asyncio
@pytest.mark.parametrize("text,focus", [("", "focus"), ("log", "  ")])
async def test_ineligible_requests_are_not_worker_failures(tmp_path, monkeypatch, text, focus):
    exchange = AsyncMock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(smoke.LogDistiller, "_exchange", exchange)
    monkeypatch.setattr(smoke, "make_entry", lambda *args: ([], []))
    selector = smoke.ObservedDistiller(tmp_path, CharacterTokenizer())
    assert await selector.distill(text, focus, "command") == text
    exchange.assert_not_called()
    assert selector.worker_failure_types == []
    assert selector.measurements[0]["worker_exchange_attempted"] is False
    assert selector.measurements[0]["worker_error_type"] is None


def test_live_pass_gate_requires_zero_worker_failures_even_when_task_and_delivery_pass():
    from scripts import verify_windows_modernbert_live as live
    result = {"task_passed": True, "successful_native_check": True, "reduced_logs": 3,
              "exact_selected_native_history_matches": 3, "bridge_outcomes": {"changed": 3},
              "worker_failures": 0}
    assert live.live_checks_pass(result)
    assert not live.live_checks_pass({**result, "worker_failures": 1})
    assert not live.live_checks_pass({**result, "exact_selected_native_history_matches": 0})


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
    assert 'Join-Path $modelRoot "live-auth"' in workflow
    assert "Join-Path $env:RUNNER_TEMP" not in workflow
    assert "if: ${{ !(github.event_name == 'workflow_dispatch' && inputs.run_live) }}" in workflow
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
    (sessions / "rollout-owned.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "owned"}}) + "\n" +
        json.dumps({"type": "response_item", "payload": payload}) + "\n")
    payload["output"] = payload["output"].replace("selected diagnostic", "unrelated")
    (sessions / "rollout-other.jsonl").write_text(json.dumps({"type": "response_item", "payload": payload}) + "\n")
    assert live.selected_history_hashes(tmp_path, "owned") == {live.digest("selected diagnostic")}


@pytest.mark.parametrize("kind,value,expected", [
    ("function_call_output", "Chunk ID: a\nProcess running with session ID 19\nOutput:\npartial\r\n", ["partial\r\n"]),
    ("function_call_output", [{"type": "input_text", "text": "Process exited with code 0\nOutput:\nkept\n"}], ["kept\n"]),
    ("custom_tool_call_output", 'Wall time 1 seconds\nOutput:\n{"output":"code\\n","exit_code":0}', ["code\n"]),
    ("function_call_output", {"body": "Process exited with code 0\nOutput:\nnot-native"}, []),
    ("function_call_output", "log itself mentions\nOutput:\nnot-a-native-packet", []),
])
def test_live_history_parser_uses_native_wire_formats_without_fuzzy_matching(kind, value, expected):
    from scripts import verify_windows_modernbert_live as live
    assert live.history_output_texts({"type": kind, "output": value}) == expected


def test_live_history_uses_explicit_owned_rollout_path_and_reports_counts(tmp_path):
    from scripts import verify_windows_modernbert_live as live
    path = tmp_path / "sessions" / "rollout-different-file-id.jsonl"
    path.parent.mkdir()
    records = [
        {"type": "session_meta", "payload": {"id": "owned"}},
        {"type": "event_msg", "payload": {"aggregatedOutput": "never-use-this"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output":
            "Process exited with code 0\nOutput:\nexact\r\n"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "unrecognized tool"}},
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    hashes, counts = live.selected_history_evidence(tmp_path, "owned", str(path))
    assert hashes == {live.digest("exact\r\n")}
    assert counts == {"path_from_server": True, "files_found": 1, "owned_files": 1,
                      "records": 4, "response_items": 2, "tool_outputs": 2,
                      "parsed_outputs": 1, "unparsed_tool_outputs": 1}
    records[0]["payload"]["id"] = "another-thread"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    with pytest.raises(ValueError, match="exact smoke thread"):
        live.selected_history_evidence(tmp_path, "owned", str(path))
    path.write_text("malformed json\n")
    with pytest.raises(ValueError):
        live.selected_history_evidence(tmp_path, "owned", str(path))
    with pytest.raises(ValueError, match="outside"):
        live.selected_history_evidence(tmp_path, "owned", str(tmp_path.parent / "unrelated.jsonl"))
    hashes, counts = live.selected_history_evidence(tmp_path, "owned", str(tmp_path / "missing.jsonl"))
    assert not hashes and counts["files_found"] == counts["owned_files"] == counts["records"] == 0


@pytest.mark.parametrize("failure_phase", [None, "prepare_private_runtime", "prepare_private_auth_home", "download_model"])
def test_live_bootstrap_records_safe_phases_and_cleans_owned_auth(tmp_path, monkeypatch, capsys, failure_phase):
    from scripts import verify_windows_modernbert_live as live
    runtime = tmp_path / "local-appdata" / "model-runtime"
    home = runtime / "live-auth"
    output = tmp_path / "receipt"
    payload = json.dumps({"tokens": {"access_token": "synthetic-never-export-this"}})
    environment = {live.SECRET_ENV: payload, "LOCALAPPDATA": str(runtime.parent),
                   "UNRELATED_SECRET": "also-never-export"}
    # Replace only this script's environment, not pytest's process/platform.
    monkeypatch.setattr(live, "os", SimpleNamespace(name="nt", environ=environment, path=live.os.path))
    monkeypatch.setattr(live.sys, "argv", ["smoke", "--runtime-root", str(runtime),
                                           "--auth-home", str(home), "--output-dir", str(output)])
    private_paths = []
    def private_directory(path, *, parents=False):
        private_paths.append(path)
        if (failure_phase == "prepare_private_runtime" and path == runtime
                or failure_phase == "prepare_private_auth_home" and path == home):
            raise ValueError("synthetic-sensitive-error-never-export")
        path.mkdir(parents=parents, exist_ok=True)
    monkeypatch.setattr(live, "_private_directory", private_directory)
    monkeypatch.setattr(live, "ensure_native_selection", lambda: ([str(runtime / "codex.exe")], runtime / "manifest"))
    monkeypatch.setattr(live, "validate_native_selection", AsyncMock())
    def bundle():
        if failure_phase == "download_model":
            raise ValueError("synthetic-sensitive-error-never-export")
        return runtime / "model"
    monkeypatch.setattr(live, "ensure_default_bundle", bundle)
    async def turn(*args):
        assert json.loads((home / "auth.json").read_text()) == json.loads(payload)
        assert live.SECRET_ENV not in environment and "UNRELATED_SECRET" not in environment
        return {"passed": True, "paid_turns_started": 1, "phase": "complete"}
    turn_mock = AsyncMock(side_effect=turn)
    monkeypatch.setattr(live, "live_turn", turn_mock)
    assert live.main() == (0 if failure_phase is None else 1)
    receipt = json.loads((output / "report.json").read_text())
    assert receipt["phase"] == (failure_phase or "complete")
    assert receipt["auth_removed"] is True and not home.exists()
    assert private_paths[0] == runtime
    assert receipt["checkpoints"]["environment_isolated"] is True
    assert receipt["paid_turns_started"] == (0 if failure_phase else 1)
    if failure_phase:
        assert receipt["error_type"] == "ValueError"
        turn_mock.assert_not_called()
    exported = (output / "report.json").read_text() + capsys.readouterr().out
    assert "synthetic-never-export-this" not in exported
    assert "synthetic-sensitive-error-never-export" not in exported
    assert "also-never-export" not in exported
