"""Configuration-to-row presentation and shared offline validation.

The functions return immutable row descriptions; they never persist config or
perform model discovery. Validation uses the catalog supplied by the session.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from supervisor.config_editor_types import EditorOption, EditorParameter
from supervisor.config_validation import ConfigReport, ModelCatalog
from supervisor.project_config import (
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_SOL,
    MODEL_GPT_5_6_TERRA,
    ProjectConfig,
)

MODEL_FAMILY_ASTRA_LABEL = "GPT-6 Astra"


MODEL_FAMILY_5_6_LABEL = "GPT-5.6"


MODEL_FAMILY_5_5_LABEL = "GPT-5.5"


MODEL_VARIANT_LABELS = {
    MODEL_GPT_5_6_SOL: "Sol",
    MODEL_GPT_5_6_TERRA: "Terra",
    MODEL_GPT_5_6_LUNA: "Luna",
}


ROLE_PURPOSES = {
    "coder": "coder that implements the task in the writable workspace snapshot",
    "revision-coder": "coder that applies completion-review and adversary feedback after the first return",
    "runtime": "runtime supervisor that evaluates live events and approval requests",
    "completion": "read-only completion reviewer that decides whether the work is done",
    "adversary": "adversarial tester that attacks the candidate in a disposable snapshot",
    "subagent-default": "default subagent selected when the coder does not request another allowed profile",
    "completion-subagent-default": (
        "default subagent selected when the completion reviewer does not request another allowed profile"
    ),
    "adversary-subagent-default": (
        "default subagent selected when the adversarial tester does not request another allowed profile"
    ),
}


MULTI_AGENT_CONFIG_FIELDS = (
    "multi_agent",
    "completion_multi_agent",
    "adversary_multi_agent",
)


def _multi_agent_editor_field_parts(field: str) -> tuple[str, str] | None:
    from supervisor import config_editor as _editor

    for config_field in _editor.MULTI_AGENT_CONFIG_FIELDS:
        prefix = f"{config_field}_"
        if field.startswith(prefix):
            return config_field, field.removeprefix(prefix)
    return None


def _is_multi_agent_allowed_field(field: str) -> bool:
    from supervisor import config_editor as _editor

    parts = _editor._multi_agent_editor_field_parts(field)
    return parts is not None and parts[1].startswith("allowed:")


def parameter_defs(config: ProjectConfig, model_choices: tuple[str, ...] | None = None) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    models = _editor._model_choices_for_config(config, model_choices)
    catalog = _editor._editor_catalog(model_choices, models)
    parameters = _editor._parameter_defs(config, models, catalog)
    return _editor._annotate_issues(config, parameters, _editor.validate_project_config(config, catalog))


def editor_report(config: ProjectConfig, model_choices: tuple[str, ...] | None = None) -> ConfigReport:
    """The same offline checks the rows show, for the STATUS panel."""
    from supervisor import config_editor as _editor

    models = _editor._model_choices_for_config(config, model_choices)
    return _editor.validate_project_config(config, _editor._editor_catalog(model_choices, models))


def _annotate_issues(
    config: ProjectConfig, parameters: tuple[EditorParameter, ...], report: ConfigReport,
) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    if not report.issues:
        return parameters
    annotated = []
    present = {parameter.key for parameter in parameters}
    for parameter in parameters:
        keys = [parameter.key]
        parts = _editor._multi_agent_editor_field_parts(parameter.key)
        if parts is not None and parts[1] in {"default_model", "default_intelligence"}:
            # The default subagent is one of the allowed profiles.
            keys.append(f"{parts[0]}_allowed:{getattr(config, parts[0]).default.model}")
        if parts is not None and parts[1] == "unavailable_profiles":
            # Unavailable allowed models have no row of their own.
            keys.extend(key for key in (f"{parts[0]}_allowed:{model}" for model in getattr(config, parts[0]).allowed)
                        if key not in present)
        issues = [issue for key in keys for issue in report.for_setting(key)]
        issues.sort(key=lambda issue: issue.level != "error")
        if not issues:
            annotated.append(parameter)
            continue
        annotated.append(_editor.replace(
            parameter,
            issue=" ".join(dict.fromkeys(issue.message for issue in issues)),
            issue_title=issues[0].title,
            issue_level=issues[0].level,
        ))
    return tuple(annotated)


def _parameter_defs(
    config: ProjectConfig, models: tuple[str, ...], catalog: ModelCatalog,
) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    coder_parameters = _editor._role_parameters(
        "coder",
        "coder_mod",
        "coder_intelligence",
        config.coder_mod,
        config.coder_intelligence,
        models,
        catalog,
    )
    revision_coder_toggle = _editor.EditorParameter(
        "revision_coder_enabled",
        "revision-coder",
        "on" if config.revision_coder_enabled else "off",
        (
            _editor.EditorOption("on", "revision_coder_enabled", True),
            _editor.EditorOption("off", "revision_coder_enabled", False),
        ),
        help_text=(
            "on starts one fresh coder thread with this profile when completion review or adversary first returns "
            "findings; later findings reuse that revision thread. off returns findings to the current coder thread."
        ),
    )
    revision_coder_parameters = (
        _editor._role_parameters(
            "revision-coder",
            "revision_coder_mod",
            "revision_coder_intelligence",
            config.revision_coder_mod,
            config.revision_coder_intelligence,
            models,
            catalog,
        )
        if config.revision_coder_enabled
        else ()
    )
    multi_agent_parameters = _editor._multi_agent_parameters(
        config,
        models,
        config_field="multi_agent",
        owner="coder",
        label_prefix="",
        catalog=catalog,
    )
    runtime_parameters = _editor._role_parameters(
        "runtime",
        "runtime_mod",
        "runtime_intelligence",
        config.runtime_mod,
        config.runtime_intelligence,
        models,
        catalog,
    ) if config.runtime_enabled else ()
    completion_parameters = (
        _editor._role_parameters(
            "completion",
            "completion_mod",
            "completion_intelligence",
            config.completion_mod,
            config.completion_intelligence,
            models,
            catalog,
        )
        if config.completion_review
        else ()
    )
    completion_multi_agent_parameters = (
        _editor._multi_agent_parameters(
            config,
            models,
            config_field="completion_multi_agent",
            owner="completion reviewer",
            label_prefix="completion-",
            catalog=catalog,
        )
        if config.completion_review
        else ()
    )
    adversary_enabled = config.adversary and config.adversary_runs > 0
    adversary_active = adversary_enabled
    adversary_parameters = (
        _editor._role_parameters(
            "adversary",
            "adversary_mod",
            "adversary_intelligence",
            config.adversary_mod,
            config.adversary_intelligence,
            models,
            catalog,
        )
        if adversary_active
        else ()
    )
    adversary_multi_agent_parameters = (
        _editor._multi_agent_parameters(
            config,
            models,
            config_field="adversary_multi_agent",
            owner="adversarial tester",
            label_prefix="adversary-",
            catalog=catalog,
        )
        if adversary_active
        else ()
    )
    protected_options = [
        _editor.EditorOption("clear all", "protected_path", ()),
        _editor.EditorOption("add path", action="add_protected_path"),
    ]
    if config.protected_path:
        protected_options.append(_editor.EditorOption(f"remove last ({config.protected_path[-1]})", "protected_path", config.protected_path[:-1]))

    review_parameters: list[_editor.EditorParameter] = [
        _editor.EditorParameter(
            "adversary",
            "adversary",
            _editor._format_bool(adversary_enabled),
            (_editor.EditorOption("true", "adversary", True), _editor.EditorOption("false", "adversary", False)),
            help_text=(
                "Run the adversarial tester before completion. It attacks the candidate in a disposable "
                "snapshot independently of completion review."
            ),
        )
    ]
    if config.completion_review:
        before_adversary_help = (
            "Maximum completion-review rounds that may return work before Bello forces the first adversary. "
            "An earlier accept starts the adversary immediately. 0 skips these rounds; Unlimited removes the cap."
            if adversary_enabled
            else (
                "Maximum completion-review rounds that may return work. After the coder applies the final return "
                "and reports readiness, Bello completes without another review. 0 skips review; Unlimited removes "
                "the cap."
            )
        )
        review_parameters.append(
            _editor.EditorParameter(
                "completion_returns_before_adversary",
                "max-reviews-before-adversary" if adversary_enabled else "max-reviews",
                _editor.format_review_limit(config.completion_returns_before_adversary),
                (),
                edit_kind="review_limit",
                help_text=before_adversary_help,
            )
        )
    if adversary_enabled:
        review_parameters.append(
            _editor.EditorParameter(
                "adversary_runs",
                "max-adversary-runs",
                str(config.adversary_runs),
                (),
                edit_kind="non_negative_int",
                help_text="Maximum adversary passes in one run. 0 disables adversary passes.",
            )
        )
        if config.completion_review:
            review_parameters.append(
                _editor.EditorParameter(
                    "completion_returns_after_adversary",
                    "max-reviews-after-adversary",
                    _editor.format_review_limit(config.completion_returns_after_adversary),
                    (),
                    edit_kind="review_limit",
                    help_text=(
                        "Maximum additional completion-review rounds after each adversary pass. 0 schedules none; "
                        "Unlimited removes the cap. A candidate adversary finding is still adjudicated once before "
                        "Bello accepts it or returns a real defect to the coder."
                    ),
                )
            )

    return (
        _editor.EditorParameter(
            "task",
            "task",
            config.task or "absent",
            (),
            edit_kind="optional_text",
            help_text="Default task file for this folder. A --task CLI argument overrides it for one run.",
        ),
        *coder_parameters,
        revision_coder_toggle,
        *revision_coder_parameters,
        *multi_agent_parameters,
        _editor.EditorParameter(
            "async_tools",
            "async-tools",
            _editor._format_bool(config.async_tools),
            (_editor.EditorOption("true", "async_tools", True), _editor.EditorOption("false", "async_tools", False)),
            help_text=("Deliver completed tool results automatically and run independent tool calls concurrently. "
                       "Applies to coder, reviewers, and their subagents. Off preserves the existing execution loop. "
                       "Choose before starting a run; independent of runtime supervision and log distillation."),
        ),
        _editor.EditorParameter(
            "windows_native_root_read",
            "Windows native root read",
            _editor._format_bool(config.windows_native_root_read),
            (_editor.EditorOption("true", "windows_native_root_read", True),
             _editor.EditorOption("false", "windows_native_root_read", False)),
            help_text=("Explicit opt-in for native Codex on Windows to read the workspace's drive/share root. "
                       "Writes stay scoped to the assigned workspace; known private controller and Codex homes remain denied. "
                       "Off by default. Choose before starting a run; ignored on other platforms and engines."),
        ),
        _editor.EditorParameter(
            "runtime_enabled",
            "runtime",
            _editor._format_bool(config.runtime_enabled),
            (_editor.EditorOption("true", "runtime_enabled", True), _editor.EditorOption("false", "runtime_enabled", False)),
            help_text=("Run runtime supervision and its optional cheap triage. Off reduces safety: network is enabled "
                       "inside the filesystem sandbox, with no runtime review or outside-sandbox escalation."),
        ),
        *runtime_parameters,
        *completion_parameters,
        *completion_multi_agent_parameters,
        *adversary_parameters,
        *adversary_multi_agent_parameters,
        _editor.EditorParameter(
            "speed",
            "speed",
            config.speed,
            tuple(_editor.EditorOption(_editor._speed_option_label(value, config, catalog), "speed", value)
                  for value in _editor.SPEED_CHOICES),
            help_text=_editor.FAST_HELP,
        ),
        *((_editor.EditorParameter(
            "cheap_runtime",
            "cheap-runtime",
            _editor._format_bool(config.cheap_runtime),
            (
                _editor.EditorOption("true", "cheap_runtime", True),
                _editor.EditorOption("false", "cheap_runtime", False),
            ),
            help_text=(
                "true lets cheap triage (gpt-5.6-luna through your Codex login by default; "
                "BELLO_RUNTIME_TRIAGE_MODEL overrides it) dismiss routine runtime checks. Human messages, "
                "approvals, and mandatory checks bypass it. If that route is unavailable, the run uses the full "
                "runtime supervisor for every check. false always uses the full runtime supervisor."
            ),
        ),) if config.runtime_enabled else ()),
        _editor.EditorParameter(
            "start_over",
            "start-over",
            _editor._format_bool(config.start_over),
            (_editor.EditorOption("true", "start_over", True), _editor.EditorOption("false", "start_over", False)),
            help_text=(
                "true deletes prior Bello logs, archives, and recovery data. false preserves them. "
                "Both start a new active run and leave project files unchanged."
            ),
        ),
        _editor.EditorParameter(
            "completion_review",
            "completion-review",
            _editor._format_bool(config.completion_review),
            (
                _editor.EditorOption("true", "completion_review", True),
                _editor.EditorOption("false", "completion_review", False),
            ),
            help_text=(
                "true sends validated coder readiness to an independent read-only reviewer. false skips the final "
                "review. Adversary remains independently configurable."
            ),
        ),
        *review_parameters,
        _editor.EditorParameter(
            "log_distiller_enabled",
            "log-distiller",
            _editor._format_bool(config.log_distiller.enabled),
            (_editor.EditorOption("true", "log_distiller_enabled", True), _editor.EditorOption("false", "log_distiller_enabled", False)),
            help_text="Use a local model bundle to select relevant tool-output excerpts before returning them to the coder.",
        ),
        *((_editor.EditorParameter(
            "distiller_model_path",
            "distiller-model",
            config.log_distiller.model_path or "automatic",
            (),
            edit_kind="optional_text",
            help_text="Leave empty to download and cache the published model. Optional local bundle override; relative paths use this project folder.",
        ),) if config.log_distiller.enabled else ()),
        _editor.EditorParameter(
            "clean",
            "clean",
            _editor._format_bool(config.clean),
            (_editor.EditorOption("false", "clean", False), _editor.EditorOption("true", "clean", True)),
            help_text=(
                "DANGER: before launch, delete everything in the project folder except the task file and "
                "protected paths, including .git. Use only in a disposable folder."
            ),
        ),
        _editor.EditorParameter(
            "protected_path",
            "protected-path",
            ", ".join(config.protected_path) if config.protected_path else "absent",
            tuple(protected_options),
            help_text=(
                "Paths kept out of the coder snapshot and rejected by approval and final-patch checks. "
                "They are also preserved by clean. Use for hidden tests, goldens, and grading files."
            ),
        ),
    )


def _speed_option_label(value: str, config: ProjectConfig, catalog: ModelCatalog) -> str:
    from supervisor import config_editor as _editor

    if value != "fast":
        return value
    blocked = list(dict.fromkeys(
        use.model for use in _editor.project_profiles(config)
        if _editor.fast_support(use.model, catalog.get(use.model)) == "unsupported"
    ))
    if not blocked:
        return value
    more = f" +{len(blocked) - 1}" if len(blocked) > 1 else ""
    return f"fast - not offered by {blocked[0]}{more}"


def _multi_agent_parameters(
    config: ProjectConfig,
    models: tuple[str, ...],
    *,
    config_field: str,
    owner: str,
    label_prefix: str,
    catalog: ModelCatalog | None = None,
) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    settings = getattr(config, config_field)
    field_prefix = config_field
    allowed_field_prefix = f"{field_prefix}_allowed:"
    default_role = f"{label_prefix}subagent-default"
    toggle = _editor.EditorParameter(
        f"{field_prefix}_enabled",
        f"{label_prefix}multi-agent",
        "on" if settings.enabled else "off",
        (
            _editor.EditorOption("on", f"{field_prefix}_enabled", True),
            _editor.EditorOption("off", f"{field_prefix}_enabled", False),
        ),
        help_text=(
            f"on lets the {owner} delegate bounded independent investigations to subagents using only the "
            f"configured models and reasoning efforts. off removes subagent tools from the {owner} thread."
        ),
    )
    if not settings.enabled:
        return (toggle,)

    allowed_models = tuple(model for model in models if settings.allowed.get(model))
    unavailable_models = tuple(model for model in settings.allowed if model not in models)
    unavailable_parameters = ()
    if unavailable_models:
        connected_allowed = {model: efforts for model, efforts in settings.allowed.items() if model in models}
        unavailable_parameters = (_editor.EditorParameter(
            f"{field_prefix}_unavailable_profiles",
            f"{label_prefix}subagent-unavailable",
            str(len(unavailable_models)),
            (_editor.EditorOption("remove unavailable profiles", config_field, _editor.replace(settings, allowed=connected_allowed)),)
            if settings.default.model in connected_allowed else (),
            help_text=(
                "Saved subagent profiles are unavailable: " + ", ".join(unavailable_models) + ". "
                "Choose a connected default before removing them, or reconnect their provider. "
                "Nothing is removed automatically."
            ),
        ),)
    default_model_parameters = _editor._model_parameters(
        default_role,
        f"{field_prefix}_default_model",
        settings.default.model,
        allowed_models,
        catalog,
    )
    default_intelligence = _editor.EditorParameter(
        f"{field_prefix}_default_intelligence",
        f"{default_role}-intelligence",
        settings.default.intelligence,
        tuple(
            _editor.EditorOption(value, f"{field_prefix}_default_intelligence", value)
            for value in settings.allowed[settings.default.model]
        ),
        help_text=(
            f"Reasoning effort used when the {owner} does not explicitly choose another allowed subagent profile. "
            "A model change keeps a still-allowed effort, otherwise the nearest allowed lower level."
        ),
    )
    allowed_parameters = tuple(
        _editor.EditorParameter(
            f"{allowed_field_prefix}{model}",
            f"{label_prefix}subagent-allowed-{_editor.MODEL_VARIANT_LABELS.get(model, model)}",
            ", ".join(settings.allowed.get(model, ())) or "none",
            tuple(
                _editor.EditorOption(
                    effort,
                    f"{allowed_field_prefix}{model}",
                    effort,
                )
                for effort in _editor.intelligence_choices_for_model(model)
            ),
            help_text=(
                f"Toggle reasoning efforts the {owner} may use with {model}. The active default and the final "
                "remaining profile cannot be removed. " + _editor.model_detail(model, catalog)
            ).strip(),
        )
        for model in models
    )
    return (
        toggle,
        _editor.EditorParameter(
            f"{field_prefix}_max_concurrent",
            f"{label_prefix}subagent-max-concurrent",
            str(settings.max_concurrent),
            (),
            edit_kind="positive_int",
            help_text=f"Maximum number of agent threads that may run concurrently in this {owner} session.",
        ),
        *default_model_parameters,
        default_intelligence,
        *unavailable_parameters,
        *allowed_parameters,
    )


def _role_parameters(
    role: str,
    model_field: str,
    intelligence_field: str,
    selected_model: str,
    selected_intelligence: str,
    available_models: tuple[str, ...],
    catalog: ModelCatalog | None = None,
) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    choices = _editor.intelligence_choices_for_model(selected_model)
    if choices == (_editor.NO_EFFORT,):
        effort_help = (
            f"{selected_model} advertises no reasoning effort, so '{_editor.NO_EFFORT}' sends none to the "
            f"{_editor.ROLE_PURPOSES[role]}."
        )
    else:
        effort_help = (
            f"Reasoning effort (intelligence) sent to the {_editor.ROLE_PURPOSES[role]}. Choices are the levels the "
            "connected engine advertises for this exact model; higher levels allow more reasoning per turn. "
            "Changing the model keeps a supported effort, otherwise the nearest lower supported level."
        )
        if selected_model not in available_models:
            effort_help += " The model is unavailable, so its levels cannot be verified yet."
    return (
        *_editor._model_parameters(role, model_field, selected_model, available_models, catalog),
        _editor.EditorParameter(
            intelligence_field,
            f"{role}-intelligence",
            selected_intelligence,
            tuple(_editor.EditorOption(value, intelligence_field, value) for value in choices),
            help_text=effort_help,
        ),
    )


def _model_parameters(
    role: str,
    field: str,
    selected_model: str,
    available_models: tuple[str, ...],
    catalog: ModelCatalog | None = None,
) -> tuple[EditorParameter, ...]:
    from supervisor import config_editor as _editor

    available = set(available_models)
    available_56 = [model for model in _editor.GPT_5_6_MODELS if model in available]
    family_options: list[_editor.EditorOption] = []
    if _editor.MODEL_GPT_6_ASTRA in available:
        family_options.append(_editor.EditorOption(_editor.MODEL_FAMILY_ASTRA_LABEL, field, _editor.MODEL_GPT_6_ASTRA))
    if available_56:
        selected_56 = selected_model if selected_model in available_56 else available_56[0]
        family_options.append(_editor.EditorOption(_editor.MODEL_FAMILY_5_6_LABEL, field, selected_56))
    if _editor.MODEL_GPT_5_5 in available:
        family_options.append(_editor.EditorOption(_editor.MODEL_FAMILY_5_5_LABEL, field, _editor.MODEL_GPT_5_5))
    for model in sorted(available - set(_editor.SUPPORTED_MODEL_CHOICES)):
        family_options.append(_editor.EditorOption(_editor.model_label(model, catalog), field, model))

    if selected_model in available:
        value = (_editor._model_family_label(selected_model) if selected_model in _editor.SUPPORTED_MODEL_CHOICES
                 else _editor.model_label(selected_model, catalog))
    else:
        value = _editor._model_family_label(selected_model) + " (unavailable)"
    help_text = (
        f"Models from connected providers for the {_editor.ROLE_PURPOSES[role]}. "
        "Saved selections are not changed automatically."
    )
    detail = _editor.model_detail(selected_model, catalog) if selected_model in available else ""
    if detail:
        help_text += " " + detail
    if catalog is not None and catalog.failures:
        help_text += " Not connected: " + " ".join(failure.text() for failure in catalog.failures.values())
    elif not available:
        help_text += " Connect a provider with bello runtime login <provider>, then reopen this editor."
    parameters = [
        _editor.EditorParameter(
            field,
            f"{role}-mod",
            value,
            tuple(family_options),
            help_text=help_text,
        )
    ]
    if selected_model in _editor.GPT_5_6_MODELS and available_56:
        parameters.append(
            _editor.EditorParameter(
                f"{field}_variant",
                f"{role}-5.6-variant",
                _editor.MODEL_VARIANT_LABELS[selected_model],
                tuple(
                    _editor.EditorOption(_editor.MODEL_VARIANT_LABELS[model], field, model)
                    for model in _editor.GPT_5_6_MODELS
                    if model in available
                ),
                help_text=f"GPT-5.6 variant for the {_editor.ROLE_PURPOSES[role]}: Sol, Terra, or Luna.",
            )
        )
    return tuple(parameters)


def _model_family_label(model: str) -> str:
    from supervisor import config_editor as _editor

    if model == _editor.MODEL_GPT_6_ASTRA:
        return _editor.MODEL_FAMILY_ASTRA_LABEL
    if model in _editor.GPT_5_6_MODELS:
        return _editor.MODEL_FAMILY_5_6_LABEL
    if model == _editor.MODEL_GPT_5_5:
        return _editor.MODEL_FAMILY_5_5_LABEL
    return model


def _option_matches_current(config: ProjectConfig, parameter: EditorParameter, option: EditorOption) -> bool:
    from supervisor import config_editor as _editor

    if option.action is not None or option.field is None:
        return False
    if option.field == "log_distiller_enabled":
        return config.log_distiller.enabled == option.value
    multi_agent_parts = _editor._multi_agent_editor_field_parts(option.field)
    if multi_agent_parts is not None:
        config_field, setting_field = multi_agent_parts
        settings = getattr(config, config_field)
        if setting_field == "enabled":
            return settings.enabled == option.value
        if setting_field == "max_concurrent":
            return settings.max_concurrent == option.value
        if setting_field == "default_model":
            return settings.default.model == option.value
        if setting_field == "default_intelligence":
            return settings.default.intelligence == option.value
        if setting_field.startswith("allowed:"):
            model = setting_field.removeprefix("allowed:")
            return str(option.value) in settings.allowed.get(model, ())
    value = getattr(config, option.field)
    return value == option.value


def _format_bool(value: bool) -> str:
    return "true" if value else "false"
