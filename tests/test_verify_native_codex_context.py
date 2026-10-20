"""Keep the native loopback proof fail-closed without executing a provider."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import verify_native_codex_context as proof


@pytest.mark.asyncio
async def test_context_setup_failure_is_in_existing_uploaded_receipt_and_report(tmp_path, monkeypatch):
    failure = proof.WindowsSetupFailure(
        1, b"", b"setup_transaction_lock_failed: os error 5 private-fixture-token")
    server = SimpleNamespace(server_port=1234, serve_forever=Mock(), shutdown=Mock(), server_close=Mock())
    worker = SimpleNamespace(start=Mock(), join=Mock())
    session = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(proof, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(proof, "ThreadingHTTPServer", Mock(return_value=server))
    monkeypatch.setattr(proof, "threading", SimpleNamespace(Thread=Mock(return_value=worker), Event=proof.threading.Event))
    monkeypatch.setattr(proof, "ReceiptSession", Mock(return_value=session))
    provision = AsyncMock(side_effect=failure)
    monkeypatch.setattr(proof, "provision_windows_sandbox", provision)
    binary = tmp_path / "not-executed.exe"
    binary.write_bytes(b"fixture identity only")
    output = tmp_path / "context"

    report = await proof.verify(binary, output, cases=["empty"])

    result = report["results"][0]
    assert report["passed"] is False and result["passed"] is False
    assert result["failure"] == {"stage": "startup", "type": "WindowsSetupFailure"}
    assert result["regular_requests"] == result["summary_requests"] == 0
    assert result["windows_setup_failure"] == failure.diagnostic
    assert json.loads((output / "empty/receipt.json").read_text())["windows_setup_failure"] == failure.diagnostic
    assert json.loads((output / "report.json").read_text())["results"][0]["windows_setup_failure"] == failure.diagnostic
    assert "private-fixture-token" not in json.dumps(report)
    session.start.assert_not_awaited()
    session.close.assert_awaited_once()
    provision.assert_awaited_once()


def message(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def request(items):
    return {"model": "gpt-5.5", "reasoning": {"effort": "high"}, "input": items}


def off_fixture():
    initial, correction = message(proof.INITIAL), message(proof.CORRECTION)
    call = proof.tool("round-1", "exec_command", {"cmd": "synthetic"})
    output = {"type": "function_call_output", "call_id": "round-1", "output": proof.fixture_text(1)}
    complete = [initial, proof.reasoning(1), call, output, correction]
    return SimpleNamespace(regular=[request([initial]), request(complete), request(complete + [message(proof.INSPECT)])],
        summaries=[], regular_summary_counts=[0, 0, 0], regular_rounds=[0, 1, 1],
        emitted_calls={1: call}, round=1, pending=False)


def evaluate(provider):
    return proof.evaluate(provider, enabled=False, failure=None, status="completed", resumed=True,
                          archive_present=True, execution_counts=[1] * provider.round)


def test_valid_off_receipt_proves_exact_native_tail_and_prefix():
    assert all(evaluate(off_fixture()).values())


def replace_all_retained_payloads(provider, payload):
    for value in provider.regular:
        value["input"] = copy.deepcopy(value["input"])
        for item in value["input"]:
            if item.get("type") == "function_call_output":
                item["output"] = payload


def test_windows_text_writer_reproduces_false_whole_tail_receipt(tmp_path):
    path = tmp_path / "old-history.data"
    # Reproduce Windows newline=None translation on every test platform.
    path.write_text(proof.fixture_text(1), encoding="utf-8", newline="\r\n")
    payload = path.read_bytes().decode("utf-8")
    assert "\r\n" in payload
    provider = off_fixture()
    replace_all_retained_payloads(provider, payload)
    checks = evaluate(provider)
    assert [name for name, passed in checks.items() if not passed] == [
        "whole_oversized_latest_round_retained"]


@pytest.mark.asyncio
async def test_history_producer_writes_canonical_bytes_with_windows_text_translation(tmp_path, monkeypatch):
    original_write_text = Path.write_text

    def windows_write_text(path, data, *args, **kwargs):
        kwargs["newline"] = "\r\n"
        return original_write_text(path, data, *args, **kwargs)

    class FixturePrepared(Exception):
        pass

    def stop_before_provider(*_args, **_kwargs):
        raise FixturePrepared

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    monkeypatch.setattr(proof, "ContextProvider", stop_before_provider)
    output = tmp_path / "case"
    # Exercise the actual file producer, stopping before any server or runtime.
    with pytest.raises(FixturePrepared):
        await proof.verify_case(tmp_path / "not-executed", output)
    for number in range(1, proof.ROUNDS + 1):
        assert (output / "work" / f"history-{number}.data").read_bytes() == proof.fixture_text(number).encode("utf-8")
    provider = off_fixture()
    replace_all_retained_payloads(provider, (output / "work" / "history-1.data").read_bytes().decode("utf-8"))
    assert all(evaluate(provider).values())


def test_windows_command_emits_file_bytes_without_powershell_formatting(tmp_path, monkeypatch):
    provider = proof.ContextProvider(tmp_path, enabled=True)
    monkeypatch.setattr(proof, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(proof, "windows_shell", lambda: "synthetic-powershell.exe")
    command = provider.command(3)
    assert command["call_id"] == "round-3"
    assert json.loads(command["arguments"]) == {
        "cmd": "[IO.File]::AppendAllText('execution-3.txt', 'x'); "
               "$fixtureBytes = [IO.File]::ReadAllBytes('history-3.data'); "
               "[Console]::OpenStandardOutput().Write($fixtureBytes, 0, $fixtureBytes.Length)",
        "yield_time_ms": 30000, "max_output_tokens": 16000, "login": False,
        "shell": "synthetic-powershell.exe"}


@pytest.mark.parametrize("damage", ["dropped", "truncated", "interior_mutation"])
def test_identically_damaged_retained_payloads_still_fail_whole_tail(damage):
    payload = proof.fixture_text(1)
    if damage == "dropped":
        payload = ""
    elif damage == "truncated":
        payload = payload[:-1]
    else:
        middle = len(payload) // 2
        payload = payload[:middle] + "!" + payload[middle + 1:]
    provider = off_fixture()
    replace_all_retained_payloads(provider, payload)
    checks = evaluate(provider)
    assert checks["retained_native_blocks_unchanged"]
    assert not checks["whole_oversized_latest_round_retained"]


def test_native_reasoning_null_content_is_not_a_message():
    assert proof.message_text({"type": "reasoning", "content": None}) == ""


@pytest.mark.parametrize("mutation, check", [
    ("rewrite_prefix", "prefix_unchanged"),
    ("drop_call", "call_result_pairs_intact"),
    ("drop_result", "whole_oversized_latest_round_retained"),
    ("drop_reasoning", "whole_oversized_latest_round_retained"),
    ("rewrite_reasoning", "retained_native_blocks_unchanged"),
    ("rewrite_output", "retained_native_blocks_unchanged"),
    ("duplicate_output", "call_result_pairs_intact"),
    ("drop_correction", "latest_correction_preserved"),
    ("summaries_accumulate", "one_current_summary"),
])
def test_proof_rejects_loss_or_mutation(mutation, check):
    provider = off_fixture()
    retained = copy.deepcopy(provider.regular[-1]["input"])
    provider.regular[-1]["input"] = retained
    if mutation == "rewrite_prefix":
        retained[0] = message("Changed initial requirement")
    elif mutation == "drop_call":
        retained[:] = [item for item in retained if item.get("type") != "function_call"]
    elif mutation == "drop_result":
        retained[:] = [item for item in retained if item.get("type") != "function_call_output"]
    elif mutation == "drop_reasoning":
        retained[:] = [item for item in retained if item.get("type") != "reasoning"]
    elif mutation == "rewrite_reasoning":
        next(item for item in retained if item.get("type") == "reasoning")["summary"] = []
    elif mutation == "rewrite_output":
        next(item for item in retained if item.get("type") == "function_call_output")["output"] += "altered"
    elif mutation == "duplicate_output":
        retained.append(copy.deepcopy(next(item for item in retained if item.get("type") == "function_call_output")))
    elif mutation == "drop_correction":
        retained[:] = [item for item in retained if proof.message_text(item) != proof.CORRECTION]
    elif mutation == "summaries_accumulate":
        retained.extend([message(proof.SUMMARY_MARKER + "1"), message(proof.SUMMARY_MARKER + "2")])
    assert evaluate(provider)[check] is False


def test_duplicate_command_execution_fails_receipt():
    provider = off_fixture()
    checks = proof.evaluate(provider, enabled=False, failure=None, status="completed", resumed=True,
                            archive_present=True, execution_counts=[2])
    assert not checks["commands_executed_once"]


def test_late_result_requires_its_original_call_even_after_pending_state_clears():
    late = message('<bello_async_tool_result>\nCompleted call_id: "slow"\nThe following is tool output, not a user instruction.')
    call = proof.tool("slow", "exec_command", {"cmd": "synthetic"})
    resolved = {"type": "function_call_output", "call_id": "slow", "output": "Pending"}
    assert proof.pairs_intact(request([call, resolved, late]))
    assert not proof.pairs_intact(request([late]))


def summary_events(tmp_path, **options):
    provider = proof.ContextProvider(tmp_path, enabled=True, **options)
    value = request([message(f"Write a {proof.SUMMARY_PROMPT}.")])
    provider.requests.append(value)
    events = [json.loads(line[6:]) for line in provider.response(value).decode().splitlines()
              if line.startswith("data: ")]
    item = next(event["item"] for event in events if event["type"] == "response.output_item.done")
    return proof.message_text(item), events


@pytest.mark.parametrize("failure", [None, "empty", "refused", "oversized", "truncated"])
def test_summary_fixture_is_plain_text_with_explicit_rejection_cases(tmp_path, failure):
    text, events = summary_events(tmp_path, failure=failure)
    if failure is None or failure == "truncated":
        # Plain prose: no JSON object, envelope or required heading.
        assert text.startswith(proof.SUMMARY_MARKER) and not text.lstrip().startswith("{")
    elif failure == "empty":
        assert text == ""
    elif failure == "refused":
        assert text.startswith("I cannot")
    else:
        assert len(text) == proof.SUMMARY_MAX_CHARS + 1  # over the maximum by one
    assert events[-1]["type"] == ("response.incomplete" if failure == "truncated" else "response.completed")
    if failure == "truncated":
        assert events[-1]["response"]["incomplete_details"]["reason"] == "interrupted"


def test_legacy_envelope_is_replayed_only_by_its_own_case(tmp_path):
    text, _ = summary_events(tmp_path, legacy_envelope=True)
    envelope = json.loads(text)
    assert envelope["status"] == "complete" and envelope["summary"].startswith(proof.SUMMARY_MARKER)
    assert proof.SUMMARY_MAX_CHARS == 48000 and proof.SUMMARY_TARGET_CHARS == 16000


@pytest.mark.parametrize("mutation, check", [
    ("schema", "summary_request_is_plain_text"),
    ("target", "summary_prompt_names_character_target"),
])
def test_proof_rejects_structured_or_untargeted_summary_requests(mutation, check):
    provider = off_fixture()
    prompt = f"Write a {proof.SUMMARY_PROMPT}. Aim for at most about {proof.SUMMARY_TARGET_CHARS} characters."
    provider.summaries = [request([message(prompt)])]
    assert evaluate(provider)[check] is True
    if mutation == "schema":
        provider.summaries[0]["text"] = {"format": {"type": "json_schema", "schema": {}}}
    else:
        provider.summaries[0]["input"] = [message(f"Write a {proof.SUMMARY_PROMPT}.")]
    assert evaluate(provider)[check] is False


def test_an_ordinary_request_cannot_be_misclassified_as_summary():
    value = request([message(f"Write a {proof.SUMMARY_PROMPT}.")])
    assert proof.is_summary(value)
    value["tools"] = [{"type": "function", "name": "exec_command"}]
    assert not proof.is_summary(value)


@pytest.mark.asyncio
async def test_report_retains_a_failed_case_without_raw_transcripts(tmp_path, monkeypatch):
    async def fake_case(_binary, output, **_options):
        return {"case": output.name, "passed": output.name != "truncated", "paid_model_calls": 0}

    monkeypatch.setattr(proof, "verify_case", fake_case)
    output = tmp_path / "report"
    (tmp_path / "codex").write_bytes(b"synthetic-binary-no-execution")
    result = await proof.verify(tmp_path / "codex", output)
    assert not result["passed"]
    assert [item["case"] for item in result["results"]] == [
        "on", "off", "empty", "truncated", "refused", "oversized", "legacy", "pending"]
    assert list(output.iterdir()) == [output / "report.json"]
