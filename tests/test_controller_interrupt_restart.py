"""Controller interrupt restart regression tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from supervisor.controller import BelloController
from supervisor.appserver import AppServerMessage
from supervisor.coder import CoderSession
from supervisor.project_config import DEFAULT_MODEL
from supervisor.schemas import BelloConfig, BelloStatus
from supervisor.state import LOG, StateStore

from tests.support.controller import (
    _FakeTUI,
    _runtime_controller,
)


async def test_late_root_turn_started_is_interrupted_without_resurrecting_paused_state(
    tmp_path: Path,
) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class LateTurnClient:
        def __init__(self) -> None:
            self.interrupted = []

        async def turn_interrupt(self, thread_id, turn_id):
            self.interrupted.append((thread_id, turn_id))
            return {}

    client = LateTurnClient()
    coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        controller.task_path,
        thread_id="thread",
    )
    controller.client = client
    controller.coder = coder
    controller.paused = True
    controller._generation_has_coder_turn = False
    store.update_bello_config(
        lambda current: current.model_copy(
            update={"status": BelloStatus.PAUSED, "active_coder_turn_id": None}
        )
    )

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "turn/started",
                "params": {"threadId": "thread", "turnId": "late-turn"},
            }
        )
    )

    assert client.interrupted == [("thread", "late-turn")]
    assert coder.active_turn_id is None
    assert store.get_bello_config().active_coder_turn_id is None
    assert controller._generation_has_coder_turn is False
    assert "late_coder_turn_rejected" in store.path(LOG).read_text(encoding="utf-8")


async def test_stale_revision_interrupt_clears_matching_persisted_turn(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="revision-thread",
            active_coder_turn_id="revision-turn",
        ),
        overwrite=True,
    )

    class InterruptClient:
        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            return {}

    coder = CoderSession(
        InterruptClient(),  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="revision-thread",
        active_turn_id="revision-turn",
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store

    await controller._interrupt_stale_revision_turn(coder, reason="concurrent pause")

    assert coder.active_turn_id is None
    assert store.get_bello_config().active_coder_turn_id is None


async def test_restart_waits_for_pending_coder_turn_start_then_interrupts_old_turn(
    tmp_path: Path,
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
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.old_turn_requested = asyncio.Event()
            self.release_old_turn = asyncio.Event()
            self.events = []

        async def turn_start(self, params, *, timeout):
            thread_id = params["threadId"]
            if thread_id == "initial-thread":
                self.events.append("old-turn-requested")
                self.old_turn_requested.set()
                await self.release_old_turn.wait()
                self.events.append("old-turn-returned")
                return {"turn": {"id": "old-turn"}}
            self.events.append("restart-turn-started")
            return {"turn": {"id": "restart-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.events.append(f"interrupted:{thread_id}:{turn_id}")
            return {}

        async def thread_start(self, params, *, timeout):
            self.events.append("restart-thread-started")
            return {"thread": {"id": "restart-thread"}}

    client = RacingClient()
    initial_coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.coder = initial_coder
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Apply runtime feedback."))
    await client.old_turn_requested.wait()
    restart_task = asyncio.create_task(controller.restart("user requested restart"))
    await asyncio.sleep(0)

    assert restart_task.done() is False
    assert store.get_bello_config().status == BelloStatus.RESTARTING
    assert store.get_bello_config().generation == 0

    client.release_old_turn.set()
    delivered, turn_id = await delivery_task
    await restart_task

    assert delivered is False
    assert turn_id == "old-turn"
    assert client.events.index("old-turn-returned") < client.events.index(
        "interrupted:initial-thread:old-turn"
    )
    assert client.events.index("interrupted:initial-thread:old-turn") < client.events.index(
        "restart-thread-started"
    )
    runtime_config = store.get_bello_config()
    assert runtime_config.generation == 1
    assert runtime_config.coder_thread_id == "restart-thread"
    assert runtime_config.active_coder_turn_id == "restart-turn"
    assert runtime_config.status == BelloStatus.RUNNING


async def test_pause_supersedes_restart_during_new_thread_start(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="initial-thread",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class RacingClient:
        def __init__(self) -> None:
            self.thread_start_requested = asyncio.Event()
            self.release_thread_start = asyncio.Event()
            self.turn_starts = 0

        async def thread_start(self, params, *, timeout):
            self.thread_start_requested.set()
            await self.release_thread_start.wait()
            return {"thread": {"id": "restart-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_starts += 1
            return {"turn": {"id": "restart-turn"}}

    client = RacingClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="initial-thread",
    )
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._quiescing_coder_tree = False
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]

    restart_task = asyncio.create_task(controller.restart("runtime restart"))
    await client.thread_start_requested.wait()
    pause_task = asyncio.create_task(controller.pause())
    await asyncio.sleep(0)

    assert controller.paused is True
    assert store.get_bello_config().status == BelloStatus.PAUSED
    assert pause_task.done() is False

    client.release_thread_start.set()
    await restart_task
    await pause_task

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.coder_thread_id == "restart-thread"
    assert runtime_config.active_coder_turn_id is None
    assert client.turn_starts == 0


async def test_pause_does_not_cancel_an_inflight_terminal_finalize(tmp_path: Path) -> None:
    controller, store, _fake = _runtime_controller(tmp_path)

    class RacingClient:
        def __init__(self) -> None:
            self.turn_start_requested = asyncio.Event()
            self.release_turn_start = asyncio.Event()
            self.interrupted = []
            self.stopped = False

        async def turn_start(self, params, *, timeout):
            self.turn_start_requested.set()
            await self.release_turn_start.wait()
            return {"turn": {"id": "pending-turn"}}

        async def turn_interrupt(self, thread_id, turn_id, *, timeout):
            self.interrupted.append((thread_id, turn_id))
            return {}

        async def stop(self):
            self.stopped = True

    client = RacingClient()
    store.update_bello_config(
        lambda current: current.model_copy(update={"status": BelloStatus.RUNNING})
    )
    controller.client = client
    controller.coder = CoderSession(
        client,  # type: ignore[arg-type]
        store,
        tmp_path,
        controller.task_path,
        thread_id="thread",
    )
    controller._finalizing = False
    controller._coder_activity_mutex = None
    controller._coder_quiesce_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._subagents = {}

    delivery_task = asyncio.create_task(controller._deliver_coder_message("Pending feedback."))
    await client.turn_start_requested.wait()
    finalize_task = asyncio.create_task(
        controller.finalize("terminal completion", status=BelloStatus.COMPLETE)
    )
    await asyncio.sleep(0)
    assert controller._finalizing is True

    await controller.pause()
    assert controller.paused is False
    assert finalize_task.done() is False

    client.release_turn_start.set()
    delivered, _ = await delivery_task
    await finalize_task

    assert delivered is False
    assert client.interrupted == [("thread", "pending-turn")]
    assert client.stopped is True
    assert store.get_bello_config().status == BelloStatus.COMPLETE


@pytest.mark.parametrize(
    ("revision_active", "expected_model", "expected_intelligence"),
    [
        (False, "gpt-coder", "high"),
        (True, "gpt-revision", "medium"),
    ],
)
async def test_restart_preserves_active_coder_profile(
    tmp_path: Path,
    revision_active: bool,
    expected_model: str,
    expected_intelligence: str,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            revision_coder_enabled=True,
            revision_coder_mod="gpt-revision",
            revision_coder_intelligence="medium",
            revision_coder_active=revision_active,
        ),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.thread_params = []
            self.turn_params = []

        async def thread_start(self, params, *, timeout):
            self.thread_params.append(params)
            return {"thread": {"id": "restart-thread"}}

        async def turn_start(self, params, *, timeout):
            self.turn_params.append(params)
            return {"turn": {"id": "restart-turn", "status": "completed"}}

    client = FakeClient()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = client
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.approvals = None
    controller.coder = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller._sequence = 0
    controller.coder_model = "gpt-coder"
    controller.coder_intelligence = "high"
    controller.fast = False

    await controller.restart("test restart")

    assert controller.coder is not None
    assert controller.coder.model == expected_model
    assert controller.coder.intelligence == expected_intelligence
    assert client.thread_params[-1]["model"] == expected_model
    assert client.turn_params[-1]["effort"] == expected_intelligence
    assert store.get_bello_config().revision_coder_active is revision_active


async def test_user_restart_cancels_inflight_supervisor_task_before_root_swap(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="old-thread",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class FakeClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "new-thread"}}

        async def turn_start(self, params, *, timeout):
            return {"turn": {"id": "new-turn"}}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = FakeClient()
    controller.coder = CoderSession(
        controller.client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="old-thread",
    )
    controller.tui = _FakeTUI()
    controller.supervisor = None
    controller.completion_supervisor = None
    controller.approvals = None
    controller.pending_approvals = {}
    controller.declared_grading_roots = ()
    controller.prior_interventions = []
    controller._subagents = {}
    controller._subagent_policy_notified = set()
    controller._coder_quiesce_mutex = None
    controller._coder_activity_mutex = None
    controller._restart_transition_token = None
    controller._revision_switch_done = None
    controller._revision_switch_owner = None
    controller._coder_snapshot = None
    controller._sequence = 0
    controller.fast = False
    controller.coder_model = DEFAULT_MODEL
    controller.coder_intelligence = "high"
    controller.running = True
    controller.paused = False
    controller._finalizing = False
    controller._terminal_cleanup_started = False

    async def no_subagent_refresh() -> None:
        return None

    controller._refresh_coder_subagents = no_subagent_refresh  # type: ignore[method-assign]
    cancelled = asyncio.Event()

    async def inflight_adversary_like_task() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor_task = asyncio.create_task(inflight_adversary_like_task())
    controller._supervisor_task = supervisor_task
    await asyncio.sleep(0)

    await controller.restart("user requested restart")

    assert cancelled.is_set()
    assert supervisor_task.cancelled()
    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.RUNNING
    assert runtime_config.generation == 1
    assert runtime_config.coder_thread_id == "new-thread"
    assert runtime_config.active_coder_turn_id == "new-turn"


async def test_restart_while_paused_requires_explicit_resume(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="paused-thread",
            status=BelloStatus.PAUSED,
        ),
        overwrite=True,
    )
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.paused = True
    controller._finalizing = False
    controller.tui = _FakeTUI()

    await controller.restart("restart requested while paused")

    runtime_config = store.get_bello_config()
    assert runtime_config.status == BelloStatus.PAUSED
    assert runtime_config.generation == 0
    assert runtime_config.coder_thread_id == "paused-thread"
    assert controller.tui.messages[-1] == ("STATUS", "paused; resume before restarting")
