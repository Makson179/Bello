from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from supervisor.approvals import ApprovalManager
from supervisor.appserver import AppServerMessage
from supervisor.controller import (
    SUBAGENT_ACTION_LIMIT,
    SUBAGENT_SUMMARY_LIMIT,
    BelloController,
    SubagentRuntimeState,
)
from supervisor.project_config import (
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_TERRA,
    MultiAgentConfig,
    ProjectConfig,
    SubagentDefaultConfig,
)
from supervisor.schemas import AppEventSource, ApprovalContext, BelloConfig, BelloStatus
from supervisor.state import StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent


class _Tui:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def render(self, role: str, text: str) -> None:
        self.messages.append((role, text))

    def status(self, text: str) -> None:
        self.messages.append(("STATUS", text))


class _Coder:
    def __init__(self, calls: list[tuple[Any, ...]] | None = None) -> None:
        self.thread_id = "coder-root"
        self.active_turn_id: str | None = None
        self.messages: list[str] = []
        self.calls = calls

    async def steer_or_start(self, message: str) -> str:
        self.messages.append(message)
        return "root-turn"

    async def interrupt(self) -> None:
        if self.calls is not None:
            self.calls.append(("root", self.thread_id, self.active_turn_id))


class _Client:
    def __init__(self) -> None:
        self.interrupts: list[tuple[str, str]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []
        self.archived: list[str] = []

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.interrupts.append((thread_id, turn_id))
        return {}

    async def respond(self, request_id: int | str, response: dict[str, Any]) -> None:
        self.responses.append((request_id, response))

    async def thread_archive(self, thread_id: str) -> dict[str, Any]:
        self.archived.append(thread_id)
        return {}

    async def thread_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": []}

    async def thread_turns_list(self, thread_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"data": []}


def _multi_agent_config() -> MultiAgentConfig:
    return MultiAgentConfig(
        enabled=True,
        max_concurrent=4,
        default=SubagentDefaultConfig(MODEL_GPT_5_6_LUNA, "high"),
        allowed={
            MODEL_GPT_5_6_LUNA: ("medium", "high", "xhigh"),
            MODEL_GPT_5_6_TERRA: ("medium", "high"),
        },
    )


def _controller(
    tmp_path: Path,
    *,
    multi_agent: MultiAgentConfig | None = None,
    completion_multi_agent: MultiAgentConfig | None = None,
    adversary_multi_agent: MultiAgentConfig | None = None,
) -> tuple[BelloController, StateStore]:
    task = tmp_path / "TASK.md"
    task.write_text("# Task\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="coder-root",
            generation=2,
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.project_config = ProjectConfig(
        multi_agent=multi_agent or _multi_agent_config(),
        completion_multi_agent=completion_multi_agent or MultiAgentConfig(),
        adversary_multi_agent=adversary_multi_agent or MultiAgentConfig(),
    )
    controller.client = _Client()
    controller.coder = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.tui = _Tui()
    controller._sequence = 0
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._reviewer_thread_ids = {}
    controller._reviewer_thread_roles = {}
    controller._deferred_completion_check = None
    controller._quiescing_coder_tree = False
    controller._terminal_cleanup_started = False
    controller._command_output_chunks = {}
    controller.validations = []
    controller.inspections = []
    controller.validation_runtime_state = {}
    controller.observed_changed_files = {}
    controller.declared_grading_roots = ()
    controller.use_git_diff = False
    return controller, store


async def test_thread_started_tracks_out_of_order_nested_descendants(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "grandchild",
                        "parentThreadId": "child",
                        "status": {"type": "active"},
                        "agentNickname": "nested",
                        "agentRole": "reviewer",
                    }
                },
            }
        )
    )
    assert not controller._is_coder_descendant("grandchild")

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "child",
                        "parentThreadId": "coder-root",
                        "status": {"type": "active"},
                    }
                },
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "grandchild",
                    "turn": {"id": "grand-turn", "status": "inProgress"},
                },
            }
        )
    )

    state = controller._subagents["grandchild"]
    assert controller._is_coder_descendant("grandchild")
    assert controller._subagent_depth("grandchild") == 2
    assert state.active_turn_id == "grand-turn"
    assert state.nickname == "nested"
    assert state.role == "reviewer"
    assert state.profile_allowed is True


