from __future__ import annotations

import pytest

from supervisor.runtime.output_budget import (
    OUTPUT_MAX_BYTES,
    OUTPUT_MAX_LINES,
    budget_output,
)


NOTICE = "\n\n[bello: tool output truncated"


def body_of(rendered: str) -> str:
    return rendered.split(NOTICE, 1)[0]


def test_small_output_is_returned_verbatim_with_stable_metadata() -> None:
    result = budget_output("alpha\nβeta\n")

    assert result.text == "alpha\nβeta\n"
    assert result.metadata == {
        "truncated": False,
        "truncatedBy": None,
        "totalBytes": 12,
        "totalLines": 2,
        "returnedBytes": 12,
        "returnedLines": 2,
        "maxBytes": OUTPUT_MAX_BYTES,
        "maxLines": OUTPUT_MAX_LINES,
        "mode": "head",
        "partialLine": False,
        "nextOffset": None,
    }


def test_head_uses_first_two_thousand_complete_lines_and_next_offset() -> None:
    source = "".join(f"line-{index}\n" for index in range(OUTPUT_MAX_LINES + 1))
    result = budget_output(source, mode="head", offset=11)
    body = body_of(result.text)

    assert body == "".join(f"line-{index}\n" for index in range(OUTPUT_MAX_LINES))
    assert result.metadata["truncatedBy"] == "lines"
    assert result.metadata["returnedLines"] == OUTPUT_MAX_LINES
    assert result.metadata["nextOffset"] == 11 + OUTPUT_MAX_LINES
    assert not result.metadata["partialLine"]
    assert "Continue with offset=2011" in result.text


def test_tail_uses_last_two_thousand_complete_lines() -> None:
    source = "".join(f"{index:04d}\n" for index in range(OUTPUT_MAX_LINES + 3))
    result = budget_output(source, mode="tail")
    body = body_of(result.text)

    assert body == "".join(f"{index:04d}\n" for index in range(3, OUTPUT_MAX_LINES + 3))
    assert result.metadata["truncatedBy"] == "lines"
    assert result.metadata["returnedLines"] == OUTPUT_MAX_LINES
    assert result.metadata["nextOffset"] is None


def test_head_byte_boundary_is_valid_utf8_and_rereads_partial_file_line() -> None:
    source = "complete\n" + ("🙂" * 20_000) + "\nremaining\n"
    result = budget_output(source, mode="head", offset=40)
    body = body_of(result.text)

    assert len(body.encode("utf-8")) <= OUTPUT_MAX_BYTES
    assert "�" not in body
    assert body.startswith("complete\n🙂")
    assert not body.endswith("\n")
    assert result.metadata["truncatedBy"] == "bytes"
    assert result.metadata["partialLine"]
    assert result.metadata["returnedLines"] == 2
    assert result.metadata["nextOffset"] == 41
    assert "Continue with offset=41" in result.text
    assert "last displayed line is partial and will be read again" in result.text


def test_head_byte_boundary_between_lines_advances_to_next_line() -> None:
    first = "x" * (OUTPUT_MAX_BYTES - 1) + "\n"
    result = budget_output(first + "second\n", mode="head", offset=7)
    body = body_of(result.text)

    assert body == first
    assert len(body.encode("utf-8")) == OUTPUT_MAX_BYTES
    assert not result.metadata["partialLine"]
    assert result.metadata["returnedLines"] == 1
    assert result.metadata["nextOffset"] == 8


def test_tail_byte_boundary_keeps_exact_suffix_and_marks_partial_first_line() -> None:
    source = ("🙂" * 20_000) + "\nfinal-status\n"
    result = budget_output(source, mode="tail")
    body = body_of(result.text)

    assert len(body.encode("utf-8")) <= OUTPUT_MAX_BYTES
    assert source.endswith(body)
    assert body.endswith("\nfinal-status\n")
    assert "�" not in body
    assert result.metadata["truncatedBy"] == "bytes"
    assert result.metadata["partialLine"]
    assert "first displayed line is partial" in result.text


def test_head_without_file_offset_has_actionable_non_pagination_notice() -> None:
    result = budget_output("a" * (OUTPUT_MAX_BYTES + 1), mode="head")

    assert result.metadata["nextOffset"] is None
    assert "Refine the request to retrieve omitted output" in result.text


@pytest.mark.parametrize(
    ("args", "error", "message"),
    [
        ((b"bytes",), TypeError, "text must be a string"),
        (("text", "middle"), ValueError, "mode must be"),
        (("text", "head", 0), ValueError, "positive one-based"),
        (("text", "head", True), ValueError, "positive one-based"),
        (("text", "tail", 1), ValueError, "only in head"),
    ],
)
def test_invalid_inputs_fail_explicitly(args, error, message) -> None:
    with pytest.raises(error, match=message):
        budget_output(*args)
