"""Controller preflight; compatibility exports live in controller."""
from __future__ import annotations

from . import compat


def _schema_file_exists(out_dir: compat.Path, name: str) -> bool:
    return (out_dir / name).exists() or (out_dir / "v2" / name).exists()


def _turn_start_schema_supports_effort(out_dir: compat.Path) -> bool:
    for path in (out_dir / "TurnStartParams.json", out_dir / "v2" / "TurnStartParams.json"):
        if not path.exists():
            continue
        try:
            payload = compat.json.loads(path.read_text(encoding="utf-8"))
        except (OSError, compat.json.JSONDecodeError):
            continue
        properties = payload.get("properties")
        if isinstance(properties, dict) and "effort" in properties:
            return True
    return False


def _run_probe(args: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    try:
        completed = compat.subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (FileNotFoundError, compat.subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return completed.returncode == 0, (completed.stdout + completed.stderr).strip()


def _controller_executable(
    name: str,
    cwd: compat.Path,
    *,
    environ: dict[str, str] | None = None,
) -> str | None:
    if not compat.is_windows_platform():
        return compat.shutil.which(name, path=(environ or compat.os.environ).get("PATH")) or name
    try:
        return compat.require_trusted_executable(
            name,
            cwd=cwd,
            environ=environ,
            windows=True,
        )
    except compat.ExecutableResolutionError:
        return None


def _resolve_controller_models(
    *,
    model: str | None,
    coder_model: str | None,
    supervisor_model: str | None,
    runtime_model: str | None,
    completion_model: str | None,
    adversary_model: str | None,
) -> tuple[str, str, str, str]:
    if model and (coder_model or supervisor_model or runtime_model or completion_model):
        raise RuntimeError(
            "model cannot be combined with coder_model, supervisor_model, runtime_model, or completion_model"
        )
    if supervisor_model and (runtime_model or completion_model):
        raise RuntimeError("supervisor_model cannot be combined with runtime_model or completion_model")
    if model:
        return model, model, model, adversary_model or compat.DEFAULT_MODEL
    legacy_supervisor_model = supervisor_model or compat.DEFAULT_MODEL
    return (
        coder_model or compat.DEFAULT_MODEL,
        runtime_model or legacy_supervisor_model,
        completion_model or legacy_supervisor_model,
        adversary_model or compat.DEFAULT_MODEL,
    )


def _shared_primary_model(config: compat.ProjectConfig) -> str | None:
    models = {config.coder_mod, config.runtime_mod, config.completion_mod}
    return config.coder_mod if len(models) == 1 else None


def _selected_model_availability(
    models_response: dict[str, compat.Any],
    *,
    coder_model: str | None,
    runtime_model: str | None,
    completion_model: str | None,
    adversary_model: str | None = None,
    revision_coder_model: str | None = None,
    subagent_models: tuple[str, ...] = (),
) -> compat.ModelAvailabilityResult:
    available_models = tuple(sorted(compat._extract_model_ids(models_response)))
    available = set(available_models)
    missing: list[str] = []
    if coder_model and coder_model not in available:
        missing.append(f"coder={coder_model}")
    if revision_coder_model and revision_coder_model not in available:
        missing.append(f"revision-coder={revision_coder_model}")
    if runtime_model and runtime_model not in available:
        missing.append(f"runtime={runtime_model}")
    if completion_model and completion_model not in available:
        missing.append(f"completion={completion_model}")
    if adversary_model and adversary_model not in available:
        missing.append(f"adversary={adversary_model}")
    for subagent_model in subagent_models:
        if subagent_model not in available:
            missing.append(f"subagent={subagent_model}")
    return compat.ModelAvailabilityResult(missing_roles=tuple(missing), available_models=available_models)


def _readable_available_models(models_response: compat.Any) -> tuple[str, ...]:
    """Selectable provider/model ids only, never display names or bare aliases.

    A floating Claude Code alias also shows what it currently resolves to.
    Legacy catalogs without qualified ids keep the previous extracted list.
    """
    data = models_response.get("data") if isinstance(models_response, dict) else None
    labels: dict[str, str] = {}
    for item in data if isinstance(data, list) else ():
        if (not isinstance(item, dict) or item.get("hidden") is True or item.get("visibility") == "hidden"
                or item.get("configured") is False or item.get("available") is False):
            continue
        qualified = item.get("qualifiedId")
        if not isinstance(qualified, str) or not qualified:
            continue
        resolved = item.get("resolvedModel")
        if item.get("alias") is True and isinstance(resolved, str) and resolved:
            labels[qualified] = f"{qualified} (alias, now {resolved})"
        else:
            labels.setdefault(qualified, qualified)
    if not labels:
        return tuple(sorted(compat._extract_model_ids(models_response)))
    return tuple(labels[key] for key in sorted(labels))


def _extract_model_ids(value: compat.Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, dict):
        for key in ("id", "model", "slug", "name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())
        for key in ("data", "models", "items"):
            if key in value:
                ids.update(compat._extract_model_ids(value[key]))
        return ids
    if isinstance(value, list):
        for item in value:
            ids.update(compat._extract_model_ids(item))
        return ids
    if isinstance(value, str) and value.strip():
        ids.add(value.strip())
    return ids


def _sandbox_is_read_only(value: compat.Any) -> bool:
    if value == "read-only":
        return True
    if isinstance(value, dict):
        return value.get("type") == "readOnly" and value.get("networkAccess") is False
    return False


def _sandbox_matches_mode(value: compat.Any, mode: str, *, workspace_root: compat.Path | None = None) -> bool:
    if mode == compat.CODER_SANDBOX_DANGER_FULL_ACCESS:
        if value == "danger-full-access":
            return True
        if isinstance(value, dict):
            return value.get("type") == "dangerFullAccess"
        return False
    if mode == compat.CODER_SANDBOX_WORKSPACE_WRITE:
        if not isinstance(value, dict) or value.get("type") != "workspaceWrite":
            return False
        if value.get("networkAccess") is not False:
            return False
        roots = value.get("writableRoots")
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            return False
        if workspace_root is None:
            return not roots
        expected = workspace_root.resolve()
        for raw in roots:
            try:
                if compat.Path(raw).expanduser().resolve(strict=False) != expected:
                    return False
            except OSError:
                return False
        return True
    return compat._sandbox_is_read_only(value)
