"""Compatibility and lifecycle boundaries of the composed controller services."""
from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import supervisor.controller as controller_module
from supervisor.controller import BelloController, ChangedFile, SubagentRuntimeState
from supervisor.controller_parts.interfaces import OwnedField
from tests.support.controller import _runtime_controller


def test_new_only_controller_retains_absent_fields_and_single_state_owner() -> None:
    controller = BelloController.__new__(BelloController)
    other = BelloController.__new__(BelloController)
    assert not hasattr(controller, "validations")
    assert not hasattr(controller, "_coder_activity_mutex")
    ledger = []
    controller.validations = ledger
    assert controller._service("commands").state.validations is ledger
    assert "validations" not in controller.__dict__
    assert not hasattr(other, "validations")
    controller._record_command_output_delta(
        "item/commandExecution/outputDelta", {"delta": "first"}, item_id="command"
    )
    assert controller._pop_command_output("command") == "first"
    assert controller._pop_command_output("command") == ""
    del controller.validations
    assert not hasattr(controller, "validations")
    # Neither accidental state fields nor unrelated coordinator capabilities leak
    # through the service boundary.
    with pytest.raises(AttributeError):
        controller._service("commands").state.undeclared = True
    with pytest.raises(AttributeError):
        controller._service("commands").ports.paused = True
    with pytest.raises(AttributeError):
        _ = controller._service("commands").ports._services


def test_every_legacy_state_field_has_one_declared_owner() -> None:
    controller = BelloController.__new__(BelloController)
    for name, descriptor in vars(BelloController).items():
        if isinstance(descriptor, OwnedField):
            state = controller._service(descriptor.owner).state
            assert name in state.__slots__
            assert not hasattr(controller, name)


def test_async_and_sync_entry_points_keep_introspection_contract() -> None:
    assert inspect.iscoroutinefunction(BelloController.changed_files)
    assert inspect.iscoroutinefunction(BelloController.restart)
    assert not inspect.iscoroutinefunction(BelloController._record_changed_files)
    assert tuple(inspect.signature(BelloController._deliver_coder_message).parameters) == (
        "self", "message", "coder", "force_new_turn"
    )


def test_service_modules_import_independently_without_loading_controller() -> None:
    root = Path(controller_module.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", (
            "import importlib, pkgutil, sys\n"
            "import supervisor.controller_parts as parts\n"
            "for module in pkgutil.iter_modules(parts.__path__):\n"
            "    importlib.import_module(parts.__name__ + '.' + module.name)\n"
            "assert 'supervisor.controller' not in sys.modules\n"
            "from supervisor.controller import BelloController\n"
            "assert BelloController.__module__ == 'supervisor.controller'\n"
        )],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_fingerprint_helper_uses_legacy_global_patch(monkeypatch, tmp_path: Path) -> None:
    def refuse_open(path):
        raise OSError("injected no-follow open")

    monkeypatch.setattr(controller_module, "_open_regular_file_no_follow", refuse_open)
    with pytest.raises(OSError, match="injected no-follow open"):
        controller_module._hash_file(tmp_path / "file")


@pytest.mark.parametrize("count", [200, 201, 499, 500, 501])
@pytest.mark.parametrize("source", ["observed", "git", "direct"])
async def test_completion_file_cap_preserves_omission_evidence(
    monkeypatch, tmp_path: Path, count: int, source: str
) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    files = [ChangedFile(path=f"src/file_{index}.py", status="A", sequence=2) for index in range(count)]
    controller.observed_changed_files = {file.path: file for file in files}
    controller.use_git_diff = source == "git"

    async def is_git():
        return True

    async def git_output(command):
        if "status" in command:
            return "".join(f"?? {file.path}\0" for file in files)
        return ""

    monkeypatch.setattr(controller, "_is_git_work_tree", is_git)
    monkeypatch.setattr(controller, "_git_output", git_output)
    # Initialize the service before patching; all future helper lookup stays live.
    controller._service("evidence")
    monkeypatch.setattr(
        controller_module, "_read_workspace_file",
        lambda root, path, *, limit: controller_module._BoundedFileText("value = 1\n", False),
    )
    changed = files if source == "direct" else await controller.changed_files()
    if source != "direct":
        assert len(changed) == min(count, 500)
    assert len(controller_module._observed_changed_files(controller)) == min(count, 500)
    packet = await controller.completion_packet_details(changed)
    assert len(packet["changed_file_diffs"]) == min(count, 500)
    assert len(packet["changed_file_contexts"]) == min(count, 500)
    limits = packet["diff_packet_limits"]
    assert limits.materially_truncated is (count > 500)
    assert limits.omitted_changed_files == [file.path for file in files[500:]]
    if count > 500:
        assert "500-file evidence limit" in limits.truncation_reason
    else:
        assert limits.truncation_reason is None


