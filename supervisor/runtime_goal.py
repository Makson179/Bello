"""Bounded, optional Goal progress reviews without a second task controller.

This policy owns cadence and advice deduplication only. It never decides that
an experiment is repetitive, changes coder state, or grants restart authority.
The full runtime must establish those facts from current work and the relevant
attempt-map entries. All clock, progress, and evidence inputs come from the
controller; polling this policy performs no filesystem or model work.
"""

from __future__ import annotations

import math
from collections import deque


GOAL_PROGRESS_REVIEW_INTERVAL_SECONDS = 20 * 60.0
GOAL_PROGRESS_ADVICE_COOLDOWN_SECONDS = 30 * 60.0


class GoalRuntimeProgressPolicy:
    """Rate-limit Goal reviews separately from nonbinding advice to the coder.

    ``meaningful_sequence`` must describe coder work or new external results,
    not reviewer wakes, token usage, status polling, or this policy's own advice.
    ``evidence_fingerprint`` is an optional already-available identity of relevant
    work/map evidence; the caller need not read or hash a map to use this policy.

    A review is eligible after a full interval, with unreviewed work/evidence.
    Continuous work does not postpone that interval. Busy reviewers/approvals
    defer a review without accumulating a queue. Disabling Goal/runtime, pausing,
    or terminating the run disarms it and gives a fresh interval on re-entry.

    Advice has a longer cooldown and a bounded history of stable evidence keys.
    Keys should identify the repeated approach and compared attempt/version, not
    the review's sequence or timestamp: another wake is not new evidence.
    """

    def __init__(
        self,
        *,
        review_interval_seconds: float = GOAL_PROGRESS_REVIEW_INTERVAL_SECONDS,
        advice_cooldown_seconds: float = GOAL_PROGRESS_ADVICE_COOLDOWN_SECONDS,
        remembered_advice: int = 32,
    ) -> None:
        self.review_interval_seconds = self._positive_seconds(review_interval_seconds)
        self.advice_cooldown_seconds = self._positive_seconds(advice_cooldown_seconds)
        if isinstance(remembered_advice, bool) or not isinstance(remembered_advice, int) or remembered_advice < 1:
            raise ValueError("remembered_advice must be a positive integer")
        self._armed_at: float | None = None
        self._last_requested_at: float | None = None
        self._last_requested_sequence = 0
        self._last_requested_fingerprint: str | None = None
        self._last_delivered_at: float | None = None
        self._delivered_evidence: deque[str] = deque(maxlen=remembered_advice)

    @staticmethod
    def _positive_seconds(value: float) -> float:
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("cadence values must be finite and positive")
        return value

    @staticmethod
    def _time(now: float) -> float:
        now = float(now)
        if not math.isfinite(now) or now < 0:
            raise ValueError("now must be a finite nonnegative monotonic time")
        return now

    @staticmethod
    def _sequence(sequence: int) -> int:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("meaningful_sequence must be a nonnegative integer")
        return sequence

    def maybe_due(
        self,
        *,
        now: float,
        goal_enabled: bool,
        runtime_enabled: bool,
        meaningful_sequence: int,
        evidence_fingerprint: str | None = None,
        review_running: bool = False,
        approval_pending: bool = False,
        runnable: bool = True,
    ) -> bool:
        """Return eligibility; only ``mark_requested`` consumes a review slot.

        ``review_running`` also covers an already queued review (any role).
        ``runnable`` is false for paused/stopped/finalizing runs, not simply for a
        coder waiting on a command. This method never sends a wake itself.
        """
        now = self._time(now)
        meaningful_sequence = self._sequence(meaningful_sequence)
        if not goal_enabled or not runtime_enabled or not runnable:
            self._armed_at = None
            return False
        # Start the grace period with actual work, not an empty startup wait or
        # a preexisting map. A long model/approval startup is not a plateau.
        if meaningful_sequence == 0:
            return False
        if self._armed_at is None:
            self._armed_at = now
            return False
        if review_running or approval_pending:
            return False
        previous = self._last_requested_at
        interval_start = max(self._armed_at, previous if previous is not None else self._armed_at)
        if now - interval_start < self.review_interval_seconds:
            return False
        new_work = meaningful_sequence > self._last_requested_sequence
        new_evidence = (
            evidence_fingerprint is not None
            and evidence_fingerprint != self._last_requested_fingerprint
        )
        return new_work or new_evidence

    def mark_requested(
        self,
        *,
        now: float,
        meaningful_sequence: int,
        evidence_fingerprint: str | None = None,
    ) -> None:
        """Record an actually scheduled review, including a later noop/error.

        Call only after successful scheduling, with the evidence seen by that
        request, not the newer evidence that may exist when the reviewer returns.
        Errors must not create an immediate periodic retry loop.
        """
        now = self._time(now)
        meaningful_sequence = self._sequence(meaningful_sequence)
        if self._armed_at is None:
            self._armed_at = now
        self._last_requested_at = now
        self._last_requested_sequence = meaningful_sequence
        self._last_requested_fingerprint = evidence_fingerprint

    def reset_review_cadence(self) -> None:
        """Start a new coder identity's review grace without forgetting advice.

        The controller separately excludes observations from the old identity.
        Retaining advisory cooldown/dedup prevents a restart itself from granting
        permission to deliver another copy of the same optional suggestion.
        """
        self._armed_at = None
        self._last_requested_at = None
        self._last_requested_sequence = 0
        self._last_requested_fingerprint = None

    def can_deliver(self, *, now: float, evidence_key: str | None) -> bool:
        """Check a prospective nonbinding advisory without reserving delivery.

        A stable advisory key is used only for frequency/deduplication. It is
        not a proof requirement. Potential staleness adds context to feedback,
        never a rejection or demand for another review before delivery.
        A refusal to follow advice is not a defect or grounds for restart.
        """
        now = self._time(now)
        if not evidence_key or not evidence_key.strip():
            return False
        if evidence_key in self._delivered_evidence:
            return False
        return self._last_delivered_at is None or now - self._last_delivered_at >= self.advice_cooldown_seconds

    def mark_delivered(self, *, now: float, evidence_key: str) -> None:
        """Record only successful coder delivery, never a rejected/failed send."""
        now = self._time(now)
        if not evidence_key or not evidence_key.strip():
            raise ValueError("a delivered advisory needs a stable evidence key")
        self._last_delivered_at = now
        if evidence_key not in self._delivered_evidence:
            self._delivered_evidence.append(evidence_key)
