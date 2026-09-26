"""Assertions used by the real binary fixture must not accept echoed commands."""
import json
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
def test_interrupt_fixture_has_real_start_and_late_side_effect(platform, monkeypatch):
    monkeypatch.setattr(proof, "os", SimpleNamespace(name=platform))
    provider = proof.ScriptedProvider(enabled=True, code=False, interrupt_probe=True)
    provider.requests.append({})
    events = [json.loads(line[6:]) for line in provider.response({}).decode().splitlines()
              if line.startswith("data: ")]
    call = next(event["item"] for event in events
                if event["type"] == "response.output_item.done" and event["item"].get("call_id") == "slow")
    command = json.loads(call["arguments"])["cmd"]
    sleep = "Start-Sleep" if platform == "nt" else "sleep"
    assert command.index("cancel.started") < command.index(sleep) < command.index("cancel.orphan")
