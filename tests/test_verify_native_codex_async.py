"""Assertions used by the real binary fixture must not accept echoed commands."""
import json
import base64
from pathlib import Path
from types import SimpleNamespace
import pytest

from scripts import verify_native_codex_async as proof


def request(text, *, late=False):
    if late:
        return {"input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<bello_async_tool_result>\nCompleted call_id: slow"},
            {"type": "input_text", "text": text},
        ]}]}
    return {"input": [{"type": "function_call_output", "call_id": "slow", "output": text}]}


def test_terminal_output_requires_success_not_an_echoed_command():
    marker = "ASYNC_SLOW_DONE"
    assert proof.delivered(request("Error running printf ASYNC_SLOW_DONE"), marker) == 0
    assert proof.delivered(request("Process exited with code 1\nOutput:\nASYNC_SLOW_DONE"), marker) == 0
    for late in (False, True):
        assert proof.delivered(request("Process exited with code 0\nOutput:\nASYNC_SLOW_DONE", late=late), marker) == 1
        assert proof.delivered(request(json.dumps({"exit_code": 0, "output": marker}), late=late), marker) == 1


def test_json_inside_failed_command_output_cannot_forge_success():
    text = json.dumps({"exit_code": 7, "output": '{"exit_code":0,"output":"ASYNC_SLOW_DONE"}'})
    assert proof.delivered(request(text), "ASYNC_SLOW_DONE") == 0


def test_resolves_a_provider_call_once_with_separate_late_data():
    value = request("Still running")
    value["input"].extend(request("Actual result", late=True)["input"])
    assert proof.unique_tool_resolutions(value)
    value["input"].extend(request("Second invalid tool resolution")["input"])
    assert not proof.unique_tool_resolutions(value)


def test_ordinary_user_text_is_not_counted_as_a_late_tool_result():
    value = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Process exited with code 0\nOutput:\nASYNC_SLOW_DONE"}]}]}
    assert proof.delivered(value, "ASYNC_SLOW_DONE") == 0


def test_windows_off_polling_fits_the_longer_fixture_under_request_cap(monkeypatch):
    monkeypatch.setattr(proof, "os", SimpleNamespace(name="nt"))
    for code in (False, True):
        provider = proof.ScriptedProvider(enabled=False, code=code)
        provider.requests.extend([{}, {}])
        pending = "Script running with cell ID pending" if code else "Process running with session ID 42"
        events = [json.loads(line[6:]) for line in provider.response(request(pending)).decode().splitlines()
                  if line.startswith("data: ")]
        item = next(event["item"] for event in events if event["type"] == "response.output_item.done")
        assert json.loads(item["arguments"])["yield_time_ms"] == 1000
        assert provider.slow_seconds / 1 < 40


