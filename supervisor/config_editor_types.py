"""Immutable row descriptions and navigation/edit state for the configuration editor.

Runtime dependencies resolve through the public config_editor module so
existing imports and late monkeypatches keep their original effect. Local
imports delay that lookup until a call and avoid import-time cycles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

EditorAction = Literal["add_protected_path"]


InlineEditKind = Literal["optional_text", "non_negative_int", "positive_int", "protected_path_entry", "review_limit"]


StyledFragment = tuple[str, str]


FragmentLine = list[StyledFragment]


FormattedRender = list[StyledFragment]


@dataclass(frozen=True)
class EditorOption:
    label: str
    field: str | None = None
    value: Any = None
    action: EditorAction | None = None


@dataclass(frozen=True)
class EditorParameter:
    key: str
    label: str
    value: str
    options: tuple[EditorOption, ...]
    edit_kind: InlineEditKind | None = None
    help_text: str = ""
    # Result of the offline checks shared with run preflight, shown at the row.
    issue: str | None = None
    issue_title: str | None = None
    issue_level: Literal["error", "warning"] | None = None


@dataclass(frozen=True)
class EditorState:
    parameter_index: int = 0
    expanded_index: int | None = None
    option_index: int | None = None
    editing: bool = False
    edit_kind: InlineEditKind | None = None
    edit_value: str = ""
    edit_error: str | None = None
    # One-shot message for a change Bello made visibly (for example an effort
    # adjusted after a model change); cleared by the next navigation.
    notice: str | None = None
