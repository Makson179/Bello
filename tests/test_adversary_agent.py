from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from supervisor.adversary_agent import AdversaryAgent, AdversaryAgentError, _report_has_candidate_finding
from supervisor.project_config import MultiAgentConfig
from supervisor.schemas import SupervisorWakePacket, ValidationRun


def _packet(tmp_path: Path) -> SupervisorWakePacket:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\nHandle stack arguments.\n", encoding="utf-8")
    return SupervisorWakePacket(
        wake_sequence=11,
        latest_event_sequence=11,
        generation=0,
        restart_count=0,
        task_path=str(task),
        task_contents=task.read_text(encoding="utf-8"),
        current_summary="coder marked ready",
        latest_relevant_change_sequence=2,
        validations=[
            ValidationRun(
                command="pytest tests/test_app.py",
                exit_code=0,
                passed=True,
                summary="1 passed",
                captured_output="1 passed\n",
                executed_test_files=["tests/test_app.py"],
                sequence=3,
            )
        ],
    )


def test_adversary_agent_disables_native_subagents_by_default(tmp_path: Path) -> None:
    agent = AdversaryAgent(object(), tmp_path)  # type: ignore[arg-type]

    params = agent._thread_params()

    assert params["config"] == {"agents": {"enabled": False}}
    assert "developerInstructions" not in params


async def test_adversary_agent_uses_fresh_workspace_write_threads(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turn_params = []
            self.archived = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": f"adv-thread-{len(self.thread_params)}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params.append(params)
            turn_number = len(self.turn_params)
            return {
                "turn": {
                    "id": f"adv-turn-{turn_number}",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": (
                                "candidate_finding: false\n"
                                f"attacked: stack args\nfindings: none\noverall: held {turn_number}"
                            ),
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    started: list[str] = []
    done: list[str] = []
    cleanup_calls: list[tuple[str, Path]] = []
    multi_agent = MultiAgentConfig(enabled=True, max_concurrent=5)

    async def cleanup_descendants(thread_id: str, workspace_root: Path) -> None:
        assert thread_id not in client.archived
        cleanup_calls.append((thread_id, workspace_root))

    agent = AdversaryAgent(
        client,  # type: ignore[arg-type]
        tmp_path,
        model="gpt-adversary",
        intelligence="ultra",
        timeout_seconds=1,
        on_thread_start=started.append,
        on_thread_done=done.append,
        multi_agent=multi_agent,
        before_thread_cleanup=cleanup_descendants,
    )

    first = await agent.run(_packet(tmp_path))
    second = await agent.run(_packet(tmp_path))

    assert first.thread_id == "adv-thread-1"
    assert second.thread_id == "adv-thread-2"
    assert started == ["adv-thread-1", "adv-thread-2"]
    assert done == ["adv-thread-1", "adv-thread-2"]
    assert cleanup_calls == [
        ("adv-thread-1", tmp_path.resolve()),
        ("adv-thread-2", tmp_path.resolve()),
    ]
    assert client.archived == ["adv-thread-1", "adv-thread-2"]
    assert client.thread_params[0]["ephemeral"] is False
    assert client.thread_params[0]["persistExtendedHistory"] is False
    assert client.thread_params[0]["sandbox"] == "workspace-write"
    assert client.thread_params[0]["model"] == "gpt-adversary"
    assert client.thread_params[0]["effort"] == "ultra"
    assert client.thread_params[0]["config"]["agents"] == {
        "enabled": True,
        "max_concurrent_threads_per_session": 5,
        "default_subagent_model": multi_agent.default.model,
        "default_subagent_reasoning_effort": multi_agent.default.intelligence,
        "allowed_profiles": {model: list(efforts) for model, efforts in multi_agent.allowed.items()},
        "role": "adversary",
    }
    developer_instructions = client.thread_params[0]["developerInstructions"]
    assert "distinct attack surfaces, edge-case classes, or failure hypotheses" in developer_instructions
    assert "do not delegate the final judgment or final output" in developer_instructions
    assert client.turn_params[0]["model"] == "gpt-adversary"
    assert client.turn_params[0]["effort"] == "ultra"
    assert client.turn_params[0]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(tmp_path.resolve())],
        "networkAccess": False,
    }
    assert "config" not in client.turn_params[0]
    assert "developerInstructions" not in client.turn_params[0]
    prompt_payload = json.loads(client.turn_params[0]["input"][0]["text"])
    assert prompt_payload["task_contents"].startswith("# Task")
    assert "judged against the task" in prompt_payload["instructions"][1]
    assert "only when the task itself requires that access" in prompt_payload["instructions"][1]
    assert "disposable snapshot" in prompt_payload["instructions"][1]
    assert "accepted_completion_review" not in prompt_payload
    assert first.candidate_finding is False