@pytest.mark.asyncio
async def test_cold_concurrency_repetitions_preserve_failed_attempts(tmp_path, monkeypatch):
    seen = []

    async def fake_case(binary, output, **kwargs):
        seen.append((output, kwargs))
        return {"case": output.name, "passed": output.name != "direct_on_repeat_2",
                "native_instructions_sha256": "unchanged"}

    async def fake_selection(binary, case, output, **kwargs):
        return {"passed": True}

    monkeypatch.setattr(proof, "verify_case", fake_case)
    monkeypatch.setattr(proof, "verify_selection_case", fake_selection)
    report = await proof.verify(tmp_path / "codex", tmp_path / "proof", concurrency_repeats=3)
    assert len(report["results"]) == 13
    assert report["passed"] is False
    assert len({path for path, _ in seen}) == len(seen)
    extra = [(path.name, options) for path, options in seen if "repeat" in path.name]
    assert extra == [
        ("direct_on_repeat_2", {"enabled": True, "code": False, "signal": None}),
        ("steer_repeat_2", {"enabled": True, "code": False, "signal": "steer"}),
        ("direct_on_repeat_3", {"enabled": True, "code": False, "signal": None}),
        ("steer_repeat_3", {"enabled": True, "code": False, "signal": "steer"}),
    ]
    assert json.loads((tmp_path / "proof/report.json").read_text())["passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("repetitions", [0, 11])
async def test_cold_concurrency_repetitions_are_bounded(tmp_path, repetitions):
    with pytest.raises(ValueError, match="between 1 and 10"):
        await proof.verify(tmp_path / "codex", tmp_path / "proof", concurrency_repeats=repetitions)
    assert not (tmp_path / "proof").exists()


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_interrupt_fixture_has_real_start_and_late_side_effect(platform, monkeypatch, tmp_path):
    monkeypatch.setattr(proof, "os", SimpleNamespace(name=platform))
    provider = proof.ScriptedProvider(enabled=True, code=False, interrupt_probe=True, workspace=tmp_path)
    provider.requests.append({})
    events = [json.loads(line[6:]) for line in provider.response({}).decode().splitlines()
              if line.startswith("data: ")]
    call = next(event["item"] for event in events
                if event["type"] == "response.output_item.done" and event["item"].get("call_id") == "slow")
    command = json.loads(call["arguments"])["cmd"]
    if platform == "nt":
        encoded = command.split("-EncodedCommand ", 1)[1].split(";", 1)[0]
        child = base64.b64decode(encoded).decode("utf-16le")
        assert "$PSHOME" not in command and proof.windows_shell() in command
        assert "$LASTEXITCODE -ne 0" in command and "exit $LASTEXITCODE" in command
        assert command.index("exit $LASTEXITCODE") < command.index("ASYNC_SLOW_DONE")
        assert "$ErrorActionPreference = 'Stop'" in command and "$ErrorActionPreference = 'Stop'" in child
        assert str(tmp_path / "cancel.started") in child and str(tmp_path / "cancel.orphan") in child
        command = child
    sleep = "Start-Sleep" if platform == "nt" else "sleep"
    assert command.index("cancel.started") < command.index(sleep) < command.index("cancel.orphan")


def wakeup_request(*, steer=False, fast=False, slow=False):
    items = []
    if steer:
        items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": proof.STEER_TEXT}]})
    for enabled, call, marker in ((fast, "fast", "ASYNC_FAST_DONE"), (slow, "slow", "ASYNC_SLOW_DONE")):
        if enabled:
            items.append({"type": "function_call_output", "call_id": call,
                          "output": "Process exited with code 0\nOutput:\n" + marker})
    return {"input": items}


@pytest.mark.parametrize("coalesced", [True, False])
def test_steering_accepts_exact_external_advances_not_exact_coalescing(coalesced):
    requests = [wakeup_request(), wakeup_request(steer=True, fast=coalesced)]
    times = [10, 10.2]
    if not coalesced:
        requests.append(wakeup_request(steer=True, fast=True))
        times.append(11.2)
    requests.append(wakeup_request(steer=True, fast=True, slow=True))
    times.append(22.1)
    assert proof.steer_wakeup_proof(requests, times, 12)


@pytest.mark.parametrize("mutation", ["empty_wake", "duplicate_steer", "lost_event", "slow_before_steer",
                                    "no_steer", "too_early", "too_late", "too_many", "wrong_times"])
def test_steering_rejects_semantically_invalid_provider_sequences(mutation):
    requests = [wakeup_request(), wakeup_request(steer=True),
                wakeup_request(steer=True, fast=True), wakeup_request(steer=True, fast=True, slow=True)]
    times = [0, .2, 1.2, 12.1]
    if mutation == "empty_wake":
        requests[2] = wakeup_request(steer=True)
    elif mutation == "duplicate_steer":
        requests[-1]["input"].insert(0, requests[-1]["input"][0])
    elif mutation == "lost_event":
        requests[2] = wakeup_request(fast=True)
    elif mutation == "slow_before_steer":
        requests[1] = wakeup_request(steer=True, slow=True)
        requests[2] = wakeup_request(steer=True, slow=True, fast=True)
    elif mutation == "no_steer":
        requests = [wakeup_request(), wakeup_request(fast=True), wakeup_request(fast=True, slow=True)]
        times = [0, 1.2, 12.1]
    elif mutation == "too_early":
        times[-1] = 1.3
    elif mutation == "too_late":
        times[1] = .9
    elif mutation == "too_many":
        requests.append(requests[-1])
        times.append(12.2)
    elif mutation == "wrong_times":
        times.pop()
    assert not proof.steer_wakeup_proof(requests, times, 12)
