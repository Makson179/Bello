"""Controller runtime rules; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _approval_wake_context(
    context: compat.ApprovalContext,
    reason: str | None = None,
    *,
    origin: str = "coder",
) -> compat.ApprovalWakeContext:
    return compat.ApprovalWakeContext(
        request_type=context.request_type.value,
        server_request_id=context.server_request_id,
        method=context.server_request_method,
        available_decisions=context.available_decisions,
        command=context.command,
        file_changes=context.file_changes,
        paths=context.paths,
        cwd=context.cwd,
        grant_root=context.grant_root,
        network_approval_context=context.network_approval_context,
        proposed_execpolicy_amendment=context.proposed_execpolicy_amendment,
        proposed_network_policy_amendments=context.proposed_network_policy_amendments,
        reason=reason,
        origin=origin,
    )


def _runtime_packet_requires_full_supervisor(packet: compat.SupervisorWakePacket) -> bool:
    reasons = set(compat._runtime_trigger_reasons_from_summary(packet.current_summary))
    if reasons & compat.MANDATORY_FULL_RUNTIME_WAKE_REASONS:
        return True
    return (packet.current_summary or "").lstrip().startswith("Runtime integrity trigger:")


def _runtime_trigger_reasons_from_summary(summary: str | None) -> tuple[str, ...]:
    if not summary:
        return ()
    match = compat.re.match(r"\s*Runtime trigger \(([^)]*)\):", summary)
    if not match:
        return ()
    return tuple(
        reason
        for reason in (part.strip() for part in match.group(1).split(","))
        if reason and reason != "masked_validation"
    )


def _runtime_unresolved_execution_key(command: str, cwd: str | None) -> str:
    payload = {
        "command": compat._canonical_restart_command(command),
        "cwd": cwd or "",
    }
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"unresolved-execution:{digest[:16]}"


def _runtime_validation_restart_issue(validation: compat.ValidationRun) -> compat.RuntimeRestartIssue | None:
    if validation.trusted_validation_outcome == "passed":
        return None
    if validation.exit_code is None or validation.shell_exit_code is None:
        return compat.RuntimeRestartIssue(
            key=compat._runtime_unresolved_execution_key(validation.command, validation.cwd),
            sequence=validation.sequence,
            validation_id=validation.validation_id,
        )
    if validation.trusted_validation_outcome != "failed":
        return None
    evidence = validation.captured_output or validation.summary
    normalized_evidence = " ".join(evidence.split())
    payload = {
        "validation_id": validation.validation_id,
        "exit_code": validation.exit_code,
        "shell_exit_code": validation.shell_exit_code,
        "executed_test_names": sorted(validation.executed_test_names),
        "executed_test_files": sorted(validation.executed_test_files),
        "failed_count": validation.failed_count,
        "evidence_sha256": compat.hashlib.sha256(normalized_evidence.encode("utf-8")).hexdigest(),
    }
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return compat.RuntimeRestartIssue(
        key=f"failed-validation:{validation.validation_id}:{digest}",
        sequence=validation.sequence,
        validation_id=validation.validation_id,
    )


def _matching_active_validation_issue(
    packet: compat.SupervisorWakePacket,
    *,
    active_issue_key: str | None,
    active_issue_last_sequence: int,
) -> compat.RuntimeRestartIssue | None:
    if active_issue_key is None:
        return None
    issues = [
        issue
        for validation in packet.validations
        if validation.sequence > active_issue_last_sequence
        if (issue := compat._runtime_validation_restart_issue(validation)) is not None
    ]
    if not issues:
        return None
    latest = max(issues, key=lambda issue: issue.sequence)
    return latest if latest.key == active_issue_key else None


def _runtime_event_issue_payload(
    packet: compat.SupervisorWakePacket,
    *,
    reasons: tuple[str, ...],
) -> dict[str, compat.Any]:
    action = packet.triggering_action
    approval = packet.approval_context
    payload: dict[str, compat.Any] = {"reasons": reasons}
    if approval is not None:
        payload["approval"] = {
            "request_type": str(approval.request_type),
            "command": (
                compat._canonical_restart_command(approval.command) if approval.command else None
            ),
            "cwd": approval.cwd,
            "paths": sorted(approval.paths),
        }
        return payload
    if action is not None and action.command:
        payload["command"] = {
            "kind": action.kind,
            "command": compat._canonical_restart_command(action.command),
            "cwd": action.cwd,
            "exit_code": action.exit_code,
        }
        return payload
    changed_paths = sorted({changed.path for changed in packet.changed_files})
    if not changed_paths and action is not None:
        changed_paths = sorted(action.paths)
    payload["paths"] = changed_paths
    if not reasons and action is not None:
        payload["kind"] = action.kind
    return payload


def _runtime_restart_issue(
    packet: compat.SupervisorWakePacket,
    *,
    active_issue_key: str | None = None,
    active_issue_last_sequence: int = 0,
) -> compat.RuntimeRestartIssue | None:
    validation = compat._runtime_triggering_validation(packet)
    if validation is not None:
        if validation.trusted_validation_outcome == "masked_or_unknown":
            # Backward-compatible schema values from older runs must not recreate the
            # retired masked-validation restart gate.
            return None
        issue = compat._runtime_validation_restart_issue(validation)
        if issue is not None:
            return issue

    reasons = tuple(sorted(compat._runtime_trigger_reasons_from_summary(packet.current_summary)))
    action = packet.triggering_action
    approval = packet.approval_context
    if action is None and approval is None and not reasons:
        if packet.current_summary.strip() != "Coder turn completed":
            return None
        return compat._matching_active_validation_issue(
            packet,
            active_issue_key=active_issue_key,
            active_issue_last_sequence=active_issue_last_sequence,
        )
    payload = compat._runtime_event_issue_payload(packet, reasons=reasons)
    digest = compat.hashlib.sha256(compat.json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return compat.RuntimeRestartIssue(key=f"runtime-event:{digest}", sequence=packet.latest_event_sequence)


def _runtime_triggering_validation(packet: compat.SupervisorWakePacket) -> compat.ValidationRun | None:
    if not packet.validations:
        return None
    action = packet.triggering_action
    if action is not None and action.command:
        normalized_command = compat._normalize_command(action.command)
        matches = [
            validation
            for validation in packet.validations
            if validation.normalized_command == normalized_command
        ]
        if matches:
            return max(matches, key=lambda validation: validation.sequence)
    validation_reasons = {
        "repeated_same_failing_validation",
        "validation_regression",
    }
    if validation_reasons & set(compat._runtime_trigger_reasons_from_summary(packet.current_summary)):
        return max(packet.validations, key=lambda validation: validation.sequence)
    return None


def _approval_resolution_is_denial(decision: str | dict[str, compat.Any]) -> bool:
    return isinstance(decision, str) and decision in {"decline", "cancel", "denied", "abort"}


def _approval_resolution_metric_key(decision: str | dict[str, compat.Any]) -> str:
    if isinstance(decision, str):
        return decision
    if isinstance(decision, dict) and decision:
        return str(next(iter(decision)))
    return "unknown"
