"""Controller command events regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from supervisor.approvals import ApprovalManager
from supervisor.appserver import AppServerMessage
from supervisor.schemas import BelloStatus
from supervisor.state import PROGRESS, RUNTIME_TRACE

from tests.support.controller import (
    _runtime_controller,
    _runtime_controller_with_plan,
)


async def test_command_output_delta_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "delta": "hello "},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "delta": {"text": "world\n"}},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 hello.py",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )
    if controller._supervisor_task is not None:
        await controller._supervisor_task

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "python3 hello.py"
    assert validation.type == "behavior_demo"
    assert validation.passed is True
    assert "hello world" in validation.summary
    assert validation.captured_output == "hello world\n"
    assert controller._command_output_chunks == {}


async def test_plan_command_line_does_not_suppress_genuine_validation_event(
    tmp_path: Path,
) -> None:
    controller, _store, _fake, snapshot, _plan = _runtime_controller_with_plan(
        tmp_path,
        "PRIVATE_PLAN_SENTINEL_42\npytest -q\n",
    )
    try:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-plan-command",
                        "delta": "1 passed in 0.01s\n",
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-plan-command",
                        "item": {
                            "type": "commandExecution",
                            "command": "pytest -q",
                            "exitCode": 0,
                            "status": "completed",
                        },
                    },
                }
            )
        )
        if controller._supervisor_task is not None:
            await controller._supervisor_task

        assert len(controller.validations) == 1
        validation = controller.validations[0]
        assert validation.command == "pytest -q"
        assert validation.raw_command == "pytest -q"
        assert validation.type == "behavioral"
        assert validation.trusted_validation_outcome == "passed"
        assert validation.captured_output == "1 passed in 0.01s\n"
        assert controller._review_safe_values([validation]) == [validation]
        details = await controller.completion_packet_details([])
        assert len(details["validation_outputs"]) == 1
        assert details["validation_outputs"][0].command == "pytest -q"
    finally:
        snapshot.cleanup()


async def test_direct_plan_read_is_not_persisted_as_review_evidence(
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE_PLAN_SENTINEL_42"
    controller, store, _fake, snapshot, plan = _runtime_controller_with_plan(
        tmp_path,
        f"{sentinel}\npytest -q\n",
    )
    assert snapshot.plan_path is not None
    try:
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-read-plan",
                        "delta": f"{sentinel}\npytest -q\n",
                    },
                }
            )
        )
        await controller.handle_notification(
            AppServerMessage(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread",
                        "turnId": "turn",
                        "itemId": "cmd-read-plan",
                        "item": {
                            "type": "commandExecution",
                            "command": f"cat {snapshot.plan_path}",
                            "cwd": str(snapshot.snapshot_root),
                            "exitCode": 0,
                            "status": "completed",
                        },
                    },
                }
            )
        )
        if controller._supervisor_task is not None:
            await controller._supervisor_task

        assert controller.validations == []
        assert controller.inspections == []
        assert controller._command_output_chunks == {}
        assert store.read_recent_actions(1) == ["workspace action completed"]
        details = await controller.completion_packet_details([])
        assert details["validation_outputs"] == []
        assert details["inspection_outputs"] == []
        forbidden = (
            sentinel.encode(),
            str(snapshot.plan_path).encode(),
            str(plan.resolve()).encode(),
        )
        for state_path in store.state_dir.rglob("*"):
            if state_path.is_file():
                payload = state_path.read_bytes()
                assert all(marker not in payload for marker in forbidden)
    finally:
        snapshot.cleanup()


async def test_non_coder_command_output_is_not_retained_or_added_to_ledger(
    tmp_path: Path,
) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": "completion-thread",
                    "turnId": "completion-turn",
                    "itemId": "completion-command",
                    "delta": "large review output",
                },
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "completion-thread",
                    "turnId": "completion-turn",
                    "itemId": "completion-command",
                    "item": {
                        "type": "commandExecution",
                        "command": "pytest tests/test_target.py",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )

    assert controller._command_output_chunks == {}
    assert controller.validations == []
    assert controller.inspections == []


async def test_camelcase_stdout_delta_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/commandExecution/stdoutDelta",
                "params": {"threadId": "thread", "turnId": "turn", "itemId": "cmd-1", "stdout": "ok pkg/a 0.01s\n"},
            }
        )
    )
    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "/usr/local/go/bin/go test -count=1 ./...",
                        "exitCode": 0,
                        "status": "completed",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "/usr/local/go/bin/go test -count=1 ./..."
    assert validation.type == "behavioral"
    assert validation.passed is True
    assert validation.captured_output == "ok pkg/a 0.01s\n"
    assert "ok pkg/a" in validation.summary


async def test_command_aggregated_output_is_attached_to_validation_ledger(tmp_path: Path) -> None:
    controller, _store, _fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "python3 hello.py",
                        "cwd": str(tmp_path),
                        "exitCode": 0,
                        "status": "completed",
                        "aggregatedOutput": "Hello world\n",
                    },
                },
            }
        )
    )

    assert len(controller.validations) == 1
    validation = controller.validations[0]
    assert validation.command == "python3 hello.py"
    assert validation.type == "behavior_demo"
    assert validation.passed is True
    assert validation.captured_output == "Hello world\n"
    assert "Hello world" in validation.summary


async def test_read_only_command_is_recorded_in_inspection_ledger(tmp_path: Path) -> None:
    controller, store, fake = _runtime_controller(tmp_path)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": "rg -n \"system\\(\" src",
                        "exitCode": 1,
                        "status": "completed",
                        "stdout": "",
                    },
                },
            }
        )
    )

    assert controller.validations == []
    assert len(controller.inspections) == 1
    assert controller.inspections[0].passed is True
    assert fake.runtime_packets == []
    trace = json.loads(store.path(RUNTIME_TRACE).read_text(encoding="utf-8").splitlines()[-1])
    assert trace["should_wake_runtime_supervisor"] is False


async def test_declared_grading_path_completed_command_escalates_integrity_failure(
    tmp_path: Path,
    posix_command_semantics: None,
) -> None:
    controller, store, fake = _runtime_controller(tmp_path)
    grading_root = tmp_path / "SpecBench" / "examples" / "c_compiler"
    grading_root.mkdir(parents=True)
    (grading_root / "hidden.c").write_text("int secret;\n", encoding="utf-8")
    controller.declared_grading_roots = (str(grading_root),)
    controller.approvals = ApprovalManager(tmp_path, declared_grading_roots=controller.declared_grading_roots)

    await controller.handle_notification(
        AppServerMessage(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread",
                    "itemId": "cmd-1",
                    "item": {
                        "type": "commandExecution",
                        "command": f"sed -n '1,20p' {grading_root / 'hidden.c'}",
                        "exitCode": 0,
                        "status": "completed",
                        "stdout": "int secret;\n",
                    },
                },
            }
        )
    )

    assert store.get_bello_config().status == BelloStatus.ESCALATED
    assert controller.running is False
    assert fake.runtime_packets == []
    progress = store.path(PROGRESS).read_text(encoding="utf-8")
    assert "coder accessed declared grading/hidden path" in progress
