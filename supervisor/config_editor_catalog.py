"""Live model discovery, capability lookup and offline catalog views.

This is the only implementation that writes the legacy discovery/effort maps
in config_editor. Each refresh clears stale state, carries its sanitized catalog
on ModelChoices, and discards the raw discovery response in finally. No disk
cache or saved model can stand in for an authenticated engine catalog.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor.config_validation import ModelCatalog
from supervisor.project_config import ProjectConfig


class ModelChoices(tuple):
    """Discovered model ids, plus the sanitized catalog they came from.

    It behaves as the plain tuple of ids other code expects; `catalog` adds
    names, alias resolution, capabilities and engine failures for display and
    offline validation. Plain tuples (previews and tests) carry no catalog.
    """

    catalog: ModelCatalog | None

    def __new__(cls, models: Any = (), catalog: ModelCatalog | None = None) -> "ModelChoices":
        instance = super().__new__(cls, models)
        instance.catalog = catalog
        return instance


def intelligence_choices_for_model(model: str) -> tuple[str, ...]:
    from supervisor import config_editor as _editor

    advertised = _editor._model_effort_catalog.get(model)
    if advertised is not None:
        # A model with no advertised effort is used without sending one.
        return advertised or (_editor.NO_EFFORT,)
    try:
        engine = _editor.parse_model_selection(model).engine if "/" in model else None
    except _editor.ModelSelectionError:
        engine = None
    if engine == "claude-code":
        # Unknown catalog: offer the Claude Code engine's own effort contract
        # rather than Bello's generic list, which it would reject.
        return _editor.CLAUDE_CODE_EFFORTS
    # Unknown is not "advertises none": do not offer the no-effort choice here.
    return tuple(value for value in _editor._fallback_intelligence_choices(model) if value != _editor.NO_EFFORT)


def _editor_catalog(model_choices: tuple[str, ...] | None, models: tuple[str, ...]) -> ModelCatalog:
    from supervisor import config_editor as _editor

    discovered = getattr(model_choices, "catalog", None)
    if discovered is not None:
        return discovered
    if model_choices is None:
        # An offline preview has no discovery, so nothing is verified.
        return _editor.ModelCatalog(discovered=False)
    entries: dict[str, _editor.CatalogModel] = {}
    for model in models:
        try:
            selection = _editor.parse_model_selection(model)
        except _editor.ModelSelectionError:
            continue
        entries.setdefault(selection.qualified, _editor.CatalogModel(
            selection.qualified, selection.engine, _editor._model_effort_catalog.get(model)))
    return _editor.ModelCatalog(models=entries, discovered=True)


def _advertised_default(model: str, catalog: ModelCatalog | None) -> str | None:
    entry = catalog.get(model) if catalog is not None else None
    return entry.default_effort if entry is not None else None


def available_model_choices(project_root: Path) -> ModelChoices:
    from supervisor import config_editor as _editor

    _editor._model_effort_catalog.clear()
    _editor._discovery.clear()
    try:
        models = _editor._available_models_from_app_server(project_root)
        # A disk cache or an old saved choice does not establish that its provider
        # is still connected. Only offer the current execution engines' catalog.
        choices = _editor._normalize_model_choices(models)
        response, error = _editor._discovery.get("response"), _editor._discovery.get("error")
        # Without a captured response (a substituted discovery function) the
        # ids are all that is known; they are then treated like a plain tuple.
        catalog = _editor.catalog_from_model_list(response, error=error) if response is not None or error else None
    finally:
        _editor._discovery.clear()
    return _editor.ModelChoices(choices, catalog)


def _model_choices_for_config(config: ProjectConfig, model_choices: tuple[str, ...] | None) -> tuple[str, ...]:
    from supervisor import config_editor as _editor

    if model_choices is not None:
        return _editor._normalize_model_choices(model_choices)
    # Offline previews may omit discovery; the interactive editor always passes
    # an explicit tuple, including an empty one when no provider is connected.
    multi_agent_settings = tuple(getattr(config, field) for field in _editor.MULTI_AGENT_CONFIG_FIELDS)
    return _editor._normalize_model_choices(
        [
            *(model_choices if model_choices is not None else _editor.SUPPORTED_MODEL_CHOICES),
            config.coder_mod,
            config.revision_coder_mod,
            config.runtime_mod,
            config.completion_mod,
            config.adversary_mod,
            *(settings.default.model for settings in multi_agent_settings),
            *(model for settings in multi_agent_settings for model in settings.allowed),
        ]
    )


def _normalize_model_choices(models: Any) -> tuple[str, ...]:
    from supervisor import config_editor as _editor

    if isinstance(models, str):
        candidates = [models]
    else:
        candidates = list(models) if isinstance(models, list | tuple | set) else []
    from supervisor.runtime.models import ModelSelectionError, parse_model_selection
    available: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate not in _editor.SUPPORTED_MODEL_CHOICES and "/" not in candidate:
            # Legacy unqualified names remain the existing Codex aliases. New
            # models carry an explicit provider so choosing one cannot change
            # the user's billing route accidentally.
            continue
        try:
            parse_model_selection(candidate)
        except ModelSelectionError:
            continue
        available.add(candidate)
    return (*tuple(model for model in _editor.SUPPORTED_MODEL_CHOICES if model in available),
            *sorted(available - set(_editor.SUPPORTED_MODEL_CHOICES)))


def _available_models_from_cache() -> tuple[str, ...]:
    from supervisor import config_editor as _editor

    path = _editor.Path.home() / ".codex" / "models_cache.json"
    try:
        payload = _editor.json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _editor.json.JSONDecodeError):
        return ()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    choices: list[str] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        if model.get("visibility") == "hidden" or model.get("hidden") is True:
            continue
        slug = model.get("slug") or model.get("id") or model.get("model")
        if isinstance(slug, str):
            choices.append(slug)
    return tuple(choices)


def _available_models_from_app_server(project_root: Path) -> tuple[str, ...]:
    from supervisor import config_editor as _editor

    async def read_models() -> tuple[str, ...]:
        client = _editor.RuntimeClient(cwd=project_root)
        await client.start()
        try:
            await client.initialize()
            response = await client.request("model/list", {
                "engines": ["codex", "pi", "claude-code"], "optionalEngines": True,
            })
            _editor._discovery["response"] = response if isinstance(response, dict) else {}
            for descriptor in response.get("data", []):
                if not isinstance(descriptor, dict):
                    continue
                efforts = descriptor.get("supportedEfforts")
                # An empty list is recorded too: "advertises no effort" is not
                # the same as a model whose capabilities are unknown.
                if isinstance(efforts, list) and all(isinstance(item, str) for item in efforts):
                    for model in _editor._extract_model_ids(descriptor):
                        _editor._model_effort_catalog[model] = tuple(dict.fromkeys(efforts))
            return tuple(_editor._extract_model_ids(response))
        finally:
            await client.stop()

    try:
        return _editor.asyncio.run(read_models())
    except Exception as exc:
        # Keep only the error class: the text may carry provider payloads.
        _editor._discovery["error"] = exc.__class__.__name__
        return ()


def _extract_model_ids(value: Any) -> set[str]:
    from supervisor import config_editor as _editor

    ids: set[str] = set()
    if isinstance(value, dict):
        if (value.get("hidden") is True or value.get("visibility") == "hidden"
                or value.get("configured") is False or value.get("available") is False):
            return ids
        qualified = value.get("qualifiedId")
        keys = ("qualifiedId",) if isinstance(qualified, str) and qualified else ("id", "model", "slug")
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())
        if value.get("provider") == "openai-codex" and isinstance(qualified, str):
            # Existing saved Codex profiles remain aliases for this route only,
            # never for an API model with the same provider-local name.
            ids.add(qualified.removeprefix("openai-codex/"))
        for key in ("data", "models", "items"):
            nested = value.get(key)
            if nested is not None:
                ids.update(_editor._extract_model_ids(nested))
    elif isinstance(value, list | tuple):
        for item in value:
            ids.update(_editor._extract_model_ids(item))
    return ids