async def test_overflow_is_scoped_to_discovery_and_delta_window(monkeypatch, tmp_path: Path) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    monkeypatch.setattr(
        controller_module, "_read_workspace_file",
        lambda root, path, *, limit: controller_module._BoundedFileText("x", False),
    )
    controller.observed_changed_files = {
        f"src/file_{index}.py": ChangedFile(path=f"src/file_{index}.py", status="A", sequence=1)
        for index in range(501)
    }
    changed = await controller.changed_files()
    packet = await controller.completion_packet_details(changed, since_sequence=2)
    assert packet["changed_file_diffs"] == []
    assert not packet["diff_packet_limits"].materially_truncated
    # A directly supplied, unrelated packet must not inherit discovery overflow.
    packet = await controller.completion_packet_details([ChangedFile(path="new.py", status="A")])
    assert not packet["diff_packet_limits"].materially_truncated
    controller.observed_changed_files = {}
    packet = await controller.completion_packet_details(await controller.changed_files())
    assert packet["diff_packet_limits"].omitted_changed_files == []


async def test_per_file_truncation_and_unreadable_files_still_reported(monkeypatch, tmp_path: Path) -> None:
    controller, _, _ = _runtime_controller(tmp_path)

    def read_file(root, path, *, limit):
        if path == "missing.py":
            return None
        return controller_module._BoundedFileText("x" * limit, True)

    monkeypatch.setattr(controller_module, "_read_workspace_file", read_file)
    packet = await controller.completion_packet_details([
        ChangedFile(path="large.py", status="A"),
        ChangedFile(path="missing.py", status="A"),
    ])
    limits = packet["diff_packet_limits"]
    assert limits.materially_truncated
    assert limits.omitted_changed_files == ["missing.py"]
    assert "final file context exceeded 8000 characters" in limits.truncation_reason
    assert packet["changed_file_contexts"][0].context_truncated


@pytest.mark.parametrize("size,truncated", [(12000, False), (12001, True)])
async def test_diff_truncation_boundary_counts_input_not_marker(
    monkeypatch, tmp_path: Path, size: int, truncated: bool
) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    controller.use_git_diff = True

    async def is_git():
        return True

    async def file_diff(path):
        return "x" * size

    controller._is_git_work_tree = is_git
    controller._changed_file_diff = file_diff
    monkeypatch.setattr(
        controller_module, "_read_workspace_file",
        lambda root, path, *, limit: controller_module._BoundedFileText("short final context", False),
    )
    packet = await controller.completion_packet_details([ChangedFile(path="app.py", status="M")])
    assert packet["changed_file_diffs"][0].diff_truncated is truncated
    assert packet["diff_packet_limits"].materially_truncated is truncated


async def test_later_discovery_does_not_replace_earlier_overflow(monkeypatch, tmp_path: Path) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    monkeypatch.setattr(
        controller_module, "_read_workspace_file",
        lambda root, path, *, limit: controller_module._BoundedFileText("x", False),
    )
    controller.observed_changed_files = {
        f"src/file_{index}.py": ChangedFile(path=f"src/file_{index}.py", status="A", sequence=1)
        for index in range(501)
    }
    earlier = await controller.changed_files()
    del controller.observed_changed_files["src/file_500.py"]
    controller.observed_changed_files["later.py"] = ChangedFile(path="later.py", status="A", sequence=2)
    later = await controller.changed_files()
    assert earlier == later  # The included paths are identical, but the omissions differ.
    first_packet = await controller.completion_packet_details(earlier)
    second_packet = await controller.completion_packet_details(later)
    assert first_packet["diff_packet_limits"].omitted_changed_files == ["src/file_500.py"]
    assert second_packet["diff_packet_limits"].omitted_changed_files == ["later.py"]


