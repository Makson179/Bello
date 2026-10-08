"""Configuration list, contextual panels, header/footer and row decorations.

Widgets consume row descriptions and session state without mutating either.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from pathlib import Path

from supervisor.config_editor_terminal import LayoutSpec, Theme
from supervisor.config_editor_types import (
    EditorOption,
    EditorParameter,
    EditorState,
    FragmentLine,
)
from supervisor.config_validation import ConfigReport
from supervisor.project_config import ProjectConfig

EDIT_CURSOR = "▏"


class Header:
    @staticmethod
    def render(path: Path, layout: LayoutSpec, theme: Theme) -> FragmentLine:
        from supervisor import config_editor as _editor

        left: _editor.FragmentLine = [
            (_editor._merge_styles(theme.style("header"), theme.style("logo")), _editor._logo_symbol(theme)),
            (theme.style("header"), " "),
            (_editor._merge_styles(theme.style("header"), theme.style("header_title")), "BELLO PROJECT CONFIG"),
            (theme.style("header"), " "),
            *_editor._inline_badge(path.name, theme, "badge"),
        ]
        if layout.content_width < 90:
            right: _editor.FragmentLine = [
                *_editor._inline_badge(f"{theme.symbols.bullet} CONFIG LOADED", theme, "chip"),
                (theme.style("header"), " "),
                *_editor._inline_badge("ESC", theme, "keycap"),
                (_editor._merge_styles(theme.style("header"), theme.style("exit")), " / q"),
            ]
        else:
            right = [
                *_editor._inline_badge(f"{theme.symbols.bullet} CONFIG LOADED", theme, "chip"),
                (theme.style("header"), " "),
                *_editor._inline_badge("ESC", theme, "keycap"),
                (_editor._merge_styles(theme.style("header"), theme.style("exit")), " / q to exit "),
            ]
        return _editor._frame_line(
            _editor._line_with_right(left, right, layout.content_width, theme, fill_style=theme.style("header")),
            layout,
            theme,
            fill_style=theme.style("header"),
        )


class PathBar:
    @staticmethod
    def render(path: Path, layout: LayoutSpec, theme: Theme) -> FragmentLine:
        from supervisor import config_editor as _editor

        prefix_width = _editor.WidthUtils.display_width(f"  {_editor._path_symbol(theme)}  Path: ")
        path_width = max(0, layout.content_width - prefix_width - 2)
        value = _editor.WidthUtils.truncate_middle(str(path), path_width)
        return _editor._frame_line(
            _editor._fit_fragments(
                [
                    (_editor._merge_styles(theme.style("surface"), theme.style("cyan")), f"  {_editor._path_symbol(theme)}  "),
                    (_editor._merge_styles(theme.style("surface"), theme.style("white")), "Path: "),
                    (_editor._merge_styles(theme.style("surface"), theme.style("cyan")), value),
                ],
                layout.content_width,
                theme,
                fill_style=theme.style("surface"),
            ),
            layout,
            theme,
            fill_style=theme.style("surface"),
        )


class HelpLine:
    @staticmethod
    def render(layout: LayoutSpec, theme: Theme, state: EditorState) -> FragmentLine:
        from supervisor import config_editor as _editor

        style_key = "muted"
        if state.editing:
            text = "    Type value. Enter saves. Esc cancels. Backspace edits."
        elif state.notice:
            text = f"    {state.notice}"
            style_key = "yellow"
        else:
            text = "    Arrows move. Enter expands or saves (each change is saved at once). Esc/q exits."
        return _editor._frame_line(
            _editor._fit_fragments(
                [(_editor._merge_styles(theme.style("surface"), theme.style(style_key)), text)],
                layout.content_width,
                theme,
                fill_style=theme.style("surface"),
            ),
            layout,
            theme,
            fill_style=theme.style("surface"),
        )


class FooterStatus:
    @staticmethod
    def render(
        config: ProjectConfig,
        parameters: tuple[EditorParameter, ...],
        state: EditorState,
        layout: LayoutSpec,
        theme: Theme,
    ) -> FragmentLine:
        from supervisor import config_editor as _editor

        left: _editor.FragmentLine = [
            *_editor._inline_badge(_editor._code_symbol(theme), theme, "icon_badge"),
            (theme.style("footer"), " "),
            *_editor._inline_badge("JSON", theme, "json_badge"),
            (theme.style("footer"), "  "),
            (_editor._merge_styles(theme.style("footer"), theme.style("muted")), f"{len(parameters)} settings"),
            (theme.style("footer"), "  "),
        ]
        right: _editor.FragmentLine = [
            *_editor._inline_badge(_editor._save_symbol(theme), theme, "save_badge"),
            (theme.style("footer"), " "),
            (_editor._merge_styles(theme.style("footer"), theme.style("exit")), "Enter to save "),
        ]
        return _editor._frame_line(
            _editor._line_with_right(left, right, layout.content_width, theme, fill_style=theme.style("footer")),
            layout,
            theme,
            fill_style=theme.style("footer"),
        )


class ConfigList:
    @staticmethod
    def render(
        config: ProjectConfig,
        parameters: tuple[EditorParameter, ...],
        state: EditorState,
        width: int,
        height: int,
        theme: Theme,
        animation_frame: int | None = None,
    ) -> list[FragmentLine]:
        from supervisor import config_editor as _editor

        rows, active_row, animated_background_rows = _editor.ConfigList._rows(
            config,
            parameters,
            state,
            width,
            theme,
            animation_frame,
        )
        if height <= 0:
            return []
        if height == 1:
            return [_editor._fit_fragments(rows[active_row], width, theme, fill_style=theme.style("surface"))]

        label_width = _editor._label_width(parameters, max(0, width - 2))
        row_height = max(0, height - 3)
        start = _editor._viewport_start(active_row, len(rows), row_height)
        visible = rows[start : start + row_height]
        inner_width = max(0, width - 2)
        rendered: list[_editor.FragmentLine] = [_editor._panel_border(width, theme, top=True)]
        if height > 2:
            rendered.append(_editor._panel_row(_editor.ConfigList._table_header(label_width, theme), inner_width, theme))
        rendered.extend(
            _editor._panel_row(
                row,
                inner_width,
                theme,
                active=start + offset == active_row,
                preserve_active_background=start + offset in animated_background_rows,
            )
            for offset, row in enumerate(visible)
        )
        while len(rendered) < height - 1:
            rendered.append(_editor._panel_row([], inner_width, theme))
        rendered.append(_editor._panel_border(width, theme, top=False))
        return rendered[:height]

    @staticmethod
    def _rows(
        config: ProjectConfig,
        parameters: tuple[EditorParameter, ...],
        state: EditorState,
        width: int,
        theme: Theme,
        animation_frame: int | None,
    ) -> tuple[list[FragmentLine], int, set[int]]:
        from supervisor import config_editor as _editor

        rows: list[_editor.FragmentLine] = []
        active_row = 0
        animated_background_rows: set[int] = set()
        label_width = _editor._label_width(parameters, max(0, width - 2))
        inner_width = max(0, width - 2)
        for parameter_index, parameter in enumerate(parameters):
            expanded = not state.editing and state.expanded_index == parameter_index
            active_parameter = state.parameter_index == parameter_index and state.option_index is None
            if active_parameter:
                active_row = len(rows)
            rows.append(
                _editor.ConfigList._parameter_row(
                    parameter,
                    parameter_index,
                    state,
                    expanded,
                    label_width,
                    theme,
                    animation_frame,
                )
            )
            if expanded:
                for option_index, option in enumerate(parameter.options):
                    active_option = state.parameter_index == parameter_index and state.option_index == option_index
                    if active_option:
                        active_row = len(rows)
                    if _editor._is_animated_ultra_option(parameter, option, active_option, animation_frame):
                        animated_background_rows.add(len(rows))
                    rows.append(
                        _editor.ConfigList._option_row(
                            config,
                            parameter,
                            option,
                            active_option,
                            inner_width,
                            theme,
                            animation_frame,
                        )
                    )
            if parameter_index + 1 < len(parameters):
                rows.append(_editor.ConfigList._row_divider(inner_width, theme))
        return rows, active_row, animated_background_rows

    @staticmethod
    def _table_header(label_width: int, theme: Theme) -> FragmentLine:
        from supervisor import config_editor as _editor

        marker_width = 9
        return [
            (theme.style("panel_header"), " " * marker_width),
            (_editor._merge_styles(theme.style("panel_header"), theme.style("table_header")), _editor.WidthUtils.pad_right("SETTING", label_width)),
            (theme.style("panel_header"), "  "),
            (_editor._merge_styles(theme.style("panel_header"), theme.style("table_header")), "VALUE"),
        ]

    @staticmethod
    def _row_divider(width: int, theme: Theme) -> FragmentLine:
        if width <= 4:
            return [(theme.style("row_divider"), theme.symbols.horizontal * max(0, width))]
        return [
            (theme.style("panel"), "  "),
            (theme.style("row_divider"), theme.symbols.horizontal * (width - 4)),
            (theme.style("panel"), "  "),
        ]

    @staticmethod
    def _parameter_row(
        parameter: EditorParameter,
        parameter_index: int,
        state: EditorState,
        expanded: bool,
        label_width: int,
        theme: Theme,
        animation_frame: int | None,
    ) -> FragmentLine:
        from supervisor import config_editor as _editor

        active = state.parameter_index == parameter_index and state.option_index is None
        focused = state.parameter_index == parameter_index
        active_style = theme.style("active") if active else theme.style("panel")
        marker = theme.symbols.active if active else " "
        expand_marker = theme.symbols.expanded if expanded else theme.symbols.collapsed
        icon = _editor._parameter_icon(parameter.key, theme)
        name = _editor.WidthUtils.pad_right(parameter.label, label_width)
        return [
            (active_style, " "),
            (_editor._merge_styles(active_style, theme.style("active_marker" if active else "muted")), marker),
            (active_style, " "),
            (_editor._merge_styles(active_style, theme.style("violet")), expand_marker),
            (active_style, " "),
            *_editor._parameter_icon_fragments(
                icon,
                active_style,
                theme.style(_editor._icon_style_key(parameter.key)),
                focused=focused,
                animation_frame=animation_frame,
            ),
            (_editor._merge_styles(active_style, theme.style("name")), name),
            (active_style, "  "),
            *_editor._parameter_value_fragments(parameter, parameter_index, state, active_style, theme),
        ]

    @staticmethod
    def _option_row(
        config: ProjectConfig,
        parameter: EditorParameter,
        option: EditorOption,
        active: bool,
        width: int,
        theme: Theme,
        animation_frame: int | None,
    ) -> FragmentLine:
        from supervisor import config_editor as _editor

        active_style = theme.style("active") if active else theme.style("panel")
        active_marker = theme.symbols.active if active else " "
        selected_marker = theme.symbols.selected if _editor._option_matches_current(config, parameter, option) else " "
        label_style = "muted" if option.action is not None else _editor._value_style_key(parameter.key, option.label)
        rendered_label_style = _editor._merge_styles(active_style, theme.style(label_style))
        if _editor._is_animated_max_option(parameter, option, active, animation_frame):
            rendered_label_style = _editor._max_glow_style(active_style, _editor.cast(int, animation_frame))
        fragments: _editor.FragmentLine = [
            (active_style, " "),
            (_editor._merge_styles(active_style, theme.style("active_marker" if active else "muted")), active_marker),
            (active_style, "      "),
            (_editor._merge_styles(active_style, theme.style("tree")), _editor._option_branch(parameter, option, theme)),
            (active_style, " "),
            (_editor._merge_styles(active_style, theme.style("green" if selected_marker.strip() else "muted")), selected_marker),
            (active_style, "  "),
            (rendered_label_style, option.label),
        ]
        if _editor._is_animated_ultra_option(parameter, option, active, animation_frame):
            fitted = _editor._fit_fragments(fragments, width, theme, fill_style=active_style)
            fitted_text = _editor._plain_line(fitted)
            label_start = fitted_text.rfind(option.label)
            label_width = _editor.WidthUtils.display_width(option.label)
            if label_start < 0:
                source_center = width / 2
            else:
                source_center = _editor.WidthUtils.display_width(fitted_text[:label_start]) + label_width / 2
            return _editor._apply_ultra_wave(
                fitted,
                width,
                _editor.cast(int, animation_frame),
                source_center=source_center,
                source_radius=max(0.5, label_width / 2),
            )
        return fragments


class SidePanel:
    @staticmethod
    def render(
        config: ProjectConfig,
        parameters: tuple[EditorParameter, ...],
        state: EditorState,
        width: int,
        height: int,
        theme: Theme,
        report: ConfigReport | None = None,
    ) -> list[FragmentLine]:
        from supervisor import config_editor as _editor

        if width <= 0 or height <= 0:
            return []
        symbols = theme.symbols
        parameter_index = min(max(state.parameter_index, 0), max(0, len(parameters) - 1))
        parameter = parameters[parameter_index]
        tip_style = "red" if parameter.key == "clean" else "muted"
        tip_lines = _editor._wrapped_side_tip(parameter.help_text, width, theme, style_key=tip_style)
        if parameter.issue:
            # The actionable explanation comes first, at the relevant setting.
            tip_lines = [
                *_editor._wrapped_side_tip(parameter.issue, width, theme,
                                   style_key="red" if parameter.issue_level == "error" else "yellow"),
                *tip_lines,
            ]
        navigation: list[_editor.FragmentLine] = [
            _editor._side_text("NAVIGATION", theme, style_key="panel_title"),
            _editor._side_text("^ up", theme, style_key="name"),
            _editor._side_text("v down", theme, style_key="name"),
            _editor._side_text(f"{symbols.active} select", theme, style_key="name"),
            _editor._side_text(f"{_editor._enter_symbol(theme)} enter", theme, style_key="name"),
            _editor._side_text("esc back / exit", theme, style_key="name"),
        ]
        compact_navigation: list[_editor.FragmentLine] = [
            _editor._side_text(f"NAVIGATION ^/v {_editor._enter_symbol(theme)} select", theme, style_key="panel_title"),
        ]
        tips: list[_editor.FragmentLine] = [
            _editor._side_text("TIPS", theme, style_key="panel_title"),
            *tip_lines,
        ]
        status: list[_editor.FragmentLine] = [
            _editor._side_text("STATUS", theme, style_key="panel_title"),
            *_editor._status_lines(parameters, report, width, theme),
        ]
        if height < 12:
            content = tips
        elif height < 18:
            content = [
                *compact_navigation,
                _editor._side_divider(width, theme),
                *tips,
            ]
        else:
            # STATUS precedes TIPS so a long explanation cannot hide it.
            content = [
                *navigation,
                _editor._side_divider(width, theme),
                *status,
                _editor._side_divider(width, theme),
                *tips,
            ]
        if height == 1:
            return [_editor._fit_fragments(content[0], width, theme, fill_style=theme.style("panel"))]

        inner_width = max(0, width - 2)
        visible = content[: max(0, height - 2)]
        rendered: list[_editor.FragmentLine] = [_editor._panel_border(width, theme, top=True)]
        rendered.extend(_editor._panel_row(line, inner_width, theme) for line in visible)
        while len(rendered) < height - 1:
            rendered.append(_editor._panel_row([], inner_width, theme))
        rendered.append(_editor._panel_border(width, theme, top=False))
        return rendered[:height]


def _side_text(text: str, theme: Theme, *, style_key: str) -> FragmentLine:
    from supervisor import config_editor as _editor

    return [
        (theme.style("panel"), "  "),
        (_editor._merge_styles(theme.style("panel"), theme.style(style_key)), text),
    ]


def _status_lines(
    parameters: tuple[EditorParameter, ...], report: ConfigReport | None, width: int, theme: Theme,
) -> list[FragmentLine]:
    """Summarize the offline checks; never claim more than they establish."""
    from supervisor import config_editor as _editor

    selected = theme.symbols.selected
    errors = list(report.errors) if report is not None else []
    if report is None:
        titles = [parameter.issue_title for parameter in parameters
                  if parameter.issue_level == "error" and parameter.issue_title]
        if titles:
            headline, detail = "  Fix before running", titles[0]
        else:
            headline, detail = "  Checks not run", "Model access not verified"
        lines = [(headline, "red" if titles else "yellow"), (f"  {detail}", "muted")]
    elif errors:
        availability = next((issue for issue in errors if issue.category == "availability"), None)
        headline = "  Check model access" if availability else "  Fix before running"
        detail = (availability or errors[0]).title
        count = len(errors) + len(report.warnings)
        lines = [(headline, "red"), (f"  {detail}", "muted"),
                 (f"  {count} issue{'s' if count != 1 else ''} marked with !", "muted")]
    elif not report.verified:
        detail = (f"  Model discovery failed ({report.discovery_error})" if report.discovery_error
                  else "  Model discovery did not run")
        lines = [("  Not verified", "yellow"), (detail, "muted")]
    elif report.warnings:
        lines = [(f"{selected} Offline checks passed", "green"), (f"  Note: {report.warnings[0].title}", "yellow")]
    else:
        lines = [(f"{selected} Ready", "green"), ("  Offline checks passed", "muted")]
    line_width = max(8, width - 6)

    def wrapped(text: str, style: str, *, limit: int = 3) -> list[_editor.FragmentLine]:
        indent = "  " if text.startswith("  ") else ""
        parts = _editor.textwrap.wrap(text.strip(), width=line_width, break_long_words=True,
                              break_on_hyphens=False) or [""]
        if len(parts) > limit:
            parts = [*parts[: limit - 1], _editor.WidthUtils.truncate_right(" ".join(parts[limit - 1:]), line_width)]
        return [_editor._side_text(f"{indent if index == 0 else '  '}{part}", theme, style_key=style)
                for index, part in enumerate(parts)]

    rendered = [line for text, style in lines for line in wrapped(text, style)]
    for failure in (report.failures if report is not None else ())[:3]:
        rendered.extend(wrapped(f"  {failure.label}: {failure.summary}", "yellow", limit=2))
    return rendered


def _wrapped_side_tip(text: str, width: int, theme: Theme, *, style_key: str) -> list[FragmentLine]:
    from supervisor import config_editor as _editor

    line_width = max(1, width - 6)
    wrapped = _editor.textwrap.wrap(
        text,
        width=line_width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or ["No details available."]
    return [
        _editor._side_text(
            f"{_editor._tip_symbol(theme)} {line}" if index == 0 else f"  {line}",
            theme,
            style_key=style_key,
        )
        for index, line in enumerate(wrapped)
    ]


def _side_divider(width: int, theme: Theme) -> FragmentLine:
    return [
        (theme.style("panel_border"), theme.symbols.horizontal * max(0, width - 2)),
    ]


def _label_width(parameters: tuple[EditorParameter, ...], width: int) -> int:
    from supervisor import config_editor as _editor

    widest = max((_editor.WidthUtils.display_width(parameter.label) + 1 for parameter in parameters), default=8)
    return min(max(32, widest), max(24, width // 2))


def _is_effort_option(parameter: EditorParameter, option: EditorOption, value: str) -> bool:
    return "intelligence" in parameter.key and option.label.strip().lower() == value


def _is_animated_max_option(
    parameter: EditorParameter,
    option: EditorOption,
    active: bool,
    animation_frame: int | None,
) -> bool:
    from supervisor import config_editor as _editor

    return active and animation_frame is not None and _editor._is_effort_option(parameter, option, "max")


def _is_animated_ultra_option(
    parameter: EditorParameter,
    option: EditorOption,
    active: bool,
    animation_frame: int | None,
) -> bool:
    from supervisor import config_editor as _editor

    return active and animation_frame is not None and _editor._is_effort_option(parameter, option, "ultra")


def _parameter_icon(parameter_key: str, theme: Theme) -> str:
    from supervisor import config_editor as _editor

    if _editor._ascii_symbols(theme):
        return {
            "task": "T",
            "coder_mod": "C",
            "coder_mod_variant": "V",
            "revision_coder_enabled": "R",
            "revision_coder_mod": "R",
            "revision_coder_mod_variant": "V",
            "runtime_mod": "R",
            "runtime_enabled": "R",
            "log_distiller_enabled": "D",
            "distiller_model_path": "P",
            "runtime_mod_variant": "V",
            "completion_mod": "F",
            "completion_mod_variant": "V",
            "adversary_mod": "A",
            "adversary_mod_variant": "V",
            "coder_intelligence": "I",
            "revision_coder_intelligence": "I",
            "runtime_intelligence": "I",
            "completion_intelligence": "I",
            "adversary_intelligence": "I",
            "speed": "F",
            "cheap_runtime": "L",
            "start_over": "R",
            "completion_review": "V",
            "adversary": "A",
            "adversary_runs": "N",
            "completion_returns_before_adversary": "N",
            "completion_returns_after_adversary": "M",
            "clean": "X",
            "protected_path": "P",
        }.get(parameter_key, "-")
    return {
        "task": "☑",
        "coder_mod": "◇",
        "coder_mod_variant": "◇",
        "revision_coder_enabled": "↪",
        "revision_coder_mod": "↪",
        "revision_coder_mod_variant": "↪",
        "runtime_mod": "☆",
        "runtime_enabled": "☆",
        "log_distiller_enabled": "≋",
        "distiller_model_path": "⌂",
        "runtime_mod_variant": "☆",
        "completion_mod": "✓",
        "completion_mod_variant": "✓",
        "adversary_mod": "◈",
        "adversary_mod_variant": "◈",
        "coder_intelligence": "✾",
        "revision_coder_intelligence": "✾",
        "runtime_intelligence": "✾",
        "completion_intelligence": "✾",
        "adversary_intelligence": "✾",
        "speed": "⚡",
        "cheap_runtime": "☆",
        "start_over": "↻",
        "completion_review": "✓",
        "adversary": "◈",
        "adversary_runs": "#",
        "completion_returns_before_adversary": "#",
        "completion_returns_after_adversary": "#",
        "clean": "✧",
        "protected_path": "▣",
    }.get(parameter_key, "•")


def _ascii_symbols(theme: Theme) -> bool:
    return theme.symbols.top_left == "+"


def _logo_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return "S" if _editor._ascii_symbols(theme) else "◇"


def _path_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return ">" if _editor._ascii_symbols(theme) else "▣"


def _code_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return "<>" if _editor._ascii_symbols(theme) else "</>"


def _save_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return "[]" if _editor._ascii_symbols(theme) else "▥"


def _enter_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return "ret" if _editor._ascii_symbols(theme) else "↵"


def _tip_symbol(theme: Theme) -> str:
    from supervisor import config_editor as _editor

    return "*" if _editor._ascii_symbols(theme) else "◇"


def _icon_style_key(parameter_key: str) -> str:
    return {
        "task": "violet",
        "coder_mod": "violet",
        "coder_mod_variant": "violet",
        "revision_coder_enabled": "violet",
        "revision_coder_mod": "violet",
        "revision_coder_mod_variant": "violet",
        "runtime_mod": "magenta",
        "runtime_mod_variant": "magenta",
        "completion_mod": "green",
        "completion_mod_variant": "green",
        "adversary_mod": "cyan",
        "adversary_mod_variant": "cyan",
        "coder_intelligence": "magenta",
        "revision_coder_intelligence": "violet",
        "runtime_intelligence": "magenta",
        "completion_intelligence": "green",
        "adversary_intelligence": "cyan",
        "speed": "yellow",
        "cheap_runtime": "magenta",
        "start_over": "magenta",
        "completion_review": "green",
        "adversary": "green",
        "adversary_runs": "cyan",
        "completion_returns_before_adversary": "cyan",
        "completion_returns_after_adversary": "cyan",
        "clean": "red",
        "protected_path": "magenta",
    }.get(parameter_key, "muted")


def _option_branch(parameter: EditorParameter, option: EditorOption, theme: Theme) -> str:
    return theme.symbols.branch_last if option == parameter.options[-1] else theme.symbols.branch_mid


def _viewport_start(active_row: int, row_count: int, height: int) -> int:
    if height <= 0 or row_count <= height:
        return 0
    if height == 1:
        return min(active_row, row_count - height)
    half_window = max(1, height // 2)
    start = max(0, active_row - half_window)
    return min(start, row_count - height)


def _primary_action_hint(state: EditorState) -> str:
    if state.expanded_index == state.parameter_index and state.option_index is not None:
        return "Enter to save"
    if state.expanded_index == state.parameter_index:
        return "Enter to collapse"
    return "Enter to expand"


def _value_style_key(parameter_key: str, value: str) -> str:
    normalized = value.strip().lower()
    if parameter_key.endswith("_mod") or parameter_key.endswith("_mod_variant"):
        return "magenta_soft"
    if parameter_key in {
        "adversary_runs",
        "completion_returns_before_adversary",
        "completion_returns_after_adversary",
    }:
        return "cyan"
    if "intelligence" in parameter_key:
        return "violet" if normalized in {"xhigh", "max", "ultra"} else "magenta"
    if parameter_key == "speed":
        return "yellow"
    if normalized in {"true", "on"}:
        return "green"
    if normalized in {"false", "off"}:
        return "red"
    if normalized in {"", "absent"}:
        return "violet"
    return "cyan"


def _parameter_value_fragments(
    parameter: EditorParameter,
    parameter_index: int,
    state: EditorState,
    active_style: str,
    theme: Theme,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    value_style = _editor._merge_styles(active_style, theme.style(_editor._value_style_key(parameter.key, parameter.value)))
    if state.editing and state.parameter_index == parameter_index:
        fragments: _editor.FragmentLine = [
            (value_style, state.edit_value),
            (_editor._merge_styles(active_style, theme.style("active_marker")), _editor.EDIT_CURSOR),
        ]
        if state.edit_error:
            fragments.extend(
                [
                    (active_style, "  "),
                    (_editor._merge_styles(active_style, theme.style("red")), state.edit_error),
                ]
            )
        return fragments
    fragments = [(value_style, parameter.value)]
    if parameter.issue_title:
        # The row names the problem; the focused TIPS panel explains it.
        fragments.extend([
            (active_style, "  "),
            (_editor._merge_styles(active_style, theme.style("red" if parameter.issue_level == "error" else "yellow")),
             f"! {parameter.issue_title}"),
        ])
    return fragments
