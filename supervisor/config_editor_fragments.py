"""Width-aware styled-fragment composition, borders, backgrounds and animation.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from supervisor.config_editor_terminal import LayoutSpec, Theme
from supervisor.config_editor_types import FormattedRender, FragmentLine

OUTER_BORDER_GRADIENT = (
    "#18f8ff",
    "#10d0ff",
    "#1888ff",
    "#383080",
    "#8078ff",
    "#b860ff",
    "#f060f8",
)


PANEL_BORDER_GRADIENT = (
    "#182850",
    "#243068",
    "#303878",
    "#383080",
    "#303878",
)


ACTIVE_ROW_BG_GRADIENT = (
    "#100832",
)


ICON_MOTION = (0, 0, 1, 1, 1, 1, 0, 0)


ICON_GLOW = (
    "#18f8ff",
    "#10d0ff",
    "#8078ff",
    "#f060f8",
    "#f3dcff",
    "#f060f8",
    "#8078ff",
    "#10d0ff",
)


MAX_GLOW = (
    "#8078ff",
    "#a990ff",
    "#d8b8ff",
    "#f3dcff",
    "#d8b8ff",
    "#a990ff",
)


ULTRA_WAVE_SPEED = 1.35


ULTRA_WAVELENGTH = 13.0


ULTRA_WAVEFRONT_FADE = 4.0


ULTRA_BASE_BG = "#100832"


ULTRA_WAVE_BACKGROUNDS = (
    "#160a35",
    "#1d0a43",
    "#250b50",
    "#2d0c5d",
    "#370e6b",
    "#421178",
    "#4e1587",
    "#5a1a96",
    "#6721a6",
    "#742bb5",
)


ULTRA_WAVE_FOREGROUNDS = (
    "#a78cff",
    "#b294ff",
    "#bd9dff",
    "#c8a7ff",
    "#d3b1ff",
    "#debcff",
    "#e8c8ff",
    "#f0d4ff",
    "#f7e1ff",
    "#ffffff",
)


ULTRA_EDGE_LEFT = "#9d58ed bg:#210b4c"


ULTRA_EDGE_RIGHT = "#c06cff bg:#321067"


def _horizontal_line(layout: LayoutSpec, theme: Theme, *, top: bool = False, tee: bool = False) -> FragmentLine:
    from supervisor import config_editor as _editor

    symbols = theme.symbols
    if top:
        left = symbols.top_left
        right = symbols.top_right
    elif tee:
        left = symbols.tee_left
        right = symbols.tee_right
    else:
        left = symbols.bottom_left
        right = symbols.bottom_right
    return [
        (theme.style("border_left"), left),
        *_editor._horizontal_border_segments(symbols.horizontal, layout.content_width, theme),
        (theme.style("border_right"), right),
    ]


def _frame_line(
    fragments: FragmentLine,
    layout: LayoutSpec,
    theme: Theme,
    *,
    fill_style: str | None = None,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    inner = _editor._fit_fragments(fragments, layout.content_width, theme, fill_style=fill_style or theme.style("surface"))
    return [
        (theme.style("border_left"), theme.symbols.vertical),
        *inner,
        (theme.style("border_right"), theme.symbols.vertical),
    ]


def _line_with_right(
    left: FragmentLine,
    right: FragmentLine,
    width: int,
    theme: Theme,
    *,
    fill_style: str | None = None,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    fill = fill_style or theme.style("root")
    right_width = _editor._fragment_width(right)
    if right_width >= width:
        return _editor._fit_fragments(right, width, theme, fill_style=fill)
    left_width = max(0, width - right_width - 1)
    fitted_left = _editor._fit_fragments(left, left_width, theme, fill_style=fill)
    gap = max(1, width - _editor._fragment_width(fitted_left) - right_width)
    return [*fitted_left, (fill, " " * gap), *right]


def _inline_badge(text: str, theme: Theme, style_key: str) -> FragmentLine:
    from supervisor import config_editor as _editor

    return [
        (theme.style("badge_border"), " "),
        (_editor._merge_styles(theme.style("badge"), theme.style(style_key)), f" {text} "),
        (theme.style("badge_border"), " "),
    ]


def _horizontal_border_segments(symbol: str, width: int, theme: Theme) -> FragmentLine:
    from supervisor import config_editor as _editor

    return _editor._gradient_segments(symbol, width, _editor.OUTER_BORDER_GRADIENT, "#050716")


def _gradient_segments(symbol: str, width: int, colors: tuple[str, ...], bg: str) -> FragmentLine:
    from supervisor import config_editor as _editor

    if width <= 0:
        return []
    fragments: _editor.FragmentLine = []
    current_color: str | None = None
    current_text: list[str] = []
    for index in range(width):
        color_index = min(len(colors) - 1, index * len(colors) // width)
        color = colors[color_index]
        if color != current_color and current_text:
            fragments.append((_editor._color_style(_editor.cast(str, current_color), bg), "".join(current_text)))
            current_text = []
        current_color = color
        current_text.append(symbol)
    if current_text and current_color is not None:
        fragments.append((_editor._color_style(current_color, bg), "".join(current_text)))
    return fragments


def _color_style(fg: str, bg: str) -> str:
    return f"{fg} bg:{bg}"


def _combine_body_line(left: FragmentLine, right: FragmentLine, layout: LayoutSpec, theme: Theme) -> FragmentLine:
    gap = (theme.style("surface"), " " * layout.gap_width)
    return [
        *left,
        gap,
        *right,
    ]


def _panel_border(width: int, theme: Theme, *, top: bool) -> FragmentLine:
    from supervisor import config_editor as _editor

    symbols = theme.symbols
    left = symbols.top_left if top else symbols.bottom_left
    right = symbols.top_right if top else symbols.bottom_right
    inner_width = max(0, width - 2)
    return [
        (theme.style("panel_border"), left),
        *_editor._gradient_segments(symbols.horizontal, inner_width, _editor.PANEL_BORDER_GRADIENT, "#06091c"),
        (theme.style("panel_border"), right),
    ]


def _panel_row(
    fragments: FragmentLine,
    inner_width: int,
    theme: Theme,
    *,
    active: bool | None = None,
    preserve_active_background: bool = False,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    resolved_active = _editor._line_has_style(fragments, theme.style("active")) if active is None else active
    if resolved_active:
        fill_style = theme.style("active")
    elif _editor._line_has_style(fragments, theme.style("panel_header")):
        fill_style = theme.style("panel_header")
    else:
        fill_style = theme.style("panel")
    fitted = (
        _editor._fit_active_row_fragments(
            fragments,
            inner_width,
            theme,
            preserve_background=preserve_active_background,
        )
        if resolved_active
        else _editor._fit_fragments(
            fragments,
            inner_width,
            theme,
            fill_style=fill_style,
        )
    )
    if preserve_active_background:
        left_edge_style = _editor.ULTRA_EDGE_LEFT
        right_edge_style = _editor.ULTRA_EDGE_RIGHT
    else:
        left_edge_style = theme.style("active_glow_left") if resolved_active else theme.style("panel_border")
        right_edge_style = theme.style("active_glow_right") if resolved_active else theme.style("panel_border")
    return [
        (left_edge_style, theme.symbols.vertical),
        *fitted,
        (right_edge_style, theme.symbols.vertical),
    ]


def _fit_active_row_fragments(
    fragments: FragmentLine,
    width: int,
    theme: Theme,
    *,
    preserve_background: bool = False,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    fitted = _editor._fit_fragments(fragments, width, theme, fill_style=theme.style("active"))
    if preserve_background:
        return fitted
    return _editor._apply_background_gradient(fitted, max(1, width), _editor.ACTIVE_ROW_BG_GRADIENT)


def _apply_background_gradient(fragments: FragmentLine, width: int, colors: tuple[str, ...]) -> FragmentLine:
    from supervisor import config_editor as _editor

    rendered: _editor.FragmentLine = []
    column = 0
    for style, text in fragments:
        current_style: str | None = None
        current_text: list[str] = []
        for char in text:
            char_width = max(_editor.wcwidth(char), 0)
            color_index = min(len(colors) - 1, column * len(colors) // width)
            next_style = _editor._style_with_bg(style, colors[color_index])
            if next_style != current_style and current_text:
                rendered.append((_editor.cast(str, current_style), "".join(current_text)))
                current_text = []
            current_style = next_style
            current_text.append(char)
            column += char_width
        if current_text and current_style is not None:
            rendered.append((current_style, "".join(current_text)))
    return rendered


def _style_with_bg(style: str, bg: str) -> str:
    tokens = [token for token in style.split() if not token.startswith("bg:")]
    return " ".join([*tokens, f"bg:{bg}"])


def _style_with_colors(style: str, *, fg: str, bg: str) -> str:
    tokens = [
        token
        for token in style.split()
        if not token.startswith("#") and not token.startswith("fg:") and not token.startswith("bg:")
    ]
    return " ".join([fg, *tokens, f"bg:{bg}"])


def _fit_fragments(
    fragments: FragmentLine,
    width: int,
    theme: Theme,
    *,
    fill_style: str | None = None,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    if width <= 0:
        return []
    if _editor._fragment_width(fragments) > width:
        return _editor._truncate_fragments(fragments, width, theme)
    padding = width - _editor._fragment_width(fragments)
    return [*fragments, (fill_style or theme.style("root"), " " * padding)]


def _truncate_fragments(fragments: FragmentLine, width: int, theme: Theme) -> FragmentLine:
    from supervisor import config_editor as _editor

    if width <= 0:
        return []
    placeholder_width = _editor.WidthUtils.display_width(_editor.ELLIPSIS)
    if width <= placeholder_width:
        return [(_editor._merge_styles(theme.style("surface"), theme.style("muted")), _editor.WidthUtils.take_start(_editor.ELLIPSIS, width))]
    limit = width - placeholder_width
    used = 0
    result: _editor.FragmentLine = []
    for style, text in fragments:
        remaining = limit - used
        if remaining <= 0:
            break
        clipped = _editor.WidthUtils.take_start(text, remaining)
        if clipped:
            result.append((style, clipped))
            used += _editor.WidthUtils.display_width(clipped)
    result.append((_editor._merge_styles(theme.style("surface"), theme.style("muted")), _editor.ELLIPSIS))
    return _editor._fit_fragments(result, width, theme)


def _fragment_width(fragments: FragmentLine) -> int:
    from supervisor import config_editor as _editor

    return _editor.WidthUtils.display_width(_editor._plain_line(fragments))


def _plain_line(fragments: FragmentLine) -> str:
    return "".join(text for _, text in fragments)


def _line_has_style(fragments: FragmentLine, style_key: str) -> bool:
    return any(style_key in style for style, _ in fragments)


def _join_fragment_lines(lines: list[FragmentLine]) -> FormattedRender:
    from supervisor import config_editor as _editor

    fragments: _editor.FormattedRender = []
    for index, line in enumerate(lines):
        fragments.extend(line)
        if index + 1 < len(lines):
            fragments.append(("", "\n"))
    return fragments


def _parameter_icon_fragments(
    icon: str,
    row_style: str,
    icon_style: str,
    *,
    focused: bool,
    animation_frame: int | None,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    if not focused or animation_frame is None:
        return [(_editor._merge_styles(row_style, icon_style), icon), (row_style, "  ")]
    position = _editor.ICON_MOTION[animation_frame % len(_editor.ICON_MOTION)]
    glow = _editor.ICON_GLOW[animation_frame % len(_editor.ICON_GLOW)]
    return [
        (row_style, " " * position),
        (_editor._style_with_colors(_editor._merge_styles(row_style, icon_style, "bold"), fg=glow, bg=_editor._style_bg(row_style)), icon),
        (row_style, " " * (2 - position)),
    ]


def _max_glow_style(row_style: str, animation_frame: int) -> str:
    from supervisor import config_editor as _editor

    color = _editor.MAX_GLOW[animation_frame % len(_editor.MAX_GLOW)]
    emphasis = "bold" if color in {"#d8b8ff", "#f3dcff"} else ""
    return _editor._style_with_colors(_editor._merge_styles(row_style, emphasis), fg=color, bg=_editor._style_bg(row_style))


def _apply_ultra_wave(
    fragments: FragmentLine,
    width: int,
    animation_frame: int,
    *,
    source_center: float,
    source_radius: float,
) -> FragmentLine:
    from supervisor import config_editor as _editor

    if width <= 0:
        return []
    wavefront = animation_frame * _editor.ULTRA_WAVE_SPEED
    rendered: _editor.FragmentLine = []
    column = 0
    for style, text in fragments:
        current_style: str | None = None
        current_text: list[str] = []
        for char in text:
            char_width = max(_editor.wcwidth(char), 0)
            cell_center = column + max(1, char_width) / 2
            distance = max(0.0, abs(cell_center - source_center) - source_radius)
            distance_behind_front = wavefront - distance
            if distance_behind_front < -_editor.ULTRA_WAVEFRONT_FADE:
                next_style = _editor._style_with_bg(style, _editor.ULTRA_BASE_BG)
            else:
                front_activation = min(
                    1.0,
                    max(0.0, (distance_behind_front + _editor.ULTRA_WAVEFRONT_FADE) / _editor.ULTRA_WAVEFRONT_FADE),
                )
                phase = _editor.math.tau * distance_behind_front / _editor.ULTRA_WAVELENGTH
                primary_wave = (_editor.math.cos(phase) + 1.0) / 2.0
                secondary_wave = (_editor.math.cos(phase * 2.0 + 0.65) + 1.0) / 2.0
                source_glow = max(0.0, 1.0 - distance / 5.0) * 0.18
                intensity = min(
                    1.0,
                    (0.14 + primary_wave * 0.68 + secondary_wave * 0.18 + source_glow) * front_activation,
                )
                shade_index = min(
                    len(_editor.ULTRA_WAVE_BACKGROUNDS) - 1,
                    round(intensity * (len(_editor.ULTRA_WAVE_BACKGROUNDS) - 1)),
                )
                wave_style = _editor._merge_styles(style, "bold") if intensity >= 0.82 else style
                next_style = _editor._style_with_colors(
                    wave_style,
                    fg=_editor.ULTRA_WAVE_FOREGROUNDS[shade_index],
                    bg=_editor.ULTRA_WAVE_BACKGROUNDS[shade_index],
                )
            if next_style != current_style and current_text:
                rendered.append((_editor.cast(str, current_style), "".join(current_text)))
                current_text = []
            current_style = next_style
            current_text.append(char)
            column += char_width
        if current_text and current_style is not None:
            rendered.append((current_style, "".join(current_text)))
    return rendered


def _style_bg(style: str) -> str:
    for token in reversed(style.split()):
        if token.startswith("bg:"):
            return token[3:]
    return "#06091c"


def _panel_line(text: str, width: int, theme: Theme, *, style_key: str = "muted") -> FragmentLine:
    from supervisor import config_editor as _editor

    symbols = theme.symbols
    inner_width = max(0, width - 4)
    clipped = _editor.WidthUtils.pad_right(text, inner_width)
    return [
        (theme.style("panel_border"), symbols.vertical),
        (theme.style("panel"), " "),
        (_editor._merge_styles(theme.style("panel"), theme.style(style_key)), clipped),
        (theme.style("panel"), " "),
        (theme.style("panel_border"), symbols.vertical),
    ]


def _merge_styles(*styles: str) -> str:
    return " ".join(style for style in styles if style)
