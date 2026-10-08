"""Shutdown service and its explicitly owned per-run state.

Only the declared port can reach the coordinator. Own state is accessed directly;
cross-service operations go through replaceable coordinator callbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from supervisor.schemas import BelloStatus

from . import compat
from .interfaces import CoordinatorPort


@dataclass(init=False, slots=True)
class ShutdownState:
    """Unset fields intentionally remain absent for legacy __new__ construction."""
    _final_report_archived: bool
    _finalizing: bool
    _snapshot_recovery_path: str | None
    _terminal_cleanup_started: bool
    _terminal_coder_tree_quiesced: bool


class ShutdownPort(CoordinatorPort):
    __slots__ = ()
    reads = frozenset({
        '_accepted_adversary_report',
        '_accepted_completion_decision',
        '_active_task_path',
        '_active_workspace_root',
        '_append_cleanup_error',
        '_apply_final_snapshot_patch_if_needed',
        '_archive_final_report_once',
        '_close_completion_review_session',
        '_coder_snapshot',
        '_coder_started',
        '_completion_supervisor_agent',
        '_immutable_approval_paths',
        '_is_review_private_path',
        '_pending_adversary_report',
        '_prepare_terminal_shutdown',
        '_preserve_snapshot_for_recovery',
        '_quiesce_coder_tree',
        '_reconcile_intervention_accounting',
        '_resolve_pending_approvals',
        '_restart_transition_token',
        '_runtime_enabled',
        '_runtime_integrity_issue',
        '_snapshot_patch_applied',
        '_stop_supervisor_task',
        '_supervisor_task',
        '_task_integrity_issue',
        '_wait_for_coder_activity',
        '_wait_for_revision_switch',
        '_wake_event_loop_for_shutdown',
        '_write_run_checkpoint',
        'approvals',
        'changed_files',
        'client',
        'coder',
        'completion_restarts',
        'declared_grading_roots',
        'diff_summary',
        'event_queue',
        'no_marker_idle_nudge_count',
        'pending_approvals',
        'running',
        'store',
        'task_path',
        'tui',
        'validations',
    })
    writes = frozenset({
        '_coder_snapshot',
        '_restart_transition_token',
        '_snapshot_patch_applied',
        'running',
    })


class Shutdown:
    """Own shutdown behavior; borrow only the declared port."""

    def __init__(self, ports: ShutdownPort) -> None:
        self.state = ShutdownState()
        self.ports = ports

    async def finalize(
        self,
        result: str,
        *,
        status: compat.BelloStatus = BelloStatus.COMPLETE,
        completion_review_accepted: bool | None = False,
    ) -> None:
        if getattr(self.state, "_finalizing", False):
            return
        self.state._finalizing = True
        self.ports._restart_transition_token = None
        self.ports._reconcile_intervention_accounting()
        await self.ports._wait_for_revision_switch()
        await self.ports._wait_for_coder_activity()
        supervisor_task = getattr(self.ports, "_supervisor_task", None)
        if supervisor_task is not None and supervisor_task is not compat.asyncio.current_task():
            await self.ports._stop_supervisor_task()
        quiesced = await self.ports._quiesce_coder_tree("terminal", strict=False)
        self.state._terminal_coder_tree_quiesced = quiesced
        if not quiesced:
            # A process-tree stop is the deterministic fallback: no agent may keep
            # mutating the snapshot while Bello computes or applies the final patch.
            try:
                await self.ports.client.stop()
                self.state._terminal_coder_tree_quiesced = True
            except Exception as exc:
                self.ports._append_cleanup_error(
                    cleanup_kind="terminal_process_tree_stop",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )
        diff = await self.ports.diff_summary()
        changed_files = await self.ports.changed_files()
        patch_error, recovery_path = await self.ports._apply_final_snapshot_patch_if_needed(status)
        if patch_error is not None:
            status = compat.BelloStatus.ESCALATED
            completion_review_accepted = False
            result = patch_error
        elif recovery_path is not None:
            result = f"{result}; unaccepted coder workspace preserved at {recovery_path}"
        health = self.ports.store.get_health()
        accepted_completion = getattr(self.ports, "_accepted_completion_decision", None)
        report = compat.FinalReport(
            task_path=str(self.ports.task_path),
            status=status,
            result=result,
            files_changed=[file.path for file in changed_files]
            or compat._changed_files_from_diff_summary(
                diff,
                project_root=self.ports._active_workspace_root(),
                task_path=self.ports._active_task_path(),
            ),
            validations=[compat._format_validation(validation) for validation in self.ports.validations],
            denied_actions=[],
            interventions=health.interventions,
            restarts=health.restart_count,
            completion_review_accepted=completion_review_accepted,
            completion_returns=self.ports.store.get_bello_config().completion_return_count,
            completion_restarts=getattr(self.ports, "completion_restarts", 0),
            no_marker_idle_nudges=getattr(self.ports, "no_marker_idle_nudge_count", 0),
            behavior_evidence_summary=compat._behavior_evidence_summary(accepted_completion),
            files_reviewed_summary=compat._files_reviewed_summary(accepted_completion),
            packet_or_access_limitations=list(accepted_completion.packet_or_access_limitations)
            if isinstance(accepted_completion, compat.CompletionReviewDecision)
            else [],
            adversary_reports=compat._final_adversary_report_summary(
                getattr(self.ports, "_accepted_adversary_report", None)
                or getattr(self.ports, "_pending_adversary_report", None)
            ),
            remaining_risks=list(accepted_completion.changed_test_risks)
            if isinstance(accepted_completion, compat.CompletionReviewDecision)
            else [],
            diff_summary=diff,
        )
        self.ports.store.write_final_report(report)
        self.ports._archive_final_report_once()
        self.ports.store.update_bello_config(lambda cfg: cfg.model_copy(update={"status": status}))
        self.ports._write_run_checkpoint("terminal", state="terminal", detail=result)
        self.ports.tui.render("SUPERVISOR", result)
        self.ports.tui.status("final report written: .supervisor/FINAL_REPORT.md")
        await self.ports._prepare_terminal_shutdown(result)
        self.ports.running = False
        self.ports._wake_event_loop_for_shutdown()

    async def _apply_final_snapshot_patch_if_needed(
        self,
        status: compat.BelloStatus,
    ) -> tuple[str | None, str | None]:
        snapshot = getattr(self.ports, "_coder_snapshot", None)
        snapshot_patch_applied = getattr(self.ports, "_snapshot_patch_applied", False)
        recovery_path = getattr(self.state, "_snapshot_recovery_path", None)
        if status == compat.BelloStatus.COMPLETE and (snapshot is None or snapshot_patch_applied):
            task_integrity_issue = self.ports._task_integrity_issue()
            if task_integrity_issue is not None:
                return (
                    "escalated: accepted workspace failed task integrity validation: "
                    f"{task_integrity_issue}",
                    recovery_path,
                )
        if snapshot is None or snapshot_patch_applied:
            return None, recovery_path
        if status != compat.BelloStatus.COMPLETE:
            if not getattr(self.ports, "_coder_started", False):
                snapshot.cleanup()
                self.ports._coder_snapshot = None
                return None, None
            recovery_path = await self.ports._preserve_snapshot_for_recovery(snapshot, reason=status.value)
            return None, recovery_path
        runtime_integrity_issue = self.ports._runtime_integrity_issue()
        if runtime_integrity_issue is not None:
            recovery_path = await self.ports._preserve_snapshot_for_recovery(
                snapshot,
                reason="runtime_integrity",
            )
            return (
                "escalated: accepted snapshot failed runtime integrity validation; "
                f"workspace preserved at {recovery_path}: {runtime_integrity_issue}",
                recovery_path,
            )
        task_integrity_issue = self.ports._task_integrity_issue()
        if task_integrity_issue is not None:
            recovery_path = await self.ports._preserve_snapshot_for_recovery(snapshot, reason="task_integrity")
            return (
                "escalated: accepted snapshot failed task integrity validation; "
                f"workspace preserved at {recovery_path}: {task_integrity_issue}",
                recovery_path,
            )
        try:
            result = await compat.asyncio.to_thread(
                compat.apply_snapshot_patch, snapshot, runtime_enabled=self.ports._runtime_enabled(),
            )
        except (compat.SnapshotPatchError, compat.WorkspaceSnapshotError) as exc:
            recovery_path = await self.ports._preserve_snapshot_for_recovery(snapshot, reason="patch_failed")
            message = (
                "escalated: accepted snapshot could not be applied to the real workspace; "
                f"snapshot preserved at {recovery_path}: {exc}"
            )
            self.ports.tui.render("PATCH", message)
            self.ports.store.append_text_locked(compat.PROGRESS, f"- {message}\n")
            self.ports.store.append_raw_log(
                {
                    "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                    "type": "coder_snapshot_patch_failed",
                    "snapshot_root": recovery_path,
                    "original_root": str(snapshot.original_root),
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
            return message, recovery_path
        self.ports._snapshot_patch_applied = True
        reportable_ignored_paths = [
            path
            for path in result.ignored_paths
            if not self.ports._is_review_private_path(path)
        ]
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "coder_snapshot_patch_applied",
                "snapshot_root": str(snapshot.snapshot_root),
                "original_root": str(snapshot.original_root),
                "applied": result.applied,
                "changed_paths": list(result.changed_paths),
                "ignored_paths": reportable_ignored_paths,
                "patch_bytes": result.patch_bytes,
            }
        )
        if result.applied:
            ignored_suffix = (
                f"; ignored {len(reportable_ignored_paths)} generated artifact paths"
                if reportable_ignored_paths
                else ""
            )
            self.ports.store.append_text_locked(
                compat.PROGRESS,
                f"- Applied accepted coder snapshot patch to real workspace ({len(result.changed_paths)} paths{ignored_suffix}).\n",
            )
        else:
            if reportable_ignored_paths:
                self.ports.store.append_text_locked(
                    compat.PROGRESS,
                    "- Accepted coder snapshot produced no workspace patch after generated artifacts were ignored.\n",
                )
            else:
                self.ports.store.append_text_locked(compat.PROGRESS, "- Accepted coder snapshot produced no workspace patch.\n")
        snapshot.cleanup()
        self.ports._coder_snapshot = None
        return None, None

    async def _preserve_snapshot_for_recovery(self, snapshot: compat.WorkspaceSnapshot, *, reason: str) -> str:
        existing = getattr(self.state, "_snapshot_recovery_path", None)
        if existing:
            return str(existing)
        destination = self.ports.store.next_recovery_dir()
        try:
            workspace = await compat.asyncio.to_thread(snapshot.preserve, destination)
            recovery_path = str(workspace)
        except compat.WorkspaceSnapshotError:
            recovery_path = str(snapshot.snapshot_root)
        self.state._snapshot_recovery_path = recovery_path
        self.ports._coder_snapshot = None
        self.ports.store.append_text_locked(
            compat.PROGRESS,
            f"- Preserved coder workspace for recovery at {recovery_path} ({reason}).\n",
        )
        self.ports.store.append_raw_log(
            {
                "timestamp": compat.datetime.now(compat.timezone.utc).isoformat(),
                "type": "coder_workspace_preserved",
                "reason": reason,
                "workspace": recovery_path,
            }
        )
        return recovery_path

    def _archive_final_report_once(self) -> None:
        if getattr(self.state, "_final_report_archived", False):
            return
        self.ports.store.archive_completed_run(self.ports.task_path)
        self.state._final_report_archived = True

    async def _prepare_terminal_shutdown(self, reason: str) -> None:
        if getattr(self.state, "_terminal_cleanup_started", False):
            return
        self.state._terminal_cleanup_started = True
        self.ports.running = False
        await self.ports._close_completion_review_session()
        coder = None if getattr(self.state, "_terminal_coder_tree_quiesced", False) else getattr(self.ports, "coder", None)
        if coder:
            try:
                await coder.interrupt()
            except Exception as exc:
                self.ports._append_cleanup_error(
                    cleanup_kind="terminal_coder_interrupt",
                    thread_id=getattr(coder, "thread_id", None) or "unknown",
                    turn_id=getattr(coder, "active_turn_id", None),
                    error=exc,
                )
        if getattr(self.ports, "pending_approvals", None) and getattr(self.ports, "client", None) is not None:
            try:
                await self.ports._resolve_pending_approvals(f"terminal state reached: {reason}")
            except Exception as exc:
                self.ports._append_cleanup_error(
                    cleanup_kind="terminal_pending_approvals",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )
        task = getattr(self.ports, "_supervisor_task", None)
        if task is not None and task is not compat.asyncio.current_task():
            await self.ports._stop_supervisor_task()
        client = getattr(self.ports, "client", None)
        if client is not None and hasattr(client, "stop"):
            try:
                await client.stop()
            except Exception as exc:
                self.ports._append_cleanup_error(
                    cleanup_kind="terminal_appserver_stop",
                    thread_id="unknown",
                    turn_id=None,
                    error=exc,
                )

    async def _close_completion_review_session(self) -> None:
        supervisor = self.ports._completion_supervisor_agent()
        if supervisor is None or not hasattr(supervisor, "close_completion_review"):
            return
        thread_id = getattr(supervisor, "completion_thread_id", None) or "unknown"
        try:
            await supervisor.close_completion_review()
        except Exception as exc:
            self.ports._append_cleanup_error(
                cleanup_kind="completion_review_session",
                thread_id=thread_id,
                turn_id=None,
                error=exc,
            )

    def _wake_event_loop_for_shutdown(self) -> None:
        queue = getattr(self.ports, "event_queue", None)
        if queue is None:
            return
        try:
            queue.put_nowait(compat.ControllerEvent(kind="shutdown"))
        except Exception:
            pass

    async def _resolve_pending_approvals(self, reason: str) -> None:
        approvals = getattr(self.ports, "approvals", None)
        if approvals is None:
            manager = compat.ApprovalManager(
                self.ports._active_workspace_root(),
                declared_grading_roots=getattr(self.ports, "declared_grading_roots", ()),
                immutable_paths=self.ports._immutable_approval_paths(),
            )
        else:
            manager = approvals
        for request_id, context in list(self.ports.pending_approvals.items()):
            resolution = manager._deny(context, reason)
            await self.ports.client.respond(request_id, manager.response_payload(context, resolution))
            self.ports.pending_approvals.pop(request_id, None)
        self.ports.store.update_bello_config(lambda cfg: cfg.model_copy(update={"pending_server_request_ids": []}))

    async def _stop_supervisor_task(self) -> None:
        task = self.ports._supervisor_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except compat.asyncio.CancelledError:
            pass
        except Exception:
            pass
