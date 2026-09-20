#!/usr/bin/env python3
"""Inspect Bello's configured model catalog without logging in or running a model."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def qualified_model(value: Any, *, allow_legacy: bool = False) -> str:
    """Match Bello's first-slash identity; never infer an API billing route."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("expected a non-empty provider/model id")
    if any(ord(char) < 33 for char in value):
        raise ValueError("model id cannot contain whitespace or control characters")
    if "/" not in value:
        if allow_legacy and value.startswith("gpt-"):
            return f"openai-codex/{value}"
        raise ValueError("use an explicit provider/model id")
    provider, model = value.split("/", 1)
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", provider) or not model or model.startswith("/"):
        raise ValueError("invalid provider/model id")
    return value


# Account data and arbitrary worker error bodies are not model metadata.
CATALOG_FIELDS = {
    "name", "displayName", "description", "resolvedModel", "api", "reasoning",
    "inputModalities", "defaultEffort", "supportedEfforts", "supportedServiceTiers",
    "supportsServiceTier", "effortCapabilitySources", "effortRoutes", "available",
    "configured", "billingRoute", "contextWindow", "maxTokens", "cost", "pricing",
}


def summarize_catalog(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("model catalog must be an object")
    rows = payload.get("data", payload.get("models"))
    if not isinstance(rows, list):
        raise ValueError("model catalog must contain a data or models array")
    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("invalid model catalog entry")
        identity = qualified_model(item.get("qualifiedId"))
        provider, model = identity.split("/", 1)
        if item.get("provider", provider) != provider or item.get("model", model) != model:
            raise ValueError(f"inconsistent provider/model identity for {identity}")
        entry = {key: item[key] for key in CATALOG_FIELDS if key in item}
        entry.update(qualifiedId=identity, provider=provider, model=model)
        efforts = entry.get("supportedEfforts")
        if not isinstance(efforts, list) or any(not isinstance(value, str) or not value for value in efforts):
            raise ValueError(f"{identity} has no valid supportedEfforts array")
        entry["supportedEfforts"] = list(dict.fromkeys(efforts))
        tiers = entry.get("supportedServiceTiers")
        if "supportedServiceTiers" in entry and (not isinstance(tiers, list) or any(not isinstance(value, str) for value in tiers)):
            raise ValueError(f"{identity} has an invalid supportedServiceTiers array")
        for field in ("available", "configured", "supportsServiceTier"):
            if field in entry and not isinstance(entry[field], bool):
                raise ValueError(f"{identity} has an invalid {field} flag")
        if identity in result and result[identity] != entry:
            raise ValueError(f"conflicting duplicate model entry: {identity}")
        result[identity] = entry
    return list(result.values())


def load_catalog(*, timeout_seconds: float, file: Path | None = None) -> dict[str, Any]:
    if file is not None:
        payload = json.loads(file.read_text(encoding="utf-8"))
        source = "supplied catalog file"
    else:
        try:
            completed = subprocess.run(
                ["bello", "runtime", "models", "--engine", "all"],
                check=True, capture_output=True, text=True, timeout=timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"bello runtime models failed (exit {exc.returncode})") from exc
        payload = json.loads(completed.stdout)
        source = "bello runtime models --engine all"
    models = summarize_catalog(payload)
    unavailable = payload.get("unavailableEngines", {})
    names = unavailable.keys() if isinstance(unavailable, dict) else unavailable if isinstance(unavailable, list) else []
    return {
        "source": source,
        "models": models,
        "unavailableEngines": [name for name in names if name in {"pi", "claude-code"}],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--file", type=Path, help="Normalize a saved Bello runtime catalog instead of querying it")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0:
        print("invalid: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        result = load_catalog(timeout_seconds=args.timeout, file=args.file)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"model catalog unavailable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
