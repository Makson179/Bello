"""Public configuration-editor API and interactive session coordinator.

The active config, frozen navigation state and animation focus belong to the
run_config_editor session. Catalog refresh is the sole writer of the two legacy
maps here; discovery returns ModelChoices carrying its sanitized catalog. Row
presentation, terminal rendering and input transitions are separate stateless
implementations. The session alone calls the explicit persistence boundary.

Public and private exports stay here, and extracted implementations resolve
runtime dependencies here, preserving existing imports and monkeypatches.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from wcwidth import wcwidth

from supervisor.runtime.client import RuntimeClient
from supervisor.config_validation import (
    CLAUDE_CODE_EFFORTS,
    FAST_HELP,
    CatalogModel,
    ConfigReport,
    ModelCatalog,
    catalog_from_model_list,
    choose_effort,
    fast_support,
    model_detail,
    model_label,
    project_profiles,
    validate_project_config,
)
from supervisor.runtime.models import NO_EFFORT, ModelSelectionError, parse_model_selection
from supervisor.project_config import (
    GPT_5_6_MODELS,
    MODEL_GPT_5_5,
    MODEL_GPT_5_6_LUNA,
    MODEL_GPT_5_6_SOL,
    MODEL_GPT_5_6_TERRA,
    MODEL_GPT_6_ASTRA,
    SPEED_CHOICES,
    SUPPORTED_MODEL_CHOICES,
    ProjectConfig,
    SubagentDefaultConfig,
    changed_project_config_fields,
    intelligence_choices_for_model as _fallback_intelligence_choices,
    load_project_config,
    project_config_path,
    sync_runtime_config_fields,
)
from supervisor.review_limits import UNLIMITED_REVIEW_LIMIT, format_review_limit


# Compatibility exports also form the late-bound dependency namespace for the
# cohesive implementations below. Keep private helpers and imported dependencies
# available to existing callers and monkeypatch targets.
from supervisor.config_editor_types import (
    EditorAction,
    InlineEditKind,
    StyledFragment,
    FragmentLine,
    FormattedRender,
    EditorOption,
    EditorParameter,
    EditorState,
)

from supervisor.config_editor_catalog import (
    ModelChoices,
    intelligence_choices_for_model,
    _editor_catalog,
    _advertised_default,
    available_model_choices,
    _model_choices_for_config,
    _normalize_model_choices,
    _available_models_from_cache,
    _available_models_from_app_server,
    _extract_model_ids,
)

from supervisor.config_editor_parameters import (
    MODEL_FAMILY_ASTRA_LABEL,
    MODEL_FAMILY_5_6_LABEL,
    MODEL_FAMILY_5_5_LABEL,
    MODEL_VARIANT_LABELS,
    ROLE_PURPOSES,
    MULTI_AGENT_CONFIG_FIELDS,
    _multi_agent_editor_field_parts,
    _is_multi_agent_allowed_field,
    parameter_defs,
    editor_report,
    _annotate_issues,
    _parameter_defs,
    _speed_option_label,
    _multi_agent_parameters,
    _role_parameters,
    _model_parameters,
    _model_family_label,
    _format_bool,
    _option_matches_current,
)

from supervisor.config_editor_updates import (
    move_down,
    move_up,
    select_current,
    advance_after_selection,
    _advance_after_parameter_change,
    _keep_parameter_expanded,
    append_inline_text,
    backspace_inline_text,
    cancel_inline_edit,
    _start_inline_edit,
    _inline_initial_value,
    _commit_inline_edit,
    _EFFORT_FIELDS,
    _effort_change_notice,
    _replace_config_field,
    _toggle_multi_agent_allowed,
    _printable_text,
    _save_config_change,
)

from supervisor.config_editor_terminal import (
    ANSI_ESCAPE_RE,
    ELLIPSIS,
    Symbols,
    Theme,
    _terminal_supports_unicode,
    LayoutSpec,
    WidthUtils,
)

from supervisor.config_editor_fragments import (
    OUTER_BORDER_GRADIENT,
    PANEL_BORDER_GRADIENT,
    ACTIVE_ROW_BG_GRADIENT,
    ICON_MOTION,
    ICON_GLOW,
    MAX_GLOW,
    ULTRA_WAVE_SPEED,
    ULTRA_WAVELENGTH,
    ULTRA_WAVEFRONT_FADE,
    ULTRA_BASE_BG,
    ULTRA_WAVE_BACKGROUNDS,
    ULTRA_WAVE_FOREGROUNDS,
    ULTRA_EDGE_LEFT,
    ULTRA_EDGE_RIGHT,
    _horizontal_line,
    _frame_line,
    _line_with_right,
    _inline_badge,
    _horizontal_border_segments,
    _gradient_segments,
    _color_style,
    _combine_body_line,
    _panel_border,
    _panel_row,
    _fit_active_row_fragments,
    _apply_background_gradient,
    _style_with_bg,
    _style_with_colors,
    _fit_fragments,
    _truncate_fragments,
    _fragment_width,
    _plain_line,
    _line_has_style,
    _join_fragment_lines,
    _parameter_icon_fragments,
    _max_glow_style,
    _apply_ultra_wave,
    _style_bg,
    _panel_line,
    _merge_styles,
)

from supervisor.config_editor_widgets import (
    EDIT_CURSOR,
    Header,
    PathBar,
    HelpLine,
    FooterStatus,
    ConfigList,
    SidePanel,
    _side_text,
    _status_lines,
    _wrapped_side_tip,
    _side_divider,
    _label_width,
    _is_effort_option,
    _is_animated_max_option,
    _is_animated_ultra_option,
    _parameter_icon,
    _ascii_symbols,
    _logo_symbol,
    _path_symbol,
    _code_symbol,
    _save_symbol,
    _enter_symbol,
    _tip_symbol,
    _icon_style_key,
    _option_branch,
    _viewport_start,
    _primary_action_hint,
    _value_style_key,
    _parameter_value_fragments,
)

from supervisor.config_editor_rendering import (
    render_editor,
    _render_editor_lines,
)


# Refreshed from the authenticated engines when opening the editor. This only
# describes selectable values; execution still validates the exact profile.
# An empty tuple means the model advertises no effort (known-empty), which is
# different from a model missing from this map (unknown).
_model_effort_catalog: dict[str, tuple[str, ...]] = {}
# Raw result of the latest discovery; consumed by available_model_choices().
_discovery: dict[str, Any] = {}


ANIMATION_INTERVAL_SECONDS = 0.10


def _prompt_toolkit_size(get_app: Any) -> tuple[int, int]:
    try:
        size = get_app().output.get_size()
    except Exception:
        terminal_size = shutil.get_terminal_size(fallback=(100, 30))
        return terminal_size.columns, terminal_size.lines
    return max(20, size.columns), max(4, size.rows)


def _config_animations_enabled() -> bool:
    return os.environ.get("BELLO_CONFIG_ANIMATIONS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def run_config_editor(project_root: Path) -> ProjectConfig:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("bello config requires an interactive terminal")
    try:
        from prompt_toolkit import Application
        from prompt_toolkit.application.current import get_app
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.layout.containers import Window
        from prompt_toolkit.styles import Style
    except ImportError as exc:
        raise RuntimeError("bello config requires prompt_toolkit; reinstall Bello with project dependencies") from exc

    config = load_project_config(project_root, create=True)
    model_choices = available_model_choices(project_root)
    path = project_config_path(project_root)
    state = EditorState()
    should_exit = False
    animations_enabled = _config_animations_enabled()
    animation_started = time.monotonic()
    animation_focus: tuple[int, int | None, int | None, bool] | None = None

    def render_current() -> FormattedRender:
        nonlocal animation_focus, animation_started
        width, height = _prompt_toolkit_size(get_app)
        now = time.monotonic()
        current_focus = (state.parameter_index, state.expanded_index, state.option_index, state.editing)
        if current_focus != animation_focus:
            animation_focus = current_focus
            animation_started = now
        animation_frame = (
            int((now - animation_started) / ANIMATION_INTERVAL_SECONDS)
            if animations_enabled
            else None
        )
        return cast(
            FormattedRender,
            render_editor(
                config,
                state,
                path,
                model_choices,
                width=width,
                height=height,
                formatted=True,
                animation_frame=animation_frame,
            ),
        )

    control = FormattedTextControl(render_current, focusable=True, show_cursor=False)
    kb = KeyBindings()

    @kb.add("down")
    def _down(event) -> None:
        nonlocal state
        state = move_down(state, parameter_defs(config, model_choices))
        event.app.invalidate()

    @kb.add("up")
    def _up(event) -> None:
        nonlocal state
        state = move_up(state, parameter_defs(config, model_choices))
        event.app.invalidate()

    @kb.add("enter")
    def _enter(event) -> None:
        nonlocal config, state
        previous_config = config
        config, state, _action = select_current(config, state, model_choices)
        _save_config_change(project_root, previous_config, config)
        if config != previous_config and state.notice is None:
            state = replace(state, notice="Saved to .supervisor/config.json.")
        event.app.invalidate()

    @kb.add("backspace")
    def _backspace(event) -> None:
        nonlocal state
        state = backspace_inline_text(state)
        event.app.invalidate()

    @kb.add("escape")
    def _escape(event) -> None:
        nonlocal state, should_exit
        if state.editing:
            state = cancel_inline_edit(state)
            event.app.invalidate()
            return
        should_exit = True
        event.app.exit()

    @kb.add("q")
    def _q(event) -> None:
        nonlocal state, should_exit
        if state.editing:
            state = append_inline_text(state, event.data)
            event.app.invalidate()
            return
        should_exit = True
        event.app.exit()

    @kb.add("c-c")
    def _ctrl_c(event) -> None:
        nonlocal should_exit
        should_exit = True
        event.app.exit()

    @kb.add("<any>")
    def _any(event) -> None:
        nonlocal state
        state = append_inline_text(state, event.data)
        event.app.invalidate()

    app = Application(
        layout=Layout(Window(content=control, wrap_lines=False)),
        key_bindings=kb,
        full_screen=True,
        style=Style.from_dict({"": "bg:#050617"}),
        refresh_interval=ANIMATION_INTERVAL_SECONDS if animations_enabled else None,
        min_redraw_interval=ANIMATION_INTERVAL_SECONDS / 2 if animations_enabled else None,
    )
    app.run()
    if should_exit:
        return config
    return config
