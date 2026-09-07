from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
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

from supervisor.controller import BelloController
from supervisor.project_config import ProjectConfig
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.transport import WorkerTransport
from supervisor.schemas import (
    AdvReportControllerDecision,
    BelloStatus,
    CompletionReviewDecision,
    SupervisorDecision,
)
from supervisor.schemas.models import (
    openai_strict_json_schema_for_adv_report_controller_decision,
    openai_strict_json_schema_for_completion_review_decision,
    openai_strict_json_schema_for_supervisor_decision,
)
from supervisor.state import FINAL_REPORT, LOG


_MIN_NODE = Version("22.19.0")
_MODEL = "bello-local/pipeline-model"
_DUMMY_KEY = "bello-offline-pipeline-dummy-key"


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
    message = "the real Pi pipeline integration test requires Node.js >= 22.19"
    if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _pytest_command() -> str:
    executable = shutil.which("pytest")
    if executable is None:
        pytest.skip("the offline pipeline fixture requires pytest on the host")
    # The disposable coder workspace intentionally contains a denied
    # .supervisor control link. Select the task test explicitly so pytest does
    # not try to recurse into that private control surface during collection.
    return (
        f"{shlex_quote(str(Path(executable).resolve()))} "
        "-q -p no:cacheprovider test_solution.py"
    )


def shlex_quote(value: str) -> str:
    # Keep the test import surface small while producing an ordinary POSIX command.
    import shlex

    return shlex.quote(value)


def _usage_chunk(request_id: str) -> str:
    usage = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "pipeline-model",
        "choices": [],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
    return f"data: {json.dumps(usage, separators=(',', ':'))}\n\n"


