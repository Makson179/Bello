"""Controller failure recovery regression tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from supervisor.controller import ADVERSARY_MODEL, ControllerEvent, BelloController
from supervisor.appserver import AppServerError, AppServerTimeoutError
from supervisor.project_config import DEFAULT_MODEL, MODEL_GPT_5_5, MODEL_GPT_5_6_SOL
from supervisor.schemas import ChangedFile, BelloConfig, BelloStatus, SupervisorDecision, SupervisorDecisionKind, TriggeringAction
from supervisor.state import FINAL_REPORT, LOG, PROGRESS, SUPERVISOR_WAKES, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent, SupervisorAgentError

from tests.support.controller import (
    _FakeTUI,
    _async_noop,
    _async_schema_hash,
    _mock_codex_probe,
    _runtime_controller,
)


async def test_transport_error_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.validations = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller._sequence = 0

    await controller.handle_controller_event(
        ControllerEvent(
            kind="transport_error",
            error_message="app-server stdout line exceeded stream limit (64 bytes): test payload",
        )
    )

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server transport error" in text
    assert controller.running is False


async def test_interrupted_coder_turn_resumes_same_thread_with_continuation(
    tmp_path: Path,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(
            project_root=str(tmp_path),
            task_path=str(task),
            coder_thread_id="coder-thread",
            active_coder_turn_id="old-turn",
            status=BelloStatus.RUNNING,
        ),
        overwrite=True,
    )

    class RecoverableCoder:
        thread_id = "coder-thread"
        active_turn_id = "old-turn"

        def __init__(self) -> None:
            self.messages: list[str] = []

        async def resume_thread(self):
            return {
                "id": "coder-thread",
                "turns": [
                    {"id": "old-turn", "status": "interrupted", "items": []}
                ],
            }

        async def start_turn(self, message: str):
            self.messages.append(message)
            self.active_turn_id = "recovery-turn"
            store.update_bello_config(
                lambda cfg: cfg.model_copy(
                    update={"active_coder_turn_id": "recovery-turn"}
                )
            )
            return "recovery-turn"

    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.coder = RecoverableCoder()

    await controller._recover_coder_thread_after_transport(start_continuation=True)

    assert controller.coder.thread_id == "coder-thread"
    assert controller.coder.active_turn_id == "recovery-turn"
    assert len(controller.coder.messages) == 1
    assert "current workspace state" in controller.coder.messages[0]
    assert store.get_bello_config().active_coder_turn_id == "recovery-turn"


async def test_supervisor_turn_start_timeout_writes_provider_failure_final_report(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class HangingTurnStartClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            await asyncio.Event().wait()

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller.supervisor = StatelessSupervisorAgent(
        HangingTurnStartClient(),
        store,
        task,
        timeout_seconds=0.01,
    )  # type: ignore[arg-type]

    await controller._run_supervisor_check("check latest state", None, None, None, None)

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "supervisor check failed" in text
    assert "supervisor turn/start response timed out after 0.01s" in text
    assert "thread_id=supervisor-thread" in text
    assert controller.running is False
    audit = json.loads(store.path(SUPERVISOR_WAKES).read_text(encoding="utf-8").splitlines()[-1])
    assert audit["status"] == "error"
    assert audit["thread_id"] == "supervisor-thread"
    assert audit["turn_id"] is None
    assert "supervisor turn/start response timed out after 0.01s" in audit["error"]


async def test_stale_runtime_supervisor_timeout_retries_before_queued_completion(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class HangingTurnStartClient:
        async def thread_start(self, params, *, timeout):
            return {"thread": {"id": "supervisor-thread"}}

        async def turn_start(self, params, *, timeout):
            await asyncio.Event().wait()

        async def thread_archive(self, thread_id, *, timeout):
            return {}

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.task_path = task
    controller.store = store
    controller.use_git_diff = False
    controller.pending_approvals = {}
    controller.last_coder_message = None
    controller.validations = []
    controller.prior_interventions = []
    controller.observed_changed_files = {}
    controller.tui = _FakeTUI()
    controller.running = True
    controller.supervisor = StatelessSupervisorAgent(
        HangingTurnStartClient(),
        store,
        task,
        timeout_seconds=0.01,
    )  # type: ignore[arg-type]
    controller._queue_supervisor_check(
        "Coder provided exact readiness marker; running completion_review.",
        completion_review=True,
    )

    await controller._run_supervisor_check("stale runtime check", None, None, None, None)

    text = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert text == ""
    assert controller.running is True
    assert controller._supervisor_next_runtime_summary is not None
    assert controller._supervisor_next_completion_summary is not None
    assert "retrying the retained runtime trigger before completion" in store.path(PROGRESS).read_text(encoding="utf-8")
    assert any("supervisor check failed" in message for _, message in controller.tui.messages)


async def test_supervisor_no_message_retries_from_latest_stable_state(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class NoMessageThenNoopSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            if self.calls == 1:
                raise SupervisorAgentError("supervisor did not produce an agent message")
            return SupervisorDecision(
                decision=SupervisorDecisionKind.NOOP,
                reason="recovered",
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    supervisor = NoMessageThenNoopSupervisor(store, controller.task_path)
    controller.supervisor = supervisor

    await controller._supervisor_check_loop("runtime check", None, None, None, None, False)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    # After a successful recovery the consecutive no_message budget resets, so a recovered
    # provider does not carry earlier blips toward infra-invalid.
    assert controller.provider_failure_recovery_counts == {}
    assert "supervisor produced no agent message" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_repeated_runtime_supervisor_no_message_skips_current_review(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageRuntimeSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

    supervisor = AlwaysNoMessageRuntimeSupervisor(store, controller.task_path)
    controller.supervisor = supervisor

    await controller._supervisor_check_loop("runtime check", None, None, None, None, False)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.STARTING
    assert store.path(FINAL_REPORT).read_text(encoding="utf-8") == ""
    assert controller.running is True
    assert controller._supervisor_dirty is False
    assert controller.provider_failure_recovery_counts["no_message"] == 2
    assert controller.provider_failure_recovery_counts["runtime_monitor_no_message"] == 2
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "retrying review from latest stable state" in progress
    assert "skipping this runtime-only review" in progress


async def test_runtime_no_message_exhaustion_explicitly_skips_and_acks_pending_large_diff(
    tmp_path: Path,
) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageRuntimeSupervisor:
        def __init__(self, state_store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, state_store, task)  # type: ignore[arg-type]

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide(self, packet):
            raise SupervisorAgentError("supervisor did not produce an agent message")

    changed_files = [
        ChangedFile(path="src/app.py", status="M", additions=600, deletions=0, sequence=2)
    ]
    decision = controller.should_wake_runtime_supervisor(
        action=TriggeringAction(
            kind="fileChange",
            paths=["src/app.py"],
            status="completed",
            summary="file change completed",
        ),
        validation=None,
        changed_files=changed_files,
    )
    pending_signature = controller._runtime_pending_trigger_signatures()["large_diff"][0]
    controller.supervisor = AlwaysNoMessageRuntimeSupervisor(store, controller.task_path)

    await controller._supervisor_check_loop(
        "Runtime trigger (large_diff): file change completed",
        None,
        None,
        None,
        None,
        False,
    )

    assert decision.reasons == ("large_diff",)
    assert controller._last_large_diff_signature == pending_signature
    assert "large_diff" not in controller._runtime_pending_trigger_signatures()
    assert "skipping this runtime-only review" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_repeated_supervisor_no_message_marks_infra_invalid_provider_failure(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide_completion(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

        async def close_completion_review(self):
            return None

    supervisor = AlwaysNoMessageSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    # Pin the configurable completion no_message budget low and disable backoff so the test
    # reaches the infra-invalid path fast (default budget rides out a transient blip).
    controller._completion_no_message_max_retries = 1
    controller._no_message_backoff_seconds = ()

    await controller._supervisor_check_loop("completion check", None, None, None, None, True)

    assert supervisor.calls == 2
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    report = store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert "infra-invalid: supervisor no_message provider failure after retry/resume" in report
    assert "- Status: provider_failure" in report
    assert "repeated supervisor no_message" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_completion_no_message_budget_rides_out_blip_before_infra_invalid(tmp_path: Path) -> None:
    # A transient provider blip (empty completions) must be ridden out with backed-off retries
    # up to the configurable budget; infra-invalid only fires after the full budget is spent.
    controller, store, _ = _runtime_controller(tmp_path)

    class AlwaysNoMessageSupervisor:
        def __init__(self, store: StateStore, task: Path) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.calls = 0

        def build_packet(self, **kwargs):
            return self.agent.build_packet(**kwargs)

        async def decide_completion(self, packet):
            self.calls += 1
            raise SupervisorAgentError("supervisor did not produce an agent message")

        async def close_completion_review(self):
            return None

    supervisor = AlwaysNoMessageSupervisor(store, controller.task_path)
    controller.supervisor = supervisor
    controller._completion_no_message_max_retries = 3
    controller._no_message_backoff_seconds = ()  # no real sleeping in the test

    await controller._supervisor_check_loop("completion check", None, None, None, None, True)

    # 3 retries then the infra-invalid attempt = 4 model calls (old behavior gave up after 1 retry).
    assert supervisor.calls == 4
    assert store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE


async def test_preflight_appserver_timeout_writes_provider_failure_final_report(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class PreflightTimeoutClient:
        async def start(self):
            return None

        async def initialize(self):
            return {}

        async def stop(self):
            return None

        async def account_read(self):
            raise AppServerTimeoutError("app-server RPC account/read response timed out after 30s")

    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=PreflightTimeoutClient(),  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    text = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in text
    assert "app-server RPC failed" in text
    assert "account/read response timed out" in text


async def test_missing_selected_model_interrupts_before_coder_and_writes_final_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class MissingModelClient:
        def __init__(self) -> None:
            self.thread_started = False
            self.stopped = False

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
            return {"data": [{"id": MODEL_GPT_5_6_SOL}, {"id": MODEL_GPT_5_5}]}

        async def thread_start(self, params):
            self.thread_started = True
            raise AssertionError("coder must not start with an unavailable model")

    client = MissingModelClient()
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model="gpt-5.6-unknown",
        supervisor_model=MODEL_GPT_5_6_SOL,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in report
    assert "model availability preflight failed before coder start" in report
    assert "coder=gpt-5.6-unknown" in report
    assert "Available models: gpt-5.5, gpt-5.6-sol" in report
    assert ".supervisor/FINAL_REPORT.md" in report
    assert client.thread_started is False
    assert client.stopped is True


async def test_missing_fixed_adversary_model_interrupts_before_coder_and_writes_final_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class MissingAdversaryModelClient:
        def __init__(self) -> None:
            self.thread_started = False
            self.stopped = False

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
            return {"data": [{"id": MODEL_GPT_5_5}]}

        async def thread_start(self, params):
            self.thread_started = True
            raise AssertionError("coder must not start with an unavailable adversary model")

    client = MissingAdversaryModelClient()
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        coder_model=MODEL_GPT_5_5,
        supervisor_model=MODEL_GPT_5_5,
        overwrite_state=True,
        use_git_diff=False,
        completion_review=True,
        adversary_enabled=True,
    )
    controller._generate_schema_hash_async = _async_schema_hash

    await controller.run()

    report = controller.store.path(FINAL_REPORT).read_text(encoding="utf-8")
    assert controller.store.get_bello_config().status == BelloStatus.PROVIDER_FAILURE
    assert "- Status: provider_failure" in report
    assert "model availability preflight failed before coder start" in report
    assert f"adversary={ADVERSARY_MODEL}" in report
    assert "Available models: gpt-5.5" in report
    assert client.thread_started is False
    assert client.stopped is True


async def test_preflight_probe_cleanup_unsubscribes_and_logs_without_failing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class ProbeCleanupClient:
        def __init__(self) -> None:
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
                return {
                    "thread": {"id": "probe-thread"},
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
                }

        async def thread_archive(self, thread_id):
            raise AssertionError("preflight probe cleanup should not archive threads without rollouts")

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            raise AppServerError("unsubscribe cleanup failed")

    client = ProbeCleanupClient()
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    assert client.unsubscribed == ["probe-thread"]
    config = controller.store.get_bello_config()
    assert config.model == DEFAULT_MODEL
    assert config.coder_model == DEFAULT_MODEL
    assert config.supervisor_model == DEFAULT_MODEL
    log_lines = controller.store.path(LOG).read_text(encoding="utf-8").splitlines()
    assert log_lines
    entry = json.loads(log_lines[-1])
    assert entry["type"] == "cleanup_error"
    assert entry["cleanup_kind"] == "preflight_probe_thread"
    assert entry["thread_id"] == "probe-thread"
    assert entry["error_type"] == "AppServerError"


async def test_preflight_rate_limit_probe_failure_warns_and_continues(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class RateLimitFailureClient:
        def __init__(self) -> None:
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            raise AppServerError(
                "{'code': -32603, 'message': 'failed to fetch codex rate limits: error sending request'}"
            )

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
                return {
                    "thread": {"id": "probe-thread"},
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
                }

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = RateLimitFailureClient()
    tui = _FakeTUI()
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=tui,
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    config = controller.store.get_bello_config()
    assert config.model == DEFAULT_MODEL
    assert config.coder_model == DEFAULT_MODEL
    assert config.supervisor_model == DEFAULT_MODEL
    assert client.unsubscribed == ["probe-thread"]
    assert any("rate limit check unavailable" in message for _, message in tui.messages)
    log_lines = controller.store.path(LOG).read_text(encoding="utf-8").splitlines()
    assert log_lines
    entry = json.loads(log_lines[-1])
    assert entry["type"] == "preflight_warning"
    assert entry["check"] == "codex_rate_limits"
    assert entry["error_type"] == "AppServerError"


async def test_preflight_accepts_configured_danger_full_access_sandbox(tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")

    class DangerSandboxClient:
        def __init__(self) -> None:
            self.thread_params: dict | None = None
            self.unsubscribed: list[str] = []

        async def account_read(self):
            return {"requiresOpenaiAuth": False, "account": {"id": "acct"}}

        async def account_rate_limits_read(self):
            return {}

        async def model_list(self):
            return {"data": [{"id": DEFAULT_MODEL}, {"id": "gpt-test"}]}

        async def config_requirements_read(self):
            return {}

        async def thread_start(self, params):
            self.thread_params = params
            return {
                "thread": {"id": "probe-thread"},
                "approvalPolicy": "on-request",
                "sandbox": "danger-full-access",
            }

        async def thread_unsubscribe(self, thread_id):
            self.unsubscribed.append(thread_id)
            return {}

    client = DangerSandboxClient()
    monkeypatch.setenv("BELLO_CODER_SANDBOX", "danger-full-access")
    _mock_codex_probe(monkeypatch)
    controller = BelloController(
        tmp_path,
        task_path=task,
        client=client,  # type: ignore[arg-type]
        tui=_FakeTUI(),
        overwrite_state=True,
        use_git_diff=False,
    )
    controller._generate_schema_hash_async = _async_schema_hash
    controller._structured_output_self_test = _async_noop
    controller.initialize_state()

    await controller.preflight()

    assert client.thread_params is not None
    assert client.thread_params["sandbox"] == "danger-full-access"
    assert client.unsubscribed == ["probe-thread"]
