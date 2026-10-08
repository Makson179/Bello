"""Controller terminal lifecycle regression tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest
import supervisor.controller as controller_module
from supervisor.approvals import ApprovalManager
from supervisor.controller import ControllerEvent, BelloController
from supervisor.approvals import normalize_approval_request
from supervisor.appserver import APP_SERVER_CODER_RPC_TIMEOUT_SECONDS, AppServerMessage, AppServerTimeoutError
from supervisor.coder import CoderSession
from supervisor.project_config import DEFAULT_MODEL
from supervisor.schemas import BelloConfig, BelloStatus
from supervisor.state import CONFIG, FINAL_REPORT, RUN_CHECKPOINT, StateStore

from tests.support.controller import (
    _FakeTUI,
    _async_noop,
    _async_schema_hash,
    _mock_codex_probe,
    _runtime_controller,
)


async def test_server_request_respond_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class RespondTimeoutClient:
        async def respond(self, request_id, response):
            raise AppServerTimeoutError("app-server respond 61 send timed out after 15s")

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = RespondTimeoutClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = None
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.running = True

    await controller.handle_controller_event(
        ControllerEvent(
            kind="server_request",
            message=AppServerMessage(
                {
                    "id": 61,
                    "method": "item/fileChange/requestApproval",
                    "params": {"grantRoot": str(tmp_path / "src.py"), "availableDecisions": ["accept", "decline"]},
                }
            ),
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed while handling server_request" in text
    assert "respond 61 send timed out" in text
    assert controller.running is False


async def test_coder_turn_start_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="coder-thread"),
        overwrite=True,
    )

    class CoderTurnTimeoutClient:
        async def respond(self, request_id, response):
            return None

        async def turn_start(self, params, *, timeout):
            assert timeout == APP_SERVER_CODER_RPC_TIMEOUT_SECONDS
            raise AppServerTimeoutError(f"app-server RPC turn/start response timed out after {timeout:g}s")

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.client = CoderTurnTimeoutClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = CoderSession(
        controller.client,  # type: ignore[arg-type]
        store,
        tmp_path,
        task,
        thread_id="coder-thread",
    )
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.running = True

    await controller.handle_controller_event(
        ControllerEvent(
            kind="server_request",
            message=AppServerMessage(
                {
                    "id": 62,
                    "method": "item/fileChange/requestApproval",
                    "params": {
                        "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                        "availableDecisions": ["accept", "decline"],
                    },
                }
            ),
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed while handling server_request" in text
    assert "turn/start response timed out after 3600s" in text
    assert controller.running is False


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, asyncio.CancelledError, KeyboardInterrupt])
async def test_run_unexpected_exception_finalizes_failure_without_applying_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException],
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    source = tmp_path / "app.py"
    source.write_text("original\n", encoding="utf-8")

    class Client:
        def __init__(self):
            self.stopped = False
            self.responses = []

        async def start(self):
            pass

        async def initialize(self):
            return {}

        async def stop(self):
            self.stopped = True

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class Coder:
        def __init__(self, client, store, *args, **kwargs):
            self.store = store
            self.thread_id = "coder-thread"
            self.active_turn_id = None

        async def start_thread(self):
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"coder_thread_id": self.thread_id}))

        async def start_initial_turn(self):
            self.active_turn_id = "coder-turn"
            self.store.update_bello_config(lambda cfg: cfg.model_copy(update={"active_coder_turn_id": self.active_turn_id}))

        async def interrupt(self):
            pass

    monkeypatch.setattr(controller_module, "CoderSession", Coder)
    client = Client()
    controller = BelloController(
        tmp_path, task_path=task, client=client, tui=_FakeTUI(),
        runtime_enabled=False, completion_review=False, adversary_enabled=False,
        overwrite_state=True, use_git_diff=False,
    )
    controller.preflight = _async_noop
    failure = error_type("synthetic event-loop failure")

    async def broken_event_loop():
        (controller._active_workspace_root() / "app.py").write_text("unaccepted change\n", encoding="utf-8")
        context = normalize_approval_request(AppServerMessage({
            "id": "pending-1", "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "coder-thread", "turnId": "coder-turn",
                       "command": "node -e 'test'", "availableDecisions": ["accept", "decline"]},
        }))
        controller.pending_approvals["pending-1"] = context
        controller.store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": ["pending-1"]}))
        raise failure

    controller.event_loop = broken_event_loop
    with pytest.raises(error_type) as caught:
        await controller.run()
    assert caught.value is failure
    assert any(frame.name == "broken_event_loop" for frame in caught.traceback)
    assert client.stopped is True
    assert source.read_text(encoding="utf-8") == "original\n"
    recovery = (
        controller._active_workspace_root() if not issubclass(error_type, Exception)
        else tmp_path / ".supervisor" / "recovery" / "run1" / "workspace"
    )
    assert (recovery / "app.py").read_text(encoding="utf-8") == "unaccepted change\n"
    assert controller._snapshot_patch_applied is False
    if not issubclass(error_type, Exception):
        # These control-flow exceptions are not converted into provider errors.
        assert controller.store.get_bello_config().status != BelloStatus.PROVIDER_FAILURE
        return
    cfg = controller.store.get_bello_config()
    assert cfg.status == BelloStatus.PROVIDER_FAILURE
    assert cfg.active_coder_turn_id is None
    assert cfg.pending_server_request_ids == []
    assert controller.pending_approvals == {}
    assert client.responses == [("pending-1", {"decision": "decline"})]
    checkpoint = json.loads(controller.store.path(RUN_CHECKPOINT).read_text(encoding="utf-8"))
    assert checkpoint["status"] == "provider_failure"
    assert checkpoint["phase"] == checkpoint["state"] == "terminal"
    assert checkpoint["active_coder_turn_id"] is None
    report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "- Status: provider_failure" in report
    assert f"run infrastructure failed: {error_type.__name__}" in report
    assert "unaccepted coder workspace preserved" in report
    assert controller.running is False


async def test_run_shutdown_after_final_report_stops_stubbed_appserver(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class ShutdownClient:
        def __init__(self) -> None:
            self.initial_turn_started = asyncio.Event()
            self.stopped = False
            self.thread_count = 0

        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            self.stopped = True

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {
                "data": [
                    {"id": DEFAULT_MODEL},
                    {"id": "gpt-coder"},
                    {"id": "gpt-runtime"},
                    {"id": "gpt-completion"},
                ]
            }

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params, **kwargs):
            self.thread_count += 1
            return {
                "thread": {"id": f"thread-{self.thread_count}"},
                "approvalPolicy": "on-request",
                "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            }

        async def thread_unsubscribe(self, thread_id, **kwargs):
            return {}

        async def turn_start(self, params, **kwargs):
            self.initial_turn_started.set()
            return {"turn": {"id": "turn-1", "status": "running"}}

    client = ShutdownClient()
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model="gpt-coder",
        runtime_model="gpt-runtime",
        completion_model="gpt-completion",
        coder_intelligence="ultra",
        runtime_intelligence="xhigh",
        completion_intelligence="high",
        adversary_enabled=False,
        completion_review=True,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    # This test owns a deliberately minimal app-server stub and verifies coder
    # shutdown, not cheap-runtime startup.  Isolate the unrelated triage probe
    # so its turn cannot satisfy ``initial_turn_started`` first.
    controller._configure_runtime_triage = _async_noop

    run_task = asyncio.create_task(controller.run())
    await asyncio.wait_for(client.initial_turn_started.wait(), timeout=5)
    await controller.finalize("task complete", status=BelloStatus.COMPLETE)
    await asyncio.wait_for(run_task, timeout=5)

    assert controller.coder is not None
    assert controller.coder.model == "gpt-coder"
    assert controller.coder.intelligence == "ultra"
    assert controller.supervisor is not None
    assert controller.supervisor.model == "gpt-runtime"
    assert controller.supervisor.intelligence == "xhigh"
    assert controller.completion_supervisor is not None
    assert controller.completion_supervisor is not controller.supervisor
    assert controller.completion_supervisor.model == "gpt-completion"
    assert controller.completion_supervisor.intelligence == "high"
    assert controller.adv_report_controller is None
    assert client.stopped is True
    assert controller.running is False


async def test_finalize_writes_report_and_status_before_terminal_shutdown(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    shutdown_seen = False

    async def fake_prepare_terminal_shutdown(reason: str) -> None:
        nonlocal shutdown_seen
        shutdown_seen = True
        assert store.get_bello_config().status == BelloStatus.COMPLETE
        report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
        assert "# Final Report" in report
        assert "task complete" in report

    controller._prepare_terminal_shutdown = fake_prepare_terminal_shutdown  # type: ignore[method-assign]

    await controller.finalize("task complete", status=BelloStatus.COMPLETE)

    assert shutdown_seen is True
    assert store.get_bello_config().status == BelloStatus.COMPLETE
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8").strip()
