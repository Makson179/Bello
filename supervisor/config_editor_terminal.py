"""Terminal symbols, theme, layout measurements and Unicode/ANSI display widths.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


ELLIPSIS = "..."


@dataclass(frozen=True)
class Symbols:
    active: str = ">"
    collapsed: str = ">"
    expanded: str = "v"
    selected: str = "*"
    horizontal: str = "-"
    vertical: str = "|"
    top_left: str = "+"
    top_right: str = "+"
    bottom_left: str = "+"
    bottom_right: str = "+"
    tee_left: str = "+"
    tee_right: str = "+"
    branch_mid: str = "|-"
    branch_last: str = "`-"
    bullet: str = "*"

    @classmethod
    def default(cls) -> Symbols:
        return cls.unicode()

    @classmethod
    def ascii(cls) -> Symbols:
        return cls()

    @classmethod
    def unicode(cls) -> Symbols:
        return cls(
            active="›",
            collapsed="▸",
            expanded="▾",
            selected="✦",
            horizontal="─",
            vertical="│",
            top_left="╭",
            top_right="╮",
            bottom_left="╰",
            bottom_right="╯",
            tee_left="├",
            tee_right="┤",
            branch_mid="├─",
            branch_last="└─",
            bullet="•",
        )


@dataclass(frozen=True)
class Theme:
    symbols: Symbols
    styles: dict[str, str]

    @classmethod
    def from_environment(cls) -> Theme:
        from supervisor import config_editor as _editor

        ascii_setting = _editor.os.environ.get("BELLO_CONFIG_ASCII", "").strip().lower()
        ascii_enabled = ascii_setting in {
            "1",
            "true",
            "yes",
            "on",
        } or (not ascii_setting and not _editor._terminal_supports_unicode())
        symbols = _editor.Symbols.ascii() if ascii_enabled else _editor.Symbols.default()
        styles = {
            "root": "#ddd7eb bg:#050716",
            "surface": "#ddd7eb bg:#050716",
            "header": "#f3dcff bold bg:#06081a",
            "header_title": "#f860ff bold",
            "logo": "#08ffff bold",
            "badge": "#a990ff bg:#090c22",
            "badge_border": "#30245c bg:#080820",
            "chip": "#40e880 bold bg:#090c22",
            "keycap": "#08ffff bg:#080820",
            "footer": "#d8d0ea bg:#06091c",
            "json_badge": "#08ffff bold bg:#090c22",
            "icon_badge": "#08ffff bold bg:#0b1030",
            "save_badge": "#08ffff bg:#090c22",
            "ok": "#40e880 bold",
            "exit": "#d8cdf4",
            "muted": "#8175a5",
            "muted_purple": "#7050c0",
            "cyan": "#08ffff",
            "violet": "#8078ff",
            "magenta": "#f060f8",
            "magenta_soft": "#a828b8",
            "green": "#40e880",
            "yellow": "#ffc018",
            "red": "#f84858",
            "active": "#f7f1ff bg:#100832",
            "active_dark": "#f7f1ff bg:#100832",
            "active_marker": "#08ffff bold",
            "active_glow_left": "#18f8ff bg:#100832",
            "active_glow_right": "#f060f8 bg:#100832",
            "name": "#f0eaff",
            "border": "#383080 bg:#050716",
            "border_soft": "#182850 bg:#050716",
            "border_left": "#18f8ff bg:#050716",
            "border_right": "#f060f8 bg:#050716",
            "border_bright": "#f060f8 bg:#050716",
            "panel_border": "#303878 bg:#06091c",
            "panel_border_soft": "#182850 bg:#06091c",
            "panel_active_border": "#18f8ff bg:#100832",
            "panel": "#d8d0ea bg:#06091c",
            "panel_header": "#d8d0ea bg:#070a20",
            "panel_title": "#8078ff bold",
            "row_divider": "#182850 bg:#06091c",
            "table_header": "#8078ff bold",
            "tree": "#7050c0",
            "white": "#f0eaff",
        }
        return cls(
            symbols=symbols,
            styles=styles,
        )

    def style(self, *keys: str) -> str:
        return " ".join(self.styles[key] for key in keys if key in self.styles)


def _terminal_supports_unicode() -> bool:
    from supervisor import config_editor as _editor

    encoding = getattr(_editor.sys.stdout, "encoding", None)
    if not encoding:
        return True
    try:
        "╭✦↵".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


@dataclass(frozen=True)
class LayoutSpec:
    width: int
    height: int
    content_width: int
    list_height: int
    main_width: int
    side_width: int
    side_panel: bool
    gap_width: int

    @classmethod
    def from_size(cls, width: int | None = None, height: int | None = None) -> LayoutSpec:
        from supervisor import config_editor as _editor

        terminal_size = _editor.shutil.get_terminal_size(fallback=(100, 30))
        resolved_width = max(20, width or terminal_size.columns)
        resolved_height = max(4, height or terminal_size.lines)
        side_panel = resolved_width >= 120
        content_width = max(0, resolved_width - 2)
        side_width = min(34, max(30, content_width // 5)) if side_panel else 0
        gap_width = 2 if side_panel else 0
        main_width = max(20, content_width - side_width - gap_width)
        fixed_lines = 8
        list_height = max(0, resolved_height - fixed_lines)
        return cls(
            width=resolved_width,
            height=resolved_height,
            content_width=content_width,
            list_height=list_height,
            main_width=main_width,
            side_width=side_width,
            side_panel=side_panel,
            gap_width=gap_width,
        )


class WidthUtils:
    @staticmethod
    def strip_ansi(text: str) -> str:
        from supervisor import config_editor as _editor

        return _editor.ANSI_ESCAPE_RE.sub("", text)

    @staticmethod
    def display_width(text: str) -> int:
        from supervisor import config_editor as _editor

        return sum(max(_editor.wcwidth(char), 0) for char in _editor.WidthUtils.strip_ansi(text))

    @staticmethod
    def take_start(text: str, width: int) -> str:
        from supervisor import config_editor as _editor

        if width <= 0:
            return ""
        result: list[str] = []
        used = 0
        index = 0
        while index < len(text):
            match = _editor.ANSI_ESCAPE_RE.match(text, index)
            if match is not None:
                result.append(match.group(0))
                index = match.end()
                continue
            char = text[index]
            char_width = max(_editor.wcwidth(char), 0)
            if char_width > 0 and used + char_width > width:
                break
            result.append(char)
            used += char_width
            index += 1
        return "".join(result)

    @staticmethod
    def take_end(text: str, width: int) -> str:
        from supervisor import config_editor as _editor

        if width <= 0:
            return ""
        text = _editor.WidthUtils.strip_ansi(text)
        result: list[str] = []
        used = 0
        for char in reversed(text):
            char_width = max(_editor.wcwidth(char), 0)
            if char_width > 0 and used + char_width > width:
                break
            result.append(char)
            used += char_width
        return "".join(reversed(result))

    @staticmethod
    def truncate_right(text: str, width: int, placeholder: str = ELLIPSIS) -> str:
        from supervisor import config_editor as _editor

        if width <= 0:
            return ""
        if _editor.WidthUtils.display_width(text) <= width:
            return text
        placeholder_width = _editor.WidthUtils.display_width(placeholder)
        if width <= placeholder_width:
            return _editor.WidthUtils.take_start(placeholder, width)
        prefix = _editor.WidthUtils.take_start(text, width - placeholder_width)
        if _editor.ANSI_ESCAPE_RE.search(prefix) and not prefix.endswith("\x1b[0m"):
            prefix = f"{prefix}\x1b[0m"
        return f"{prefix}{placeholder}"

    @staticmethod
    def truncate_middle(text: str, width: int, placeholder: str = ELLIPSIS) -> str:
        from supervisor import config_editor as _editor

        if width <= 0:
            return ""
        if _editor.WidthUtils.display_width(text) <= width:
            return text
        placeholder_width = _editor.WidthUtils.display_width(placeholder)
        if width <= placeholder_width:
            return _editor.WidthUtils.take_start(placeholder, width)
        available = width - placeholder_width
        left_width = max(1, available // 2)
        right_width = max(0, available - left_width)
        return f"{_editor.WidthUtils.take_start(text, left_width)}{placeholder}{_editor.WidthUtils.take_end(text, right_width)}"

    @staticmethod
    def pad_right(text: str, width: int) -> str:
        from supervisor import config_editor as _editor

        clipped = _editor.WidthUtils.truncate_right(text, width)
        padding = max(0, width - _editor.WidthUtils.display_width(clipped))
        return f"{clipped}{' ' * padding}"

    @staticmethod
    def pad_left(text: str, width: int) -> str:
        from supervisor import config_editor as _editor

        clipped = _editor.WidthUtils.truncate_right(text, width)
        padding = max(0, width - _editor.WidthUtils.display_width(clipped))
        return f"{' ' * padding}{clipped}"
