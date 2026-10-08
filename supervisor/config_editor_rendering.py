"""Compose the terminal editor from immutable rows, state, validation and widgets.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from pathlib import Path

from supervisor.config_editor_terminal import LayoutSpec, Theme
from supervisor.config_editor_types import (
    EditorParameter,
    EditorState,
    FormattedRender,
    FragmentLine,
)
from supervisor.config_validation import ConfigReport
from supervisor.project_config import ProjectConfig


def render_editor(
    config: ProjectConfig,
    state: EditorState,
    path: Path,
    model_choices: tuple[str, ...] | None = None,
    *,
    width: int | None = None,
    height: int | None = None,
    formatted: bool = False,
    animation_frame: int | None = None,
) -> str | FormattedRender:
    from supervisor import config_editor as _editor

    theme = _editor.Theme.from_environment()
    layout = _editor.LayoutSpec.from_size(width, height)
    parameters = _editor.parameter_defs(config, model_choices)
    lines = _editor._render_editor_lines(
        config,
        state,
        path,
        parameters,
        layout,
        theme,
        animation_frame=animation_frame,
        report=_editor.editor_report(config, model_choices),
    )
    if formatted:
        return _editor._join_fragment_lines(lines)
    return "\n".join(_editor._plain_line(line) for line in lines)


def _render_editor_lines(
    config: ProjectConfig,
    state: EditorState,
    path: Path,
    parameters: tuple[EditorParameter, ...],
    layout: LayoutSpec,
    theme: Theme,
    *,
    animation_frame: int | None,
    report: ConfigReport | None = None,
) -> list[FragmentLine]:
    from supervisor import config_editor as _editor

    lines = [
        _editor._horizontal_line(layout, theme, top=True),
        _editor.Header.render(path, layout, theme),
        _editor._horizontal_line(layout, theme, tee=True),
        _editor.PathBar.render(path, layout, theme),
        _editor._horizontal_line(layout, theme, tee=True),
        _editor.HelpLine.render(layout, theme, state),
    ]
    config_lines = _editor.ConfigList.render(
        config,
        parameters,
        state,
        layout.main_width,
        layout.list_height,
        theme,
        animation_frame,
    )
    if layout.side_panel:
        side_lines = _editor.SidePanel.render(config, parameters, state, layout.side_width, layout.list_height, theme,
                                      report)
        body_lines = [
            _editor._frame_line(_editor._combine_body_line(left, right, layout, theme), layout, theme, fill_style=theme.style("surface"))
            for left, right in zip(config_lines, side_lines, strict=True)
        ]
    else:
        body_lines = [
            _editor._frame_line(
                _editor._fit_fragments(line, layout.content_width, theme, fill_style=theme.style("surface")),
                layout,
                theme,
                fill_style=theme.style("surface"),
            )
            for line in config_lines
        ]
    lines.extend(body_lines)
    lines.append(_editor.FooterStatus.render(config, parameters, state, layout, theme))
    lines.append(_editor._horizontal_line(layout, theme, top=False))
    return lines[: layout.height]
