#!/usr/bin/env python3
"""Validate a fully resolved Bello config-advisor recommendation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_models import qualified_model, summarize_catalog

# This is project-file syntax, not a promise that each model supports every value.
CONFIG_EFFORTS = {"off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}

REQUIRED_KEYS = {
    "review_limit_format",
    "task",
    "coder_mod",
    "revision_coder_enabled",
    "revision_coder_mod",
    "runtime_mod",
    "completion_mod",
    "adversary_mod",
    "coder_intelligence",
    "revision_coder_intelligence",
    "runtime_intelligence",
    "completion_intelligence",
    "adversary_intelligence",
    "speed",
    "cheap_runtime",
    "start_over",
    "completion_review",
    "adversary",
    "max_adversary_runs",
    "max_completion_returns_before_adversary",
    "max_completion_returns_after_adversary",
    "clean",
    "protected_path",
    "multi_agent",
    "completion_multi_agent",
    "adversary_multi_agent",
}


def _is_int(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _validate_review_limit(value: Any, field: str, errors: list[str], *, allow_unlimited: bool) -> None:
    if _is_int(value):
        return
    if value == "unlimited":
        if not allow_unlimited:
            errors.append(f"{field}: unlimited is not allowed for a cost-sensitive recommendation")
        return
    errors.append(f"{field}: expected a non-negative integer or 'unlimited'")


def _validate_profile(
    model: Any, effort: Any, field: str, errors: list[str], *,
    catalog: dict[str, dict[str, Any]], active: bool, fast: bool,
) -> None:
    try:
        identity = qualified_model(model)
    except ValueError as exc:
        errors.append(f"{field}: {exc}")
        return
    if not isinstance(effort, str) or effort not in CONFIG_EFFORTS:
        errors.append(f"{field}: invalid project effort {effort!r}")
        return
    if not active:
        return
    entry = catalog.get(identity)
    if entry is None or entry.get("available") is False or entry.get("configured") is False:
        errors.append(f"{field}: {identity} is not available in the supplied catalog")
        return
    if effort not in entry["supportedEfforts"]:
        errors.append(f"{field}: {effort!r} is not advertised for {identity}")
    if fast and (entry.get("supportsServiceTier") is not True or "priority" not in entry.get("supportedServiceTiers", [])):
        errors.append(f"{field}: speed=fast requires advertised priority service tier support")


def _validate_role(config: dict[str, Any], role: str, errors: list[str], *, catalog: dict, active: bool) -> None:
    model_key = f"{role}_mod"
    effort_key = f"{role}_intelligence"
    model = config.get(model_key)
    effort = config.get(effort_key)
    _validate_profile(model, effort, f"{model_key}/{effort_key}", errors,
                      catalog=catalog, active=active, fast=config.get("speed") == "fast")


def _validate_multi_agent(value: Any, field_name: str, errors: list[str], *, catalog: dict, active: bool, fast: bool) -> None:
    if not isinstance(value, dict):
        errors.append(f"{field_name}: expected an object")
        return
    expected = {"enabled", "max_concurrent", "default", "allowed"}
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        errors.append(f"{field_name}: missing keys {sorted(missing)}")
    if extra:
        errors.append(f"{field_name}: unknown keys {sorted(extra)}")
    if not isinstance(value.get("enabled"), bool):
        errors.append(f"{field_name}.enabled: expected boolean")
    if not _is_int(value.get("max_concurrent"), minimum=1):
        errors.append(f"{field_name}.max_concurrent: expected a positive integer")

    allowed = value.get("allowed")
    if not isinstance(allowed, dict) or not allowed:
        errors.append(f"{field_name}.allowed: expected a non-empty object")
        return
    normalized_allowed: dict[str, set[str]] = {}
    for model, efforts in allowed.items():
        if not isinstance(efforts, list) or not efforts:
            errors.append(f"{field_name}.allowed.{model}: expected a non-empty list")
            continue
        if any(not isinstance(effort, str) for effort in efforts):
            errors.append(f"{field_name}.allowed.{model}: efforts must be strings")
            continue
        if len(efforts) != len(set(efforts)):
            errors.append(f"{field_name}.allowed.{model}: duplicate efforts")
        for effort in efforts:
            _validate_profile(model, effort, f"{field_name}.allowed.{model}", errors,
                              catalog=catalog, active=active and value.get("enabled") is True, fast=fast)
        normalized_allowed[model] = set(efforts)

    default = value.get("default")
    if not isinstance(default, dict) or set(default) != {"model", "intelligence"}:
        errors.append(f"{field_name}.default: expected exactly model and intelligence")
        return
    default_model = default.get("model")
    default_effort = default.get("intelligence")
    if not isinstance(default_model, str) or not isinstance(default_effort, str):
        errors.append(f"{field_name}.default: model and intelligence must be strings")
    elif default_effort not in normalized_allowed.get(default_model, set()):
        errors.append(
            f"{field_name}.default: profile must be present in {field_name}.allowed"
        )


def _task_path_error(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "expected a non-empty project-root-relative path"
    if value != value.strip():
        return "must not contain leading or trailing whitespace"
    if "\\" in value:
        return "must use '/' separators"

    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        return "must be project-root-relative, not absolute, drive-relative, or UNC"
    if value in {".", "./"} or ".." in posix.parts:
        return "must name a file inside the project root without '..'"
    if posix.as_posix() != value:
        return "must be normalized without './' or repeated separators"
    return None


def _resolve_expected_task(project_root: Path, task_file: Path) -> str:
    root = project_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {project_root}")

    candidate = task_file.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    task = candidate.resolve(strict=True)
    if not task.is_file():
        raise ValueError(f"task is not a regular file: {task_file}")
    try:
        relative = task.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"task is outside the project root: {task}") from exc
    return relative.as_posix()


def validate(
    config: Any,
    *,
    allow_clean: bool,
    allow_unlimited: bool,
    expected_task: str | None = None,
    catalog: Any = None,
    triage_model: str = "openai-codex/gpt-5.6-luna",
) -> list[str]:
    if not isinstance(config, dict):
        return ["root: expected a JSON object"]

    errors: list[str] = []
    try:
        entries = {item["qualifiedId"]: item for item in summarize_catalog(catalog)}
    except ValueError as exc:
        return [f"catalog: {exc}; supply a current catalog from inspect_models.py"]
    missing = REQUIRED_KEYS - config.keys()
    extra = config.keys() - REQUIRED_KEYS
    if missing:
        errors.append(f"root: missing keys {sorted(missing)}")
    if extra:
        errors.append(f"root: unknown keys {sorted(extra)}")

    if config.get("review_limit_format") != "explicit":
        errors.append("review_limit_format: must be 'explicit'")
    task = config.get("task")
    task_error = _task_path_error(task)
    if task_error:
        errors.append(f"task: {task_error}")
    elif expected_task is not None and task != expected_task:
        errors.append(f"task: expected the resolved input task {expected_task!r}")
    review = config.get("completion_review") is True
    active_roles = {"coder", "runtime"}
    if review:
        active_roles.add("completion")  # Also used by the adversary-report controller.
        if config.get("revision_coder_enabled") is True:
            active_roles.add("revision_coder")
        if config.get("adversary") is True:
            active_roles.add("adversary")
    for role in ("coder", "revision_coder", "runtime", "completion", "adversary"):
        _validate_role(config, role, errors, catalog=entries, active=role in active_roles)
    if config.get("cheap_runtime") is True:
        try:
            identity = qualified_model(triage_model, allow_legacy=True)
            triage = entries.get(identity)
            if triage is None or triage.get("available") is False or triage.get("configured") is False:
                errors.append("cheap_runtime: triage model is unavailable; disable it or verify an explicit triage override")
        except ValueError as exc:
            errors.append(f"cheap_runtime: {exc}")
    if not isinstance(config.get("speed"), str) or config["speed"] not in {"usual", "fast"}:
        errors.append("speed: expected 'usual' or 'fast'")
    for field in (
        "revision_coder_enabled",
        "cheap_runtime",
        "start_over",
        "completion_review",
        "adversary",
        "clean",
    ):
        if not isinstance(config.get(field), bool):
            errors.append(f"{field}: expected boolean")
    if config.get("clean") is True and not allow_clean:
        errors.append("clean: true requires explicit authorization and --allow-clean")

    if not _is_int(config.get("max_adversary_runs")):
        errors.append("max_adversary_runs: expected a non-negative integer")
    for field in (
        "max_completion_returns_before_adversary",
        "max_completion_returns_after_adversary",
    ):
        _validate_review_limit(config.get(field), field, errors, allow_unlimited=allow_unlimited)

    completion = config.get("completion_review")
    adversary = config.get("adversary")
    adversary_runs = config.get("max_adversary_runs")
    before = config.get("max_completion_returns_before_adversary")
    after = config.get("max_completion_returns_after_adversary")
    if completion is False:
        if config.get("revision_coder_enabled") is True:
            errors.append("revision_coder_enabled: runtime-only has no review finding to hand off")
        if adversary is not False or adversary_runs != 0 or before != 0 or after != 0:
            errors.append("review pipeline: runtime-only requires adversary=false and all review budgets=0")
    elif completion is True:
        if adversary is True:
            if not _is_int(adversary_runs, minimum=1):
                errors.append("review pipeline: adversary=true requires max_adversary_runs >= 1")
        elif adversary is False:
            if adversary_runs != 0 or after != 0:
                errors.append("review pipeline: adversary=false requires adversary runs and post-adversary returns=0")
            if before == 0:
                errors.append("review pipeline: completion review has no scheduled return budget")

    protected = config.get("protected_path")
    if not isinstance(protected, list) or any(not isinstance(item, str) or not item.strip() for item in protected):
        errors.append("protected_path: expected a list of non-empty strings")
    elif len(protected) != len(set(protected)):
        errors.append("protected_path: duplicate entries")

    for field in (
        "multi_agent",
        "completion_multi_agent",
        "adversary_multi_agent",
    ):
        active = field == "multi_agent" or (review and (field == "completion_multi_agent" or config.get("adversary") is True))
        _validate_multi_agent(config.get(field), field, errors, catalog=entries,
                              active=active, fast=config.get("speed") == "fast")
    return errors


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="Read JSON from standard input")
    source.add_argument("--file", type=Path, help="Read JSON from a file")
    parser.add_argument("--catalog", type=Path, required=True, help="Current catalog emitted by inspect_models.py")
    parser.add_argument("--triage-model", default="openai-codex/gpt-5.6-luna",
                        help="Only set when an existing BELLO_RUNTIME_TRIAGE_MODEL override was explicitly verified")
    parser.add_argument("--allow-clean", action="store_true", help="Allow an explicitly authorized clean=true")
    parser.add_argument("--allow-unlimited", action="store_true", help="Allow unlimited review budgets")
    parser.add_argument("--project-root", type=Path, help="Project root used to resolve the exact input task")
    parser.add_argument("--task-file", type=Path, help="Task file supplied to the advisor")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        raw = sys.stdin.read() if args.stdin else args.file.read_text(encoding="utf-8")
        config = json.loads(raw)
        catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2

    if (args.project_root is None) != (args.task_file is None):
        print("invalid: --project-root and --task-file must be provided together", file=sys.stderr)
        return 2
    expected_task = None
    if args.project_root is not None:
        try:
            expected_task = _resolve_expected_task(args.project_root, args.task_file)
        except (OSError, ValueError) as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2

    errors = validate(
        config,
        allow_clean=args.allow_clean,
        allow_unlimited=args.allow_unlimited,
        expected_task=expected_task,
        catalog=catalog,
        triage_model=args.triage_model,
    )
    if errors:
        for error in errors:
            print(f"invalid: {error}", file=sys.stderr)
        return 1
    print("valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