async def test_adversary_agent_reads_completed_report_from_turns_list(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turns_list_calls: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": "adv-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "adv-turn", "status": "completed", "items": []}}

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            self.turns_list_calls.append(thread_id)
            return {
                "data": [
                    {
                        "id": "adv-turn",
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": (
                                    "candidate_finding: false\n"
                                    "attacked: ephemeral-regression\nfindings: none\noverall: held"
                                ),
                            }
                        ],
                    }
                ]
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert result.thread_id == "adv-thread"
    assert result.turn_id == "adv-turn"
    assert result.report_text.endswith("overall: held")
    assert client.turns_list_calls == ["adv-thread"]
    assert client.archived == ["adv-thread"]
    assert client.thread_params[0]["ephemeral"] is False


@pytest.mark.parametrize("empty_text", [None, "", " \n\t"])
async def test_adversary_agent_retries_once_after_no_message(tmp_path: Path, empty_text: str | None) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "adv-thread-1":
                items = [] if empty_text is None else [{"type": "agentMessage", "text": empty_text}]
                return {"turn": {"id": "adv-turn-1", "status": "completed", "items": items}}
            return {
                "turn": {
                    "id": "adv-turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "candidate_finding: false\nattacked: retry\nfindings: none\noverall: held",
                        }
                    ],
                }
            }

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            return {"data": [{"id": "adv-turn-1", "items": []}]}

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert result.thread_id == "adv-thread-2"
    assert "overall: held" in result.report_text
    assert client.thread_ids == ["adv-thread-1", "adv-thread-2"]
    assert client.archived == ["adv-thread-1", "adv-thread-2"]


async def test_adversary_agent_retry_carries_denied_probes_note(tmp_path: Path) -> None:
    # A denial can abort the whole turn before the agent records not_reached; the retry
    # must tell the fresh thread what was refused so it does not replay the same request.
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.prompts: list[str] = []

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            self.prompts.append(params["input"][0]["text"])
            if params["threadId"] == "adv-thread-1":
                return {"turn": {"id": "adv-turn-1", "status": "completed", "items": []}}
            return {
                "turn": {
                    "id": "adv-turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "candidate_finding: false\nattacked: retry\nfindings: none\noverall: held",
                        }
                    ],
                }
            }

        async def thread_turns_list(self, thread_id, *, limit, items_view, timeout):
            return {"data": [{"id": "adv-turn-1", "items": []}]}

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    denied = ["some-host-binary --flag file.html (denied: needs supervisor judgment)"]
    result = await AdversaryAgent(
        client,  # type: ignore[arg-type]
        tmp_path,
        timeout_seconds=1,
        denied_probes=lambda: list(denied),
    ).run(_packet(tmp_path))

    assert result.thread_id == "adv-thread-2"
    assert "Retry note" not in client.prompts[0]
    assert "some-host-binary --flag file.html" in client.prompts[1]
    assert "record them under not_reached" in client.prompts[1]


