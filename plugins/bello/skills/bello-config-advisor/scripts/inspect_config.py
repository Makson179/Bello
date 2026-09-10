#!/usr/bin/env python3
"""Read and normalize Bello project settings without mutating workspace state."""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_models import qualified_model

TARGET_VERSION = "0.6.0"
ACTIVE_STATUSES = {"starting", "running", "paused", "restarting"}
TERMINAL_STATUSES = {"complete", "escalated", "stuck", "provider_failure", "exited"}
LEGACY_MODELS = {"gpt-6-astra", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.5"}
ALL_EFFORTS = {"off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}

DEFAULT_MULTI_AGENT = {
    "enabled": False,
    "max_concurrent": 4,
    "default": {"model": "gpt-5.6-luna", "intelligence": "high"},
    "allowed": {
        "gpt-5.6-luna": ["medium", "high", "xhigh"],
        "gpt-5.6-terra": ["medium", "high"],
    },
}


def _first(payload: dict[str, Any], keys: tuple[str, ...], default: Any, *, skip_none: bool = False) -> Any:
    for key in keys:
        if key in payload and (not skip_none or payload[key] is not None):
            return payload[key]
    return default


def _normalized_review_limits(payload: dict[str, Any]) -> tuple[Any, Any]:
    legacy = "review_limit_format" not in payload
    before = _first(
        payload,
        ("max_completion_returns_before_adversary", "max_completion_returns_per_generation"),
        4 if legacy else 1,
    )
    after = payload.get("max_completion_returns_after_adversary", 2 if legacy else 0)
    if isinstance(before, str) and before.strip().lower() == "unlimited":
        before = "unlimited"
    if isinstance(after, str) and after.strip().lower() == "unlimited":
        after = "unlimited"
    if legacy:
        if before == 0:
            before = "unlimited"
        if after == 0:
            after = "unlimited"
    return before, after


def _optional_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _stripped(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _normalized_choice(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _normalize_multi_agent(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return copy.deepcopy(DEFAULT_MULTI_AGENT)
    raw_default = value.get("default", DEFAULT_MULTI_AGENT["default"])
    if not isinstance(raw_default, dict):
        raw_default = DEFAULT_MULTI_AGENT["default"]
    raw_allowed = value.get("allowed", DEFAULT_MULTI_AGENT["allowed"])
    if not isinstance(raw_allowed, dict):
        raw_allowed = DEFAULT_MULTI_AGENT["allowed"]
    allowed: dict[str, list[Any]] = {}
    for raw_model, raw_efforts in raw_allowed.items():
        model = _stripped(raw_model)
        if not isinstance(raw_efforts, list):
            allowed[model] = raw_efforts
            continue
        efforts: list[Any] = []
        for raw_effort in raw_efforts:
            effort = _normalized_choice(raw_effort)
            if effort not in efforts:
                efforts.append(effort)
        allowed[model] = efforts
    return {
        "enabled": value.get("enabled", False),
        "max_concurrent": value.get("max_concurrent", 4),
        "default": {
            "model": _stripped(raw_default.get("model", "gpt-5.6-luna")),
            "intelligence": _normalized_choice(raw_default.get("intelligence", "high")),
        },
        "allowed": allowed,
    }


def _normalize(payload: dict[str, Any], *, config_exists: bool) -> dict[str, Any]:
    legacy_super_model = _first(payload, ("super_mod", "supervisor_model", "model"), "gpt-5.6-sol", skip_none=True)
    legacy_super_effort = _first(
        payload,
        ("super_intelligence", "supervisor_intelligence"),
        "xhigh",
        skip_none=True,
    )
    coder_model = _stripped(_first(payload, ("coder_mod", "coder_model", "model"), "gpt-5.6-sol", skip_none=True))
    coder_effort = _normalized_choice(
        _first(payload, ("coder_intelligence",), "xhigh", skip_none=True)
    )
    revision_coder_model = _stripped(
        _first(payload, ("revision_coder_mod",), coder_model, skip_none=True)
    )
    revision_coder_effort = _normalized_choice(
        _first(payload, ("revision_coder_intelligence",), coder_effort, skip_none=True)
    )
    runtime_model = _stripped(_first(payload, ("runtime_mod", "runtime_model"), legacy_super_model, skip_none=True))
    completion_model = _stripped(
        _first(payload, ("completion_mod", "completion_model"), legacy_super_model, skip_none=True)
    )
    adversary_model = _stripped(
        _first(payload, ("adversary_mod", "adversary_model"), "gpt-5.6-sol", skip_none=True)
    )

    if payload.get("speed") is not None:
        speed = _normalized_choice(payload["speed"])
    elif isinstance(payload.get("fast"), bool):
        speed = "fast" if payload["fast"] else "usual"
    else:
        speed = "usual"

    raw_runs = payload.get("max_adversary_runs", 1)
    if raw_runs == 0:
        adversary = False
    elif isinstance(payload.get("adversary"), bool):
        adversary = payload["adversary"]
    elif "max_adversary_runs" in payload:
        adversary = isinstance(raw_runs, int) and not isinstance(raw_runs, bool) and raw_runs > 0
    else:
        adversary = False

    before, after = _normalized_review_limits(payload) if config_exists else (1, 0)
    multi_agent = _normalize_multi_agent(payload.get("multi_agent", DEFAULT_MULTI_AGENT))
    completion_multi_agent = _normalize_multi_agent(
        payload.get("completion_multi_agent", DEFAULT_MULTI_AGENT)
    )
    adversary_multi_agent = _normalize_multi_agent(
        payload.get("adversary_multi_agent", DEFAULT_MULTI_AGENT)
    )

    protected = _first(payload, ("protected_path", "protected_paths"), [], skip_none=True)
    if not isinstance(protected, list):
        protected = []
    else:
        protected = [item.strip() for item in protected if isinstance(item, str) and item.strip()]

    return {
        "review_limit_format": "explicit",
        "task": _optional_nonempty_string(_first(payload, ("task", "task_path"), None, skip_none=True)),
        "coder_mod": coder_model,
        "revision_coder_enabled": payload.get("revision_coder_enabled", False),
        "revision_coder_mod": revision_coder_model,
        "runtime_mod": runtime_model,
        "completion_mod": completion_model,
        "adversary_mod": adversary_model,
        "coder_intelligence": coder_effort,
        "revision_coder_intelligence": revision_coder_effort,
        "runtime_intelligence": _normalized_choice(
            _first(payload, ("runtime_intelligence",), legacy_super_effort, skip_none=True)
        ),
        "completion_intelligence": _normalized_choice(
            _first(payload, ("completion_intelligence",), legacy_super_effort, skip_none=True)
        ),
        "adversary_intelligence": _normalized_choice(
            _first(payload, ("adversary_intelligence",), "xhigh", skip_none=True)
        ),
        "speed": speed,
        "cheap_runtime": _first(payload, ("cheap_runtime", "cheap_runtime_enabled"), True),
        "start_over": payload.get("start_over", False),
        "completion_review": _first(
            payload,
            ("completion_review", "completion_review_enabled"),
            False,
        ),
        "adversary": adversary,
        "max_adversary_runs": raw_runs,
        "max_completion_returns_before_adversary": before,
        "max_completion_returns_after_adversary": after,
        "clean": payload.get("clean", False),
        "protected_path": protected,
        "multi_agent": multi_agent,
        "completion_multi_agent": completion_multi_agent,
        "adversary_multi_agent": adversary_multi_agent,
    }


def _is_int(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _valid_review_limit(value: Any) -> bool:
    return _is_int(value) or value == "unlimited"


def _valid_effort(model: Any, effort: Any, *, subagent: bool = False) -> bool:
    if not isinstance(effort, str) or effort not in ALL_EFFORTS:
        return False
    try:
        qualified_model(model, allow_legacy=True)
    except ValueError:
        return False
    if "/" in model:
        return True  # Exact capabilities are checked later against the current catalog.
    if model not in LEGACY_MODELS or effort in {"off", "minimal"}:
        return False
    if model == "gpt-5.5" and effort in {"max", "ultra"}:
        return False
    return model != "gpt-5.6-luna" or effort != "ultra"


def _validate_multi_agent(value: Any, *, field_name: str = "multi_agent") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field_name} must be an object"]
    errors: list[str] = []
    if not isinstance(value.get("enabled", False), bool):
        errors.append(f"{field_name}.enabled must be boolean")
    if not _is_int(value.get("max_concurrent", 4), minimum=1):
        errors.append(f"{field_name}.max_concurrent must be a positive integer")
    default = value.get("default", DEFAULT_MULTI_AGENT["default"])
    allowed = value.get("allowed", DEFAULT_MULTI_AGENT["allowed"])
    if not isinstance(default, dict):
        errors.append(f"{field_name}.default must be an object")
        default = {}
    if not isinstance(allowed, dict) or not allowed:
        errors.append(f"{field_name}.allowed must be a non-empty object")
        allowed = {}
    normalized: dict[str, set[str]] = {}
    for raw_model, efforts in allowed.items():
        model = _stripped(raw_model)
        if not isinstance(efforts, list) or not efforts:
            errors.append(f"{field_name}.allowed.{model} must be a non-empty list")
            continue
        normalized_efforts = [_normalized_choice(effort) for effort in efforts]
        if any(not _valid_effort(model, effort, subagent=True) for effort in normalized_efforts):
            errors.append(f"{field_name}.allowed.{model} contains an invalid effort")
            continue
        normalized[model] = set(normalized_efforts)
    default_model = _stripped(default.get("model"))
    default_effort = _normalized_choice(default.get("intelligence"))
    if not isinstance(default_model, str) or not isinstance(default_effort, str):
        errors.append(f"{field_name}.default model and intelligence must be strings")
    elif default_effort not in normalized.get(default_model, set()):
        errors.append(
            f"{field_name}.default must be included in {field_name}.allowed"
        )
    return errors


def _source_config_errors(payload: dict[str, Any], current: dict[str, Any], *, config_exists: bool) -> list[str]:
    if not config_exists:
        return []
    errors: list[str] = []
    if "review_limit_format" in payload and payload["review_limit_format"] != "explicit":
        errors.append("review_limit_format must be 'explicit'")
    raw_task = _first(payload, ("task", "task_path"), None, skip_none=True)
    if raw_task is not None and not isinstance(raw_task, str):
        errors.append("task must be a string or null")
    for role in ("coder", "revision_coder", "runtime", "completion", "adversary"):
        model = current[f"{role}_mod"]
        effort = current[f"{role}_intelligence"]
        if not isinstance(model, str) or not model.strip():
            errors.append(f"{role}_mod must be a non-empty string")
        elif not _valid_effort(model, effort):
            errors.append(f"{role}_intelligence is invalid for {model}")
    if not isinstance(current["speed"], str) or current["speed"] not in {"usual", "fast"}:
        errors.append("speed must be 'usual' or 'fast'")
    if payload.get("speed") is None and "fast" in payload and not isinstance(payload["fast"], bool):
        errors.append("fast must be boolean")
    for field in (
        "revision_coder_enabled",
        "cheap_runtime",
        "start_over",
        "completion_review",
        "adversary",
        "clean",
    ):
        if not isinstance(current[field], bool):
            errors.append(f"{field} must be boolean")
    if not _is_int(current["max_adversary_runs"]):
        errors.append("max_adversary_runs must be a non-negative integer")
    if (
        current["max_adversary_runs"] != 0
        and "adversary" in payload
        and not isinstance(payload["adversary"], bool)
    ):
        errors.append("adversary must be boolean")
    for field in (
        "max_completion_returns_before_adversary",
        "max_completion_returns_after_adversary",
    ):
        if not _valid_review_limit(current[field]):
            errors.append(f"{field} must be a non-negative integer or 'unlimited'")
    raw_protected = _first(payload, ("protected_path", "protected_paths"), [], skip_none=True)
    if not isinstance(raw_protected, list) or any(not isinstance(item, str) for item in raw_protected):
        errors.append("protected_path must be a list of strings")
    for field in (
        "multi_agent",
        "completion_multi_agent",
        "adversary_multi_agent",
    ):
        if field in payload:
            errors.extend(_validate_multi_agent(payload[field], field_name=field))
    return errors


def _release_version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev\d+)?", value)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _read_bello_version(timeout_seconds: float) -> tuple[str | None, list[str]]:
    warnings: list[str] = []
    executable = shutil.which("bello")
    if executable is None:
        return None, ["Bello executable was not found on PATH; version compatibility is unverified."]
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, [f"Could not query Bello version: {exc}; compatibility is unverified."]

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    match = re.search(r"\bBello\s+([^\s]+)", output, flags=re.IGNORECASE)
    if match is None:
        return None, ["Bello version output was not recognized; compatibility is unverified."]
    version = match.group(1)
    installed = _release_version_tuple(version)
    target = _release_version_tuple(TARGET_VERSION)
    if installed is None or target is None:
        warnings.append(f"Could not compare installed Bello {version!r} with schema target {TARGET_VERSION}.")
    elif installed < target:
        warnings.append(
            f"Installed Bello {version} predates schema target {TARGET_VERSION}; recommendation may require an update."
        )
    elif installed > target:
        warnings.append(
            f"Installed Bello {version} is newer than the exact schema target {TARGET_VERSION}; compatibility is unverified."
        )
    return version, warnings


def _version_compatibility(version: str | None) -> str:
    installed = _release_version_tuple(version) if version is not None else None
    target = _release_version_tuple(TARGET_VERSION)
    if installed is None or target is None:
        return "unverified"
    if installed < target:
        return "update_required"
    if installed == target:
        return "verified"
    return "unverified"


def _pid_liveness(pid: int) -> str:
    if sys.platform == "win32":
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return "dead" if error == 87 else "unknown"
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return "unknown"
            return "alive" if exit_code.value == still_active else "dead"
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def _read_process_command(pid: int) -> str | None:
    if sys.platform == "win32":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell:
            script = (
                f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\"; "
                "if ($null -ne $p) {[Console]::Out.Write($p.CommandLine)}"
            )
            command = [powershell, "-NoProfile", "-NonInteractive", "-Command", script]
        else:
            wmic = shutil.which("wmic")
            if not wmic:
                return None
            command = [wmic, "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"]
    else:
        ps = shutil.which("ps")
        if not ps:
            return None
        command = [ps, "-p", str(pid), "-o", "command="]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip()
    if sys.platform == "win32" and output.startswith("CommandLine="):
        output = output.removeprefix("CommandLine=").strip()
    return output or None


def _read_process_cwd(pid: int) -> Path | None:
    if sys.platform == "win32":
        return None
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve(strict=True)
    except OSError:
        pass
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        completed = subprocess.run(
            [lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            try:
                return Path(line[1:]).resolve(strict=True)
            except OSError:
                return None
    return None


def _pid_identity(pid: int, root: Path) -> str:
    command = _read_process_command(pid)
    if command is None:
        return "unknown"
    if (
        re.search(
            r"(?:^|[\\/\s\"'])bello(?:\.exe|\.cmd|\.bat)?(?=$|[\s\"'])",
            command,
            flags=re.IGNORECASE,
        )
        is None
    ):
        return "mismatch"
    cwd = _read_process_cwd(pid)
    if cwd is None:
        return "probable"
    return "confirmed" if cwd == root else "mismatch"


def _process_observation(root: Path) -> dict[str, Any]:
    path = root / ".codex" / "bello-run" / "pid"
    if not path.exists():
        return {"pid_file": str(path), "pid": None, "liveness": "missing", "identity": "not_checked"}
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
        if pid <= 0:
            raise ValueError
    except (OSError, ValueError):
        return {"pid_file": str(path), "pid": None, "liveness": "invalid", "identity": "not_checked"}
    liveness = _pid_liveness(pid)
    identity = _pid_identity(pid, root) if liveness == "alive" else "not_checked"
    return {"pid_file": str(path), "pid": pid, "liveness": liveness, "identity": identity}


def _apply_guard(
    *,
    config_exists: bool,
    status: str | None,
    config_path: Path,
    liveness: str,
    identity: str,
) -> tuple[str, str]:
    if liveness == "alive" and identity == "confirmed":
        return "blocked", "A live Bello launcher was confirmed for this workspace."
    if liveness == "alive" and identity in {"probable", "unknown"}:
        return "uncertain", "The saved PID is alive, but its Bello workspace identity was not confirmed."
    if liveness == "alive" and identity == "mismatch" and status in ACTIVE_STATUSES:
        return "uncertain", "Saved status appears active, but the PID belongs to another process."
    if status in ACTIVE_STATUSES:
        return "uncertain", "Saved status appears active but no live launcher PID was confirmed."
    if status in TERMINAL_STATUSES:
        try:
            age = time.time() - config_path.stat().st_mtime
        except OSError:
            age = 0
        if age < 5:
            return "uncertain", "Terminal status is too recent to prove process shutdown."
    if not config_exists or status is None or status in TERMINAL_STATUSES or liveness in {"dead", "alive"}:
        return "clear", "No active Bello process was detected."
    return "uncertain", "Run state could not be classified safely."


def inspect(workspace: Path, *, include_bello_version: bool, timeout_seconds: float) -> dict[str, Any]:
    root = workspace.resolve()
    path = root / ".supervisor" / "config.json"
    warnings: list[str] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid {path}: expected a JSON object")
    else:
        payload = {}

    status = payload.get("status") if isinstance(payload.get("status"), str) else None
    process = _process_observation(root)
    apply_guard, apply_guard_reason = _apply_guard(
        config_exists=path.exists(),
        status=status,
        config_path=path,
        liveness=process["liveness"],
        identity=process["identity"],
    )
    if apply_guard != "clear":
        warnings.append(f"Configuration apply guard is {apply_guard}: {apply_guard_reason}")
    if payload.get("clean") is True:
        warnings.append("Existing clean=true requires explicit reconfirmation before it is carried into a recommendation.")

    version = None
    if include_bello_version:
        version, version_warnings = _read_bello_version(timeout_seconds)
        warnings.extend(version_warnings)

    current = _normalize(payload, config_exists=path.exists())
    source_errors = _source_config_errors(payload, current, config_exists=path.exists())
    warnings.extend(f"Existing config is invalid: {error}" for error in source_errors)

    return {
        "schema_target": TARGET_VERSION,
        "workspace": str(root),
        "config_path": str(path),
        "config_exists": path.exists(),
        "runtime_status": status,
        "status_indicates_active": status in ACTIVE_STATUSES,
        "process_observation": process,
        "apply_guard": apply_guard,
        "apply_guard_reason": apply_guard_reason,
        "installed_bello_version": version,
        "version_compatibility": _version_compatibility(version),
        "source_config_valid": not source_errors,
        "source_config_errors": source_errors,
        "model_capabilities_checked": False,
        "current_project_config": current,
        "warnings": warnings,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Target project directory")
    parser.add_argument("--include-bello-version", action="store_true", help="Query read-only `bello --version`")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="Version-query timeout")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.timeout_seconds <= 0:
        print("invalid: --timeout-seconds must be positive", file=sys.stderr)
        return 2
    try:
        result = inspect(
            args.workspace,
            include_bello_version=args.include_bello_version,
            timeout_seconds=args.timeout_seconds,
        )
    except RuntimeError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
