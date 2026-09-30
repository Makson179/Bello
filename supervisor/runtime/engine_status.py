"""Sanitized, actionable reasons for an execution engine that could not list models.

Discovery errors can contain provider payloads, paths or other external text.
Nothing from the original message is shown except environment-variable *names*
from Bello's own fixed list. Every other word is Bello-authored, chosen by
matching Bello's own error messages. Unknown failures say only that details are
hidden and point to ``bello doctor``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


FailureKind = Literal["login", "dependency", "unsupported-setting", "unavailable"]

ENGINE_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "pi": "Pi"}
# Names only; values are never read here. Mirrors the Claude backend's refusal list.
_CLAUDE_ENVIRONMENT_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "ANTHROPIC_BASE_URL", "ANTHROPIC_FEDERATION_RULE_ID",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
})


@dataclass(frozen=True)
class EngineFailure:
    engine: str
    kind: FailureKind
    summary: str
    action: str | None = None

    @property
    def label(self) -> str:
        return ENGINE_LABELS.get(self.engine, self.engine)

    def text(self) -> str:
        suffix = f" Next: {self.action}." if self.action else ""
        return f"{self.label}: {self.summary}.{suffix}"

    def to_json_data(self) -> dict[str, str | None]:
        return {"kind": self.kind, "summary": self.summary, "action": self.action, "text": self.text()}


def _rule(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# (engine or "*", pattern over Bello's own message, kind, summary, action)
_RULES: tuple[tuple[str, re.Pattern[str], FailureKind, str, str | None], ...] = (
    ("claude-code", _rule(r"not signed in"), "login", "not signed in",
     "bello runtime login claude-code"),
    ("claude-code", _rule(r"no paid Claude subscription"), "login",
     "signed in, but the official CLI reported no paid Claude subscription",
     "sign in with a paid Claude account: bello runtime login claude-code"),
    ("claude-code", _rule(r"requires an existing first-party claude\.ai login|non-subscription provider"),
     "unsupported-setting", "signed in through an API key or cloud provider, not a claude.ai subscription",
     "bello runtime login claude-code"),
    ("claude-code", _rule(r"managed policy exposed MCP servers"), "unsupported-setting",
     "a managed Claude Code policy adds MCP servers outside Bello, so Bello refuses to use it",
     "remove those managed MCP servers for this account, then reopen bello config"),
    ("claude-code", _rule(r"requires the pinned claude-agent-sdk package"), "dependency",
     "optional Claude Code support is not installed", "pipx install 'bello[claude]' --force"),
    ("claude-code", _rule(r"is not prepared yet"), "dependency",
     "the official Claude Code CLI for this platform is not prepared", "bello runtime install claude-code"),
    ("claude-code", _rule(r"bundled with claude-agent-sdk is missing"), "dependency",
     "the claude-agent-sdk installation has no bundled CLI", "bello update"),
    ("claude-code", _rule(r"is not the release Bello pairs"), "dependency",
     "the installed claude-agent-sdk does not match this Bello release", "bello update"),
    ("claude-code", _rule(r"cached Claude Code CLI failed verification"), "dependency",
     "Bello's cached Claude Code CLI failed verification", "bello doctor"),
    ("claude-code", _rule(r"download|downloads\.claude\.ai"), "dependency",
     "the official Claude Code CLI could not be downloaded", "bello runtime install claude-code"),
    ("claude-code", _rule(r"auth status"), "unavailable",
     "the official CLI did not report its login status", "bello doctor"),
    ("codex", _rule(r"executable not found|No such file|cannot find|not recognized"), "dependency",
     "the native Codex CLI was not found", "install Codex, then run bello runtime login openai-codex"),
    ("codex", _rule(r"\blogin\b|auth"), "login", "not signed in", "bello runtime login openai-codex"),
    ("codex", _rule(r"bello_async_tools|Async tools requires"), "unsupported-setting",
     "this Codex build does not support Smart Execution (async tools)",
     "turn async-tools off or use a compatible Codex build"),
    ("pi", _rule(r"Node\.js|node executable|\bnode\b"), "dependency",
     "Node.js 22.19 or newer is required for Pi providers", "install Node.js, then bello runtime install pi"),
    ("pi", _rule(r"Pi dependencies are not installed|Pi installation|Pi runtime"), "dependency",
     "the pinned Pi runtime is not installed", "bello runtime install pi"),
    ("*", _rule(r"not signed in|not authenticated|not configured"), "login", "not signed in", None),
)
_ENVIRONMENT_REFUSAL = _rule(r"refuses environment-based API/provider auth")


def classify_engine_failure(engine: str, message: object) -> EngineFailure:
    """Map one discovery error to Bello-authored text without echoing it."""
    text = message if isinstance(message, str) else ""
    if engine == "claude-code" and _ENVIRONMENT_REFUSAL.search(text):
        names = sorted({name for name in re.findall(r"[A-Z][A-Z0-9_]{2,63}", text)
                        if name in _CLAUDE_ENVIRONMENT_NAMES})
        listed = ", ".join(names) if names else "provider variables"
        return EngineFailure(
            engine, "unsupported-setting",
            f"environment variables would switch it away from the subscription route ({listed})",
            "unset them for Bello; use an explicit anthropic/<model> Pi route for API billing",
        )
    for rule_engine, pattern, kind, summary, action in _RULES:
        if rule_engine in {engine, "*"} and pattern.search(text):
            return EngineFailure(engine, kind, summary, action or _default_login(engine))
    return EngineFailure(engine, "unavailable", "unavailable (details hidden)", "bello doctor")


def _default_login(engine: str) -> str | None:
    return {"claude-code": "bello runtime login claude-code",
            "codex": "bello runtime login openai-codex"}.get(engine)


def sanitized_unavailable_engines(unavailable: dict[str, object]) -> tuple[dict[str, str], dict[str, dict]]:
    """Return (engine -> sanitized text, engine -> structured reason)."""
    texts: dict[str, str] = {}
    reasons: dict[str, dict] = {}
    for engine, message in unavailable.items():
        failure = classify_engine_failure(engine, message)
        texts[engine] = failure.text()
        reasons[engine] = failure.to_json_data()
    return texts, reasons
