"""Assertions used by the real binary fixture must not accept echoed commands."""
import json
from types import SimpleNamespace

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