async def test_spawn_profiles_allow_default_and_deny_with_root_steering(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    coder = _Coder()
    controller.coder = coder

    async def spawn(
        thread_id: str,
        *,
        model: str | None,
        effort: str | None,
    ) -> SubagentRuntimeState:
        item: dict[str, Any] = {
            "type": "collabAgentToolCall",
            "tool": "spawnAgent",
            "senderThreadId": "coder-root",
            "receiverThreadIds": [thread_id],
            "agentsStates": {thread_id: {"status": "running"}},
            "status": "completed",
            "prompt": f"work for {thread_id}",
        }
        if model is not None:
            item["model"] = model
        if effort is not None:
            item["reasoningEffort"] = effort
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {"threadId": "coder-root", "turnId": "root-turn", "item": item},
                }
            )
        )
        return controller._subagents[thread_id]

    allowed = await spawn("allowed", model=MODEL_GPT_5_6_LUNA, effort="xhigh")
    defaulted = await spawn("defaulted", model=None, effort=None)
    denied = await spawn("denied", model=MODEL_GPT_5_6_TERRA, effort="xhigh")

    assert (allowed.model, allowed.reasoning_effort, allowed.profile_allowed) == (
        MODEL_GPT_5_6_LUNA,
        "xhigh",
        True,
    )
    assert (defaulted.model, defaulted.reasoning_effort, defaulted.profile_allowed) == (
        MODEL_GPT_5_6_LUNA,
        "high",
        True,
    )
    assert denied.profile_allowed is False
    assert len(coder.messages) == 1
    assert "denied" in coder.messages[0]
    assert f"{MODEL_GPT_5_6_TERRA}/xhigh" in coder.messages[0]

    # Once the forbidden child's actual turn id is known, it is stopped without
    # sending a duplicate policy message to the parent.
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "denied",
                    "turn": {"id": "denied-turn", "status": "inProgress"},
                },
            }
        )
    )
    assert controller.client.interrupts == [("denied", "denied-turn")]
    assert len(coder.messages) == 1


async def test_completion_reviewer_subagents_use_own_policy_and_forbid_nested_children(
    tmp_path: Path,
) -> None:
    controller, _store = _controller(
        tmp_path,
        completion_multi_agent=_multi_agent_config(),
    )
    controller._register_reviewer_thread("completion-root", role="completion_review")

    async def spawn(
        sender: str,
        receiver: str,
        *,
        model: str,
        effort: str,
    ) -> None:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": sender,
                        "turnId": f"{sender}-turn",
                        "item": {
                            "type": "collabAgentToolCall",
                            "tool": "spawnAgent",
                            "senderThreadId": sender,
                            "receiverThreadIds": [receiver],
                            "agentsStates": {receiver: {"status": "running"}},
                            "status": "completed",
                            "model": model,
                            "reasoningEffort": effort,
                            "prompt": f"probe {receiver}",
                        },
                    },
                }
            )
        )

    await spawn(
        "completion-root",
        "completion-child",
        model=MODEL_GPT_5_6_LUNA,
        effort="high",
    )
    assert controller._subagents["completion-child"].profile_allowed is True
    assert controller._reviewer_role_for_thread("completion-child") == "completion_review"

    await spawn(
        "completion-child",
        "completion-grandchild",
        model=MODEL_GPT_5_6_LUNA,
        effort="high",
    )
    grandchild = controller._subagents["completion-grandchild"]
    assert controller._reviewer_descendant_depth(grandchild.thread_id) == 2
    assert grandchild.profile_allowed is False

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "turn/started",
                "params": {
                    "threadId": grandchild.thread_id,
                    "turn": {"id": "grandchild-turn", "status": "inProgress"},
                },
            }
        )
    )
    assert controller.client.interrupts == [(grandchild.thread_id, "grandchild-turn")]
    assert controller.coder is None
    assert any("limited to one child level" in text for _role, text in controller.tui.messages)


