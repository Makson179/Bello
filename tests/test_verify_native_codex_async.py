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
    assert len(report["results"]) == 14
    assert report["passed"] is False
    assert len({path for path, _ in seen}) == len(seen)
    assert (tmp_path / "proof/code_interrupt", {
        "enabled": True, "code": True, "signal": "interrupt",
    }) in seen
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
@pytest.mark.parametrize("code", [False, True])
def test_interrupt_fixture_has_real_start_and_late_side_effect(platform, code, monkeypatch, tmp_path):
    monkeypatch.setattr(proof, "os", SimpleNamespace(name=platform))
    provider = proof.ScriptedProvider(enabled=True, code=code, interrupt_probe=True, workspace=tmp_path)
    provider.requests.append({})
    events = [json.loads(line[6:]) for line in provider.response({}).decode().splitlines()
              if line.startswith("data: ")]
    call = next(event["item"] for event in events
                if event["type"] == "response.output_item.done" and event["item"].get("call_id") == "slow")
    if code:
        assert call["name"] == "exec"
        source = call["input"]
        assert source.startswith('// @exec: {"yield_time_ms": 1}\n')
        args = json.loads(source.split("tools.exec_command(", 1)[1].removesuffix("));"))
        assert args["yield_time_ms"] == 30_000
    else:
        args = json.loads(call["arguments"])
    command = args["cmd"]
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


def turn_completed(thread, turn, status):
    return {"method": "turn/completed", "params": {
        "threadId": thread, "turn": {"id": turn, "status": status},
    }}


@pytest.mark.asyncio
async def test_interrupt_wait_matches_exact_thread_and_turn_and_keeps_sibling_events():
    session = SimpleNamespace(notifications=proof.asyncio.Queue())
    unrelated = [turn_completed("sibling", "target-turn", "interrupted"),
                 turn_completed("target", "old-turn", "interrupted")]
    for event in [*unrelated, turn_completed("target", "target-turn", "interrupted")]:
        session.notifications.put_nowait(event)
    await proof.wait_for_turn(session, "target", "target-turn", status="interrupted", timeout=.1)
    assert [session.notifications.get_nowait(), session.notifications.get_nowait()] == unrelated
    session.notifications.put_nowait(turn_completed("target", "new-turn", "interrupted"))
    with pytest.raises(RuntimeError, match="did not reach completed"):
        await proof.wait_for_turn(session, "target", "new-turn", status="completed", timeout=.1)


@pytest.mark.parametrize("rewrite", [False, True])
def test_interrupt_prefix_checks_separate_threads_but_include_resumed_history(rewrite):
    def message(text):
        return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
    original = message("Start target")
    sibling = message(proof.SIBLING_TEXT)
    resumed = message(proof.RESUME_TEXT)
    values = [{"input": [original]}, {"input": [sibling]}, {"input": [sibling, message("scope result")]},
              {"input": [original, resumed]}, {"input": [original, resumed, message("resume result")]}]
    if rewrite:
        values[-1]["input"][0] = message("Rewritten prior target history")
    assert proof.interrupt_prefixes_stable(values) is (not rewrite)


@pytest.mark.parametrize("platform", ["nt", "posix"])
@pytest.mark.parametrize("code", [False, True])
@pytest.mark.parametrize("text,call_id,marker", [
    (proof.SIBLING_TEXT, "sibling", "ASYNC_SCOPE_DONE"),
    (proof.RESUME_TEXT, "resume", "ASYNC_RESUMED"),
])
def test_interrupt_followups_execute_real_commands_and_require_delivered_results(
    platform, code, text, call_id, marker, monkeypatch, tmp_path,
):
    monkeypatch.setattr(proof, "os", SimpleNamespace(name=platform))
    provider = proof.ScriptedProvider(enabled=True, code=code, interrupt_probe=True, workspace=tmp_path)
    value = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": text}]}]}
    provider.requests.extend([{}, value])
    item, = provider.interrupt_followup(value)
    assert item["call_id"] == call_id
    if code:
        assert item["name"] == "exec"
        args = json.loads(item["input"].split("tools.exec_command(", 1)[1].removesuffix("));"))
    else:
        assert item["name"] == "exec_command"
        args = json.loads(item["arguments"])
    command = args["cmd"]
    assert marker in command
    if call_id == "sibling":
        sleep = "Start-Sleep" if platform == "nt" else "sleep"
        assert command.index("scope.started") < command.index(sleep) < command.index("scope.completed")
    else:
        assert "cancel.resumed" in command
    provider.requests.append(value)
    waiting, = provider.interrupt_followup(value)
    assert waiting["content"][0]["text"] != "Done."
    value["input"].extend(request("Process exited with code 0\nOutput:\n" + marker)["input"])
    completed, = provider.interrupt_followup(value)
    assert completed["content"][0]["text"] == "Done."


