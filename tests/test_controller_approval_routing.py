"""Controller approval routing regression tests."""
from __future__ import annotations

from pathlib import Path
from supervisor.approvals import ApprovalManager
from supervisor.controller import BelloController
from supervisor.approvals import normalize_approval_request
from supervisor.appserver import AppServerError, AppServerMessage
from supervisor.schemas import ApprovalDecisionKind, CoderMessage, PriorIntervention, BelloConfig, SupervisorDecision, SupervisorDecisionKind, ValidationRun
from supervisor.state import CONFIG, PROGRESS, StateStore
from supervisor.supervisor_agent import StatelessSupervisorAgent

from tests.support.controller import (
    _FakeTUI,
)


async def test_terminal_state_denies_new_server_request_without_policy_path(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.tui = _FakeTUI()
    controller._terminal_cleanup_started = True

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 99,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "echo after terminal", "availableDecisions": ["accept", "decline"]},
            }
        )
    )

    assert controller.client.responses == [(99, {"decision": "decline"})]


async def test_controller_approval_packet_carries_structured_context(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(BelloConfig(project_root=str(tmp_path), task_path=str(task)), overwrite=True)
    context = normalize_approval_request(
        AppServerMessage(
            {
                "id": 42,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "t",
                    "turnId": "u",
                    "itemId": "i",
                    "command": "pytest",
                    "cwd": str(tmp_path),
                    "availableDecisions": ["accept", "decline"],
                },
            }
        )
    )

    class FakeSupervisor:
        def __init__(self) -> None:
            self.agent = StatelessSupervisorAgent(None, store, task)  # type: ignore[arg-type]
            self.packet = None

        def build_packet(self, **kwargs):
            self.packet = self.agent.build_packet(**kwargs)
            return self.packet

        async def decide(self, packet):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.NOOP,
                reason="ok",
                wake_sequence=packet.wake_sequence,
                generation=packet.generation,
            )

    fake = FakeSupervisor()
    controller = BelloController.__new__(BelloController)
    controller.store = store
    controller.project_root = tmp_path
    controller.task_path = task
    controller.supervisor = fake
    controller.pending_approvals = {context.server_request_id: context}
    controller.last_coder_message = CoderMessage(text="ready", sequence=1)
    controller.validations = [ValidationRun(command="pytest", exit_code=1, passed=False, summary="failed", sequence=2)]
    controller.prior_interventions = [PriorIntervention(reason="drift", message_to_coder="focus", sequence=3)]
    controller.use_git_diff = False

    await controller.decide_approval(context, "needs judgment")

    packet = fake.packet
    assert packet.approval_context.command == "pytest"
    assert packet.approval_context.available_decisions == ["accept", "decline"]
    assert len(packet.pending_approvals) == 1
    assert packet.last_coder_message.text == "ready"
    assert packet.validations[0].passed is False
    assert packet.prior_interventions[0].message_to_coder == "focus"


async def test_supervisor_deny_reason_is_steered_to_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )
    context = normalize_approval_request(
        AppServerMessage(
            {
                "id": 51,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "curl https://example.com", "availableDecisions": ["accept", "decline", "cancel"]},
            }
        )
    )

    class FakeSupervisor:
        async def decide_approval(self, context, reason):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.DENY,
                approval_decision="decline",
                reason="Network access is not required by the task.",
                message_to_coder="do not use this",
            )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path, supervisor=FakeSupervisor())
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(AppServerMessage({"id": 51, "method": context.server_request_method, "params": context.raw_params}))

    assert controller.client.responses == [(51, {"decision": "decline"})]
    assert controller.coder.messages == ["Network access is not required by the task."]


async def test_policy_deny_reason_is_steered_to_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 52,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    assert controller.client.responses == [(52, {"decision": "decline"})]
    assert controller.coder.messages == ["writes to supervisor runtime/state files are denied"]


