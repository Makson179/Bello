from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
import sys

import pytest


spec = importlib.util.spec_from_file_location("native_selection_proof",
    Path(__file__).resolve().parents[1] / "scripts" / "verify_native_codex_selection.py")
proof = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = proof
spec.loader.exec_module(proof)


@pytest.mark.parametrize("mode", ["direct", "code", "poll"])
def test_extracts_only_exact_call_output(mode):
    value = "Chunk ID: a\nProcess exited with code 7\nOutput:\n" + proof.SELECTED if mode == "direct" else (
        "Script completed\nOutput:\n" + json.dumps({"output": proof.SELECTED, "exit_code": 7}))
    kind = "function_call_output" if mode == "direct" else "custom_tool_call_output"
    request = {"input": [
        {"type": kind, "call_id": "wrong", "output": value.replace("KEEP_SENTINEL", "WRONG")},
        {"type": kind, "call_id": proof.CALL_ID, "output": [{"type": "input_text", "text": value}]},
    ]}
    assert proof.output_packets(request, mode) == [{"output": proof.SELECTED, "exit_code": 7}]


def test_failed_or_missing_output_never_claims_exact_packet():
    for mode in ("direct", "code", "poll"):
        assert proof.output_packets({}, mode) == []
        kind = "function_call_output" if mode == "direct" else "custom_tool_call_output"
        assert proof.output_packets({"input": [{"type": kind, "call_id": proof.CALL_ID,
                                               "output": "Script failed"}]}, mode) == []


def test_code_packet_does_not_parse_json_inside_escaped_log():
    text = json.dumps({"output": '{"output":"FAKE","exit_code":0}', "exit_code": 7})
    request = {"input": [{"type": "custom_tool_call_output", "call_id": proof.CALL_ID, "output": text}]}
    assert proof.output_packets(request, "code") == [{"output": '{"output":"FAKE","exit_code":0}', "exit_code": 7}]


def test_environment_does_not_inherit_credentials_or_parent_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-an-api-key")
    monkeypatch.setenv("CODEX_HOME", "/wrong")
    monkeypatch.setenv("CODEX_PERMISSION_PROFILE", "danger-full-access")
    env = proof.isolated_environment(tmp_path, tmp_path / "bin" / "codex", 9123,
                                    {"BELLO_SELECTOR_SOCKET": "/tmp/synthetic.sock"})
    assert env["HOME"] == env["CODEX_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in env and "CODEX_PERMISSION_PROFILE" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9123"
    assert env["NO_PROXY"] == "127.0.0.1,localhost"


@pytest.mark.parametrize("encoding", ["", "identity", "gzip"])
def test_decodes_provider_request(encoding):
    body = b'{"input": []}'
    if encoding == "gzip":
        body = gzip.compress(body)
    assert proof.decode_body(body, encoding) == {"input": []}


def test_rejects_bad_provider_request():
    with pytest.raises(ValueError):
        proof.decode_body(b'[]', "")
    with pytest.raises(ValueError):
        proof.decode_body(b'{}', "unsupported")


def test_wire_invocations_use_optional_focus_and_real_poll_command():
    direct, cmd = proof.invocation(proof.Case("direct"), windows=False)
    assert direct["name"] == "exec_command"
    assert json.loads(direct["arguments"])["focus"] == proof.FOCUS
    assert json.loads(direct["arguments"])["cmd"] == cmd
    missing, _ = proof.invocation(proof.Case("missing", focus=False))
    assert "focus" not in json.loads(missing["arguments"])
    poll, command = proof.invocation(proof.Case("poll", mode="poll"), windows=False)
    assert poll["type"] == "custom_tool_call" and poll["name"] == "exec"
    assert command.startswith("sleep 2;")
    assert "tools.write_stdin" in poll["input"] and proof.POLL_FOCUS in poll["input"]
    assert 'Missing live session' in poll["input"]


def test_windows_environment_only_inherits_required_os_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-copy")
    monkeypatch.setenv("PATH", "untrusted-parent-directory")
    bridge_env = {"BELLO_SELECTOR_TCP": "127.0.0.1:9999", "BELLO_SELECTOR_TOKEN": "a" * 64}
    env = proof.isolated_environment(tmp_path, tmp_path / "bin/codex.exe", 9123, bridge_env, windows=True)
    assert env["USERPROFILE"] == env["CODEX_HOME"] == str(tmp_path)
    assert "SystemRoot" in env and "COMSPEC" in env and ";" in env["PATH"]
    assert "SHELL" not in env and "ANTHROPIC_API_KEY" not in env
    assert "untrusted-parent-directory" not in env["PATH"]
    assert all(env[k] == v for k, v in bridge_env.items())


@pytest.mark.parametrize("case", proof.CASES)
def test_windows_invocations_preserve_exit_code_focus_and_polling(case):
    tool, command = proof.invocation(case, windows=True)
    assert command.endswith("exit 7")
    assert "bash" not in command and "cat " not in command
    if case.mode == "direct":
        args = json.loads(tool["arguments"])
        assert args["shell"].endswith("powershell.exe")
        assert ("focus" in args) == case.focus
    if case.mode == "poll":
        assert command.startswith("Start-Sleep -Seconds 2;")
        assert "tools.write_stdin" in tool["input"]
    if case.protected == "help":
        assert "--help" in command


def test_bridge_environment_cannot_smuggle_credentials(tmp_path):
    with pytest.raises(ValueError, match="Unexpected native bridge"):
        proof.isolated_environment(tmp_path, tmp_path / "codex", 9123, {"OPENAI_API_KEY": "never-copy"})


@pytest.mark.parametrize("protected", ["task", "help"])
def test_windows_protected_commands_are_recognized_at_real_boundary(tmp_path, protected):
    from supervisor.runtime.distiller_policy import preserve_tool_output
    _, command = proof.invocation(proof.Case("protected", protected=protected), windows=True)
    assert preserve_tool_output("exec_command", {"command": command}, workspace=tmp_path,
                                task_path=tmp_path / "fixture-requirements.data")


def test_sse_second_response_cannot_request_another_tool():
    tool, _ = proof.invocation(proof.Case("direct"))
    first = proof.response_events(1, tool).decode()
    second = proof.response_events(2, tool).decode()
    assert '"function_call"' in first and 'response.completed' in first
    assert '"function_call"' not in second and 'Synthetic fixture complete.' in second


def test_nine_cases_include_protection_and_all_paired_paths():
    names = {case.name for case in proof.CASES}
    assert names == {"direct_off", "direct_on", "code_off", "code_on", "poll_off", "poll_on",
                     "missing_focus", "task_protected", "help_protected"}
