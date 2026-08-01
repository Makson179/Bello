from __future__ import annotations

from typing import Any, Literal, Mapping


UNLIMITED_REVIEW_LIMIT = "unlimited"
EXPLICIT_REVIEW_LIMIT_FORMAT = "explicit"
REVIEW_LIMIT_FORMAT_FIELD = "review_limit_format"
REVIEW_LIMIT_FIELDS = (
    "max_completion_returns_before_adversary",
    "max_completion_returns_after_adversary",
)
LEGACY_REVIEW_LIMIT_FIELDS = ("max_completion_returns_per_generation",)
LEGACY_REVIEW_LIMIT_DEFAULTS = {
    "max_completion_returns_before_adversary": 4,
    "max_completion_returns_after_adversary": 2,
}

ReviewLimit = int | Literal["unlimited"]


def normalize_review_limit(value: Any) -> ReviewLimit:
    if isinstance(value, str) and value.strip().lower() == UNLIMITED_REVIEW_LIMIT:
        return UNLIMITED_REVIEW_LIMIT
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("must be a non-negative integer or 'unlimited'")


def normalize_review_limit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert pre-explicit zero limits without changing their old meaning."""

    normalized = dict(payload)
    legacy_zero_is_unlimited = REVIEW_LIMIT_FORMAT_FIELD not in normalized
    if legacy_zero_is_unlimited:
        before_field = "max_completion_returns_before_adversary"
        legacy_before_field = LEGACY_REVIEW_LIMIT_FIELDS[0]
        if before_field not in normalized:
            normalized[before_field] = normalized.get(
                legacy_before_field,
                LEGACY_REVIEW_LIMIT_DEFAULTS[before_field],
            )
        normalized.setdefault(
            "max_completion_returns_after_adversary",
            LEGACY_REVIEW_LIMIT_DEFAULTS["max_completion_returns_after_adversary"],
        )
        for field in (*REVIEW_LIMIT_FIELDS, *LEGACY_REVIEW_LIMIT_FIELDS):
            value = normalized.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value == 0:
                normalized[field] = UNLIMITED_REVIEW_LIMIT
        normalized[REVIEW_LIMIT_FORMAT_FIELD] = EXPLICIT_REVIEW_LIMIT_FORMAT
    return normalized


def review_limit_reached(limit: ReviewLimit, count: int) -> bool:
    return limit != UNLIMITED_REVIEW_LIMIT and count >= limit


def format_review_limit(limit: ReviewLimit) -> str:
    return "Unlimited" if limit == UNLIMITED_REVIEW_LIMIT else str(limit)