async def test_adversary_agent_retries_failed_turn_instead_of_accepting_progress_message(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "adv-thread-1":
                return {"turn": {"id": "adv-turn-1", "status": "inProgress", "items": []}}
            return {
                "turn": {
                    "id": "adv-turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "candidate_finding: false\nattacked: retry\nfindings: none\noverall: held",
                        }
                    ],
                }
            }

        async def wait_for_notification(self, predicate, *, timeout):
            notification = SimpleNamespace(
                method="turn/completed",
                params={
                    "threadId": "adv-thread-1",
                    "turn": {
                        "id": "adv-turn-1",
                        "status": "failed",
                        "error": {
                            "message": "upstream response failed",
                            "codexErrorInfo": "internalServerError",
                        },
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": "I am replaying the previous findings before probing new behavior.",
                            }
                        ],
                    },
                },
            )
            assert predicate(notification)
            return notification

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert result.thread_id == "adv-thread-2"
    assert result.candidate_finding is False
    assert client.thread_ids == ["adv-thread-1", "adv-thread-2"]
    assert client.archived == ["adv-thread-1", "adv-thread-2"]


async def test_adversary_agent_retries_once_after_turn_completion_timeout(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "adv-thread-1":
                return {"turn": {"id": "adv-turn-1", "status": "inProgress", "items": []}}
            return {
                "turn": {
                    "id": "adv-turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "candidate_finding: false\nattacked: retry\nfindings: none\noverall: held",
                        }
                    ],
                }
            }

        async def wait_for_notification(self, predicate, *, timeout):
            raise asyncio.TimeoutError

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert result.thread_id == "adv-thread-2"
    assert client.thread_ids == ["adv-thread-1", "adv-thread-2"]
    assert client.archived == ["adv-thread-1", "adv-thread-2"]


async def test_adversary_agent_retries_terminal_error_notification(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "adv-thread-1":
                return {"turn": {"id": "adv-turn-1", "status": "inProgress", "items": []}}
            return {
                "turn": {
                    "id": "adv-turn-2",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "candidate_finding: false\nattacked: retry\nfindings: none\noverall: held",
                        }
                    ],
                }
            }

        async def wait_for_notification(self, predicate, *, timeout):
            notification = SimpleNamespace(
                method="error",
                params={
                    "threadId": "adv-thread-1",
                    "turnId": "adv-turn-1",
                    "willRetry": False,
                    "error": {
                        "message": "provider stream stalled",
                        "codexErrorInfo": "internalServerError",
                    },
                },
            )
            assert predicate(notification)
            return notification

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert result.thread_id == "adv-thread-2"
    assert client.thread_ids == ["adv-thread-1", "adv-thread-2"]
    assert client.archived == ["adv-thread-1", "adv-thread-2"]


async def test_adversary_agent_leaves_transient_error_to_codex_retry(tmp_path: Path) -> None:
    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "adv-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "adv-turn", "status": "inProgress", "items": []}}

        async def wait_for_notification(self, predicate, *, timeout):
            transient = SimpleNamespace(
                method="error",
                params={
                    "threadId": "adv-thread",
                    "turnId": "adv-turn",
                    "willRetry": True,
                    "error": {"message": "retrying upstream response"},
                },
            )
            assert not predicate(transient)
            completed = SimpleNamespace(
                method="turn/completed",
                params={
                    "threadId": "adv-thread",
                    "turn": {
                        "id": "adv-turn",
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "text": (
                                    "candidate_finding: false\n"
                                    "attacked: built-in retry\nfindings: none\noverall: held"
                                ),
                            }
                        ],
                    },
                },
            )
            assert predicate(completed)
            return completed

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    result = await AdversaryAgent(FakeClient(), tmp_path, timeout_seconds=1).run(  # type: ignore[arg-type]
        _packet(tmp_path)
    )

    assert result.thread_id == "adv-thread"
    assert result.candidate_finding is False


