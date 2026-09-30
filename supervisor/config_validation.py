"""Offline configuration checks shared by ``bello config`` and run preflight.

Pure functions only: no login, model request, or engine start. They read a
ProjectConfig and a model catalog the editor already obtained through normal
engine discovery. They mirror the deterministic parts of run preflight:

* which role and subagent profiles are active (``preflight_profiles`` is the one
  enumeration used by both the editor and ``BelloController._runtime_preflight``);
* model availability for the signed-in accounts;
* the advertised efforts of each exact profile, including models that advertise
  none (known-empty) as opposed to a missing catalog (unknown);
* Bello Fast: the OpenAI/Codex ``priority`` service tier that preflight requests
  for every active profile.

A green result means only "these offline checks pass"; login, sandbox and
structured-output checks still run when a task starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Any, Iterable, Literal, Mapping

from supervisor.project_config import MultiAgentConfig, ProjectConfig
from supervisor.runtime.engine_status import ENGINE_LABELS, EngineFailure, classify_engine_failure
from supervisor.runtime.models import NO_EFFORT, ModelSelectionError, parse_model_selection


# Bello's effort order, lowest to highest. Unsupported values are clamped toward
# the nearest supported level that does not exceed the previous choice.
EFFORT_ORDER = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# The Claude Code backend's engine contract (supervisor.runtime.claude.SUPPORTED_EFFORTS).
CLAUDE_CODE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAST_SERVICE_TIER = "priority"
FAST_HELP = (
    "fast requests Bello Fast: the OpenAI/Codex priority service tier (billed at that tier's rates) for coder, "
    "revision-coder, runtime and completion-review turns and the subagents they start. It is not Smart Execution "
    "(async-tools) and not Claude Code's differently named fast mode. Preflight requires the tier for every active "
    "role and allowed subagent profile; Claude Code subscription models do not offer it. usual leaves the tier unset."
)


# --- Active profiles ---------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileUse:
    """One (model, effort) profile that a run validates before model work."""

    role: str
    model: str
    effort: str
    model_setting: str
    effort_setting: str


def preflight_profiles(
    *,
    coder: tuple[str, str],
    completion: tuple[str, str] | None = None,
    revision_coder: tuple[str, str] | None = None,
    adversary: tuple[str, str] | None = None,
    runtime: tuple[str, str] | None = None,
    policies: Iterable[tuple[str, Any]] = (),
) -> tuple[ProfileUse, ...]:
    """The exact profiles run preflight validates, in preflight order.

    ``policies`` are (config field, MultiAgentConfig) pairs for active roles;
    disabled policies contribute nothing.
    """
    uses = [ProfileUse("coder", coder[0], coder[1], "coder_mod", "coder_intelligence")]
    if completion is not None:
        uses.append(ProfileUse("completion", *completion, "completion_mod", "completion_intelligence"))
    if revision_coder is not None:
        uses.append(ProfileUse("revision-coder", *revision_coder, "revision_coder_mod", "revision_coder_intelligence"))
    if adversary is not None:
        uses.append(ProfileUse("adversary", *adversary, "adversary_mod", "adversary_intelligence"))
    for field_name, policy in policies:
        if not getattr(policy, "enabled", False):
            continue
        for model, efforts in policy.allowed.items():
            key = f"{field_name}_allowed:{model}"
            uses.extend(ProfileUse("subagent", model, effort, key, key) for effort in efforts)
    if runtime is not None:
        uses.append(ProfileUse("runtime", *runtime, "runtime_mod", "runtime_intelligence"))
    return tuple(uses)


def adversary_active(config: ProjectConfig) -> bool:
    return bool(config.adversary and config.adversary_runs > 0)


def project_profiles(config: ProjectConfig) -> tuple[ProfileUse, ...]:
    """Active profiles of a saved configuration, as a run would check them."""
    adversary = adversary_active(config)
    review = config.completion_review or adversary
    policies: list[tuple[str, MultiAgentConfig]] = [("multi_agent", config.multi_agent)]
    if config.completion_review:
        policies.append(("completion_multi_agent", config.completion_multi_agent))
    if adversary:
        policies.append(("adversary_multi_agent", config.adversary_multi_agent))
    return preflight_profiles(
        coder=(config.coder_mod, config.coder_intelligence),
        completion=(config.completion_mod, config.completion_intelligence) if config.completion_review else None,
        revision_coder=((config.revision_coder_mod, config.revision_coder_intelligence)
                        if config.revision_coder_enabled and review else None),
        adversary=(config.adversary_mod, config.adversary_intelligence) if adversary else None,
        runtime=(config.runtime_mod, config.runtime_intelligence) if config.runtime_enabled else None,
        policies=policies,
    )


# --- Catalog -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogModel:
    qualified_id: str
    engine: str
    efforts: tuple[str, ...] | None  # None: not advertised (unknown); (): advertises none
    default_effort: str | None = None
    supports_service_tier: bool | None = None
    service_tiers: tuple[str, ...] | None = None
    display_name: str | None = None
    description: str | None = None
    resolved_model: str | None = None
    alias: bool = False


@dataclass(frozen=True)
class ModelCatalog:
    """What discovery established, keeping unknown distinct from empty."""

    models: Mapping[str, CatalogModel] = field(default_factory=dict)
    failures: Mapping[str, EngineFailure] = field(default_factory=dict)
    discovered: bool = False
    discovery_error: str | None = None

    def get(self, model: str) -> CatalogModel | None:
        entry = self.models.get(model)
        if entry is None and "/" not in model:
            try:
                entry = self.models.get(parse_model_selection(model).qualified)
            except ModelSelectionError:
                return None
        return entry

    def available(self, model: str) -> bool:
        return self.get(model) is not None


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_text(value: Any, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_CONTROL.sub(" ", value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(dict.fromkeys(value))


def catalog_entry(descriptor: Mapping[str, Any]) -> CatalogModel | None:
    """Normalize one model/list descriptor; unavailable descriptors are ignored."""
    if (descriptor.get("hidden") is True or descriptor.get("visibility") == "hidden"
            or descriptor.get("configured") is False or descriptor.get("available") is False):
        return None
    qualified = descriptor.get("qualifiedId")
    if not isinstance(qualified, str):
        return None
    try:
        selection = parse_model_selection(qualified)
    except ModelSelectionError:
        return None
    default = descriptor.get("defaultEffort", descriptor.get("defaultReasoningEffort"))
    supports_tier = descriptor.get("supportsServiceTier")
    resolved = descriptor.get("resolvedModel")
    return CatalogModel(
        qualified_id=selection.qualified,
        engine=selection.engine,
        efforts=_string_tuple(descriptor.get("supportedEfforts")),
        default_effort=default if isinstance(default, str) else None,
        supports_service_tier=supports_tier if isinstance(supports_tier, bool) else None,
        service_tiers=_string_tuple(descriptor.get("supportedServiceTiers")),
        display_name=_clean_text(descriptor.get("displayName") or descriptor.get("name"), 60),
        description=_clean_text(descriptor.get("description")),
        resolved_model=resolved if isinstance(resolved, str) and resolved else None,
        alias=descriptor.get("alias") is True or (
            selection.engine == "claude-code" and isinstance(resolved, str) and bool(resolved)
            and resolved != selection.model
        ),
    )


def catalog_from_model_list(response: Mapping[str, Any] | None, *, error: str | None = None) -> ModelCatalog:
    """Build the editor catalog from a RuntimeClient model/list response."""
    if response is None:
        # Discovery did not run or failed as a whole: every model is unknown.
        return ModelCatalog(discovered=False, discovery_error=error)
    models: dict[str, CatalogModel] = {}
    for descriptor in response.get("data", []) if isinstance(response.get("data"), list) else []:
        if not isinstance(descriptor, dict):
            continue
        entry = catalog_entry(descriptor)
        if entry is not None:
            models.setdefault(entry.qualified_id, entry)
    failures: dict[str, EngineFailure] = {}
    reasons = response.get("unavailableReasons")
    raw = response.get("unavailableEngines")
    for engine, message in (raw.items() if isinstance(raw, dict) else ()):
        reason = reasons.get(engine) if isinstance(reasons, dict) else None
        if isinstance(reason, dict) and reason.get("kind") in {"login", "dependency", "unsupported-setting", "unavailable"} \
                and isinstance(reason.get("summary"), str):
            failures[engine] = EngineFailure(engine, reason["kind"], reason["summary"],
                                             reason.get("action") if isinstance(reason.get("action"), str) else None)
        else:
            failures[engine] = classify_engine_failure(engine, message)
    return ModelCatalog(models=models, failures=failures, discovered=True)


# --- Human-readable model identity -------------------------------------------------------


_CLAUDE_ID = re.compile(
    r"claude-(?P<family>[a-z]+)-(?P<major>\d{1,2})(?:-(?P<minor>\d{1,2}))?(?:-(?P<date>\d{8}))?(?P<suffix>\[[a-z0-9]+\])?\Z"
)


def claude_model_name(model_id: str) -> str | None:
    """`claude-sonnet-5-5` -> `Sonnet 5.5`; `claude-opus-5-5[1m]` -> `Opus 5.5 [1m]`."""
    match = _CLAUDE_ID.fullmatch(model_id)
    if match is None:
        return None
    version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
    suffix = f" {match['suffix']}" if match["suffix"] else ""
    return f"{match['family'].title()} {version}{suffix}"


def model_label(model: str, catalog: ModelCatalog | None) -> str:
    """Exact identity first; floating aliases show what they resolve to now."""
    try:
        selection = parse_model_selection(model)
    except ModelSelectionError:
        return model
    if selection.engine != "claude-code":
        return model
    entry = catalog.get(model) if catalog is not None else None
    if entry is not None and entry.alias and entry.resolved_model:
        name = claude_model_name(entry.resolved_model)
        target = f"{name} ({entry.resolved_model})" if name else entry.resolved_model
        return f"{model} - alias, now {target}"
    name = claude_model_name(selection.model)
    return f"{name} - {model}" if name else model


def model_detail(model: str, catalog: ModelCatalog | None) -> str:
    """One sentence for the focused model row's help text."""
    entry = catalog.get(model) if catalog is not None else None
    try:
        selection = parse_model_selection(model)
    except ModelSelectionError:
        return ""
    parts: list[str] = []
    if selection.engine == "claude-code":
        parts.append(f"{model} uses your Claude subscription through the official Claude Code CLI "
                     "(anthropic/<model> would be API billing through Pi).")
        if entry is not None and entry.alias and entry.resolved_model:
            parts.append(f"It is a floating alias: Claude Code currently resolves it to {entry.resolved_model}; "
                         "a future CLI may resolve it to a newer model. Choose the exact id to pin it.")
    if entry is not None and entry.description:
        parts.append(f"Catalog note: {entry.description}.")
    if entry is not None and entry.efforts == ():
        parts.append(f"It advertises no effort setting; use effort '{NO_EFFORT}' (Bello sends none).")
    return " ".join(parts)