async def test_adversary_reviewer_subagent_forbidden_profile_is_interrupted_without_coder_steering(
    tmp_path: Path,
) -> None:
    controller, _store = _controller(
        tmp_path,
        adversary_multi_agent=_multi_agent_config(),
    )
    controller._register_reviewer_thread("adversary-root", role="adversary")
    controller._subagents["adversary-child"] = SubagentRuntimeState(
        thread_id="adversary-child",
        parent_thread_id="adversary-root",
        generation=2,
        status="active",
        active_turn_id="adversary-child-turn",
        model=MODEL_GPT_5_6_TERRA,
        reasoning_effort="xhigh",
    )

    await controller._enforce_subagent_profile(controller._subagents["adversary-child"])

    assert controller._subagents["adversary-child"].profile_allowed is False
    assert controller.client.interrupts == [("adversary-child", "adversary-child-turn")]
    assert controller.coder is None
    assert any(
        f"{MODEL_GPT_5_6_TERRA}/xhigh" in text
        for _role, text in controller.tui.messages
    )


def test_reviewer_descendant_events_do_not_invalidate_readiness_snapshot(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    controller._register_reviewer_thread("completion-root", role="completion_review")
    controller._subagents["completion-child"] = SubagentRuntimeState(
        thread_id="completion-child",
        parent_thread_id="completion-root",
        generation=2,
    )
    packet = SimpleNamespace(latest_event_sequence=0)

    controller._append_event(
        AppEventSource.APP_SERVER,
        "item/completed",
        thread_id="completion-child",
    )
    cfg = store.get_bello_config()

    assert controller._readiness_snapshot_has_new_invalidating_event(packet, cfg=cfg) is False

    next_packet = SimpleNamespace(latest_event_sequence=cfg.last_event_sequence)
    controller._append_event(
        AppEventSource.APP_SERVER,
        "item/completed",
        thread_id="unknown-thread",
    )
    cfg = store.get_bello_config()
    assert controller._readiness_snapshot_has_new_invalidating_event(next_packet, cfg=cfg) is True


def test_adversary_child_approval_context_uses_adversary_ancestry(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    controller._register_reviewer_thread("adversary-root", role="adversary")
    controller._subagents["adversary-child"] = SubagentRuntimeState(
        thread_id="adversary-child",
        parent_thread_id="adversary-root",
        generation=2,
    )
    context = ApprovalContext(
        server_request_id=9,
        server_request_method="item/commandExecution/requestApproval",
        thread_id="adversary-child",
        turn_id="child-turn",
        command="pytest -q",
    )

    assert controller._is_adversary_approval_context(context) is True

    controller._active_adversary_thread_id = "adversary-root"
    early_child_context = context.model_copy(update={"thread_id": "not-yet-tracked-child"})
    coder_context = context.model_copy(update={"thread_id": "coder-root"})
    assert controller._is_adversary_approval_context(early_child_context) is True
    assert controller._is_adversary_approval_context(coder_context) is False


async def test_reviewer_cleanup_interrupts_and_archives_descendants_before_snapshot_removal(
    tmp_path: Path,
) -> None:
    controller, _store = _controller(tmp_path)
    controller._register_reviewer_thread("completion-root", role="completion_review")
    controller._subagents["completion-child"] = SubagentRuntimeState(
        thread_id="completion-child",
        parent_thread_id="completion-root",
        generation=2,
        status="active",
        active_turn_id="completion-child-turn",
    )
    controller._subagents["completion-grandchild"] = SubagentRuntimeState(
        thread_id="completion-grandchild",
        parent_thread_id="completion-child",
        generation=2,
        status="completed",
    )

    await controller._cleanup_completion_reviewer_descendants("completion-root", tmp_path)

    assert controller.client.interrupts == [("completion-child", "completion-child-turn")]
    assert controller.client.archived == ["completion-grandchild", "completion-child"]
    assert controller._subagents["completion-child"].status == "shutdown"
    assert controller._subagents["completion-grandchild"].status == "shutdown"


def test_model_preflight_includes_only_enabled_active_stage_subagent_models(tmp_path: Path) -> None:
    coder = MultiAgentConfig(
        enabled=True,
        default=SubagentDefaultConfig(MODEL_GPT_5_6_LUNA, "high"),
        allowed={MODEL_GPT_5_6_LUNA: ("high",)},
    )
    completion = MultiAgentConfig(
        enabled=True,
        default=SubagentDefaultConfig(MODEL_GPT_5_6_TERRA, "high"),
        allowed={MODEL_GPT_5_6_TERRA: ("high",)},
    )
    adversary = MultiAgentConfig(
        enabled=True,
        default=SubagentDefaultConfig("gpt-5.6-sol", "high"),
        allowed={"gpt-5.6-sol": ("high",)},
    )
    controller, store = _controller(
        tmp_path,
        multi_agent=coder,
        completion_multi_agent=completion,
        adversary_multi_agent=adversary,
    )

    assert controller._enabled_subagent_models_for_preflight() == (
        MODEL_GPT_5_6_LUNA,
        MODEL_GPT_5_6_TERRA,
        "gpt-5.6-sol",
    )

    store.update_bello_config(
        lambda cfg: cfg.model_copy(update={"completion_review_enabled": False})
    )
    assert controller._enabled_subagent_models_for_preflight() == (MODEL_GPT_5_6_LUNA,)


def test_runtime_packet_subagent_summaries_are_bounded(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    long_text = "x" * 2_000
    for child_index in range(SUBAGENT_SUMMARY_LIMIT + 4):
        state = SubagentRuntimeState(
            thread_id=f"child-{child_index}",
            parent_thread_id="coder-root",
            generation=2,
            status="running",
            active_turn_id=f"turn-{child_index}",
            prompt=long_text,
            last_message=long_text,
            last_sequence=child_index + 1,
        )
        for action_index in range(SUBAGENT_ACTION_LIMIT + 3):
            state.record_action(
                long_text,
                sequence=action_index + 1,
                kind="commandExecution",
                item_id=f"item-{child_index}-{action_index}",
            )
        controller._subagents[state.thread_id] = state

    packet = StatelessSupervisorAgent(None, store, controller.task_path).build_packet(  # type: ignore[arg-type]
        wake_sequence=100,
        current_summary="runtime observation",
        subagents=controller._subagent_summaries(),
    )
    summaries = packet.subagents

    assert len(summaries) == SUBAGENT_SUMMARY_LIMIT
    assert all(len(summary.recent_actions) == SUBAGENT_ACTION_LIMIT for summary in summaries)
    assert all(len(summary.prompt or "") <= 600 for summary in summaries)
    assert all(len(summary.last_message or "") <= 600 for summary in summaries)
    assert all(
        len(action.summary) <= 400
        for summary in summaries
        for action in summary.recent_actions
    )


async def test_child_validation_enters_shared_ledger_without_runtime_wake(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    controller._subagents["child"] = SubagentRuntimeState(
        thread_id="child",
        parent_thread_id="coder-root",
        generation=2,
        status="running",
        active_turn_id="child-turn",
    )
    controller._sequence = 11
    controller._repair_snapshot_runtime_controls = lambda **_kwargs: ()  # type: ignore[method-assign]

    async def no_integrity_issue(**_kwargs: Any) -> bool:
        return False

    controller._escalate_runtime_integrity_issue = no_integrity_issue  # type: ignore[method-assign]
    controller._declared_grading_access_issue = lambda _action: None  # type: ignore[method-assign]

    def unexpected_wake(**_kwargs: Any) -> None:
        pytest.fail("a child action must not invoke the runtime wake classifier")

    controller.should_wake_runtime_supervisor = unexpected_wake  # type: ignore[method-assign]

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "child",
                    "turnId": "child-turn",
                    "itemId": "child-pytest",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_widget.py",
                        "exitCode": 0,
                        "status": "completed",
                        "aggregatedOutput": "tests/test_widget.py::test_widget PASSED\n1 passed\n",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "pytest tests/test_widget.py"
    assert validation.passed is True
    child = controller._subagents["child"]
    assert child.validation_ids == [validation.validation_id]
    assert child.recent_actions[-1][3] == "child-pytest"


async def test_child_approval_denial_is_explained_to_root_parent(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    coder = _Coder()
    controller.coder = coder
    controller.approvals = ApprovalManager(tmp_path)
    controller._subagents["child"] = SubagentRuntimeState(
        thread_id="child",
        parent_thread_id="coder-root",
        generation=2,
        status="running",
    )

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 51,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "threadId": "child",
                    "turnId": "child-turn",
                    "grantRoot": str(tmp_path / ".supervisor" / "CONFIG.json"),
                    "availableDecisions": ["accept", "decline"],
                },
            }
        )
    )

    assert controller.client.responses == [(51, {"decision": "decline"})]
    assert len(coder.messages) == 1
    assert "Approval for subagent child was denied" in coder.messages[0]
    assert "steer or stop that child" in coder.messages[0]


async def test_completion_review_is_deferred_until_descendants_are_idle(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)

    class DescendantClient(_Client):
        async def thread_list(self, params: dict[str, Any]) -> dict[str, Any]:
            assert params["cwd"] == str(tmp_path)
            return {
                "data": [
                    {
                        "id": "child",
                        "parentThreadId": "coder-root",
                        "status": {"type": "active"},
                    }
                ],
                "nextCursor": None,
            }

        async def thread_turns_list(self, thread_id: str, **_kwargs: Any) -> dict[str, Any]:
            assert thread_id == "child"
            return {"data": [{"id": "child-turn", "status": "inProgress"}]}

    controller.client = DescendantClient()
    scheduled: list[tuple[str, dict[str, Any]]] = []
    controller._schedule_supervisor_check = (  # type: ignore[method-assign]
        lambda summary, **kwargs: scheduled.append((summary, kwargs))
    )

    await controller._run_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        "ready-item",
        None,
        None,
        None,
        True,
    )

    assert scheduled == []
    assert controller._deferred_completion_check is not None
    assert controller._deferred_completion_check.completion_review is True

    child = controller._subagents["child"]
    child.status = "completed"
    child.active_turn_id = None
    await controller._resume_deferred_completion_if_quiescent()

    assert controller._deferred_completion_check is None
    assert scheduled == [
        (
            "Coder provided exact readiness marker; running completion_review.",
            {
                "triggering_item_id": "ready-item",
                "triggering_action": None,
                "human_message": None,
                "patch_summary": None,
                "completion_review": True,
            },
        )
    ]


