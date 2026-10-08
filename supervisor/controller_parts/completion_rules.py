"""Controller completion rules; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _normalized_surface_key(category: str) -> str:
    return compat.re.sub(r"\s+", " ", category).strip().lower()


def _merge_behavior_surface_items(
    existing: list[dict[str, compat.Any]],
    updates: list[compat.BehaviorSurfaceItem],
) -> tuple[list[dict[str, compat.Any]], bool]:
    """Upsert reviewer-returned surface entries into the stored list.

    Entries are never removed: a reviewer that judges an entry not actually required marks it
    status=out_of_scope with a note instead, so the audit trail of what was considered stays
    visible to later reviews.
    """
    merged: list[dict[str, compat.Any]] = [
        dict(item) for item in existing if isinstance(item, dict) and str(item.get("category") or "").strip()
    ]
    index = {compat._normalized_surface_key(str(item.get("category") or "")): pos for pos, item in enumerate(merged)}
    changed = False
    for item in updates:
        category = (item.category or "").strip()
        if not category:
            continue
        key = compat._normalized_surface_key(category)
        pos = index.get(key)
        if pos is None:
            merged.append({"category": category, "status": item.status, "note": item.note})
            index[key] = len(merged) - 1
            changed = True
        elif merged[pos].get("status") != item.status or merged[pos].get("note") != item.note:
            merged[pos] = {**merged[pos], "status": item.status, "note": item.note}
            changed = True
    return merged, changed


def _completion_returns_this_generation(controller: compat.Any, generation: int) -> int:
    return sum(
        1
        for record in getattr(controller, "completion_returns", []) or []
        if getattr(record, "generation", None) == generation
    )


def _prior_record_counts_as_health_intervention(record: compat.Any) -> bool:
    reason = str(getattr(record, "reason", "") or "")
    return not reason.startswith("Completion review returned:")


def _completion_return_summary(decision: compat.CompletionReviewDecision) -> str:
    parts = [decision.reason]
    if decision.uncovered_behaviors:
        parts.append("uncovered=" + ", ".join(decision.uncovered_behaviors[:5]))
    if decision.validation_gaps:
        parts.append("validation_gaps=" + ", ".join(decision.validation_gaps[:5]))
    if decision.claim_evidence_mismatches:
        parts.append("mismatches=" + ", ".join(decision.claim_evidence_mismatches[:5]))
    if decision.packet_or_access_limitations:
        parts.append("limitations=" + ", ".join(decision.packet_or_access_limitations[:5]))
    return "; ".join(part for part in parts if part)


def _behavior_evidence_summary(decision: compat.Any) -> list[str]:
    if not isinstance(decision, compat.CompletionReviewDecision):
        return []
    return [
        f"{row.status}: {row.behavior}"
        + (f" ({len(row.evidence)} evidence item{'s' if len(row.evidence) != 1 else ''})" if row.evidence else "")
        for row in decision.behavior_evidence_matrix
    ]


def _files_reviewed_summary(decision: compat.Any) -> list[str]:
    if not isinstance(decision, compat.CompletionReviewDecision):
        return []
    return [
        f"{file.kind}: {file.path} ({'inspected' if file.inspected else 'not inspected'})"
        + (f" - {file.limitation}" if file.limitation else "")
        for file in decision.files_reviewed
    ]


def _normalize_review_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _adversary_enabled_from_env() -> bool | None:
    raw = compat.os.environ.get("BELLO_ADVERSARY_ENABLED", "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _latest_validation_sequence(validations: list[compat.ValidationRun]) -> int | None:
    return max((validation.sequence for validation in validations), default=None)


_ADVERSARY_REPORT_DEFINITIONS = (
    "Finding: a confirmed defect that requires correction.\n"
    "Observation: a concern that is not yet confirmed; investigate it and fix it only if confirmed."
)


def _adversary_report_with_definitions(report_to_coder: str) -> str:
    return f"{compat._ADVERSARY_REPORT_DEFINITIONS}\n\n{report_to_coder.strip()}"


def _final_adversary_report_summary(report: compat.AdversaryReport | None) -> list[str]:
    if report is None:
        return []
    first_line = next((line.strip() for line in report.report_text.splitlines() if line.strip()), "")
    if len(first_line) > 240:
        first_line = first_line[:237].rstrip() + "..."
    details = [
        f"status={report.status}",
        f"candidate_finding={str(report.candidate_finding).lower()}",
        f"completion_wake_sequence={report.completion_wake_sequence}",
        f"latest_relevant_change_sequence={report.latest_relevant_change_sequence}",
    ]
    if first_line:
        details.append(f"summary={first_line}")
    return ["; ".join(details)]