async def test_delivery_waiting_on_activity_barrier_observes_pause_and_resume(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    delivered = []

    async def steer(message):
        delivered.append(message)
        return "turn"

    controller.coder = SimpleNamespace(thread_id=store.get_bello_config().coder_thread_id, steer_or_start=steer)
    lock = controller._coder_activity_lock()
    await lock.acquire()
    pending = asyncio.create_task(controller._deliver_coder_message("before pause"))
    await asyncio.sleep(0)
    controller.paused = True
    lock.release()
    assert await pending == (False, None)
    assert delivered == []
    controller.paused = False
    assert await controller._deliver_coder_message("after resume") == (True, "turn")
    assert delivered == ["after resume"]


async def test_delayed_delivery_cannot_claim_a_replacement_coder(tmp_path: Path) -> None:
    controller, store, _ = _runtime_controller(tmp_path)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def steer(message):
        started.set()
        await finish.wait()
        return "old-turn"

    controller.coder = SimpleNamespace(thread_id=store.get_bello_config().coder_thread_id, steer_or_start=steer)
    delivery = asyncio.create_task(controller._deliver_coder_message("continue"))
    await started.wait()
    controller.coder = SimpleNamespace(thread_id="replacement")
    finish.set()
    assert await delivery == (False, "old-turn")
    assert controller.coder.thread_id == "replacement"


async def test_transport_retry_budget_uses_patched_constant_and_preserves_evidence(
    monkeypatch, tmp_path: Path
) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    attempts = []
    ledger = controller.validations
    controller._transport_error_pending = True

    async def restart():
        attempts.append("restart")
        raise OSError("synthetic transport failure")

    monkeypatch.setattr(controller_module, "APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS", 2)
    monkeypatch.setattr(controller_module, "APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS", (0,))
    controller._restart_app_server_client = restart
    assert not await controller._recover_app_server_transport("broken pipe")
    assert attempts == ["restart", "restart"]
    assert controller.validations is ledger
    assert not controller._transport_recovery_mutex().locked()


async def test_reviewer_child_cleanup_keeps_depth_order_after_archive_failure(tmp_path: Path) -> None:
    controller, _, _ = _runtime_controller(tmp_path)
    calls = []

    async def refresh(*args):
        return None

    async def interrupt(thread_id, turn_id):
        calls.append(("interrupt", thread_id))

    async def archive(thread_id):
        calls.append(("archive", thread_id))
        if thread_id == "grandchild":
            raise OSError("synthetic archive failure")

    async def unsubscribe(thread_id):
        calls.append(("unsubscribe", thread_id))

    controller.client = SimpleNamespace(turn_interrupt=interrupt, thread_archive=archive, thread_unsubscribe=unsubscribe)
    controller._refresh_reviewer_subagents = refresh
    controller._subagents = {
        "child": SubagentRuntimeState("child", parent_thread_id="reviewer", active_turn_id="child-turn"),
        "grandchild": SubagentRuntimeState("grandchild", parent_thread_id="child", active_turn_id="nested-turn"),
    }
    await controller._cleanup_completion_reviewer_descendants("reviewer", tmp_path)
    assert calls == [
        ("interrupt", "grandchild"), ("archive", "grandchild"), ("unsubscribe", "grandchild"),
        ("interrupt", "child"), ("archive", "child"),
    ]
    assert all(state.status == "shutdown" and state.active_turn_id is None for state in controller._subagents.values())


async def test_completion_timeout_retry_closes_session_and_respects_budget(tmp_path: Path) -> None:
    controller, _, reviewer = _runtime_controller(tmp_path)
    controller.completion_supervisor = reviewer
    assert await controller._handle_completion_review_timeout_failure(message="timeout", summary="review")
    assert reviewer.closed_completion_reviews == 1
    assert controller._supervisor_next_completion_check.completion_review
    assert not await controller._handle_completion_review_timeout_failure(message="timeout again", summary="review")
    assert reviewer.closed_completion_reviews == 1
