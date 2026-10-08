"""CoderLifecycle service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class CoderLifecycleState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _active_provider_phase: str
    _coder_activity_mutex: compat.asyncio.Lock | None
    _coder_snapshot: compat.WorkspaceSnapshot | None
    _coder_started: bool
    _coder_watch: compat._ActiveCoderWatch
    _current_turn_action_count: int
    _generation_has_coder_turn: bool
    _idle_guard_fired_for_sequence: int | None
    _last_controller_activity_monotonic: float
    _restart_transition_token: object | None
    _revision_switch_done: compat.asyncio.Future[None] | None
    _revision_switch_in_progress: bool
    _revision_switch_owner: compat.asyncio.Task[Any] | None
    _snapshot_patch_applied: bool
    _transport_error_pending: bool
    _transport_recovery_lock: compat.asyncio.Lock | None
    _transport_recovery_total: int
    coder: compat.CoderSession | None
    last_coder_message: compat.CoderMessage | None
    workspace_plan_path: compat.Path | None
    workspace_root: compat.Path
    workspace_task_path: compat.Path


class CoderLifecyclePort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_abandon_dead_completion_review',
        '_active_adversary_thread_id',
        '_active_adversary_workspace_root',
        '_active_coder_intelligence',
        '_active_coder_model',
        '_active_coder_plan_path',
        '_active_coder_subagents',
        '_active_coder_watch',
        '_active_dependency_roots',
        '_active_supervisor_check',
        '_active_task_path',
        '_active_workspace_root',
        '_adversary_denied_commands',
        '_adversary_reservation_recovery_pending',
        '_append_cleanup_error',
        '_append_event',
        '_canonical_task_contents',
        '_canonical_task_hash',
        '_canonical_task_text',
        '_clear_persisted_coder_turn',
        '_close_completion_review_session',
        '_coder_activity_lock',
        '_coder_lifecycle_accepts_activity',
        '_completion_supervisor_agent',
        '_deferred_completion_check',
        '_discard_uncommitted_revision_thread',
        '_fast_mode',
        '_finalizing',
        '_handle_active_coder_guard',
        '_handle_coder_turn_completed',
        '_handle_no_marker_idle',
        '_interrupt_stale_revision_turn',
        '_last_completion_marker_sequence',
        '_last_restart_budget_signature',
        '_multi_agent_config',
        '_no_marker_completion_review_key',
        '_pending_adversary_report',
        '_pending_runtime_trigger_actions',
        '_pending_runtime_trigger_signatures',
        '_perform_revision_coder_switch',
        '_quiesce_coder_tree',
        '_record_cancelled_revision_switch',
        '_recover_app_server_transport',
        '_recover_coder_thread_after_transport',
        '_repair_snapshot_runtime_controls',
        '_resolve_pending_approvals',
        '_restart_after_activity_barrier',
        '_restart_app_server_client',
        '_restart_transition_is_current',
        '_reviewer_thread_ids',
        '_reviewer_thread_roles',
        '_revision_coder_active',
        '_revision_coder_intelligence',
        '_revision_coder_model',
        '_revision_switch_context_is_current',
        '_rollback_interrupted_adversary_reservation',
        '_runtime_apply_retry_count',
        '_runtime_decision_retry_count',
        '_runtime_integrity_issue',
        '_schedule_supervisor_check',
        '_sequence',
        '_start_fallback_recovery_coder',
        '_stop_supervisor_task',
        '_subagent_policy_notified',
        '_subagent_registry',
        '_subagents',
        '_supervisor_next_completion_check',
        '_supervisor_next_completion_summary',
        '_supervisor_next_runtime_check',
        '_supervisor_next_runtime_summary',
        '_supervisor_task',
        '_sync_legacy_supervisor_queue_fields',
        '_terminal_cleanup_started',
        '_transport_recovery_mutex',
        '_uses_coder_snapshot',
        '_wait_for_coder_activity',
        '_wait_for_revision_switch',
        '_write_run_checkpoint',
        'client',
        'completion_review_return_sequence',
        'declared_grading_roots',
        'event_queue',
        'fail_provider',
        'finalize',
        'no_marker_idle_nudge_count',
        'paused',
        'pending_approvals',
        'plan_path',
        'prior_interventions',
        'project_root',
        'running',
        'store',
        'task_path',
        'tui',
        'validation_runtime_state',
    })
    writes = frozenset({
        '_active_adversary_thread_id',
        '_active_adversary_workspace_root',
        '_adversary_denied_commands',
        '_adversary_reservation_recovery_pending',
        '_deferred_completion_check',
        '_last_completion_marker_sequence',
        '_last_restart_budget_signature',
        '_no_marker_completion_review_key',
        '_pending_adversary_report',
        '_pending_runtime_trigger_actions',
        '_pending_runtime_trigger_signatures',
        '_reviewer_thread_ids',
        '_reviewer_thread_roles',
        '_runtime_apply_retry_count',
        '_runtime_decision_retry_count',
        '_subagent_policy_notified',
        '_subagents',
        '_supervisor_next_completion_check',
        '_supervisor_next_completion_summary',
        '_supervisor_next_runtime_check',
        '_supervisor_next_runtime_summary',
        '_supervisor_task',
        'completion_review_return_sequence',
        'no_marker_idle_nudge_count',
        'paused',
        'prior_interventions',
        'validation_runtime_state',
    })


class CoderLifecycle:
    """Own coder lifecycle behavior; borrow only the declared port."""

    def __init__(self, ports: CoderLifecyclePort) -> None:
        self.state = CoderLifecycleState()
        self.ports = ports

    def _active_workspace_root(self) -> compat.Path:
        return compat.Path(getattr(self.state, "workspace_root", self.ports.project_root)).resolve()

    def _active_dependency_roots(self) -> tuple[compat.Path, ...]:
        snapshot = getattr(self.state, "_coder_snapshot", None)
        return getattr(snapshot, "readonly_dependency_roots", ())

    def _active_task_path(self) -> compat.Path:
        return compat.Path(getattr(self.state, "workspace_task_path", self.ports.task_path)).resolve()

    def _active_coder_plan_path(self) -> compat.Path | None:
        if self.ports._revision_coder_active():
            return None
        plan_path = getattr(self.state, "workspace_plan_path", None)
        if plan_path is None:
            return None
        return compat.Path(plan_path).absolute()

    def _canonical_task_text(self) -> str:
        return getattr(self.ports, "_canonical_task_contents", compat._read_task_text(self.ports.task_path))

    def _immutable_approval_paths(self) -> tuple[compat.Path, ...]:
        snapshot = getattr(self.state, "_coder_snapshot", None)
        plan_path = getattr(self.ports, "plan_path", None)
        if snapshot is not None:
            paths = [snapshot.original_root, self.ports.task_path]
            if plan_path is not None:
                paths.append(compat.Path(plan_path))
            return tuple(paths)
        task_path = getattr(self.ports, "task_path", None)
        paths = [compat.Path(task_path)] if task_path is not None else []
        if plan_path is not None:
            paths.append(compat.Path(plan_path))
        return tuple(paths)

    def _task_integrity_issue(self) -> str | None:
        expected_hash = getattr(self.ports, "_canonical_task_hash", None)
        if expected_hash:
            try:
                current_hash = compat._hash_file(self.ports.task_path)
            except OSError:
                return "the original task file is missing or unreadable"
            if current_hash != expected_hash:
                return "the original task file changed after the run started"
        snapshot = getattr(self.state, "_coder_snapshot", None)
        if snapshot is None:
            return None
        return snapshot.task_integrity_issue()

    def _runtime_integrity_issue(self) -> str | None:
        snapshot = getattr(self.state, "_coder_snapshot", None)
        if snapshot is None:
            return None
        return snapshot.plan_integrity_issue() or snapshot.runtime_integrity_issue()

    async def _escalate_runtime_integrity_issue(self, *, source: str) -> bool:
        issue = self.ports._runtime_integrity_issue()
        if issue is None:
            return False
        message = f"coder workspace runtime integrity failure ({source}): {issue}"
        self.ports.tui.render("INTEGRITY", message)
        self.ports.store.append_text_locked(compat.PROGRESS, f"- Integrity failure: {message}\n")
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "integrity/runtime_control_mutation",
            reason=message,
        )
        await self.ports.finalize(f"escalated: {message}", status=compat.BelloStatus.ESCALATED)
        return True

    def _repair_snapshot_runtime_controls(self, *, source: str) -> tuple[str, ...]:
        snapshot = getattr(self.state, "_coder_snapshot", None)
        if snapshot is None:
            return ()
        repaired = list(snapshot.restore_runtime_links())
        if snapshot.restore_git_control():
            repaired.append("git_config")
        if not repaired:
            return ()
        detail = ", ".join(repaired)
        message = f"restored replaced coder workspace runtime control(s): {detail} ({source})"
        self.ports.store.append_text_locked(compat.PROGRESS, f"- Integrity guard: {message}.\n")
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "coder_workspace_runtime_controls_restored",
                "source": source,
                "repaired": list(repaired),
            }
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "integrity/runtime_controls_restored",
            reason=message,
        )
        cfg = self.ports.store.get_bello_config()
        compat.patch_health(
            self.ports.store,
            compat.HealthDelta(generation=cfg.generation, add_risk_signals=["runtime_control_replacement"]),
        )
        self.ports.tui.render("INTEGRITY", message)
        return tuple(repaired)

    def _uses_coder_snapshot(self) -> bool:
        return compat.coder_sandbox_mode() == compat.CODER_SANDBOX_WORKSPACE_WRITE

    def _prepare_coder_workspace(self) -> None:
        if not self.ports._uses_coder_snapshot():
            if self.ports.plan_path is not None:
                raise compat.WorkspaceSnapshotError(
                    "--plan requires the default workspace-write coder snapshot so independent reviewers can remain plan-blind"
                )
            self.state.workspace_root = self.ports.project_root
            self.state.workspace_task_path = self.ports.task_path
            self.state.workspace_plan_path = None
            self.state._coder_snapshot = None
            return
        snapshot = compat.create_workspace_snapshot(
            self.ports.project_root,
            self.ports.task_path,
            plan_path=self.ports.plan_path,
            declared_grading_roots=getattr(self.ports, "declared_grading_roots", ()),
        )
        self.state._coder_snapshot = snapshot
        self.state.workspace_root = snapshot.snapshot_root
        self.state.workspace_task_path = snapshot.task_path
        self.state.workspace_plan_path = snapshot.plan_path
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "coder_workspace_snapshot_created",
                "snapshot_root": str(snapshot.snapshot_root),
                "original_root": str(snapshot.original_root),
                "rewritten_symlinks": [rewrite.path for rewrite in snapshot.rewritten_symlinks],
                "excluded_external_symlinks": list(snapshot.excluded_external_symlink_paths),
            }
        )

    def _coder_lifecycle_accepts_activity(
        self,
        cfg: compat.BelloConfig | None = None,
        *,
        require_running: bool = True,
    ) -> bool:
        cfg = cfg or self.ports.store.get_bello_config()
        return bool(
            (not require_running or getattr(self.ports, "running", True))
            and not getattr(self.ports, "paused", False)
            and not getattr(self.ports, "_finalizing", False)
            and not getattr(self.ports, "_terminal_cleanup_started", False)
            and cfg.status in {compat.BelloStatus.STARTING, compat.BelloStatus.RUNNING}
        )

    def _coder_activity_lock(self) -> compat.asyncio.Lock:
        mutex = getattr(self.state, "_coder_activity_mutex", None)
        if mutex is None:
            mutex = compat.asyncio.Lock()
            self.state._coder_activity_mutex = mutex
        return mutex

    async def _wait_for_coder_activity(self) -> None:
        async with self.ports._coder_activity_lock():
            return

    async def _deliver_coder_message(
        self,
        message: str,
        *,
        coder: compat.Any | None = None,
        force_new_turn: bool = False,
    ) -> tuple[bool, str | None]:
        async with self.ports._coder_activity_lock():
            cfg = self.ports.store.get_bello_config()
            target = coder if coder is not None else getattr(self.state, "coder", None)
            if (
                not self.ports._coder_lifecycle_accepts_activity(cfg)
                or target is None
                or target is not getattr(self.state, "coder", None)
                or getattr(target, "thread_id", cfg.coder_thread_id) != cfg.coder_thread_id
            ):
                return False, None
            if force_new_turn:
                result = await target.start_turn(message)
            else:
                result = await target.steer_or_start(message)
            current = self.ports.store.get_bello_config()
            delivered_to_current_lifecycle = bool(
                self.ports._coder_lifecycle_accepts_activity(current)
                and target is getattr(self.state, "coder", None)
                and getattr(target, "thread_id", current.coder_thread_id) == current.coder_thread_id
            )
            return delivered_to_current_lifecycle, result

    def _mark_controller_activity(self) -> None:
        self.state._last_controller_activity_monotonic = compat.time.monotonic()
        self.state._idle_guard_fired_for_sequence = None

    async def _handle_controller_idle_guard(self, *, now: float | None = None, force: bool = False) -> None:
        if not self.ports.running or getattr(self.ports, "paused", False) or getattr(self.ports, "_terminal_cleanup_started", False):
            return
        cfg = self.ports.store.get_bello_config()
        if cfg.active_coder_turn_id:
            await self.ports._handle_active_coder_guard(now=now)
            return
        coder = getattr(self.state, "coder", None)
        if coder is None:
            await self.ports.finalize(
                "controller idle guard: no active coder session, no pending approvals, and no supervisor check",
                status=compat.BelloStatus.PROVIDER_FAILURE,
            )
            return
        if getattr(coder, "active_turn_id", None):
            return
        if getattr(self.ports, "pending_approvals", None):
            return
        task = getattr(self.ports, "_supervisor_task", None)
        if task is not None and not task.done():
            return
        current_time = compat.time.monotonic() if now is None else now
        last_activity = getattr(self.state, "_last_controller_activity_monotonic", current_time)
        if not force and current_time - last_activity < compat.CONTROLLER_IDLE_GUARD_STALL_SECONDS:
            return
        sequence = cfg.last_event_sequence
        if getattr(self.state, "_idle_guard_fired_for_sequence", None) == sequence:
            return
        self.state._idle_guard_fired_for_sequence = sequence
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "controller_idle_guard",
                "sequence": sequence,
                "reason": "running with no active coder turn, pending approval, or supervisor check",
            }
        )
        await self.ports._handle_no_marker_idle()

    def _active_coder_watch(self, cfg: compat.BelloConfig | None = None, *, now: float | None = None) -> compat._ActiveCoderWatch | None:
        cfg = cfg or self.ports.store.get_bello_config()
        coder = getattr(self.state, "coder", None)
        if (not self.ports._coder_lifecycle_accepts_activity(cfg) or coder is None
                or not cfg.coder_thread_id or not cfg.active_coder_turn_id):
            return None
        identity = (cfg.generation, cfg.coder_thread_id, cfg.active_coder_turn_id, id(coder))
        watch = getattr(self.state, "_coder_watch", None)
        if watch is None or watch.identity != identity:
            watch = compat._ActiveCoderWatch(identity, compat.time.monotonic() if now is None else now)
            self.state._coder_watch = watch
        return watch

    def _record_coder_progress(self, method: str, params: dict[str, compat.Any], cfg: compat.BelloConfig) -> None:
        watch = self.ports._active_coder_watch(cfg)
        if watch is None:
            return
        item = params.get("item")
        item_id = compat._item_id_from_params(params)
        if isinstance(item, dict) and item_id:
            if (method == "item/started" and compat._is_completed_action(item)
                    and item.get("status") in {None, "inProgress"}
                    and (item.get("type") != "commandExecution" or item.get("command"))):
                watch.running_tools.add(item_id)
            elif method == "item/completed":
                watch.running_tools.discard(item_id)
        # A repeated item/started, token counter, or retry is not forward
        # progress. Real output and completed work break the retry streak.
        meaningful = method == "item/completed" or (
            compat._is_stream_delta_method(method) and bool(params.get("delta"))
        )
        if meaningful:
            watch.last_progress = compat.time.monotonic()
            watch.retry_since = None
            watch.last_error = ""
            watch.reported_stall = False

    async def _handle_active_coder_guard(self, *, now: float | None = None) -> None:
        current_time = compat.time.monotonic() if now is None else now
        watch = self.ports._active_coder_watch(now=current_time)
        if watch is None or getattr(self.state, "_transport_error_pending", False):
            return
        # Other engines keep their own lifecycle. The read-only reconciliation
        # below relies specifically on native Codex turn records.
        if compat.parse_model_selection(self.ports._active_coder_model() or compat.DEFAULT_MODEL).engine != "codex":
            return
        if (getattr(self.ports, "pending_approvals", None) or watch.running_tools
                or self.ports._active_coder_subagents()):
            # Waiting on an approved tool/user is not waiting for model retry.
            watch.retry_since = None
        retry_expired = (watch.retry_since is not None
                         and current_time - watch.retry_since >= compat.CODER_PROVIDER_RETRY_BUDGET_SECONDS)
        if (not retry_expired and current_time - watch.last_progress < compat.CODER_STATUS_PROBE_AFTER_SECONDS
                or current_time - watch.last_probe < compat.CONTROLLER_IDLE_GUARD_INTERVAL_SECONDS):
            return
        watch.last_probe = current_time
        progress_before_probe = watch.last_progress
        _, thread_id, turn_id, _ = watch.identity
        probe_error = None
        turn = None
        try:
            result = await compat.asyncio.wait_for(
                self.ports.client.thread_read(thread_id, include_turns=True,
                                        timeout=compat.CODER_STATUS_PROBE_TIMEOUT_SECONDS),
                timeout=compat.CODER_STATUS_PROBE_TIMEOUT_SECONDS,
            )
            thread = result.get("thread", {})
            if isinstance(thread, dict) and thread.get("id") == thread_id:
                turn = compat._thread_turn_by_id(thread, turn_id)
        except (compat.AppServerError, TimeoutError) as exc:
            probe_error = compat.sanitize_error_text(str(exc) or "thread/read timed out")
        if (self.ports._active_coder_watch() is not watch or watch.last_progress != progress_before_probe
                or not self.ports.event_queue.empty()):
            return  # Paused/restarted/completed or made progress during the RPC.
        if isinstance(turn, dict) and turn.get("status") in {"completed", "failed", "interrupted"}:
            reconcile = getattr(self.ports.client, "reconcile_terminal_turn", None)
            if reconcile is not None:
                try:
                    reconciled = await compat.asyncio.wait_for(reconcile(thread_id, turn_id, turn),
                                                        timeout=compat.CODER_STATUS_PROBE_TIMEOUT_SECONDS)
                    if reconciled and self.ports._active_coder_watch() is watch:
                        self.ports._append_event(compat.AppEventSource.APP_SERVER, "coder/completionRecovered",
                                           thread_id=thread_id, turn_id=turn_id,
                                           reason="Recovered terminal status from native thread/read; no task replay")
                        # The final agentMessage notification may have been lost
                        # along with completion. Restore it from the exact turn,
                        # not from other turns or an untrusted thread status.
                        text = compat.last_agent_message_text(turn)
                        if text and text.strip():
                            self.state.last_coder_message = compat.CoderMessage(text=text.strip(), sequence=self.ports._sequence)
                    return  # Normal queued turn/completed performs review exactly once.
                except (compat.AppServerError, TimeoutError) as exc:
                    probe_error = compat.sanitize_error_text(str(exc) or "turn reconciliation timed out")
                    # The provider did finish; a local cleanup problem must not
                    # be misreported as an exhausted model retry budget.
                    retry_expired = False
        if not watch.reported_stall:
            reason = ("Coder has no new progress; checking native turn status without restarting it. "
                      + (f"Status read failed: {probe_error}" if probe_error else
                         f"Native turn status: {turn.get('status', 'unknown') if turn else 'unknown'}"))
            self.ports._append_event(compat.AppEventSource.APP_SERVER, "coder/progressStalled", thread_id=thread_id,
                               turn_id=turn_id, reason=reason)
            self.ports.tui.render("SYSTEM", reason)
            watch.reported_stall = True
        if retry_expired:
            await self.ports.fail_provider(
                "Coder provider retry budget exceeded: no forward progress for "
                f"{compat.CODER_PROVIDER_RETRY_BUDGET_SECONDS:g}s after a retryable error. "
                f"No task replay was attempted. Last error: {watch.last_error}"
            )

    async def handle_transport_error(self, event: compat.ControllerEvent) -> None:
        message = event.error_message or str(event.error) or "app-server transport error"
        self.ports._append_event(compat.AppEventSource.APP_SERVER, "appServer/transportError", reason=message)
        if compat._is_recoverable_app_server_transport_error(message):
            recovered = await self.ports._recover_app_server_transport(message)
            if recovered:
                return
        self.state._transport_error_pending = False
        await self.ports.finalize(f"app-server transport error: {message}", status=compat.BelloStatus.PROVIDER_FAILURE)

    def _transport_recovery_mutex(self) -> compat.asyncio.Lock:
        lock = getattr(self.state, "_transport_recovery_lock", None)
        if lock is None:
            lock = compat.asyncio.Lock()
            self.state._transport_recovery_lock = lock
        return lock

    async def _recover_app_server_transport(self, message: str) -> bool:
        """Restart app-server and resume the current logical run in place."""

        async with self.ports._transport_recovery_mutex():
            if not self.ports._coder_lifecycle_accepts_activity(require_running=False):
                return False
            active_check = getattr(self.ports, "_active_supervisor_check", None)
            failed_phase = getattr(self.state, "_active_provider_phase", "unknown")
            self.ports._write_run_checkpoint(
                failed_phase,
                state="recovering",
                detail=message,
            )
            self.ports.tui.render(
                "SYSTEM",
                f"app-server transport lost during {failed_phase}; recovering",
            )
            self.ports.store.append_text_locked(
                compat.PROGRESS,
                f"- Provider recovery: app-server transport was lost during {failed_phase}; "
                "restarting the transport and preserving the current workspace.\n",
            )
            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                    "type": "app_server_transport_recovery_started",
                    "phase": failed_phase,
                    "message": message,
                    "coder_thread_id": self.ports.store.get_bello_config().coder_thread_id,
                    "active_coder_turn_id": self.ports.store.get_bello_config().active_coder_turn_id,
                }
            )

            supervisor_task = getattr(self.ports, "_supervisor_task", None)
            if supervisor_task is not None and supervisor_task is not compat.asyncio.current_task():
                await self.ports._stop_supervisor_task()
            self.ports._supervisor_task = None
            await self.ports._abandon_dead_completion_review()
            self.ports.pending_approvals.clear()
            self.ports.store.update_bello_config(
                lambda cfg: cfg.model_copy(update={"pending_server_request_ids": []})
            )
            self.ports._subagents = {}
            self.ports._subagent_policy_notified = set()
            self.ports._reviewer_thread_ids = compat.OrderedDict()
            self.ports._reviewer_thread_roles = {}
            self.ports._active_adversary_thread_id = None

            if failed_phase == "adversary" and not getattr(
                self.ports, "_adversary_reservation_recovery_pending", False
            ):
                self.ports._rollback_interrupted_adversary_reservation()
                self.ports._adversary_reservation_recovery_pending = True

            errors: list[str] = []
            for attempt in range(1, compat.APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS + 1):
                delay = compat.APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS[
                    min(attempt - 1, len(compat.APP_SERVER_TRANSPORT_RECOVERY_BACKOFF_SECONDS) - 1)
                ]
                if delay:
                    await compat.asyncio.sleep(delay)
                try:
                    await self.ports._restart_app_server_client()
                    await self.ports._recover_coder_thread_after_transport(
                        start_continuation=active_check is None,
                    )
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                    errors.append(error)
                    self.ports.store.append_raw_log(
                        {
                            "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                            "type": "app_server_transport_recovery_attempt_failed",
                            "attempt": attempt,
                            "phase": failed_phase,
                            "error": error,
                        }
                    )
                    continue

                self.state._transport_recovery_total = int(
                    getattr(self.state, "_transport_recovery_total", 0) or 0
                ) + 1
                self.state._transport_error_pending = False
                self.ports._adversary_reservation_recovery_pending = False
                self.ports._write_run_checkpoint(
                    "coder" if active_check is None else failed_phase,
                    state="stable",
                    detail=f"transport recovered on attempt {attempt}",
                )
                self.ports.store.append_text_locked(
                    compat.PROGRESS,
                    f"- Provider recovery complete: app-server resumed on attempt "
                    f"{attempt}/{compat.APP_SERVER_TRANSPORT_RECOVERY_ATTEMPTS}.\n",
                )
                self.ports.store.append_raw_log(
                    {
                        "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                        "type": "app_server_transport_recovered",
                        "attempt": attempt,
                        "phase": failed_phase,
                        "coder_thread_id": self.ports.store.get_bello_config().coder_thread_id,
                        "active_coder_turn_id": self.ports.store.get_bello_config().active_coder_turn_id,
                    }
                )
                self.ports.tui.render("SYSTEM", "app-server transport recovered")
                if active_check is not None and self.ports._coder_lifecycle_accepts_activity():
                    self.ports._schedule_supervisor_check(
                        active_check.summary,
                        triggering_item_id=active_check.triggering_item_id,
                        triggering_action=active_check.triggering_action,
                        human_message=active_check.human_message,
                        patch_summary=active_check.patch_summary,
                        completion_review=active_check.completion_review,
                    )
                return True

            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                    "type": "app_server_transport_recovery_exhausted",
                    "phase": failed_phase,
                    "errors": errors,
                }
            )
            return False

    async def _restart_app_server_client(self) -> None:
        client = self.ports.client
        restart = getattr(client, "restart", None)
        if callable(restart):
            await restart()
        else:
            await client.stop()
            await client.start()
        await client.initialize()

    async def _abandon_dead_completion_review(self) -> None:
        supervisor = self.ports._completion_supervisor_agent()
        if supervisor is None:
            return
        abandon = getattr(
            supervisor,
            "abandon_completion_review_after_transport_loss",
            None,
        )
        if callable(abandon):
            await abandon()
            return
        if hasattr(supervisor, "completion_thread_id"):
            supervisor.completion_thread_id = None

    def _rollback_interrupted_adversary_reservation(self) -> None:
        self.ports.store.update_bello_config(
            lambda cfg: cfg.model_copy(
                update={
                    "adversary_run_count": max(0, cfg.adversary_run_count - 1),
                }
            )
        )
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "interrupted_adversary_reservation_rolled_back",
            }
        )

    async def _recover_coder_thread_after_transport(
        self,
        *,
        start_continuation: bool,
    ) -> None:
        coder = getattr(self.state, "coder", None)
        cfg = self.ports.store.get_bello_config()
        if coder is None or not cfg.coder_thread_id:
            raise RuntimeError("no persisted coder thread is available for recovery")
        coder.thread_id = cfg.coder_thread_id
        coder.active_turn_id = cfg.active_coder_turn_id
        try:
            thread = await coder.resume_thread()
        except Exception as exc:
            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                    "type": "coder_thread_resume_failed",
                    "thread_id": cfg.coder_thread_id,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
            await self.ports._start_fallback_recovery_coder(
                coder,
                start_continuation=start_continuation,
            )
            return

        active_turn_id = cfg.active_coder_turn_id
        active_turn = compat._thread_turn_by_id(thread, active_turn_id)
        status = active_turn.get("status") if active_turn is not None else None
        if active_turn_id and active_turn is not None and status == "completed":
            text = compat.last_agent_message_text(active_turn)
            if text:
                self.ports._append_event(
                    compat.AppEventSource.APP_SERVER,
                    "transport/replayedCoderMessage",
                    thread_id=cfg.coder_thread_id,
                    turn_id=active_turn_id,
                    reason="replayed from thread/resume after transport loss",
                )
                self.state.last_coder_message = compat.CoderMessage(
                    text=text.strip(),
                    sequence=self.ports._sequence,
                )
                self.ports.tui.render("CODER", text.strip())
            coder.mark_turn_completed(active_turn_id)
            self.ports._write_run_checkpoint("coder_turn_complete", state="stable")
            await self.ports._handle_coder_turn_completed(item_id=None)
            return
        if active_turn_id and status == "inProgress":
            coder.active_turn_id = active_turn_id
            self.ports.store.update_bello_config(
                lambda current: current.model_copy(
                    update={"active_coder_turn_id": active_turn_id}
                )
            )
            return
        if active_turn_id:
            self.ports._clear_persisted_coder_turn(cfg.coder_thread_id, active_turn_id)
        if start_continuation:
            await coder.start_turn(compat.TRANSPORT_RECOVERY_CODER_PROMPT)

    async def _start_fallback_recovery_coder(
        self,
        previous: compat.CoderSession,
        *,
        start_continuation: bool,
    ) -> None:
        cfg = self.ports.store.get_bello_config()
        replacement = compat.CoderSession(
            self.ports.client,
            self.ports.store,
            self.ports._active_workspace_root(),
            previous.task_read_path,
            model=self.ports._active_coder_model(),
            fast=self.ports._fast_mode(),
            intelligence=self.ports._active_coder_intelligence(),
            multi_agent=self.ports._multi_agent_config(),
            plan_path=self.ports._active_coder_plan_path(),
            readonly_roots=previous.readonly_roots,
        )
        self.state.coder = replacement
        new_thread_id = await replacement.start_thread()
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "coder_transport_recovery_fallback_thread",
                "previous_thread_id": cfg.coder_thread_id or previous.thread_id,
                "new_thread_id": new_thread_id,
            }
        )
        if start_continuation:
            await replacement.start_turn(compat.TRANSPORT_RECOVERY_CODER_PROMPT)

    async def fail_provider(self, message: str) -> None:
        if not self.ports.running and self.ports.store.get_bello_config().status == compat.BelloStatus.PROVIDER_FAILURE:
            return
        await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)

    async def _cleanup_preflight_probe_thread(self, thread_id: str) -> None:
        try:
            await self.ports.client.thread_unsubscribe(thread_id)
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind="preflight_probe_thread",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    def _clear_persisted_coder_turn(self, thread_id: str | None, turn_id: str | None) -> None:
        if not thread_id or not turn_id:
            return
        coder = getattr(self.state, "coder", None)
        if (
            coder is not None
            and getattr(coder, "thread_id", None) == thread_id
            and getattr(coder, "active_turn_id", None) == turn_id
        ):
            coder.active_turn_id = None
        self.ports.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "active_coder_turn_id": (
                        None
                        if current.coder_thread_id == thread_id
                        and current.active_coder_turn_id == turn_id
                        else current.active_coder_turn_id
                    )
                }
            )
        )

    async def _reject_late_coder_turn(self, thread_id: str, turn_id: str, *, root: bool) -> None:
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "late_coder_turn_rejected",
                "thread_id": thread_id,
                "turn_id": turn_id,
                "root": root,
                "status": self.ports.store.get_bello_config().status.value,
            }
        )
        try:
            await self.ports.client.turn_interrupt(thread_id, turn_id)
        except Exception as exc:
            if compat._is_turn_already_inactive_error(exc):
                if root:
                    self.ports._clear_persisted_coder_turn(thread_id, turn_id)
                else:
                    state = self.ports._subagent_registry().get(thread_id)
                    if state is not None and state.active_turn_id == turn_id:
                        state.active_turn_id = None
                        state.status = "interrupted"
                return
            self.ports._append_cleanup_error(
                cleanup_kind="late_coder_turn_interrupt",
                thread_id=thread_id,
                turn_id=turn_id,
                error=exc,
            )
            if root:
                coder = getattr(self.state, "coder", None)
                if coder is not None and getattr(coder, "thread_id", None) == thread_id:
                    coder.active_turn_id = turn_id
                self.ports.store.update_bello_config(
                    lambda current: current.model_copy(
                        update={
                            "active_coder_turn_id": (
                                turn_id if current.coder_thread_id == thread_id else current.active_coder_turn_id
                            )
                        }
                    )
                )
            quiesced = await self.ports._quiesce_coder_tree("late_turn_notification", strict=False)
            if quiesced:
                if root:
                    self.ports._clear_persisted_coder_turn(thread_id, turn_id)
                return
            cfg = self.ports.store.get_bello_config()
            message = (
                f"late coder turn {turn_id} on {thread_id} could not be interrupted during "
                f"{cfg.status.value} lifecycle cleanup"
            )
            if cfg.status == compat.BelloStatus.PAUSED and not getattr(self.ports, "_finalizing", False):
                await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)
                return
            try:
                await self.ports.client.stop()
            except Exception as stop_error:
                self.ports._append_cleanup_error(
                    cleanup_kind="late_coder_turn_process_tree_stop",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    error=stop_error,
                )
            return
        if root:
            self.ports._clear_persisted_coder_turn(thread_id, turn_id)
        else:
            state = self.ports._subagent_registry().get(thread_id)
            if state is not None and state.active_turn_id == turn_id:
                state.active_turn_id = None
                state.status = "interrupted"

    async def pause(self) -> None:
        if getattr(self.ports, "_finalizing", False):
            return
        self.state._restart_transition_token = None
        self.ports.paused = True
        self.ports.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": compat.BelloStatus.PAUSED}))
        self.ports._supervisor_next_runtime_check = None
        self.ports._supervisor_next_completion_check = None
        self.ports._supervisor_next_runtime_summary = None
        self.ports._supervisor_next_completion_summary = None
        self.ports._pending_runtime_trigger_signatures = {}
        self.ports._pending_runtime_trigger_actions = {}
        self.ports._sync_legacy_supervisor_queue_fields()
        await self.ports._wait_for_revision_switch()
        await self.ports._wait_for_coder_activity()
        if getattr(self.ports, "_finalizing", False):
            return
        supervisor_task = getattr(self.ports, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not compat.asyncio.current_task():
            await self.ports._stop_supervisor_task()
        await self.ports._close_completion_review_session()
        await self.ports._quiesce_coder_tree("pause")
        await self.ports._resolve_pending_approvals("paused")
        self.ports.tui.status("paused")

    async def restart(self, reason: str, *, handoff: compat.RestartHandoff | None = None) -> None:
        if getattr(self.ports, "_finalizing", False):
            return
        cfg = self.ports.store.get_bello_config()
        if getattr(self.ports, "paused", False) or cfg.status == compat.BelloStatus.PAUSED:
            self.ports.tui.status("paused; resume before restarting")
            return
        if cfg.restart_count >= cfg.max_restarts:
            await self.ports.finalize("restart cap reached", status=compat.BelloStatus.STUCK)
            return
        transition_token = object()
        self.state._restart_transition_token = transition_token
        self.ports._append_event(compat.AppEventSource.SUPERVISOR, "controller/restart", reason=reason)
        self.ports.store.update_bello_config(lambda current: current.model_copy(update={"status": compat.BelloStatus.RESTARTING}))
        await self.ports._wait_for_revision_switch()
        restart_cap_reached = False
        try:
            async with self.ports._coder_activity_lock():
                current = self.ports.store.get_bello_config()
                if not self.ports._restart_transition_is_current(
                    transition_token,
                    expected_generation=current.generation,
                    expected_thread_id=current.coder_thread_id,
                ):
                    return
                if current.restart_count >= current.max_restarts:
                    restart_cap_reached = True
                else:
                    await self.ports._restart_after_activity_barrier(
                        reason,
                        handoff=handoff,
                        previous_config=current,
                        transition_token=transition_token,
                    )
        finally:
            if self.state._restart_transition_token is transition_token:
                self.state._restart_transition_token = None
        if restart_cap_reached:
            await self.ports.finalize("restart cap reached", status=compat.BelloStatus.STUCK)

    async def _restart_after_activity_barrier(
        self,
        reason: str,
        *,
        handoff: compat.RestartHandoff | None,
        previous_config: compat.BelloConfig,
        transition_token: object,
    ) -> None:
        supervisor_task = getattr(self.ports, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not compat.asyncio.current_task():
            await self.ports._stop_supervisor_task()
        if not self.ports._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self.ports._close_completion_review_session()
        if not self.ports._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self.ports._quiesce_coder_tree("restart")
        if not self.ports._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        await self.ports._resolve_pending_approvals("restart")
        handoff = handoff or compat._fallback_restart_handoff(
            task_contents=self.ports._canonical_task_text(),
            reason=reason,
            last_actions=self.ports.store.read_recent_actions(10),
        )
        self.ports.store.write_handoff(handoff.model_dump_json(indent=2) + "\n")
        self.ports._repair_snapshot_runtime_controls(source="restart")
        self.ports.prior_interventions = []
        self.ports.no_marker_idle_nudge_count = 0
        self.ports._last_completion_marker_sequence = None
        # The new generation has produced no coder work yet; until its first turn starts,
        # completion machinery must not judge (or restart over) the previous generation's state.
        self.state._generation_has_coder_turn = False
        self.ports.completion_review_return_sequence = None
        self.ports._pending_adversary_report = None
        self.ports._active_adversary_thread_id = None
        self.ports._active_adversary_workspace_root = None
        self.ports._adversary_denied_commands = []
        self.ports.validation_runtime_state = {}
        self.ports._last_restart_budget_signature = None
        self.ports._pending_runtime_trigger_signatures = {}
        self.ports._pending_runtime_trigger_actions = {}
        self.ports._deferred_completion_check = None
        self.ports._subagent_policy_notified = set()
        self.ports._runtime_apply_retry_count = 0
        self.ports._runtime_decision_retry_count = 0
        self.ports._supervisor_next_runtime_check = None
        self.ports._supervisor_next_completion_check = None
        self.ports._supervisor_next_runtime_summary = None
        self.ports._supervisor_next_completion_summary = None
        self.ports._sync_legacy_supervisor_queue_fields()
        if not self.ports._restart_transition_is_current(
            transition_token,
            expected_generation=previous_config.generation,
            expected_thread_id=previous_config.coder_thread_id,
        ):
            return
        compat.patch_health(
            self.ports.store,
            compat.HealthDelta(
                generation=previous_config.generation,
                restart_count=1,
                reset_generation_scoped=True,
                new_generation=previous_config.generation + 1,
            ),
        )
        self.ports.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "generation": current.generation + 1,
                    "restart_count": current.restart_count + 1,
                    "active_coder_turn_id": None,
                    "coder_thread_id": None,
                    "status": compat.BelloStatus.RUNNING,
                }
            )
        )
        self.state.coder = compat.CoderSession(
            self.ports.client,
            self.ports.store,
            self.ports._active_workspace_root(),
            self.ports._active_task_path(),
            model=self.ports._active_coder_model(),
            fast=self.ports._fast_mode(),
            intelligence=self.ports._active_coder_intelligence(),
            multi_agent=self.ports._multi_agent_config(),
            plan_path=self.ports._active_coder_plan_path(),
            readonly_roots=self.ports._active_dependency_roots(),
        )
        await self.state.coder.start_thread()
        if (
            self.state._restart_transition_token is not transition_token
            or not self.ports._coder_lifecycle_accepts_activity()
        ):
            return
        await self.state.coder.start_restart_turn()
        if (
            self.state._restart_transition_token is not transition_token
            or not self.ports._coder_lifecycle_accepts_activity()
        ):
            return
        self.ports.tui.render("SYSTEM", "restart complete")

    def _restart_transition_is_current(
        self,
        transition_token: object,
        *,
        expected_generation: int,
        expected_thread_id: str | None,
    ) -> bool:
        current = self.ports.store.get_bello_config()
        return bool(
            self.state._restart_transition_token is transition_token
            and not getattr(self.ports, "_finalizing", False)
            and current.status == compat.BelloStatus.RESTARTING
            and current.generation == expected_generation
            and current.coder_thread_id == expected_thread_id
        )

    async def _switch_to_revision_coder(
        self,
        reviewer_feedback: str,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        done = compat.asyncio.get_running_loop().create_future()
        owner = compat.asyncio.current_task()
        self.state._revision_switch_in_progress = True
        self.state._revision_switch_done = done
        self.state._revision_switch_owner = owner
        try:
            async with self.ports._coder_activity_lock():
                if not self.ports._coder_lifecycle_accepts_activity():
                    self.ports._record_cancelled_revision_switch(
                        "lifecycle changed before the revision coder switch began"
                    )
                    return
                await self.ports._perform_revision_coder_switch(
                    reviewer_feedback,
                    source=source,
                )
        finally:
            self.state._revision_switch_in_progress = False
            if not done.done():
                done.set_result(None)
            if getattr(self.state, "_revision_switch_done", None) is done:
                self.state._revision_switch_done = None
            if getattr(self.state, "_revision_switch_owner", None) is owner:
                self.state._revision_switch_owner = None

    async def _fail_revision_coder_switch(
        self,
        error: Exception,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        message = (
            f"revision coder profile switch failed while delivering {source} feedback: "
            f"{error.__class__.__name__}: {error}"
        )
        self.ports.store.append_text_locked(compat.PROGRESS, f"- {message}\n")
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "revision_coder_switch_failed",
                "source": source,
                "error_type": error.__class__.__name__,
                "error": str(error),
            }
        )
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "coder/profile_switch_failed",
            reason=message,
            payload={"source": source, "error_type": error.__class__.__name__},
        )
        # The finding has already been recorded, but it was not delivered. Treat an
        # app-server failure here like an initial coder startup failure: preserve the
        # workspace and stop explicitly instead of leaving a dead supervisor task.
        await self.ports.finalize(message, status=compat.BelloStatus.PROVIDER_FAILURE)

    async def _wait_for_revision_switch(self) -> None:
        if getattr(self.state, "_revision_switch_owner", None) is compat.asyncio.current_task():
            return
        done = getattr(self.state, "_revision_switch_done", None)
        if done is not None and not done.done():
            await compat.asyncio.shield(done)

    async def _perform_revision_coder_switch(
        self,
        reviewer_feedback: str,
        *,
        source: Literal["completion_review", "adversary_report_controller"],
    ) -> None:
        """Move review-driven revisions to one fresh coder thread without a health restart."""
        previous_coder = self.state.coder
        previous_thread_id = getattr(previous_coder, "thread_id", None)
        expected_config = self.ports.store.get_bello_config()
        expected_generation = expected_config.generation
        if previous_thread_id != expected_config.coder_thread_id:
            return
        try:
            await self.ports._quiesce_coder_tree("revision_profile_switch")
        except Exception as exc:
            raise compat._RevisionCoderDeliveryError(
                "prepare",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self.ports._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            self.ports._record_cancelled_revision_switch("lifecycle changed while quiescing the initial coder")
            return
        try:
            await self.ports._resolve_pending_approvals("revision coder profile switch")
            self.ports._repair_snapshot_runtime_controls(source="revision_profile_switch")
        except Exception as exc:
            raise compat._RevisionCoderDeliveryError(
                "prepare",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self.ports._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            self.ports._record_cancelled_revision_switch("lifecycle changed while resolving pending approvals")
            return

        revision_coder = compat.CoderSession(
            self.ports.client,
            self.ports.store,
            self.ports._active_workspace_root(),
            self.ports._active_task_path(),
            model=self.ports._revision_coder_model(),
            fast=self.ports._fast_mode(),
            intelligence=self.ports._revision_coder_intelligence(),
            multi_agent=self.ports._multi_agent_config(),
            plan_path=None,
            readonly_roots=self.ports._active_dependency_roots(),
        )
        try:
            new_thread_id = await revision_coder.start_thread(persist_state=False)
        except Exception as exc:
            raise compat._RevisionCoderDeliveryError(
                "thread/start",
                exc,
                generation=expected_generation,
                thread_id=previous_thread_id,
                coder=previous_coder,
            ) from exc
        if not self.ports._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=previous_thread_id,
            coder=previous_coder,
        ):
            await self.ports._discard_uncommitted_revision_thread(
                revision_coder,
                reason="lifecycle changed while starting the revision thread",
            )
            return
        snapshot = getattr(self.state, "_coder_snapshot", None)
        if snapshot is not None:
            try:
                snapshot.detach_plan_exposure()
            except Exception as exc:
                await self.ports._discard_uncommitted_revision_thread(
                    revision_coder,
                    reason="private plan could not be detached before revision",
                )
                raise compat._RevisionCoderDeliveryError(
                    "prepare",
                    exc,
                    generation=expected_generation,
                    thread_id=previous_thread_id,
                    coder=previous_coder,
                ) from exc
        self.state.workspace_plan_path = None
        self.state.coder = revision_coder
        self.ports.store.update_bello_config(
            lambda current: current.model_copy(
                update={
                    "revision_coder_active": True,
                    "coder_thread_id": new_thread_id,
                    "active_coder_turn_id": None,
                }
            )
        )
        self.state.last_coder_message = None
        self.ports._last_completion_marker_sequence = None
        self.ports._no_marker_completion_review_key = None
        self.ports._deferred_completion_check = None
        self.ports._subagent_policy_notified = set()
        self.state._generation_has_coder_turn = False
        self.ports._append_event(
            compat.AppEventSource.SUPERVISOR,
            "coder/profile_switch",
            thread_id=new_thread_id,
            reason=f"first {source} return moved revisions to the configured revision coder",
            payload={
                "source": source,
                "previous_thread_id": previous_thread_id,
                "revision_thread_id": new_thread_id,
                "model": self.ports._revision_coder_model(),
                "intelligence": self.ports._revision_coder_intelligence(),
            },
        )
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            "- Switched once to the configured revision coder after reviewer feedback; "
            f"thread {new_thread_id}, profile {self.ports._revision_coder_model()}/"
            f"{self.ports._revision_coder_intelligence()}.\n",
        )
        self.ports.tui.render(
            "SYSTEM",
            f"revision coder started ({self.ports._revision_coder_model()}/"
            f"{self.ports._revision_coder_intelligence()})",
        )
        try:
            turn_id = await revision_coder.start_revision_turn(
                reviewer_feedback,
                persist_state=False,
            )
        except Exception as exc:
            raise compat._RevisionCoderDeliveryError(
                "turn/start",
                exc,
                generation=expected_generation,
                thread_id=new_thread_id,
                coder=revision_coder,
            ) from exc
        if not self.ports._revision_switch_context_is_current(
            generation=expected_generation,
            thread_id=new_thread_id,
            coder=revision_coder,
        ):
            try:
                await self.ports._interrupt_stale_revision_turn(
                    revision_coder,
                    reason="lifecycle changed while starting the first revision turn",
                )
            except Exception:
                # pause/restart/finalize is waiting for this switch and will retry the
                # preserved turn id through its normal serialized quiesce path.
                pass
            return
        self.ports.store.update_bello_config(
            lambda current: current.model_copy(update={"active_coder_turn_id": turn_id})
        )

    def _revision_switch_context_is_current(
        self,
        *,
        generation: int,
        thread_id: str | None,
        coder: compat.Any,
    ) -> bool:
        config = self.ports.store.get_bello_config()
        blocked_statuses = {
            compat.BelloStatus.PAUSED,
            compat.BelloStatus.RESTARTING,
            compat.BelloStatus.COMPLETE,
            compat.BelloStatus.ESCALATED,
            compat.BelloStatus.STUCK,
            compat.BelloStatus.PROVIDER_FAILURE,
            compat.BelloStatus.EXITED,
        }
        return bool(
            self.state.coder is coder
            and config.generation == generation
            and config.coder_thread_id == thread_id
            and config.status not in blocked_statuses
            and not getattr(self.ports, "paused", False)
            and not getattr(self.ports, "_finalizing", False)
            and getattr(self.ports, "running", True)
        )

    def _record_cancelled_revision_switch(self, reason: str) -> None:
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "revision_coder_switch_cancelled",
                "reason": reason,
            }
        )

    async def _discard_uncommitted_revision_thread(
        self,
        coder: compat.CoderSession,
        *,
        reason: str,
    ) -> None:
        self.ports._record_cancelled_revision_switch(reason)
        thread_id = coder.thread_id
        if not isinstance(thread_id, str) or not hasattr(self.ports.client, "thread_unsubscribe"):
            return
        try:
            await self.ports.client.thread_unsubscribe(thread_id)
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind="cancelled_revision_thread",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    async def _interrupt_stale_revision_turn(
        self,
        coder: compat.CoderSession,
        *,
        reason: str,
    ) -> None:
        self.ports._record_cancelled_revision_switch(reason)
        try:
            await coder.interrupt()
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind="stale_revision_turn",
                thread_id=coder.thread_id or "unknown",
                turn_id=coder.active_turn_id,
                error=exc,
            )
            raise
        interrupted_thread_id = coder.thread_id
        interrupted_turn_id = coder.active_turn_id
        coder.active_turn_id = None
        if interrupted_thread_id and interrupted_turn_id:
            self.ports.store.update_bello_config(
                lambda current: current.model_copy(
                    update={
                        "active_coder_turn_id": (
                            None
                            if current.coder_thread_id == interrupted_thread_id
                            and current.active_coder_turn_id == interrupted_turn_id
                            else current.active_coder_turn_id
                        )
                    }
                )
            )
