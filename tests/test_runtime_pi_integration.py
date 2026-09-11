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
            "content": json.dumps(message.get("content"), ensure_ascii=False)[:2400],
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


def _tool_chunk(flow: str, step: int, name: str, arguments: dict[str, Any]) -> bytes:
    return _response_chunks(flow, step, {
        "role": "assistant",
        "tool_calls": [{
            "index": 0,
            "id": f"call-{flow.lower()}-{name}",
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
    def __init__(self, output_schema: dict[str, Any], final_mode: str):
        self.output_schema = output_schema
        self.final_mode = final_mode
        self.condition = threading.Condition()
        self.first_requests: set[str] = set()
        self.steps: dict[str, int] = {}
        self.requests: list[tuple[str, int, dict[str, Any]]] = []
        self.errors: list[str] = []

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
        required_tools = {"write_file", "read_file", "exec_command", "submit_result"}
        if not required_tools.issubset(tools):
            raise AssertionError(f"missing controlled tools: {sorted(required_tools - tools.keys())}")
        if tools["submit_result"].get("parameters") != self.output_schema:
            raise AssertionError("Pi did not send the exact Bello output schema to the local provider")

        filename = f"flow-{flow.lower()}.txt"
        if step == 0:
            return _tool_chunk(flow, step, "write_file", {
                "path": filename,
                "content": f"payload-{flow}\n",
            })
        if step == 1:
            if f"call-{flow.lower()}-write_file" not in serialized or "bytes_written" not in serialized:
                raise AssertionError("the write result did not return through the Pi conversation")
            return _tool_chunk(flow, step, "read_file", {"path": filename})
        if step == 2:
            if f"call-{flow.lower()}-read_file" not in serialized or f"payload-{flow}" not in serialized:
                raise AssertionError("the read result did not return through the Pi conversation")
            command = (
                f'findstr /x /c:"payload-{flow}" {filename} && echo exec-{flow}-ok'
                if os.name == "nt"
                else f'test "$(cat {filename})" = "payload-{flow}" && printf "exec-{flow}-ok"'
            )
            return _tool_chunk(flow, step, "exec_command", {"command": command, "timeout": 10})
        if step == 3:
            if f"call-{flow.lower()}-exec_command" not in serialized or f"exec-{flow}-ok" not in serialized:
                raise AssertionError("the command result did not return through the Pi conversation")
            if self.final_mode == "text":
                return _response_chunks(flow, step, {
                    "role": "assistant",
                    "content": json.dumps(_completion_decision(flow), ensure_ascii=False),
                }, "stop")
            return _tool_chunk(flow, step, "submit_result", _completion_decision(flow))
        raise AssertionError(f"unexpected extra model request for flow {flow}: step {step}")


@contextmanager
def _local_openai_provider(
    output_schema: dict[str, Any], final_mode: str,
) -> Iterator[tuple[str, _LocalProviderState]]:
    state = _LocalProviderState(output_schema, final_mode)

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
async def test_real_pi_sdk_runtime_client_toolhost_and_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_mode: str,
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
    await client.start()
    transport: WorkerTransport | None = None
    try:
        with _local_openai_provider(output_schema, final_mode) as (base_url, provider):
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

            listed = await client.request("model/list", {})
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
            assert provider.steps == {"A": 4, "B": 4}
            assert len(provider.requests) == 8
            assert not transport_errors
            usage_events = [
                raw["params"]["item"]
                for raw in events
                if raw.get("method") == "item/completed"
                and isinstance(raw.get("params", {}).get("item"), dict)
                and raw["params"]["item"].get("type") == "agentMessage"
                and isinstance(raw["params"]["item"].get("usage"), dict)
            ]
            assert len(usage_events) == 8
            assert all(item["usage"]["totalTokens"] == 15 for item in usage_events)
            for flow, (turn, decision) in zip(("A", "B"), results, strict=True):
                assert decision.decision is CompletionReviewDecisionKind.ACCEPT
                assert decision.reason == f"local Pi integration {flow} completed"
                if final_mode == "submit_result":
                    assert turn["structuredResult"] == _completion_decision(flow)
                else:
                    assert "structuredResult" not in turn
                    assert last_agent_message_text(turn) == json.dumps(_completion_decision(flow), ensure_ascii=False)
                assert turn["usage"]["input"] == 40
                assert turn["usage"]["output"] == 20
                assert turn["usage"]["cacheRead"] == 0
                assert turn["usage"]["cacheWrite"] == 0
                assert turn["usage"]["reasoning"] == 0
                assert turn["usage"]["totalTokens"] == 60
                assert turn["usage"]["cost"] == {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "total": 0,
                }
                assert len(turn["usage"]["responses"]) == 4
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
                ]
                assert all(item["status"] == "completed" for item in host_items)

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
            assert len(provider.requests) == 8
            assert not provider.errors
            if os.name == "nt":
                assert len(file_tool_commands) == 4  # A/B each perform a real write and read.
            assert not transport_errors
    finally:
        # RuntimeClient owns any backend inserted into _engines.
        await client.stop()
