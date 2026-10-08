"""Controller revision lifecycle regression tests."""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
import pytest
from supervisor.controller import BelloController, _selected_model_availability
from supervisor.appserver import AppServerError
from supervisor.coder import CODEX_FAST_SERVICE_TIER, CoderSession
from supervisor.project_config import MODEL_GPT_5_6_LUNA, MODEL_GPT_5_6_SOL, MultiAgentConfig, ProjectConfig
from supervisor.schemas import CoderMessage, CompletionReviewDecision, BelloConfig, BelloStatus, ValidationRun
from supervisor.state import EVENTS, HANDOFF, LOG, StateStore
from supervisor.workspace_snapshot import create_workspace_snapshot

from tests.support.controller import (
    _FakeTUI,
)


@pytest.mark.parametrize(
    ("first_source", "first_completion_return_count"),
    [
        ("completion_review", 1),
        ("adversary_report_controller", 0),
    ],
)
async def test_review_returns_switch_once_and_reuse_revision_coder_thread(
    tmp_path: Path,
    first_source: str,
    first_completion_return_count: int,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    plan = tmp_path / "PLAN.md"
    plan.write_text("PRIVATE INITIAL IMPLEMENTATION PLAN\n", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            completion_review_enabled=True,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="xhigh",
        ),
        overwrite=True,
    )
    snapshot = create_workspace_snapshot(tmp_path, task, plan_path=plan)

    class RecordingClient:
        def __init__(self) -> None:
            self.thread_starts = []
            self.turn_starts = []
            self.turn_steers = []

        async def thread_start(self, params, *, timeout):
            self.thread_starts.append(params)
            return {"thread": {"id": f"revision-thread-{len(self.thread_starts)}"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts.append(params)
            return {"turn": {"id": f"revision-turn-{len(self.turn_starts)}"}}

        async def turn_steer(self, thread_id, turn_id, message, *, timeout):
            self.turn_steers.append((thread_id, turn_id, message))
            return {}

    multi_agent = MultiAgentConfig(enabled=True)
    project_config = ProjectConfig(
        coder_mod=MODEL_GPT_5_6_SOL,
        coder_intelligence="ultra",
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="xhigh",
        completion_review=True,
        multi_agent=multi_agent,
    )
    client = RecordingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        snapshot.snapshot_root,
        snapshot.task_path,
        model=MODEL_GPT_5_6_SOL,
        intelligence="ultra",
        thread_id="initial-thread",
        multi_agent=multi_agent,
        plan_path=snapshot.plan_path,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.plan_path = plan
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = project_config
    controller.fast = True
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_restarts = 0
    controller.completion_review_return_sequence = None
    controller.validations = [
        ValidationRun(command="pytest", exit_code=0, passed=True, summary="1 passed", sequence=2)
    ]
    original_validations = controller.validations
    pending_report = object()
    controller._pending_adversary_report = pending_report
    controller.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=3)
    controller._last_completion_marker_sequence = 3
    controller._no_marker_completion_review_key = "old"
    controller._deferred_completion_check = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_snapshot = snapshot
    controller._sequence = 3

    subprocess.run(
        ["git", "add", "-f", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        check=True,
    )
    staged_plan_blob = subprocess.run(
        ["git", "ls-files", "--stage", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.split()[1]

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    first_feedback = "Validate the missing-key fallback before reporting readiness again."
    first_decision = CompletionReviewDecision(
        decision="return",
        reason="fallback behavior is uncovered",
        uncovered_behaviors=["missing-key fallback"],
        validation_gaps=["only the happy path was validated"],
        message_to_coder=first_feedback,
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=4,
        generation=0,
    )

    await controller._return_completion_to_coder(first_decision, source=first_source)  # type: ignore[arg-type]

    config_after_switch = store.get_bello_config()
    first_prompt = client.turn_starts[0]["input"][0]["text"]
    assert len(client.thread_starts) == 1
    assert client.thread_starts[0]["model"] == MODEL_GPT_5_6_LUNA
    assert client.thread_starts[0]["serviceTier"] == CODEX_FAST_SERVICE_TIER
    assert client.thread_starts[0]["config"]["agents"]["enabled"] is True
    assert client.turn_starts[0]["threadId"] == "revision-thread-1"
    assert client.turn_starts[0]["model"] == MODEL_GPT_5_6_LUNA
    assert client.turn_starts[0]["effort"] == "xhigh"
    assert first_feedback in first_prompt
    assert ".supervisor/HANDOFF.md" in first_prompt
    assert ".supervisor/DECISIONS.md" in first_prompt
    assert ".supervisor/PROGRESS.md" in first_prompt
    assert first_prompt.count("BELLO_READY_FOR_REVIEW") == 1
    assert "on its own line" in first_prompt
    assert str(snapshot.plan_path) not in first_prompt
    assert "PRIVATE INITIAL IMPLEMENTATION PLAN" not in first_prompt
    assert snapshot.plan_exposed is False
    assert not snapshot.plan_path.exists()
    assert not snapshot.plan_path.is_symlink()
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "PLAN.md"],
        cwd=snapshot.snapshot_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).returncode != 0
    assert subprocess.run(
        ["git", "cat-file", "-e", staged_plan_blob],
        cwd=snapshot.snapshot_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).returncode != 0
    assert controller.workspace_plan_path is None
    assert config_after_switch.coder_thread_id == "revision-thread-1"
    assert config_after_switch.revision_coder_active is True
    assert config_after_switch.generation == 0
    assert config_after_switch.restart_count == 0
    assert store.get_health().restart_count == 0
    assert controller.completion_restarts == 0
    assert store.path(HANDOFF).read_text(encoding="utf-8") == ""
    assert controller.validations is original_validations
    assert controller._pending_adversary_report is pending_report
    assert store.get_bello_config().completion_return_count == first_completion_return_count

    assert controller.coder is not None
    controller.coder.mark_turn_completed("revision-turn-1")
    second_feedback = "Investigate and correct the confirmed seven-argument crash."
    second_decision = CompletionReviewDecision(
        decision="return",
        reason="adversary confirmed a crash",
        uncovered_behaviors=["seven-argument invocation must not crash"],
        message_to_coder=second_feedback,
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=5,
        generation=0,
    )

    second_source = (
        "adversary_report_controller"
        if first_source == "completion_review"
        else "completion_review"
    )
    await controller._return_completion_to_coder(second_decision, source=second_source)  # type: ignore[arg-type]

    assert len(client.thread_starts) == 1
    assert len(client.turn_starts) == 2
    assert client.turn_starts[1]["threadId"] == "revision-thread-1"
    assert client.turn_starts[1]["input"][0]["text"] == second_feedback
    assert client.turn_steers == []
    assert store.get_bello_config().completion_return_count == 1
    events = [json.loads(line) for line in store.path(EVENTS).read_text(encoding="utf-8").splitlines()]
    switches = [event for event in events if event["event_type"] == "coder/profile_switch"]
    assert len(switches) == 1
    assert switches[0]["payload"]["previous_thread_id"] == "initial-thread"
    assert switches[0]["payload"]["revision_thread_id"] == "revision-thread-1"
    assert switches[0]["payload"]["source"] == first_source
    snapshot.cleanup()


async def test_revision_switch_waits_for_pending_initial_coder_delivery(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.old_turn_requested = asyncio.Event()
            self.release_old_turn = asyncio.Event()
            self.events = []

        async def turn_start(self, params, *, timeout):
            if params["threadId"] == "initial-thread":
                self.events.append("old-turn-requested")
                self.old_turn_requested.set()
                await self.release_old_turn.wait()
                self.events.append("old-turn-returned")
                return {"turn": {"id": "old-turn"}}
            self.events.append("revision-turn-started")
            return {"turn": {"id": "revision-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.events.append(f"interrupted:{thread_id}:{turn_id}")
            return {}

        async def thread_start(self, params, *, timeout):
            self.events.append("revision-thread-started")
            return {"thread": {"id": "revision-thread"}}

    client = RacingClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.workspace_root = tmp_path
    controller.workspace_task_path = task
    controller.store = store
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._coder_activity_mutex = None
    controller._coder_quiesce_mutex = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_snapshot = None
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="review found an edge case",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the reviewed edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Runtime feedback."))
    await client.old_turn_requested.wait()
    switch_task = asyncio.create_task(controller._return_completion_to_coder(decision))
    await asyncio.sleep(0)

    assert "revision-thread-started" not in client.events

    client.release_old_turn.set()
    delivered, turn_id = await delivery_task
    await switch_task

    assert delivered is True
    assert turn_id == "old-turn"
    assert client.events.index("old-turn-returned") < client.events.index(
        "interrupted:initial-thread:old-turn"
    )
    assert client.events.index("interrupted:initial-thread:old-turn") < client.events.index(
        "revision-thread-started"
    )
    runtime_config = store.get_bello_config()
    assert runtime_config.revision_coder_active is True
    assert runtime_config.coder_thread_id == "revision-thread"
    assert runtime_config.active_coder_turn_id == "revision-turn"


def test_selected_model_availability_includes_revision_coder_role() -> None:
    result = _selected_model_availability(
        {"data": [{"id": MODEL_GPT_5_6_SOL}]},
        coder_model=MODEL_GPT_5_6_SOL,
        runtime_model=MODEL_GPT_5_6_SOL,
        completion_model=MODEL_GPT_5_6_SOL,
        revision_coder_model=MODEL_GPT_5_6_LUNA,
    )

    assert result.missing_roles == (f"revision-coder={MODEL_GPT_5_6_LUNA}",)


@pytest.mark.parametrize("pause_stage", ["thread", "turn"])
async def test_revision_switch_cannot_overwrite_a_concurrent_pause(
    tmp_path: Path,
    pause_stage: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )
    plan = tmp_path / "PLAN.md"
    plan.write_text("PRIVATE INITIAL PLAN\n", encoding="utf-8")
    snapshot = create_workspace_snapshot(tmp_path, task, plan_path=plan)

    class RacingClient:
        def __init__(self) -> None:
            self.request_started = asyncio.Event()
            self.allow_response = asyncio.Event()
            self.turn_starts = 0
            self.unsubscribed = []
            self.interrupted = []

        async def thread_start(self, params, *, timeout):
            if pause_stage == "thread":
                self.request_started.set()
                await self.allow_response.wait()
            return {"thread": {"id": "revision-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts += 1
            if pause_stage == "turn":
                self.request_started.set()
                await self.allow_response.wait()
            return {"turn": {"id": "revision-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.interrupted.append((thread_id, turn_id))
            return {}

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = RacingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        snapshot.snapshot_root,
        snapshot.task_path,
        thread_id="initial-thread",
        plan_path=snapshot.plan_path,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.plan_path = plan
    controller.workspace_root = snapshot.snapshot_root
    controller.workspace_task_path = snapshot.task_path
    controller.workspace_plan_path = snapshot.plan_path
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._revision_switch_in_progress = False
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_snapshot = snapshot
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="one defect remains",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the remaining edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    return_task = asyncio.create_task(controller._return_completion_to_coder(decision))
    controller._supervisor_task = return_task
    await client.request_started.wait()
    pause_task = asyncio.create_task(controller.pause())
    await asyncio.sleep(0)
    assert controller.paused is True
    assert return_task.cancelled() is False
    client.allow_response.set()
    await pause_task
    await asyncio.gather(return_task, return_exceptions=True)

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.active_coder_turn_id is None
    assert runtime_config.generation == 0
    assert runtime_config.restart_count == 0
    if pause_stage == "thread":
        assert controller.coder is initial_coder
        assert runtime_config.coder_thread_id == "initial-thread"
        assert runtime_config.revision_coder_active is False
        assert snapshot.plan_exposed is True
        assert snapshot.plan_path is not None and snapshot.plan_path.exists()
        assert controller.workspace_plan_path == snapshot.plan_path
        assert client.turn_starts == 0
        assert client.unsubscribed == ["revision-thread"]
        assert client.interrupted == []
    else:
        assert controller.coder is not initial_coder
        assert runtime_config.coder_thread_id == "revision-thread"
        assert runtime_config.revision_coder_active is True
        assert snapshot.plan_exposed is False
        assert snapshot.plan_path is not None and not snapshot.plan_path.exists()
        assert controller.workspace_plan_path is None
        assert client.turn_starts == 1
        assert client.unsubscribed == []
        assert client.interrupted == [("revision-thread", "revision-turn")]
    assert "revision_coder_switch_cancelled" in store.path(LOG).read_text(encoding="utf-8")
    snapshot.cleanup()


async def test_concurrent_coder_quiesce_waits_for_the_inflight_cleanup(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread"),
        overwrite=True,
    )

    class BlockingCoder:
        def __init__(self) -> None:
            self.thread_id = "thread"
            self.active_turn_id = "turn"
            self.calls = 0
            self.first_call_started = asyncio.Event()
            self.release_first_call = asyncio.Event()

        async def interrupt(self) -> None:
            self.calls += 1
            if self.calls == 1:
                self.first_call_started.set()
                await self.release_first_call.wait()

    coder = BlockingCoder()
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.coder = coder
    controller._subagents = {}
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    first = asyncio.create_task(controller._quiesce_coder_tree("first"))
    await coder.first_call_started.wait()
    second = asyncio.create_task(controller._quiesce_coder_tree("second"))
    await asyncio.sleep(0)

    assert second.done() is False
    assert coder.calls == 1

    coder.release_first_call.set()
    assert await first is True
    assert await second is True
    assert coder.calls == 2


async def test_failed_stale_revision_interrupt_keeps_turn_id_for_lifecycle_retry(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task)),
        overwrite=True,
    )

    class FailingInterruptClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            raise AppServerError("interrupt failed")

    coder = CoderSession(
        FailingInterruptClient(),  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="revision-thread",
        active_turn_id="revision-turn",
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    with pytest.raises(AppServerError, match="interrupt failed"):
        await controller._interrupt_stale_revision_turn(
            coder,
            reason="concurrent pause",
        )

    assert coder.active_turn_id == "revision-turn"
    assert "stale_revision_turn" in store.path(LOG).read_text(encoding="utf-8")


@pytest.mark.parametrize("failure_stage", ["prepare", "thread", "turn"])
async def test_revision_coder_start_failure_finalizes_as_provider_failure(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            active_coder_turn_id="initial-turn" if failure_stage == "prepare" else None,
            status=BelloStatus.RUNNING,
            revision_coder_enabled=True,
            revision_coder_mod=MODEL_GPT_5_6_LUNA,
            revision_coder_intelligence="high",
        ),
        overwrite=True,
    )

    class FailingClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            if failure_stage == "prepare":
                raise AppServerError("initial coder interrupt unavailable")
            return {}

        async def thread_start(self, params, *, timeout):
            if failure_stage == "thread":
                raise AppServerError("thread/start unavailable")
            return {"thread": {"id": "revision-thread"}}

        async def turn_start(self, params, *, timeout):
            raise AppServerError("turn/start unavailable")

    client = FailingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
        active_turn_id="initial-turn" if failure_stage == "prepare" else None,
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.workspace_root = tmp_path
    controller.workspace_task_path = task
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.project_config = ProjectConfig(
        revision_coder_enabled=True,
        revision_coder_mod=MODEL_GPT_5_6_LUNA,
        revision_coder_intelligence="high",
    )
    controller.fast = False
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.prior_interventions = []
    controller.completion_returns = []
    controller.completion_review_return_sequence = None
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_snapshot = None
    controller._sequence = 0

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    finalized = []

    async def record_finalize(
        result: str,
        *,
        status: BelloStatus,
        completion_review_accepted: bool | None = False,
    ) -> None:
        finalized.append((result, status, completion_review_accepted))
        controller.running = False
        store.update_bello_config(lambda current: current.model_copy(update={"status": status}))

    controller.finalize = record_finalize  # type: ignore[method-assign]
    decision = CompletionReviewDecision(
        decision="return",
        reason="one defect remains",
        uncovered_behaviors=["edge case"],
        message_to_coder="Fix the remaining edge case.",
        persistent_decision=None,
        progress_update=None,
        clear_handoff=False,
        display_message=None,
        handoff=None,
        wake_sequence=1,
        generation=0,
    )

    await controller._return_completion_to_coder(decision)

    assert len(finalized) == 1
    assert finalized[0][1] == BelloStatus.PROVIDER_FAILURE
    expected_stage = "prepare" if failure_stage == "prepare" else f"{failure_stage}/start"
    assert f"{expected_stage} failed" in finalized[0][0]
    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PROVIDER_FAILURE
    assert runtime_config.completion_return_count == 1
    assert "revision_coder_switch_failed" in store.path(LOG).read_text(encoding="utf-8")
    if failure_stage in {"prepare", "thread"}:
        assert controller.coder is initial_coder
        assert runtime_config.coder_thread_id == "initial-thread"
        assert runtime_config.revision_coder_active is False
    else:
        assert controller.coder is not initial_coder
        assert runtime_config.coder_thread_id == "revision-thread"
        assert runtime_config.revision_coder_active is True
        assert runtime_config.active_coder_turn_id is None
