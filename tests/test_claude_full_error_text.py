"""Claude Code CLI error text is shown whole: never truncated, filtered or redacted.

This is an explicit owner decision for Claude Code errors. Other engines keep
their bounded, redacted provider errors. Offline: no CLI or model is started,
except the local Python interpreter standing in for a failing auth probe.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from claude_agent_sdk._errors import ProcessError

from supervisor.appserver import AppServerError, AppServerMessage
from supervisor.runtime.claude import CLI_STDERR_LOG, ClaudeBackend, ClaudeCliError
from supervisor.runtime_errors import bounded_provider_error
from supervisor.schemas import BelloStatus
from tests.support.controller import _runtime_controller
from tests.test_runtime_claude import (
    FakeClient, FakeFactory, backend, result_message, start_thread, wait_completed,
)

LONG_STDERR = "Claude Code: OAuth token has expired; refresh_token=abc123 Authorization: Bearer xyz " + "z" * 6000


class _StderrFactory(FakeFactory):
    failing = False

    def __call__(self, options):
        factory = self

        class Client(FakeClient):
            async def connect(self):
                if not factory.failing:
                    return await super().connect()
                self.options.stderr(LONG_STDERR)
                self.options.stderr("second CLI line")
                raise ProcessError("Command failed with exit code 1", exit_code=1,
                                   stderr="Check stderr output for details")

        client = Client(self, options)
        self.clients.append(client)
        return client


@pytest.mark.asyncio
async def test_cli_stderr_reaches_the_turn_error_diagnostic_and_log_in_full(tmp_path):
    events = []
    factory = _StderrFactory(complete_after_queries=None)
    runtime = backend(tmp_path, factory, events)
    try:
        await start_thread(runtime, tmp_path)
        factory.failing = True
        await runtime.request("turn/start", {"threadId": "thread-1", "turnId": "failed-turn",
                                             "input": [{"type": "text", "text": "work"}]})
        completed = (await wait_completed(events))["params"]["turn"]
        assert completed["status"] == "failed" and completed["error"]["belloFullText"] is True
        message = completed["error"]["message"]
        assert message.startswith("Claude Code turn failed: ProcessError: Command failed with exit code 1")
        assert message.endswith("Claude Code CLI stderr:\n" + LONG_STDERR + "\nsecond CLI line")
        diagnostic = completed["failureDiagnostic"]
        assert diagnostic["cliStderr"] == LONG_STDERR + "\nsecond CLI line"
        assert diagnostic["exceptions"][0]["type"] == "ProcessError" and diagnostic["exceptions"][0]["exit_code"] == 1
        log_path = tmp_path / "claude-state" / CLI_STDERR_LOG
        log = log_path.read_text(encoding="utf-8")
        assert f"thread=thread-1 {LONG_STDERR}\n" in log and "thread=thread-1 second CLI line\n" in log
        if os.name != "nt":
            assert log_path.stat().st_mode & 0o777 == 0o600
        # A new turn starts with an empty stderr buffer for that thread.
        factory.failing = False
        assert runtime._cli_stderr_lines("thread-1") == [LONG_STDERR, "second CLI line"]
    finally:
        await runtime.stop()



class _ErrorResultFactory(FakeFactory):
    """The CLI ends the turn with an error result instead of raising."""

    def __init__(self, result):
        super().__init__(complete_after_queries=None)
        self.result = result

    def __call__(self, options):
        factory = self

        class Client(FakeClient):
            async def query(self, prompt):
                await super().query(prompt)
                self.options.stderr(RESULT_STDERR)
                await self.messages.put(AssistantMessage(content=[TextBlock(text="working")], model="sonnet"))
                await self.messages.put(factory.result)

        client = Client(self, options)
        self.clients.append(client)
        return client


RESULT_STDERR = "CLI: request failed with 529; Authorization: Bearer xyz"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["api_error", "cli_errors"])
async def test_an_error_result_reports_the_cli_text_and_stderr_in_full(tmp_path, case):
    # API failures arrive as subtype "success" with the prose in result; the
    # CLI's own terminal errors arrive in errors[]. Neither raises in the SDK.
    events = []
    result = result_message(result="API Error: 529 {\"type\":\"overloaded_error\"} token=abc123"
                            if case == "api_error" else None, is_error=True)
    if case == "api_error":
        result.api_error_status = 529
    else:
        result.subtype = "error_during_execution"
        result.errors = ["Tool permission stream closed before response", "second CLI error"]
    runtime = backend(tmp_path, _ErrorResultFactory(result), events)
    try:
        await start_thread(runtime, tmp_path)
        await runtime.request("turn/start", {"threadId": "thread-1", "turnId": "failed-turn",
                                             "input": [{"type": "text", "text": "work"}]})
        turn = (await wait_completed(events))["params"]["turn"]
    finally:
        await runtime.stop()
    assert turn["status"] == "failed" and turn["error"]["belloFullText"] is True
    message, diagnostic = turn["error"]["message"], turn["failureDiagnostic"]
    assert message.endswith("\nClaude Code CLI stderr:\n" + RESULT_STDERR)
    assert diagnostic["version"] == 2 and diagnostic["cliStderr"] == RESULT_STDERR
    if case == "api_error":
        assert message.startswith("Claude Code returned an error result (subtype=success, terminal_reason=completed, "
                                  "api_error_status=529, num_turns=1):\nAPI Error: 529 ")
        assert "token=abc123" in message
        assert diagnostic["errorResult"]["api_error_status"] == 529
        assert diagnostic["errorResult"]["result"] == result.result
    else:
        assert message.startswith("Claude Code returned an error result (subtype=error_during_execution, ")
        assert "\nTool permission stream closed before response\nsecond CLI error\n" in message
        assert diagnostic["errorResult"]["errors"] == result.errors

def test_provider_errors_flagged_full_text_stay_whole_and_others_stay_bounded():
    message = "token=abc123 " + "x" * 9000
    full = bounded_provider_error({"error": {"message": message, "additionalDetails": message, "belloFullText": True}})
    assert full == {"error": {"message": message, "additionalDetails": message, "belloFullText": True}}
    bounded = bounded_provider_error({"error": {"message": message}})["error"]["message"]
    assert bounded != message and "abc123" not in bounded and len(bounded) < 4200
    # Only the literal boolean set by the Claude backend lifts the bound.
    assert bounded_provider_error({"error": {"message": message, "belloFullText": "yes"}})["error"]["message"] == bounded


@pytest.mark.asyncio
async def test_controller_logs_a_failed_claude_turn_in_full(tmp_path, monkeypatch):
    controller, store, _ = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={
        "status": BelloStatus.RUNNING, "active_coder_turn_id": "turn-1",
    }))
    for name in ("_track_subagent_notification", "_refresh_coder_subagents"):
        monkeypatch.setattr(controller, name, AsyncMock())
    monkeypatch.setattr(controller, "_write_run_checkpoint", Mock())
    fail = AsyncMock()
    monkeypatch.setattr(controller, "fail_provider", fail)
    message = "Claude Code turn failed: ProcessError: exit 1\nClaude Code CLI stderr:\n" + LONG_STDERR
    await controller.handle_notification(AppServerMessage({"method": "turn/completed", "params": {
        "threadId": "thread",
        "turn": {"id": "turn-1", "status": "failed", "error": {"message": message, "belloFullText": True}},
    }}))
    fail.assert_awaited_once()
    assert fail.await_args.args[0] == "coder execution failed: " + message


@pytest.mark.asyncio
async def test_auth_status_failure_carries_the_complete_cli_output(tmp_path):
    runtime = ClaudeBackend.__new__(ClaudeBackend)
    # The local interpreter stands in for a CLI that fails without JSON output:
    # `python auth status --json` exits 2 with an explanation on stderr.
    runtime._cli_path = Path(sys.executable)
    runtime._environment = dict(os.environ)
    runtime.state_dir = tmp_path
    with pytest.raises(ClaudeCliError) as raised:
        await runtime._official_auth_status()
    text = str(raised.value)
    assert text.startswith("official Claude Code auth status returned invalid JSON (exit code 2)")
    assert "Claude Code CLI stderr:\n" in text and "auth" in text.split("Claude Code CLI stderr:\n", 1)[1]
    assert raised.value.bello_full_text is True
    assert "auth" in (tmp_path / CLI_STDERR_LOG).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_rejected_commands_nobody_awaits_are_not_reported_as_unretrieved(tmp_path):
    import asyncio
    import gc

    from supervisor.runtime.claude import _Command, _Owner

    loop = asyncio.get_running_loop()
    reports = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context.get("message", "")))
    try:
        runtime = backend(tmp_path, FakeFactory(complete_after_queries=None), [])
        await start_thread(runtime, tmp_path)
        queue: asyncio.Queue = asyncio.Queue()
        blocker = asyncio.Event()
        runtime._owners["thread-1"] = _Owner(queue, asyncio.create_task(blocker.wait()))
        # A requester that gave up, and stop()'s own never-awaited command.
        waiting = asyncio.create_task(runtime._send_owner("thread-1", "interrupt", {}))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        stopping = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        while not queue.empty():
            runtime._reject(queue.get_nowait(), AppServerError("Claude Code session owner is closed"))
        blocker.set()
        await stopping
        del waiting, stopping
        gc.collect()
        await asyncio.sleep(0)
        assert not [message for message in reports if "never retrieved" in message]
    finally:
        loop.set_exception_handler(previous)
