"""Controller lifecycle rules; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _is_no_active_turn_to_steer_error(exc: compat.AppServerError) -> bool:
    return "no active turn to steer" in str(exc).lower()


def _is_turn_already_inactive_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "already completed",
            "already interrupted",
            "no active turn",
            "turn is not active",
            "turn not found",
        )
    )


def _fallback_restart_handoff(*, task_contents: str, reason: str, last_actions: list[str]) -> compat.RestartHandoff:
    objective = " ".join(task_contents.strip().split())[:1000] or "Continue the selected task."
    known_evidence = "; ".join(last_actions[-5:]) or "No completed coder actions are recorded."
    return compat.RestartHandoff(
        objective=objective,
        restart_reason=reason,
        bad_pattern="The previous generation was interrupted or judged unreliable before completing the task.",
        known_evidence=known_evidence,
        next_step="Read the task, progress, decisions, and this handoff, then take the next concrete task step.",
        recovery_signal="The new generation makes task-relevant progress without repeating the prior failure mode.",
    )


def _restart_rejection_steering(handoff: compat.RestartHandoff | None) -> str:
    if handoff is None:
        return "Continue the current task. Use the latest observation to make the next concrete progress step."
    return (
        f"Correct the current non-converging pattern before continuing. Avoid: {handoff.bad_pattern} "
        f"Next step: {handoff.next_step} Recovery signal: {handoff.recovery_signal}"
    )


def _has_readiness_marker(text: str) -> bool:
    return bool(compat.READINESS_MARKER_RE.search(text.strip()))


def _is_recoverable_app_server_transport_error(message: str) -> bool:
    normalized = " ".join(message.lower().split())
    return any(
        marker in normalized
        for marker in (
            "app-server stream closed",
            "pi worker stream closed",
            "claude code stream closed",
            "broken pipe",
            "connection reset",
            "connection closed",
            "unexpected eof",
            "end of file",
        )
    )


def _thread_turn_by_id(
    thread: dict[str, compat.Any],
    turn_id: str | None,
) -> dict[str, compat.Any] | None:
    if not turn_id:
        return None
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return None
    for turn in turns:
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return turn
    return None


def _has_malformed_readiness_marker(text: str) -> bool:
    if compat._has_readiness_marker(text):
        return False
    if compat._readiness_reference_is_negated(text):
        return False
    lowered = text.lower()
    compact = compat.re.sub(r"[\s_\-]+", "_", lowered)
    return any(
        marker in lowered or marker in compact
        for marker in (
            "bello ready for review",
            "bello_ready",
            "bello_ready_for_review",
            "ready_for_review",
        )
    )


def _readiness_reference_is_negated(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    marker = r"(?:bello[\s_`'\-]*ready[\s_`'\-]*for[\s_`'\-]*review|ready[\s_`'\-]*for[\s_`'\-]*review|readiness marker)"
    negator = r"(?:do not|don't|not|cannot|can't|will not|won't|without|no)"
    return bool(compat.re.search(rf"\b{negator}\b.{{0,120}}\b{marker}\b", lowered))


def _appears_to_claim_readiness(text: str) -> bool:
    if compat._readiness_reference_is_negated(text):
        return False
    lowered = " ".join(text.lower().split())
    phrases = (
        "done",
        "complete",
        "completed",
        "finished",
        "implemented",
        "all tests pass",
        "all tests passed",
        "ready for review",
        "task is complete",
        "validation:",
    )
    return any(phrase in lowered for phrase in phrases)