async def test_adversary_file_change_request_is_denied_without_steering_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="coder-thread"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0
    controller._active_adversary_thread_id = "adv-thread"

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 53,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "threadId": "adv-thread",
                    "turnId": "adv-turn",
                    "grantRoot": str(tmp_path / "src" / "app.py"),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    assert controller.client.responses == [(53, {"decision": "decline"})]
    assert controller.coder.messages == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "Adversary approval denied without steering coder" in progress
    # The denial is remembered so an adversary retry can be told what was refused.
    assert len(controller._adversary_denied_commands) == 1
    assert str(tmp_path / "src" / "app.py") in controller._adversary_denied_commands[0]
    assert "(denied:" in controller._adversary_denied_commands[0]


async def test_policy_deny_no_active_turn_starts_new_coder_turn(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.active_turn_id = "turn"
            self.started_messages = []

        async def steer_or_start(self, message):
            raise AppServerError("{'code': -32600, 'message': 'no active turn to steer'}")

        async def start_turn(self, message):
            self.started_messages.append(message)
            self.active_turn_id = "new-turn"
            return "new-turn"

    coder = FakeCoder()
    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = coder
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 55,
                "method": "item/fileChange/requestApproval",
                "params": {
                    "grantRoot": str(tmp_path / ".supervisor" / CONFIG),
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        )
    )

    health = store.get_health()
    assert controller.client.responses == [(55, {"decision": "decline"})]
    assert health.denied_requests == 1
    assert health.last_denial == "writes to supervisor runtime/state files are denied"
    assert coder.started_messages == ["writes to supervisor runtime/state files are denied"]
    assert store.get_bello_config().active_coder_turn_id == "new-turn"
    assert "started a new coder turn with the denial reason" in store.path(PROGRESS).read_text(encoding="utf-8")


async def test_approval_accept_does_not_steer_coder(tmp_path: Path) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path)
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 53,
                "method": "item/fileChange/requestApproval",
                "params": {"grantRoot": str(tmp_path / "src.py"), "availableDecisions": ["accept", "decline"]},
            }
        )
    )

    assert controller.client.responses == [(53, {"decision": "accept"})]
    assert controller.coder.messages == []


async def test_execpolicy_amendment_approval_is_not_rendered_as_denied(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    task = tmp_path / "TASK.md"
    task.write_text("# Task", encoding="utf-8")
    store = StateStore(tmp_path)
    store.initialize_bello(
        BelloConfig(project_root=str(tmp_path), task_path=str(task), coder_thread_id="thread", active_coder_turn_id="turn"),
        overwrite=True,
    )
    amendment = ["/bin/zsh", "-lc", "printf 'hello bello\\n' > hello.txt"]
    offered_decision = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": amendment}}

    class FakeSupervisor:
        async def decide_approval(self, context, reason):
            return SupervisorDecision(
                decision=SupervisorDecisionKind.APPROVE,
                approval_decision=ApprovalDecisionKind.ACCEPT,
                execpolicy_amendment=amendment,
                reason="scoped task file write",
            )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = []

        async def respond(self, request_id, response):
            self.responses.append((request_id, response))

    class FakeCoder:
        def __init__(self) -> None:
            self.messages = []

        async def steer_or_start(self, message):
            self.messages.append(message)
            return "turn"

    controller = BelloController.__new__(BelloController)
    controller.project_root = tmp_path
    controller.store = store
    controller.client = FakeClient()
    controller.approvals = ApprovalManager(tmp_path, supervisor=FakeSupervisor())
    controller.coder = FakeCoder()
    controller.pending_approvals = {}
    controller.tui = _FakeTUI()
    controller._sequence = 0

    await controller.handle_server_request(
        AppServerMessage(
            {
                "id": 54,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "command": "printf 'hello bello\\n' > hello.txt",
                    "cwd": str(tmp_path),
                    "availableDecisions": [offered_decision, "decline"],
                },
            }
        )
    )

    assert controller.client.responses == [(54, {"decision": offered_decision})]
    assert controller.tui.messages[0][0] == "APPROVAL"
    assert controller.coder.messages == []