# --- Effort policy ----------------------------------------------------------------------


def choose_effort(current: str, supported: tuple[str, ...], *, advertised_default: str | None = None) -> str:
    """Pick the effort kept after a model change.

    Keep a supported effort. A model with no effort levels uses ``default``.
    Otherwise take the nearest supported level that does not exceed the previous
    one (the lowest level if none is lower), so a change never raises cost to
    the maximum. Coming from ``default`` (no effort), use the model's advertised
    default, else ``high`` (Bello's subagent default), else the middle level.
    """
    if not supported:
        return NO_EFFORT
    if current in supported:
        return current
    if current not in EFFORT_ORDER:
        if advertised_default in supported:
            return advertised_default
        if "high" in supported:
            return "high"
        return supported[len(supported) // 2]
    rank = EFFORT_ORDER.index(current)
    ranked = [effort for effort in supported if effort in EFFORT_ORDER]
    lower = [effort for effort in ranked if EFFORT_ORDER.index(effort) <= rank]
    if lower:
        return max(lower, key=EFFORT_ORDER.index)
    if ranked:
        return min(ranked, key=EFFORT_ORDER.index)
    return supported[0]


# --- Validation --------------------------------------------------------------------------


IssueLevel = Literal["error", "warning"]
IssueCategory = Literal["availability", "effort", "fast", "triage", "dependency"]


@dataclass(frozen=True)
class ConfigIssue:
    level: IssueLevel
    category: IssueCategory
    title: str
    message: str
    settings: tuple[str, ...]


@dataclass(frozen=True)
class ConfigReport:
    issues: tuple[ConfigIssue, ...]
    verified: bool
    failures: tuple[EngineFailure, ...] = ()
    discovery_error: str | None = None

    @property
    def errors(self) -> tuple[ConfigIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "error")

    @property
    def warnings(self) -> tuple[ConfigIssue, ...]:
        return tuple(issue for issue in self.issues if issue.level == "warning")

    def for_setting(self, key: str) -> tuple[ConfigIssue, ...]:
        found = [issue for issue in self.issues if key in issue.settings]
        return tuple(sorted(found, key=lambda issue: issue.level != "error"))


def fast_support(model: str, entry: CatalogModel | None) -> Literal["supported", "unsupported", "unknown"]:
    """Whether preflight's ``serviceTier: priority`` is accepted for this profile."""
    try:
        engine = parse_model_selection(model).engine
    except ModelSelectionError:
        return "unknown"
    if entry is not None and entry.supports_service_tier is False:
        return "unsupported"
    if entry is not None and entry.supports_service_tier is True:
        if entry.service_tiers is None or FAST_SERVICE_TIER in entry.service_tiers:
            return "supported"
        return "unsupported"
    if engine == "claude-code":
        return "unsupported"  # the backend rejects any service tier
    if engine == "codex":
        return "supported"  # the native Codex backend accepts priority for exact subscription models
    return "unknown"


def _engine_label(model: str) -> str:
    try:
        return ENGINE_LABELS.get(parse_model_selection(model).engine, "its engine")
    except ModelSelectionError:
        return "its engine"


def validate_project_config(
    config: ProjectConfig,
    catalog: ModelCatalog | None,
    *,
    triage_model: str | None = None,
) -> ConfigReport:
    """Offline checks that a run's preflight would fail deterministically."""
    catalog = catalog or ModelCatalog()
    issues: list[ConfigIssue] = []
    uses = project_profiles(config)
    by_model: dict[str, list[ProfileUse]] = {}
    for use in uses:
        by_model.setdefault(use.model, []).append(use)

    for model, model_uses in by_model.items():
        settings = tuple(dict.fromkeys(use.model_setting for use in model_uses))
        try:
            selection = parse_model_selection(model)
        except ModelSelectionError as exc:
            issues.append(ConfigIssue("error", "availability", "Invalid model id", f"{model}: {exc}", settings))
            continue
        entry = catalog.get(model)
        if entry is None and catalog.discovered:
            failure = catalog.failures.get(selection.engine)
            if failure is not None:
                message = f"{model} is unavailable because {failure.text()}"
            else:
                message = (f"{model} is not advertised by {ENGINE_LABELS.get(selection.engine, selection.engine)} "
                           "for the signed-in account.")
                if selection.engine == "claude-code" and selection.model.endswith("[1m]"):
                    message += (" The current Claude Code catalog does not list this 1M-context id. Bello keeps "
                                "the saved value and does not infer a smaller context window from its absence; "
                                "choose a listed model explicitly if you want to change it.")
            issues.append(ConfigIssue(
                "error", "availability", "Saved model unavailable",
                message + " The saved value is kept; nothing is replaced automatically.", settings,
            ))
        issues.extend(_effort_issues(model, selection.engine, entry, model_uses))

    if config.fast:
        unsupported: list[ProfileUse] = []
        unknown: list[ProfileUse] = []
        for model, model_uses in by_model.items():
            support = fast_support(model, catalog.get(model))
            if support == "unsupported":
                unsupported.extend(model_uses)
            elif support == "unknown":
                unknown.extend(model_uses)
        if unsupported:
            models = list(dict.fromkeys(use.model for use in unsupported))
            issues.append(ConfigIssue(
                "error", "fast", "Fast not available",
                "speed=fast requests the OpenAI/Codex priority service tier for every active profile, but "
                + ", ".join(f"{model} ({_engine_label(model)})" for model in models)
                + " does not offer it. Choose speed usual, or use models that advertise the tier. Bello does "
                "not substitute another provider, model or speed.",
                ("speed", *dict.fromkeys(use.model_setting for use in unsupported)),
            ))
        if unknown and catalog.discovered:
            models = list(dict.fromkeys(use.model for use in unknown))
            issues.append(ConfigIssue(
                "warning", "fast", "Fast not verified",
                "The catalog does not state whether " + ", ".join(models) + " offers the priority service tier; "
                "preflight will check it before any model work.",
                ("speed", *dict.fromkeys(use.model_setting for use in unknown)),
            ))

    if config.log_distiller.enabled:
        # Run start checks the same optional packages (by presence only) and
        # stops before any model work when they are missing.
        from supervisor.runtime.distiller import require_dependencies

        try:
            require_dependencies()
        except RuntimeError as exc:
            issues.append(ConfigIssue("error", "dependency", "Distiller dependencies missing", str(exc),
                                      ("log_distiller_enabled",)))

    if config.runtime_enabled and config.cheap_runtime and catalog.discovered:
        model = triage_model or _default_triage_model()
        if model and not catalog.available(model):
            issues.append(ConfigIssue(
                "warning", "triage", "Cheap triage route not connected",
                f"cheap-runtime triage uses {model} ({_engine_label(model)}), which is not connected. The run "
                "continues with the full runtime supervisor for every check (slower and costlier). Set "
                "cheap-runtime false to make that explicit, or connect the triage route.",
                ("cheap_runtime",),
            ))
    failures = tuple(catalog.failures[name] for name in sorted(catalog.failures))
    return ConfigReport(issues=tuple(issues), verified=catalog.discovered, failures=failures,
                        discovery_error=catalog.discovery_error)


def _effort_issues(model: str, engine: str, entry: CatalogModel | None,
                   uses: list[ProfileUse]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for effort in dict.fromkeys(use.effort for use in uses):
        settings = tuple(dict.fromkeys(use.effort_setting for use in uses if use.effort == effort))
        if entry is not None and entry.efforts is not None:
            if not entry.efforts:
                if effort != NO_EFFORT:
                    issues.append(ConfigIssue(
                        "error", "effort", "Effort not advertised",
                        f"{model} advertises no effort setting, so '{effort}' would fail preflight. Choose "
                        f"'{NO_EFFORT}' (Bello sends no effort).", settings,
                    ))
                continue
            if effort == NO_EFFORT:
                if engine == "pi" and entry.default_effort is None:
                    issues.append(ConfigIssue(
                        "error", "effort", "Effort required",
                        f"{model} has no default effort; choose one of: {', '.join(entry.efforts)}.", settings,
                    ))
                continue
            if effort not in entry.efforts:
                issues.append(ConfigIssue(
                    "error", "effort", "Effort not advertised",
                    f"'{effort}' is not advertised for {model}; available: {', '.join(entry.efforts)}. "
                    "Bello will not substitute an effort.", settings,
                ))
        elif engine == "claude-code" and effort != NO_EFFORT and effort not in CLAUDE_CODE_EFFORTS:
            issues.append(ConfigIssue(
                "error", "effort", "Effort not supported",
                f"Claude Code accepts only {', '.join(CLAUDE_CODE_EFFORTS)} (or '{NO_EFFORT}'); '{effort}' would "
                f"fail preflight for {model}.", settings,
            ))
    return issues


def _default_triage_model() -> str | None:
    from supervisor.approval_triage import RUNTIME_TRIAGE_MODEL_ENV, DEFAULT_TRIAGE_MODEL

    value = os.environ.get(RUNTIME_TRIAGE_MODEL_ENV, "").strip()
    return value or DEFAULT_TRIAGE_MODEL