@pytest.mark.parametrize("code", [False, True])
@pytest.mark.parametrize("text", [proof.SIBLING_TEXT, proof.RESUME_TEXT])
def test_off_interrupt_followups_poll_current_handle_until_real_terminal_result(code, text, tmp_path):
    provider = proof.ScriptedProvider(enabled=False, code=code, interrupt_probe=True, workspace=tmp_path)
    old = "Script running with cell ID old-cell" if code else "Process running with session ID 17"
    current = "Script running with cell ID new-cell" if code else "Process running with session ID 42"
    value = {"input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": text}]},
        {"type": "function_call_output", "call_id": "old", "output": old},
        {"type": "function_call_output", "call_id": "new", "output": current}]}
    provider.requests.extend([{}, value, value])
    item, = provider.interrupt_followup(value)
    assert item["name"] == ("wait" if code else "write_stdin")
    expected = {"cell_id": "new-cell", "yield_time_ms": 1000} if code else {
        "session_id": 42, "chars": "", "yield_time_ms": 1000,
    }
    assert json.loads(item["arguments"]) == expected
    assert provider.slow_seconds / (expected["yield_time_ms"] / 1000) + 10 < 40
    marker = "ASYNC_SCOPE_DONE" if text == proof.SIBLING_TEXT else "ASYNC_RESUMED"
    value["input"].append({"type": "function_call_output", "call_id": "poll",
                           "output": "Process exited with code 0\nOutput:\n" + marker})
    done, = provider.interrupt_followup(value)
    assert done["content"][0]["text"] == "Done."
    value["input"][-1]["output"] = "Process exited with code 1\nOutput:\n" + marker
    with pytest.raises(ValueError, match="pending follow-up"):
        provider.interrupt_followup(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mutation", [None, "sibling_killed", "sibling_already_completed", "missing_resume_file", "missing_resume_output",
                                    "late_write", "late_target_request", "cancelled_success", "same_turn_id", "wrong_polling"])
async def test_code_interrupt_proof_requires_scoped_cancellation_and_same_thread_recovery(
    mutation, enabled, tmp_path, monkeypatch,
):
    """Drive the real verifier lifecycle without a native process or provider."""
    output = tmp_path / "proof"
    workspace = output / "work"
    provider = proof.ScriptedProvider(enabled=enabled, code=True, interrupt_probe=True, workspace=workspace)
    if not enabled:
        provider.tools.append("wait")
    if mutation == "wrong_polling":
        provider.tools = ["wait"] if enabled else []
    calls = []

    def record(text=None, marker=None):
        value = {"instructions": "synthetic fixture " * 100, "input": []}
        if text:
            value["input"].append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": text}]})
        if marker:
            value["input"].extend(request("Process exited with code 0\nOutput:\n" + marker)["input"])
        provider.requests.append(value)
        provider.times.append(float(len(provider.requests)))

    class Server:
        server_port = 12345
        def __init__(self, *args): pass
        def serve_forever(self): pass
        def shutdown(self): pass
        def server_close(self): pass

    class Session:
        def __init__(self, *args, **kwargs):
            self.notifications = proof.asyncio.Queue()
            self.thread_count = 0

        async def start(self): pass
        async def close(self, output): pass

        async def request(self, method, params):
            calls.append((method, params))
            if method == "experimentalFeature/list":
                return {"data": [{"name": name} for name in ("bello_async_tools", "bello_native_selection")]}
            if method == "thread/start":
                self.thread_count += 1
                return {"thread": {"id": "target" if self.thread_count == 1 else "sibling"}}
            if method == "turn/interrupt":
                assert params == {"threadId": "target", "turnId": "initial"}
                self.notifications.put_nowait(turn_completed("sibling", "sibling-turn", "completed"))
                self.notifications.put_nowait(turn_completed("target", "initial", "interrupted"))
                if mutation != "sibling_killed":
                    (workspace / "scope.completed").write_text("survived")
                record(proof.SIBLING_TEXT, "ASYNC_SCOPE_DONE")
                return {}
            assert method == "turn/start"
            text = params["input"][0]["text"]
            if text == proof.SIBLING_TEXT:
                assert params["threadId"] == "sibling"
                record(text)
                (workspace / "scope.started").write_text("started")
                if mutation == "sibling_already_completed":
                    (workspace / "scope.completed").write_text("survived")
                return {"turn": {"id": "sibling-turn"}}
            if text == proof.RESUME_TEXT:
                assert params["threadId"] == "target"
                if mutation == "late_target_request":
                    record()
                record(text)
                if mutation != "missing_resume_file":
                    (workspace / "cancel.resumed").write_text("resumed")
                marker = None if mutation == "missing_resume_output" else "ASYNC_RESUMED"
                record(text, marker)
                if mutation == "cancelled_success":
                    provider.requests[-1]["input"].extend([
                        {"type": "function_call_output", "call_id": "cancelled",
                         "output": "Process exited with code 0\nOutput:\nASYNC_SLOW_DONE"}])
                if mutation == "late_write":
                    (workspace / "cancel.orphan").write_text("orphan")
                turn = "initial" if mutation == "same_turn_id" else "resumed"
                self.notifications.put_nowait(turn_completed("target", turn, "completed"))
                return {"turn": {"id": turn}}
            record()
            (workspace / "cancel.started").write_text("started")
            return {"turn": {"id": "initial"}}

    async def no_sleep(_): pass

    monkeypatch.setattr(proof, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(proof, "ScriptedProvider", lambda **kwargs: provider)
    monkeypatch.setattr(proof, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(proof, "ChildWaitSession", Session)
    monkeypatch.setattr(proof, "isolated_environment", lambda *args: {})
    monkeypatch.setattr(proof.asyncio, "sleep", no_sleep)
    result = await proof.verify_case(tmp_path / "unused", output, enabled=enabled, code=True, signal="interrupt")
    assert result["passed"] is (mutation is None)
    assert result["interrupted"] and result["interrupted_command_started"]
    assert [params["threadId"] for method, params in calls if method == "turn/start"] == [
        "target", "sibling", "target",
    ]
    thread_configs = [params["config"] for method, params in calls if method == "thread/start"]
    assert len(thread_configs) == 2
    assert all(config["features.code_mode_interrupt"] is True for config in thread_configs)
    if mutation is None:
        assert result["same_thread_resume_completed"] and result["resume_output_delivered"]
        assert result["sibling_turn_survived_interrupt"]
