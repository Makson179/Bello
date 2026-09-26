"""Exercise controller callers, not only the local decision-repair loop."""
from __future__ import annotations

import pytest

from supervisor.controller import _classify_supervisor_agent_error
from supervisor.schemas import BelloStatus
from supervisor.state import FINAL_REPORT, PROGRESS
from supervisor.supervisor_agent import SupervisorAgentError, SupervisorTurnError
from tests.test_bello_state import _runtime_controller
from tests.test_supervisor_terminal_error import TerminalClient, terminal_turn
from supervisor.supervisor_agent import StatelessSupervisorAgent


@pytest.mark.parametrize("detail", ["did not produce an agent message", "timeout", "rate limit 429", "unauthorized", "unknown"])
def test_typed_error_precedes_diagnostic_substring_classification(detail):
    error = SupervisorTurnError({"status": "failed", "error": {"message": detail}})
    assert _classify_supervisor_agent_error(error) == "terminal_turn"


@pytest.mark.parametrize("detail", ["did not produce an agent message", "timeout", "unknown"])
@pytest.mark.parametrize("status", ["failed", "interrupted"])
async def test_actual_completion_controller_never_retries_terminal_provider_turn(tmp_path, monkeypatch, detail, status):
    controller, store, _ = _runtime_controller(tmp_path)
    turn = terminal_turn("pi", status, "valid")
    turn["error"]["message"] = detail
    client = TerminalClient(turn, direct=True)
    controller.supervisor = StatelessSupervisorAgent(client, store, controller.task_path,
        model="openrouter/anthropic/claude-sonnet-5", completion_workspace_write=True)
    controller._completion_no_message_max_retries = 3
    controller._no_message_backoff_seconds = ()

    async def forbidden_retry(**kwargs):
        pytest.fail("typed terminal error reached text-based controller recovery")

    monkeypatch.setattr(controller, "_handle_supervisor_no_message_failure", forbidden_retry)
    monkeypatch.setattr(controller, "_handle_completion_review_timeout_failure", forbidden_retry)
    await controller._supervisor_check_loop("completion check", None, None, None, None, True)
    assert len(client.inputs) == 1 and client.history_reads == 0
    assert client.archived == ["review-thread"]
    assert controller.completion_attempt_count == 1
    assert controller.provider_failure_recovery_counts == {}
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "supervisor check failed (terminal_turn)" in store.path(FINAL_REPORT).read_text()
    assert controller.supervisor.completion_workspace_snapshot is None


async def test_retained_runtime_trigger_policy_remains_a_separate_bounded_retry(tmp_path):
    controller, store, _ = _runtime_controller(tmp_path)
    agent = StatelessSupervisorAgent(None, store, controller.task_path)

    class TerminalRuntime:
        calls = 0
        def build_packet(self, **kwargs):
            return agent.build_packet(**kwargs)
        async def decide(self, packet):
            self.calls += 1
            raise SupervisorTurnError({"status": "failed", "error": {"message": "timeout"}})

    runtime = TerminalRuntime()
    controller.supervisor = runtime
    controller._queue_supervisor_check("queued completion", completion_review=True)
    await controller._run_supervisor_check("runtime trigger", None, None, None, None)
    assert runtime.calls == 1
    assert controller.provider_failure_recovery_counts == {"runtime_monitor_terminal_turn": 1}
    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_completion_summary is not None
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert "retrying the retained runtime trigger" in store.path(PROGRESS).read_text()
    await controller._run_supervisor_check("retained runtime trigger", None, None, None, None)
    assert runtime.calls == 2
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert controller._supervisor_next_completion_summary is None


@pytest.mark.parametrize("detail,expected", [("did not produce an agent message", "no_message"), ("timeout", "tool_timeout")])
def test_existing_untyped_recovery_classification_is_unchanged(detail, expected):
    assert _classify_supervisor_agent_error(SupervisorAgentError(detail)) == expected
