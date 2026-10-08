"""Pure input transitions and configuration updates, plus the save boundary.

Navigation and selections return new frozen state/config values. Only
_save_config_change persists a change, and the interactive session invokes it
with the before/after configurations so unrelated runtime fields are preserved.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from supervisor.config_editor_types import (
    EditorAction,
    EditorParameter,
    EditorState,
    InlineEditKind,
)
from supervisor.config_validation import ModelCatalog
from supervisor.project_config import ProjectConfig


def move_down(state: EditorState, parameters: tuple[EditorParameter, ...]) -> EditorState:
    from supervisor import config_editor as _editor

    if state.editing:
        return state
    if state.expanded_index == state.parameter_index:
        option_count = len(parameters[state.parameter_index].options)
        if state.option_index is None and option_count:
            return _editor.replace(state, option_index=0)
        if state.option_index is not None and state.option_index + 1 < option_count:
            return _editor.replace(state, option_index=state.option_index + 1)
    next_index = min(state.parameter_index + 1, len(parameters) - 1)
    return _editor.EditorState(parameter_index=next_index)


def move_up(state: EditorState, parameters: tuple[EditorParameter, ...]) -> EditorState:
    from supervisor import config_editor as _editor

    if state.editing:
        return state
    if state.expanded_index == state.parameter_index and state.option_index is not None:
        if state.option_index > 0:
            return _editor.replace(state, option_index=state.option_index - 1)
        return _editor.replace(state, option_index=None)
    previous_index = max(state.parameter_index - 1, 0)
    return _editor.EditorState(parameter_index=previous_index)


def select_current(
    config: ProjectConfig,
    state: EditorState,
    model_choices: tuple[str, ...] | None = None,
) -> tuple[ProjectConfig, EditorState, EditorAction | None]:
    from supervisor import config_editor as _editor

    parameters = _editor.parameter_defs(config, model_choices)
    parameter = parameters[state.parameter_index]
    if state.editing:
        updated, updated_state = _editor._commit_inline_edit(config, state, parameters)
        if updated_state.editing:
            return updated, updated_state, None
        return updated, _editor._advance_after_parameter_change(state, parameters, updated, model_choices), None
    if parameter.edit_kind is not None and state.option_index is None:
        return config, _editor._start_inline_edit(config, state, parameter), None
    if not parameter.options:
        return config, state, None
    if state.expanded_index != state.parameter_index:
        return config, _editor.replace(state, expanded_index=state.parameter_index, option_index=None), None
    if state.option_index is None:
        return config, _editor.replace(state, expanded_index=None), None

    option = parameter.options[state.option_index]
    if option.action == "add_protected_path":
        return config, _editor._start_inline_edit(config, state, parameter, edit_kind="protected_path_entry", initial_value=""), None
    if option.field is None:
        return config, _editor.advance_after_selection(state, len(parameters)), None
    if _editor._is_multi_agent_allowed_field(option.field):
        updated = _editor._toggle_multi_agent_allowed(config, option.field, str(option.value))
        return updated, _editor._keep_parameter_expanded(state, parameter.key, updated, model_choices), None
    catalog = getattr(model_choices, "catalog", None)
    updated = _editor._replace_config_field(config, option.field, option.value, catalog=catalog)
    next_state = _editor._advance_after_parameter_change(state, parameters, updated, model_choices)
    notice = _editor._effort_change_notice(config, updated)
    return updated, _editor.replace(next_state, notice=notice) if notice else next_state, None


def advance_after_selection(state: EditorState, parameter_count: int) -> EditorState:
    from supervisor import config_editor as _editor

    return _editor.EditorState(parameter_index=min(state.parameter_index + 1, parameter_count - 1))


def _advance_after_parameter_change(
    state: EditorState,
    previous_parameters: tuple[EditorParameter, ...],
    updated_config: ProjectConfig,
    model_choices: tuple[str, ...] | None,
) -> EditorState:
    from supervisor import config_editor as _editor

    updated_parameters = _editor.parameter_defs(updated_config, model_choices)
    if not updated_parameters:
        return _editor.EditorState()

    current_index = min(max(state.parameter_index, 0), len(previous_parameters) - 1)
    current_key = previous_parameters[current_index].key
    updated_indexes = {parameter.key: index for index, parameter in enumerate(updated_parameters)}
    if current_key in updated_indexes:
        return _editor.EditorState(parameter_index=min(updated_indexes[current_key] + 1, len(updated_parameters) - 1))

    for distance in range(1, len(previous_parameters)):
        for candidate_index in (current_index + distance, current_index - distance):
            if candidate_index < 0 or candidate_index >= len(previous_parameters):
                continue
            candidate_key = previous_parameters[candidate_index].key
            if candidate_key in updated_indexes:
                return _editor.EditorState(parameter_index=updated_indexes[candidate_key])
    return _editor.EditorState(parameter_index=len(updated_parameters) - 1)


def _keep_parameter_expanded(
    state: EditorState,
    parameter_key: str,
    updated_config: ProjectConfig,
    model_choices: tuple[str, ...] | None,
) -> EditorState:
    from supervisor import config_editor as _editor

    updated_parameters = _editor.parameter_defs(updated_config, model_choices)
    updated_index = next(
        (index for index, parameter in enumerate(updated_parameters) if parameter.key == parameter_key),
        min(state.parameter_index, len(updated_parameters) - 1),
    )
    option_count = len(updated_parameters[updated_index].options)
    option_index = state.option_index
    if option_index is not None and option_count:
        option_index = min(option_index, option_count - 1)
    return _editor.EditorState(
        parameter_index=updated_index,
        expanded_index=updated_index,
        option_index=option_index,
    )


def append_inline_text(state: EditorState, text: str) -> EditorState:
    from supervisor import config_editor as _editor

    if not state.editing or not _editor._printable_text(text):
        return state
    return _editor.replace(state, edit_value=state.edit_value + text, edit_error=None)


def backspace_inline_text(state: EditorState) -> EditorState:
    from supervisor import config_editor as _editor

    if not state.editing:
        return state
    return _editor.replace(state, edit_value=state.edit_value[:-1], edit_error=None)


def cancel_inline_edit(state: EditorState) -> EditorState:
    from supervisor import config_editor as _editor

    if not state.editing:
        return state
    return _editor.replace(state, editing=False, edit_kind=None, edit_value="", edit_error=None)


def _start_inline_edit(
    config: ProjectConfig,
    state: EditorState,
    parameter: EditorParameter,
    *,
    edit_kind: InlineEditKind | None = None,
    initial_value: str | None = None,
) -> EditorState:
    from supervisor import config_editor as _editor

    kind = edit_kind or parameter.edit_kind
    if kind is None:
        return state
    value = initial_value if initial_value is not None else _editor._inline_initial_value(config, parameter)
    return _editor.replace(
        state,
        expanded_index=None,
        option_index=None,
        editing=True,
        edit_kind=kind,
        edit_value=value,
        edit_error=None,
    )


def _inline_initial_value(config: ProjectConfig, parameter: EditorParameter) -> str:
    from supervisor import config_editor as _editor

    if parameter.key == "task":
        return config.task or ""
    if parameter.key == "adversary_runs":
        return str(config.adversary_runs if config.adversary else 0)
    if parameter.key == "distiller_model_path":
        return config.log_distiller.model_path or ""
    if parameter.key == "completion_returns_before_adversary":
        return _editor.format_review_limit(config.completion_returns_before_adversary)
    if parameter.key == "completion_returns_after_adversary":
        return _editor.format_review_limit(config.completion_returns_after_adversary)
    multi_agent_parts = _editor._multi_agent_editor_field_parts(parameter.key)
    if multi_agent_parts is not None and multi_agent_parts[1] == "max_concurrent":
        settings = getattr(config, multi_agent_parts[0])
        return str(settings.max_concurrent)
    return parameter.value if parameter.value != "absent" else ""


def _commit_inline_edit(
    config: ProjectConfig,
    state: EditorState,
    parameters: tuple[EditorParameter, ...],
) -> tuple[ProjectConfig, EditorState]:
    from supervisor import config_editor as _editor

    parameter = parameters[state.parameter_index]
    raw = state.edit_value.strip()
    if state.edit_kind == "optional_text":
        updated = _editor._replace_config_field(config, parameter.key, raw or None)
        return updated, _editor.advance_after_selection(state, len(parameters))
    if state.edit_kind == "protected_path_entry":
        updated = config
        if raw:
            updated = _editor.replace(config, protected_path=tuple([*config.protected_path, raw]))
        return updated, _editor.advance_after_selection(state, len(parameters))
    if state.edit_kind == "non_negative_int":
        if not raw.isdecimal():
            return config, _editor.replace(state, edit_error="enter a non-negative integer")
        updated = _editor._replace_config_field(config, parameter.key, int(raw))
        return updated, _editor.advance_after_selection(state, len(parameters))
    if state.edit_kind == "positive_int":
        if not raw.isdecimal() or int(raw) < 1:
            return config, _editor.replace(state, edit_error="enter a positive integer")
        updated = _editor._replace_config_field(config, parameter.key, int(raw))
        return updated, _editor.advance_after_selection(state, len(parameters))
    if state.edit_kind == "review_limit":
        if raw.lower() == _editor.UNLIMITED_REVIEW_LIMIT:
            value: int | str = _editor.UNLIMITED_REVIEW_LIMIT
        elif raw.isdecimal():
            value = int(raw)
        else:
            return config, _editor.replace(state, edit_error="enter 0 or a positive integer, or Unlimited")
        updated = _editor._replace_config_field(config, parameter.key, value)
        return updated, _editor.advance_after_selection(state, len(parameters))
    return config, _editor.cancel_inline_edit(state)


_EFFORT_FIELDS = {
    "coder_intelligence": ("coder", "coder_mod"),
    "revision_coder_intelligence": ("revision-coder", "revision_coder_mod"),
    "runtime_intelligence": ("runtime", "runtime_mod"),
    "completion_intelligence": ("completion", "completion_mod"),
    "adversary_intelligence": ("adversary", "adversary_mod"),
}


def _effort_change_notice(before: ProjectConfig, after: ProjectConfig) -> str | None:
    """Describe any effort Bello had to change because the new model lacks it."""
    from supervisor import config_editor as _editor

    changes = []
    for effort_field, (role, model_field) in _editor._EFFORT_FIELDS.items():
        old, new = getattr(before, effort_field), getattr(after, effort_field)
        model = getattr(after, model_field)
        if old != new and getattr(before, model_field) != model:
            changes.append(f"{role} effort {old} -> {new} ({old} is not offered by {model})")
    for field_name in _editor.MULTI_AGENT_CONFIG_FIELDS:
        old_default, new_default = getattr(before, field_name).default, getattr(after, field_name).default
        if old_default.model != new_default.model and old_default.intelligence != new_default.intelligence:
            changes.append(
                f"{field_name.replace('_', '-')} default effort {old_default.intelligence} -> "
                f"{new_default.intelligence} ({old_default.intelligence} is not allowed for {new_default.model})"
            )
    return "Saved; " + "; ".join(changes) if changes else None


def _replace_config_field(
    config: ProjectConfig, field: str, value: Any, *, catalog: ModelCatalog | None = None,
) -> ProjectConfig:
    from supervisor import config_editor as _editor

    if field == "log_distiller_enabled":
        return _editor.replace(config, log_distiller=_editor.replace(config.log_distiller, enabled=bool(value)))
    if field == "distiller_model_path":
        return _editor.replace(config, log_distiller=_editor.replace(config.log_distiller, model_path=value))
    multi_agent_parts = _editor._multi_agent_editor_field_parts(field)
    if multi_agent_parts is not None:
        config_field, setting_field = multi_agent_parts
        settings = getattr(config, config_field)
    else:
        config_field = setting_field = ""
        settings = None
    if setting_field == "enabled":
        return _editor.replace(config, **{config_field: _editor.replace(settings, enabled=bool(value))})
    if setting_field == "max_concurrent":
        return _editor.replace(config, **{config_field: _editor.replace(settings, max_concurrent=int(value))})
    if setting_field == "default_model":
        model = str(value)
        # The same rule as a role's model change, within this policy's allowed efforts.
        intelligence = _editor.choose_effort(settings.default.intelligence, tuple(settings.allowed[model]),
                                     advertised_default=_editor._advertised_default(model, catalog))
        default = _editor.SubagentDefaultConfig(model=model, intelligence=intelligence)
        return _editor.replace(config, **{config_field: _editor.replace(settings, default=default)})
    if setting_field == "default_intelligence":
        default = _editor.replace(settings.default, intelligence=str(value))
        return _editor.replace(config, **{config_field: _editor.replace(settings, default=default)})
    if field == "adversary":
        enabled = bool(value)
        return _editor.replace(
            config,
            adversary=enabled,
            adversary_runs=max(1, config.adversary_runs) if enabled else config.adversary_runs,
        )
    if field == "adversary_runs":
        runs = int(value)
        return _editor.replace(config, adversary_runs=runs, adversary=runs > 0)
    model_effort_fields = {
        "coder_mod": "coder_intelligence",
        "revision_coder_mod": "revision_coder_intelligence",
        "runtime_mod": "runtime_intelligence",
        "completion_mod": "completion_intelligence",
        "adversary_mod": "adversary_intelligence",
    }
    if field in model_effort_fields:
        updated = _editor.replace(config, **{field: value})
        intelligence_field = model_effort_fields[field]
        current_intelligence = getattr(updated, intelligence_field)
        supported = _editor.intelligence_choices_for_model(str(value))
        if current_intelligence not in supported:
            # Never jump to the most expensive level: keep the nearest supported
            # level at or below the previous one (see choose_effort). The editor
            # reports the change instead of making it silently.
            chosen = _editor.choose_effort(current_intelligence, tuple(v for v in supported if v != _editor.NO_EFFORT),
                                   advertised_default=_editor._advertised_default(str(value), catalog))
            updated = _editor.replace(updated, **{intelligence_field: chosen})
        return updated
    return _editor.replace(config, **{field: value})


def _toggle_multi_agent_allowed(config: ProjectConfig, field: str, effort: str) -> ProjectConfig:
    from supervisor import config_editor as _editor

    parts = _editor._multi_agent_editor_field_parts(field)
    if parts is None or not parts[1].startswith("allowed:"):
        return config
    config_field, setting_field = parts
    settings = getattr(config, config_field)
    model = setting_field.removeprefix("allowed:")
    current = settings.allowed.get(model, ())
    if effort in current:
        total_profiles = sum(len(efforts) for efforts in settings.allowed.values())
        if total_profiles == 1 or (model == settings.default.model and effort == settings.default.intelligence):
            return config
        updated_efforts = tuple(value for value in current if value != effort)
    else:
        selected = {*current, effort}
        order = _editor.intelligence_choices_for_model(model)
        # Keep saved values the current catalog does not list (they are flagged,
        # not silently dropped) after the advertised ones.
        updated_efforts = (*(value for value in order if value in selected),
                           *(value for value in current if value not in order))

    allowed = dict(settings.allowed)
    if updated_efforts:
        allowed[model] = updated_efforts
    else:
        allowed.pop(model, None)
    return _editor.replace(config, **{config_field: _editor.replace(settings, allowed=allowed)})


def _printable_text(text: str) -> bool:
    return bool(text) and all(char >= " " and char != "\x7f" for char in text)


def _save_config_change(project_root: Path, previous_config: ProjectConfig, config: ProjectConfig) -> None:
    from supervisor import config_editor as _editor

    changed_fields = _editor.changed_project_config_fields(previous_config, config)
    if changed_fields:
        _editor.sync_runtime_config_fields(project_root, config, changed_fields)