def _tool_chunk(request_id: str, call_id: str, name: str, arguments: dict[str, Any]) -> bytes:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "pipeline-model",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(
                                    arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    return (
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        f"{_usage_chunk(request_id)}"
        "data: [DONE]\n\n"
    ).encode()


def _text_chunk(request_id: str, text: str) -> bytes:
    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "pipeline-model",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    return (
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        f"{_usage_chunk(request_id)}"
        "data: [DONE]\n\n"
    ).encode()


def _tool_functions(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        entry.get("function", {}).get("name"): entry.get("function", {})
        for entry in body.get("tools", [])
        if isinstance(entry, dict) and isinstance(entry.get("function"), dict)
    }


def _json_prompt(body: dict[str, Any], required_key: str) -> dict[str, Any]:
    def strings(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for nested in value.values():
                yield from strings(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from strings(nested)

    for candidate in strings(body.get("messages", [])):
        if required_key not in candidate:
            continue
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict) and required_key in decoded:
            return decoded
    raise AssertionError(f"request did not retain the JSON prompt key {required_key!r}")


def _supervisor_decision() -> dict[str, Any]:
    return SupervisorDecision(
        decision="noop",
        reason="The scripted offline run is making task-relevant progress.",
        wake_sequence=1,
        generation=0,
    ).model_dump(mode="json")


def _completion_decision() -> dict[str, Any]:
    return CompletionReviewDecision.model_validate(
        {
            "decision": "accept",
            "reason": "The implementation and focused pytest cover the requested addition behavior.",
            "decision_artifact": {
                "current_state": "solution.py implements addition and the fresh focused pytest passes.",
                "resolved_concerns": [],
                "stale_concerns": [],
                "uncovered_edge_candidates": [],
                "actionable_gap_or_none": None,
            },
            "files_reviewed": [
                {
                    "path": "app/solution.py",
                    "reason": "Inspected the implementation.",
                    "kind": "source",
                    "inspected": True,
                    "limitation": None,
                },
                {
                    "path": "app/test_solution.py",
                    "reason": "Inspected the behavioral pytest.",
                    "kind": "test",
                    "inspected": True,
                    "limitation": None,
                },
            ],
            "behavior_evidence_matrix": [],
            "uncovered_behaviors": [],
            "validation_gaps": [],
            "claim_evidence_mismatches": [],
            "packet_or_access_limitations": [],
            "changed_test_risks": [],
            "behavior_surface": [
                {
                    "category": "Adding two Python numeric values",
                    "status": "required",
                    "note": "Implemented directly and covered by a fresh pytest.",
                }
            ],
            "message_to_coder": None,
            "persistent_decision": None,
            "progress_update": "Independent completion review accepted the submitted behavior.",
            "clear_handoff": True,
            "display_message": None,
            "handoff": None,
            "wake_sequence": 1,
            "generation": 0,
        }
    ).model_dump(mode="json")


_ADVERSARY_REPORT = """candidate_finding: false
attacked: inspected the implementation and reran the focused addition behavior
previous_findings_checked: none supplied
findings: none
observations: none
held: positive, negative, and zero operand cases passed
not_reached: none
overall: I believe no defects remain in the submitted solution
"""


class _PipelineProviderState:
    def __init__(self, pytest_command: str, expected_reasoning_effort: str | None):
        self.pytest_command = pytest_command
        self.expected_reasoning_effort = expected_reasoning_effort
        self.lock = threading.Lock()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.roles: Counter[str] = Counter()
        self.finished_roles: set[str] = set()
        self.schemas: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []

    def response(self, body: dict[str, Any], authorization: str | None) -> bytes:
        if authorization != f"Bearer {_DUMMY_KEY}":
            raise AssertionError("the fake provider received anything other than its isolated dummy key")
        if body.get("model") != "pipeline-model":
            raise AssertionError(f"unexpected paid model route: {body.get('model')!r}")
        if self.expected_reasoning_effort is None:
            if "reasoning_effort" in body:
                raise AssertionError(
                    f"non-reasoning fixture received reasoning_effort={body['reasoning_effort']!r}"
                )
        elif body.get("reasoning_effort") != self.expected_reasoning_effort:
            raise AssertionError(
                "reasoning fixture did not receive the configured exact effort: "
                f"{body.get('reasoning_effort')!r}"
            )
        serialized = json.dumps(body, ensure_ascii=False)
        tools = _tool_functions(body)
        submit = tools.get("submit_result")
        properties = (
            submit.get("parameters", {}).get("properties", {})
            if isinstance(submit, dict)
            else {}
        )
        if "forward_to_coder" in properties:
            role = "adv_report_controller"
        elif "behavior_evidence_matrix" in properties:
            role = "completion"
        elif "approval_decision" in properties:
            role = "supervisor"
        elif "previous_adversary_report" in serialized:
            role = "adversary"
        elif "BELLO_READY_FOR_REVIEW" in serialized:
            role = "coder"
        else:
            raise AssertionError("could not identify the real Bello role from the request")

        with self.lock:
            self.requests.append((role, body))
            self.roles[role] += 1
            if submit is not None:
                self.schemas[role] = submit["parameters"]

        if role == "supervisor":
            return _tool_chunk(
                f"chatcmpl-supervisor-{self.roles[role]}",
                f"call-supervisor-{self.roles[role]}",
                "submit_result",
                _supervisor_decision(),
            )
        if role == "coder":
            return self._coder_response(serialized)
        if role == "completion":
            return self._completion_response(serialized)
        if role == "adversary":
            return self._adversary_response(serialized)
        return self._adv_report_controller_response(body, serialized)

    def _coder_response(self, serialized: str) -> bytes:
        if "call-coder-write-solution" not in serialized:
            return _tool_chunk(
                "chatcmpl-coder-1",
                "call-coder-write-solution",
                "write_file",
                {
                    "path": "app/solution.py",
                    "content": (
                        '"""Small arithmetic helpers."""\n\n'
                        "def add(left: int, right: int) -> int:\n"
                        "    return left + right\n"
                    ),
                },
            )
        if "call-coder-write-test" not in serialized:
            if "bytes_written" not in serialized:
                raise AssertionError("coder did not receive the implementation write result")
            return _tool_chunk(
                "chatcmpl-coder-2",
                "call-coder-write-test",
                "write_file",
                {
                    "path": "app/test_solution.py",
                    "content": (
                        "from solution import add\n\n\n"
                        "def test_add_handles_signs_and_zero():\n"
                        "    assert add(2, 3) == 5\n"
                        "    assert add(-2, 3) == 1\n"
                        "    assert add(0, 0) == 0\n"
                    ),
                },
            )
        if "call-coder-pytest" not in serialized:
            if "test_add_handles_signs_and_zero" not in serialized:
                raise AssertionError("coder did not receive the test write result")
            return _tool_chunk(
                "chatcmpl-coder-3",
                "call-coder-pytest",
                "exec_command",
                {"command": self.pytest_command, "cwd": "app", "timeout": 30},
            )
        if "1 passed" not in serialized:
            raise AssertionError("coder did not receive a passing pytest result")
        with self.lock:
            self.finished_roles.add("coder")
        return _text_chunk(
            "chatcmpl-coder-4",
            "Implemented the addition helper and a focused pytest; 1 test passed.\n\n"
            "BELLO_READY_FOR_REVIEW",
        )

    def _completion_response(self, serialized: str) -> bytes:
        if "call-completion-read-solution" not in serialized:
            return _tool_chunk(
                "chatcmpl-completion-1",
                "call-completion-read-solution",
                "read_file",
                {"path": "app/solution.py"},
            )
        if "call-completion-read-test" not in serialized:
            if "return left + right" not in serialized:
                raise AssertionError("completion review did not receive the implementation")
            return _tool_chunk(
                "chatcmpl-completion-2",
                "call-completion-read-test",
                "read_file",
                {"path": "app/test_solution.py"},
            )
        if "test_add_handles_signs_and_zero" not in serialized:
            raise AssertionError("completion review did not receive the behavioral test")
        with self.lock:
            self.finished_roles.add("completion")
        return _tool_chunk(
            "chatcmpl-completion-3",
            "call-completion-result",
            "submit_result",
            _completion_decision(),
        )

    def _adversary_response(self, serialized: str) -> bytes:
        if "call-adversary-read-solution" not in serialized:
            return _tool_chunk(
                "chatcmpl-adversary-1",
                "call-adversary-read-solution",
                "read_file",
                {"path": "app/solution.py"},
            )
        if "call-adversary-pytest" not in serialized:
            if "return left + right" not in serialized:
                raise AssertionError("adversary did not receive the submitted implementation")
            return _tool_chunk(
                "chatcmpl-adversary-2",
                "call-adversary-pytest",
                "exec_command",
                {"command": self.pytest_command, "cwd": "app", "timeout": 30},
            )
        if "1 passed" not in serialized:
            raise AssertionError("adversary did not receive its passing probe result")
        with self.lock:
            self.finished_roles.add("adversary")
        return _text_chunk("chatcmpl-adversary-3", _ADVERSARY_REPORT)

    def _adv_report_controller_response(
        self, body: dict[str, Any], serialized: str
    ) -> bytes:
        prompt = _json_prompt(body, "raw_adversary_report_path")
        if "call-adv-controller-read-task" not in serialized:
            return _tool_chunk(
                "chatcmpl-adv-controller-1",
                "call-adv-controller-read-task",
                "read_file",
                {"path": prompt["task_path"]},
            )
        if "call-adv-controller-read-report" not in serialized:
            if "Create app/solution.py" not in serialized:
                raise AssertionError("adversary report controller did not receive the task")
            return _tool_chunk(
                "chatcmpl-adv-controller-2",
                "call-adv-controller-read-report",
                "read_file",
                {"path": prompt["raw_adversary_report_path"]},
            )
        if "candidate_finding: false" not in serialized:
            raise AssertionError("adversary report controller did not receive the raw report")
        with self.lock:
            self.finished_roles.add("adv_report_controller")
        result = AdvReportControllerDecision(
            forward_to_coder=False,
            reason="0 findings kept, 0 rejected, 0 downgraded, and 0 observations carried.",
            report_to_coder=None,
        ).model_dump(mode="json")
        return _tool_chunk(
            "chatcmpl-adv-controller-3",
            "call-adv-controller-result",
            "submit_result",
            result,
        )


@contextmanager
def _local_provider(
    pytest_command: str,
    *,
    expected_reasoning_effort: str | None,
) -> Iterator[tuple[str, _PipelineProviderState]]:
    state = _PipelineProviderState(pytest_command, expected_reasoning_effort)

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
            except Exception as exc:  # pragma: no cover - surfaced in the parent test
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
        message = "the outer test sandbox denied the local Pi pipeline server"
        if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(f"{message}: {exc}")
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="bello-pipeline-provider", daemon=True)
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
    return {
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "PATH": os.pathsep.join((str(node.parent), "/usr/bin", "/bin")),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _git(project: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout


class _QuietTUI:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.input_queue: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def render(self, title: str, message: str) -> None:
        self.messages.append((title, message))

    def status(self, message: str) -> None:
        self.messages.append(("STATUS", message))


@pytest.mark.skipif(os.name == "nt", reason="restricted Windows execution intentionally fails closed")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasoning", "intelligence"),
    [
        pytest.param(False, "off", id="nonreasoning-off"),
        pytest.param(True, "high", id="reasoning-high-without-off"),
    ],
)
async def test_real_pi_offline_coder_completion_adversary_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reasoning: bool,
    intelligence: str,
) -> None:
    node = _supported_node()
    worker_dir = Path(__file__).resolve().parents[1] / "supervisor" / "pi_worker"
    worker = worker_dir / "worker.mjs"
    if not (worker_dir / "node_modules" / "@earendil-works" / "pi-coding-agent").is_dir():
        message = "the pinned Pi worker dependencies are not installed"
        if os.environ.get("BELLO_REQUIRE_PI_INTEGRATION") == "1":
            pytest.fail(message)
        pytest.skip(message)

    monkeypatch.setenv("BELLO_CODER_SANDBOX", "workspace-write")
    monkeypatch.delenv("BELLO_PROMPTS_FILE", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    task = project / "TASK.md"
    task.write_text(
        "# Task\n\nCreate app/solution.py with add(left, right), add a focused pytest, and run it.\n",
        encoding="utf-8",
    )
    (project / "README.md").write_text("# Tiny offline integration fixture\n", encoding="utf-8")
    (project / "app").mkdir()
    (project / "app" / "README.md").write_text("Implementation lives here.\n", encoding="utf-8")
    _git(project, "init", "-q")
    _git(project, "config", "user.name", "Bello Integration")
    _git(project, "config", "user.email", "bello-integration@example.invalid")
    _git(project, "add", "TASK.md", "README.md", "app/README.md")
    _git(project, "commit", "-q", "-m", "initial fixture")

    agent_dir = tmp_path / "isolated-pi-agent"
    state_dir = tmp_path / "private-runtime"
    agent_dir.mkdir()
    tui = _QuietTUI()
    client = RuntimeClient(cwd=project, state_dir=state_dir)
    config = ProjectConfig(
        task="TASK.md",
        coder_mod=_MODEL,
        runtime_mod=_MODEL,
        completion_mod=_MODEL,
        adversary_mod=_MODEL,
        coder_intelligence=intelligence,
        runtime_intelligence=intelligence,
        completion_intelligence=intelligence,
        adversary_intelligence=intelligence,
        cheap_runtime=False,
        completion_review=True,
        adversary=True,
        adversary_runs=1,
    )
    controller = BelloController(
        project,
        task_path=task,
        client=client,
        tui=tui,  # type: ignore[arg-type]
        coder_model=_MODEL,
        runtime_model=_MODEL,
        completion_model=_MODEL,
        adversary_model=_MODEL,
        coder_intelligence=intelligence,
        runtime_intelligence=intelligence,
        completion_intelligence=intelligence,
        adversary_intelligence=intelligence,
        overwrite_state=True,
        use_git_diff=True,
        adversary_enabled=True,
        adversary_runs=1,
        completion_review=True,
        project_config=config,
    )
    client.notification_handler = controller._on_notification
    client.server_request_handler = controller._on_server_request
    client.transport_error_handler = controller._on_transport_error

    await client.start()
    transport: WorkerTransport | None = None
    inserted = False
    try:
        with _local_provider(
            _pytest_command(),
            expected_reasoning_effort="high" if reasoning else None,
        ) as (base_url, provider):
            (agent_dir / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "bello-local": {
                                "name": "Bello offline pipeline provider",
                                "baseUrl": base_url,
                                "api": "openai-completions",
                                "apiKey": _DUMMY_KEY,
                                "authHeader": True,
                                "compat": {
                                    "supportsDeveloperRole": False,
                                    "supportsReasoningEffort": reasoning,
                                    "supportsUsageInStreaming": True,
                                },
                                "models": [
                                    {
                                        "id": "pipeline-model",
                                        "name": "Offline pipeline model",
                                        "reasoning": reasoning,
                                        **(
                                            {"thinkingLevelMap": {"off": None, "high": "high"}}
                                            if reasoning
                                            else {}
                                        ),
                                        "input": ["text"],
                                        "contextWindow": 32768,
                                        "maxTokens": 4096,
                                        "cost": {
                                            "input": 0,
                                            "output": 0,
                                            "cacheRead": 0,
                                            "cacheWrite": 0,
                                        },
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            async def emit(message: dict[str, Any]) -> None:
                await client._emit(message, engine="pi")

            transport = WorkerTransport(
                [str(node), str(worker)],
                worker_dir,
                emit=emit,
                tool=client._call_tool,
                on_error=client._notify_transport_error,
                env=_worker_environment(
                    node,
                    tmp_path / "worker-home",
                    tmp_path / "worker-tmp",
                ),
            )
            await transport.start()
            initialized = await transport.request(
                "initialize",
                {
                    "stateDir": str(state_dir / "pi"),
                    "agentDir": str(agent_dir),
                    "allowModelNetwork": False,
                },
            )
            assert initialized["serverInfo"]["piSdkVersion"] == "0.85.1"
            client._engines["pi"] = transport
            inserted = True

            await asyncio.wait_for(controller.run(), timeout=90)

            assert not provider.errors
            assert provider.finished_roles == {
                "coder",
                "completion",
                "adversary",
                "adv_report_controller",
            }
            assert provider.roles["supervisor"] >= 2  # startup self-test plus real runtime oversight
            assert provider.schemas["supervisor"] == openai_strict_json_schema_for_supervisor_decision()
            assert provider.schemas["completion"] == openai_strict_json_schema_for_completion_review_decision()
            assert provider.schemas["adv_report_controller"] == (
                openai_strict_json_schema_for_adv_report_controller_decision()
            )
    finally:
        if not inserted and transport is not None:
            await transport.stop()
        if client._started:
            await client.stop()

    assert controller.store.get_bello_config().status is BelloStatus.COMPLETE
    assert controller._snapshot_patch_applied is True
    assert controller._coder_snapshot is None
    assert (project / "app" / "solution.py").read_text(encoding="utf-8").endswith(
        "    return left + right\n"
    )
    assert "test_add_handles_signs_and_zero" in (
        project / "app" / "test_solution.py"
    ).read_text(encoding="utf-8")
    assert sorted(
        _git(
            project,
            "status",
            "--short",
            "--",
            "app/solution.py",
            "app/test_solution.py",
        ).splitlines()
    ) == ["?? app/solution.py", "?? app/test_solution.py"]

    final_report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "- Status: complete" in final_report
    assert "Completion review accepted: true" in final_report
    assert "pytest" in final_report and "behavioral pass" in final_report
    assert "candidate_finding=false" in final_report
    log_entries = [
        json.loads(line)
        for line in controller.store.path(LOG).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(entry.get("type") == "coder_snapshot_patch_applied" for entry in log_entries)
    assert any(entry.get("type") == "adversary_report" for entry in log_entries)
