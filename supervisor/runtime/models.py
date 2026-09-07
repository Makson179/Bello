"""Explicit provider identity: authentication and billing are never fallbacks."""

from __future__ import annotations

import re
from dataclasses import dataclass


class ModelSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str

    @property
    def engine(self) -> str:
        return "claude-code" if self.provider == "claude-code" else "pi"

    @property
    def qualified(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def billing_route(self) -> str:
        if self.provider in {"openai-codex", "claude-code"}:
            return "subscription"
        return "provider-api"


def parse_model_selection(value: str) -> ModelSelection:
    """Keep 0.5.x OpenAI configs working without changing their billing route.

    Qualified model ids split at the first slash, so providers can retain model
    namespaces (for example openrouter/qwen/qwen3-coder).
    """
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ModelSelectionError("model must be a nonempty model id without surrounding whitespace")
    if any(ord(char) < 33 for char in value):
        raise ModelSelectionError("model id cannot contain whitespace or control characters")
    if "/" not in value:
        if value.startswith("gpt-"):
            return ModelSelection("openai-codex", value)
        raise ModelSelectionError("use an explicit provider/model id, for example anthropic/claude-sonnet-4-6")
    provider, model = value.split("/", 1)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", provider) or not model or model.startswith("/"):
        raise ModelSelectionError("invalid provider/model id")
    return ModelSelection(provider, model)


def validate_effort(effort: str | None, supported: list[str] | tuple[str, ...]) -> None:
    if effort is not None and effort not in supported:
        raise ModelSelectionError(
            f"reasoning effort {effort!r} is not supported; available: {', '.join(supported) or 'none'}. "
            "Bello will not substitute a different effort."
        )
