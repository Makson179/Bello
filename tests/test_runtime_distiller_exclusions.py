"""Protected requirements/help bypass the selector, not the existing tool budget."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from supervisor.runtime.file_worker import frame_response
from supervisor.runtime.client import RuntimeClient
from supervisor.runtime.journal import RuntimeJournal
from supervisor.runtime.output_budget import budget_command_output
from supervisor.runtime.sandbox import SandboxResult
from supervisor.runtime.tools import ToolHost, ToolScope


@dataclass
class Harness:
    host: ToolHost
    root: Path
    scopes: dict
    events: list
    selector_calls: list
    output: str = "--output FORMAT\n--errors-only\n--timeout SECONDS\n"
    exit_code: int = 0
    executions: list = field(default_factory=list)

    async def call(self, name, arguments, call_id="call"):
        return await self.host.call({
            "threadId": "coder", "turnId": "turn", "callId": call_id,
            "name": name, "arguments": arguments,
        })


@pytest.fixture
def harness(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    journal = RuntimeJournal(tmp_path / "state")
    scopes = {("coder", "turn"): ToolScope(root, "workspace-write", distiller_enabled=True)}
    events, selector_calls = [], []

    async def approve(*_args):
        raise AssertionError("ordinary contained calls must not request approval")

    async def emit(message):
        events.append(message)

    async def delegate(*_args):
        raise AssertionError("unexpected delegation")

    async def distill(text, focus, command):
        selector_calls.append((text, focus, command))
        return "short\n"

    class Runner:
        def __init__(self, policy):
            self.policy = policy

        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            fixture.executions.append((self.policy, command, cwd))
            if "file_worker.py" in command:
                operation = json.loads(base64.b64decode(shlex.split(command)[-1]))
                name = operation["name"]
                if name == "read_file":
                    value = {"text": fixture.output, "offset": 1, "returned_lines": 3,
                             "total_lines": 3}
                elif name == "search":
                    value = {"matches": [{"path": operation["arguments"]["path"],
                                           "line": 1, "text": fixture.output}], "errors": []}
                elif name == "list_directory":
                    value = {"entries": [{"name": "overview.md", "type": "file"}]}
                else:
                    raise AssertionError(f"unexpected file operation: {name}")
                return SandboxResult(frame_response(operation["response_nonce"], name, value), 0, .25)
            if on_output:
                await on_output(fixture.output)
            return SandboxResult(fixture.output, fixture.exit_code, .25)

    host = ToolHost(journal, lambda thread, turn: scopes[(thread, turn)],
                    approve, emit, delegate, runner_factory=Runner, distill=distill)
    fixture = Harness(host, root, scopes, events, selector_calls)
    yield fixture
    journal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "TASK.md", "TASK", "README", "README.rst", "AGENTS.md",
    "CLAUDE.md", "SPEC.txt", "instructions.txt", "SPECIFICATION.json",
])
async def test_requirement_reads_match_unmodified_tool_response(harness, path):
    # The fake runner returns exactly the same packet with D off and on.
    args = {"path": path, "focus": "Learn requirements"}
    harness.scopes[("coder", "turn")] = ToolScope(harness.root, "workspace-write")
    baseline = await harness.call("read_file", args, "without-selector")
    harness.scopes[("coder", "turn")] = ToolScope(
        harness.root, "workspace-write", distiller_enabled=True)
    protected = await harness.call("read_file", args, "with-selector")
    assert not protected["isError"]
    assert protected == baseline
    assert not harness.selector_calls
    assert len(harness.executions) == 2
    completed = [event["params"]["item"] for event in harness.events
                 if event["method"] == "item/completed"]
    assert json.loads(completed[-1]["aggregatedOutput"])["text"] == harness.output


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "tickets/custom-spec.txt"}),
    ("exec_command", {"command": "cat tickets/custom-spec.txt"}),
    ("exec_command", {"command": "cat custom-spec.txt", "cwd": "tickets"}),
])
async def test_actual_configured_task_is_protected_without_a_special_filename(harness, name, args):
    task = harness.root / "tickets" / "custom-spec.txt"
    task.parent.mkdir()
    task.write_text("These are the actual task requirements.\n", encoding="utf-8")
    harness.scopes[("coder", "turn")] = ToolScope(
        harness.root, "workspace-write", distiller_enabled=True, task_path=task)
    result = await harness.call(name, {**args, "focus": "Read task"})
    assert not result["isError"]
    text = result["details"]["output"] if name == "exec_command" else result["content"][0]["text"]
    assert text == harness.output
    assert not harness.selector_calls


@pytest.mark.asyncio
async def test_same_basename_elsewhere_does_not_inherit_configured_task_protection(harness):
    task = harness.root / "tickets" / "custom-spec.txt"
    harness.scopes[("coder", "turn")] = ToolScope(
        harness.root, "workspace-write", distiller_enabled=True, task_path=task)
    result = await harness.call("read_file", {"path": "notes/custom-spec.txt", "focus": "Read details"})
    assert not result["isError"]
    assert result["content"][0]["text"] == "short\n"
    assert len(harness.selector_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("search", {"path": "README.md", "pattern": "output"}),
    ("search", {"path": "SPECIFICATION.json", "pattern": "timeout"}),
])
async def test_searching_named_requirements_bypasses(harness, name, args):
    result = await harness.call(name, {**args, "focus": "Inspect docs"})
    assert not result["isError"]
    assert result["content"][0]["text"] != "short\n"
    assert not harness.selector_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "cat TASK.md",
    "cat README; python checks.py",
    "sed -n '1,120p' SPEC.txt && ./reference --help",
    "./reference --help",
    "./reference -h",
    "bash fixture-help.sh --help; exit 7",
    "sh ./reference.sh -h",
    "bash -e ./reference.sh --help",
    "zsh -- ./reference.sh --help",
    "bash --help",
    "git help status",
    "man git",
    'python -c "from pathlib import Path; print(Path(\'TASK.md\').read_text())"',
    "python - <<'PY'\nimport subprocess\nprint(subprocess.run(['./reference', '--help'], capture_output=True).stdout)\nPY",
    'set +e\nfor args in "" "--help" "-h" "--version"; do echo "=== $args"; ./executable $args; echo STATUS:$?; done',
    "cat TASK.md; python - <<'PY'\nprint(1)\nPY",
])
async def test_help_and_mixed_command_packets_keep_text_and_evidence(harness, command):
    harness.output = "HEADER\n" + "details\n" * 100 + "--errors-only\n"
    result = await harness.call("exec_command", {
        "command": command, "focus": "Find requirements", "max_output_tokens": 100,
    })
    packet = result["details"]
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == packet
    assert packet["output"] == budget_command_output(harness.output, 100).text
    assert packet["outputBudget"]["truncated"]
    assert packet["exitCode"] == 0 and packet["status"] == "completed"
    assert packet["sessionId"]
    assert not harness.selector_calls
    assert harness.events[-1]["params"]["item"]["aggregatedOutput"] == harness.output
    assert not any(key in packet for key in ("distiller", "raw_handle", "focus"))


@pytest.mark.asyncio
async def test_help_error_keeps_error_status_and_does_not_run_selector(harness):
    harness.exit_code = 2
    harness.output = "Usage: reference [--output FORMAT] [--errors-only]\nMissing input\n"
    result = await harness.call("exec_command", {"command": "./reference --help", "focus": "Inspect CLI"})
    assert result["isError"]
    assert result["details"]["exitCode"] == 2
    assert result["details"]["status"] == "failed"
    assert result["details"]["output"] == harness.output
    assert not harness.selector_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "python checks.py", "pytest -q", "python help_parser.py", "pytest tests/test_help.py",
    "git diff", "git diff -- README.md", "cat docs/run-log.md", "cat requirements.txt",
    "python -c 'print(\"--help\")'",
    "bash -c 'printf \"%s\" --help'",
    "bash -lc 'echo --help'",
    "bash -ec 'echo --help'",
    "bash -s -- --help",
    "bash ./checks.sh '--help appears only in text'",
    "bash ./checks.sh -- --help",
    "bash ./checks.sh",
    "bash cat TASK.md",
    "grep -h error build.log", "du -h build", "echo --help", "printf '%s' --help",
    "./check -- --help", "sed -i 's/foo/bar/' README.md", "cat notes.txt > README.md",
    'python -c "open(\'TASK.md\', \'w\').write(\'text\')"',
    'set +e\nfor args in "" "--help" "-h" "--version"; do echo "$args"; done',
])
async def test_normal_test_logs_still_reach_selector(harness, command):
    # Words in focus/output are not a reason to classify a command as a docs read.
    result = await harness.call("exec_command", {
        "command": command, "focus": "Check help support",
    })
    assert result["details"]["output"] == "short\n"
    assert harness.selector_calls == [(harness.output, "Check help support", command)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "notes.md"}),
    ("read_file", {"path": "docs/run-log.md"}),
    ("read_file", {"path": "requirements.txt"}),
    ("read_file", {"path": "result.json"}),
    ("search", {"path": "docs", "pattern": "timeout"}),
    ("list_directory", {"path": "docs"}),
])
async def test_generic_document_and_log_paths_remain_eligible(harness, name, args):
    result = await harness.call(name, {**args, "focus": "Check details"})
    assert not result["isError"]
    assert result["content"][0]["text"] == "short\n"
    assert len(harness.selector_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
async def test_disabled_distiller_remains_disabled_for_any_scope(harness, mode):
    harness.scopes[("coder", "turn")] = ToolScope(harness.root, mode, distiller_enabled=False)
    result = await harness.call("exec_command", {"command": "python checks.py", "focus": "Check"})
    assert result["details"]["output"] == harness.output
    assert not harness.selector_calls
    assert harness.executions[0][0].mode == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["poll_command", "stop_command"])
@pytest.mark.parametrize("command,cwd", [
    ("./reference --help", None),
    ("cat TASK.md; wait_for_diagnostics", None),
    ("cat custom-task.txt", "tickets"),
])
async def test_yielded_protected_command_stays_protected_when_polled_or_stopped(harness, operation, command, cwd):
    release = asyncio.Event()
    tail = "--output FORMAT\n--errors-only\n"
    if cwd:
        (harness.root / cwd).mkdir()
        harness.scopes[("coder", "turn")] = ToolScope(
            harness.root, "workspace-write", distiller_enabled=True,
            task_path=harness.root / cwd / "custom-task.txt")

    class Runner:
        def __init__(self, policy):
            pass

        async def run(self, command, cwd, timeout, on_output=None, *, cancel_event=None):
            if operation == "stop_command":
                await cancel_event.wait()
            else:
                await release.wait()
            await on_output(tail)
            cancelled = operation == "stop_command"
            return SandboxResult(tail, 130 if cancelled else 0, .25, cancelled=cancelled)

    harness.host.runner_factory = Runner
    try:
        initial = await harness.call("exec_command", {
            "command": command, "yield_time_ms": 0, "focus": "Read requirements",
            **({"cwd": cwd} if cwd else {}),
        }, "initial")
        assert initial["details"]["status"] == "running"
        release.set()
        args = {"session_id": initial["details"]["sessionId"], "focus": "Read result"}
        if operation == "poll_command":
            args["yield_time_ms"] = 1000
        final = await harness.call(operation, args, "followup")
        assert final["details"]["output"] == tail
        assert final["details"]["status"] == ("cancelled" if operation == "stop_command" else "completed")
        assert not final["isError"]
        assert not harness.selector_calls
        assert harness.events[-1]["params"]["item"]["aggregatedOutput"] == tail
    finally:
        release.set()
        await harness.host.close()


@pytest.mark.asyncio
async def test_configured_task_scope_survives_coder_repair_resume_and_child(tmp_path):
    class FakeBackend:
        async def request(self, method, params, timeout=30):
            if method == "thread/start":
                return {"thread": {"id": params["threadId"]}}
            if method == "turn/start":
                return {"turn": {"id": params["turnId"], "status": "inProgress"}}
            return {}

        async def stop(self):
            pass

    class FakeDistiller:
        async def distill(self, *_args):
            raise AssertionError("this scope-only test must not invoke the selector")

        async def close(self):
            pass

    root, workspace = tmp_path / "controller", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    task = workspace / "custom-job.txt"
    client = RuntimeClient(cwd=root, backends={"codex": FakeBackend(), "claude-code": FakeBackend()})
    client._distiller = FakeDistiller()
    await client.start()
    try:
        request = {"cwd": str(workspace), "runtimeWorkspaceRoots": [str(workspace)],
                   "runtimeTaskPath": str(task), "model": "gpt-5.6-sol",
                   "sandbox": "workspace-write", "belloRole": "coder"}
        parent = (await client.thread_start({**request, "config": {"agents": {
            "enabled": True, "max_concurrent_threads_per_session": 1,
            "allowed_profiles": {"claude-code/claude-sonnet-4-6": ["high"]},
        }}}))["thread"]["id"]
        first = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        assert client._scope_for(parent, first).task_path == task
        assert client._scope_for(parent, first).distiller_enabled
        await client._emit({"method": "turn/completed", "params": {
            "threadId": parent, "turn": {"id": first, "status": "completed"},
        }})
        repair = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        assert client._scope_for(parent, repair).task_path == task
        assert client._scope_for(parent, repair).distiller_enabled
        delegated = await client._delegate("spawn_agent", {
            "model": "claude-code/claude-sonnet-4-6", "effort": "high", "message": "Inspect CLI requirements",
        }, parent, repair)
        child = json.loads(delegated["content"][0]["text"])["agent_id"]
        child_turn = client._threads[child]["activeTurnId"]
        assert client._scope_for(child, child_turn).task_path == task
        assert client._scope_for(child, child_turn).distiller_enabled
        await client.turn_interrupt(parent, repair)
        await client.request("thread/resume", {"threadId": parent})
        resumed = (await client.turn_start({"threadId": parent}))["turn"]["id"]
        assert client._scope_for(parent, resumed).task_path == task
        assert client._scope_for(parent, resumed).distiller_enabled
        reviewer = (await client.thread_start({**request, "belloRole": "completion"}))["thread"]["id"]
        review_turn = (await client.turn_start({"threadId": reviewer}))["turn"]["id"]
        assert not client._scope_for(reviewer, review_turn).distiller_enabled
    finally:
        await client.stop()
