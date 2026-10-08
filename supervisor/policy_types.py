"""Shared command-analysis data contracts for deterministic policy decisions.

Runtime dependencies are looked up through ``supervisor.policy`` so existing
imports and monkeypatches keep their original effect. The local imports defer
that lookup until a call; these helpers own no mutable engine state.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from supervisor.schemas import PolicyDecision


ShellKind = Literal["posix", "powershell", "cmd"]


class ParsedCommandSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executable: str
    args: list[str] = Field(default_factory=list)
    tokens: list[str] = Field(default_factory=list)
    raw_paths: list[str] = Field(default_factory=list)
    resolved_paths: list[str] = Field(default_factory=list)
    read_only: bool = False


class CommandAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    tokens: list[str] = Field(default_factory=list)
    segments: list[ParsedCommandSegment] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    resolved_paths: list[str] = Field(default_factory=list)
    risk_tags: set[str] = Field(default_factory=set)
    parse_error: str | None = None

    def policy_payload(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["risk_tags"] = sorted(self.risk_tags)
        return data


def command_analysis_from_policy_decision(evaluation: PolicyDecision) -> CommandAnalysis | None:
    from supervisor import policy as _policy

    raw = evaluation.payload.get("command_analysis")
    if isinstance(raw, _policy.CommandAnalysis):
        return raw
    if isinstance(raw, dict):
        try:
            return _policy.CommandAnalysis.model_validate(raw)
        except _policy.ValidationError:
            return None
    return None