@dataclass
class _TreeNode:
    parent: str
    active: bool = True


@pytest.mark.parametrize("reason", ["pause", "restart"])
async def test_pause_and_restart_cleanup_requery_and_interrupt_new_descendants(
    tmp_path: Path,
    reason: str,
) -> None:
    controller, _store = _controller(tmp_path)
    calls: list[tuple[Any, ...]] = []
    controller.coder = _Coder(calls)

    class GrowingTreeClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.nodes: dict[str, _TreeNode] = {"child-1": _TreeNode("coder-root")}
            self.next_child = 2
            self.list_calls = 0

        async def thread_list(self, _params: dict[str, Any]) -> dict[str, Any]:
            self.list_calls += 1
            return {
                "data": [
                    {
                        "id": thread_id,
                        "parentThreadId": node.parent,
                        "status": {"type": "active" if node.active else "idle"},
                    }
                    for thread_id, node in self.nodes.items()
                ],
                "nextCursor": None,
            }

        async def thread_turns_list(self, thread_id: str, **_kwargs: Any) -> dict[str, Any]:
            return {"data": [{"id": f"turn-{thread_id}", "status": "inProgress"}]}

        async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
            calls.append(("child", thread_id, turn_id))
            self.nodes[thread_id].active = False
            # Model the exact race cleanup must cover: the interrupted child spawned
            # another descendant after the previous authoritative list response.
            if self.next_child <= 3:
                new_id = f"child-{self.next_child}"
                self.nodes[new_id] = _TreeNode(thread_id)
                self.next_child += 1
            return {}

    client = GrowingTreeClient()
    controller.client = client

    assert await controller._quiesce_coder_tree(reason)
    assert calls == [
        ("root", "coder-root", None),
        ("child", "child-1", "turn-child-1"),
        ("child", "child-2", "turn-child-2"),
        ("child", "child-3", "turn-child-3"),
    ]
    # The last successful interruption pass is followed by an authoritative
    # empty-tree read; checking only controller-mutated in-memory state races.
    assert client.list_calls == 4
    assert controller._active_coder_subagents() == []
