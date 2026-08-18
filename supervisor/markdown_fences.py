from __future__ import annotations

import re


MarkdownFenceState = tuple[str, int] | None


def advance_markdown_fence(
    line: str,
    state: MarkdownFenceState,
) -> tuple[MarkdownFenceState, bool]:
    """Track CommonMark-style fenced blocks without interpreting their contents."""

    candidate = line.lstrip(" \t")
    if state is None:
        opening = re.match(r"^(`{3,}|~{3,})(.*)$", candidate)
        if opening is None:
            return None, False
        marker = opening.group(1)
        remainder = opening.group(2)
        if marker[0] == "`" and "`" in remainder:
            return None, False
        return (marker[0], len(marker)), True

    marker_char, opening_length = state
    closing = re.fullmatch(
        re.escape(marker_char) + "{" + str(opening_length) + r",}[ \t]*",
        candidate,
    )
    if closing is not None:
        return None, True
    return state, False
