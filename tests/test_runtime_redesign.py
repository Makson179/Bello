"""Neutral non-gating runtime delivery, advisory policy and restart health."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from pydantic import ValidationError
from supervisor.appserver import AppServerMessage
from supervisor.approvals import normalize_approval_request
from supervisor.approval_triage import cheap_runtime_packet
from supervisor.controller_parts import runtime_review
from supervisor.schemas import AppEventSource, ChangedFile, CoderMessage, HumanMessage, SupervisorDecision, SupervisorWakePacket
from supervisor.state import DECISIONS, HANDOFF, PROGRESS
from tests.support.controller import _CheapRuntimeNoopReviewer, _runtime_controller, _runtime_failure_validation, _runtime_validation_packet

@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("supervisor.controller.time.monotonic", lambda: clock[0])
    controller, store, reviewer = _runtime_controller(tmp_path)
    store.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": True, "last_event_sequence": 10}))
    controller.coder = SimpleNamespace(thread_id="thread", active_turn_id=None, steer_or_start=AsyncMock(return_value="turn"))
    controller.restart, controller.pause = AsyncMock(), AsyncMock()
    return controller, store, reviewer, clock

def packet(wake=11, **kwargs):
    return SupervisorWakePacket(wake_sequence=wake, latest_event_sequence=wake-1, generation=0, restart_count=0,
        task_path="neutral-task.md", task_contents="Implement a neutral function.", coder_thread_id="thread", goal=True, **kwargs)

def correction(wake=11, kind="intervene", **kwargs):
    return SupervisorDecision(decision=kind, generation=0, wake_sequence=wake,
        reason="A neutral behavior merits attention.", message_to_coder="Check the neutral edge case.", **kwargs)

def advice(wake=11, key="same-neutral-approach"):
    return SupervisorDecision(decision="noop", generation=0, wake_sequence=wake, advisory_key=key,
        advisory_to_coder="A04 repeats A01. Consider a different approach; keep this one if justified. Creativity helps.")

async def apply(controller, decision, wake=None):
    return await controller.apply_supervisor_decision(decision, packet_thread_id="thread", packet=wake or packet(decision.wake_sequence or 11))

def read_text(store, name):
    path = store.path(name)
    return path.read_text() if path.exists() else ""

def test_schema_has_no_model_supplied_proof_fields():
    assert not {"observed_files", "evidence_sequences"} & set(SupervisorDecision.model_fields)

def test_host_review_clock_is_not_serialized_to_model_or_audit_packet():
    wake = packet(runtime_review_started_monotonic=123.0)
    assert wake.runtime_review_started_monotonic == 123.0
    assert "runtime_review_started_monotonic" not in wake.model_dump()
    assert "runtime_review_started_monotonic" not in wake.model_dump_json()

async def test_no_file_or_event_proof_required_to_deliver(rig):
    c, s, _, _ = rig
    assert await apply(c, correction())
    c.coder.steer_or_start.assert_awaited_once_with(correction().message_to_coder)
    assert s.get_health().interventions == 1
    assert c.prior_interventions[-1].authority == "runtime_observation"
    assert c.prior_interventions[-1].status == "historical_not_binding"

async def test_no_packet_is_not_a_proof_failure(rig):
    c, s, _, _ = rig
    assert await c.apply_supervisor_decision(correction(), packet_thread_id="thread", packet=None)
    c.coder.steer_or_start.assert_awaited_once()
    assert s.get_health().interventions == 1

async def test_no_delivery_time_workspace_read_hash_or_scan(rig, tmp_path, monkeypatch):
    c, _, _, _ = rig
    c.changed_files = AsyncMock(side_effect=AssertionError("No delivery workspace scan."))
    source = tmp_path / "neutral.py"
    source.write_text("neutral\n")
    original = Path.open
    def guarded(path, *args, **kwargs):
        if path == source: pytest.fail("No source read/hash at delivery.")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    assert await apply(c, correction())
    c.changed_files.assert_not_awaited()
    c.coder.steer_or_start.assert_awaited_once()

async def test_wake_identity_dedup_remains_but_content_hash_dedup_removed(rig):
    c, s, _, _ = rig
    assert await apply(c, correction())
    assert not await apply(c, correction())
    assert await apply(c, correction(12))
    assert c.coder.steer_or_start.await_count == 2
    assert s.get_health().interventions == 2

@pytest.mark.parametrize("elapsed,changed,expected", [(59, False, False), (60, False, True), (0, True, True), (70, True, True)])
async def test_stale_notice_is_nonblocking_and_added_once(rig, elapsed, changed, expected):
    c, s, _, clock = rig
    wake = packet(runtime_review_started_monotonic=0.0)
    clock[0] = elapsed
    if changed: c.observed_changed_files = {"neutral.py": ChangedFile(path="neutral.py", status="modified", sequence=12)}
    assert await apply(c, correction(), wake)
    message = c.coder.steer_or_start.await_args.args[0]
    assert correction().message_to_coder in message
    assert message.count(runtime_review.RUNTIME_STALENESS_NOTICE) == int(expected)
    assert s.get_health().interventions == 1

@pytest.mark.parametrize("kind", ["file", "passing_validation", "elapsed"])
async def test_progress_during_activity_lock_does_not_cancel_message(rig, kind):
    c, s, _, clock = rig
    lock = c._coder_activity_lock()
    await lock.acquire()
    pending = asyncio.create_task(apply(c, correction(), packet(runtime_review_started_monotonic=0.0)))
    try:
        for _ in range(3): await asyncio.sleep(0)
        assert not pending.done()
        if kind == "file": c.observed_changed_files = {"neutral.py": ChangedFile(path="neutral.py", status="modified", sequence=12)}
        elif kind == "passing_validation": c.validations.append(_runtime_failure_validation(sequence=12, output="neutral pass", trusted_outcome="passed"))
        else: clock[0] = 65
    finally: lock.release()
    assert await pending
    c.coder.steer_or_start.assert_awaited_once()
    message = c.coder.steer_or_start.await_args.args[0]
    assert correction().message_to_coder in message
    if kind in {"file", "elapsed"}: assert message.count(runtime_review.RUNTIME_STALENESS_NOTICE) == 1
    assert s.get_health().interventions == 1

@pytest.mark.parametrize("identity", ["generation", "thread"])
async def test_run_identity_still_guards_delivery(rig, identity):
    c, _, _, _ = rig
    decision = correction().model_copy(update={"generation": 1}) if identity == "generation" else correction()
    assert not await c.apply_supervisor_decision(decision, packet_thread_id="other" if identity == "thread" else "thread", packet=packet())
    c.coder.steer_or_start.assert_not_awaited()

async def test_pause_without_defect_proof_is_not_evidence_gated(rig):
    c, s, _, _ = rig
    assert await apply(c, correction(kind="pause"))
    c.pause.assert_awaited_once()
    c.coder.steer_or_start.assert_not_awaited()
    assert s.get_health().interventions == 0

async def test_repeated_rejected_restarts_create_no_progress_constraints_or_health(rig):
    c, s, _, _ = rig
    before = s.get_health().model_dump()
    previous = {name: read_text(s, name) for name in (PROGRESS, DECISIONS)}
    s.write_text_locked(HANDOFF, "Keep original handoff.")
    for wake in range(11, 16):
        assert await apply(c, correction(wake, "restart", persistent_decision="Ban valid workflow.", progress_update="Not real progress.", clear_handoff=True))
    c.restart.assert_not_awaited()
    c.coder.steer_or_start.assert_not_awaited()
    assert s.get_health().model_dump() == before
    assert not c.prior_interventions
    assert {name: read_text(s, name) for name in previous} == previous
    assert read_text(s, HANDOFF) == "Keep original handoff."

async def test_restart_requires_new_same_failure_after_two_real_corrections(rig):
    c, s, _, _ = rig
    for index in (1, 2, 3):
        seq = index*10
        validation = _runtime_failure_validation(sequence=seq, output="neutral assertion failed", validation_id=f"v-{index}")
        c.validations.append(validation)
        s.update_bello_config(lambda cfg: cfg.model_copy(update={"last_event_sequence": seq}))
        assert await apply(c, correction(seq+1, "intervene" if index < 3 else "restart"), _runtime_validation_packet(validation, wake_sequence=seq+1))
    assert c.coder.steer_or_start.await_count == 2
    c.restart.assert_awaited_once()
    assert s.get_health().interventions == s.get_health().restart_issue_interventions == 2

async def test_one_failure_repeated_wakes_cannot_manufacture_recurrence(rig):
    c, s, _, _ = rig
    validation = _runtime_failure_validation(sequence=10, output="neutral assertion failed", validation_id="one-result")
    c.validations = [validation]
    for wake in (11, 12, 13, 14):
        assert await apply(c, correction(wake), _runtime_validation_packet(validation, wake_sequence=wake))
    assert c.coder.steer_or_start.await_count == 4
    assert s.get_health().restart_issue_interventions == 1
    c.restart.assert_not_awaited()

@pytest.mark.parametrize("changed_while_waiting", [False, True])
async def test_actual_restart_after_activity_lock_has_no_file_freshness_gate(rig, changed_while_waiting):
    c, s, _, _ = rig
    for index in (1, 2, 3):
        seq = index * 10
        validation = _runtime_failure_validation(sequence=seq, output="neutral failed", validation_id=f"restart-v-{index}")
        c.validations.append(validation)
        s.update_bello_config(lambda cfg: cfg.model_copy(update={"last_event_sequence": seq}))
        if index < 3:
            assert await apply(c, correction(seq+1), _runtime_validation_packet(validation, wake_sequence=seq+1))
    del c.restart
    c._wait_for_revision_switch = AsyncMock()
    c._restart_after_activity_barrier = AsyncMock()
    lock = c._coder_activity_lock()
    await lock.acquire()
    pending = asyncio.create_task(apply(c, correction(31, "restart"), _runtime_validation_packet(validation, wake_sequence=31)))
    try:
        for _ in range(3): await asyncio.sleep(0)
        if changed_while_waiting:
            c.observed_changed_files = {"neutral.py": ChangedFile(path="neutral.py", status="modified", sequence=32)}
    finally: lock.release()
    assert await pending
    c._restart_after_activity_barrier.assert_awaited_once()
    assert s.get_health().interventions == 2

async def test_runtime_finding_is_not_permanent_constraint(rig):
    c, s, _, _ = rig
    before = read_text(s, DECISIONS)
    assert await apply(c, correction(persistent_decision="Never edit again."))
    assert read_text(s, DECISIONS) == before

async def test_human_direction_remains_exact_original(rig):
    c, s, _, clock = rig
    human = HumanMessage(text="Keep public interface.", sequence=10)
    decision = SupervisorDecision(decision="intervene", generation=0, wake_sequence=11, message_to_coder=human.text, persistent_decision="Invented restriction.")
    clock[0] = 120
    assert await apply(c, decision, packet(human_message=human, runtime_review_started_monotonic=0.0))
    c.coder.steer_or_start.assert_awaited_once_with(human.text)
    assert human.text in read_text(s, DECISIONS)
    assert "Invented restriction" not in read_text(s, DECISIONS)

async def test_explicit_human_pause_is_not_steered(rig):
    c, s, _, _ = rig
    human = HumanMessage(text="Pause now.", sequence=10)
    decision = SupervisorDecision(decision="pause", generation=0, wake_sequence=11, message_to_coder=human.text)
    assert await apply(c, decision, packet(human_message=human))
    c.pause.assert_awaited_once_with()
    c.coder.steer_or_start.assert_not_awaited()
    assert s.get_health().interventions == 0

@pytest.mark.parametrize("route", ["normal", "cheap", "approval"])
async def test_resolved_reviewer_advice_is_audit_not_future_constraint(rig, route, tmp_path):
    c, s, reviewer, _ = rig
    imperative = "HISTORICAL_NEUTRAL_CANARY: stop editing before validation."
    failed = _runtime_failure_validation(sequence=10, output="neutral failed")
    c.validations = [failed]
    assert await apply(c, correction().model_copy(update={"message_to_coder": imperative}), packet(validations=[failed]))
    c.validations.append(_runtime_failure_validation(sequence=11, output="neutral passed", trusted_outcome="passed"))
    c._sequence = 11
    s.update_bello_config(lambda cfg: cfg.model_copy(update={"last_event_sequence": 11}))
    if route == "approval":
        context = normalize_approval_request(AppServerMessage({"id": 42, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread", "turnId": "turn", "itemId": "neutral", "command": "pytest tests/test_neutral.py", "cwd": str(tmp_path), "availableDecisions": ["accept", "decline"]}}))
        c.pending_approvals[context.server_request_id] = context
        await c.decide_approval(context, "neutral approval")
        current = reviewer.runtime_packets[-1]
    else:
        if route == "cheap":
            cheap = _CheapRuntimeNoopReviewer()
            c.runtime_triage_reviewer, c.runtime_triage_config = cheap, SimpleNamespace(model=cheap.model)
        await c._run_supervisor_check("Neutral runtime status", None, None, None, None)
        current = cheap.calls[-1] if route == "cheap" else reviewer.runtime_packets[-1]
        if route == "cheap":
            assert current.runtime_review_started_monotonic is None
            slim = cheap_runtime_packet(current)
            assert slim["prior_interventions"] == [] and slim["latest_validation_request"] is None
            assert not slim["routing_signals"]["stale_edit_after_stop_and_validate"]
            assert imperative not in str(slim)
    assert current.prior_interventions == []
    assert imperative not in current.model_dump_json()
    assert c.prior_interventions[-1].message_to_coder == imperative

@pytest.mark.parametrize("illegal", [{"decision": "intervene"}, {"message_to_coder": "Obey."}, {"persistent_decision": "Rule."}, {"progress_update": "Done."}, {"clear_handoff": True}])
def test_advice_cannot_also_carry_binding_correction(illegal):
    with pytest.raises(ValidationError): SupervisorDecision.model_validate({"decision": "noop", "advisory_to_coder": "Optional.", **illegal})

async def test_proofless_advice_is_nonbinding_and_has_no_health_sideeffects(rig):
    c, s, _, _ = rig
    before = s.get_health().model_dump()
    previous = {name: read_text(s, name) for name in (PROGRESS, DECISIONS)}
    assert await apply(c, advice())
    assert "Optional Goal observation (not a requirement;" in c.coder.steer_or_start.await_args.args[0]
    assert s.get_health().model_dump() == before
    assert {name: read_text(s, name) for name in previous} == previous
    assert not c.prior_interventions
    c.restart.assert_not_awaited(); c.pause.assert_not_awaited()

async def test_slow_goal_advice_delivered_once_with_nonblocking_notice(rig):
    c, s, _, clock = rig
    before = s.get_health().model_dump()
    clock[0] = 90
    assert await apply(c, advice(), packet(runtime_review_started_monotonic=0.0))
    message = c.coder.steer_or_start.await_args.args[0]
    assert advice().advisory_to_coder in message
    assert message.count(runtime_review.RUNTIME_STALENESS_NOTICE) == 1
    assert s.get_health().model_dump() == before

async def test_advice_cooldown_and_key_dedup_not_source_hash(rig):
    c, _, _, clock = rig
    assert await apply(c, advice())
    clock[0] = 1799
    assert await apply(c, advice(12, "another"))
    assert c.coder.steer_or_start.await_count == 1
    clock[0] = 1800
    assert await apply(c, advice(13, "another"))
    assert c.coder.steer_or_start.await_count == 2
    clock[0] = 9000
    assert await apply(c, advice(14))
    assert c.coder.steer_or_start.await_count == 2

@pytest.mark.parametrize("disabled", ["config", "packet", "no_key"])
async def test_advice_requires_goal_and_key(rig, disabled):
    c, s, _, _ = rig
    decision, wake = advice(), packet()
    if disabled == "config": s.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": False}))
    elif disabled == "packet": wake = wake.model_copy(update={"goal": False})
    else: decision = decision.model_copy(update={"advisory_key": None})
    assert await apply(c, decision, wake)
    c.coder.steer_or_start.assert_not_awaited()

async def test_periodic_goal_review_reaches_full_reviewer(rig):
    c, s, reviewer, clock = rig
    c._record_goal_work(1); c._maybe_schedule_goal_progress_review()
    assert c._supervisor_task is None
    clock[0] = 1199
    c._record_goal_work(2); c._maybe_schedule_goal_progress_review()
    assert c._supervisor_task is None
    clock[0] = 1200
    c._maybe_schedule_goal_progress_review(); await c._supervisor_task
    assert len(reviewer.runtime_packets) == 1 and reviewer.runtime_packets[0].goal_progress_review
    assert reviewer.runtime_packets[0].runtime_review_started_monotonic == 1200
    assert runtime_review.GOAL_PROGRESS_REVIEW_SUMMARY in reviewer.runtime_packets[0].current_summary
    clock[0] = 2400
    c._maybe_schedule_goal_progress_review()
    assert len(reviewer.runtime_packets) == 1
    c._sequence = 12
    s.update_bello_config(lambda cfg: cfg.model_copy(update={"last_event_sequence": 12}))
    c._record_goal_work(12); c._maybe_schedule_goal_progress_review(); await c._supervisor_task
    assert len(reviewer.runtime_packets) == 2

@pytest.mark.parametrize("blocker", ["non_goal", "paused", "stopped", "approval", "status_only"])
async def test_periodic_review_still_respects_lifecycle(rig, blocker):
    c, s, reviewer, clock = rig
    if blocker != "status_only": c._record_goal_work(1)
    c._maybe_schedule_goal_progress_review()
    if blocker == "non_goal": s.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": False}))
    elif blocker == "paused": c.paused = True
    elif blocker == "stopped": c.running = False
    elif blocker == "approval": c.pending_approvals["neutral"] = object()
    else: c._append_event(AppEventSource.APP_SERVER, "thread/status/changed", thread_id="thread")
    clock[0] = 9000
    c._maybe_schedule_goal_progress_review()
    assert c._supervisor_task is None and reviewer.runtime_packets == []

async def test_goal_idle_never_nudges_or_reviews_without_readiness(rig):
    c, _, reviewer, clock = rig
    c._generation_has_coder_turn = True
    await c._handle_no_marker_idle(subagents_refreshed=True)
    clock[0] = 2399
    await c._handle_no_marker_idle(subagents_refreshed=True)
    c.coder.steer_or_start.assert_not_awaited()
    clock[0] = 2400
    await c._handle_no_marker_idle(subagents_refreshed=True)
    assert c.coder.steer_or_start.await_count == 0 and c._supervisor_task is None
    assert reviewer.completion_packets == [] and getattr(c, '_no_marker_idle_since', None) is None
    clock[0] = 2401
    await c._handle_no_marker_idle(subagents_refreshed=True)
    assert c.coder.steer_or_start.await_count == 0
    clock[0] = 86400
    await c._handle_controller_idle_guard()
    c.coder.steer_or_start.assert_not_awaited()
    assert reviewer.completion_packets == []

@pytest.mark.parametrize("state", ["busy", "unknown"])
async def test_goal_idle_does_not_nudge_unconfirmed_thread(rig, state):
    c, _, reviewer, clock = rig
    c._generation_has_coder_turn = True
    c.client.read_execution_status.return_value = {"state": state}
    await c._handle_no_marker_idle(subagents_refreshed=True)
    clock[0] = 2400
    await c._handle_no_marker_idle(subagents_refreshed=True)
    c.coder.steer_or_start.assert_not_awaited()
    assert reviewer.completion_packets == []

@pytest.mark.parametrize("explicit", [False, True])
async def test_completion_still_runs_for_non_goal_idle_or_explicit_readiness(rig, explicit):
    c, s, reviewer, clock = rig
    c._generation_has_coder_turn = True
    if explicit:
        c.last_coder_message = CoderMessage(text="BELLO_READY_FOR_REVIEW", sequence=10)
        await c._handle_coder_turn_completed(item_id=None)
    else:
        s.update_bello_config(lambda cfg: cfg.model_copy(update={"goal": False}))
        await c._handle_no_marker_idle(subagents_refreshed=True)
        clock[0] = 2400
        await c._handle_no_marker_idle(subagents_refreshed=True)
    await c._supervisor_task
    assert len(reviewer.completion_packets) == 1
