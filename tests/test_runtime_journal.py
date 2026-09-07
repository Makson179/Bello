from __future__ import annotations

import pytest

from supervisor.runtime.journal import JournalError, RuntimeJournal
from supervisor.runtime.models import ModelSelectionError, parse_model_selection, validate_effort


def test_old_model_keeps_subscription_route():
    selected = parse_model_selection("gpt-5.6-sol")
    assert selected.qualified == "openai-codex/gpt-5.6-sol"
    assert selected.billing_route == "subscription"
    assert selected.engine == "pi"
    assert parse_model_selection("openai/gpt-5.6-sol").billing_route == "provider-api"
    assert parse_model_selection("claude-code/claude-sonnet-4-6").engine == "claude-code"
    assert parse_model_selection("openrouter/qwen/qwen3-coder").model == "qwen/qwen3-coder"


@pytest.mark.parametrize("model", ["", "qwen3", " gpt-5", "openai/", "openai//gpt", "OPENAI/gpt", "openai/gpt\n"])
def test_ambiguous_model_rejected(model):
    with pytest.raises(ModelSelectionError):
        parse_model_selection(model)


def test_effort_is_not_clamped():
    with pytest.raises(ModelSelectionError, match="not substitute"):
        validate_effort("ultra", ["high", "xhigh", "max"])
    validate_effort("max", ["high", "max"])


def test_completed_tool_is_reused_but_never_executed_twice(tmp_path):
    journal = RuntimeJournal(tmp_path)
    assert journal.claim_tool("t", "c", "exec_command", {"command": "test"}) is None
    journal.complete_tool("t", "c", {"output": "ok", "exitCode": 0})
    assert journal.claim_tool("t", "c", "exec_command", {"command": "test"}) == {"output": "ok", "exitCode": 0}
    with pytest.raises(JournalError, match="different"):
        journal.claim_tool("t", "c", "exec_command", {"command": "different"})
    journal.close()


def test_uncertain_request_is_not_replayed_after_restart(tmp_path):
    journal = RuntimeJournal(tmp_path)
    journal.claim_tool("t", "c", "exec_command", {"command": "write"})
    journal.save_thread("t", {"provider": "openai-codex", "cwd": "/isolated"})
    journal.close()
    journal = RuntimeJournal(tmp_path)
    assert journal.threads() == {"t": {"provider": "openai-codex", "cwd": "/isolated"}}
    with pytest.raises(JournalError, match="uncertain"):
        journal.claim_tool("t", "c", "exec_command", {"command": "write"})
    journal.close()


def test_journal_symlink_rejected(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.write_text("do not modify")
    (tmp_path / "runtime.sqlite3").symlink_to(outside)
    with pytest.raises(JournalError, match="symbolic"):
        RuntimeJournal(tmp_path)
    assert outside.read_text() == "do not modify"


@pytest.mark.parametrize("filename", ["runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm"])
def test_journal_rejects_linked_database_and_sidecars(tmp_path, filename):
    outside = tmp_path / "unrelated.txt"
    outside.write_text("preserve me")
    state = tmp_path / "state"
    state.mkdir()
    (state / filename).hardlink_to(outside)
    with pytest.raises(JournalError, match="unshared"):
        RuntimeJournal(state)
    assert outside.read_text() == "preserve me"


def test_journal_rejects_redirected_supervisor_parent_before_writing(tmp_path):
    outside = tmp_path / "unrelated"
    outside.mkdir()
    (tmp_path / ".supervisor").symlink_to(outside, target_is_directory=True)
    with pytest.raises(JournalError, match="symbolic"):
        RuntimeJournal(tmp_path / ".supervisor" / "engines")
    assert not (outside / "engines").exists()
