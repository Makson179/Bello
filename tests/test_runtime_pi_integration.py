from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

from packaging.version import InvalidVersion, Version
import pytest

from supervisor.appserver import last_agent_message_text
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.transport import WorkerTransport
from supervisor.schemas import CompletionReviewDecision, CompletionReviewDecisionKind
from supervisor.schemas.models import openai_strict_json_schema_for_completion_review_decision


_MIN_NODE = Version("22.19.0")
_DUMMY_KEY = "bello-local-integration-dummy-key"


def _bounded_fixture_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= 2400:
        return text
    marker = "\n...[diagnostic middle omitted]...\n"
    return text[:700] + marker + text[-(2400 - 700 - len(marker)):]


def _fixture_exchange_diagnostic(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Show bounded synthetic tool results/retry hints, never headers or system prompts."""
    messages = [
        message for message in body.get("messages", [])
        if isinstance(message, dict) and message.get("role") in {"tool", "user"}
    ]
    return [
        {
            "role": message["role"],
            "tool_call_id": message.get("tool_call_id"),
            "content": _bounded_fixture_json(message.get("content")),
        }
        for message in messages[-6:]
    ]


def _supported_node() -> Path:
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
    candidates = [os.environ.get("BELLO_NODE"), str(bundled), shutil.which("node")]
    for raw in candidates:
        if not raw:
            continue
        candidate = Path(raw).resolve()
        if not candidate.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            version = Version(probe.stdout.strip().removeprefix("v"))
        except (OSError, subprocess.SubprocessError, InvalidVersion):
            continue
        if version >= _MIN_NODE:
            return candidate
    message = "the real Pi integration test requires Node.js >= 22.19"
    if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _completion_decision(flow: str) -> dict[str, Any]:
    return CompletionReviewDecision(
        decision="accept",
        reason=f"local Pi integration {flow} completed",
        message_to_coder=None,
        persistent_decision=None,
        progress_update=None,
        clear_handoff=True,
        display_message=None,
        handoff=None,
        wake_sequence=1 if flow == "A" else 2,
        generation=0,
    ).model_dump(mode="json")


def _tool_chunk(flow: str, step: int, name: str, arguments: dict[str, Any], *, call_id: str | None = None) -> bytes:
    return _response_chunks(flow, step, {
        "role": "assistant",
        "tool_calls": [{
            "index": 0,
            "id": call_id or f"call-{flow.lower()}-{name}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            },
        }],
    }, "tool_calls")


def _response_chunks(flow: str, step: int, delta: dict[str, Any], finish_reason: str) -> bytes:
    payload = {
        "id": f"chatcmpl-{flow}-{step}",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "bello-integration-model",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    usage = {
        "id": f"chatcmpl-{flow}-{step}",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "bello-integration-model",
        "choices": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return (
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        f"data: {json.dumps(usage, separators=(',', ':'))}\n\n"
        "data: [DONE]\n\n"
    ).encode()


class _LocalProviderState:
    # Windows' deadline includes native sandbox setup and cleanup for both
    # concurrent flows. This is a fixture budget, not a runtime default.
    COMMAND_TIMEOUT_SECONDS = 50 if os.name == "nt" else 10
    # Keep bounded five-second polling alive beyond that execution deadline.
    MAX_COMMAND_POLLS = max(6, COMMAND_TIMEOUT_SECONDS // 5 + 2)

    def __init__(self, output_schema: dict[str, Any], final_mode: str, distiller_enabled: bool = False):
        self.output_schema = output_schema
        self.final_mode = final_mode
        self.distiller_enabled = distiller_enabled
        self.condition = threading.Condition()
        self.first_requests: set[str] = set()
        self.steps: dict[str, int] = {}
        self.requests: list[tuple[str, int, dict[str, Any]]] = []
        self.errors: list[str] = []
        self.poll_calls: dict[str, list[str]] = {flow: [] for flow in ("A", "B")}
        self.command_sessions: dict[str, str] = {}
        self.command_output: dict[str, str] = {flow: "" for flow in ("A", "B")}
        self.command_raw_bytes: dict[str, int] = {flow: 0 for flow in ("A", "B")}

    def response(self, body: dict[str, Any], authorization: str | None) -> bytes:
        serialized = json.dumps(body, ensure_ascii=False)
        flow = next((candidate for candidate in ("A", "B") if f"FLOW-{candidate}" in serialized), None)
        if flow is None:
            raise AssertionError("request did not retain its originating FLOW marker")
        if authorization != f"Bearer {_DUMMY_KEY}":
            raise AssertionError("the local provider did not receive exactly the isolated dummy key")

        with self.condition:
            step = self.steps.get(flow, 0)
            if step == 0:
                self.first_requests.add(flow)
                self.condition.notify_all()
                if not self.condition.wait_for(lambda: len(self.first_requests) == 2, timeout=10):
                    raise AssertionError("Pi sessions did not reach the fake provider concurrently")
            self.steps[flow] = step + 1
            self.requests.append((flow, step, body))

        tools = {
            entry.get("function", {}).get("name"): entry.get("function", {})
            for entry in body.get("tools", [])
            if isinstance(entry, dict)
        }
        required_tools = {"write_file", "read_file", "exec_command", "poll_command", "submit_result"}
        if not required_tools.issubset(tools):
            raise AssertionError(f"missing controlled tools: {sorted(required_tools - tools.keys())}")
        if tools["submit_result"].get("parameters") != self.output_schema:
            raise AssertionError("Pi did not send the exact Bello output schema to the local provider")
        for name in ("write_file", "read_file", "exec_command", "poll_command"):
            assert ("focus" in tools[name]["parameters"]["properties"]) is self.distiller_enabled

        filename = f"flow-{flow.lower()}.txt"
        focus = {"focus": "Keep the useful tool result."} if self.distiller_enabled else {}
        if step == 0:
            return _tool_chunk(flow, step, "write_file", {
                "path": filename,
                "content": f"payload-{flow}\n",
                **focus,
            })
        if step == 1:
            if f"call-{flow.lower()}-write_file" not in serialized or "bytes_written" not in serialized:
                raise AssertionError("the write result did not return through the Pi conversation")
            return _tool_chunk(flow, step, "read_file", {"path": filename, **focus})
        if step == 2:
            if f"call-{flow.lower()}-read_file" not in serialized or f"payload-{flow}" not in serialized:
                raise AssertionError("the read result did not return through the Pi conversation")
            command = (
                f'findstr /x /c:"payload-{flow}" {filename} && echo synthetic-^noise-{flow} && echo exec-{flow}-ok'
                if os.name == "nt"
                else f'test "$(cat {filename})" = "payload-{flow}" && printf "synthetic-%s-%s\\n" noise {flow} && printf "exec-{flow}-ok"'
            )
            return _tool_chunk(flow, step, "exec_command", {
                "command": command, "timeout": self.COMMAND_TIMEOUT_SECONDS, **focus,
            })
        polls = self.poll_calls[flow]
        if step == 3 + len(polls):
            result_call = polls[-1] if polls else f"call-{flow.lower()}-exec_command"
            tool_message = next(message for message in body["messages"]
                                if message.get("role") == "tool"
                                and message.get("tool_call_id") == result_call)
            packet = json.loads(tool_message["content"])
            assert packet["outputTruncated"] is False and isinstance(packet["sessionId"], str)
            assert packet["outputBudget"]["truncated"] is False
            assert packet["outputBudget"]["returnedBytes"] == packet["outputBudget"]["totalBytes"]
            session_id = self.command_sessions.setdefault(flow, packet["sessionId"])
            assert packet["sessionId"] == session_id, "poll returned a different command session"
            self.command_output[flow] += packet["output"]
            self.command_raw_bytes[flow] += packet["outputBudget"]["totalBytes"]
            if packet["status"] == "running":
                assert len(polls) < self.MAX_COMMAND_POLLS, "command never completed within the fixture poll budget"
                call_id = f"call-{flow.lower()}-poll_command-{len(polls) + 1}"
                polls.append(call_id)
                return _tool_chunk(flow, step, "poll_command", {
                    "session_id": session_id, "yield_time_ms": 5000, **focus,
                }, call_id=call_id)
            assert packet["status"] == "completed" and packet["exitCode"] == 0
            assert packet["timedOut"] is False and packet["cancelled"] is False
            output = self.command_output[flow]
            assert f"exec-{flow}-ok" in output, "the command result did not return through the Pi conversation"
            assert (f"synthetic-noise-{flow}" not in output) is self.distiller_enabled
            if self.distiller_enabled:
                assert output.strip() == f"exec-{flow}-ok"
                assert f"synthetic-noise-{flow}" not in serialized
                assert self.command_raw_bytes[flow] > len(output.encode())
            if self.final_mode == "text":
                return _response_chunks(flow, step, {
                    "role": "assistant",
                    "content": json.dumps(_completion_decision(flow), ensure_ascii=False),
                }, "stop")
            return _tool_chunk(flow, step, "submit_result", _completion_decision(flow))
        raise AssertionError(f"unexpected extra model request for flow {flow}: step {step}")


def _polling_fixture_body(state: _LocalProviderState, call_id: str, packet: dict[str, Any]) -> dict[str, Any]:
    from supervisor.runtime.tools import tool_definitions

    definitions = tool_definitions(distiller=state.distiller_enabled)
    definitions.append({"name": "submit_result", "parameters": state.output_schema})
    return {
        "tools": [{"type": "function", "function": entry} for entry in definitions],
        "messages": [
            {"role": "user", "content": "Run controlled integration FLOW-A."},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps(packet)},
        ],
    }


def _fixture_response_delta(response: bytes) -> dict[str, Any]:
    return json.loads(response.split(b"\n\n", 1)[0].removeprefix(b"data: "))["choices"][0]["delta"]


def test_local_provider_command_budget_covers_windows_sandbox_lifecycle() -> None:
    state = _LocalProviderState({}, "text")
    state.steps["A"] = 2
    body = _polling_fixture_body(state, "call-a-read_file", {"text": "payload-A"})
    delta = _fixture_response_delta(state.response(body, f"Bearer {_DUMMY_KEY}"))
    call = delta["tool_calls"][0]
    assert call["function"]["name"] == "exec_command"
    arguments = json.loads(call["function"]["arguments"])
    expected_timeout = 50 if os.name == "nt" else 10
    assert arguments["timeout"] == state.COMMAND_TIMEOUT_SECONDS == expected_timeout
    assert state.MAX_COMMAND_POLLS == (12 if os.name == "nt" else 6)
    assert state.MAX_COMMAND_POLLS * 5 >= expected_timeout + 10


@pytest.mark.parametrize("final_mode", ["submit_result", "text"])
@pytest.mark.parametrize("distiller_enabled", [False, True], ids=["original", "distilled"])
async def test_local_provider_polls_forced_running_and_keeps_consumed_output(
    tmp_path: Path, final_mode: str, distiller_enabled: bool,
) -> None:
    """No subprocess/provider: real ToolHost sessions with a gated fake runner.

    The command emits all its output before completing, as in the Windows CI
    failure. The terminal poll therefore has empty output, not missing facts.
    """
    from supervisor.runtime.journal import RuntimeJournal
    from supervisor.runtime.sandbox import SandboxResult
    from supervisor.runtime.tools import ToolHost, ToolScope

    release = asyncio.Event()
    raw = "payload-A\nsynthetic-noise-A\nexec-A-ok\n"
    executions = []

    class Runner:
        async def run(self, command, _cwd, _timeout, on_output, *, cancel_event):
            executions.append(command)
            await on_output(raw)
            await release.wait()
            return SandboxResult(raw, 0, .01)

    async def unexpected(*_args):
        raise AssertionError("no approval or delegate call is expected")

    async def emit(_message):
        pass

    async def distill(text, focus, command):
        assert focus == "Keep the useful tool result."
        assert command == "fixture-command"
        return "".join(line for line in text.splitlines(keepends=True) if line.startswith("exec-"))

    journal = RuntimeJournal(tmp_path / "state")
    host = ToolHost(journal, lambda *_: ToolScope(tmp_path, "workspace-write", distiller_enabled=distiller_enabled),
                    unexpected, emit, unexpected, runner_factory=lambda _: Runner(), distill=distill)
    state = _LocalProviderState(openai_strict_json_schema_for_completion_review_decision(), final_mode, distiller_enabled)
    state.steps["A"] = 3  # Write/read are covered by the real SDK test above.
    focus = {"focus": "Keep the useful tool result."} if distiller_enabled else {}
    try:
        result = await host.call({"threadId": "thread", "turnId": "turn", "callId": "call-a-exec_command",
                                  "name": "exec_command", "arguments": {"command": "fixture-command", "yield_time_ms": 0, **focus}})
        initial = json.loads(result["content"][0]["text"])
        assert initial["status"] == "running"
        assert initial["output"] == ("exec-A-ok\n" if distiller_enabled else raw)
        body = _polling_fixture_body(state, "call-a-exec_command", initial)
        delta = _fixture_response_delta(state.response(body, f"Bearer {_DUMMY_KEY}"))
        poll = delta["tool_calls"][0]
        assert poll["function"]["name"] == "poll_command"
        arguments = json.loads(poll["function"]["arguments"])
        assert arguments == {"session_id": initial["sessionId"], "yield_time_ms": 5000, **focus}
        assert poll["id"] != "call-a-exec_command"
        release.set()
        result = await host.call({"threadId": "thread", "turnId": "turn", "callId": poll["id"],
                                  "name": "poll_command", "arguments": arguments})
        final = json.loads(result["content"][0]["text"])
        assert final["status"] == "completed" and final["output"] == ""
        body["messages"].append({"role": "tool", "tool_call_id": poll["id"], "content": json.dumps(final)})
        delta = _fixture_response_delta(state.response(body, f"Bearer {_DUMMY_KEY}"))
        if final_mode == "submit_result":
            assert delta["tool_calls"][0]["function"]["name"] == "submit_result"
            decision = json.loads(delta["tool_calls"][0]["function"]["arguments"])
        else:
            decision = json.loads(delta["content"])
        assert decision == _completion_decision("A")
        assert state.command_output["A"] == initial["output"]
        assert state.steps["A"] == 5 and state.poll_calls["A"] == ["call-a-poll_command-1"]
        assert executions == ["fixture-command"]  # Polling never replays execution.
    finally:
        release.set()
        await host.close()
        journal.close()


@pytest.mark.parametrize("failure", ["failed", "timed_out", "cancelled", "nonzero_exit", "timedOut", "cancelled_flag", "truncated", "wrong_session"])
def test_local_provider_poll_does_not_accept_failed_or_invalid_completion(failure: str) -> None:
    state = _LocalProviderState({}, "text")
    state.steps["A"] = 4
    state.poll_calls["A"] = ["call-a-poll_command-1"]
    state.command_sessions["A"] = "session-a"
    state.command_output["A"] = "synthetic-noise-A\nexec-A-ok"
    packet = {"sessionId": "session-a", "status": "completed", "exitCode": 0, "output": "",
              "outputTruncated": False, "timedOut": False, "cancelled": False,
              "outputBudget": {"truncated": False, "returnedBytes": 0, "totalBytes": 0}}
    if failure in {"failed", "timed_out", "cancelled"}:
        packet["status"] = failure
    elif failure == "nonzero_exit":
        packet["exitCode"] = 1
    elif failure == "wrong_session":
        packet["sessionId"] = "different-session"
    elif failure == "truncated":
        packet["outputBudget"]["truncated"] = True
    else:
        packet["cancelled" if failure == "cancelled_flag" else failure] = True
    with pytest.raises(AssertionError):
        state.response(_polling_fixture_body(state, "call-a-poll_command-1", packet), f"Bearer {_DUMMY_KEY}")


def test_local_provider_poll_budget_is_bounded_and_call_ids_are_unique() -> None:
    state = _LocalProviderState({}, "text")
    state.steps["A"] = 3
    packet = {"sessionId": "session-a", "status": "running", "output": "", "outputTruncated": False,
              "outputBudget": {"truncated": False, "returnedBytes": 0, "totalBytes": 0}}
    call_id = "call-a-exec_command"
    for _ in range(state.MAX_COMMAND_POLLS):
        delta = _fixture_response_delta(state.response(_polling_fixture_body(state, call_id, packet), f"Bearer {_DUMMY_KEY}"))
        call_id = delta["tool_calls"][0]["id"]
    assert len(set(state.poll_calls["A"])) == state.MAX_COMMAND_POLLS
    with pytest.raises(AssertionError, match="never completed"):
        state.response(_polling_fixture_body(state, call_id, packet), f"Bearer {_DUMMY_KEY}")


@contextmanager
def _local_openai_provider(
    output_schema: dict[str, Any], final_mode: str, distiller_enabled: bool = False,
) -> Iterator[tuple[str, _LocalProviderState]]:
    state = _LocalProviderState(output_schema, final_mode, distiller_enabled)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            try:
                if self.path != "/v1/chat/completions":
                    raise AssertionError(f"unexpected provider path: {self.path}")
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                response = state.response(body, self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(response)
                self.wfile.flush()
            except Exception as exc:  # pragma: no cover - asserted in the parent test
                state.errors.append(str(exc))
                response = json.dumps({"error": {"message": str(exc)}}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError as exc:
        message = "the outer test sandbox denied the local Pi integration server"
        if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(f"{message}: {exc}")
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="bello-fake-provider", daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _worker_environment(node: Path, home: Path, scratch: Path) -> dict[str, str]:
    home.mkdir()
    scratch.mkdir()
    environment = {
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "PATH": os.pathsep.join((str(node.parent), "/usr/bin", "/bin")),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    if os.name == "nt":
        for name in ("SystemRoot", "WINDIR", "ComSpec", "PATHEXT"):
            if value := os.environ.get(name):
                environment[name] = value
        environment.update({
            "USERPROFILE": str(home),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "PATH": os.pathsep.join((str(node.parent), str(Path(os.environ["SystemRoot"]) / "System32"))),
        })
    return environment


@pytest.mark.asyncio
@pytest.mark.parametrize("final_mode", ["submit_result", "text"])
@pytest.mark.parametrize("distiller_enabled", [False, True], ids=["original", "distilled"])
async def test_real_pi_sdk_runtime_client_toolhost_and_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_mode: str, distiller_enabled: bool,
) -> None:
    file_tool_commands: list[tuple[str, ...]] = []
    if os.name == "nt":
        if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") != "1":
            pytest.skip("Windows Pi integration requires the native CI host-setup fixture")
        from supervisor.runtime import sandbox
        from tests.test_runtime_pipeline_integration import (
            _stage_windows_pytest_runtime,
            _use_staged_windows_file_tools,
        )

        # Shell commands use system CMD/findstr, but read/write tools also need
        # their real Python interpreter. Authorize only the copied runtime.
        python = _stage_windows_pytest_runtime(tmp_path / "staged-python")
        file_tool_commands = _use_staged_windows_file_tools(monkeypatch, python)
        monkeypatch.setattr(
            sandbox, "_discover_toolchain",
            lambda _policy: sandbox._Toolchain(readable_roots=(python.parent,)),
        )
    node = _supported_node()
    worker_dir = Path(__file__).resolve().parents[1] / "supervisor" / "pi_worker"
    worker = worker_dir / "worker.mjs"
    if not (worker_dir / "node_modules" / "@earendil-works" / "pi-coding-agent").is_dir():
        message = "the pinned Pi worker dependencies are not installed"
        if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)

    output_schema = openai_strict_json_schema_for_completion_review_decision()
    agent_dir = tmp_path / "isolated-pi-agent"
    state_dir = tmp_path / "private-runtime"
    agent_dir.mkdir()
    workspaces = {flow: tmp_path / f"workspace-{flow.lower()}" for flow in ("A", "B")}
    for workspace in workspaces.values():
        workspace.mkdir()

    events: list[dict[str, Any]] = []
    transport_errors: list[BaseException] = []

    async def notification(message) -> None:
        events.append(message.raw)

    async def transport_error(error: BaseException) -> None:
        transport_errors.append(error)

    client = RuntimeClient(
        cwd=workspaces["A"],
        state_dir=state_dir,
        notification_handler=notification,
        transport_error_handler=transport_error,
    )
    selected_inputs: list[str] = []
    if distiller_enabled:
        from supervisor.runtime import distiller

        class FakeSelector:
            async def distill(self, text, focus, command):
                assert focus == "Keep the useful tool result."
                assert isinstance(command, str) and command
                if "synthetic-noise-" in text:
                    selected_inputs.append(text)
                    return "".join(line for line in text.splitlines(keepends=True) if line.startswith("exec-"))
                return text

            async def close(self):
                pass

        monkeypatch.setattr(distiller, "LogDistiller", lambda _path: FakeSelector())
        monkeypatch.setattr(distiller, "validate_bundle", lambda _path: {})
        monkeypatch.setattr(distiller, "require_dependencies", lambda: None)
        client.configure_run(log_distiller={"enabled": True, "model_path": str(tmp_path / "fake-bundle")})
    await client.start()
    transport: WorkerTransport | None = None
    try:
        with _local_openai_provider(output_schema, final_mode, distiller_enabled) as (base_url, provider):
            (agent_dir / "models.json").write_text(json.dumps({
                "providers": {
                    "bello-local": {
                        "name": "Bello local integration provider",
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": _DUMMY_KEY,
                        "authHeader": True,
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                            "supportsUsageInStreaming": True,
                        },
                        "models": [{
                            "id": "integration-model",
                            "name": "Local integration model",
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": 32768,
                            "maxTokens": 4096,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }],
                    },
                },
            }), encoding="utf-8")

            async def emit(message: dict[str, Any]) -> None:
                await client._emit(message, engine="pi")

            worker_environment = _worker_environment(node, tmp_path / "worker-home", tmp_path / "worker-tmp")

            async def start_worker() -> WorkerTransport:
                backend = WorkerTransport(
                    [str(node), str(worker)],
                    worker_dir,
                    emit=emit,
                    tool=client._call_tool,
                    on_error=client._notify_transport_error,
                    env=worker_environment,
                )
                # RuntimeClient must own cleanup even if initialization fails.
                client._engines["pi"] = backend
                await backend.start()
                initialized = await backend.request("initialize", {
                    "stateDir": str(state_dir / "pi"),
                    "agentDir": str(agent_dir),
                    "allowModelNetwork": False,
                })
                assert initialized["serverInfo"]["piSdkVersion"] == "0.85.1"
                return backend

            transport = await start_worker()

            listed = await client.request("model/list", {"engines": ["pi"]})
            local = next(item for item in listed["data"] if item.get("qualifiedId") == "bello-local/integration-model")
            assert local["configured"] is True
            assert local["supportedEfforts"] == ["off"]

            thread_ids: dict[str, str] = {}

            async def run_flow(flow: str) -> tuple[dict[str, Any], CompletionReviewDecision]:
                workspace = workspaces[flow]
                started = await client.thread_start({
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                    "model": "bello-local/integration-model",
                    "effort": "off",
                    "sandbox": "workspace-write",
                    "approvalPolicy": "never",
                    "belloRole": "coder",
                })
                thread_id = started["thread"]["id"]
                thread_ids[flow] = thread_id
                response = await client.turn_start({
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": f"Run controlled integration FLOW-{flow}."}],
                    "effort": "off",
                    "outputSchema": output_schema,
                })
                turn_id = response["turn"]["id"]
                completed = await client.wait_for_notification(
                    lambda message: message.method == "turn/completed"
                    and message.params.get("threadId") == thread_id
                    and message.params.get("turn", {}).get("id") == turn_id,
                    timeout=90 if os.name == "nt" else 30,
                )
                turn = completed.params["turn"]
                assert turn["status"] == "completed", json.dumps({
                    "error": turn.get("error"),
                    "provider_errors": provider.errors,
                    "recent_exchange": _fixture_exchange_diagnostic(next(
                        (body for request_flow, _step, body in reversed(provider.requests) if request_flow == flow),
                        {},
                    )),
                }, ensure_ascii=False, indent=2)
                text = last_agent_message_text(turn)
                assert text is not None
                return turn, CompletionReviewDecision.model_validate_json(text)

            results = await asyncio.gather(run_flow("A"), run_flow("B"))

            assert not provider.errors
            assert provider.first_requests == {"A", "B"}
            expected_responses = {flow: 4 + len(provider.poll_calls[flow]) for flow in ("A", "B")}
            assert provider.steps == expected_responses
            assert len(provider.requests) == sum(expected_responses.values())
            assert len(selected_inputs) == (2 if distiller_enabled else 0)
            assert not transport_errors
            usage_events = [
                raw["params"]["item"]
                for raw in events
                if raw.get("method") == "item/completed"
                and isinstance(raw.get("params", {}).get("item"), dict)
                and raw["params"]["item"].get("type") == "agentMessage"
                and isinstance(raw["params"]["item"].get("usage"), dict)
            ]
            assert len(usage_events) == sum(expected_responses.values())
            assert all(item["usage"]["totalTokens"] == 15 for item in usage_events)
            for flow, (turn, decision) in zip(("A", "B"), results, strict=True):
                assert decision.decision is CompletionReviewDecisionKind.ACCEPT
                assert decision.reason == f"local Pi integration {flow} completed"
                if final_mode == "submit_result":
                    assert turn["structuredResult"] == _completion_decision(flow)
                else:
                    assert "structuredResult" not in turn
                    assert last_agent_message_text(turn) == json.dumps(_completion_decision(flow), ensure_ascii=False)
                assert turn["usage"]["input"] == 10 * expected_responses[flow]
                assert turn["usage"]["output"] == 5 * expected_responses[flow]
                assert turn["usage"]["cacheRead"] == 0
                assert turn["usage"]["cacheWrite"] == 0
                assert turn["usage"]["reasoning"] == 0
                assert turn["usage"]["totalTokens"] == 15 * expected_responses[flow]
                assert turn["usage"]["cost"] == {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "total": 0,
                }
                assert len(turn["usage"]["responses"]) == expected_responses[flow]
                assert all(
                    response["provider"] == "bello-local"
                    and response["model"] == "integration-model"
                    and response["api"] == "openai-completions"
                    and response["usage"]["input"] == 10
                    and response["usage"]["output"] == 5
                    and response["usage"]["totalTokens"] == 15
                    for response in turn["usage"]["responses"]
                )
                assert (workspaces[flow] / f"flow-{flow.lower()}.txt").read_text() == f"payload-{flow}\n"
                host_items = [item for item in turn["items"] if item.get("type") == "hostTool"]
                assert [item["id"] for item in host_items] == [
                    f"call-{flow.lower()}-write_file",
                    f"call-{flow.lower()}-read_file",
                    f"call-{flow.lower()}-exec_command",
                    *provider.poll_calls[flow],
                ]
                assert all(item["status"] == "completed" for item in host_items), json.dumps([
                    {
                        "id": item["id"],
                        "name": item.get("name"),
                        "status": item["status"],
                        "error": _bounded_fixture_json(item.get("error")),
                        "result": _bounded_fixture_json(item.get("result")),
                    }
                    for item in host_items
                ], ensure_ascii=False, indent=2)

            started_host_items = {
                raw["params"]["item"]["id"]
                for raw in events
                if raw.get("method") == "item/started"
                and isinstance(raw.get("params", {}).get("item"), dict)
                and raw["params"]["item"].get("type")
                in {"fileChange", "fileRead", "dynamicToolCall", "commandExecution"}
            }
            assert started_host_items == {
                "tool-" + hashlib.sha256(json.dumps([
                    turn["id"], f"call-{flow.lower()}-{tool}",
                ]).encode()).hexdigest()
                for flow, (turn, _) in zip(("A", "B"), results, strict=True)
                for tool in ("write_file", "read_file", "exec_command")
            }
            completed_host_items = {
                raw["params"]["item"]["id"]
                for raw in events
                if raw.get("method") == "item/completed"
                and isinstance(raw.get("params", {}).get("item"), dict)
                and raw["params"]["item"].get("type")
                in {"fileChange", "fileRead", "dynamicToolCall", "commandExecution"}
            }
            assert completed_host_items == started_host_items

            # Both final forms must survive a real worker restart without a
            # replayed model request, fabricated structured data or lost usage.
            await transport.stop()
            transport = await start_worker()
            for flow, (turn, _) in zip(("A", "B"), results, strict=True):
                await client.request("thread/resume", {"threadId": thread_ids[flow]})
                restored = await client.request("thread/read", {
                    "threadId": thread_ids[flow], "includeTurns": True,
                })
                restored_turns = restored["thread"]["turns"]
                assert len(restored_turns) == 1
                assert restored_turns[0] == turn
            assert len(provider.requests) == sum(expected_responses.values())
            assert not provider.errors
            if os.name == "nt":
                assert len(file_tool_commands) == 4  # A/B each perform a real write and read.
            assert not transport_errors
    finally:
        # RuntimeClient owns any backend inserted into _engines.
        await client.stop()
