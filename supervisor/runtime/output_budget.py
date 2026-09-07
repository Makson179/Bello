"""Deterministic model-facing budgets for host tool output.

The sandbox and controller retain their own, larger evidence capture.  This
module only bounds the text copied into a provider conversation.  It performs
no summarisation: callers receive an exact prefix or suffix plus an explicit
notice describing what was omitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


OutputBudgetMode = Literal["head", "tail"]

OUTPUT_MAX_LINES = 2_000
OUTPUT_MAX_BYTES = 50 * 1024


@dataclass(frozen=True, slots=True)
class BudgetedOutput:
    """Text safe to return to a model and machine-readable truncation facts."""

    text: str
    metadata: dict[str, Any]


def budget_output(
    text: str,
    mode: OutputBudgetMode = "head",
    offset: int | None = None,
) -> BudgetedOutput:
    """Return an exact, bounded head or tail of *text*.

    The body is limited independently by UTF-8 bytes and newline-delimited
    logical lines.  A truncation notice is appended outside that body budget.
    ``offset`` is the one-based source line of a head-mode file read; when a
    byte boundary cuts a displayed line, ``nextOffset`` points back to that
    line so a continuation cannot silently skip its undisplayed suffix.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if mode not in {"head", "tail"}:
        raise ValueError("mode must be 'head' or 'tail'")
    if offset is not None and (
        isinstance(offset, bool) or not isinstance(offset, int) or offset < 1
    ):
        raise ValueError("offset must be a positive one-based integer")
    if mode == "tail" and offset is not None:
        raise ValueError("offset is supported only in head mode")

    chunks = _line_chunks(text)
    total_lines = len(chunks)
    total_bytes = _byte_length(text)
    metadata: dict[str, Any] = {
        "truncated": False,
        "truncatedBy": None,
        "totalBytes": total_bytes,
        "totalLines": total_lines,
        "returnedBytes": total_bytes,
        "returnedLines": total_lines,
        "maxBytes": OUTPUT_MAX_BYTES,
        "maxLines": OUTPUT_MAX_LINES,
        "mode": mode,
        "partialLine": False,
        "nextOffset": None,
    }
    if total_lines <= OUTPUT_MAX_LINES and total_bytes <= OUTPUT_MAX_BYTES:
        return BudgetedOutput(text=text, metadata=metadata)

    if mode == "head":
        body, truncated_by, complete_lines, partial_line = _bounded_head(chunks)
        returned_lines = complete_lines + int(partial_line)
        next_offset = None
        if offset is not None:
            # A partial boundary line must be requested again.  Otherwise the
            # next unread line follows all complete lines in this response.
            next_offset = offset + complete_lines
        direction = "first"
    else:
        body, truncated_by, partial_line = _bounded_tail(chunks)
        returned_lines = len(_line_chunks(body))
        next_offset = None
        direction = "last"

    returned_bytes = _byte_length(body)
    metadata.update(
        {
            "truncated": True,
            "truncatedBy": truncated_by,
            "returnedBytes": returned_bytes,
            "returnedLines": returned_lines,
            "partialLine": partial_line,
            "nextOffset": next_offset,
        }
    )
    notice = _truncation_notice(
        direction=direction,
        metadata=metadata,
        has_continuation=offset is not None,
    )
    rendered = f"{body}\n\n{notice}" if body else notice
    return BudgetedOutput(text=rendered, metadata=metadata)


def _bounded_head(chunks: list[str]) -> tuple[str, str, int, bool]:
    selected: list[str] = []
    remaining_bytes = OUTPUT_MAX_BYTES
    complete_lines = 0
    partial_line = False

    for chunk in chunks[:OUTPUT_MAX_LINES]:
        chunk_bytes = _byte_length(chunk)
        if chunk_bytes <= remaining_bytes:
            selected.append(chunk)
            remaining_bytes -= chunk_bytes
            complete_lines += 1
            continue

        fragment = _utf8_prefix(chunk, remaining_bytes)
        if fragment:
            selected.append(fragment)
            partial_line = True
        return "".join(selected), "bytes", complete_lines, partial_line

    # More chunks remain only when the line boundary was reached first.  A tie
    # is labelled as a line truncation because every returned line is complete.
    return "".join(selected), "lines", complete_lines, False


def _bounded_tail(chunks: list[str]) -> tuple[str, str, bool]:
    selected = "".join(chunks[-OUTPUT_MAX_LINES:])
    if _byte_length(selected) <= OUTPUT_MAX_BYTES:
        return selected, "lines", False

    body = _utf8_suffix(selected, OUTPUT_MAX_BYTES)
    cut_at = len(selected) - len(body)
    partial_line = cut_at > 0 and selected[cut_at - 1] != "\n"
    return body, "bytes", partial_line


def _line_chunks(text: str) -> list[str]:
    """Split only on LF, retaining it with the preceding logical line."""

    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while True:
        newline = text.find("\n", start)
        if newline < 0:
            if start < len(text):
                chunks.append(text[start:])
            break
        chunks.append(text[start : newline + 1])
        start = newline + 1
        if start == len(text):
            break
    return chunks


def _utf8_prefix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _utf8_suffix(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    start = len(encoded) - limit
    while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
        start += 1
    return encoded[start:].decode("utf-8")


def _byte_length(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncation_notice(
    *,
    direction: str,
    metadata: dict[str, Any],
    has_continuation: bool,
) -> str:
    reason = "50 KiB byte limit" if metadata["truncatedBy"] == "bytes" else "2000 line limit"
    notice = (
        "[bello: tool output truncated at "
        f"{reason}; showing {direction} {metadata['returnedBytes']} of "
        f"{metadata['totalBytes']} UTF-8 bytes across "
        f"{metadata['returnedLines']} of {metadata['totalLines']} lines."
    )
    if has_continuation:
        notice += f" Continue with offset={metadata['nextOffset']}."
        if metadata["partialLine"]:
            notice += " The last displayed line is partial and will be read again."
    elif metadata["mode"] == "head":
        notice += " Refine the request to retrieve omitted output."
    if metadata["mode"] == "tail" and metadata["partialLine"]:
        notice += " The first displayed line is partial."
    return notice + "]"
