"""Compatibility comparison for persisted, run-owned context policies."""

from typing import Any


def context_policies_match(saved: Any, requested: Any) -> bool:
    """Inactive defaults may evolve; an enabled policy must stay exact.

    This comparison never migrates the saved policy. Require actual booleans so
    malformed values such as 0 cannot acquire the disabled-policy exception.
    """
    if not isinstance(saved, dict) or not isinstance(requested, dict):
        return False
    if type(saved.get("enabled")) is not bool or type(requested.get("enabled")) is not bool:
        return False
    return (saved["enabled"] is False and requested["enabled"] is False) or saved == requested