async def test_adversary_agent_surfaces_terminal_error_after_bounded_retry(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": f"adv-thread-{self.thread_count}"}}

        async def turn_start(self, params, *, timeout):
            return {
                "turn": {
                    "id": f"adv-turn-{self.thread_count}",
                    "status": "inProgress",
                    "items": [],
                }
            }

        async def wait_for_notification(self, predicate, *, timeout):
            notification = SimpleNamespace(
                method="error",
                params={
                    "threadId": f"adv-thread-{self.thread_count}",
                    "turnId": f"adv-turn-{self.thread_count}",
                    "willRetry": False,
                    "error": {
                        "message": "provider stream stalled",
                        "codexErrorInfo": "internalServerError",
                    },
                },
            )
            assert predicate(notification)
            return notification

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    with pytest.raises(
        AdversaryAgentError,
        match="provider stream stalled.*codexErrorInfo='internalServerError'",
    ):
        await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert client.thread_count == 2


@pytest.mark.parametrize(
    ("report_text", "candidate_finding"),
    [
        pytest.param(
            "candidate_finding: true\n\n## attacked\nParser\n\n## findings\nInvalid input accepted\n\n## overall\nDefects remain",
            True,
            id="markdown-headings-without-colons",
        ),
        pytest.param(
            "## attacked\nParser\n\n## findings\nInvalid input accepted\n\n## overall\nDefects remain",
            True,
            id="no-routing-line",
        ),
        pytest.param(
            "I am replaying the previous findings before probing new behavior.",
            True,
            id="unstructured-prose",
        ),
        pytest.param("candidate_finding: false\nNo defects found.", False, id="declared-no-findings"),
    ],
)
async def test_adversary_agent_returns_nonempty_report_without_format_retry(
    tmp_path: Path,
    report_text: str,
    candidate_finding: bool,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_ids: list[str] = []
            self.archived: list[str] = []
            self.turn_count = 0

        async def thread_start(self, params, *, timeout):
            thread_id = f"adv-thread-{len(self.thread_ids) + 1}"
            self.thread_ids.append(thread_id)
            return {"thread": {"id": thread_id}}

        async def turn_start(self, params, *, timeout):
            self.turn_count += 1
            return {
                "turn": {
                    "id": "adv-turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": report_text,
                        }
                    ],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    result = await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(  # type: ignore[arg-type]
        _packet(tmp_path)
    )

    assert result.report_text == report_text
    assert result.thread_id == "adv-thread-1"
    assert result.candidate_finding is candidate_finding
    assert client.thread_ids == ["adv-thread-1"]
    assert client.turn_count == 1
    assert client.archived == ["adv-thread-1"]


@pytest.mark.parametrize("terminal_status", ["failed", "interrupted"])
async def test_adversary_agent_surfaces_unsuccessful_turn_after_bounded_retry(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": f"adv-thread-{self.thread_count}"}}

        async def turn_start(self, params, *, timeout):
            return {
                "turn": {
                    "id": f"adv-turn-{self.thread_count}",
                    "status": terminal_status,
                    "error": {"message": "provider overloaded", "codexErrorInfo": "serverOverloaded"},
                    "items": [{"type": "agentMessage", "text": "Starting the audit now."}],
                }
            }

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    client = FakeClient()
    with pytest.raises(AdversaryAgentError, match=f"status='{terminal_status}'.*provider overloaded"):
        await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert client.thread_count == 2


async def test_adversary_agent_does_not_retry_cancellation(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.thread_count = 0
            self.archived: list[str] = []

        async def thread_start(self, params, *, timeout):
            self.thread_count += 1
            return {"thread": {"id": "adv-thread"}}

        async def turn_start(self, params, *, timeout):
            raise asyncio.CancelledError

        async def thread_archive(self, thread_id, *, timeout):
            self.archived.append(thread_id)
            return {}

    client = FakeClient()
    with pytest.raises(asyncio.CancelledError):
        await AdversaryAgent(client, tmp_path, timeout_seconds=1).run(_packet(tmp_path))  # type: ignore[arg-type]

    assert client.thread_count == 1
    assert client.archived == ["adv-thread"]


def test_adversary_candidate_finding_parser_handles_multiline_findings() -> None:
    assert _report_has_candidate_finding("attacked: x\nfindings:\n- crash on input\noverall: broke")
    assert not _report_has_candidate_finding("attacked: x\nfindings:\n- none\nheld: x\noverall: held")
    assert not _report_has_candidate_finding("candidate_finding: false\nfindings:\n- crash-looking note")
